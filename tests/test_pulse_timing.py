"""Timing of the measurement pulses: the quiet window, the setpoint log, the automatic calibration after an upgrade, the
gate on the start of the pulses, and the end-of-calibration report."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi import calibration_report
from custom_components.vtherm_smartpi.smartpi.auto_calibration import (
    AUTO_CALIBRATION_ATTEMPTS,
    AUTO_CALIBRATION_RETRY_S,
    AUTO_CALIBRATION_WAIT_S,
    AutoCalibration,
)
from custom_components.vtherm_smartpi.smartpi.calibration import RESULT_AGREED, RESULT_CANCELLED, RESULT_TIME_LIMIT
from custom_components.vtherm_smartpi.smartpi.measure_event import MODE_HEAT, SOURCE_PULSE
from custom_components.vtherm_smartpi.smartpi.measure_pulse import MeasurePulse
from custom_components.vtherm_smartpi.smartpi.pulse_timing import (
    DAY_S,
    LOG_MAX,
    QUIET_STABLE_S,
    SetpointLog,
    is_dark,
    pulse_window_s,
    quiet_window,
)
from custom_components.vtherm_smartpi.smartpi.quiet_periods.solar import validate_position

from .test_smartpi_calibration import TARGET, _calibration, _Clock, _learned, _Room, _start_pulse, clock  # noqa: F401

POSITION = validate_position(48.85, 2.35)
H = 3600.0
NIGHT = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc).timestamp()    # dark at the position
NOON = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp()    # light


def _log(wall: float, since_days: float = 0.0, changes: tuple[float, ...] = ()) -> SetpointLog:
    """A log that has watched since ``since_days`` before ``wall`` with the given changes (as ``wall - hours``)."""
    log = SetpointLog()
    log.load({"since": wall - since_days * DAY_S, "changes": [wall - c * H for c in changes], "target": TARGET,
              "mode": "heat", "seen": wall})
    return log


# -- the quiet window --------------------------------------------------------------------------------------------------
def test_the_window_needs_darkness_and_a_known_position():
    assert is_dark(POSITION, NIGHT) and not is_dark(POSITION, NOON)
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 0.5)) is True
    assert quiet_window(NOON, POSITION, _log(NOON, 0.5)) is False
    assert quiet_window(NIGHT, None, _log(NIGHT, 0.5)) is False


def test_the_setpoint_and_the_mode_must_have_been_unchanged_for_an_hour():
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 5, changes=(0.9,))) is False
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 5, changes=(1.1,))) is True
    # a log that began less than an hour ago does not know how long the setpoint has stood
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 0.02)) is False
    assert QUIET_STABLE_S == H


def test_the_two_previous_days_are_looked_at_only_when_the_log_covers_them():
    short = _log(NIGHT, 1.5, changes=(24 + 1.0,))        # a change in the hours of yesterday, but the log is short
    assert quiet_window(NIGHT, POSITION, short) is True
    assert quiet_window(NIGHT, POSITION, short, require_history=True) is False
    for day in (1, 2):
        busy = _log(NIGHT, 3, changes=(24.0 * day - 1.0,))   # an hour into the window of that day
        assert quiet_window(NIGHT, POSITION, busy) is False
        assert quiet_window(NIGHT, POSITION, _log(NIGHT, 3, changes=(24.0 * day + 1.0,))) is True   # before the window
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 3), require_history=True) is True


def test_the_hours_looked_at_follow_six_emitter_lags_within_three_and_six_hours():
    assert pulse_window_s(None) == 3 * H and pulse_window_s(0.0) == 3 * H and pulse_window_s(600.0) == 3 * H
    assert pulse_window_s(3600.0) == 6 * H and pulse_window_s(7200.0) == 6 * H and pulse_window_s(2700.0) == 4.5 * H
    # a change 24 h - 5 h before: inside the window of an emitter lag of 1 h (6 h), outside the one of 30 min (3 h)
    log = _log(NIGHT, 3, changes=(24.0 - 5.0,))
    assert quiet_window(NIGHT, POSITION, log, lag_s=3600.0) is False
    assert quiet_window(NIGHT, POSITION, log, lag_s=1800.0) is True
    assert quiet_window(NIGHT, POSITION, log, lag_s=None) is True
    assert quiet_window(NIGHT, POSITION, _log(NIGHT, 3, changes=(24.0 - 6.5,)), lag_s=7200.0) is True


# -- the setpoint log -------------------------------------------------------------------------------------------------
def test_the_log_records_the_changes_of_the_setpoint_and_of_the_mode_once():
    log = SetpointLog()
    log.note(1000.0, 20.0, VThermHvacMode_HEAT)
    assert log.since == 1000.0 and log.changes == [] and log.last_change == 1000.0
    log.note(1060.0, 20.0, VThermHvacMode_HEAT)
    log.note(1120.0, 19.0, VThermHvacMode_HEAT)
    log.note(1180.0, 19.0, VThermHvacMode_COOL)
    log.note(1240.0, None, VThermHvacMode_COOL)          # no setpoint: nothing noted
    assert log.changes == [1120.0, 1180.0] and log.last_change == 1180.0


def test_the_log_keeps_72_hours_within_a_bounded_size_and_counts_a_long_gap_as_a_change():
    log = SetpointLog()
    log.note(0.0, 20.0, VThermHvacMode_HEAT)
    for k in range(1, 400):
        log.note(float(k) * 60.0, 20.0 + (k % 2), VThermHvacMode_HEAT)
    assert len(log.changes) == LOG_MAX
    log.note(100 * H, 20.0, VThermHvacMode_HEAT)          # the setpoint 399 % 2 = 1: back to 20 after a long gap
    assert log.changes == [100 * H] and log.since == 0.0
    log.note(100 * H + 5 * H, 20.0, VThermHvacMode_HEAT)  # a gap with no change of value: still a change
    assert log.changes[-1] == 105 * H
    log.note(105 * H + 200 * H, 20.0, VThermHvacMode_HEAT)
    assert all(c >= 305 * H - 72 * H for c in log.changes)


def test_the_log_survives_save_and_load_and_tolerates_a_missing_or_damaged_state():
    log = SetpointLog()
    log.note(1000.0, 20.0, VThermHvacMode_HEAT)
    log.note(1500.0, 21.0, VThermHvacMode_HEAT)
    other = SetpointLog()
    other.load(log.save())
    assert (other.since, other.changes, other.last_change) == (1000.0, [1500.0], 1500.0)
    other.note(1560.0, 21.0, VThermHvacMode_HEAT)          # same value after the load: no change
    assert other.changes == [1500.0]
    for bad in (None, {}, "x", {"since": "x"}, {"since": 5.0, "changes": "x", "target": "y", "mode": 3, "seen": "z"}):
        other.load(bad)
        assert other.since is None or other.changes == []


# -- the automatic calibration after an upgrade ------------------------------------------------------------------------
def _prime(algo: SmartPI, clock: _Clock, wall: float, since_days: float = 3.0, seen_days: float | None = 4.0) -> None:
    """The wall clock at ``wall``, a setpoint log that began ``since_days`` ago without change, a carried-over state
    seen ``seen_days`` ago, a known position."""
    clock.wall = wall
    start = algo._start
    start.setpoints.load({"since": wall - since_days * DAY_S, "changes": [], "target": TARGET, "mode": "heat",
                          "seen": wall})
    if seen_days is not None:
        start.auto.seen["heat"] = wall - seen_days * DAY_S
    start.model_reading.position = POSITION


def _auto(algo: SmartPI) -> bool:
    """One look of the automatic calibration; True when it started one."""
    started = algo._start.calibration.in_progress
    algo._start._auto_calibrate(VThermHvacMode_HEAT, False)
    return algo._start.calibration.in_progress and not started


def test_the_automatic_calibration_starts_at_a_quiet_night_after_three_days(clock):
    algo = _learned()
    _prime(algo, clock, NIGHT, seen_days=AUTO_CALIBRATION_WAIT_S / DAY_S - 0.01)
    assert _auto(algo) is False
    assert _calibration(algo)["automatic_status"] == "waiting: 3 days after the update"
    _prime(algo, clock, NIGHT, seen_days=AUTO_CALIBRATION_WAIT_S / DAY_S)
    assert _auto(algo) is True
    cal = _calibration(algo)
    assert cal["automatic"] is True and cal["automatic_status"] == "in progress" and cal["state"] == "waiting"
    assert algo._start.auto.attempts == {"heat": 1}


def test_the_automatic_calibration_needs_the_quiet_window_with_two_days_of_history(clock):
    algo = _learned()
    _prime(algo, clock, NOON)
    assert _auto(algo) is False and _calibration(algo)["automatic_status"] == "waiting: quiet night"
    _prime(algo, clock, NIGHT, since_days=1.5)
    assert _auto(algo) is False
    _prime(algo, clock, NIGHT)
    algo._start.setpoints.changes = [NIGHT - 24 * H + H]    # the setpoint moved in the same hours yesterday
    assert _auto(algo) is False
    algo._start.setpoints.changes = [NIGHT - 0.5 * H]       # and not an hour of stability
    assert _auto(algo) is False
    algo._start.setpoints.changes = []
    assert _auto(algo) is True


def test_without_a_home_position_there_is_no_automatic_calibration(clock):
    algo = _learned()
    _prime(algo, clock, NIGHT)
    algo._start.model_reading.position = None
    assert _auto(algo) is False and _calibration(algo)["automatic_status"] == "home position unknown"


def test_the_automatic_calibration_waits_while_the_learning_is_blocked_off_or_busy_or_a_calibration_just_ended(clock):
    algo = _learned()
    _prime(algo, clock, NIGHT)
    algo.set_learning_enabled(False)
    assert _auto(algo) is False
    algo.set_learning_enabled(True)
    _prime(algo, clock, NIGHT)
    algo._last_restart_reason = "off"
    assert _auto(algo) is False
    algo._last_restart_reason = "none"
    algo._start.calibration.last_end_wall = NIGHT - 23 * H
    assert _auto(algo) is False
    algo._start.calibration.last_end_wall = NIGHT - 25 * H
    algo._start._auto_calibrate(VThermHvacMode_HEAT, True)         # the learning stage
    assert algo._start.calibration.in_progress is False
    assert _auto(algo) is True
    assert _auto(algo) is False and algo._start.auto.attempts == {"heat": 1}   # one in progress: no second request
    algo._start.calibration.finish(RESULT_CANCELLED)
    assert algo._start.auto.attempts == {"heat": 1}


def test_at_most_three_attempts_per_mode_twenty_hours_apart(clock):
    algo = _learned()
    wall = NIGHT
    for attempt in range(AUTO_CALIBRATION_ATTEMPTS):
        _prime(algo, clock, wall)
        assert _auto(algo) is True
        algo._start.calibration.finish(RESULT_CANCELLED)             # a cancelled calibration does not hold the 24 h
        wall += AUTO_CALIBRATION_RETRY_S - 0.5 * H
        _prime(algo, clock, wall)
        assert _auto(algo) is False                                  # dark (evening), but 20 h are not over
        wall += H
    assert algo._start.auto.attempts == {"heat": AUTO_CALIBRATION_ATTEMPTS}
    _prime(algo, clock, NIGHT + 30 * DAY_S)
    algo._start.auto.attempts, algo._start.auto.last = {"heat": AUTO_CALIBRATION_ATTEMPTS}, {"heat": NIGHT}
    assert _auto(algo) is False and _calibration(algo)["automatic_status"] == "attempts used"


def test_the_automatic_calibration_stops_for_good_once_the_mode_has_a_measured_event(clock):
    algo = _learned()
    _prime(algo, clock, NIGHT)
    assert algo._start.model.values(MODE_HEAT)["source"] == "previous"
    fit = {"theta_s": 60.0, "tau_e_s": 440.0, "s_s": 500.0, "split_ok": True, "rms_c": 0.01, "reject": None}
    algo._start.model.add(MODE_HEAT, SOURCE_PULSE, fit, 0.0)
    assert _auto(algo) is False
    assert "heat" not in algo._start.auto.seen and _calibration(algo)["automatic_status"] == "not needed"


def test_the_first_sight_of_the_carried_over_state_is_kept_and_survives_a_restart(clock):
    algo = _learned()
    clock.wall = NIGHT
    algo._start.model_reading.position = POSITION
    algo._start._auto_calibrate(VThermHvacMode_HEAT, False)
    assert algo._start.auto.seen == {"heat": NIGHT}
    algo._start.auto.started(NIGHT + H, "heat")
    algo._start.setpoints.note(NIGHT, TARGET, VThermHvacMode_HEAT)
    saved = algo.save_state()
    clock.advance(10 * DAY_S)
    other = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="R")
    other.load_state(saved)
    assert other._start.auto.seen == {"heat": NIGHT} and other._start.auto.attempts == {"heat": 1}
    assert other._start.auto.last == {"heat": NIGHT + H}
    assert other._start.setpoints.since == NIGHT
    old = dict(saved)
    for key in ("setpoint_log", "auto_calibration"):
        old.pop(key, None)
    third = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="T")
    third.load_state(old)                                     # a state of an earlier version: nothing known yet
    assert third._start.auto.seen == {} and third._start.setpoints.since is None


def test_the_auto_state_loads_tolerantly():
    auto = AutoCalibration()
    for bad in (None, 3, {"seen": 3, "attempts": [], "last": "x"}, {"seen": {"heat": "x"}, "attempts": {"heat": -2}}):
        auto.load(bad)
        assert auto.seen == {} and auto.attempts == {}
    auto.load({"attempts": {"heat": 9}})
    assert auto.attempts == {"heat": AUTO_CALIBRATION_ATTEMPTS}


# -- the start of the pulses -------------------------------------------------------------------------------------------
class _Dt:
    state = "WAITING_HEAT_RESPONSE"
    heat_start_time = None
    last_outcome: dict = {}


def _update(pulse: MeasurePulse, allowed: bool, learning_stage: bool = True) -> None:
    pulse.update(0.0, TARGET, TARGET, VThermHvacMode_HEAT, cooling=False, heat_or_cool=True,
                 learning_stage=learning_stage, model_known=False, dt_est=_Dt(), start_allowed=allowed)


def test_a_pulse_starts_only_when_the_start_is_allowed():
    pulse = MeasurePulse()
    _update(pulse, False)
    assert not pulse.active
    _update(pulse, True)
    assert pulse.active


def test_the_pulses_of_the_direct_start_have_no_timing_rule(clock):
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="D",
                   learning_start="direct")
    start = algo._start
    assert start._start_allowed(VThermHvacMode_HEAT, True, False) is True            # no position
    start.model_reading.position = POSITION
    clock.wall = NOON
    start.setpoints.load({"since": NOON - 0.1 * DAY_S, "changes": [], "target": TARGET, "mode": "heat", "seen": NOON})
    assert start._start_allowed(VThermHvacMode_HEAT, True, False) is True            # light
    clock.wall = NIGHT
    start.setpoints.load({"since": NIGHT - 0.1 * DAY_S, "changes": [], "target": TARGET, "mode": "heat", "seen": NIGHT})
    assert start._start_allowed(VThermHvacMode_HEAT, True, False) is True            # dark
    start.setpoints.changes = [NIGHT - 0.5 * H]
    assert start._start_allowed(VThermHvacMode_HEAT, True, False) is True            # the setpoint just moved
    assert start._start_allowed(VThermHvacMode_HEAT, False, False) is True          # not in the learning stage


def test_a_requested_calibration_has_no_timing_rule_an_automatic_one_has(clock):
    algo = _learned()
    _prime(algo, clock, NOON, since_days=0.1, seen_days=None)
    start = algo._start
    assert algo.force_calibration() is True
    assert start._start_allowed(VThermHvacMode_HEAT, False, True) is True
    start.calibration.finish(RESULT_CANCELLED)
    start.calibration.begin(automatic=True)
    assert start._start_allowed(VThermHvacMode_HEAT, False, True) is False          # light
    _prime(algo, clock, NIGHT, since_days=0.1, seen_days=None)
    assert start._start_allowed(VThermHvacMode_HEAT, False, True) is False          # night, but no two days of history
    _prime(algo, clock, NIGHT, since_days=3, seen_days=None)
    assert start._start_allowed(VThermHvacMode_HEAT, False, True) is True


def test_an_automatic_calibration_runs_its_pulse_at_night_and_a_requested_one_at_once(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(240)
    _prime(algo, clock, NOON, seen_days=None)
    algo._start.calibration.begin(automatic=True)
    assert room.run(180, until=lambda: algo._start.pulse.for_calibration) is False   # daytime: no pulse
    algo._start.calibration.finish(RESULT_CANCELLED)
    algo.force_calibration()
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration) is True    # requested: starts at once


# -- the interruptions and the report ---------------------------------------------------------------------------------
def test_a_setpoint_change_during_a_pulse_is_recorded_with_its_time_and_the_calibration_goes_on(clock):
    algo, room = _start_pulse(clock)
    room.target = TARGET + 0.5
    room.tick()
    cal = _calibration(algo)
    assert [i["reason"] for i in cal["interruptions"]] == ["setpoint change"]
    assert cal["interruptions"][0]["at"] == datetime.fromtimestamp(clock.wall).isoformat()
    assert algo._start.calibration.in_progress and cal["automatic"] is False


def test_each_kind_of_interruption_has_its_plain_reason(clock):
    algo, room = _start_pulse(clock)
    room.run(1, mode=VThermHvacMode_OFF)
    assert [i["reason"] for i in _calibration(algo)["interruptions"]] == ["thermostat stopped"]
    algo, room = _start_pulse(clock)
    room.run(1, mode=VThermHvacMode_COOL)
    cal = _calibration(algo)
    assert [i["reason"] for i in cal["interruptions"]] == ["mode change"] and cal["result"] == RESULT_CANCELLED
    algo, room = _start_pulse(clock)
    algo.set_learning_enabled(False)
    cal = _calibration(algo)
    assert [i["reason"] for i in cal["interruptions"]] == ["learning paused"] and cal["result"] == RESULT_CANCELLED


def test_a_pulse_of_the_learning_stage_interrupted_is_not_a_calibration_interruption(clock):
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="L",
                   learning_start="direct")
    algo._start.pulse.interrupt(0.0, "thermostat stopped")
    assert algo._start.pulse.take_interruptions() == []


def test_the_report_is_taken_once_with_its_result_origin_and_interruptions(clock):
    algo, room = _start_pulse(clock)
    assert algo._start.take_report() is None
    room.target = TARGET + 0.5
    room.tick()
    room.target = TARGET
    assert room.run(600, until=lambda: algo._start.pulse.for_calibration)     # the next pulse
    algo.set_learning_enabled(False)
    report = algo._start.take_report()
    assert report["result"] == RESULT_CANCELLED and report["automatic"] is False
    assert [i["reason"] for i in report["interruptions"]] == ["setpoint change", "learning paused"]
    assert all(set(i) == {"reason", "at"} for i in report["interruptions"])
    assert report["duration_s"] == pulse_window_s(algo._start._lag_s("heat")) and report["duration_s"] >= 3 * H
    assert algo._start.take_report() is None


def test_a_learning_reset_gives_no_report_and_a_new_calibration_starts_with_no_interruption(clock):
    algo, room = _start_pulse(clock)
    room.target = TARGET + 0.5
    room.tick()
    algo.reset_learning()
    assert algo._start.take_report() is None
    cal = algo._start.calibration
    cal.begin(automatic=True)
    assert cal.interruptions == [] and cal.automatic is True


def test_the_calibration_flags_and_interruptions_survive_a_restart(clock):
    algo, room = _start_pulse(clock)
    algo._start.calibration.automatic = True
    room.target = TARGET + 0.5
    room.tick()
    other = _learned(saved_state=algo.save_state())
    cal = _calibration(other)
    assert cal["automatic"] is True and [i["reason"] for i in cal["interruptions"]] == ["setpoint change"]


# -- the event and the notification ----------------------------------------------------------------------------------
def _thermostat() -> MagicMock:
    thermostat = MagicMock()
    thermostat.entity_id, thermostat.unique_id, thermostat.name = "climate.room", "uid1", "Room"
    return thermostat


def _report(result: str = RESULT_AGREED, interruptions=(), automatic: bool = False) -> dict:
    return {"result": result, "automatic": automatic, "mode": "heat", "duration_s": 4 * H,
            "interruptions": [{"reason": r, "at": "2026-01-15T02:10:00"} for r in interruptions]}


@pytest.fixture
def notifier():
    with patch.object(calibration_report.persistent_notification, "async_create") as create, patch.object(
            calibration_report.persistent_notification, "async_dismiss") as dismiss, patch.object(
            calibration_report.translation, "async_get_translations", new=AsyncMock(return_value={})):
        yield create, dismiss


async def test_the_event_carries_the_result_origin_and_interruptions(notifier):
    thermostat = _thermostat()
    report = _report(RESULT_TIME_LIMIT, ("setpoint change",), automatic=True)
    await calibration_report.async_report_calibration(thermostat, report)
    event, payload = thermostat.hass.bus.async_fire.call_args.args
    assert event == "vtherm_smartpi_event"
    assert payload == {"entity_id": "climate.room", "type": "calibration_ended", "result": "time limit",
                       "automatic": True, "interruptions": report["interruptions"]}


async def test_a_failed_or_interrupted_calibration_creates_one_notification_per_thermostat(notifier):
    create, dismiss = notifier
    thermostat = _thermostat()
    await calibration_report.async_report_calibration(thermostat, _report(RESULT_TIME_LIMIT))
    await calibration_report.async_report_calibration(thermostat, _report(RESULT_AGREED, ("mode change",)))
    assert create.call_count == 2 and dismiss.call_count == 0
    assert {c.kwargs["notification_id"] for c in create.call_args_list} == {"vtherm_smartpi_calibration_uid1"}
    message = create.call_args.args[1]
    assert "Room" in message and "mode change" in message and "2026-01-15 02:10" in message and "4 h" in message
    assert "keep the setpoint unchanged" in message and "Room" in create.call_args.kwargs["title"]
    assert "none" in create.call_args_list[0].args[1]
    thermostat.unique_id = None
    assert calibration_report.notification_id(thermostat) == "vtherm_smartpi_calibration_climate.room"


async def test_a_clean_success_dismisses_the_notification(notifier):
    create, dismiss = notifier
    thermostat = _thermostat()
    await calibration_report.async_report_calibration(thermostat, _report())
    assert create.call_count == 0
    dismiss.assert_called_once_with(thermostat.hass, "vtherm_smartpi_calibration_uid1")


async def test_a_failure_of_the_report_is_logged_and_never_raised(notifier, caplog):
    thermostat = _thermostat()
    thermostat.hass.bus.async_fire.side_effect = RuntimeError("bus")
    await calibration_report.async_report_calibration(thermostat, _report(RESULT_CANCELLED))
    assert "Calibration report failed" in caplog.text


async def test_the_handler_side_function_sends_a_pending_report_once(clock):
    algo, _room = _start_pulse(clock)
    algo.set_learning_enabled(False)
    thermostat = _thermostat()
    tasks = []
    thermostat.hass.async_create_task = lambda coro: (tasks.append(coro), coro.close())
    calibration_report.schedule_report(thermostat, algo)
    calibration_report.schedule_report(thermostat, algo)
    assert len(tasks) == 1


def test_every_word_of_the_report_is_translated_in_english_and_french():
    import json
    from pathlib import Path

    folder = Path(calibration_report.__file__).parents[1] / "translations"
    for language in ("en", "fr"):
        issues = json.loads((folder / f"{language}.json").read_text(encoding="utf-8"))["issues"]
        for key in (*calibration_report._KEYS.values(), "calibration_none"):
            assert issues[key]["title"]
        report = issues["calibration_report"]
        for placeholder in ("{name}", "{result}", "{interruptions}", "{duration}"):
            assert placeholder in report["description"]
        assert "{name}" in report["title"]
