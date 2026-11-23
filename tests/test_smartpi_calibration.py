"""Calibration: measurement pulses on request once the first model is published, in either start mode, their limits,
the requests, and the correction of the model by an agreeing reading."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.const import HVAC_OFF_REASON_WINDOW_DETECTION
from custom_components.vtherm_smartpi.smartpi import calibration as calibration_module
from custom_components.vtherm_smartpi.smartpi.calibration import (
    CALIBRATION_GAP_MAX_S,
    CALIBRATION_GAP_MIN_S,
    CALIBRATION_HEAT_MIN_S,
    CALIBRATION_MIN_INTERVAL_S,
    CALIBRATION_REST_EXTRA_S,
    CALIBRATION_STEP_MAX_S,
    REQUEST_IN_PROGRESS,
    REQUEST_LEARNING_STAGE,
    REQUEST_OFF,
    REQUEST_PAUSED,
    REQUEST_STARTED,
    REQUEST_TOO_SOON,
    RESULT_AGREED,
    RESULT_CANCELLED,
    RESULT_IN_PROGRESS,
    RESULT_PULSES_DONE,
    RESULT_TIME_LIMIT,
    PulseLimits,
    fall_rate,
    pulse_limits,
)
from custom_components.vtherm_smartpi.smartpi.const import CALIBRATION_MAX_S, FreezeReason, GovernanceDecision, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.measure_pulse import PULSE_MAX_COUNT, MeasurePulse

from .helpers import force_smartpi_stable_mode

TARGET, T_EXT = 20.0, 5.0
A_ROOM, B_ROOM, LAG_MIN = 0.05, 0.0003, 8.0   # C/min at full power, 1/min, emitter lag (min)
STEP_S = 60.0


class _Clock:
    def __init__(self) -> None:
        self.mono, self.wall = 100_000.0, 1_700_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "time", fake.time)
    return fake


def _learned(start: str = "bootstrap", saved_state: dict | None = None) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Cal", learning_start=start, saved_state=saved_state)
    force_smartpi_stable_mode(algo)
    if start == "direct":
        algo.est.give_starting_values(A_ROOM, B_ROOM, "heat", 1.0)
    algo.dt_est.deadtime_heat_s, algo.dt_est.deadtime_heat_reliable = 600.0, True
    algo.dt_est.deadtime_cool_s, algo.dt_est.deadtime_cool_reliable = 900.0, True
    assert algo.phase == SmartPIPhase.STABLE
    return algo


class _Room:
    """A 1R1C room behind a first-order emitter, observed every minute; the applied power is the committed one."""

    def __init__(self, algo: SmartPI, clock: _Clock, temp: float = TARGET) -> None:
        self.algo, self.clock, self.temp, self.heat = algo, clock, temp, 0.0
        self.mode = VThermHvacMode_HEAT
        self.target = TARGET
        self.trace: list[tuple[float, str, float]] = []

    def tick(self, mode=None) -> None:
        mode = self.mode if mode is None else mode
        u = self.algo.committed_on_percent if mode != VThermHvacMode_OFF else 0.0
        self.heat += (u - self.heat) * STEP_S / 60.0 / LAG_MIN
        self.temp += A_ROOM * self.heat - B_ROOM * (self.temp - T_EXT)
        self.clock.advance(STEP_S)
        self.algo.calculate(target_temp=self.target, current_temp=round(self.temp, 2), ext_current_temp=T_EXT,
                            hvac_mode=mode)
        if mode != VThermHvacMode_OFF and not self.algo.command_is_on_off:
            self.algo._commit_applied_command(self.algo.on_percent, mode, cycle_start=True)
        self.algo._start.reading.run_queued_sync()   # the fit of a reading, run by the handler in the product
        self.trace.append((self.clock.mono, self.algo._start.pulse.state, self.algo.committed_on_percent))

    def run(self, minutes: int, until=None, mode=None) -> bool:
        for _ in range(minutes):
            self.tick(mode)
            if until is not None and until():
                return True
        return False


def _calibration(algo: SmartPI) -> dict:
    return algo.get_published_diagnostics()["calibration"]


# -- request ---------------------------------------------------------------------------------------------------
def test_a_request_during_the_learning_stage_or_with_the_learning_paused_is_refused():
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="C")
    assert algo.phase == SmartPIPhase.HYSTERESIS and algo.force_calibration() is False
    assert _calibration(algo)["last_request"] == REQUEST_LEARNING_STAGE
    force_smartpi_stable_mode(algo)
    algo.set_learning_enabled(False)
    assert algo.force_calibration() is False and _calibration(algo)["state"] == "idle"
    assert _calibration(algo)["last_request"] == REQUEST_PAUSED
    algo.set_learning_enabled(True)
    assert algo.force_calibration() is True and _calibration(algo)["result"] == RESULT_IN_PROGRESS
    assert _calibration(algo)["last_request"] == REQUEST_STARTED
    assert algo.force_calibration() is False     # one at a time
    assert _calibration(algo)["last_request"] == REQUEST_IN_PROGRESS and _calibration(algo)["result"] == RESULT_IN_PROGRESS


def test_a_request_while_off_is_refused_but_a_window_or_a_shedding_only_defers_it(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(5)
    algo.calculate(target_temp=TARGET, current_temp=TARGET, ext_current_temp=T_EXT, hvac_mode=VThermHvacMode_OFF)
    assert algo.force_calibration() is False and _calibration(algo)["last_request"] == REQUEST_OFF
    algo.calculate(target_temp=TARGET, current_temp=TARGET, ext_current_temp=T_EXT, hvac_mode=VThermHvacMode_OFF,
                   off_reason=HVAC_OFF_REASON_WINDOW_DETECTION)
    assert algo.force_calibration() is True       # the window closes, the pulses wait for it
    algo.set_learning_enabled(False)
    algo.set_learning_enabled(True)
    algo.calculate(target_temp=TARGET, current_temp=TARGET, ext_current_temp=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                   power_shedding=True)
    assert algo.force_calibration() is True


def test_a_request_within_a_day_of_the_last_calibration_is_refused_a_cancelled_one_does_not_count(clock):
    algo = _learned()
    assert algo.force_calibration() is True
    algo.set_learning_enabled(False)              # cancelled
    algo.set_learning_enabled(True)
    assert algo.force_calibration() is True
    algo._start.calibration.finish(RESULT_PULSES_DONE)
    clock.advance(CALIBRATION_MIN_INTERVAL_S - 60.0)
    assert algo.force_calibration() is False and _calibration(algo)["last_request"] == REQUEST_TOO_SOON
    assert _calibration(algo)["result"] == RESULT_PULSES_DONE    # the result of the last calibration stays
    clock.advance(60.0)
    assert algo.force_calibration() is True


@pytest.mark.parametrize("start", ["bootstrap", "direct"])
def test_a_calibration_runs_pulses_in_either_start_mode_through_the_on_off_path(clock, start):
    algo = _learned(start)
    room = _Room(algo, clock)
    room.run(240)                                # the PI settles near the setpoint
    assert algo.force_calibration() is True
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration)
    assert algo.phase == SmartPIPhase.CALIBRATION and algo.command_is_on_off is True
    assert _calibration(algo)["state"] == "rest" and _calibration(algo)["pulses_done"] == 0   # counts once it heats
    assert algo.get_published_diagnostics()["learning"]["start"]["pulse"] == "none"
    assert room.run(300, until=lambda: not algo._start.pulse.active)
    pulse = algo._start.pulse.history[-1]
    assert pulse["calibration"] is True and pulse["heat_start_s"] is not None and pulse["armed"] is True
    assert pulse["end_reason"] == "peak"                              # the event closed, the stop was measured
    assert algo.phase == SmartPIPhase.STABLE and _calibration(algo)["state"] == "waiting"
    assert _calibration(algo)["deadtime_start_s"] is not None and _calibration(algo)["deadtime_stop_s"] is not None
    assert _calibration(algo)["pulses_done"] == 1
    # the pulses of a calibration do not count for the learning stage
    assert algo._start.pulse.count_for("heat") == 0
    assert algo.get_published_diagnostics()["learning"]["start"]["pulses_done"] == 0


def test_the_detector_gets_the_committed_command_once_per_calculation_during_a_calibration_pulse(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(240)
    algo.force_calibration()
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration)
    calls: list[tuple[float, float]] = []
    update = algo.dt_est.update

    def spy(**kwargs):
        calls.append((kwargs["now"], kwargs["u_applied"], algo.committed_on_percent, algo.on_percent))
        return update(**kwargs)

    algo.dt_est.update = spy
    while algo._start.pulse.for_calibration:
        before = len(calls)
        room.tick()
        assert len(calls) - before == 1 and calls[-1][0] == clock.mono
        if algo._start.pulse.for_calibration:
            now, u, committed, computed = calls[-1]
            assert u == committed == computed      # committed before the feed, as for any pulse
    assert {u for _, u, _, _ in calls[:-1]} == {0.0, 1.0}


def test_pulses_are_counted_spaced_and_at_most_three_then_the_calibration_ends(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(240)
    algo.force_calibration()
    starts: list[float] = []
    for _ in range(int(CALIBRATION_MAX_S / STEP_S)):
        was = algo._start.pulse.for_calibration
        room.tick()
        if algo._start.pulse.for_calibration and not was:
            starts.append(clock.mono)
        if not algo._start.calibration.in_progress:
            break
    ends = [p["end_s"] for p in algo._start.pulse.history if p["calibration"]]
    assert 1 <= len(starts) <= PULSE_MAX_COUNT
    assert all(s - e >= CALIBRATION_GAP_MIN_S for s, e in zip(starts[1:], ends))
    cal = _calibration(algo)
    assert cal["state"] == "idle" and cal["pulses_done"] == len(starts)
    assert cal["result"] in (RESULT_AGREED, RESULT_PULSES_DONE) and cal["last_time"] is not None


# -- ends and interruptions --------------------------------------------------------------------------------------
def _start_pulse(clock) -> tuple[SmartPI, _Room]:
    algo = _learned()
    room = _Room(algo, clock)
    room.run(240)
    algo.force_calibration()
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration)
    return algo, room


def _heating_pulse(clock) -> tuple[SmartPI, _Room]:
    algo, room = _start_pulse(clock)
    assert room.run(300, until=lambda: algo._start.pulse.state == "heat")
    return algo, room


def test_a_pulse_ended_during_its_rest_does_not_count_and_the_next_one_starts_at_once(clock):
    algo, room = _start_pulse(clock)
    assert algo._start.pulse.state == "rest"
    room.target = TARGET + 0.5                   # a setpoint change ends the pulse during its rest
    room.tick()
    pulse = algo._start.pulse.history[-1]
    assert pulse["calibration"] is True and pulse["end_reason"] == "setpoint change" and pulse["heat_start_s"] is None
    assert _calibration(algo)["pulses_done"] == 0 and algo._start.calibration.in_progress
    room.target = TARGET
    ended = clock.mono
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration)
    assert clock.mono - ended < CALIBRATION_GAP_MIN_S   # no gap after a pulse that measured nothing


def test_two_agreeing_readings_end_the_calibration_and_its_pulse(clock):
    algo, room = _start_pulse(clock)
    algo._start.reading._cal_kept = {"heat": {"mode": "heat", "a_per_min": 0.05, "lag_s": 480.0, "b_per_min": 0.002,
                                              "pulses": [1, 2], "at_s": clock.mono}}
    room.tick()
    cal = _calibration(algo)
    assert cal["result"] == RESULT_AGREED and cal["state"] == "idle"
    assert cal["reading_a"] == 0.05 and cal["reading_b"] == 0.002 and cal["reading_lag_s"] == 480
    assert algo._start.pulse.active is False and algo._start.pulse.history[-1]["end_reason"] == "calibration ended"


def test_a_stop_ends_the_pulse_which_counts_and_the_calibration_goes_on(clock):
    algo, room = _heating_pulse(clock)
    room.run(5, mode=VThermHvacMode_OFF)
    assert algo._start.pulse.active is False and algo._start.pulse.history[-1]["end_reason"] == "thermostat stopped"
    assert algo._start.calibration.in_progress and _calibration(algo)["pulses_done"] == 1
    assert room.run(int(CALIBRATION_MAX_S / STEP_S), until=lambda: algo._start.pulse.state == "heat")
    assert _calibration(algo)["pulses_done"] == 2


def test_the_time_limit_of_48_hours_ends_the_calibration(clock):
    assert CALIBRATION_MAX_S == 48.0 * 3600.0
    algo, room = _start_pulse(clock)
    algo._start.calibration.start_s = clock.mono - CALIBRATION_MAX_S + 120.0
    room.tick()
    assert algo._start.calibration.in_progress
    room.tick()
    assert _calibration(algo)["result"] == RESULT_TIME_LIMIT and algo._start.pulse.active is False


def test_the_end_of_a_calibration_is_checked_during_a_guard_cut(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(5)
    algo.force_calibration()
    room.tick()
    algo._start.calibration.start_s = clock.mono - CALIBRATION_MAX_S
    algo.guards._guard_cut_active = True
    room.tick()
    assert algo.guards.guard_cut_active and _calibration(algo)["result"] == RESULT_TIME_LIMIT


def test_a_change_of_mode_cancels_the_calibration(clock):
    algo, room = _start_pulse(clock)
    room.run(1, mode=VThermHvacMode_COOL)
    cal = _calibration(algo)
    assert cal["result"] == RESULT_CANCELLED and cal["state"] == "idle" and cal["last_time"] is None


def test_a_learning_reset_cancels_the_calibration(clock):
    algo, _room = _start_pulse(clock)
    algo.reset_learning()
    assert algo._start.calibration.in_progress is False and _calibration(algo)["result"] == RESULT_CANCELLED
    assert algo._start.pulse.active is False


def test_pausing_the_learning_cancels_the_calibration(clock):
    algo, _room = _start_pulse(clock)
    algo.set_learning_enabled(False)
    assert algo._start.calibration.in_progress is False and _calibration(algo)["result"] == RESULT_CANCELLED


# -- persistence ----------------------------------------------------------------------------------------------------
def test_the_calibration_and_its_count_survive_a_restart_and_the_pulse_in_progress_counts(clock):
    algo, room = _heating_pulse(clock)
    state = json.loads(json.dumps(algo.save_state()))
    clock.advance(600.0)
    other = _learned(saved_state=state)
    cal = other._start.calibration
    assert cal.in_progress and cal.pulses == 1 and cal.mode == "heat"
    assert cal.start_s == pytest.approx(algo._start.calibration.start_s)
    assert other._start.pulse.active is False and other._start.pulse.count == 1
    assert other.get_published_diagnostics()["calibration"]["state"] == "waiting"


def test_a_saved_state_of_the_former_calibration_and_automatic_calibration_loads_without_a_calibration(clock):
    algo = _learned()
    state = json.loads(json.dumps(algo.save_state()))
    state["cal_state"] = {
        "last_calibration_time": 1_699_000_000.0, "calibration_state": "HeatUp", "calibration_start_time": 1_699_990_000.0,
        "force_calibration_requested": True, "calibration_mode": "heat", "calibration_pause_start": None,
        "calibration_pause_mode": None, "calibration_result": "Pending", "retry_count": 2,
    }
    state["ac_state"] = {"state": "triggered", "retry_count": 2, "model_degraded": True, "snapshot_ts": 1_699_000_000.0}
    other = _learned(saved_state=state)
    assert other._start.calibration.in_progress is False and other.phase == SmartPIPhase.STABLE
    cal = other.get_published_diagnostics()["calibration"]
    assert cal["state"] == "idle" and cal["result"] is None and cal["last_time"] is not None
    assert "autocalib" not in other.get_published_diagnostics()
    assert "cal_state" not in other.save_state() and "ac_state" not in other.save_state()


# -- limits of the calibration pulses --------------------------------------------------------------------------------
class _Est:
    """Dead-time estimator as the pulses read it."""

    def __init__(self, active=(None, False), passive=(None, False), history=()) -> None:
        self._active, self._passive = active, passive
        self.tin_history = list(history)
        self.heat_start_time = None
        self.state = "WAITING_HEAT_RESPONSE"
        self.last_outcome: dict = {}
        self.stop_response_pending = False

    def active_deadtime(self, _mode):
        return self._active

    def passive_deadtime(self, _mode):
        return self._passive


def test_the_limits_follow_the_dead_times_and_the_horizon_within_their_bounds():
    est = _Est(active=(1200.0, True), passive=(1800.0, True))
    lim = pulse_limits(est, VThermHvacMode_HEAT, 2400.0)
    assert lim.gap_s == 4800.0 and lim.rest_s == 1800.0 + CALIBRATION_REST_EXTRA_S
    assert lim.heat_max_s == 2.0 * (1200.0 + 2400.0) and lim.trough_lead_s == 1200.0
    short = pulse_limits(_Est(), VThermHvacMode_HEAT, 0.0)       # nothing known: the lower bounds
    assert short.gap_s == CALIBRATION_GAP_MIN_S and short.heat_max_s == CALIBRATION_HEAT_MIN_S
    assert short.rest_s == CALIBRATION_REST_EXTRA_S and short.trough_lead_s == 0.0
    slow = pulse_limits(_Est(active=(9000.0, True), passive=(20000.0, True)), VThermHvacMode_HEAT, 12000.0)
    assert slow.gap_s == CALIBRATION_GAP_MAX_S and slow.heat_max_s == CALIBRATION_STEP_MAX_S
    assert slow.rest_s == CALIBRATION_STEP_MAX_S
    assert pulse_limits(_Est(active=(1200.0, False)), VThermHvacMode_HEAT, 0.0).trough_lead_s == 0.0   # not reliable


def test_the_fall_rate_is_read_on_the_demand_side():
    falling = [(t * 60.0, 20.0 - 0.001 * t) for t in range(30)]    # 0.001 C/min
    assert fall_rate(falling, cooling=False) == pytest.approx(0.001 / 60.0)
    assert fall_rate(falling, cooling=True) == 0.0
    assert fall_rate([(0.0, 20.0)], cooling=False) == 0.0


def _limits(**kw) -> PulseLimits:
    base = dict(start_over_c=0.2, gap_s=3600.0, rest_s=1200.0, trough_lead_s=0.0, fall_c_per_s=0.0,
                heat_max_s=7200.0, depth_cap_c=0.8, step_max_s=CALIBRATION_STEP_MAX_S)
    base.update(kw)
    return PulseLimits(**base)


def _step(pulse: MeasurePulse, now: float, temp: float, est: _Est, limits: PulseLimits | None, calibration=True,
          learning_stage=False) -> None:
    pulse.update(now, TARGET, temp, VThermHvacMode_HEAT, cooling=False, heat_or_cool=True,
                 learning_stage=learning_stage, active_deadtime_known=False, passive_deadtime_known=False,
                 dt_est=est, calibration=calibration, calibration_pulses_left=calibration,
                 limits=limits if calibration else None)


def test_a_calibration_pulse_may_start_past_the_setpoint_a_pulse_of_the_learning_stage_may_not():
    est = _Est()
    learning = MeasurePulse()
    _step(learning, 0.0, TARGET + 0.15, est, None, calibration=False, learning_stage=True)
    assert not learning.active
    pulse = MeasurePulse()
    _step(pulse, 0.0, TARGET + 0.25, est, _limits())
    assert not pulse.active                       # further than the window
    _step(pulse, 60.0, TARGET + 0.15, est, _limits())
    assert pulse.active and pulse.for_calibration


def test_a_calibration_pulse_waits_its_gap_after_the_previous_one():
    pulse = MeasurePulse()
    pulse._last_end_s = 0.0
    _step(pulse, 3000.0, TARGET, _Est(), _limits(gap_s=3600.0))
    assert not pulse.active
    _step(pulse, 3600.0, TARGET, _Est(), _limits(gap_s=3600.0))
    assert pulse.active


def test_the_rest_of_a_calibration_pulse_holds_the_free_running_window_or_ends_on_the_predicted_lowest_point():
    est = _Est()
    pulse = MeasurePulse()
    _step(pulse, 0.0, TARGET, est, _limits(rest_s=1500.0))
    _step(pulse, 1440.0, TARGET - 0.45, est, _limits(rest_s=1500.0))
    assert pulse.state == "rest"                  # 0.45 C under, no fall counted: the window goes on
    _step(pulse, 1500.0, TARGET - 0.45, est, _limits(rest_s=1500.0))
    assert pulse.state == "heat" and pulse.current["rest_end_reason"] == "time limit"
    falling = MeasurePulse()
    lim = _limits(rest_s=1500.0, trough_lead_s=1200.0, fall_c_per_s=0.2 / 1200.0)
    _step(falling, 0.0, TARGET, est, lim)
    _step(falling, 600.0, TARGET - 0.25, est, lim)
    assert falling.state == "rest"                # lowest point 0.45 C under
    _step(falling, 660.0, TARGET - 0.31, est, lim)
    assert falling.state == "heat" and falling.current["rest_end_reason"] == "room under the setpoint"


def _to_stop(pulse: MeasurePulse, est: _Est, lim: PulseLimits, stop_temp: float) -> None:
    _step(pulse, 0.0, TARGET, est, lim)
    _step(pulse, lim.rest_s, TARGET - 0.2, est, lim)
    assert pulse.state == "heat"
    est.heat_start_time = lim.rest_s              # the estimator armed its detection at that start
    _step(pulse, lim.rest_s + 60.0, TARGET - 0.3, est, lim)
    _step(pulse, lim.rest_s + lim.heat_max_s, stop_temp, est, lim)


def test_the_heating_step_of_a_calibration_pulse_ends_on_its_own_limit():
    est, lim = _Est(), _limits(heat_max_s=9000.0)
    pulse = MeasurePulse()
    _to_stop(pulse, est, lim, TARGET - 0.1)
    assert pulse.state == "stop" and pulse.current["heat_s"] == 9000.0


def test_the_regulation_takes_over_a_calibration_stop_only_once_the_room_goes_deeper_or_at_the_cap():
    est, lim = _Est(), _limits()
    pulse = MeasurePulse()
    _to_stop(pulse, est, lim, TARGET - 0.6)       # the heating ends 0.6 C under: the stop step runs
    assert pulse.state == "stop"
    now = lim.rest_s + lim.heat_max_s
    _step(pulse, now + 60.0, TARGET - 0.6, est, lim)
    assert pulse.state == "stop"
    _step(pulse, now + 120.0, TARGET - 0.61, est, lim)
    assert not pulse.active and pulse.history[-1]["end_reason"] == "room under the setpoint"
    capped = MeasurePulse()
    _to_stop(capped, est, lim, TARGET - 0.8)
    assert not capped.active and capped.history[-1]["end_reason"] == "room under the setpoint"


# -- correction of the model ------------------------------------------------------------------------------------------
def _modelled(start: str = "bootstrap", a: float = A_ROOM, b: float = B_ROOM, mode=VThermHvacMode_HEAT) -> SmartPI:
    """A learned thermostat whose model is ``a`` and ``b`` (``a`` signed by the mode)."""
    algo = _learned(start)
    est = algo.est
    sign = -1.0 if mode == VThermHvacMode_COOL else 1.0
    if mode == VThermHvacMode_COOL:
        est.ensure_hvac_mode(VThermHvacMode_COOL)
        est.learn_ok_count_a = 1
    est.a_meas_hist.clear()
    est.a_meas_hist.extend([sign * a] * 31)
    est.b_meas_hist.clear()
    est.b_meas_hist.extend([b] * 31)
    est._b_hat_hist.extend([b] * 5)
    est.a, est.b = sign * a, b
    est.learn_ok_count, est.learn_ok_count_b = 30, 5   # the feed-forward ramp is complete, b reliable
    algo._cycles_since_reset = 100
    if start == "bootstrap":
        est.start.own_b = True
    assert algo.phase == SmartPIPhase.STABLE
    return algo


def _agree(algo: SmartPI, clock: _Clock, room: _Room, a=A_ROOM, b=B_ROOM, lag=480.0, b_agree=True, mode="heat") -> None:
    """A calibration whose two readings agree on ``a``, ``b`` and ``lag`` ends at the next calculation."""
    assert algo.force_calibration() is True
    room.tick(VThermHvacMode_COOL if mode == "cool" else None)
    algo._start.reading._cal_kept = {mode: {"mode": mode, "a_per_min": a, "lag_s": lag, "b_per_min": b,
                                            "b_agree": b_agree, "pulses": [1, 2], "at_s": clock.mono}}
    room.tick(VThermHvacMode_COOL if mode == "cool" else None)
    assert _calibration(algo)["result"] == RESULT_AGREED


@pytest.mark.parametrize("start", ["bootstrap", "direct"])
def test_a_heating_rate_more_than_30_percent_off_is_corrected_and_stands_until_the_learning_has_its_own(clock, start):
    algo = _modelled(start)
    room = _Room(algo, clock)
    room.run(5)
    ramp = algo.est.ramp_count
    _agree(algo, clock, room, a=0.08)
    cal = _calibration(algo)
    assert cal["corrected"] == ["heating rate"] and cal["in_force_a"] == A_ROOM and cal["reading_a"] == 0.08
    assert algo.est.a == pytest.approx(0.08) and algo.est.start.standing("a") and algo.est.start.value("a") == 0.08
    assert len(algo.est.a_meas_hist) == 0 and algo.est.learn_ok_count_a == 0
    assert algo.est.ramp_count >= ramp            # the restart of the channel does not lower the ramp
    assert algo.phase == SmartPIPhase.STABLE


def test_a_heating_rate_within_30_percent_is_not_corrected(clock):
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(5)
    _agree(algo, clock, room, a=0.06)
    assert _calibration(algo)["corrected"] == [] and algo.est.a == A_ROOM and not algo.est.start.standing("a")
    assert _calibration(algo)["in_force_a"] == A_ROOM


def test_the_heating_rate_of_cooling_is_corrected_with_its_sign(clock):
    algo = _modelled(mode=VThermHvacMode_COOL)
    room = _Room(algo, clock)
    room.mode = VThermHvacMode_COOL
    room.run(5)
    _agree(algo, clock, room, a=0.08, mode="cool")
    assert _calibration(algo)["corrected"] == ["heating rate"] and algo.est.a == pytest.approx(-0.08)


def test_with_its_switch_the_loss_coefficient_is_corrected_only_when_both_readings_agree_on_it(clock, monkeypatch):
    monkeypatch.setattr(calibration_module, "CALIBRATION_CORRECT_B", True)
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(5)
    _agree(algo, clock, room, b=2.0 * B_ROOM, b_agree=False)
    assert _calibration(algo)["corrected"] == [] and algo.est.b == B_ROOM
    clock.advance(CALIBRATION_MIN_INTERVAL_S)
    _agree(algo, clock, room, b=2.0 * B_ROOM)
    assert _calibration(algo)["corrected"] == ["loss coefficient"] and _calibration(algo)["in_force_b"] == B_ROOM
    est = algo.est
    assert est.b == 2.0 * B_ROOM and est.start.standing("b") and not est.start.own_b
    assert len(est.b_meas_hist) == 0 and len(est._b_hat_hist) == 0
    room.run(5)
    assert algo.phase == SmartPIPhase.STABLE      # the bootstrap start does not go back to its learning stage


def test_the_loss_coefficient_is_read_and_published_but_not_corrected(clock):
    assert calibration_module.CALIBRATION_CORRECT_B is False
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(5)
    _agree(algo, clock, room, a=0.08, b=2.0 * B_ROOM)
    cal = _calibration(algo)
    assert cal["corrected"] == ["heating rate"] and algo.est.b == B_ROOM and not algo.est.start.standing("b")
    assert cal["reading_b"] == round(2.0 * B_ROOM, 6) and cal["in_force_b"] == B_ROOM


def test_the_fallback_switch_keeps_a_value_the_learnings_own_measures_agree_with(clock, monkeypatch):
    monkeypatch.setattr(calibration_module, "CALIBRATION_SKIP_WHEN_OWN_AGREE", True)
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(5)
    _agree(algo, clock, room, a=0.08)
    assert _calibration(algo)["corrected"] == [] and algo.est.a == A_ROOM
    algo.est.a_meas_hist.extend([0.075] * 3)          # the learning's own measures now contradict the value in force
    clock.advance(CALIBRATION_MIN_INTERVAL_S)
    _agree(algo, clock, room, a=0.08)
    assert _calibration(algo)["corrected"] == ["heating rate"]


def test_the_emitter_lag_is_corrected_with_the_direct_start_only_and_never_sets_the_horizon(clock):
    for start in ("bootstrap", "direct"):
        algo = _modelled(start)
        room = _Room(algo, clock)
        room.run(5)
        horizon = algo.anticipation_horizon(VThermHvacMode_HEAT)
        given = algo.learn_win.response_lag._given
        _agree(algo, clock, room, lag=3.0 * horizon.seconds)
        room.tick()
        cal = _calibration(algo)
        assert cal["in_force_lag_s"] == round(horizon.seconds)
        assert algo.anticipation_horizon(VThermHvacMode_HEAT) == horizon
        if start == "direct":
            assert cal["corrected"] == ["emitter lag"]
            assert algo.learn_win.response_lag._given == 3.0 * horizon.seconds
        else:
            assert cal["corrected"] == [] and algo.learn_win.response_lag._given == given


def test_the_command_stays_continuous_at_a_correction(clock):
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(360)                                 # the PI settles with its feed-forward
    before = algo.on_percent
    ff_before = algo._last_ff_result.u_ff1
    _agree(algo, clock, room, a=0.08)
    ff_after = algo._last_ff_result.u_ff1
    assert abs(ff_after - ff_before) > 0.02       # the learned holding power stepped ...
    assert algo._ff_holding.last_delta == pytest.approx(ff_after - ff_before, abs=2e-3)   # ... into the integral
    assert abs(algo.on_percent - before) <= 0.02  # the command moves by 2 points at most


def test_corrected_values_and_the_corrected_lag_survive_a_restart(clock, monkeypatch):
    monkeypatch.setattr(calibration_module, "CALIBRATION_CORRECT_B", True)
    algo = _modelled("bootstrap")
    room = _Room(algo, clock)
    room.run(5)
    assert not algo.est.start.handed              # nothing was handed over by the readings of the start
    _agree(algo, clock, room, a=0.08, b=2.0 * B_ROOM)
    state = json.loads(json.dumps(algo.save_state()))
    other = _learned(saved_state=state)
    est = other.est
    assert est.start.standing("a") and est.start.value("a") == pytest.approx(0.08) and est.a == pytest.approx(0.08)
    assert est.start.standing("b") and est.b == 2.0 * B_ROOM and est.start.corrected == {"a", "b"}
    cal = other.get_published_diagnostics()["calibration"]
    assert cal["corrected"] == ["heating rate", "loss coefficient"] and cal["in_force_a"] == A_ROOM
    assert cal["last_request"] == REQUEST_STARTED
    direct = _modelled("direct")
    room = _Room(direct, clock)
    room.run(5)
    clock.advance(CALIBRATION_MIN_INTERVAL_S)
    _agree(direct, clock, room, lag=2400.0)
    restored = _learned("direct", saved_state=json.loads(json.dumps(direct.save_state())))
    assert restored._start.calibration.lag_s == {"heat": 2400.0}
    room = _Room(restored, clock)
    room.tick()
    assert restored.learn_win.response_lag._given == 2400.0


def test_a_learning_reset_drops_the_corrections(clock):
    algo = _modelled("direct")
    room = _Room(algo, clock)
    room.run(5)
    _agree(algo, clock, room, a=0.08, lag=2400.0)
    algo.reset_learning()
    assert not algo.est.start.standing("a") and algo._start.calibration.lag_s == {}


def test_the_threshold_is_relative_to_the_reading():
    """A heating rate in force 34 % over the reading is corrected, although it is only 25 % off the value in force."""
    reading = {"a_per_min": 0.0331, "b_per_min": None, "lag_s": 300.0, "b_agree": False}
    in_force = {"a": 0.0443, "b": 0.0007, "lag_s": 300.0}
    assert "a" in calibration_module.corrections(reading, in_force, {}, lag=False)
    in_force["a"] = 0.0420   # 27 % over the reading: left as it is
    assert "a" not in calibration_module.corrections(reading, in_force, {}, lag=False)


# -- the end of a calibration, the learning and the integral around its pulses ------------------------------------------
def _cal_heating(clock, a: float = A_ROOM) -> tuple[SmartPI, _Room]:
    """A learned model (``a`` in force) whose calibration pulse is in its heating step."""
    algo = _modelled(a=a)
    room = _Room(algo, clock)
    room.run(240)
    algo.force_calibration()
    assert room.run(900, until=lambda: algo._start.pulse.for_calibration and algo._start.pulse.state == "heat")
    return algo, room


def _keep(algo: SmartPI, clock: _Clock, a: float) -> None:
    algo._start.reading._cal_kept = {"heat": {"mode": "heat", "a_per_min": a, "lag_s": 480.0, "b_per_min": B_ROOM,
                                              "b_agree": True, "pulses": [1, 2], "at_s": clock.mono}}


def test_an_end_during_the_heating_step_lets_the_pulse_stop_and_the_calibration_ends_with_it(clock):
    algo, room = _cal_heating(clock)
    _keep(algo, clock, 0.08)
    room.tick()
    pulse = algo._start.pulse
    assert pulse.for_calibration and pulse.state == "stop" and pulse.current["heat_end_reason"] == "calibration ended"
    assert algo._start.calibration.in_progress and algo.est.a == A_ROOM      # nothing corrected yet
    while pulse.for_calibration:
        room.tick()
        assert algo._start.calibration.in_progress == pulse.for_calibration
    ended = pulse.history[-1]
    assert ended["end_reason"] != "calibration ended" and ended["end_s"] == clock.mono   # its own stop rules
    cal = _calibration(algo)
    assert cal["result"] == RESULT_AGREED and cal["corrected"] == ["heating rate"] and algo.est.a == pytest.approx(0.08)
    assert algo.phase == SmartPIPhase.STABLE


def test_a_cancellation_during_the_heating_step_ends_the_pulse_at_once(clock):
    algo, room = _cal_heating(clock)
    room.run(1, mode=VThermHvacMode_COOL)
    assert not algo._start.pulse.active and _calibration(algo)["result"] == RESULT_CANCELLED


def test_no_heating_rate_is_measured_while_a_calibration_is_in_progress(clock):
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(5)
    est = algo.est
    assert est.calibrating is False
    algo.force_calibration()
    room.tick()
    assert est.calibrating is True
    measures, b_count = list(est.a_meas_hist), est.learn_ok_count_b
    est.learn(0.02, 1.0, TARGET, T_EXT)                  # a heating window: no a measure
    assert est.learn_last_reason == "skip: calibration in progress" and list(est.a_meas_hist) == measures
    est.learn(-0.0003 * (TARGET - T_EXT), 0.0, TARGET, T_EXT)   # a passive window: b goes on
    assert est.learn_last_reason != "skip: calibration in progress" and est.learn_ok_count_b == b_count + 1
    _keep(algo, clock, A_ROOM)
    room.tick()
    assert not algo._start.calibration.in_progress and est.calibrating is False


def test_the_integral_is_held_after_a_calibration_pulse_until_the_room_reaches_the_setpoint(clock):
    algo, room = _cal_heating(clock)
    assert room.run(600, until=lambda: algo._start.pulse.state == "stop")
    room.temp = TARGET - 0.7                     # the room falls under the setpoint: the regulation takes over
    room.tick()
    assert not algo._start.pulse.active and algo._start.pulse.history[-1]["end_reason"] == "room under the setpoint"
    for _ in range(30):                          # the room stays under (the dead-time hold comes first)
        room.temp = TARGET - 0.7
        room.tick()
        if algo.ctl.last_integral_hold_source != "deadtime":
            break
    assert algo._start.recovering and algo.ctl.last_integral_hold_source == "calibration_recovery"
    assert algo.get_published_diagnostics()["temperature"]["integral_hold_source"] == "calibration_recovery"
    held = algo.Ki * algo.integral
    room.temp = TARGET - 0.7
    room.tick()
    assert algo.Ki * algo.integral == pytest.approx(held)
    room.temp = TARGET + 0.05                    # the setpoint is reached: the integral runs again
    room.tick()
    assert not algo._start.recovering and algo.ctl.last_integral_hold_source != "calibration_recovery"


def _correction_away_from_the_setpoint(clock, corrected: bool) -> dict:
    """Terms of the command at the calculation where a calibration corrects ``a`` with the room 0.6 C under the
    setpoint (no calibration pulse starts); without the correction (``corrected`` False) the same calculations."""
    clock.mono, clock.wall = 100_000.0, 1_700_000_000.0
    algo = _modelled()
    room = _Room(algo, clock)
    room.run(360)
    room.temp -= 0.6
    room.run(3)
    algo.force_calibration()
    room.tick()
    if corrected:
        _keep(algo, clock, 0.08)
    room.tick()
    assert _calibration(algo)["corrected"] == (["heating rate"] if corrected else []) and algo.error > 0.3
    ctl = algo.ctl
    return {"u_ff": ctl.u_ff, "u_pi": ctl.u_pi, "u_p": ctl.u_p, "ki_i": algo.Ki * ctl.integral, "kp": algo.Kp,
            "ki": algo.Ki, "error": algo.error}


def test_the_feedforward_step_of_a_correction_goes_into_the_integral_and_the_kp_change_does_not(clock):
    without = _correction_away_from_the_setpoint(clock, False)
    after = _correction_away_from_the_setpoint(clock, True)
    assert abs(after["kp"] - without["kp"]) > 0.1 * without["kp"]       # the correction changes Kp
    ff_step = after["u_ff"] - without["u_ff"]
    assert abs(ff_step) > 0.02
    # the feed-forward step is moved into the integral: feed-forward + integral term does not move
    assert abs((after["u_ff"] + after["ki_i"]) - (without["u_ff"] + without["ki_i"])) <= 0.005
    # the Kp change x error is not: the command follows the change of the proportional term
    assert abs((after["u_ff"] + after["u_pi"]) - (without["u_ff"] + without["u_pi"])
               - (after["u_p"] - without["u_p"])) <= 0.005
