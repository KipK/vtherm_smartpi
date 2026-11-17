"""SmartPI does not learn heating while it calls for heat and Versatile Thermostat's central boiler
is off (documentation/en/technical_doc.md, 3.8)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import entity_registry as er

import custom_components.vtherm_smartpi.algo as algo_module
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.boiler_call import (
    BOILER_BLINK_S,
    BOILER_PAUSE_CONFIRM_S,
    BOILER_VALVE_HOLD_S,
    VT_CENTRAL_BOILER_UNIQUE_ID,
    VT_DOMAIN,
    central_boiler_on,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SMARTPI_RECALC_INTERVAL_SEC, SmartPICalibrationPhase, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.diagnostics import build_published_diagnostics
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter
from fakes.fake_thermostat_runtime import FakeThermostatRuntime
from helpers import force_smartpi_stable_mode

RECALC_S = float(SMARTPI_RECALC_INTERVAL_SEC)


# --- reading Versatile Thermostat's central boiler -------------------------------


async def test_the_boiler_is_unknown_without_its_entity(hass) -> None:
    assert central_boiler_on(hass) is None


@pytest.mark.parametrize(("state", "on"), [("on", True), ("off", False), ("unavailable", None), ("unknown", None)])
async def test_the_boiler_s_state(hass, state, on) -> None:
    entry = er.async_get(hass).async_get_or_create("binary_sensor", VT_DOMAIN, VT_CENTRAL_BOILER_UNIQUE_ID, suggested_object_id="central_boiler")
    hass.states.async_set(entry.entity_id, state)

    assert central_boiler_on(hass) is on


# --- SmartPI's learning ------------------------------------------------------------


def _smartpi() -> SmartPI:
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="Boiler")
    force_smartpi_stable_mode(smartpi)
    smartpi._committed_on_percent = 0.6
    return smartpi


def _calculate_twice(smartpi: SmartPI, monkeypatch) -> None:
    """Two calculations ten minutes apart, so that learning is fed."""
    now = [5000.0]
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: now[0])
    for _ in range(2):
        smartpi.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)
        now[0] += 600.0


@pytest.mark.parametrize("paused", [False, True])
def test_no_learning_during_the_pause(paused, monkeypatch) -> None:
    """The room model is not learned during the pause; the dead-time estimator still records the
    temperature (no detection arms while SmartPI keeps calling)."""
    smartpi = _smartpi()
    smartpi.set_boiler_learning_paused(paused)
    smartpi.learn_win.update = MagicMock(return_value=(0, 0))
    smartpi.dt_est.update = MagicMock()

    _calculate_twice(smartpi, monkeypatch)

    assert smartpi.learn_win.update.called is not paused
    assert smartpi.dt_est.update.called


def test_the_pause_drops_only_the_detection_in_flight(monkeypatch) -> None:
    """The pause drops learning's window and the armed dead-time detection (``aborted``), and
    learning's episode starts at the change; the estimator keeps its last stop and its
    temperature history, and the regulation's episode keeps its start."""
    smartpi = _smartpi()
    est = smartpi.dt_est
    est.last_stop_time = 100.0
    est.update(now=900.0, tin=19.0, u_applied=0.0)
    est.update(now=1000.0, tin=19.0, u_applied=1.0)  # 900 s without heat: a detection arms
    assert est.state == "WAITING_HEAT_RESPONSE"
    history = list(est._tin_history)
    smartpi.learn_win._active = True
    smartpi._t_heat_episode_start = 100.0
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: 1050.0)

    smartpi.set_boiler_learning_paused(True)

    assert smartpi.boiler_learning_paused is True
    assert smartpi.learn_win_active is False
    assert (est.state, est.last_outcome["heat"]) == ("HEATING", "aborted")
    assert est.last_stop_time == 100.0
    assert list(est._tin_history) == history
    assert smartpi._t_heat_episode_start == 100.0
    assert smartpi._learning_episode_start(smartpi._t_heat_episode_start) == 1050.0


def test_after_the_pause_learning_waits_for_the_room_dead_time(monkeypatch) -> None:
    """After the resume SmartPI does not learn during the room's dead time; the regulation's
    dead-time window still counts from the heating's own start."""
    smartpi = _smartpi()
    smartpi.dt_est.deadtime_heat_s = 1200.0
    smartpi.dt_est.deadtime_heat_reliable = True
    smartpi._t_heat_episode_start = 100.0
    now = [200.0]
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: now[0])
    smartpi.set_boiler_learning_paused(True)
    now[0] = 5000.0
    smartpi.set_boiler_learning_paused(False)

    now[0] = 5600.0
    assert smartpi.in_deadtime_window is False  # the regulation's window ended at 1300 s
    smartpi.update_learning(dt_min=10.0, current_temp=19.0, ext_temp=5.0, u_active=0.6, hvac_mode=VThermHvacMode_HEAT)
    assert smartpi.est.learn_last_reason == "skip: deadtime window"

    now[0] = 6300.0
    smartpi.update_learning(dt_min=10.0, current_temp=19.0, ext_temp=5.0, u_active=0.6, hvac_mode=VThermHvacMode_HEAT)
    assert smartpi.est.learn_last_reason != "skip: deadtime window"


def test_a_pause_and_its_resume_leave_the_regulation_as_without_them(monkeypatch) -> None:
    """The same inputs, once with a pause and its resume and once without: the command, the
    integral and the regulation's dead-time window (in which the controller holds its integral)
    are the same at every calculation. The valve applies each command at once. The user's
    learning setting is off in both, so that learning changes no model either way."""
    runs = []
    for pause_at, resume_at in ((3, 10), (None, None)):
        smartpi = _smartpi()
        smartpi.set_learning_enabled(False)
        smartpi.dt_est.deadtime_heat_s = 780.0
        smartpi.dt_est.deadtime_heat_reliable = True
        now = [10_000.0]
        monkeypatch.setattr(algo_module.time, "monotonic", lambda now=now: now[0])
        steps = []
        for i in range(30):
            if i == pause_at:
                smartpi.set_boiler_learning_paused(True)
            if i == resume_at:
                smartpi.set_boiler_learning_paused(False)
            smartpi.calculate(target_temp=20.0, current_temp=19.6 + 0.01 * i, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)
            smartpi.on_applied_power_updated(smartpi.on_percent, VThermHvacMode_HEAT)
            steps.append((smartpi.on_percent, smartpi.ctl.integral, smartpi.in_deadtime_window))
            now[0] += RECALC_S
        runs.append(steps)

    paused, normal = runs
    for i, (a, b) in enumerate(zip(paused, normal, strict=True)):
        assert a[0] == pytest.approx(b[0]) and a[1] == pytest.approx(b[1]) and a[2] == b[2], (i, a, b)
    assert {window for _, _, window in normal} == {True, False}  # the window opened and closed
    assert len({round(integral, 6) for _, integral, _ in normal}) > 1  # the integral moved


@pytest.mark.parametrize("pause", [None, "boiler", "user"])
def test_a_calibration_feeds_no_power_while_the_boiler_is_off(pause, monkeypatch) -> None:
    """A calibration's heating step feeds the dead-time estimator its own command, and no power
    while the central boiler is off (the boiler's pause); during the user's pause it feeds its
    command, as in the author's code."""
    smartpi = _smartpi()
    smartpi.force_calibration()
    now = [1000.0]
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: now[0])
    smartpi.calculate(target_temp=18.0, current_temp=17.9, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)
    smartpi.calibration_mgr._calibration_state = SmartPICalibrationPhase.HEAT_UP
    smartpi._committed_on_percent = 1.0
    if pause == "boiler":
        smartpi.set_boiler_state(True)
        smartpi.set_boiler_state(False)
        smartpi.set_boiler_learning_paused(True)
    elif pause == "user":
        smartpi.set_learning_enabled(False)
    smartpi.dt_est.update = MagicMock()

    now[0] += 60.0
    smartpi.calculate(target_temp=18.0, current_temp=17.9, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)

    assert smartpi.calibration_state == SmartPICalibrationPhase.HEAT_UP
    assert smartpi.on_percent == pytest.approx(1.0)
    assert smartpi.dt_est.update.call_args.kwargs["u_applied"] == (0.0 if pause == "boiler" else pytest.approx(1.0))


def _paused_while_calling(monkeypatch, now: list[float], learning: bool = True) -> SmartPI:
    """SmartPI calling at full power from 40 s with a filtered room temperature, the central
    boiler off since before the call, paused at 100 s."""
    smartpi = _smartpi()
    smartpi.set_learning_enabled(learning)
    smartpi._committed_on_percent = 0.0
    smartpi.tint_filter.update(19.0, 0.0)
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: now[0])
    now[0] = 30.0
    smartpi.set_boiler_state(True)
    smartpi.set_boiler_state(False)
    now[0] = 40.0
    smartpi._committed_on_percent = 1.0
    now[0] = 100.0
    smartpi.set_boiler_learning_paused(True)
    return smartpi


def test_the_boiler_s_start_arms_the_dead_time_detection_from_then(monkeypatch) -> None:
    """The boiler starts at 220 s while SmartPI calls, and the handler gives it 5 s later: the
    detection arms as at a start from 0, its time counted from the boiler's start, not from the
    call that waited."""
    now = [0.0]
    smartpi = _paused_while_calling(monkeypatch, now)
    assert smartpi.dt_est.state == "OFF" and smartpi._dead_time_power() == 0.0

    now[0] = 220.0
    smartpi.set_boiler_learning_paused(False)
    assert smartpi.dt_est.state == "OFF"
    now[0] = 225.0
    assert smartpi.set_boiler_state(True, since=220.0) is True

    assert smartpi.dt_est.state == "WAITING_HEAT_RESPONSE"
    assert (smartpi.dt_est.heat_start_time, smartpi.dt_est.last_start_time) == (220.0, 220.0)


@pytest.mark.parametrize(("boiler_started", "learning", "power"), [(False, True, 1.0), (True, False, 1.0), (True, True, 0.5)])
def test_no_detection_without_the_boiler_s_start(boiler_started, learning, power, monkeypatch) -> None:
    """No detection arms when the pause ends for another reason (SmartPI stopped calling), while
    the user's learning is off, or at a power the estimator does not time (its own 80 % rule)."""
    now = [0.0]
    smartpi = _paused_while_calling(monkeypatch, now, learning=learning)
    smartpi._committed_on_percent = power

    now[0] = 220.0
    smartpi.set_boiler_learning_paused(False)
    if boiler_started:
        smartpi.set_boiler_state(True, since=220.0)

    assert smartpi.dt_est.state != "WAITING_HEAT_RESPONSE"


@pytest.mark.parametrize("paused", [False, True])
def test_a_stop_seen_during_the_pause_arms_no_passive_detection(paused, monkeypatch) -> None:
    """In SmartPI's first phase the stop is committed within the calculation. Without the pause
    the estimator times the room's passive response from it (the heating lasted its dead time).
    Paused, the room called while the boiler was off since 1000 s: the estimator saw no power,
    so the room's stop changes nothing for it, and it arms nothing."""
    now = [1000.0]
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: now[0])
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="Boiler")
    smartpi.set_boiler_state(True)
    if paused:
        smartpi.set_boiler_state(False)
    smartpi._committed_on_percent = 1.0
    smartpi.dt_est.last_power = 0.0 if paused else 1.0
    dt_est = smartpi.dt_est
    dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable, dt_est.last_start_time = 120.0, True, 1000.0
    smartpi.set_boiler_learning_paused(paused)

    now[0] = 1600.0
    smartpi.calculate(target_temp=18.0, current_temp=19.5, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)

    assert smartpi.phase == SmartPIPhase.HYSTERESIS and smartpi.committed_on_percent == 0.0
    assert (smartpi.dt_est.last_stop_time == 1600.0) is not paused
    assert (smartpi.dt_est.state == "WAITING_COOL_RESPONSE") is not paused


def test_the_user_setting_is_untouched() -> None:
    """The user's pause and the boiler's pause are two separate causes."""
    smartpi = _smartpi()
    smartpi.set_learning_enabled(False)
    smartpi.set_boiler_learning_paused(True)
    smartpi.set_boiler_learning_paused(False)

    assert smartpi.learning_enabled is False
    assert smartpi._learning_active is False
    assert smartpi.save_state()["learning_enabled"] is False


def test_the_diagnostics_show_the_pause() -> None:
    smartpi = _smartpi()
    assert build_published_diagnostics(smartpi)["learning"]["boiler_paused"] is False

    smartpi.set_boiler_learning_paused(True)

    learning = build_published_diagnostics(smartpi)["learning"]
    assert (learning["boiler_paused"], learning["enabled"]) == (True, True)


def _heat_then_stop_then_start(room, *, pre_c_per_h: float, off_min: float, monkeypatch=None) -> None:
    """A room heated for an hour, its heat stopped at 0 s, free-running for ``off_min`` with a
    trend of ``pre_c_per_h``, then started at full power: its response comes 300 s later at
    3 degC/h. Fed every 60 s, read to 0.01 degC, through the regulation's filter, until the
    detection ends. ``room`` is the author's estimator, whose room stops itself at 0 s, or SmartPI,
    fed as it feeds its estimator: the central boiler goes off at 0 s while the room still calls,
    learning pauses a calculation period later, and the room stops calling at 300 s, which ends
    the pause, as the boiler comes back for other rooms."""
    smartpi = room if isinstance(room, SmartPI) else None
    est = smartpi.dt_est if smartpi else room
    filt = smartpi.tint_filter if smartpi else AdaptiveTintFilter("test")
    start = off_min * 60.0
    now = [-3600.0]
    if smartpi:
        monkeypatch.setattr(algo_module.time, "monotonic", lambda: 10_000.0 + now[0])
        smartpi.set_boiler_state(True)
    while now[0] <= start + 7200.0:
        t = now[0]
        temp = 20.0 + t / 3600.0 if t < 0.0 else 20.0 + pre_c_per_h * t / 3600.0 + 3.0 * max(0.0, t - start - 300.0) / 3600.0
        raw = round(temp, 2)
        lp, _ = filt.update(raw, 10_000.0 + t)
        if smartpi:
            smartpi._committed_on_percent = 1.0 if t < 300.0 or t >= start else 0.0
            if t == 0.0:
                smartpi.set_boiler_state(False, current_temp=raw)
            if t == RECALC_S:
                smartpi.set_boiler_learning_paused(True)
            if t == 300.0:
                smartpi.set_boiler_learning_paused(False)
                smartpi.set_boiler_state(True, current_temp=raw, since=10_000.0 + t)
            power = smartpi._dead_time_power()
        else:
            power = 1.0 if t < 0.0 or t >= start else 0.0
        est.update(10_000.0 + t, lp, power, hvac_mode=VThermHvacMode_HEAT, tin_raw=raw)
        if t >= start and est.state != "WAITING_HEAT_RESPONSE":
            return
        now[0] += 60.0


@pytest.mark.parametrize(("pre_c_per_h", "off_min"), [(0.0, 60.0), (0.2, 60.0), (-0.3, 60.0), (0.2, 10.0)])
def test_a_pause_leaves_the_next_start_s_dead_time_as_without_it(pre_c_per_h, off_min, monkeypatch) -> None:
    """The dead time is measured against the room's trend before the start, from the period
    without heating. The boiler's stop while the room calls is the end of its heat, and the pause
    after it keeps the estimator's history and last stop, so the next start's outcome, dead time
    and trend, and the passive response timed from the stop, are those of the same room stopping
    itself at that moment (the author's estimator with the same learned dead times); the room's
    own stop during the pause changes nothing."""
    paused = _smartpi()
    normal = DeadTimeEstimator()
    for name in ("deadtime_heat_s", "deadtime_cool_s", "deadtime_heat_reliable", "deadtime_cool_reliable", "active_settle_s"):
        setattr(normal, name, getattr(paused.dt_est, name))
    _heat_then_stop_then_start(paused, pre_c_per_h=pre_c_per_h, off_min=off_min, monkeypatch=monkeypatch)
    _heat_then_stop_then_start(normal, pre_c_per_h=pre_c_per_h, off_min=off_min)

    assert paused.dt_est.last_outcome == normal.last_outcome and normal.last_outcome["heat"] == "detected"
    assert paused.dt_est._history_heat == normal._history_heat
    assert paused.dt_est._history_cool == normal._history_cool
    assert paused.dt_est._trend_slope == pytest.approx(normal._trend_slope)
    assert paused.dt_est.last_stop_time == normal.last_stop_time == 10_000.0



def _a_start_that_waits(room, *, call_s: float, pre_c_per_h: float = 0.0, monkeypatch=None) -> None:
    """A room heating, stopped at 0 s, free-running with a trend of ``pre_c_per_h``, calling again
    at ``call_s`` while the boiler is off; the boiler starts 120 s later, and the room's response
    comes 300 s after it at 3 degC/h. Fed every 20 s, read to 0.01 degC, through the regulation's
    filter, until the detection ends (or at once when none arms). ``room`` is SmartPI, fed as it
    feeds its estimator (the call from ``call_s``, the pause a calculation period later, the
    resume and the boiler's state, off from the room's stop, on at the boiler's start), or the
    author's estimator alone, whose heat starts with the boiler."""
    smartpi = room if isinstance(room, SmartPI) else None
    est = smartpi.dt_est if smartpi else room
    filt = smartpi.tint_filter if smartpi else AdaptiveTintFilter("test")
    boiler = call_s + 120.0
    now = [-3600.0]
    if smartpi:
        monkeypatch.setattr(algo_module.time, "monotonic", lambda: 10_000.0 + now[0])
        smartpi.set_boiler_state(True)
    est.last_power = 1.0  # heating already
    while now[0] <= boiler + 7200.0:
        t = now[0]
        temp = 20.0 + t / 3600.0 if t < 0.0 else 20.0 + pre_c_per_h * t / 3600.0 + 3.0 * max(0.0, t - boiler - 300.0) / 3600.0
        raw = round(temp, 2)
        lp, _ = filt.update(raw, 10_000.0 + t)
        if smartpi:
            smartpi._committed_on_percent = 1.0 if t < 0.0 or t >= call_s else 0.0
            if t == 0.0:
                smartpi.set_boiler_state(False, current_temp=raw)  # the boiler goes off with the room's stop
            if t == call_s + RECALC_S:
                smartpi.set_boiler_learning_paused(True)
            if t == boiler:
                smartpi.set_boiler_learning_paused(False)
                smartpi.set_boiler_state(True, current_temp=raw, since=10_000.0 + t)
            power = smartpi._dead_time_power()
        else:
            power = 1.0 if t < 0.0 or t >= boiler else 0.0
        est.update(10_000.0 + t, lp, power, hvac_mode=VThermHvacMode_HEAT, tin_raw=raw)
        if t >= boiler and est.state != "WAITING_HEAT_RESPONSE":
            return
        now[0] += 20.0


@pytest.mark.parametrize(("call_s", "armed"), [(300.0, False), (500.0, True)])
def test_the_wait_for_the_boiler_counts_as_time_without_heat(call_s, armed, monkeypatch) -> None:
    """The author's rule, 600 s without heat before a detection, counted from the room's last
    stop: stopped 300 s before its call and waiting 120 s for the boiler, 420 s, no detection;
    stopped 500 s before, 620 s, a detection, counted from the boiler's start."""
    smartpi = _smartpi()

    _a_start_that_waits(smartpi, call_s=call_s, monkeypatch=monkeypatch)

    est = smartpi.dt_est
    if armed:
        assert est.last_outcome["heat"] == "detected"
        assert len(est._history_heat) == 1 and est._history_heat[0] == pytest.approx(300.0, abs=60.0)
    else:
        assert est.state == "HEATING" and not est._history_heat


@pytest.mark.parametrize("pre_c_per_h", [0.2, 0.0, -0.3])
def test_a_start_that_waited_is_judged_as_the_author_judges_a_start_at_the_boiler_s_start(pre_c_per_h, monkeypatch) -> None:
    """After more than 900 s without heat, a room warming, flat or cooling calls and waits for the
    boiler: SmartPI's estimator ends as the author's estimator fed the same temperatures with the
    heat starting at the boiler's start, the trend before the start included (0.6.3)."""
    smartpi = _smartpi()
    _a_start_that_waits(smartpi, call_s=1800.0, pre_c_per_h=pre_c_per_h, monkeypatch=monkeypatch)
    author = DeadTimeEstimator()
    _a_start_that_waits(author, call_s=1800.0, pre_c_per_h=pre_c_per_h)

    assert smartpi.dt_est.last_outcome["heat"] == author.last_outcome["heat"]
    assert smartpi.dt_est._history_heat == author._history_heat
    assert smartpi.dt_est._trend_slope == pytest.approx(author._trend_slope)
    if pre_c_per_h > 0.0:
        assert author._trend_slope > 0.0 or author.last_outcome["heat"] == "rejected"


# --- the handler -------------------------------------------------------------------


def _handler(counted: bool = True) -> SmartPIHandler:
    thermostat = FakeThermostatRuntime()
    thermostat.cycle_scheduler.is_valve_mode = True  # a valve
    thermostat.is_used_by_central_boiler = counted
    handler = SmartPIHandler(thermostat)
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        handler.init_algorithm()
    return handler


async def _control(handler: SmartPIHandler, boiler_on: bool | None, power: float, at: float | None = None) -> SmartPI:
    """One calculation at ``at`` (the handler's monotonic time; by default one calculation period
    after the previous one); returns the algorithm, with the diagnostics' publications in
    ``handler.published``."""
    algo = handler._thermostat.prop_algorithm
    algo._committed_on_percent = power
    handler.clock = at if at is not None else getattr(handler, "clock", 1000.0 - RECALC_S) + RECALC_S
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=handler.clock),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=boiler_on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send") as send,
        patch("custom_components.vtherm_smartpi.handler.async_call_later"),  # the handler's checks: not run here
        patch.object(handler, "_async_save", new_callable=AsyncMock),
    ):
        await handler.control_heating(force=False)
    handler.published = send.call_count
    handler.should_publish_intermediate()  # as Versatile Thermostat asks after each calculation
    return algo


async def test_a_calculation_period_with_the_boiler_off_pauses() -> None:
    """The pause starts at the first calculation after one period (60 s, less 1 s for the timer)
    of calling with the boiler off, not before; it resumes once the boiler's start counts, after
    5 s (here at the next calculation): a shorter run gave the room no heat."""
    handler = _handler()

    await _control(handler, boiler_on=False, power=0.5, at=1000.0)
    algo = await _control(handler, boiler_on=False, power=0.5, at=1000.0 + BOILER_PAUSE_CONFIRM_S - 0.1)
    assert algo.boiler_learning_paused is False
    algo = await _control(handler, boiler_on=False, power=0.5, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is True
    assert BOILER_PAUSE_CONFIRM_S == RECALC_S - 1.0

    algo = await _control(handler, boiler_on=True, power=0.5, at=1100.0)
    assert algo.boiler_learning_paused is True
    algo = await _control(handler, boiler_on=True, power=0.5, at=1100.0 + BOILER_BLINK_S)
    assert algo.boiler_learning_paused is False


async def test_an_own_start_that_turns_the_boiler_on_within_seconds_does_not_pause() -> None:
    """A room's own start: the setpoint's calculation, a new room temperature and the periodic
    calculation while its valve has not reported (15 s), then Versatile Thermostat counts it and
    turns the boiler on."""
    handler = _handler()
    algo = handler._thermostat.prop_algorithm
    algo.set_boiler_learning_paused = MagicMock(wraps=algo.set_boiler_learning_paused)

    for at, on in ((1000.0, False), (1000.002, False), (1010.0, False), (1015.2, True), (1060.0, True)):
        algo = await _control(handler, boiler_on=on, power=1.0, at=at)
        assert algo.boiler_learning_paused is False
    algo.set_boiler_learning_paused.assert_not_called()  # the detection armed at the start runs on


async def test_the_pause_holds_while_the_boiler_waits_for_its_activation_delay() -> None:
    """The threshold is reached but Versatile Thermostat starts the boiler 120 s later: the room
    gets no heat meanwhile, so learning pauses after a period and resumes once the boiler's start
    counts, 5 s after it."""
    handler = _handler()

    times = (1000.0, 1040.0, 1100.0, 1120.0, 1120.0 + BOILER_BLINK_S)
    paused = [(await _control(handler, boiler_on=t >= 1120.0, power=1.0, at=t)).boiler_learning_paused for t in times]

    assert paused == [False, False, True, True, False]


@pytest.mark.parametrize(("boiler_on", "power"), [(False, 0.0), (None, 0.5)])
async def test_a_break_restarts_the_time(boiler_on, power) -> None:
    """No call, or a boiler state that cannot be read breaks the run: the time counts again from
    the next calculation with the boiler off."""
    handler = _handler()

    await _control(handler, boiler_on=False, power=0.5, at=1000.0)
    await _control(handler, boiler_on=boiler_on, power=power, at=1030.0)
    await _control(handler, boiler_on=False, power=0.5, at=1060.0)
    algo = await _control(handler, boiler_on=False, power=0.5, at=1060.0 + BOILER_PAUSE_CONFIRM_S - 0.1)
    assert algo.boiler_learning_paused is False
    algo = await _control(handler, boiler_on=False, power=0.5, at=1060.0 + BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is True


async def test_a_run_of_the_boiler_shorter_than_its_count_does_not_restart_the_time() -> None:
    """The boiler on at one calculation only, its start not counted (5 s): it gave the room no
    heat, so the time without heat goes on from its first calculation and the pause starts one
    period after it."""
    handler = _handler()

    await _control(handler, boiler_on=False, power=0.5, at=1000.0)
    algo = await _control(handler, boiler_on=True, power=0.5, at=1030.0)
    assert algo.boiler_learning_paused is False
    algo = await _control(handler, boiler_on=False, power=0.5, at=1060.0)
    assert algo.boiler_learning_paused is True


@pytest.mark.parametrize("run_s", [0.03, 2.0, BOILER_BLINK_S - 0.1])
async def test_a_short_run_of_the_boiler_during_the_pause_does_not_resume(run_s) -> None:
    """The room paused, the boiler runs less than 5 s and goes off again: it gave the room no heat.
    Learning stays paused, without a change of the pause: an alarm clears only once its signal has
    stayed back for a set time (Adnan 2013, 3.3, p. 30). Before, learning resumed at the boiler's
    start and paused again at the next calculation, up to 119 s later."""
    handler = _handler()
    handler._boiler_listener_remove = MagicMock()
    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    algo = await _control(handler, boiler_on=False, power=1.0, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is True
    algo.calculate = MagicMock()
    algo.set_boiler_learning_paused = MagicMock(wraps=algo.set_boiler_learning_paused)

    with patch("custom_components.vtherm_smartpi.handler.async_call_later"):
        _boiler_changes(handler, on=True, at=1100.0)
        _boiler_changes(handler, on=False, at=1100.0 + run_s)
    algo = await _control(handler, boiler_on=False, power=1.0, at=1160.0)

    assert algo.boiler_learning_paused is True
    algo.set_boiler_learning_paused.assert_not_called()
    assert algo.boiler_on is False and algo.dt_est.state != "WAITING_HEAT_RESPONSE"


async def test_the_pause_starts_one_period_after_the_boiler_s_stop_without_a_calculation() -> None:
    """The room calls with the boiler off, its changes followed: a check is scheduled one period
    (59 s) later and starts the pause then, not at the next calculation, up to a period later.
    Adaptation should be interrupted as quickly as possible (Hagglund and
    Astrom 2000, 5, p. 1175). A check that fires a little early still starts it."""
    handler = _handler()
    handler._boiler_listener_remove = MagicMock()
    algo = handler._thermostat.prop_algorithm
    algo._committed_on_percent = 1.0

    with patch("custom_components.vtherm_smartpi.handler.async_call_later") as later:
        _boiler_changes(handler, on=False, at=1000.0)
    assert later.call_args.args[1] == pytest.approx(BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is False

    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=1000.0 + BOILER_PAUSE_CONFIRM_S - 1e-6),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=False),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send") as send,
    ):
        later.call_args.args[2](None)

    assert algo.boiler_learning_paused is True
    assert send.called


async def test_no_pause_for_a_thermostat_the_boiler_does_not_heat() -> None:
    handler = _handler(counted=False)
    for at in (1000.0, 1100.0, 1200.0):
        algo = await _control(handler, boiler_on=False, power=0.5, at=at)
    assert algo.boiler_learning_paused is False


async def test_a_change_of_the_pause_is_published_at_once() -> None:
    """The diagnostics are published when the pause changes, though no input changed (SmartPI past
    its first phase, where not every calculation publishes)."""
    handler = _handler()
    force_smartpi_stable_mode(handler._thermostat.prop_algorithm)
    await _control(handler, boiler_on=False, power=0.5)
    await _control(handler, boiler_on=False, power=0.5)
    assert handler._thermostat.prop_algorithm.boiler_learning_paused is True
    assert handler.published >= 1

    await _control(handler, boiler_on=False, power=0.5)
    assert handler.published == 0

    await _control(handler, boiler_on=True, power=0.5)
    assert handler.published == 0  # the boiler's start does not count yet
    await _control(handler, boiler_on=True, power=0.5, at=handler.clock + BOILER_BLINK_S)
    assert handler.published >= 1


async def test_the_boiler_s_start_resumes_once_it_counts_and_arms_the_detection() -> None:
    """Versatile Thermostat's boiler starts while the room waits for it, paused: the change of
    the boiler's state changes nothing yet; the check scheduled 5 s later, the boiler still on,
    resumes learning, arms the dead-time detection from the boiler's start and publishes both,
    without a calculation."""
    handler = _handler()
    handler._boiler_listener_remove = MagicMock()  # the boiler's changes are followed
    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    algo = await _control(handler, boiler_on=False, power=1.0, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is True
    algo.calculate = MagicMock()

    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=1120.0),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1120.0),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=True),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send") as send,
        patch("custom_components.vtherm_smartpi.handler.async_call_later") as later,
    ):
        handler._on_boiler_change(MagicMock())

    assert algo.boiler_learning_paused is True
    assert not send.called
    algo.calculate.assert_not_called()
    assert algo.dt_est.state != "WAITING_HEAT_RESPONSE"
    assert later.call_args.args[1] == BOILER_BLINK_S

    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=1120.0 + BOILER_BLINK_S),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1120.0 + BOILER_BLINK_S),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=True),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send") as send,
    ):
        later.call_args.args[2](None)

    assert algo.boiler_learning_paused is False
    assert send.called
    algo.calculate.assert_not_called()
    assert (algo.dt_est.state, algo.dt_est.heat_start_time) == ("WAITING_HEAT_RESPONSE", 1120.0)


async def test_a_boiler_that_goes_off_before_its_check_starts_nothing() -> None:
    """The boiler runs 2 s and goes off again: the check scheduled for its start is cancelled, and
    a check that still fires changes nothing; the estimator sees no start. On removal a pending
    check is cancelled too."""
    handler = _handler()
    handler._boiler_listener_remove = MagicMock()
    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    algo = handler._thermostat.prop_algorithm
    algo.calculate = MagicMock()
    with patch("custom_components.vtherm_smartpi.handler.async_call_later") as later:
        _boiler_changes(handler, on=True, at=1120.0)
        check = later.call_args.args[2]
        cancel = later.return_value
        _boiler_changes(handler, on=False, at=1122.0)
        cancel.assert_called_once()
        _boiler_changes(handler, on=True, at=1130.0)
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=1125.0),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=True),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    ):
        check(None)  # the first check, for the start at 1120 s

    assert algo.boiler_on is False and algo.dt_est.state != "WAITING_HEAT_RESPONSE"
    handler._store = None  # no state to save here
    with patch("custom_components.vtherm_smartpi.handler.clear_valve_config_issues"):
        handler.remove()
    assert cancel.call_count == 2  # the check for the start at 1130 s


async def test_a_room_that_stops_before_the_boiler_starts_resumes_without_a_detection() -> None:
    """The pause ends because SmartPI stops calling (its calculation not run here, which would
    start the test thermostat calling again)."""
    handler = _handler()
    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    algo = await _control(handler, boiler_on=False, power=1.0, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    algo.calculate = MagicMock()

    algo = await _control(handler, boiler_on=False, power=0.0, at=1080.0)

    assert algo.boiler_learning_paused is False
    assert algo.dt_est.state != "WAITING_HEAT_RESPONSE"


def _boiler_changes(handler: SmartPIHandler, on: bool, at: float) -> SmartPI:
    """Versatile Thermostat's boiler goes on or off at ``at``, seen by the handler's listener."""
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    ):
        handler._on_boiler_change(MagicMock())
    return handler._thermostat.prop_algorithm


def _heating_room(handler: SmartPIHandler) -> SmartPI:
    """SmartPI calling at full power, with a filtered room temperature, its calculations not run
    (they would make the test thermostat call or stop by themselves)."""
    algo = handler._thermostat.prop_algorithm
    algo.calculate = MagicMock()
    algo.tint_filter.update(19.0, 0.0)
    algo.dt_est.last_power = 1.0
    return algo


@pytest.mark.parametrize(("back_after_s", "measured"), [(300.0, False), (700.0, True)])
async def test_the_boiler_going_off_while_it_heats_the_room_is_the_room_s_stop(back_after_s, measured) -> None:
    """The boiler heats the calling room, goes off at 1030 s and comes back: held as heat for
    120 s (a valve), then the room's heat stopped with it, so the estimator records the stop at
    1030 s and arms nothing, and learning pauses at once (120 s since the stop); the boiler's
    return is judged by the estimator's own rules, 600 s without heat: back after 300 s, no
    measurement; after 700 s, a measurement from the boiler's return, as the author's estimator
    gives for a stop at 1030 s and a start at the return."""
    handler = _handler()
    algo = _heating_room(handler)
    await _control(handler, boiler_on=True, power=1.0, at=1000.0)

    _boiler_changes(handler, on=False, at=1030.0)
    assert algo.boiler_on is True and algo.dt_est.last_power == 1.0  # held as heat
    await _control(handler, boiler_on=False, power=1.0, at=1090.0)
    assert algo.boiler_on is True and algo.boiler_learning_paused is False  # still held at 60 s
    await _control(handler, boiler_on=False, power=1.0, at=1030.0 + BOILER_VALVE_HOLD_S)
    assert algo.dt_est.last_stop_time == 1030.0
    assert algo.dt_est.state != "WAITING_COOL_RESPONSE"
    assert algo.boiler_learning_paused is True

    back = 1030.0 + back_after_s
    _boiler_changes(handler, on=True, at=back)
    assert algo.boiler_learning_paused is True  # its start does not count yet
    _boiler_changes(handler, on=True, at=back + BOILER_BLINK_S)  # the boiler still on 5 s later
    assert algo.boiler_learning_paused is False

    assert algo.dt_est.last_stop_time == 1030.0
    if measured:
        assert (algo.dt_est.state, algo.dt_est.heat_start_time) == ("WAITING_HEAT_RESPONSE", back)
        author = DeadTimeEstimator()
        author.last_power, author.last_stop_time = 0.0, 1030.0
        author.update(back, algo.tint_filter.t_int_lp, 1.0, hvac_mode=VThermHvacMode_HEAT)
        assert (author.state, author.heat_start_time) == ("WAITING_HEAT_RESPONSE", back)
    else:
        assert algo.dt_est.state == "HEATING"


async def test_an_own_start_that_waits_for_the_boiler_records_no_stop() -> None:
    """A room starts calling while the boiler is off and waits for it: the boiler has not heated
    it, so no stop is recorded, and the boiler's start is measured as before."""
    handler = _handler()
    algo = _heating_room(handler)
    algo.dt_est.last_power = 0.0
    algo.dt_est.last_stop_time = 300.0  # its last heating stopped 700 s before its call

    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    await _control(handler, boiler_on=False, power=1.0, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    assert algo.boiler_learning_paused is True
    _boiler_changes(handler, on=True, at=1120.0)
    _boiler_changes(handler, on=True, at=1120.0 + BOILER_BLINK_S)

    assert algo.dt_est.last_stop_time == 300.0
    assert (algo.dt_est.state, algo.dt_est.heat_start_time) == ("WAITING_HEAT_RESPONSE", 1120.0)


@pytest.mark.parametrize("learning", [True, False])
def test_the_heating_starts_with_the_boiler(learning, monkeypatch) -> None:
    """The room called long before, waited for the boiler, and stops 5 minutes after the
    boiler's start: its heat lasted 5 minutes, shorter than its dead time, so no passive
    response is timed; the heating's start is the boiler's, also when the estimator is not fed
    at the boiler's start (the user's learning off)."""
    now = [0.0]
    smartpi = _paused_while_calling(monkeypatch, now, learning=learning)
    est = smartpi.dt_est
    est.deadtime_heat_s, est.deadtime_heat_reliable, est.last_start_time = 780.0, True, 0.0

    now[0] = 1000.0
    smartpi.set_boiler_learning_paused(False)
    now[0] = 1005.0
    smartpi.set_boiler_state(True, since=1000.0)
    assert est.last_start_time == 1000.0

    smartpi._committed_on_percent = 0.0
    est.update(1300.0, 19.0, 0.0)

    assert est.state != "WAITING_COOL_RESPONSE" and est.last_stop_time == 1300.0


async def test_the_boiler_going_off_drops_the_heating_detection_in_flight(caplog) -> None:
    """The heating detection armed at the boiler's start is dropped when the boiler goes off
    while the room calls and stays off for a valve's hold, 120 s ("aborted"), logged and
    published at once; the estimator sees the power fall to 0 from the boiler's stop (5 minutes of
    heat, no reliable dead time: no passive detection) and no power after it, so a later rise is
    not timed as a response to heat that has stopped."""
    handler = _handler()
    algo = _heating_room(handler)
    algo.dt_est.last_power = 0.0
    await _control(handler, boiler_on=False, power=1.0, at=1000.0)
    await _control(handler, boiler_on=False, power=1.0, at=1000.0 + BOILER_PAUSE_CONFIRM_S)
    _boiler_changes(handler, on=True, at=1120.0)
    _boiler_changes(handler, on=True, at=1120.0 + BOILER_BLINK_S)
    assert algo.dt_est.state == "WAITING_HEAT_RESPONSE"

    caplog.set_level(logging.INFO, logger="custom_components.vtherm_smartpi.algo")
    _boiler_changes(handler, on=False, at=1420.0)
    assert algo.dt_est.state == "WAITING_HEAT_RESPONSE"  # held as heat
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=1420.0 + BOILER_VALVE_HOLD_S),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1420.0 + BOILER_VALVE_HOLD_S),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=False),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send") as send,
    ):
        handler._on_boiler_change(MagicMock())

    assert (algo.dt_est.state, algo.dt_est.last_outcome["heat"], algo.dt_est.last_stop_time) == ("COOLING", "aborted", 1420.0)
    assert algo.dt_est.last_power == 0.0 and algo.boiler_on is False and algo._dead_time_power() == 0.0
    assert send.called
    assert "heat stopped with the central boiler (heating dead-time detection aborted)" in caplog.text

    for t, temp in ((1600.0, 19.0), (1660.0, 19.2), (1720.0, 19.4)):
        algo.dt_est.update(t, temp, algo._dead_time_power())
    assert algo.dt_est.state == "COOLING" and not algo.dt_est._history_heat


def test_the_handler_follows_the_boiler_once_its_entity_exists_and_stops_on_removal() -> None:
    handler = _handler()
    unsubscribe = MagicMock()
    with (
        patch("custom_components.vtherm_smartpi.handler.central_boiler_entity_id", side_effect=[None, "binary_sensor.central_boiler"]),
        patch("custom_components.vtherm_smartpi.handler.async_track_state_change_event", return_value=unsubscribe) as track,
    ):
        handler._listen_to_the_boiler()
        assert track.call_count == 0
        handler._listen_to_the_boiler()
        handler._listen_to_the_boiler()
    assert track.call_count == 1
    assert track.call_args.args[1] == ["binary_sensor.central_boiler"]

    handler._store = None  # no state to save here
    with patch("custom_components.vtherm_smartpi.handler.clear_valve_config_issues"):
        handler.remove()
    unsubscribe.assert_called_once()
