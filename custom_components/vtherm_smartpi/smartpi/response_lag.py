"""
Response lag estimator for Smart-PI learning.

Over a learning window the room behaves as an integrator behind fast lags
(actuator, emitter, air mixing, sensor housing). After a power step the slope
settles and its ramp asymptote crosses the extrapolated passive trajectory at
t = lag (pure delay plus the sum of the lags). The estimator measures this
intercept on constant-power active episodes and exposes the settling gates of
the learning windows. The intercept fixes the mean delay of the response, not
its shape: the delivered power model splits it into the learned dead time
(pure delay) and a first-order lag for the remainder.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from typing import TYPE_CHECKING, Iterable, Tuple

from .const import (
    AB_SETTLE_ACTIVE_CAP_FRAC,
    AB_SETTLE_K_ACTIVE,
    AB_SETTLE_K_PASSIVE,
    RESPONSE_LAG_HISTORY,
    RESPONSE_LAG_B_REL_UNC,
    RESPONSE_LAG_MAX_REL_UNC,
    RESPONSE_LAG_MIN_INVERSION_SLOPE,
    RESPONSE_LAG_UNC_FLOOR_S,
    RESPONSE_LAG_MAX_S,
    RESPONSE_LAG_MIN_ESTIMATES,
    RESPONSE_LAG_MIN_CROSSINGS,
    RESPONSE_LAG_MIN_OFF_LAGS,
    RESPONSE_LAG_MIN_OFF_S,
    RESPONSE_LAG_PASSIVE_SPAN_S,
    RESPONSE_LAG_RESOLVE_EPISODES,
    RESPONSE_LAG_RESOLVABLE_FRAC,
    RESPONSE_LAG_U_TOL,
)

from ..hvac_mode import VThermHvacMode_COOL

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode

_LOGGER = logging.getLogger(__name__)

_POWER_ON = 0.01
# Two observed intervals are contiguous within this tolerance (s).
_CONTIGUITY_TOL_S = 1.0
# Floor of a residual standard deviation (C): crossing times are exact to the
# sampling of the history.
_SIGMA_FLOOR_C = 1e-4


def single_lag_intercept(t1_s: float, t2_s: float, lag_s: float) -> float:
    """Intercept of the OLS line fitted over [t1_s, t2_s] on a single-lag ramp.

    Times are relative to the power step. The response to a unit rate is
    t - lag * (1 - exp(-t / lag)); its least-squares line over the interval
    (uniform sampling) crosses the passive trajectory before t = lag while the
    interval is within the transient. The value increases with lag_s.
    """
    if lag_s <= 0.0:
        return 0.0
    width = t2_s - t1_s
    mid = 0.5 * (t1_s + t2_s)
    e1 = math.exp(-t1_s / lag_s)
    e2 = math.exp(-t2_s / lag_s)
    mean_e = lag_s * (e1 - e2) / width
    mean_te = lag_s * (e1 * (t1_s + lag_s) - e2 * (t2_s + lag_s)) / width
    beta_e = (mean_te - mid * mean_e) / (width * width / 12.0)
    slope = 1.0 + lag_s * beta_e
    mean_y = mid - lag_s + lag_s * mean_e
    return mid - mean_y / slope


def _intercept_limit(t1_s: float, t2_s: float) -> float:
    """Limit of single_lag_intercept for an infinite lag (parabolic response)."""
    width = t2_s - t1_s
    mid = 0.5 * (t1_s + t2_s)
    return mid / 2.0 - width * width / (24.0 * mid)


def invert_single_lag_intercept(
    intercept_s: float, t1_s: float, t2_s: float
) -> float | None:
    """Return the lag whose fitted intercept over [t1_s, t2_s] is intercept_s.

    None when the intercept cannot be resolved from a fit over this interval.
    """
    if t1_s < 0.0 or t2_s <= t1_s:
        return None
    if intercept_s <= 0.0:
        return 0.0
    if intercept_s >= RESPONSE_LAG_RESOLVABLE_FRAC * _intercept_limit(t1_s, t2_s):
        return None
    lo, hi = 0.0, intercept_s
    while single_lag_intercept(t1_s, t2_s, hi) < intercept_s:
        lo, hi = hi, hi * 2.0
        if hi > RESPONSE_LAG_MAX_S * 4.0:
            return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if single_lag_intercept(t1_s, t2_s, mid) < intercept_s:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _crossing_midpoints(
    history: Iterable[Tuple[float, float]],
) -> list[Tuple[float, float, bool]]:
    """Return (t, value, is_change) with single-step changes at their midpoint.

    A sensor publishing on change reports a one-step change when the
    temperature crosses the boundary between two levels: at that time the
    temperature is the midpoint of the two levels. A change of several steps
    comes from a sampling instant and is the rounded temperature at that
    time. Held values between changes are not attached to a known time.
    """
    points = list(history)
    steps = [abs(b[1] - a[1]) for a, b in zip(points, points[1:]) if b[1] != a[1]]
    single = 1.5 * min(steps) if steps else 0.0
    out: list[Tuple[float, float, bool]] = []
    prev: float | None = None
    for t, v in points:
        change = prev is not None and v != prev
        if change and abs(v - prev) <= single:
            out.append((t, 0.5 * (v + prev), True))
        else:
            out.append((t, v, change))
        prev = v
    return out


def _ols(points: list[Tuple[float, float]]) -> Tuple[float, float, float, float, float] | None:
    """Least-squares line, or None.

    Returns (slope per s, centroid t, centroid value, residual standard
    deviation, sum of squared time deviations). At least
    RESPONSE_LAG_MIN_CROSSINGS points over two distinct values.
    """
    if len(points) < RESPONSE_LAG_MIN_CROSSINGS or len({v for _, v in points}) < 2:
        return None
    n = len(points)
    tm = sum(t for t, _ in points) / n
    vm = sum(v for _, v in points) / n
    sxx = sum((t - tm) ** 2 for t, _ in points)
    if sxx <= 0.0:
        return None
    slope = sum((t - tm) * (v - vm) for t, v in points) / sxx
    ssr = sum((v - vm - slope * (t - tm)) ** 2 for t, v in points)
    sigma = max(math.sqrt(ssr / (n - 2)) if n > 2 else 0.0, _SIGMA_FLOOR_C)
    return slope, tm, vm, sigma, sxx


def _weighted_median(pairs: list[Tuple[float, float]]) -> float:
    """Median of values weighted by 1 / sigma^2."""
    ordered = sorted(pairs)
    weights = [1.0 / (sig * sig) for _, sig in ordered]
    half = 0.5 * sum(weights)
    acc = 0.0
    for (value, _), w in zip(ordered, weights):
        acc += w
        if acc >= half:
            return value
    return ordered[-1][0]


class ResponseLagEstimator:
    """Estimate the response lag and track the power steps of the learning."""

    def __init__(self, name: str) -> None:
        self._name = name
        # (lag, standard deviation) of the recent estimates, in s.
        self._estimates: deque[Tuple[float, float]] = deque(maxlen=RESPONSE_LAG_HISTORY)
        self._published: float | None = None
        self._durations: deque[float] = deque(maxlen=RESPONSE_LAG_HISTORY)
        # Active episodes ended since the start of the learning.
        self._episodes: int = 0
        self._model_hvac_mode: str | None = None
        self._last_reason: str = ""
        self._clear_runtime()

    def _clear_runtime(self) -> None:
        self._last_now: float | None = None
        self._u_filt: float | None = None
        self._interval_mean: float = 0.0
        # Applied command segments (start, power) feeding the delayed input.
        self._cmd: deque[tuple[float, float]] = deque(maxlen=512)
        self._ep_start: float | None = None
        self._ep_u0: float = 0.0
        self._ep_const: bool = False
        self._ep_off_before_s: float | None = None
        self._ep_t_ext: float | None = None
        self._off_start: float | None = None

    # ------------------------------------------------------------------
    # Published values
    # ------------------------------------------------------------------

    @property
    def lag_s(self) -> float | None:
        """Published response lag (s), or None before a first consistent set.

        The published value is the median of the last consistent set of
        estimates: an inconsistent set keeps it, so that dispersed estimates do
        not switch the delivered power model on and off.
        """
        return self._published

    def _consistent_median(self) -> float | None:
        """Weighted median of the estimates when it is precise enough, else None.

        Each estimate is weighted by its inverse variance; estimates more than
        three standard deviations away from the median are not combined. The
        combined standard deviation must not exceed RESPONSE_LAG_MAX_REL_UNC
        of the median (RESPONSE_LAG_UNC_FLOOR_S for short lags).
        """
        if len(self._estimates) < RESPONSE_LAG_MIN_ESTIMATES:
            return None
        pairs = list(self._estimates)
        median = _weighted_median(pairs)
        kept = [(v, sig) for v, sig in pairs if abs(v - median) <= 3.0 * sig]
        if len(kept) < RESPONSE_LAG_MIN_ESTIMATES:
            return None
        median = _weighted_median(kept)
        combined = 1.0 / math.sqrt(sum(1.0 / (sig * sig) for _, sig in kept))
        if combined > max(RESPONSE_LAG_MAX_REL_UNC * median, RESPONSE_LAG_UNC_FLOOR_S):
            return None
        return median

    def _refresh_published(self) -> None:
        median = self._consistent_median()
        if median is not None:
            self._published = median

    @property
    def resolved(self) -> bool:
        """Whether the delivered power model is settled for the a learning.

        It is once a lag is published, or once RESPONSE_LAG_RESOLVE_EPISODES
        active episodes have ended without a publication (the installation
        does not resolve its lag: the applied power is used).
        """
        return self._published is not None or self._episodes >= RESPONSE_LAG_RESOLVE_EPISODES

    @property
    def estimates_count(self) -> int:
        return len(self._estimates)

    @property
    def episode_median_s(self) -> float | None:
        """Median duration of the recent active episodes (s)."""
        if len(self._durations) < RESPONSE_LAG_MIN_ESTIMATES:
            return None
        return statistics.median(self._durations)

    @property
    def last_reason(self) -> str:
        return self._last_reason

    @property
    def interval_mean_power(self) -> float:
        """Mean delivered power over the last observed interval.

        It equals the applied power while the lag or the dead time is unknown.
        """
        return self._interval_mean

    @property
    def episode_start(self) -> float | None:
        """Start of the current active episode (power step), or None."""
        return self._ep_start

    @property
    def episode_power(self) -> float | None:
        """Applied power of the current episode while it is constant, or None."""
        if self._ep_start is None or not self._ep_const:
            return None
        return self._ep_u0

    def delivered_fraction(
        self, start_s: float, duration_s: float, deadtime_s: float | None
    ) -> float:
        """Mean delivered fraction over a window after a power step from rest.

        start_s is the window start after the step and duration_s its length.
        The model is the one of the delivered power: pure delay
        min(dead time, lag), then a first-order lag for the remainder. Without
        a published lag or a reliable dead time the delivered power is the
        applied power.
        """
        lag = self.lag_s
        if lag is None or lag <= 0.0 or deadtime_s is None or deadtime_s < 0.0 or duration_s <= 0.0:
            return 1.0
        delay = min(deadtime_s, lag)
        tau = lag - delay
        end = start_s + duration_s
        on = max(start_s, delay)
        if end <= on:
            return 0.0
        if tau <= 0.0:
            return (end - on) / duration_s
        integral = (end - on) - tau * (math.exp(-(on - delay) / tau) - math.exp(-(end - delay) / tau))
        return integral / duration_s

    @property
    def active_settle_s(self) -> float | None:
        """Settling time of an active window after the power step, or None."""
        lag = self.lag_s
        if lag is None:
            return None
        settle = AB_SETTLE_K_ACTIVE * lag
        episode = self.episode_median_s
        if episode is not None:
            settle = min(settle, AB_SETTLE_ACTIVE_CAP_FRAC * episode)
        return settle

    def active_gate_end(self) -> float | None:
        """Earliest start of an active window in the current episode."""
        settle = self.active_settle_s
        if settle is None or self._ep_start is None:
            return None
        return self._ep_start + settle

    def passive_gate_end(self, min_settle_s: float | None = None) -> float | None:
        """Earliest start of a passive window after the last power cut.

        The settling time is the longer of AB_SETTLE_K_PASSIVE published lags
        and min_settle_s (ignored when None or non-positive). None when neither
        is available.
        """
        if self._ep_start is not None or self._off_start is None:
            return None
        settle = None
        lag = self.lag_s
        if lag is not None:
            settle = AB_SETTLE_K_PASSIVE * lag
        if min_settle_s is not None and min_settle_s > 0.0:
            settle = min_settle_s if settle is None else max(settle, min_settle_s)
        if settle is None:
            return None
        return self._off_start + settle

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        now: float,
        dt_s: float,
        u: float,
        t_ext: float | None,
        hvac_mode: "VThermHvacMode | None",
        b: float | None,
        tin_history: Iterable[Tuple[float, float]],
        deadtime_s: float | None = None,
    ) -> None:
        """Observe the power applied over [now - dt_s, now].

        The interval convention is the one of the learning window: the power
        passed with an update is applied over the elapsed interval. b is the
        published b (None before), used only when the passive slope cannot be
        measured; deadtime_s is the reliable active dead time (None if unknown).
        """
        mode = str(hvac_mode) if hvac_mode is not None else None
        if mode is not None and mode != self._model_hvac_mode:
            if self._model_hvac_mode is not None:
                self.clear()
            self._model_hvac_mode = mode
        if dt_s <= 0.0:
            return
        if self._last_now is not None and now <= self._last_now:
            return
        t_step = now - dt_s
        if self._last_now is not None and t_step > self._last_now + _CONTIGUITY_TOL_S:
            # Unobserved time (guard cut, OFF, interval capped at one cycle):
            # the power over the gap is unknown.
            self._on_gap(t_step, b, tin_history)
        self._last_now = now
        self._advance_filter(t_step, now, u, deadtime_s)

        on = u > _POWER_ON
        if on and self._ep_start is None:
            self._ep_start = t_step
            self._ep_u0 = u
            self._ep_const = True
            self._ep_t_ext = t_ext
            self._ep_off_before_s = (
                None if self._off_start is None else t_step - self._off_start
            )
        elif on and abs(u - self._ep_u0) > RESPONSE_LAG_U_TOL:
            self._ep_const = False
        elif not on and self._ep_start is not None:
            self._end_episode(t_step, b, tin_history)
            self._off_start = t_step
        elif not on and self._off_start is None:
            self._off_start = t_step

    def _on_gap(
        self,
        t_resume: float,
        b: float | None,
        tin_history: Iterable[Tuple[float, float]],
    ) -> None:
        """Close the episode at the last observed time and forget the filter.

        A heating guard cut ends an episode this way: the power is known up to
        the last observation, not across the gap.
        """
        if self._ep_start is not None and self._last_now is not None:
            self._end_episode(self._last_now, b, tin_history)
        self._ep_start = None
        self._u_filt = None
        self._cmd.clear()
        # The power cut time is unknown: settling is counted from the resume.
        self._off_start = t_resume

    def _advance_filter(
        self, t_a: float, t_b: float, u: float, deadtime_s: float | None
    ) -> None:
        """Advance the delivered power over [t_a, t_b] (command u applied).

        Model: the command delayed by L = min(dead time, lag), then a
        first-order lag of lag - L. The split preserves the intercept; the
        dead time bounds the pure delay from above (it is detected after the
        response starts), so the model never delivers power earlier than a
        pure delay of the same intercept would.
        """
        if not self._cmd or self._cmd[-1][1] != u:
            self._cmd.append((t_a, u))
        lag = self.lag_s
        if (
            self._u_filt is None
            or lag is None
            or lag <= 0.0
            or deadtime_s is None
            or deadtime_s < 0.0
        ):
            self._u_filt = u
            self._interval_mean = u
            self._prune_cmd(t_b, 0.0)
            return
        delay = min(deadtime_s, lag)
        tau = lag - delay
        edges = [t_a]
        edges += sorted(ts + delay for ts, _ in self._cmd if t_a < ts + delay < t_b)
        edges.append(t_b)
        total = 0.0
        for x0, x1 in zip(edges, edges[1:]):
            value = self._cmd_at(x0 - delay)
            h = x1 - x0
            if tau <= 0.0:
                total += value * h
                self._u_filt = value
                continue
            gap = self._u_filt - value
            total += value * h + gap * tau * (-math.expm1(-h / tau))
            self._u_filt = value + gap * math.exp(-h / tau)
        self._interval_mean = total / (t_b - t_a)
        self._prune_cmd(t_b, delay)

    def _cmd_at(self, t: float) -> float:
        """Applied command at time t (the oldest known before the history)."""
        value = self._cmd[0][1]
        for ts, u in self._cmd:
            if ts > t:
                break
            value = u
        return value

    def _prune_cmd(self, now: float, delay: float) -> None:
        """Keep the segment active at now - delay and the later ones."""
        while len(self._cmd) >= 2 and self._cmd[1][0] <= now - delay:
            self._cmd.popleft()

    def _end_episode(
        self,
        t_end: float,
        b: float | None,
        tin_history: Iterable[Tuple[float, float]],
    ) -> None:
        t_start = self._ep_start
        self._ep_start = None
        if t_start is None:
            return
        duration = t_end - t_start
        if duration <= 0.0:
            return
        self._durations.append(duration)
        self._episodes += 1
        if not self._ep_const:
            self._last_reason = "power not constant"
            return
        # The baseline extrapolates the passive trajectory: the power of the
        # previous episode must have decayed (residual exp(-off / lag)).
        known = statistics.median(v for v, _ in self._estimates) if self._estimates else 0.0
        min_off = max(RESPONSE_LAG_MIN_OFF_S, RESPONSE_LAG_MIN_OFF_LAGS * known)
        if self._ep_off_before_s is None or self._ep_off_before_s < min_off:
            self._last_reason = "passive baseline not settled"
            return
        if self._ep_t_ext is None:
            self._last_reason = "no outdoor temperature"
            return
        estimate = self._estimate(
            t_start, t_end, b, min(self._ep_off_before_s, 2.0 * RESPONSE_LAG_PASSIVE_SPAN_S),
            _crossing_midpoints(tin_history),
        )
        if estimate is not None:
            self._estimates.append(estimate)
            self._refresh_published()
            _LOGGER.debug(
                "%s - response lag estimate %.0f +/- %.0f s (published %s)",
                self._name, estimate[0], estimate[1], self.lag_s,
            )

    def _estimate(
        self,
        t_start: float,
        t_end: float,
        b: float | None,
        off_s: float,
        history: list[Tuple[float, float, bool]],
    ) -> Tuple[float, float] | None:
        """Return (lag, standard deviation) of one episode, or None."""
        # Import here to avoid circular imports at module level
        from .ab_estimator import ABEstimator

        # Only the level crossings are used: at a crossing the temperature is
        # the midpoint of the two levels, while held values form a staircase
        # whose least-squares slope is biased low when it spans few levels.
        tail = [p[:2] for p in history if p[2] and t_start - 0.5 * off_s <= p[0] <= t_start]
        t_mid = t_start + 0.5 * (t_end - t_start)
        late = [p[:2] for p in history if p[2] and t_mid <= p[0] <= t_end]
        late_fit = _ols(late)
        if late_fit is None:
            self._last_reason = "late crossings insufficient"
            return None
        slope, tw, yw, sigma_late, sxx_late = late_fit
        if not tail:
            self._last_reason = "no baseline crossing"
            return None
        # Passive baseline through the crossings of the preceding OFF period,
        # with their measured slope when robust, else the published b.
        t0 = sum(p[0] for p in tail) / len(tail)
        y0 = sum(p[1] for p in tail) / len(tail)
        passive_min, _, _ = ABEstimator.robust_dTdt_per_min(tail)
        tail_fit = _ols(tail)
        if passive_min is not None and tail_fit is not None:
            passive = passive_min / 60.0
            sigma_tail = tail_fit[3]
            sigma_passive = tail_fit[3] / math.sqrt(tail_fit[4])
        elif b is not None and b > 0.0:
            passive = -b / 60.0 * (y0 - self._ep_t_ext)
            sigma_passive = RESPONSE_LAG_B_REL_UNC * abs(passive)
            if len(tail) > 1:
                ssr = sum((v - y0 - passive * (t - t0)) ** 2 for t, v in tail)
                sigma_tail = max(math.sqrt(ssr / (len(tail) - 1)), _SIGMA_FLOOR_C)
            else:
                sigma_tail = sigma_late
        else:
            self._last_reason = "passive slope unknown"
            return None
        sign = -1.0 if self._model_hvac_mode == str(VThermHvacMode_COOL) else 1.0
        rise = slope - passive
        if sign * rise <= 0.0:
            self._last_reason = "no active response"
            return None
        t_cross = (y0 - yw + slope * tw - passive * t0) / rise
        t1, t2 = late[0][0] - t_start, late[-1][0] - t_start
        lag = invert_single_lag_intercept(t_cross - t_start, t1, t2)
        if lag is None or lag > RESPONSE_LAG_MAX_S:
            self._last_reason = "intercept not resolvable"
            return None
        # Standard deviation of the intercept (first-order propagation of the
        # baseline and late line uncertainties), then of the lag through the
        # slope of the inversion, which grows within the transient.
        sigma_t = math.sqrt(
            (sigma_tail * sigma_tail) / len(tail)
            + (sigma_late * sigma_late) / len(late)
            + ((tw - t_cross) * sigma_late / math.sqrt(sxx_late)) ** 2
            + ((t_cross - t0) * sigma_passive) ** 2
        ) / abs(rise)
        h = max(1.0, 0.01 * lag)
        slope_inv = (
            single_lag_intercept(t1, t2, lag + h) - single_lag_intercept(t1, t2, max(lag - h, 0.0))
        ) / (lag + h - max(lag - h, 0.0))
        if slope_inv <= RESPONSE_LAG_MIN_INVERSION_SLOPE:
            self._last_reason = "intercept not resolvable"
            return None
        self._last_reason = "estimated"
        return lag, max(sigma_t / slope_inv, 1.0)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Forget the estimates and the tracked episode."""
        self._estimates.clear()
        self._durations.clear()
        self._episodes = 0
        self._published = None
        self._last_reason = ""
        self._clear_runtime()

    def save_state(self) -> dict:
        return {
            "estimates": [list(e) for e in self._estimates],
            "durations": list(self._durations),
            "published": self._published,
            "episodes": self._episodes,
            "hvac_mode": self._model_hvac_mode,
        }

    def load_state(self, state: dict | None) -> None:
        """Restore the estimates; the episode in progress is not attributable."""
        self.clear()
        if not state:
            return
        for value in state.get("estimates") or []:
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value)
                and value[0] >= 0.0
                and value[1] > 0.0
            ):
                self._estimates.append((float(value[0]), float(value[1])))
        durations = state.get("durations")
        if isinstance(durations, list):
            self._durations.extend(
                float(v) for v in durations
                if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0.0
            )
        mode = state.get("hvac_mode")
        self._model_hvac_mode = str(mode) if isinstance(mode, str) else None
        episodes = state.get("episodes")
        if isinstance(episodes, int) and episodes >= 0:
            self._episodes = episodes
        published = state.get("published")
        if isinstance(published, (int, float)) and math.isfinite(published) and published >= 0.0:
            self._published = float(published)
        self._refresh_published()
