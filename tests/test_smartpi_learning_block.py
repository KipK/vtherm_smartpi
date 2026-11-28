"""Learning block: the pause of the service, the disturbance entity and the heating-unavailable entity stop every
learning; the regulation goes on unchanged (the heating-unavailable entity holds the integral)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.config_flow import build_options_schema, build_user_settings_schema
from custom_components.vtherm_smartpi.const import (
    CONF_SMART_PI_DISTURBANCE_ENTITY,
    CONF_SMART_PI_HEATING_UNAVAILABLE_ENTITY,
    DEFAULT_OPTIONS,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.calibration import REQUEST_PAUSED, RESULT_CANCELLED
from custom_components.vtherm_smartpi.smartpi.event_reading import STATE_ENDED, STATE_WAITING
from custom_components.vtherm_smartpi.smartpi.learning_block import entity_on

from .helpers import force_smartpi_stable_mode
from .test_smartpi_calibration import TARGET, T_EXT, _calibration, _learned, _Room, clock  # noqa: F401

DISTURBANCE = "binary_sensor.disturbance"
UNAVAILABLE = "input_boolean.heating_unavailable"


class _States:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, entity_id: str):
        value = self.values.get(entity_id)
        return None if value is None else SimpleNamespace(state=value)


def _with_entities(algo: SmartPI) -> _States:
    states = _States()
    algo.learning_block.configure(SimpleNamespace(states=states), {
        CONF_SMART_PI_DISTURBANCE_ENTITY: DISTURBANCE, CONF_SMART_PI_HEATING_UNAVAILABLE_ENTITY: UNAVAILABLE})
    return states


def _published(algo: SmartPI) -> dict:
    return algo.get_published_diagnostics()


# -- entity states ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("state, on", [("on", True), ("off", False), ("unavailable", False), ("unknown", False),
                                       (None, False)])
def test_only_the_state_on_counts_unavailable_unknown_and_absent_count_as_off(state, on):
    states = _States()
    if state is not None:
        states.values[DISTURBANCE] = state
    assert entity_on(SimpleNamespace(states=states), DISTURBANCE) is on
    assert entity_on(SimpleNamespace(states=states), None) is False


def test_unavailable_entities_leave_the_learning_free(clock):
    algo = _learned()
    states = _with_entities(algo)
    states.values.update({DISTURBANCE: "unavailable", UNAVAILABLE: "unknown"})
    _Room(algo, clock).run(3)
    assert algo.learning_block.blocked is False and _published(algo)["learning"]["block_reason"] is None
    assert algo.ctl.last_integral_hold_source != "heating_unavailable"


def test_the_entities_never_change_the_saved_pause(clock):
    algo = _learned()
    states = _with_entities(algo)
    states.values[DISTURBANCE] = "on"
    _Room(algo, clock).run(2)
    assert algo.learning_block.blocked and algo.learning_enabled is True
    assert algo.save_state()["learning_enabled"] is True
    assert _published(algo)["learning"]["block_reason"] == "disturbance"
    algo.set_learning_enabled(False)
    states.values[DISTURBANCE] = "off"
    _Room(algo, clock).run(1)
    assert _published(algo)["learning"]["block_reason"] == "paused" and algo.save_state()["learning_enabled"] is False


# -- leaks of the pause ----------------------------------------------------------------------------------------
def test_the_pause_gives_the_t_ar_learning_and_the_room_model_an_exclusion_reason(clock):
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Block", room_model=True)
    force_smartpi_stable_mode(algo)
    algo.calculate(target_temp=TARGET, current_temp=19.5, ext_current_temp=T_EXT, hvac_mode=VThermHvacMode_HEAT)
    algo.room_history.observe = MagicMock()
    algo.room_model.observe = MagicMock()
    algo.set_learning_enabled(False)
    algo.observe_room_history(TARGET, 19.5, T_EXT, VThermHvacMode_HEAT)
    algo.observe_room_model(19.5, T_EXT, VThermHvacMode_HEAT, excluded="power shedding")
    assert algo.room_history.observe.call_args.kwargs["excluded"] == "learning paused"
    assert algo.room_model.observe.call_args.kwargs["excluded"] == "learning paused"
    algo.observe_room_model(19.5, T_EXT, VThermHvacMode_HEAT, excluded="window open")   # the room model follows it
    assert algo.room_model.observe.call_args.kwargs["excluded"] == "window open"
    algo.set_learning_enabled(True)
    algo.observe_room_history(TARGET, 19.5, T_EXT, VThermHvacMode_HEAT)
    assert algo.room_history.observe.call_args.kwargs["excluded"] is None


def test_no_learning_computation_starts_while_the_learning_is_blocked():
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Block", room_model=True)
    algo.room_history = MagicMock(active=True, dirty=False)
    handler = SmartPIHandler(MagicMock())
    handler._thermostat.hass.async_create_background_task = MagicMock(side_effect=lambda coro, _name: coro.close())
    algo.set_learning_enabled(False)
    handler._process_learning(algo)
    handler._thermostat.hass.async_create_background_task.assert_not_called()
    algo.set_learning_enabled(True)
    handler._process_learning(algo)
    handler._thermostat.hass.async_create_background_task.assert_called_once()


def test_the_pause_suspends_the_dead_time_detection_during_a_guard_cut_but_feeds_the_history(clock):
    algo = _learned()
    algo.set_learning_enabled(False)
    algo.dt_est.update = MagicMock()
    algo.guards._guard_cut_active = True
    before = len(algo.learn_win.temperatures.raw)
    clock.advance(60.0)
    algo.calculate(target_temp=TARGET, current_temp=21.0, ext_current_temp=T_EXT, hvac_mode=VThermHvacMode_HEAT)
    algo.dt_est.update.assert_not_called()
    assert len(algo.learn_win.temperatures.raw) == before + 1


def test_no_direct_start_pulse_and_no_hand_over_while_the_learning_is_blocked(clock):
    free = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Free", learning_start="direct")
    _Room(free, clock).run(2)
    assert free._start.pulse.active                       # the reference: a blank direct start begins a pulse
    paused = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                     name="Paused", learning_start="direct")
    paused.set_learning_enabled(False)
    paused._start.reading.kept_for = MagicMock(return_value={
        "mode": "heat", "a_per_min": 0.05, "b_per_min": 0.0003, "lag_s": 480.0, "at_s": None})
    paused.est.give_starting_values = MagicMock()
    _Room(paused, clock).run(30)
    assert not paused._start.pulse.active and paused._start.pulse.count == 0
    paused.est.give_starting_values.assert_not_called()


def test_a_pulse_in_progress_ends_when_the_learning_becomes_blocked(clock):
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Pulse", learning_start="direct")
    states = _with_entities(algo)
    room = _Room(algo, clock)
    room.run(2)
    assert algo._start.pulse.active
    states.values[UNAVAILABLE] = "on"
    room.tick()
    assert not algo._start.pulse.active and algo._start.pulse.history[-1]["end_reason"] == "heating unavailable"


def test_a_calibration_is_refused_and_cancelled_whatever_blocks_the_learning(clock):
    algo = _learned()
    states = _with_entities(algo)
    room = _Room(algo, clock)
    room.run(2)
    assert algo.force_calibration() is True
    states.values[DISTURBANCE] = "on"
    room.tick()
    assert _calibration(algo)["result"] == RESULT_CANCELLED and not algo._start.pulse.active
    assert algo.force_calibration() is False and _calibration(algo)["last_request"] == REQUEST_PAUSED


# -- response model ----------------------------------------------------------------------------------------------
def test_the_response_model_reads_nothing_while_blocked_but_its_record_stays_continuous(clock):
    algo = _learned()
    room = _Room(algo, clock)
    room.run(3)
    reader = algo._start.model_reading
    reader._readings.append({"state": STATE_WAITING, "mode": "heat", "index": 99, "fitting": False})
    rows = reader.record.rows
    algo.set_learning_enabled(False)
    assert reader._readings[-1]["state"] == STATE_ENDED and reader._readings[-1]["reason"] == "learning paused"
    algo.learn_win.events.observe = MagicMock()
    room.run(5)
    algo.learn_win.events.observe.assert_not_called()      # no event, no reading fed
    assert reader.record.rows > rows                       # the record goes on
    assert reader._excluded_at == pytest.approx(clock.mono)  # a regulation event overlapping the block is refused


# -- regulation unchanged --------------------------------------------------------------------------------------
def test_the_pause_leaves_the_temperature_history_and_the_anticipation_unchanged(clock):
    runs = {}
    for paused in (False, True):
        start = clock.mono
        algo = _learned()
        algo.est.give_starting_values = MagicMock()
        if paused:
            algo.set_learning_enabled(False)
        anticipated, commands = [], []
        for i in range(30):
            clock.advance(60.0)
            algo.calculate(target_temp=TARGET, current_temp=round(19.6 + 0.01 * i, 2), ext_current_temp=T_EXT,
                           hvac_mode=VThermHvacMode_HEAT)
            anticipated.append(_published(algo)["temperature"]["anticipated"])
            commands.append(algo.on_percent)
        history = [(t - start, v) for t, v in algo.learn_win.temperatures.raw]
        runs[paused] = (history, anticipated, commands)
    assert runs[True] == runs[False]
    assert runs[True][1][-1] > 19.6 + 0.01 * 29 + 0.03      # the anticipation works during the pause


def test_a_disturbance_leaves_the_integral_free(clock):
    algo = _learned()
    states = _with_entities(algo)
    states.values[DISTURBANCE] = "on"
    room = _Room(algo, clock, temp=TARGET - 1.0)
    room.run(12)                                       # past the dead-time window of the first heating
    first = algo.integral
    room.run(5)
    assert algo.learning_block.reason == "disturbance" and algo.integral != first
    assert algo.ctl.last_integral_hold_source == "none"


def test_heating_unavailable_holds_the_integral_then_for_the_effective_dead_time_without_learning_pause(clock):
    algo = _learned()
    states = _with_entities(algo)
    room = _Room(algo, clock, temp=TARGET - 1.0)
    room.run(15)                                       # past the dead-time window of the first heating
    states.values[UNAVAILABLE] = "on"
    room.run(1)
    held = algo.integral
    room.run(10)
    live = _published(algo)
    assert algo.integral == held and live["learning"]["block_reason"] == "heating unavailable"
    assert live["temperature"]["integral_hold_source"] == "heating_unavailable"
    states.values[UNAVAILABLE] = "off"
    room.run(1)
    resumed_at = clock.mono
    assert algo.learning_block.blocked is False
    assert algo.ctl.integral_hold_mode == "heating_unavailable_resume"
    assert _published(algo)["temperature"]["integral_hold_source"] == "heating_unavailable_resume"
    assert algo.learn_win.learning_resume_ts is None or algo.learn_win.learning_resume_ts <= resumed_at
    room.run(4)                                        # within the 270 s effective dead time of the model
    assert algo.integral == held
    room.run(3)
    assert algo.ctl.integral_hold_mode == "none" and algo.integral != held


# -- configuration ---------------------------------------------------------------------------------------------
def test_the_entity_selectors_belong_to_the_thermostat_settings_only():
    keys = {str(k) for k in build_user_settings_schema(dict(DEFAULT_OPTIONS), False).schema}
    assert {CONF_SMART_PI_DISTURBANCE_ENTITY, CONF_SMART_PI_HEATING_UNAVAILABLE_ENTITY} <= keys
    defaults = {str(k) for k in build_options_schema(dict(DEFAULT_OPTIONS)).schema}
    assert CONF_SMART_PI_DISTURBANCE_ENTITY not in defaults
