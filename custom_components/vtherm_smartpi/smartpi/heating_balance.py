"""
Heating balance for Smart-PI.

On a heating phase followed by a stop, the room integrates the delivered power:
the temperature rise above the passive trajectory, plus the losses over the
phase, equals the heating rate times the delivered full-power time. The
estimator measures the heating rate `a` (C/min at full power) from the rise
at the temperature peak after the cut, and the emitter lag from the heat still
missing at the cut (the emitter has not delivered all the energy commanded).
On a constant-power phase the heat the emitter still holds at the peak is
counted too: the temperature is flat there, so the emitter gives what the room
loses, and that power is taken to arrive over one lag. The heating rate and
the lag are then solved together.

The phases and the peaks are detected by the measurement event detector (smartpi/measure_event.py); the
balance reads those of the heating mode.

The results are published. The regulation reads only the median of the last
lags, as the horizon of its anticipation (smartpi/anticipation.py).
"""
from __future__ import annotations

import logging
import math
import statistics
from typing import TYPE_CHECKING, Iterable, Tuple

from .const import (
    HEATING_BALANCE_HISTORY,
    HEATING_BALANCE_MIN_ON_S,
    HEATING_BALANCE_MIN_RISE_C,
    RESPONSE_LAG_U_TOL,
)
from .measure_event import MODE_HEAT, MeasureEvent, MeasureEventDetector
from .timestamp_utils import monotonic_to_wall_ts

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode

_LOGGER = logging.getLogger(__name__)

# A measured lag at or beyond this fraction of the phase duration is not resolved.
_LAG_RESOLVABLE_FRAC = 0.98
# The lag and the heat still to come at the peak are solved together: steps, tolerance (s).
_LAG_ITERATIONS = 60
_LAG_TOL_S = 1.0

def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


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
        # Lags (s) of the last measurements that resolved one, oldest first: a measurement
        # without lag removes none, unlike the kept results.
        self._lags: list[float] = []
        self._last_reason: str = ""
        # The phases of the heating are detected by the event detector; the balance reads the HEAT ones.
        self._events = MeasureEventDetector(name)

    @property
    def events(self) -> MeasureEventDetector:
        """Return the detector of the measurement events."""
        return self._events

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
    def kept_lag_s(self) -> float | None:
        """Median of the last measured lags (s), None while none was measured."""
        return statistics.median(self._lags) if self._lags else None

    @property
    def kept_lag_count(self) -> int:
        return len(self._lags)

    @property
    def count(self) -> int:
        return len(self._results)

    @property
    def state(self) -> str:
        stage = self._events.phase_stage(MODE_HEAT)
        if stage is None:
            return "resting"
        return "waiting for the peak" if stage == "cut" else "heating"

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
        ext: float | None = None,
    ) -> None:
        """Observe the power applied over [now - dt_s, now].

        The interval convention is the one of the learning window. b is the
        published loss rate per minute (None before); tin_history holds the
        (monotonic time, temperature) samples of the dead time estimator;
        context names the learning phase ("bootstrap", "calibration" or
        "regulation"); ext is the outdoor temperature. Only the events of the heating mode give measurements.
        """
        events = self._events.observe(
            now=now, dt_s=dt_s, u=u, hvac_mode=hvac_mode, tin_history=tin_history, context=context, ext=ext,
        )
        self._read(events, b)

    def _read(self, events: list[MeasureEvent], b: float | None) -> None:
        """Take the events of the heating mode: a lost phase gives its reason, a closed one a result when the peak
        was found."""
        for event in events:
            if event.mode != MODE_HEAT:
                continue
            if event.reason:
                self._last_reason = event.reason
            if not event.closed:
                continue
            if not event.reason:
                self._measure(event.phase, 0.0 if b is None else b)
            _LOGGER.debug(
                "%s - heating balance: %s (%d measurements, a %s per min, lag %s s)",
                self._name, self._last_reason, self.count, self.a_per_min, self.lag_s,
            )

    def _measure(self, phase: dict, b_used: float) -> None:
        t_start, t_cut = phase["t_start"], phase["t_cut"]
        slope, value = phase["base_slope"], phase["base_value"]
        search = phase["search"]
        t_peak = t_cut + search.peak_delay_s
        rise_c = search.peak_value - (value + slope * (t_peak - t_start))
        on_s = phase["on_s"]
        if on_s < HEATING_BALANCE_MIN_ON_S:
            self._last_reason = "heating too short"
            return
        if rise_c < HEATING_BALANCE_MIN_RISE_C:
            self._last_reason = "temperature rise too small"
            return
        on_min = on_s / 60.0
        a = (rise_c + b_used * search.best_mark) / on_min
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
                # At the peak the temperature is flat: the emitter still gives what the room
                # loses (C/min). That heat, arriving over one lag, belongs to the phase.
                late_c_min = max(0.0, b_used * rise_c - 60.0 * slope)
                delivered_min = cut_dev_c + b_used * phase["area_cut"]
                a_lag, lag_s = a, 0.0
                for _ in range(_LAG_ITERATIONS):
                    previous = lag_s
                    a_lag = a + late_c_min * previous / 60.0 / on_min
                    lag_s = _lag_from_deficit((on_s - 60.0 * delivered_min / a_lag) / mean_u, duration_s)
                    if lag_s is None or abs(lag_s - previous) < _LAG_TOL_S:
                        break
                else:
                    lag_s = None
                if lag_s is not None:
                    a = a_lag
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
            "area_peak": search.best_mark,
            "cut_dev_c": cut_dev_c,
            "b_used": b_used,
            "a": a,
            "a_no_loss": rise_c / on_min,
            "lag_s": lag_s,
        })
        del self._results[:-HEATING_BALANCE_HISTORY]
        if lag_s is not None:
            self._lags.append(lag_s)
            del self._lags[:-HEATING_BALANCE_HISTORY]
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
        self._read(self._events.resolve_restart(bridged), None)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Forget the measurements and the phase in progress."""
        self._results.clear()
        self._lags.clear()
        self._last_reason = ""
        self._events.clear()

    def save_state(self) -> dict:
        state: dict = {"results": [dict(r) for r in self._results], "lags": list(self._lags)}
        context = self._events.restart_context()
        if context is not None and context["mode"] == MODE_HEAT:
            state["restart"] = self._events.save_context(context)
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
        lags = state.get("lags")
        if isinstance(lags, list):
            self._lags = [lag for lag in map(_number, lags) if lag is not None][-HEATING_BALANCE_HISTORY:]
        if not self._lags and self.lag_s is not None:
            # A published lag saved without any kept lag is the one kept.
            self._lags = [self.lag_s]
        self._events.load_context(state.get("restart"))
