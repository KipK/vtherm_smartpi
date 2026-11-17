"""The dead-time estimator sees the heat the central boiler really gives (documentation/en/
technical_doc.md, 3.6 and 3.8): SmartPI's command while the boiler is on, no power while it is off.
The boiler going off stops the room's heat at that moment, so the author's rules time the passive
response from the boiler's stop, and what the room does while the boiler stays off (its own stop, a
new call) changes nothing; the boiler's start is the start of the room's heat. The real handler
and SmartPI's real calculation, times on the handler's monotonic clock. The room heated from 0 s (dead time 780 s, reliable); the boiler goes off
at 1030 s; the room's passive response starts 780 s after its heat really ended, then it cools
0.3 degC/h."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import logging

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.boiler_call import BOILER_BLINK_S, BOILER_VALVE_HOLD_S
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.const import SmartPICalibrationPhase
from fakes.fake_thermostat_runtime import FakeThermostatRuntime
from helpers import force_smartpi_stable_mode

STOP = 1030.0  # the boiler goes off (or, in the reference, the room stops) here
DEAD = 780.0
COOL_C_PER_S = 0.3 / 3600.0
T0 = 19.0
REFERENCE_S = 1020.0  # the passive dead time of the reference room, stopping by itself


def _handler(heat_start: float = 0.0) -> SmartPIHandler:
    thermostat = FakeThermostatRuntime()
    thermostat.cycle_scheduler.is_valve_mode = True  # a valve
    thermostat.is_used_by_central_boiler = True
    handler = SmartPIHandler(thermostat)
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        handler.init_algorithm()
    algo = thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    est = algo.dt_est
    est.deadtime_heat_s, est.deadtime_heat_reliable = DEAD, True
    est.deadtime_cool_s, est.deadtime_cool_reliable = DEAD, True
    est.last_start_time, est.last_power, est.state = heat_start, 1.0, "HEATING"
    return handler


def _temp(at: float) -> float:
    """Flat until the passive response arrives, DEAD after the heat really ended, then cooling."""
    return T0 - max(0.0, at - STOP - DEAD) * COOL_C_PER_S


async def _step(handler: SmartPIHandler, at: float, boiler_on: bool | None, committed: float, temp: float | None = None) -> SmartPI:
    """One control at ``at``: SmartPI's committed power, the pause check, the real calculation;
    the room's temperature ``temp``, by default the model's."""
    t = handler._thermostat
    algo = t.prop_algorithm
    algo._committed_on_percent = committed
    t.current_temperature = _temp(at) if temp is None else temp
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=boiler_on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
        patch.object(handler, "_async_save", new_callable=AsyncMock),
    ):
        await handler.control_heating(force=False)
    return algo


def _boiler(handler: SmartPIHandler, on: bool | None, at: float) -> None:
    """Versatile Thermostat's boiler changes at ``at``, seen by the handler's listener (or, the
    state unchanged, the boiler still in that state then)."""
    handler._thermostat.current_temperature = _temp(at)
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    ):
        handler._on_boiler_change(MagicMock())


def _boiler_cut(handler: SmartPIHandler, at: float) -> None:
    """The boiler goes off at ``at`` and stays off: the handler holds it as heat for a valve, and
    its check 120 s later confirms the cut, dated from ``at``."""
    _boiler(handler, False, at)
    _boiler(handler, False, at + BOILER_VALVE_HOLD_S)


async def _heated(heat_start: float = 0.0) -> SmartPIHandler:
    """The room heated with the boiler on, a calculation a minute, up to 1000 s."""
    handler = _handler(heat_start)
    for at in range(400, 1001, 60):
        await _step(handler, float(at), True, 1.0)
    return handler


async def _cools_down(handler: SmartPIHandler, after: float, boiler_on: bool) -> None:
    """The room not calling, a calculation a minute, until the passive detection ends."""
    est = handler._thermostat.prop_algorithm.dt_est
    at = after
    while at < after + 2400.0 and est.state == "WAITING_COOL_RESPONSE":
        at += 60.0
        await _step(handler, at, boiler_on, 0.0)


async def _reference(times: list[float]) -> list[float]:
    """The room stops itself at STOP with the boiler on, then SmartPI calculates at ``times`` and
    each minute until the passive detection ends: the author's measurement, on the same samples."""
    handler = await _heated()
    algo = await _step(handler, STOP, True, 0.0)
    est = algo.dt_est
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", STOP, STOP)
    for at in times:
        await _step(handler, at, True, 0.0)
    await _cools_down(handler, times[-1] if times else STOP, True)
    assert est.last_outcome["cool"] == "detected"
    return list(est._history_cool)


async def test_the_reference_stop_with_the_boiler_on_is_the_author_s() -> None:
    """Without the boiler's cut the room's own stop arms the passive response from the stop
    (1020 s)."""
    assert await _reference([]) == [REFERENCE_S]


@pytest.mark.parametrize(
    "calculations",
    [
        [(1060.0, 0.0)],  # the room's stop seen 30 s after the boiler's
        [(1088.0, 0.0)],  # 58 s after: within the hold
        [(1060.0, 1.0), (1120.0, 0.0)],  # a calculation while it calls, its stop committed between
        [(1090.0, 1.0), (1150.0, 1.0), (1210.0, 0.0)],  # paused first, then it stops
    ],
    ids=["30 s", "58 s", "90 s", "paused"],
)
async def test_the_boiler_s_cut_times_the_passive_response_from_its_stop(calculations) -> None:
    """The boiler goes off at 1030 s after a settled heating. A valve holds it as heat for 120 s;
    then, or at the room's own stop during the hold, the stop and the passive response are dated
    from 1030 s, a dead time counting from the onset of the step (Astrom and Hagglund 1995, 2.4,
    p. 16): whenever the room's own stop comes and whether learning paused meanwhile, the dead
    time is the reference's on the same samples (a room's own stop before its pause)."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est

    _boiler(handler, False, STOP)
    assert (est.state, est.last_power) == ("HEATING", 1.0)  # held as heat
    for at, committed in calculations:
        await _step(handler, at, False, committed)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", STOP, STOP)

    await _cools_down(handler, calculations[-1][0], False)

    assert est.last_outcome["cool"] == "detected"
    assert list(est._history_cool) == await _reference([at for at, _ in calculations])


async def test_the_boiler_s_cut_after_a_short_heating_records_the_stop_only() -> None:
    """The room heated 430 s, less than its dead time: the boiler's cut records the stop and arms
    nothing, and the room's own stop afterwards arms nothing either."""
    handler = await _heated(heat_start=600.0)
    est = handler._thermostat.prop_algorithm.dt_est

    _boiler(handler, False, STOP)
    await _step(handler, STOP + 60.0, False, 0.0)

    assert est.state != "WAITING_COOL_RESPONSE" and est.last_stop_time == STOP and est.last_outcome["cool"] is None


@pytest.mark.parametrize(("back_after_s", "measured"), [(300.0, False), (700.0, True)])
async def test_the_boiler_s_return_before_the_response_ends_the_passive_detection(back_after_s, measured) -> None:
    """The boiler heats the calling room again before its passive response came: once the boiler
    has run 5 s, learning resumes and the estimator sees the start from the boiler's return. The passive detection ends "aborted", whether the start is refused (300 s after the
    boiler's stop, less than 600 s) or measured from the boiler's return (700 s)."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    _boiler(handler, False, STOP)
    await _step(handler, STOP + BOILER_VALVE_HOLD_S, False, 1.0)
    assert algo.boiler_learning_paused and est.state == "WAITING_COOL_RESPONSE"

    back = STOP + back_after_s
    _boiler(handler, True, back)
    assert algo.boiler_learning_paused and est.state == "WAITING_COOL_RESPONSE"
    _boiler(handler, True, back + BOILER_BLINK_S)
    assert not algo.boiler_learning_paused

    assert est.last_outcome["cool"] == "aborted" and est.last_stop_time == STOP
    assert algo.boiler_on is True and algo._dead_time_power() == 1.0
    assert est.last_start_time == back
    if measured:
        assert (est.state, est.heat_start_time) == ("WAITING_HEAT_RESPONSE", back)
    else:
        assert est.state == "HEATING"


async def test_the_boiler_s_state_holds_through_the_thermostat_off_a_reset_and_a_restart() -> None:
    """The estimator follows the boiler, not SmartPI's own events: with the boiler off it sees no
    power after the thermostat off and a reset of learning. After a restart SmartPI has no
    boiler state until the handler reads it: it sees the command, and the first reading (the
    boiler off) is no stop."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    _boiler_cut(handler, STOP)
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=STOP + BOILER_VALVE_HOLD_S + 1.0):
        algo.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_OFF)
    algo._committed_on_percent = 1.0
    assert algo._dead_time_power() == 0.0
    algo.reset_learning()
    assert algo._dead_time_power() == 0.0

    restarted = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="Restart")
    restarted.load_state(algo.save_state())
    restarted._committed_on_percent = 1.0
    assert restarted.boiler_on is None and restarted._dead_time_power() == 1.0
    stop = restarted.dt_est.last_stop_time
    assert restarted.set_boiler_state(False) is False
    assert (restarted.dt_est.last_power, restarted.dt_est.last_stop_time) == (0.0, stop)


@pytest.mark.parametrize(("dip", "rise_at"), [(0.0, 1210.0), (0.005, 1210.0), (0.005, 1700.0), (0.0, 1700.0)])
async def test_a_dip_and_a_new_call_while_the_boiler_is_off_arm_nothing(dip, rise_at) -> None:
    """A dip and a new call: the room calls as the boiler
    cuts its heat at 1030 s and pauses at 1150 s, after a valve's hold; its command dips to 0 or to
    0.5 % (still above 0,
    so the pause holds) and rises to 100 % again, the boiler still off. The estimator saw no power
    from the boiler's stop: nothing arms, no stop is recorded after 1030 s, and the passive
    detection timed from the boiler's stop goes on. The boiler's start 200 s after the rise is the
    start of the room's heat, judged by the estimator's rules from it: measured 870 s after the
    boiler's stop, not 380 s after it."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    _boiler(handler, False, STOP)
    await _step(handler, 1090.0, False, 1.0)
    at = 1150.0
    while at < rise_at:
        await _step(handler, at, False, dip)
        assert algo._dead_time_power() == 0.0
        at += 60.0
    for at in (rise_at, rise_at + 60.0, rise_at + 120.0):
        await _step(handler, at, False, 1.0)
        assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", STOP, STOP)
        assert est.last_outcome["heat"] is None

    on = rise_at + 200.0
    _boiler(handler, True, on)
    _boiler(handler, True, on + BOILER_BLINK_S)

    assert (est.last_stop_time, est.last_start_time, est.last_outcome["cool"]) == (STOP, on, "aborted")
    if on - STOP >= est.min_off_time_seconds:
        assert (est.state, est.heat_start_time) == ("WAITING_HEAT_RESPONSE", on)
    else:
        assert est.state == "HEATING"


async def test_a_rise_of_the_room_while_the_boiler_is_off_gives_no_heating_dead_time(caplog) -> None:
    """A false heat: as above (0.5 % from 1150 s, 100 % at
    1700 s, the boiler still off), then the room warms 0.1 degC without the boiler (sun, a
    neighbour) from 1760 s. No heating detection arms and none is detected: the room's heating
    dead time stays as it was."""
    caplog.set_level(logging.INFO)
    handler = await _heated()
    est = handler._thermostat.prop_algorithm.dt_est
    before = (est.deadtime_heat_s, list(est._history_heat))
    _boiler(handler, False, STOP)
    await _step(handler, 1090.0, False, 1.0, T0)
    for at in range(1150, 1700, 60):
        await _step(handler, float(at), False, 0.005, T0)
    for at in range(1700, 2300, 60):
        await _step(handler, float(at), False, 1.0, T0 if at < 1760 else T0 + 0.1)
        assert est.state != "WAITING_HEAT_RESPONSE"

    assert est.last_outcome["heat"] is None
    assert (est.deadtime_heat_s, list(est._history_heat)) == before
    assert "Heat Deadtime detected" not in caplog.text


async def test_a_new_call_with_the_boiler_still_off_lets_the_passive_detection_end() -> None:
    """Decision A: after the boiler's cut the room stops itself (1060 s) and calls again at 1700 s,
    the boiler still off. Nothing arms at the call; the passive detection timed from the boiler's
    stop goes on without a break and ends detected, with the reference's dead time on the same
    samples. The start of the room's heat counts from the boiler's start."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    _boiler(handler, False, STOP)
    await _step(handler, STOP + 30.0, False, 0.0)
    at = 1700.0
    await _step(handler, at, False, 1.0)
    times = [STOP + 30.0, at]
    while est.state == "WAITING_COOL_RESPONSE" and at < STOP + 3600.0:
        assert (est.cool_start_time, est.last_stop_time, est.last_outcome["heat"]) == (STOP, STOP, None)
        at += 60.0
        await _step(handler, at, False, 1.0)
        times.append(at)

    assert est.last_outcome["cool"] == "detected" and algo.boiler_learning_paused
    assert list(est._history_cool) == await _reference(times)

    on = at + 300.0
    _boiler(handler, True, on)
    _boiler(handler, True, on + BOILER_BLINK_S)
    assert (est.last_start_time, est.last_stop_time) == (on, STOP)


def _without_heat(handler: SmartPIHandler) -> None:
    """The room has been without heat since 0 s, its learned dead times kept."""
    est = handler._thermostat.prop_algorithm.dt_est
    est.last_power, est.state, est.last_start_time, est.last_stop_time = 0.0, "OFF", None, 0.0


async def test_an_own_start_that_turns_the_boiler_on_counts_from_the_boiler_s_start() -> None:
    """Decision B: a room without heat for long calls at 1000 s with the boiler off; its valve
    reports 15 s later and Versatile Thermostat turns the boiler on. The heating's start is the
    boiler's (1015 s), not the call's, seen once the boiler has run 5 s; the room does not
    pause."""
    handler = _handler()
    _without_heat(handler)
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    for at in range(100, 1000, 60):
        await _step(handler, float(at), False, 0.0)
    await _step(handler, 1000.0, False, 1.0)
    assert est.state != "WAITING_HEAT_RESPONSE" and est.last_start_time is None

    _boiler(handler, True, 1015.0)
    assert est.state != "WAITING_HEAT_RESPONSE"
    _boiler(handler, True, 1015.0 + BOILER_BLINK_S)

    assert (est.state, est.heat_start_time, est.last_start_time) == ("WAITING_HEAT_RESPONSE", 1015.0, 1015.0)
    assert not algo.boiler_learning_paused


async def test_a_start_of_the_boiler_shorter_than_a_blink_is_not_seen() -> None:
    """The room waits for the boiler, which runs 3 s and goes off again: the estimator sees no
    start."""
    handler = _handler()
    _without_heat(handler)
    est = handler._thermostat.prop_algorithm.dt_est
    for at in range(100, 1001, 60):
        await _step(handler, float(at), False, 1.0 if at >= 1000 else 0.0)
    _boiler(handler, True, 1015.0)
    _boiler(handler, False, 1018.0)
    await _step(handler, 1060.0, False, 1.0)

    assert est.state != "WAITING_HEAT_RESPONSE" and est.last_start_time is None


async def test_a_blink_of_the_boiler_off_on_and_off_is_one_stop(caplog) -> None:
    """At a final stop Versatile Thermostat's boiler can go off, on and off again within 30 ms: a
    valve holds it as heat; the room's own stop 60 s later is dated from the
    boiler's last stop, 30 ms after the first, so the passive detection ends detected, as the
    reference on the same samples (to the 30 ms)."""
    caplog.set_level(logging.INFO)
    handler = await _heated()
    est = handler._thermostat.prop_algorithm.dt_est
    _boiler(handler, False, STOP)
    _boiler(handler, True, STOP + 0.015)
    _boiler(handler, False, STOP + 0.030)
    assert (est.state, est.last_power) == ("HEATING", 1.0)  # held as heat

    await _step(handler, STOP + 60.0, False, 0.0)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", STOP + 0.030, STOP + 0.030)
    await _cools_down(handler, STOP + 60.0, False)

    assert est.last_outcome["cool"] == "detected"
    assert list(est._history_cool) == pytest.approx(await _reference([STOP + 60.0]), abs=0.05)
    assert caplog.text.count("heat stopped with the central boiler") == 1 and "blink" not in caplog.text


@pytest.mark.parametrize("back_after", [0.03, BOILER_VALVE_HOLD_S - 1.0], ids=["a blink", "119 s"])
async def test_a_boiler_back_within_the_hold_keeps_the_heating_going(back_after, caplog) -> None:
    """The boiler heating the room goes off and back on within 30 ms, or 119 s with a calculation
    in between: a valve holds it as heat, so the estimator sees no stop and no start; the heating
    goes on (its period still counts from 0 s), nothing is logged, and the room's own stop at
    1500 s times its passive response from that stop. Before the hold was 120 s, the calculation
    60 s after the stop ended the heating."""
    caplog.set_level(logging.INFO)
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    _boiler(handler, False, STOP)
    if back_after > 60.0:
        await _step(handler, STOP + 60.0, False, 1.0)  # a calculation within the hold
    _boiler(handler, True, STOP + back_after)

    await _step(handler, STOP + BOILER_VALVE_HOLD_S, True, 1.0)

    assert (est.state, est.last_start_time, est.last_stop_time, est.last_power) == ("HEATING", 0.0, None, 1.0)
    assert "heat stopped" not in caplog.text and "blink" not in caplog.text
    await _step(handler, 1500.0, True, 0.0)
    assert (est.state, est.cool_start_time) == ("WAITING_COOL_RESPONSE", 1500.0)


async def test_a_stop_of_the_boiler_longer_than_the_hold_is_a_stop_and_its_return_a_start() -> None:
    """The boiler off for 122 s, 2 s longer than the hold: the cut is dated from its stop, and its
    return, once it has run 5 s, is a start judged by the estimator's rules (no blink: the stop
    came 122 s before): within 600 s of the stop it is not measured, the passive detection ends
    aborted, and the heating period counts from the return."""
    handler = await _heated()
    est = handler._thermostat.prop_algorithm.dt_est
    _boiler_cut(handler, STOP)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", STOP, STOP)
    back = STOP + BOILER_VALVE_HOLD_S + 2.0
    _boiler(handler, True, back)
    _boiler(handler, True, back + BOILER_BLINK_S)

    assert (est.last_outcome["cool"], est.state, est.last_start_time) == ("aborted", "HEATING", back)


@pytest.mark.parametrize("counted", [True, False])
async def test_an_unknown_boiler_or_one_that_does_not_count_the_room_is_the_author_s(counted, caplog) -> None:
    """A boiler whose state cannot be read, or a thermostat the boiler does not count: the
    estimator sees SmartPI's command, as in the author's code, and the room's own stop times its
    passive response from the stop, as the reference. A state that becomes unknown is no stop."""
    caplog.set_level(logging.INFO)
    handler = await _heated()
    handler._thermostat.is_used_by_central_boiler = counted
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    read = None if counted else False  # not counted: the boiler's state is not read at all

    await _step(handler, 1010.0, read, 1.0)
    assert (algo.boiler_on, algo._dead_time_power(), est.state) == (None, 1.0, "HEATING")
    await _step(handler, STOP, read, 0.0)
    assert (est.state, est.cool_start_time) == ("WAITING_COOL_RESPONSE", STOP)
    await _cools_down(handler, STOP, read)

    assert list(est._history_cool) == [REFERENCE_S]
    assert "heat stopped with the central boiler" not in caplog.text


def _fresh_handler() -> SmartPIHandler:
    thermostat = FakeThermostatRuntime()
    thermostat.cycle_scheduler.is_valve_mode = True  # a valve
    thermostat.is_used_by_central_boiler = True
    handler = SmartPIHandler(thermostat)
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        handler.init_algorithm()
    return handler


async def _control_at(handler: SmartPIHandler, at: float, boiler_on: bool, target: float) -> SmartPI:
    """One control in SmartPI's first phase, where the setpoint's command is committed within
    the calculation (after the handler's check of the pause)."""
    t = handler._thermostat
    t.target_temperature, t.current_temperature = target, 19.0
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.smartpi.deadtime_estimator.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=boiler_on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
        patch.object(handler, "_async_save", new_callable=AsyncMock),
    ):
        await handler.control_heating(force=False)
    return t.prop_algorithm


@pytest.mark.parametrize("cut", [False, True])
async def test_a_new_call_after_the_cut_s_stop_starts_at_once_in_the_first_phase(cut) -> None:
    """A fresh room, cut by the boiler at 1030 s or not,
    stops itself; the boiler runs again for other rooms; the room calls again at 2330 s with the
    boiler on. Its command is committed within that calculation, after the handler's check: the
    heating detection counts from 2330 s either way, not a calculation later."""
    handler = _fresh_handler()
    for at in range(0, 1001, 60):
        await _control_at(handler, float(at), True, 23.0)
    if cut:
        _boiler(handler, False, STOP)
    await _control_at(handler, 1040.0, not cut, 7.0)
    if cut:
        _boiler(handler, True, 1700.0)
    for at in range(1760, 2301, 60):
        await _control_at(handler, float(at), True, 7.0)

    algo = await _control_at(handler, 2330.0, True, 23.0)

    est = algo.dt_est
    assert algo.phase.value == "Hysteresis" and algo.committed_on_percent == 1.0
    assert algo.boiler_on is True
    assert (est.state, est.last_start_time, est.heat_start_time) == ("WAITING_HEAT_RESPONSE", 2330.0, 2330.0)


@pytest.mark.parametrize("cut", [False, True])
async def test_a_new_call_after_the_cut_s_stop_in_the_stable_phase_is_as_without_the_cut(cut) -> None:
    """In SmartPI's stable phase the command is committed before the handler's check: the new call
    with the boiler on starts at the same moment, cut or not."""
    handler = await _heated()
    if cut:
        _boiler(handler, False, STOP)
    await _step(handler, STOP + 10.0, not cut, 0.0)
    if cut:
        _boiler(handler, True, 1700.0)
    for at in range(1760, 2301, 60):
        await _step(handler, float(at), True, 0.0)

    algo = await _step(handler, 2330.0, True, 1.0)

    est = algo.dt_est
    assert algo.boiler_on is True
    assert (est.state, est.last_start_time, est.heat_start_time) == ("WAITING_HEAT_RESPONSE", 2330.0, 2330.0)


# --- a calibration with the boiler off, and a restart ---------------------------------------------


def _patched(at: float, boiler_on: bool | None):
    return (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.smartpi.calibration.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.smartpi.deadtime_estimator.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=boiler_on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    )


async def _control(handler: SmartPIHandler, at: float, boiler_on: bool | None, committed: float, temp: float, target: float = 20.0, hvac=VThermHvacMode_HEAT) -> SmartPI:
    """One control at ``at``, the clocks of the handler, SmartPI, its calibration and its estimator
    all at ``at``."""
    t = handler._thermostat
    algo = t.prop_algorithm
    algo._committed_on_percent = committed
    t.current_temperature, t.target_temperature, t.vtherm_hvac_mode = temp, target, hvac
    p = _patched(at, boiler_on)
    with p[0], p[1], p[2], p[3], p[4], p[5], patch.object(handler, "_async_save", new_callable=AsyncMock):
        await handler.control_heating(force=False)
    return algo


def _boiler_at(handler: SmartPIHandler, on: bool | None, at: float, temp: float) -> None:
    handler._thermostat.current_temperature = temp
    p = _patched(at, on)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        handler._on_boiler_change(MagicMock())


BOILER_ON = 780.1  # an activation delay: the boiler starts 780 s after the calibration's start


def _cooling(at: float) -> float:
    """The room cools all along: no heat reached it before the boiler's start, nor 780 s after."""
    return 19.0 - min(at, BOILER_ON + 780.0) / 3600.0


async def _calibrating_with_the_boiler_off() -> SmartPIHandler:
    """A learned room (dead times 780 s, reliable, six detections each), at rest; a forced calibration heats from 0 s with the boiler off, learning pauses at
    60 s; the boiler starts at 780.1 s."""
    handler = _fresh_handler()
    algo = handler._thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    est = algo.dt_est
    est.deadtime_heat_s = est.deadtime_cool_s = 780.0
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    est._history_heat.extend([780.0] * 6)
    est._history_cool.extend([780.0] * 6)
    est.state, est.last_power, est.last_start_time, est.last_stop_time = "OFF", 0.0, None, -1000.0
    _boiler_at(handler, False, 0.0, _cooling(0.0))
    algo.calibration_mgr.request_calibration()
    await _control(handler, 0.03, False, 0.0, _cooling(0.03))
    at = 60.03
    while at < BOILER_ON:
        await _control(handler, at, False, 1.0, _cooling(at))
        at += 60.0
    assert algo.calibration_mgr.is_calibrating and algo.boiler_learning_paused
    assert (est.last_power, est.last_start_time, est.state) == (0.0, None, "OFF")
    _boiler_at(handler, True, BOILER_ON, _cooling(BOILER_ON))
    return handler


@pytest.mark.parametrize("stop_after", [0.9, 30.0, 300.0])
async def test_a_calibration_with_the_boiler_off_times_no_passive_response(stop_after) -> None:
    """With an activation delay: a calibration heats with the boiler off, the boiler
    starts, and the room stops (its HVAC off, the calibration cancelled) 0.9 s, 30 s or 300 s
    later. The calibration fed no power while the boiler was off, and the boiler's start, once it
    has run 5 s, is the start of the heating: a stop within 780 s of it times no passive response."""
    handler = await _calibrating_with_the_boiler_off()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    on = stop_after > BOILER_BLINK_S
    if on:
        _boiler_at(handler, True, BOILER_ON + BOILER_BLINK_S, _cooling(BOILER_ON + BOILER_BLINK_S))
        assert est.last_start_time == BOILER_ON
        at = BOILER_ON + 20.0
        while at < BOILER_ON + stop_after:
            await _control(handler, at, True, 1.0, _cooling(at))
            at += 60.0
    stop = BOILER_ON + stop_after
    algo.calibration_mgr.reset()
    await _control(handler, stop, on, 0.0, _cooling(stop), hvac=VThermHvacMode_OFF)
    await _control(handler, stop + 0.02, on, 0.0, _cooling(stop), target=7.0)
    at = stop + 60.0
    while at < stop + 1800.0:
        await _control(handler, at, False, 0.0, _cooling(at), target=7.0)
        assert est.state != "WAITING_COOL_RESPONSE", at
        at += 60.0

    assert list(est._history_cool) == [780.0] * 6 and est.last_outcome["cool"] is None


async def test_a_calibration_with_the_boiler_off_counts_its_heating_from_the_boiler_s_start() -> None:
    """The calibration's heating counts from the boiler's start (780.1 s), not from its own start
    with the boiler off: its heating dead-time detection arms then; it reaches its upper
    threshold 300 s later and drives the room to its low level, which times no passive response,
    the heating having lasted less than the room's dead time."""
    handler = await _calibrating_with_the_boiler_off()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    _boiler_at(handler, True, BOILER_ON + BOILER_BLINK_S, 18.8)

    assert (est.state, est.heat_start_time, est.last_start_time) == ("WAITING_HEAT_RESPONSE", BOILER_ON, BOILER_ON)
    for at, temp in ((BOILER_ON + 60.0, 19.5), (BOILER_ON + 120.0, 20.0), (BOILER_ON + 300.0, 21.0), (BOILER_ON + 360.0, 21.0)):
        await _control(handler, at, True, 1.0, temp)
    assert algo.calibration_mgr._calibration_state == SmartPICalibrationPhase.COOL_DOWN_FINAL and est.last_power == 0.0
    assert est.state != "WAITING_COOL_RESPONSE" and list(est._history_cool) == [780.0] * 6


def _restarted() -> tuple[SmartPIHandler, SmartPI]:
    """A learned room's saved state loaded at 0 s by a new handler, as after a restart."""
    before = _fresh_handler()
    force_smartpi_stable_mode(before._thermostat.prop_algorithm)
    est = before._thermostat.prop_algorithm.dt_est
    est.deadtime_heat_s = est.deadtime_cool_s = 780.0
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    est._history_heat.extend([780.0] * 3)
    est._history_cool.extend([780.0] * 3)
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=-100.0):
        state = before._thermostat.prop_algorithm.save_state()
    handler = _fresh_handler()
    algo = handler._thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    p = _patched(0.0, None)
    with p[1], p[3], patch("custom_components.vtherm_smartpi.algo.time.time", return_value=1e9):
        algo.load_state(state)
    return handler, algo


@pytest.mark.parametrize("back_after", [2.0, None])
async def test_a_restart_with_the_boiler_read_off_first_is_no_blink(back_after, caplog) -> None:
    """Home Assistant restarts while the room calls with the boiler on; SmartPI's
    first reading finds the boiler off (or on at once), and it is back 2 s later. That first
    reading is no stop the estimator saw: the boiler's return is the start of the heating, not a
    blink, so after 1260 s of heat the room's own stop times its passive response."""
    caplog.set_level(logging.INFO)
    handler, algo = _restarted()
    est = algo.dt_est
    await _control(handler, 0.02, back_after is None, 0.0, 19.0)
    if back_after is not None:
        algo._committed_on_percent = 1.0  # the scheduler commits the first cycle
        _boiler_at(handler, True, back_after, 19.0)
        _boiler_at(handler, True, back_after + BOILER_BLINK_S, 19.0)
        assert est.last_start_time == back_after
    at = 60.02
    while at < 1260.0:
        await _control(handler, at, True, 1.0, 19.0 + max(0.0, at - 900.0) * 0.0003)
        at += 60.0
    await _control(handler, at, True, 0.0, 19.3)

    assert (est.state, est.cool_start_time) == ("WAITING_COOL_RESPONSE", at)
    assert "blink" not in caplog.text


async def test_a_state_that_becomes_known_off_while_the_room_heats_records_the_stop() -> None:
    """The boiler's state could not be read while the room heated (the estimator saw SmartPI's
    command, a heating detection waiting); it becomes known, off: the heat stopped at a moment not
    known. The stop is recorded and the heating detection dropped, but no passive response is
    timed."""
    handler = _fresh_handler()
    algo = handler._thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    est = algo.dt_est
    est.deadtime_heat_s = est.deadtime_cool_s = 780.0
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    est.state, est.last_power, est.last_start_time, est.last_stop_time = "OFF", 0.0, None, -1000.0
    for at in range(0, 1201, 60):
        await _control(handler, float(at), None, 1.0, 19.0)
    assert (est.state, est.last_start_time) == ("WAITING_HEAT_RESPONSE", 0.0)

    await _control(handler, 1230.0, False, 1.0, 19.0)

    assert (est.last_power, est.last_stop_time, est.last_outcome["heat"]) == (0.0, 1230.0, "aborted")
    for at in (1290.0, 1350.0):
        await _control(handler, at, False, 1.0, 19.0)
    assert est.state != "WAITING_COOL_RESPONSE" and est.last_stop_time == 1230.0


async def test_a_heating_detection_goes_on_through_short_stops_of_the_boiler() -> None:
    """A valve room whose boiler goes off 24 s in every 10 minutes, in another room's relay off
    parts (threshold of 2 devices): the water in its emitter goes on heating. Held as heat, the
    short stops neither end its heating detection nor record a stop, and the detection ends
    detected. Without the hold, each stop ended it (in a simulation, no heating dead time in six
    days)."""
    handler = _handler()
    _without_heat(handler)
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    for at in range(100, 1000, 60):
        await _step(handler, float(at), False, 0.0, temp=T0)
    await _step(handler, 1000.0, True, 1.0, temp=T0)
    _boiler(handler, True, 1000.0 + BOILER_BLINK_S)
    assert (est.state, est.heat_start_time) == ("WAITING_HEAT_RESPONSE", 1000.0)

    def rising(at: float) -> float:
        return T0 + max(0.0, at - 1000.0 - DEAD) * COOL_C_PER_S * 4

    for off in (1300.0, 1900.0):
        _boiler(handler, False, off)
        _boiler(handler, True, off + 24.0)
    at = 1000.0
    while at < 2400.0 and est.state == "WAITING_HEAT_RESPONSE":
        at += 60.0
        await _step(handler, at, True, 1.0, temp=rising(at))

    assert est.last_outcome["heat"] == "detected" and est.last_stop_time == 0.0
    assert not algo.boiler_learning_paused


async def test_the_hold_is_checked_120_s_after_the_boiler_s_stop() -> None:
    """With the boiler's changes followed, the handler checks the hold 120 s after the boiler's
    stop, without waiting for a calculation: the cut is dated from the stop, and the pause starts
    then. A boiler back before cancels the check."""
    handler = await _heated()
    algo = handler._thermostat.prop_algorithm
    handler._boiler_listener_remove = MagicMock()
    timers: list[tuple[float, object]] = []
    with patch("custom_components.vtherm_smartpi.handler.async_call_later", side_effect=lambda _h, delay, action: timers.append((delay, action)) or MagicMock()):
        _boiler(handler, False, STOP)
    assert [delay for delay, _ in timers] == [BOILER_VALVE_HOLD_S]

    trace = algo._causal_power_trace
    with (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=STOP + BOILER_VALVE_HOLD_S),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=STOP + BOILER_VALVE_HOLD_S),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=False),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
        patch.object(trace, "mark_unobserved_power", wraps=trace.mark_unobserved_power) as mark,
    ):
        timers[0][1](None)

    assert (algo.dt_est.last_stop_time, algo.boiler_on, algo.boiler_learning_paused) == (STOP, False, True)
    # The FF trim's trace marks the time without heat from the boiler's stop too.
    mark.assert_any_call(STOP, "central_boiler_off")


async def _idle_with_the_boiler_on(handler: SmartPIHandler) -> SmartPI:
    """The room without heat for long, not calling, while the boiler heats another room: the
    estimator follows the boiler on, at no power."""
    _without_heat(handler)
    for at in range(100, 1001, 60):
        algo = await _step(handler, float(at), True, 0.0, temp=T0)
    assert algo.boiler_on is True and algo.dt_est.last_power == 0.0
    return algo


@pytest.mark.parametrize("call_at", [1030.002, 1032.0], ids=["in the blink", "2 s after the boiler's return"])
async def test_a_room_that_starts_calling_around_a_blink_of_the_boiler_records_its_start(call_at, caplog) -> None:
    """The boiler, heating another room, goes off for 8 ms (a threshold raised),
    and the room starts calling in that blink or within 5 s of the boiler's return. Its heat had
    not flowed, so this is no blink for it: the boiler's return is the onset of its heat, from
    which a dead time counts (Astrom and Hagglund 1995, 2.4, p. 16). Its start is recorded and its
    heating detection arms; no "after a blink" line for it."""
    caplog.set_level(logging.INFO)
    handler = _handler()
    algo = await _idle_with_the_boiler_on(handler)
    est = algo.dt_est
    _boiler(handler, False, 1030.0)
    back = 1030.008
    if call_at < back:
        await _step(handler, call_at, False, 1.0, temp=T0)
        _boiler(handler, True, back)
    else:
        _boiler(handler, True, back)
        await _step(handler, call_at, True, 1.0, temp=T0)
    _boiler(handler, True, back + BOILER_BLINK_S)

    # Dated from the boiler's start (3.8.2); a call within its first 5 s counts from it too, at most 5 s early.
    assert est.state == "WAITING_HEAT_RESPONSE" and est.last_start_time == pytest.approx(back)
    assert "after a blink" not in caplog.text


@pytest.mark.parametrize(("stop_at", "armed"), [(1400.0, False), (1870.0, True)], ids=["370 s of heat", "840 s of heat"])
async def test_a_room_with_an_earlier_start_that_calls_in_a_blink_starts_again(stop_at, armed) -> None:
    """The room heated from 0 s and stopped at 100 s, then waited without heat while the boiler
    heated another room. It starts calling in an 8-ms blink of the boiler at 1030 s. The boiler's
    return is its new start, which replaces the one at 0 s, and its heating's settling (its dead
    time, 780 s) counts from it (3.6): stopped 370 s later, before it settled, it arms no passive
    detection; stopped 840 s later, after it, it does. Taken for a blink, the start at 0 s would
    have stayed, the heating would have counted 1400 s, and a passive detection would have armed
    after too short a heating."""
    handler = _handler(heat_start=0.0)
    algo = await _step(handler, 100.0, True, 0.0, temp=T0)
    est = algo.dt_est
    assert est.last_stop_time == 100.0 and est.state != "WAITING_COOL_RESPONSE"
    for at in range(160, 1001, 60):
        await _step(handler, float(at), True, 0.0, temp=T0)

    _boiler(handler, False, 1030.0)
    await _step(handler, 1030.002, False, 1.0, temp=T0)
    _boiler(handler, True, 1030.008)
    _boiler(handler, True, 1030.008 + BOILER_BLINK_S)
    assert est.last_start_time == pytest.approx(1030.008)
    for at in range(1090, int(stop_at), 60):
        await _step(handler, float(at), True, 1.0, temp=T0)
    await _step(handler, stop_at, True, 0.0, temp=T0)

    assert est.last_stop_time == stop_at and (est.state == "WAITING_COOL_RESPONSE") is armed
    if armed:
        assert est.cool_start_time == stop_at
