"""Start of the learning: the option, its default and its neutrality with the bootstrap start."""

import json
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from helpers import make_receipt
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.learning_start import LearningStart


def _make(**kwargs) -> SmartPI:
    return SmartPI(
        hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="LS", **kwargs
    )


def test_learning_start_defaults_to_bootstrap():
    algo = _make()
    assert algo.learning_start == "bootstrap"
    assert algo.phase == SmartPIPhase.HYSTERESIS
    assert algo.command_is_hysteresis is True
    assert algo.command_is_on_off is True
    assert algo.dt_est.passive_by_peak is False


def test_learning_start_rejects_an_unknown_value():
    with pytest.raises(ValueError):
        _make(learning_start="pulses")
    with pytest.raises(ValueError):
        LearningStart("")


def test_direct_start_lets_the_pi_drive_the_learning_stage():
    algo = _make(learning_start="direct")
    assert algo.phase == SmartPIPhase.HYSTERESIS
    assert algo.command_is_hysteresis is False
    assert algo.command_is_on_off is False
    assert algo.dt_est.passive_by_peak is True


def test_bootstrap_start_keeps_no_pulse_state_and_attaches_the_reader():
    algo = _make()
    assert algo.save_state().get("measure_pulse") is None
    assert algo.save_state().get("event_reading") is None and algo.save_state().get("measure_events") is None
    assert algo.learn_win.events.reader is algo._start.reading and algo.learn_win.events.record is algo._start.record
    assert algo.est.ramp_count == 0 and algo.est.start.handed is False
    assert algo._start.pre_model_a(None) is None


def test_direct_start_attaches_its_reader_and_record_to_the_detector():
    algo = _make(learning_start="direct")
    assert algo.learn_win.events.reader is algo._start.reading
    assert algo.learn_win.events.record is algo._start.record


def test_direct_state_with_a_record_survives_a_save_and_a_load():
    algo = _make(learning_start="direct")
    algo._start.record.enabled = True
    algo._start.record.feed(5000.0, 60.0, 0.4, 8.0, [(4900.0, 19.5)])
    algo._start.reading._index = 2
    state = json.loads(json.dumps(algo.save_state()))
    assert state["measure_events"]["power"] and state["event_reading"]["index"] == 2
    other = _make(learning_start="direct")
    other.load_state(state)
    assert other._start.record.rows >= 1 and other._start.reading.save()["index"] == 2
    bootstrap = _make()
    bootstrap.load_state(state)
    assert bootstrap._start.record.rows >= 1 and bootstrap._start.reading.save()["index"] == 2
    assert bootstrap._start.pulse.save(0.0) is None


def test_phase_and_command_properties_are_the_start_properties():
    for mode in ("bootstrap", "direct"):
        algo = _make(learning_start=mode)
        start = algo._start
        assert algo.phase == start.phase == SmartPIPhase.HYSTERESIS and start.learning_stage
        assert algo.command_is_hysteresis == start.command_is_hysteresis == (mode == "bootstrap")
        assert algo.command_is_on_off == start.command_is_on_off == (mode == "bootstrap")


async def test_a_switch_receipt_notes_the_applied_cycle_in_the_record():
    algo = _make(learning_start="direct")
    receipt = make_receipt(0.3, cycle_duration_sec=600.0)
    await algo.on_command_receipt(receipt)
    assert algo._start.record._cycles == [(receipt.cycle_start_monotonic, 180.0, 600.0)]
    assert algo.committed_on_percent == pytest.approx(0.3)


async def test_a_valve_receipt_notes_no_cycle():
    algo = _make(learning_start="direct")
    algo.configure_valve_linearization(False, None, valve_mode=True)
    await algo.on_command_receipt(make_receipt(0.3, cycle_duration_sec=600.0))
    await algo.on_command_receipt(make_receipt(0.35, kind="update"))
    assert algo._start.record._cycles == []
    assert algo.committed_on_percent == pytest.approx(0.35)


async def test_the_legacy_cycle_start_without_receipt_notes_no_cycle_and_the_record_holds_the_fraction():
    algo = _make(learning_start="direct")
    await algo.on_cycle_started(180.0, 420.0, 0.3, "heat")
    assert algo._start.record._cycles == []
    record = algo._start.record
    record.enabled = True
    record.feed(5000.0, 600.0, algo.linear_committed_on_percent, 8.0, [(4300.0, 19.5)])
    assert record.snapshot(5000.0)["power"][0][1] == pytest.approx(0.3)


def test_a_receipt_without_cycle_timing_notes_no_cycle():
    start = LearningStart("direct", MagicMock())
    receipt = MagicMock(spec=["on_time_sec", "cycle_duration_sec"])
    start.note_receipt(receipt, valve=False)
    assert start.record._cycles == []
