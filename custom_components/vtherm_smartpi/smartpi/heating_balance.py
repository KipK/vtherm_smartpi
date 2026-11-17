"""
Heating balance for Smart-PI (observation only).

On a heating phase followed by a stop, the room integrates the delivered power:
the temperature rise above the passive trajectory, plus the losses over the
phase, equals the heating rate times the delivered full-power time. The
estimator measures the heating rate `a` (C/min at full power) from the rise
at the temperature peak after the cut, and the emitter lag from the heat still
missing at the cut (the emitter has not delivered all the energy commanded).

The results are published for observation: no regulation or learning reads
them.
"""
from __future__ import annotations

import logging
import math
import statistics
from typing import TYPE_CHECKING, Iterable, Tuple

from .const import (
    HEATING_BALANCE_BASELINE_SPAN_S,
    HEATING_BALANCE_HISTORY,
    HEATING_BALANCE_MIN_ON_S,
    HEATING_BALANCE_MIN_REST_S,
    HEATING_BALANCE_MIN_RISE_C,
    HEATING_BALANCE_PEAK_BIN_S,
    HEATING_BALANCE_PEAK_CONFIRM_S,
    HEATING_BALANCE_PEAK_MAX_S,
    HEATING_BALANCE_PHASE_MIN_OFF_S,
    RESPONSE_LAG_U_TOL,
)
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

from ..hvac_mode import VThermHvacMode_HEAT

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode

_LOGGER = logging.getLogger(__name__)

_POWER_ON = 0.01
# Two observed intervals are contiguous within this tolerance (s).
_CONTIGUITY_TOL_S = 1.0
# Half width of the window around the cut averaged for the temperature at the cut (s).
_CUT_WINDOW_S = 150.0
# Smallest number of samples of the baseline fit.
_BASELINE_MIN_SAMPLES = 4
# A measured lag at or beyond this fraction of the phase duration is not resolved.
_LAG_RESOLVABLE_FRAC = 0.98

_HEATING = "heating"
_CUT = "cut"

# Saved phase: field -> kind. "time" fields are saved in wall-clock time.
_PHASE_FIELDS = {
    "stage": "str",
    "context": "str",
    "reason": "str?",
    "t_start": "time",
    "rest_s": "num?",
    "base_value": "num",
    "base_slope": "num",
    "u_max": "num",
    "on_s": "num",
    "area": "num",
    "last_t": "time",
    "last_dev": "num",
    "t_cut": "time?",
    "area_cut": "num?",
    "cut_temp": "num?",
    "bin_k": "int?",
    "bin_sum": "num",
    "bin_n": "int",
    "best_k": "int?",
    "best_mean": "num?",
    "best_area": "num?",
    "last_k": "int?",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _samples_after(
    history: Iterable[Tuple[float, float]], t_from: float, t_to: float
) -> list[Tuple[float, float]]:
    """Samples with t_from < t <= t_to, oldest first, reading the history from its end."""
    try:
        reverse = reversed(history)  # type: ignore[arg-type]
    except TypeError:
        reverse = reversed(list(history))
    out: list[Tuple[float, float]] = []
    for t, v in reverse:
        if t <= t_from:
            break
        if t <= t_to:
            out.append((t, v))
    out.reverse()
    return out


def _baseline(
    history: Iterable[Tuple[float, float]], t_from: float, t_start: float
) -> Tuple[float, float] | None:
    """Least-squares line over [t_from, t_start]: (value at t_start, slope per s)."""
    points = _samples_after(history, t_from - 1e-9, t_start)
    if len(points) < _BASELINE_MIN_SAMPLES:
        return None
    n = len(points)
    tm = sum(t for t, _ in points) / n
    vm = sum(v for _, v in points) / n
    sxx = sum((t - tm) ** 2 for t, _ in points)
    if sxx <= 0.0:
        return None
    slope = sum((t - tm) * (v - vm) for t, v in points) / sxx
    return vm + slope * (t_start - tm), slope


def _lag_from_deficit(deficit_s: float, duration_s: float) -> float | None:
    """First-order lag whose delivery deficit over the phase is deficit_s (s)."""
    if deficit_s <= 0.0:
        return 0.0
    if deficit_s >= _LAG_RESOLVABLE_FRAC * duration_s:
        return None
    lo, hi = 0.0, 100.0 * duration_s
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if mid * -math.expm1(-duration_s / mid) < deficit_s:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class HeatingBalance:
    """Measure the heating rate and the emitter lag from an energy balance."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._results: list[dict] = []
        self._last_reason: str = ""
        self._clear_runtime()

    def _clear_runtime(self) -> None:
        self._last_now: float | None = None
        # Start of the current rest (monotonic s), None when unknown.
        self._rest_start: float | None = None
        # Phase in progress (see _PHASE_FIELDS), None while resting.
        self._phase: dict | None = None
        # Context saved before a restart, applied on the first observation.
        self._restore: dict | None = None

    # ------------------------------------------------------------------
    # Published values
    # ------------------------------------------------------------------

    @property
    def results(self) -> list[dict]:
        """Kept measurements, oldest first."""
        return list(self._results)

    @property
    def a_per_min(self) -> float | None:
        """Median heating rate (C/min at full power), None without measurement."""
        if not self._results:
            return None
        return statistics.median(r["a"] for r in self._results)

    @property
    def lag_s(self) -> float | None:
        """Median emitter lag (s) of the measurements that resolve it."""
        lags = [r["lag_s"] for r in self._results if r["lag_s"] is not None]
        return statistics.median(lags) if lags else None

    @property
    def count(self) -> int:
        return len(self._results)

    @property
    def state(self) -> str:
        if self._phase is None:
            return "resting"
        return "waiting for the peak" if self._phase["stage"] == _CUT else "heating"

    @property
    def last_reason(self) -> str:
        return self._last_reason

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        now: float,
        dt_s: float,
        u: float,
        hvac_mode: "VThermHvacMode | None",
        b: float | None,
        tin_history: Iterable[Tuple[float, float]],
        context: str,
    ) -> None:
        """Observe the power applied over [now - dt_s, now].

        The interval convention is the one of the learning window. b is the
        published loss rate per minute (None before); tin_history holds the
        (monotonic time, temperature) samples of the dead time estimator;
        context names the learning phase ("bootstrap", "calibration" or
        "regulation").
        """
        if hvac_mode is None or str(hvac_mode) != str(VThermHvacMode_HEAT):
            self._restore = None
            self._drop_phase("heating mode left")
            self._last_now = None
            self._rest_start = None
            return
        if dt_s <= 0.0:
            return
        if self._last_now is not None and now <= self._last_now:
            return
        t_step = now - dt_s
        if self._restore is not None:
            # No restart decision: the stop is not bridged.
            self.resolve_restart(False)
        if self._last_now is not None and t_step > self._last_now + _CONTIGUITY_TOL_S:
            self._drop_phase("observation gap")
            self._rest_start = None
        self._last_now = now
        on = u > _POWER_ON
        history = tin_history

        phase = self._phase
        if phase is not None and phase["stage"] == _HEATING and not on:
            phase["stage"] = _CUT
            phase["t_cut"] = t_step
            phase["area_cut"] = phase["area"]
        if phase is not None and phase["stage"] == _CUT:
            self._advance_cut(phase, now, dt_s, t_step, u, on, b, history)
        elif phase is not None:
            self._advance_heating(phase, now, dt_s, u, history)
        if self._phase is None:
            if on:
                self._start_phase(t_step, now, dt_s, u, history, context)
            elif self._rest_start is None:
                self._rest_start = t_step

    def _advance_heating(
        self, phase: dict, now: float, dt_s: float, u: float, history: Iterable[Tuple[float, float]]
    ) -> None:
        phase["on_s"] += u * dt_s
        phase["u_max"] = max(phase["u_max"], u)
        self._feed(phase, _samples_after(history, phase["last_t"], now))

    def _start_phase(
        self,
        t_start: float,
        now: float,
        dt_s: float,
        u: float,
        history: Iterable[Tuple[float, float]],
        context: str,
    ) -> None:
        rest_s = None if self._rest_start is None else t_start - self._rest_start
        reason: str | None = None
        base = None
        if rest_s is None or rest_s < HEATING_BALANCE_MIN_REST_S:
            reason = "rest before heating too short"
        else:
            span = min(HEATING_BALANCE_BASELINE_SPAN_S, rest_s)
            base = _baseline(history, t_start - span, t_start)
            if base is None:
                reason = "baseline samples insufficient"
        self._rest_start = None
        self._phase = {
            "stage": _HEATING,
            "context": context,
            "reason": reason,
            "t_start": t_start,
            "rest_s": rest_s,
            "base_value": 0.0 if base is None else base[0],
            "base_slope": 0.0 if base is None else base[1],
            "u_max": u,
            "on_s": 0.0,
            "area": 0.0,
            "last_t": t_start,
            "last_dev": 0.0,
            "t_cut": None,
            "area_cut": None,
            "cut_temp": None,
            "bin_k": None,
            "bin_sum": 0.0,
            "bin_n": 0,
            "best_k": None,
            "best_mean": None,
            "best_area": None,
            "last_k": None,
        }
        self._advance_heating(self._phase, now, dt_s, u, history)

    def _feed(self, phase: dict, samples: list[Tuple[float, float]]) -> bool:
        """Accumulate the area under the baseline and, after the cut, the peak bins.

        Returns True once the peak is confirmed (the remaining samples are not used).
        """
        cut = phase["stage"] == _CUT
        for t, v in samples:
            if cut:
                k = max(0, int((t - phase["t_cut"]) // HEATING_BALANCE_PEAK_BIN_S))
                if phase["bin_k"] is None:
                    phase["bin_k"], phase["bin_sum"], phase["bin_n"] = k, v, 1
                elif k == phase["bin_k"]:
                    phase["bin_sum"] += v
                    phase["bin_n"] += 1
                else:
                    if self._close_bin(phase):
                        return True
                    phase["bin_k"], phase["bin_sum"], phase["bin_n"] = k, v, 1
            dev = v - (phase["base_value"] + phase["base_slope"] * (t - phase["t_start"]))
            phase["area"] += 0.5 * (dev + phase["last_dev"]) * (t - phase["last_t"]) / 60.0
            phase["last_t"], phase["last_dev"] = t, dev
        return False

    @staticmethod
    def _close_bin(phase: dict) -> bool:
        """Close the bin in progress; True when it confirms the best bin as the peak."""
        k = phase["bin_k"]
        mean = phase["bin_sum"] / phase["bin_n"]
        if phase["best_mean"] is None or mean > phase["best_mean"]:
            phase["best_k"], phase["best_mean"], phase["best_area"] = k, mean, phase["area"]
        phase["last_k"] = k
        return (k - phase["best_k"]) * HEATING_BALANCE_PEAK_BIN_S >= HEATING_BALANCE_PEAK_CONFIRM_S

    def _advance_cut(
        self,
        phase: dict,
        now: float,
        dt_s: float,
        t_step: float,
        u: float,
        on: bool,
        b: float | None,
        history: Iterable[Tuple[float, float]],
    ) -> None:
        t_cut = phase["t_cut"]
        if on and t_step - t_cut < HEATING_BALANCE_PHASE_MIN_OFF_S:
            # The cut was short: the same heating phase continues.
            phase["stage"] = _HEATING
            for key in ("t_cut", "area_cut", "cut_temp", "bin_k", "best_k", "best_mean",
                        "best_area", "last_k"):
                phase[key] = None
            phase["bin_sum"], phase["bin_n"] = 0.0, 0
            self._advance_heating(phase, now, dt_s, u, history)
            return
        if phase["cut_temp"] is None and now >= t_cut + _CUT_WINDOW_S:
            phase["cut_temp"] = self._temperature_at(history, t_cut)
        limit = t_step if on else now
        if self._feed(phase, _samples_after(history, phase["last_t"], limit)):
            self._end_phase(phase, True, b, "")
        elif on:
            peak = False
            if phase["bin_k"] is not None:
                self._close_bin(phase)
                peak = phase["best_k"] != phase["last_k"]
            self._end_phase(phase, peak, b, "still rising at the next heating")
        elif now - t_cut >= HEATING_BALANCE_PEAK_MAX_S:
            self._end_phase(phase, False, b, "no peak within the wait")

    @staticmethod
    def _temperature_at(history: Iterable[Tuple[float, float]], t_cut: float) -> float | None:
        """Mean of the samples within the window around t_cut, else the last one before it."""
        near = [
            v for _, v in _samples_after(history, t_cut - _CUT_WINDOW_S - 1e-9, t_cut + _CUT_WINDOW_S)
        ]
        if near:
            return sum(near) / len(near)
        before = [v for _, v in _samples_after(history, t_cut - 5 * 3600.0, t_cut)]
        return before[-1] if before else None

    # ------------------------------------------------------------------
    # End of a phase
    # ------------------------------------------------------------------

    def _drop_phase(self, reason: str) -> None:
        if self._phase is not None:
            self._phase = None
            self._last_reason = reason

    def _end_phase(self, phase: dict, peak: bool, b: float | None, no_peak_reason: str) -> None:
        """End the phase after its cut: a result when the peak was found."""
        self._phase = None
        self._rest_start = phase["t_cut"]
        if phase["reason"] is not None:
            self._last_reason = phase["reason"]
        elif not peak:
            self._last_reason = no_peak_reason
        else:
            self._measure(phase, 0.0 if b is None else b)
        _LOGGER.debug(
            "%s - heating balance: %s (%d measurements, a %s per min, lag %s s)",
            self._name, self._last_reason, self.count, self.a_per_min, self.lag_s,
        )

    def _measure(self, phase: dict, b_used: float) -> None:
        t_start, t_cut = phase["t_start"], phase["t_cut"]
        slope, value = phase["base_slope"], phase["base_value"]
        t_peak = t_cut + (phase["best_k"] + 0.5) * HEATING_BALANCE_PEAK_BIN_S
        rise_c = phase["best_mean"] - (value + slope * (t_peak - t_start))
        on_s = phase["on_s"]
        if on_s < HEATING_BALANCE_MIN_ON_S:
            self._last_reason = "heating too short"
            return
        if rise_c < HEATING_BALANCE_MIN_RISE_C:
            self._last_reason = "temperature rise too small"
            return
        on_min = on_s / 60.0
        a = (rise_c + b_used * phase["best_area"]) / on_min
        if a <= 0.0:
            self._last_reason = "temperature rise too small"
            return
        duration_s = t_cut - t_start
        # Constant power: the mean power of the phase is its highest interval
        # power (a short stop or a partial first interval stays within the tolerance).
        mean_u = on_s / duration_s
        constant = mean_u >= (1.0 - RESPONSE_LAG_U_TOL) * phase["u_max"]
        cut_dev_c = None
        lag_s = None
        if phase["cut_temp"] is not None:
            cut_dev_c = phase["cut_temp"] - (value + slope * (t_cut - t_start))
            if constant:
                deficit = (on_s - 60.0 * (cut_dev_c + b_used * phase["area_cut"]) / a) / mean_u
                lag_s = _lag_from_deficit(deficit, duration_s)
        self._results.append({
            "end_wall": monotonic_to_wall_ts(t_cut),
            "context": phase["context"],
            "on_s": on_s,
            "duration_s": duration_s,
            "constant_power": constant,
            "rest_s": phase["rest_s"],
            "baseline_slope_c_h": slope * 3600.0,
            "rise_c": rise_c,
            "peak_delay_s": t_peak - t_cut,
            "area_cut": phase["area_cut"],
            "area_peak": phase["best_area"],
            "cut_dev_c": cut_dev_c,
            "b_used": b_used,
            "a": a,
            "a_no_loss": rise_c / on_min,
            "lag_s": lag_s,
        })
        del self._results[:-HEATING_BALANCE_HISTORY]
        self._last_reason = "measured"

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------

    def resolve_restart(self, bridged: bool) -> None:
        """Apply the restart decision (restart_bridge.py) to the saved phase.

        Bridged: the phase resumes, and the first observation, which spans the
        stop, keeps it contiguous. Otherwise the phase is dropped and the start
        of the rest is unknown.
        """
        restore = self._restore
        if restore is None:
            return
        self._restore = None
        if bridged:
            self._phase = restore["phase"]
            self._rest_start = restore["rest_start"]
            self._last_now = None
            return
        if restore["phase"] is not None:
            self._last_reason = "interrupted by restart"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Forget the measurements and the phase in progress."""
        self._results.clear()
        self._last_reason = ""
        self._clear_runtime()

    def _restart_context(self) -> dict | None:
        """Return the phase or the rest to resume, in monotonic time."""
        if self._restore is not None:
            # Not yet applied: no observation since the previous restart.
            return self._restore
        if self._phase is None and self._rest_start is None:
            return None
        return {"phase": self._phase, "rest_start": self._rest_start}

    def save_state(self) -> dict:
        state: dict = {"results": [dict(r) for r in self._results]}
        context = self._restart_context()
        if context is not None:
            phase = context["phase"]
            saved = None
            if phase is not None:
                saved = {
                    key: monotonic_to_wall_ts(phase[key]) if kind.startswith("time") else phase[key]
                    for key, kind in _PHASE_FIELDS.items()
                }
            state["restart"] = {
                "rest_start_wall": monotonic_to_wall_ts(context["rest_start"]),
                "phase": saved,
            }
        return state

    @staticmethod
    def _load_result(value: object) -> dict | None:
        """Validate a saved measurement."""
        if not isinstance(value, dict):
            return None
        out: dict = {}
        for key in ("end_wall", "on_s", "duration_s", "rest_s", "baseline_slope_c_h", "rise_c",
                    "peak_delay_s", "area_cut", "area_peak", "cut_dev_c", "b_used", "a",
                    "a_no_loss"):
            number = _number(value.get(key))
            if number is None and not (key in ("rest_s", "cut_dev_c") and value.get(key) is None):
                return None
            out[key] = number
        lag = value.get("lag_s")
        out["lag_s"] = None if lag is None else _number(lag)
        if lag is not None and out["lag_s"] is None:
            return None
        if not isinstance(value.get("constant_power"), bool) or not isinstance(value.get("context"), str):
            return None
        out["constant_power"] = value["constant_power"]
        out["context"] = value["context"]
        return out

    @staticmethod
    def _load_phase(value: object) -> dict | None:
        """Validate a saved phase and convert it to monotonic time."""
        if not isinstance(value, dict):
            return None
        phase: dict = {}
        for key, kind in _PHASE_FIELDS.items():
            raw = value.get(key)
            optional = kind.endswith("?")
            base = kind.rstrip("?")
            if raw is None:
                if not optional:
                    return None
                phase[key] = None
                continue
            if base == "time":
                parsed = wall_to_monotonic_ts(raw)
            elif base == "num":
                parsed = _number(raw)
            elif base == "int":
                parsed = _integer(raw)
            elif base == "bool":
                parsed = raw if isinstance(raw, bool) else None
            else:
                parsed = raw if isinstance(raw, str) else None
            if parsed is None:
                return None
            phase[key] = parsed
        if phase["stage"] not in (_HEATING, _CUT) or phase["u_max"] <= _POWER_ON or phase["bin_n"] < 0:
            return None
        if phase["stage"] == _CUT and (
            phase["t_cut"] is None or phase["area_cut"] is None
            or (phase["bin_k"] is None) != (phase["bin_n"] == 0)
        ):
            return None
        if phase["best_k"] is not None and (
            phase["best_mean"] is None or phase["best_area"] is None or phase["last_k"] is None
        ):
            return None
        return phase

    def load_state(self, state: dict | None) -> None:
        """Restore the measurements and keep the saved phase pending.

        The phase is resumed or dropped by resolve_restart.
        """
        self.clear()
        if not isinstance(state, dict):
            return
        results = state.get("results")
        if isinstance(results, list):
            for value in results:
                result = self._load_result(value)
                if result is not None:
                    self._results.append(result)
            del self._results[:-HEATING_BALANCE_HISTORY]
        restart = state.get("restart")
        if not isinstance(restart, dict):
            return
        phase = restart.get("phase")
        loaded = self._load_phase(phase) if phase is not None else None
        if phase is not None and loaded is None:
            return
        rest_wall = restart.get("rest_start_wall")
        rest_start = wall_to_monotonic_ts(rest_wall) if rest_wall is not None else None
        if loaded is None and rest_start is None:
            return
        self._restore = {"phase": loaded, "rest_start": rest_start}
