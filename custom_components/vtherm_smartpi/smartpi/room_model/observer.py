"""Kalman observer of the room model state (pure Python).

The state (Ta, w, d) is propagated over the real elapsed time in sub-steps of
RM_STEP_S with the command actually applied (through the emitter) and the
measured outdoor temperature. It is corrected only on a new timestamped
temperature value: a repeated value (heartbeat) carries no new information.
A coarse sensor's change is read as the crossing of the quantisation boundary,
at the midpoint of its two levels. The disturbance d is a slow bounded random
walk, so heat gains or losses that the model does not explain go to d instead
of the wall state. The observer never learns parameters.

The observer runs only while SmartPI is in control in heating. A pause
(another HVAC mode, window open, power shedding, safety mode, command not
SmartPI's) or a downtime up to RM_RESUME_MAX_S keeps the state and reopens its
uncertainty; a longer one restarts the observer at the wall equilibrium.

Health checks are separate from the estimation: mean and spread of the
normalised innovations, air uncertainty, a disturbance held at its bound, an
implausible wall state, and the settling time after a restart.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from ..tar_learning.const import ID_PRECISE_Q, NIGHT_FROM_H, NIGHT_TO_H
from . import model as rm
from .const import (
    H,
    RM_AIR_SD_MAX_C,
    RM_D_SAT_MAX_S,
    RM_HORIZONS_S,
    RM_INNOV_BIAS_MAX,
    RM_INNOV_EMA,
    RM_INNOV_MIN_COUNT,
    RM_INNOV_NIS_MAX,
    RM_NIGHT_D_DEV_C_H,
    RM_NIGHT_MIN_TRACK_S,
    RM_NIGHTS_KEEP,
    RM_R_PRECISE,
    RM_RESUME_MAX_S,
    RM_SETTLE_S,
    RM_STEP_S,
    RM_WALL_EXTRA_MAX,
)
from .emitter import CommandTimeline, emitter_step
from .model import Discrete, RoomParams
from .scoring import ForecastScore

# Health codes (translated in public.py)
HEALTH_OK = "ok"
HEALTH_NO_MODEL = "no model"
HEALTH_WAITING = "waiting"
HEALTH_PAUSED = "paused"
HEALTH_SETTLING = "settling"
HEALTH_BIAS = "innovation bias"
HEALTH_SPREAD = "innovation spread"
HEALTH_UNCERTAIN = "uncertain"
HEALTH_D_BOUND = "disturbance at bound"
HEALTH_WALL = "wall implausible"

_FORECAST_STEP_S = 600.0


def is_precise(q: float | None) -> bool:
    """Sensor step small enough for the wall (SmartPI's precise-sensor rule)."""
    return q is not None and q <= ID_PRECISE_Q


class RoomObserver:
    """State of one room under published parameters."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.params: RoomParams | None = None
        self.q: float | None = None
        self._disc: Discrete | None = None
        self._disc_fc: Discrete | None = None
        self._p0: list[list[float]] | None = None
        self.timeline = CommandTimeline()
        self.ue_x = 0.0
        self.t: float | None = None
        self.x: list[float] | None = None
        self.P: list[list[float]] | None = None
        self.te: float | None = None
        self.y_last: float | None = None
        self._meas_key: Any = None
        self._pending: tuple[float, float] | None = None
        self.paused_since: float | None = None
        self.settle_until: float | None = None
        self.innov_bias = 0.0
        self.innov_nis = 1.0
        self.innov_n = 0
        self.d_sat_s = 0.0
        self.restarts = 0
        self.score = ForecastScore()
        self.nights: dict[str, list[float]] = {}
        self._resumed = False
        self._fc_cache: tuple[float, list[float]] | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def precise(self) -> bool:
        return is_precise(self.q)

    @property
    def r(self) -> float:
        if self.precise:
            return RM_R_PRECISE
        q = self.q if self.q is not None else 0.1
        return q * q / 12.0

    def configure(self, params: RoomParams | None, q: float | None) -> None:
        """Use new parameters; a change of structure restarts the state at the wall equilibrium.

        Forecasts in progress go on with the new parameters of the same structure.
        """
        old = self.params
        self.params = params
        self.q = q
        self._fc_cache = None
        if params is None or old is None or old.struct != params.struct:
            self.score.drop_pending()
        if params is None:
            self._disc = self._disc_fc = self._p0 = None
            self.x = self.P = None
            return
        self._disc = rm.discretize(params, RM_STEP_S)
        self._disc_fc = rm.discretize(params, _FORECAST_STEP_S)
        self._p0 = rm.initial_covariance(params)
        if old is None or old.struct != params.struct:
            self.x = self.P = None

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    def _new_measurement(self, now: float, temp: float, meas_key: Any, meas_t: float | None) -> None:
        if meas_key is not None and meas_key == self._meas_key:
            return
        self._meas_key = meas_key
        prev = self.y_last
        if prev is not None and temp == prev:
            return
        self.y_last = temp
        y = temp
        if prev is not None and not self.precise:
            q = self.q if self.q is not None else 0.1
            if abs(temp - prev) <= 1.5 * q:
                y = 0.5 * (temp + prev)
        self._pending = (meas_t if meas_t is not None else now, y)

    def observe(self, now: float, *, temp: float | None, meas_key: Any, meas_t: float | None,
                ext: float | None, active: bool) -> None:
        """One regulation tick: propagate to now and correct on a new value."""
        if temp is not None:
            self._new_measurement(now, float(temp), meas_key, meas_t)
        if self.params is None:
            return
        if temp is None or ext is None:
            active = False
        if self.x is None or self.t is None:
            if active:
                self.reinit(now, float(temp), float(ext))
            return
        if now < self.t:
            return
        if now - self.t > RM_RESUME_MAX_S:
            if active:
                self.reinit(now, float(temp), float(ext))
            else:
                self._pause(now)
            return
        if not active:
            self._pause(now)
        elif self.paused_since is not None:
            paused = now - self.paused_since
            self.paused_since = None
            if paused > RM_RESUME_MAX_S:
                self.reinit(now, float(temp), float(ext))
                return
            self._reopen()
        te_now = float(ext) if ext is not None else self.te
        while self.t + RM_STEP_S <= now + 1e-6:
            self._step(te_now, active)
            self._apply_pending(active)
        self._apply_pending(active)
        if self._resumed:
            self._resumed = False
            self._reopen()
        if active and self.health() == HEALTH_OK:
            self.score.maybe_start(self.t, self.x, self.y_last)

    def _pause(self, now: float) -> None:
        if self.paused_since is None:
            self.paused_since = now
            self.score.drop_pending()
        self._pending = None

    def _apply_pending(self, active: bool) -> None:
        if self._pending is None or self.t is None or self._pending[0] > self.t + 1e-6:
            return
        y = self._pending[1]
        self._pending = None
        if active:
            self._correct(y)

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------
    def reinit(self, t: float, ta: float, te: float) -> None:
        """Restart at the measured air with the wall at equilibrium and no disturbance."""
        p = self.params
        self.x = [ta, rm.equilibrium_wall(p, ta, te), 0.0]
        self.P = [list(r) for r in self._p0]
        self.t = t
        self.te = te
        self.paused_since = None
        self.settle_until = t + RM_SETTLE_S
        self.innov_bias, self.innov_nis, self.innov_n = 0.0, 1.0, 0
        self.d_sat_s = 0.0
        self.restarts += 1
        self._resumed = False
        self._pending = None
        self._fc_cache = None
        self.score.drop_pending()

    def _reopen(self) -> None:
        """Reopen the air and disturbance uncertainty after an interruption (the wall state is kept)."""
        for i in (0, 2):
            if self.P[i][i] < self._p0[i][i]:
                self.P[i][i] = self._p0[i][i]
        self.innov_bias, self.innov_nis, self.innov_n = 0.0, 1.0, 0

    def _step(self, te_now: float, tracking: bool) -> None:
        p, dsc = self.params, self._disc
        t0 = self.t
        t1 = t0 + RM_STEP_S
        v_cmd = self.timeline.mean(t0 - p.L, t1 - p.L)
        ue, self.ue_x = emitter_step(self.ue_x, v_cmd, RM_STEP_S, p.tau_e)
        te0 = self.te if self.te is not None else te_now
        v = [ue, 0.5 * (te0 + te_now), (te_now - te0) / RM_STEP_S]
        phi, gam = dsc.phi, dsc.gam
        x = self.x
        nx = [
            phi[i][0] * x[0] + phi[i][1] * x[1] + phi[i][2] * x[2]
            + gam[i][0] * v[0] + gam[i][1] * v[1] + gam[i][2] * v[2]
            for i in range(3)
        ]
        dmax = p.d_max
        nx[2] = min(dmax, max(-dmax, nx[2]))
        P = self.P
        fp = [[sum(phi[i][k] * P[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        self.P = [[sum(fp[i][k] * phi[j][k] for k in range(3)) + dsc.qd[i][j] for j in range(3)] for i in range(3)]
        self.x = nx
        self.t = t1
        self.te = te_now
        self.d_sat_s = self.d_sat_s + RM_STEP_S if abs(nx[2]) >= 0.99 * dmax else 0.0
        if tracking:
            self.score.step(phi, gam, v, dmax)
            self.score.score(t1, self.y_last)
            if not self.settling:
                self._night_track(t1, nx[2])

    def _correct(self, y: float) -> None:
        P, x = self.P, self.x
        s = P[0][0] + self.r
        if s <= 0.0:
            return
        k = [P[i][0] / s for i in range(3)]
        nu = y - x[0]
        x = [x[i] + k[i] * nu for i in range(3)]
        dmax = self.params.d_max
        x[2] = min(dmax, max(-dmax, x[2]))
        row0 = list(P[0])
        self.P = [[P[i][j] - k[i] * row0[j] for j in range(3)] for i in range(3)]
        self.x = x
        self._fc_cache = None
        z = nu / math.sqrt(s)
        self.innov_bias += RM_INNOV_EMA * (z - self.innov_bias)
        self.innov_nis += RM_INNOV_EMA * (z * z - self.innov_nis)
        self.innov_n += 1

    @property
    def settling(self) -> bool:
        return self.settle_until is not None and self.t is not None and self.t < self.settle_until

    # ------------------------------------------------------------------
    # Health and outputs
    # ------------------------------------------------------------------
    def health(self) -> str:
        if self.params is None:
            return HEALTH_NO_MODEL
        if self.x is None:
            return HEALTH_WAITING
        if self.paused_since is not None:
            return HEALTH_PAUSED
        if self.settling:
            return HEALTH_SETTLING
        if self.innov_n >= RM_INNOV_MIN_COUNT:
            if abs(self.innov_bias) > RM_INNOV_BIAS_MAX:
                return HEALTH_BIAS
            if self.innov_nis > RM_INNOV_NIS_MAX:
                return HEALTH_SPREAD
        if math.sqrt(max(self.P[0][0], 0.0)) > RM_AIR_SD_MAX_C:
            return HEALTH_UNCERTAIN
        if self.d_sat_s >= RM_D_SAT_MAX_S:
            return HEALTH_D_BOUND
        wx = self.wall_extra()
        if wx is not None and abs(wx) > RM_WALL_EXTRA_MAX:
            return HEALTH_WALL
        return HEALTH_OK

    def wall_extra(self) -> float | None:
        """Wall extra power (fraction of full power): only with a precise sensor and the wall model."""
        if self.params is None or self.x is None or self.te is None or not self.precise:
            return None
        return rm.wall_extra_power(self.params, self.x[0], self.x[1], self.te)

    def forecast(self) -> list[float] | None:
        """Air temperature at each horizon, open loop under the current command, outdoor and d held."""
        if self.params is None or self.x is None or self.t is None or self.te is None:
            return None
        if self._fc_cache is not None and self._fc_cache[0] == self.t:
            return self._fc_cache[1]
        p, dsc = self.params, self._disc_fc
        phi, gam = dsc.phi, dsc.gam
        x = list(self.x)
        xe = self.ue_x
        now = self.t
        power = self.timeline.power
        h = _FORECAST_STEP_S
        out = []
        k_next = 0
        steps = int(round(max(RM_HORIZONS_S) / h))
        for n in range(1, steps + 1):
            a, b = now + (n - 1) * h - p.L, now + n * h - p.L
            if b <= now:
                v_cmd = self.timeline.mean(a, b)
            elif a >= now:
                v_cmd = power
            else:
                v_cmd = (self.timeline.mean(a, now) * (now - a) + power * (b - now)) / h
            ue, xe = emitter_step(xe, v_cmd, h, p.tau_e)
            v = (ue, self.te, 0.0)
            x = [phi[i][0] * x[0] + phi[i][1] * x[1] + phi[i][2] * x[2]
                 + gam[i][0] * v[0] + gam[i][1] * v[1] for i in range(3)]
            while k_next < len(RM_HORIZONS_S) and n * h >= RM_HORIZONS_S[k_next] - 1e-6:
                out.append(x[0])
                k_next += 1
        self._fc_cache = (self.t, out)
        return out

    # ------------------------------------------------------------------
    # Night disturbance summary
    # ------------------------------------------------------------------
    def _night_of(self, t: float) -> int | None:
        tod = self._clock.tod_h(t)
        if tod >= NIGHT_FROM_H:
            return self._clock.day_of(t)
        if tod < NIGHT_TO_H:
            return self._clock.day_of(t) - 1
        return None

    def _night_track(self, t: float, d: float) -> None:
        n = self._night_of(t)
        if n is None:
            return
        rec = self.nights.setdefault(str(n), [0.0, 0.0])
        rec[0] += d * RM_STEP_S
        rec[1] += RM_STEP_S
        if len(self.nights) > RM_NIGHTS_KEEP:
            for key in sorted(self.nights, key=int)[:-RM_NIGHTS_KEEP]:
                del self.nights[key]

    def disturbed_nights(self) -> list[int]:
        """Nights whose mean disturbance stands out from the recent nights (detected heat gains or losses)."""
        means = {int(k): v[0] / v[1] * H for k, v in self.nights.items() if v[1] >= RM_NIGHT_MIN_TRACK_S}
        if len(means) < 3:
            return []
        med = statistics.median(means.values())
        return sorted(n for n, m in means.items() if abs(m - med) > RM_NIGHT_D_DEV_C_H)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t, "x": self.x, "P": self.P, "te": self.te, "ue_x": self.ue_x, "y_last": self.y_last,
            "paused_since": self.paused_since, "settle_until": self.settle_until, "restarts": self.restarts,
            "timeline": self.timeline.to_dict(), "score": self.score.to_dict(), "nights": self.nights,
        }

    def load(self, data: dict[str, Any] | None) -> None:
        """Restore a saved state; the heater is taken as stopped during the downtime."""
        if not isinstance(data, dict):
            return
        try:
            self.timeline = CommandTimeline.from_dict(data.get("timeline"))
            self.score = ForecastScore.from_dict(data.get("score"))
            self.nights = {str(k): [float(v[0]), float(v[1])] for k, v in (data.get("nights") or {}).items()}
            self.restarts = int(data.get("restarts") or 0)
            self.ue_x = float(data.get("ue_x") or 0.0)
            y = data.get("y_last")
            self.y_last = None if y is None else float(y)
            x, P, t, te = data.get("x"), data.get("P"), data.get("t"), data.get("te")
            if (self.params is not None and t is not None and te is not None and isinstance(x, list) and len(x) == 3
                    and isinstance(P, list) and len(P) == 3 and all(len(r) == 3 for r in P)):
                vals = [float(v) for v in x] + [float(v) for r in P for v in r] + [float(t), float(te)]
                if all(math.isfinite(v) for v in vals):
                    self.x = [float(v) for v in x]
                    self.P = [[float(v) for v in r] for r in P]
                    self.t, self.te = float(t), float(te)
                    ps, su = data.get("paused_since"), data.get("settle_until")
                    self.paused_since = None if ps is None else float(ps)
                    self.settle_until = None if su is None else float(su)
                    self._resumed = True
                    self.timeline.stop(self.t)
        except (TypeError, ValueError, IndexError):
            self.x = self.P = self.t = self.te = None
        self.score.drop_pending()
