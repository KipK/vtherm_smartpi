"""Measurement pulses of the learning stage.

While the first model is not published and the PI drives the command (``learning_start`` "direct"), a pulse replaces
the PI command for a short sequence that gives the learning what the hysteresis gave it: a dead time after a
start, a heating rate and a dead time after a stop. A pulse has three steps:

1. rest: no heating for ``PULSE_REST_MAX_S``, or until the room is ``PULSE_REST_UNDER_C`` on the demand side of the
   setpoint (under it in HEAT, over it in COOL) if that comes first;
2. heating at full power until the dead-time estimator has detected the response to that start (its own detection,
   unchanged) and the rise stands out of the sensor and, while the step has lasted less than
   ``PULSE_HEAT_EXTEND_MAX_S``, the room is back within ``PULSE_HEAT_BACK_C`` of the demand side of the setpoint (so
   that the stop starts high and lasts long enough to show its peak; past that time the step ends on the rise alone),
   or for ``PULSE_HEAT_MAX_S`` at most, or as soon as the room is ``PULSE_HEAT_OVER_C`` on the other side of the setpoint
   (over it in HEAT, under it in COOL); in the last two cases the pulse gives no measure of the dead time after a
   start, and the stop step still runs;
3. stop until the dead-time estimator has confirmed the temperature peak that follows the stop (for a pulse of a
   calibration: until the measurement event of the pulse is closed and the estimator no longer waits for the
   response to the stop), or for ``PULSE_STOP_MAX_S`` at most; the PI takes over earlier when the room is
   ``PULSE_TAKEOVER_C`` on the demand side of the setpoint and one step of the sensor lower than at the start of
   the stop, and in any case at the cap (``PULSE_TAKEOVER_CAP_C``, or the calibration's own cap).

A pulse starts at the first calculation where the room is at or on the demand side of the setpoint and no more than
``PULSE_START_UNDER_C`` on it and where the command of the previous calculation was under ``PULSE_START_MAX_COMMAND``
(a heating already at that power is a full-power start the estimator measures; while the room is further away the PI
regulates), at most
``PULSE_MAX_COUNT`` times per mode and blank start and ``PULSE_MIN_GAP_S`` after the end of the previous one, while the
learning stage lasts and the dead time after a start or the dead time after a stop is not published. A calibration
(smartpi/calibration.py) starts pulses in either start mode, with its own count and its own limits (``limits``, given
by the calibration: start window, gap, rest, heating step and stop); those pulses do not count for the learning stage.
A pulse interrupted by a restart, a stop of the thermostat, a change of the
setpoint, a change of mode or the cancellation of its calibration is not resumed and counts (when its calibration
ends, a pulse in its rest ends at once and a pulse in its heating step goes to its stop step), except a calibration pulse
ended before its heating step: it does not count for its calibration and the next one may start at once. A pulse whose heating step cannot
be followed by the dead-time estimator (it did not arm its detection on that start) ends at once and counts.
The sequence is the same in COOL, mirrored.

The command, the learning, the heating balance and the dead-time estimator see a pulse as an on/off command:
the algorithm applies it immediately and feeds the estimator after the commit. The readings of the events they cause
(smartpi/event_reading.py) give the gains of the learning stage (smartpi/gains.py) their heating rate.
"""
from __future__ import annotations

from typing import Any

from .sensor_step import sensor_step
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

PULSE_REST_MAX_S = 45.0 * 60.0
PULSE_REST_UNDER_C = 0.5
PULSE_HEAT_MAX_S = 120.0 * 60.0
PULSE_STOP_MAX_S = 4.0 * 3600.0
PULSE_TAKEOVER_C = 0.5
PULSE_TAKEOVER_CAP_C = 0.8  # the PI takes over at this distance on the demand side, whatever the room did since the stop
PULSE_HEAT_BACK_C = 0.1     # the heating step goes on until the room is at most this far on the demand side ...
PULSE_HEAT_EXTEND_MAX_S = 30.0 * 60.0   # ... while the step has lasted less than this; then it ends on the rise alone
PULSE_START_UNDER_C = 0.3   # no pulse starts while the room is further than this on the demand side of the setpoint
PULSE_START_MAX_COMMAND = 0.8   # nor while the command of the previous calculation is at or over this share of full power
PULSE_HEAT_OVER_C = 0.3     # the heating step ends when the room is this far on the other side of the setpoint
PULSE_MIN_GAP_S = 6.0 * 3600.0
PULSE_MAX_COUNT = 3

STATE_IDLE = "idle"
STATE_REST = "rest"
STATE_HEAT = "heat"
STATE_STOP = "stop"


PULSE_RISE_STEPS = 3.0


def _sensor_step(history: Any) -> float:
    """Smallest change between two successive samples of the last hour of the room temperature: the step of the sensor."""
    return sensor_step(history)


class MeasurePulse:
    """State machine of the pulses. Times are monotonic seconds, as ``SmartPI.calculate`` reads them."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Blank start: three pulses are available again in each mode."""
        self._state = STATE_IDLE
        self._cur: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        self._temps: Any = ()                # raw room temperature samples of the calculation in progress
        self._count = 0                      # pulses started, all modes (index of the pulse)
        self._mode_counts: dict[str, int] = {}   # pulses started by mode
        self._last_end_s: float | None = None
        self._calls = 0
        self.calibration_heated = 0          # calibration pulses that reached their heating step (never reset)

    # -- read-only view ------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a pulse commands the output."""
        return self._state != STATE_IDLE

    @property
    def state(self) -> str:
        return self._state

    @property
    def for_calibration(self) -> bool:
        """True while the pulse in progress is one of a calibration."""
        return self._cur is not None and self._cur["calibration"]

    @property
    def output(self) -> float | None:
        """Command of the pulse: 100 % during the heating step, 0 % during the other steps, None outside a pulse."""
        if self._state == STATE_HEAT:
            return 1.0
        if self._state in (STATE_REST, STATE_STOP):
            return 0.0
        return None

    @property
    def count(self) -> int:
        """Pulses started since the blank start, all modes (an interrupted pulse counts)."""
        return self._count

    def count_for(self, hvac_mode: object) -> int:
        """Pulses started since the blank start in ``hvac_mode``."""
        return self._mode_counts.get(str(hvac_mode), 0)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Pulses ended, oldest first (monotonic seconds, None when a step was not reached)."""
        return [dict(p) for p in self._history]

    @property
    def current(self) -> dict[str, Any] | None:
        return None if self._cur is None else dict(self._cur)

    def snapshot(self) -> dict[str, Any]:
        return {"state": self._state, "count": self._count, "current": self.current, "history": self.history}

    # -- state machine ---------------------------------------------------------------------------------------
    def update(
        self,
        now: float,
        target: float,
        temp: float,
        hvac_mode: object,
        cooling: bool,
        heat_or_cool: bool,
        learning_stage: bool,
        active_deadtime_known: bool,
        passive_deadtime_known: bool,
        dt_est: Any,
        previous_command: float = 0.0,
        readings_pending: bool = False,
        calibration: bool = False,
        calibration_pulses_left: bool = False,
        event_stage: str | None = None,
        limits: Any = None,
        history: Any = None,
    ) -> None:
        """Advance the pulse for this calculation (called once per calculation, before the command is chosen).

        ``readings_pending``: the readings of the events (smartpi/event_reading.py) have not kept values yet, so the
        pulses go on even when both dead times are known. ``calibration``: a calibration is in progress; its pulses
        start while ``calibration_pulses_left``. ``event_stage``: stage of the measurement event in progress
        (``MeasureEventDetector.phase_stage``), which ends the stop step of a calibration pulse. ``limits``: the limits of
        the calibration pulses (``smartpi.calibration.PulseLimits``), read while a calibration is in progress. ``history``:
        the raw room temperature samples (smartpi/temperature_history.py), the ``tin_history`` of ``dt_est`` when not given."""
        self._temps = getattr(dt_est, "tin_history", []) if history is None else history
        self._calls += 1
        sign = -1.0 if cooling else 1.0
        demand = sign * (target - temp)           # > 0: the room is on the demand side of the setpoint
        cur = self._cur
        if cur is None:
            wanted = calibration_pulses_left if calibration else (
                learning_stage
                and self.count_for(hvac_mode) < PULSE_MAX_COUNT
                and (readings_pending or not (active_deadtime_known and passive_deadtime_known))
            )
            own = limits if calibration else None
            low, gap = (0.0, PULSE_MIN_GAP_S) if own is None else (-own.start_over_c - 1e-9, own.gap_s)
            if (
                heat_or_cool
                and wanted
                and low <= demand <= PULSE_START_UNDER_C + 1e-9
                and previous_command < PULSE_START_MAX_COMMAND
                and (self._last_end_s is None or now - self._last_end_s >= gap)
            ):
                self._begin(now, str(hvac_mode), cooling, temp, target, calibration, own)
                cur = self._cur
            else:
                return
        if str(hvac_mode) != cur["mode"] or not heat_or_cool:
            self._finish(now, "mode change")
            return
        if abs(float(target) - cur["target_last"]) > 1e-9:
            self._finish(now, "setpoint change")
            return
        if not (calibration if cur["calibration"] else learning_stage):
            self._finish(now, "calibration ended" if cur["calibration"] else "model published")
            return
        cur["room_min"] = min(cur["room_min"], temp)
        cur["room_max"] = max(cur["room_max"], temp)
        own = cur.get("_limits")
        if self._state == STATE_REST:
            reason = None
            if own is not None:
                # the lowest point the room is heading to: its fall goes on during the active dead time
                fall = (limits if limits is not None else own).fall_c_per_s
                if demand + fall * own.trough_lead_s >= PULSE_REST_UNDER_C - 1e-9:
                    reason = "room under the setpoint"
                elif now - cur["rest_start_s"] >= own.rest_s:
                    reason = "time limit"
            elif demand >= PULSE_REST_UNDER_C - 1e-9:
                reason = "room under the setpoint"
            elif now - cur["rest_start_s"] >= PULSE_REST_MAX_S:
                reason = "time limit"
            if reason is not None:
                self._enter_heat(now, reason, temp)
            return
        if self._state == STATE_HEAT:
            self._advance_heat(now, temp, cooling, dt_est, demand)
            if self._state == STATE_STOP:
                # the PI takes over at once when the room is far under
                self._advance_stop(now, temp, sign, demand, dt_est, event_stage)
            return
        if self._state == STATE_STOP:
            self._advance_stop(now, temp, sign, demand, dt_est, event_stage)

    def _begin(
        self, now: float, mode: str, cooling: bool, temp: float, target: float, calibration: bool, limits: Any = None,
    ) -> None:
        self._count += 1
        if not calibration:
            self._mode_counts[mode] = self._mode_counts.get(mode, 0) + 1
        self._state = STATE_REST
        self._cur = {
            "index": self._count, "mode": mode, "cooling": bool(cooling), "calibration": bool(calibration),
            "start_s": now, "rest_start_s": now,
            "rest_end_s": None, "rest_end_reason": None,
            "heat_start_s": None, "heat_end_s": None, "heat_end_reason": None, "heat_s": None,
            "armed": None, "outcome": None, "armed_s": None,
            "stop_start_s": None, "end_s": None, "end_reason": None,
            "temp_start": float(temp), "temp_heat_start": None, "temp_stop": None, "temp_peak": None,
            "target_start": float(target), "target_last": float(target), "room_min": float(temp), "room_max": float(temp),
            "_heat_call": None, "_limits": limits,
        }

    def _enter_heat(self, now: float, reason: str, temp: float) -> None:
        cur = self._cur
        cur["rest_end_s"], cur["rest_end_reason"] = now, reason
        cur["heat_start_s"], cur["temp_heat_start"] = now, float(temp)
        cur["_heat_call"] = self._calls
        if cur["calibration"]:
            self.calibration_heated += 1
        self._state = STATE_HEAT

    def _advance_heat(self, now: float, temp: float, cooling: bool, dt_est: Any, demand: float) -> None:
        """Wait for the response to the start that the dead-time estimator armed, or for a limit."""
        cur = self._cur
        if self._calls > cur["_heat_call"]:
            # The estimator saw the start at the calculation that began the step: it is armed when its detection
            # of the active response was started at that very instant.
            if cur["armed"] is None:
                start = getattr(dt_est, "cool_start_time" if cooling else "heat_start_time", None)
                cur["armed"] = start is not None and abs(float(start) - cur["heat_start_s"]) < 1e-6
                cur["armed_s"] = now
                cur["outcome"] = getattr(dt_est, "last_outcome", {}).get("cool" if cooling else "heat")
                if not cur["armed"]:
                    # no detection can follow this start: no blind heating, the PI takes over
                    self._finish(now, "not armed")
                    return
            if cur["armed"]:
                waiting = "WAITING_COOL_RESPONSE" if cooling else "WAITING_HEAT_RESPONSE"
                sign = -1.0 if cooling else 1.0
                low = cur.get("_extreme")
                cur["_extreme"] = temp if low is None else (min(low, temp) if sign > 0 else max(low, temp))
                if getattr(dt_est, "state", None) != waiting:
                    outcome = getattr(dt_est, "last_outcome", {}).get("cool" if cooling else "heat")
                    cur["outcome"] = outcome
                    # The response must stand out of the sensor: three of its steps. The step goes on until the
                    # room is back near the setpoint, for ``PULSE_HEAT_EXTEND_MAX_S`` at most.
                    if sign * (temp - cur["_extreme"]) >= PULSE_RISE_STEPS * _sensor_step(self._temps) - 1e-9 and (
                        demand <= PULSE_HEAT_BACK_C + 1e-9
                        or now - cur["heat_start_s"] >= PULSE_HEAT_EXTEND_MAX_S - 1e-9
                    ):
                        self._enter_stop(now, "rise seen", temp)
                        return
            if demand <= -PULSE_HEAT_OVER_C + 1e-9:
                self._enter_stop(now, "room over the setpoint", temp)
                return
        own = cur["_limits"]
        if now - cur["heat_start_s"] >= (PULSE_HEAT_MAX_S if own is None else own.heat_max_s):
            self._enter_stop(now, "time limit", temp)

    def stop_heating(self, now: float, reason: str, temp: float | None) -> None:
        """End the heating step of the pulse in progress now: its stop step runs and ends by its own rules."""
        if self._state == STATE_HEAT:
            self._enter_stop(now, reason, self._cur["temp_heat_start"] if temp is None else temp)

    def _enter_stop(self, now: float, reason: str, temp: float) -> None:
        cur = self._cur
        cur["heat_end_s"], cur["heat_end_reason"] = now, reason
        cur["heat_s"] = now - cur["heat_start_s"]
        cur["stop_start_s"], cur["temp_stop"] = now, float(temp)
        cur["temp_peak"] = float(temp)
        cur["_stop_call"] = self._calls
        self._state = STATE_STOP

    def _advance_stop(
        self, now: float, temp: float, sign: float, demand: float, dt_est: Any, event_stage: str | None,
    ) -> None:
        cur = self._cur
        cur["temp_peak"] = max(cur["temp_peak"], temp) if sign > 0 else min(cur["temp_peak"], temp)
        if cur["calibration"]:
            # The event of the pulse is closed once its cut has been seen and it is no longer at that stage.
            cur["_cut_seen"] = cur.get("_cut_seen", False) or event_stage == "cut"
            peak = cur["_cut_seen"] and event_stage != "cut" and not getattr(dt_est, "stop_response_pending", False)
        else:
            peak = self._calls > cur["_stop_call"] and bool(getattr(dt_est, "stop_peak_confirmed", False))
        own = cur["_limits"]
        # the regulation takes over once the room is far enough under the setpoint and lower than at the start of
        # the stop (the peak has then had time to show), at the cap in any case
        cap = PULSE_TAKEOVER_CAP_C if own is None else own.depth_cap_c
        deeper = sign * (cur["temp_stop"] - temp) >= _sensor_step(self._temps) - 1e-9
        under = demand >= cap - 1e-9 or (demand >= PULSE_TAKEOVER_C - 1e-9 and deeper)
        if peak:
            self._finish(now, "peak")
        elif under:
            self._finish(now, "room under the setpoint")
        elif now - cur["stop_start_s"] >= PULSE_STOP_MAX_S:
            self._finish(now, "time limit")

    def _finish(self, now: float, reason: str) -> None:
        cur = self._cur
        self._cur = None
        self._state = STATE_IDLE
        if cur is None:
            return
        cur["end_s"], cur["end_reason"] = now, reason
        if cur["heat_start_s"] is not None and cur["heat_end_s"] is None:
            cur["heat_end_s"], cur["heat_end_reason"] = now, reason
            cur["heat_s"] = now - cur["heat_start_s"]
        if not (cur["calibration"] and cur["heat_start_s"] is None):
            self._last_end_s = now      # a calibration pulse ended before heating measured nothing: no gap after it
        self._history.append({k: v for k, v in cur.items() if not k.startswith("_")})

    def interrupt(self, now: float, reason: str) -> None:
        """The thermostat stops (off, window, shedding): the pulse in progress ends and is not resumed."""
        if self._cur is not None:
            self._finish(now, reason)

    # -- persistence -------------------------------------------------------------------------------------------
    def save(self, now: float | None = None) -> dict | None:
        """A pulse in progress is saved as ended by the restart and counts; it is never resumed."""
        if self._count == 0:
            return None
        last = self._last_end_s
        if self._cur is not None:
            last = now if now is not None else self._cur.get("rest_start_s")
        return {
            "count": self._count,
            "mode_counts": dict(self._mode_counts),
            "last_end_wall": None if last is None else monotonic_to_wall_ts(last),
        }

    def load(self, saved: object) -> None:
        self.reset()
        if not isinstance(saved, dict):
            return
        count = saved.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            self._count = count
        by_mode = saved.get("mode_counts")
        for mode, n in (by_mode.items() if isinstance(by_mode, dict) else ()):
            if isinstance(mode, str) and isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                self._mode_counts[mode] = min(n, PULSE_MAX_COUNT)
        self._last_end_s = wall_to_monotonic_ts(saved.get("last_end_wall"))
