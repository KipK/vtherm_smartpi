"""Calibration: measurement pulses on request once the first model is published, and the correction they may bring.

A calibration is requested by the ``force_smartpi_calibration`` service, which never fails: a request that does not
start a calibration is refused or ignored with a plain reason (``last_request``). It is refused during the learning
stage (the start mode has its own measurements then), while the learning is paused (nothing would be measured), while
the thermostat is off, and less than ``CALIBRATION_MIN_INTERVAL_S`` after the end of the last calibration that was not
cancelled; it is ignored while one is in progress. An open window, a guard cut or a power shedding does not refuse it:
the pulses wait for the room to be ready.

A calibration drives the measurement pulses of the direct start (smartpi/measure_pulse.py), in either start mode,
with its own count (at most ``PULSE_MAX_COUNT`` pulses) and its own limits, sized from what the learning knows of the
room (``pulse_limits``): a pulse may start from ``CALIBRATION_START_OVER_C`` past the setpoint; the next one waits
twice the anticipation horizon after the end of the previous one (one to six hours); the rest lasts the passive dead
time plus ``CALIBRATION_REST_EXTRA_S`` (the free-running window the dead-time estimator needs to measure the start),
and ends earlier when the lowest point the room is heading to (the room plus its fall during the active dead time)
reaches ``PULSE_REST_UNDER_C`` on the demand side; the heating step ends like that of the other pulses (response seen and the room back within
``PULSE_HEAT_BACK_C`` of the setpoint) or lasts twice the active dead time plus the horizon (two to four hours) at most; during the stop the regulation takes over only when the room is ``PULSE_TAKEOVER_C`` on
the demand side and lower than at the start of the stop by one step of the sensor, or ``CALIBRATION_DEPTH_CAP_C`` on
the demand side in any case. No step lasts more than ``CALIBRATION_STEP_MAX_S``. The command of a pulse is applied as
an on/off command like any pulse: the dead-time estimator measures the start and the stop as usual and its samples go
into its median. The stop step of a calibration pulse ends when the measurement event of the pulse is closed (its peak
confirmed) and the estimator no longer waits for the response to the stop.

The events of the calibration pulses are read like those of the start (smartpi/event_reading.py) but apart from them:
two agreeing readings give the calibration reading (heating rate, loss coefficient, emitter lag). When the calibration
ends on that reading, each quantity is compared with the value in force in the mode of the calibration (``in_force``)
and corrected only when the value in force is off the reading by more than ``CALIBRATION_CORRECT_REL`` of the reading
(the reading is the estimate of the truth; ``corrections``):

- the heating rate: the channel of the mode restarts and the reading stands as its starting value until the learning
  publishes its own (smartpi/starting_values.py);
- the loss coefficient is read and published, not corrected (the reading of a pulse is biased in a room with heavy
  walls). With ``CALIBRATION_CORRECT_B`` and when the two readings' own values agree within the agreement margin of
  the readings, it is corrected: the channel restarts and the reading stands until the learning's own time constant
  is reliable again;
- the emitter lag, with the direct start only: it is given to the response-lag estimator in place of the lag kept from
  the start, and stands while that estimator has no lag of its own. It never sets the anticipation horizon. With the
  bootstrap start it is published only.

With ``CALIBRATION_SKIP_WHEN_OWN_AGREE``, a quantity is not corrected while the learning's own recent measures agree
with the value in force (no drift candidate and the last ``CALIBRATION_OWN_RECENT`` measures within the threshold).

A calibration ends when two of its readings agree, when its pulses are done and their readings have ended, or
``CALIBRATION_MAX_S`` after its first calculation (the end is checked during a guard cut too). When that end comes
during a pulse, a pulse in its rest hands back at once, a pulse in its heating step goes to its stop step and one in
its stop step goes on: the calibration ends, and applies its correction, at the calculation where the pulse ends,
before the regulation computes its command. The step of the learned holding power is moved into the integral so that
the feed-forward and the integral term do not jump, at the first regulation calculation after the correction; a
change of Kp acts on the error as usual (it is not moved into the integral). While a calibration is in progress the
learning takes no measure of the heating rate (its pulses and the recoveries after them would bias it); the loss
coefficient, the dead times and the heating balance go on. After each calibration pulse the integral is held until
the room reaches the setpoint. A pulse counts for its
calibration once it reaches its heating step; one that ends before (a setpoint change during its rest, for example)
does not count and the next one may start at once. A stop of the thermostat (off, window, shedding) ends the pulse in
progress; the calibration goes on with its next pulse. A change of mode, a return of the learning stage, a learning
reset or a pause of the learning cancels it. The calibration in progress, its pulses done and its start, the last
corrections and the corrected lag are saved; a pulse in progress at a restart ends (smartpi/measure_pulse.py).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Iterable, NamedTuple

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .const import CALIBRATION_MAX_S, DEADTIME_PRESTART_MIN_SPAN_S
from .event_fit import READING_AGREE_REL
from .timestamp_utils import monotonic_to_wall_ts, normalize_wall_timestamp, wall_to_monotonic_ts

RESULT_IN_PROGRESS = "in progress"
RESULT_AGREED = "readings agree"
RESULT_PULSES_DONE = "pulses done"
RESULT_TIME_LIMIT = "time limit"
RESULT_CANCELLED = "cancelled"
_RESULTS = (RESULT_IN_PROGRESS, RESULT_AGREED, RESULT_PULSES_DONE, RESULT_TIME_LIMIT, RESULT_CANCELLED)

REQUEST_STARTED = "started"
REQUEST_LEARNING_STAGE = "refused: learning stage"
REQUEST_PAUSED = "refused: learning paused"
REQUEST_OFF = "refused: thermostat off"
REQUEST_TOO_SOON = "refused: too soon"
REQUEST_IN_PROGRESS = "ignored: in progress"
_REQUESTS = (REQUEST_STARTED, REQUEST_LEARNING_STAGE, REQUEST_PAUSED, REQUEST_OFF, REQUEST_TOO_SOON, REQUEST_IN_PROGRESS)

CALIBRATION_MIN_INTERVAL_S = 24.0 * 3600.0   # a request this soon after the end of the last calibration is refused

# Correction of the model by an agreeing calibration reading
CALIBRATION_CORRECT_REL = 0.30               # a quantity is corrected when the value in force is off the reading by more than this share of it
CALIBRATION_CORRECT_B = False                # True: the loss coefficient is corrected too (False: published only)
CALIBRATION_SKIP_WHEN_OWN_AGREE = False      # True: no correction while the learning's recent measures agree with it
CALIBRATION_OWN_RECENT = 3                   # recent measures of the learning read by that rule

# Limits of the calibration pulses (the pulses of the learning stage keep those of smartpi/measure_pulse.py)
CALIBRATION_START_OVER_C = 0.2               # a pulse may start up to this far past the setpoint
CALIBRATION_GAP_MIN_S = 3600.0               # gap after the previous pulse: twice the horizon, within these bounds
CALIBRATION_GAP_MAX_S = 6.0 * 3600.0
CALIBRATION_REST_EXTRA_S = DEADTIME_PRESTART_MIN_SPAN_S + 300.0   # free-running window + one sensor heartbeat
CALIBRATION_HEAT_MIN_S = 2.0 * 3600.0        # heating step limit: twice (active dead time + horizon), within these
CALIBRATION_STEP_MAX_S = 4.0 * 3600.0        # longest step of a calibration pulse
CALIBRATION_DEPTH_CAP_C = 0.8                # the regulation takes over at this distance on the demand side
CALIBRATION_FALL_WINDOW_S = 900.0            # window of the fall rate of the room

QUANTITIES = ("a", "b", "lag")
_WORDS = {"a": "heating rate", "b": "loss coefficient", "lag": "emitter lag"}


def _seconds(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


class PulseLimits(NamedTuple):
    """Limits of a calibration pulse (seconds, C and C/s)."""

    start_over_c: float     # the pulse may start this far past the setpoint
    gap_s: float            # after the end of the previous pulse
    rest_s: float           # rest: the free-running window
    trough_lead_s: float    # rest: the fall of the room is counted over this time (active dead time)
    fall_c_per_s: float     # rest: fall rate of the room on the demand side (>= 0)
    heat_max_s: float
    depth_cap_c: float
    step_max_s: float


def fall_rate(tin_history: Iterable[tuple[float, float]], cooling: bool) -> float:
    """Least-squares slope of the last ``CALIBRATION_FALL_WINDOW_S`` of the room temperature, on the demand side
    (falling in heating, rising in cooling), in C/s; 0 when the room moves the other way or with fewer than 2 samples."""
    points = list(tin_history)
    if not points:
        return 0.0
    t_last = points[-1][0]
    points = [(t, v) for t, v in points if t >= t_last - CALIBRATION_FALL_WINDOW_S]
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in points)
    if n < 2 or var_t <= 0.0:
        return 0.0
    mean_v = sum(v for _, v in points) / n
    slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / var_t
    return max(0.0, slope if cooling else -slope)


def pulse_limits(dt_est: Any, hvac_mode: object, horizon_s: float, history: Any = None) -> PulseLimits:
    """Limits of a calibration pulse from the dead times of ``hvac_mode`` (0 when not reliable), the anticipation
    horizon in use and the recent fall of the room (``history``: the raw room temperature samples, the ``tin_history``
    of ``dt_est`` when not given)."""
    def reliable(view: tuple[float | None, bool]) -> float:
        value, ok = view
        return float(value) if ok and value is not None and value > 0.0 else 0.0

    active = reliable(dt_est.active_deadtime(hvac_mode))
    passive = reliable(dt_est.passive_deadtime(hvac_mode))
    horizon = max(float(horizon_s or 0.0), 0.0)
    return PulseLimits(
        start_over_c=CALIBRATION_START_OVER_C,
        gap_s=_clamp(2.0 * horizon, CALIBRATION_GAP_MIN_S, CALIBRATION_GAP_MAX_S),
        rest_s=min(passive + CALIBRATION_REST_EXTRA_S, CALIBRATION_STEP_MAX_S),
        trough_lead_s=active,
        fall_c_per_s=fall_rate(
            (getattr(dt_est, "tin_history", ()) if history is None else history) or (), hvac_mode == VThermHvacMode_COOL),
        heat_max_s=_clamp(2.0 * (active + horizon), CALIBRATION_HEAT_MIN_S, CALIBRATION_STEP_MAX_S),
        depth_cap_c=CALIBRATION_DEPTH_CAP_C,
        step_max_s=CALIBRATION_STEP_MAX_S,
    )


def _differs(value: float, reference: float) -> bool:
    """True when ``value`` is off ``reference`` by more than ``CALIBRATION_CORRECT_REL`` of ``reference``."""
    return abs(value - reference) > CALIBRATION_CORRECT_REL * abs(reference)


def own_measures_agree(history: Iterable[float], drift_buffer: Iterable[Any], in_force: float) -> bool:
    """True when the learning's own recent measures agree with the value in force: no drift candidate and the last
    ``CALIBRATION_OWN_RECENT`` measures within ``CALIBRATION_CORRECT_REL`` of it."""
    recent = list(history)[-CALIBRATION_OWN_RECENT:]
    return (not list(drift_buffer) and len(recent) == CALIBRATION_OWN_RECENT
            and all(not _differs(abs(v), abs(in_force)) for v in recent))


def corrections(reading: dict[str, Any], in_force: dict[str, float], own_agree: dict[str, bool], lag: bool) -> list[str]:
    """Quantities of the agreeing ``reading`` to correct: those whose value ``in_force`` is off the reading by more than
    ``CALIBRATION_CORRECT_REL`` of the reading (``b`` also needs the two readings to agree on it; the lag needs ``lag``: it acts with
    the direct start only)."""
    skip = own_agree if CALIBRATION_SKIP_WHEN_OWN_AGREE else {}
    out = []
    if _differs(in_force["a"], float(reading["a_per_min"])) and not skip.get("a"):
        out.append("a")
    b = reading.get("b_per_min")
    if (CALIBRATION_CORRECT_B and b is not None and reading.get("b_agree") and _differs(in_force["b"], float(b))
            and not skip.get("b")):
        out.append("b")
    if lag and _differs(in_force["lag_s"], float(reading["lag_s"])):
        out.append("lag")
    return out


def readings_agree_on(first: float | None, second: float | None) -> bool:
    """True when two positive values agree within the agreement margin of the readings."""
    if first is None or second is None or first <= 0.0 or second <= 0.0:
        return False
    return abs(first - second) <= READING_AGREE_REL * max(first, second)


class Calibration:
    """State of the calibration. Times are monotonic seconds, as ``SmartPI.calculate`` reads them."""

    def __init__(self) -> None:
        self.last_end_wall: float | None = None   # wall time of the last calibration that was not cancelled
        self.result: str | None = None
        self.last_request: str | None = None
        self.lag_s: dict[str, float] = {}         # corrected emitter lag in force, by mode (direct start)
        self.clear()

    def clear(self) -> None:
        """No calibration in progress, nothing measured nor corrected (the last result, request, end time and
        corrected lag are kept)."""
        self.in_progress = False
        self.pulses = 0
        self.start_s: float | None = None
        self.mode: str | None = None
        self.deadtime_start_s: float | None = None
        self.deadtime_stop_s: float | None = None
        self.corrected: list[str] = []
        self.in_force: dict[str, float] | None = None
        self._seen: dict[str, int] = {}   # dead-time samples the estimator had added at the last look (runtime)

    def begin(self) -> None:
        """A request accepted: a calibration starts with no pulse."""
        self.clear()
        self.in_progress = True
        self.result = RESULT_IN_PROGRESS

    def request(self, learning_stage: bool, learning_enabled: bool, off: bool) -> str:
        """A request: the calibration starts (``REQUEST_STARTED``) or the reason it does not."""
        if learning_stage:
            reason = REQUEST_LEARNING_STAGE
        elif not learning_enabled:
            reason = REQUEST_PAUSED
        elif self.in_progress:
            reason = REQUEST_IN_PROGRESS
        elif off:
            reason = REQUEST_OFF
        elif self.last_end_wall is not None and time.time() - self.last_end_wall < CALIBRATION_MIN_INTERVAL_S:
            reason = REQUEST_TOO_SOON
        else:
            reason = REQUEST_STARTED
            self.begin()
        self.last_request = reason
        return reason

    def finish(self, result: str) -> None:
        """The calibration ends; its pulses and dead times stay published until the next one."""
        self.in_progress = False
        self.start_s = None
        self.result = result
        if result != RESULT_CANCELLED:
            self.last_end_wall = time.time()

    def forget_lag(self) -> None:
        """Learning reset: the corrected lag stops."""
        self.lag_s = {}

    def check(
        self, now: float, hvac_mode: object, learning_stage: bool, pulse_active: bool, readings_waiting: bool,
        agreed: bool, max_pulses: int,
    ) -> str | None:
        """Once per calculation while in progress: the result that ends the calibration, None while it goes on."""
        if self.start_s is None:
            self.start_s = now
        if hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            if self.mode is None:
                self.mode = str(hvac_mode)
            elif self.mode != str(hvac_mode):
                return RESULT_CANCELLED
        if learning_stage:
            return RESULT_CANCELLED
        if agreed:
            return RESULT_AGREED
        if now - self.start_s >= CALIBRATION_MAX_S:
            return RESULT_TIME_LIMIT
        if self.pulses >= max_pulses and not pulse_active and not readings_waiting:
            return RESULT_PULSES_DONE
        return None

    def note_deadtimes(self, dt_est: Any, hvac_mode: object, pulse_active: bool) -> None:
        """Keep the dead times the estimator measured since the last look while a calibration pulse runs; the samples
        added between the pulses are not the calibration's."""
        cooling = hvac_mode == VThermHvacMode_COOL
        added = getattr(dt_est, "samples_added", None)
        if not pulse_active or not isinstance(added, dict):
            self._seen = {}
            return
        for key, response in (("start", "cool" if cooling else "heat"), ("stop", "heat" if cooling else "cool")):
            n = added.get(response, 0)
            seen = self._seen.get(response)
            self._seen[response] = n
            if seen is not None and n > seen:
                setattr(self, f"deadtime_{key}_s", dt_est.last_sample_s(response))

    # -- persistence -------------------------------------------------------------------------------------------
    def save(self) -> dict[str, Any] | None:
        if not self.in_progress and self.result is None and self.last_end_wall is None and self.last_request is None:
            return None
        saved = {
            "in_progress": self.in_progress, "pulses": self.pulses, "mode": self.mode,
            "start_wall": monotonic_to_wall_ts(self.start_s), "result": self.result,
            "last_end_wall": self.last_end_wall,
            "deadtime_start_s": self.deadtime_start_s, "deadtime_stop_s": self.deadtime_stop_s,
            "last_request": self.last_request,
        }
        if self.in_force is not None:
            saved.update(in_force=dict(self.in_force), corrected=list(self.corrected))
        if self.lag_s:
            saved["lag_s"] = dict(self.lag_s)
        return saved

    def load(self, saved: object, legacy: object = None) -> None:
        """Restore what ``save`` wrote. Without it, the end time of a calibration saved by the former format
        (``legacy``, the old ``cal_state`` block) is kept; a calibration it held in progress is not resumed."""
        self.clear()
        self.result = None
        self.last_end_wall = None
        self.last_request = None
        self.lag_s = {}
        if not isinstance(saved, dict):
            if isinstance(legacy, dict):
                self.last_end_wall = normalize_wall_timestamp(legacy.get("last_calibration_time"))
            return
        self.last_end_wall = normalize_wall_timestamp(saved.get("last_end_wall"))
        result = saved.get("result")
        self.result = result if result in _RESULTS else None
        request = saved.get("last_request")
        self.last_request = request if request in _REQUESTS else None
        pulses = saved.get("pulses")
        self.pulses = pulses if isinstance(pulses, int) and not isinstance(pulses, bool) and pulses >= 0 else 0
        self.mode = saved["mode"] if isinstance(saved.get("mode"), str) else None
        self.deadtime_start_s = _seconds(saved.get("deadtime_start_s"))
        self.deadtime_stop_s = _seconds(saved.get("deadtime_stop_s"))
        in_force = saved.get("in_force")
        if isinstance(in_force, dict) and all(_seconds(in_force.get(q)) is not None for q in ("a", "b", "lag_s")):
            self.in_force = {q: float(in_force[q]) for q in ("a", "b", "lag_s")}
            corrected = saved.get("corrected")
            self.corrected = [q for q in QUANTITIES if isinstance(corrected, list) and q in corrected]
        lags = saved.get("lag_s")
        for mode, lag in (lags.items() if isinstance(lags, dict) else ()):
            if isinstance(mode, str) and _seconds(lag):
                self.lag_s[mode] = float(lag)
        if saved.get("in_progress") is not True:
            if self.result == RESULT_IN_PROGRESS:
                self.result = RESULT_CANCELLED
            return
        self.in_progress = True
        self.result = RESULT_IN_PROGRESS
        self.start_s = wall_to_monotonic_ts(saved.get("start_wall"))

    # -- published summary -------------------------------------------------------------------------------------
    def public(self, step: str, reading: dict[str, Any] | None) -> dict[str, Any]:
        """Plain summary: ``step`` is the step of the calibration pulse in progress in words, ``reading`` the agreeing
        reading of the calibration (None before)."""
        b = None if reading is None else reading.get("b_per_min")
        in_force = self.in_force or {}
        return {
            "state": step if self.in_progress else "idle",
            "pulses_done": self.pulses,
            "result": self.result,
            "last_request": self.last_request,
            "last_time": None if self.last_end_wall is None else datetime.fromtimestamp(self.last_end_wall).isoformat(),
            "deadtime_start_s": None if self.deadtime_start_s is None else round(self.deadtime_start_s),
            "deadtime_stop_s": None if self.deadtime_stop_s is None else round(self.deadtime_stop_s),
            "reading_a": None if reading is None else round(reading["a_per_min"], 5),
            "reading_b": None if b is None else round(b, 6),
            "reading_lag_s": None if reading is None else round(reading["lag_s"]),
            "in_force_a": None if not in_force else round(in_force["a"], 5),
            "in_force_b": None if not in_force else round(in_force["b"], 6),
            "in_force_lag_s": None if not in_force else round(in_force["lag_s"]),
            "corrected": [_WORDS[q] for q in self.corrected],
        }
