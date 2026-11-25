"""Values kept from the readings, the pulses and the starting values are per mode; plain summary of the start."""

import json
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import event_reading as er
from custom_components.vtherm_smartpi.smartpi.measure_event import MeasureRecord
from custom_components.vtherm_smartpi.smartpi.measure_pulse import MeasurePulse, PULSE_MAX_COUNT, PULSE_MIN_GAP_S
from custom_components.vtherm_smartpi.smartpi.starting_values import KEPT_RAMP_MEASURES


def _make(**kwargs) -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="PM", **kwargs)


def _reading(index: int, mode: str, a: float, b: float = 0.0004, lag: float = 300.0) -> dict:
    return {"index": index, "mode": mode, "a_per_min": a, "lag_s": lag, "b_per_min": b}


def _keep(reader: er.EventReader, mode: str, a: float, b: float, now: float = 100.0) -> None:
    reader._keep(_reading(1, mode, a, b), _reading(2, mode, a, b), now)


def test_kept_values_are_per_mode():
    reader = er.EventReader(MeasureRecord())
    _keep(reader, "heat", 0.08, 0.0004)
    assert reader.kept_a_per_min(VThermHvacMode_HEAT) == pytest.approx(0.08)
    assert reader.kept_for(VThermHvacMode_COOL) is None and reader.kept_a_per_min(VThermHvacMode_COOL) is None
    assert reader.wants_pulses(VThermHvacMode_HEAT) is False and reader.wants_pulses(VThermHvacMode_COOL) is True
    assert reader.wants_record(True, VThermHvacMode_HEAT) is False and reader.wants_record(True, VThermHvacMode_COOL) is True
    _keep(reader, "cool", 0.05, 0.0006)
    assert reader.kept_a_per_min(VThermHvacMode_COOL) == pytest.approx(0.05)
    assert reader.kept_a_per_min(VThermHvacMode_HEAT) == pytest.approx(0.08)       # the first mode's values stand
    assert reader.kept_b_per_min(VThermHvacMode_COOL) == pytest.approx(0.0006)     # a kept b stays with its reading


def test_a_keep_ends_only_the_readings_of_its_mode():
    reader = er.EventReader(MeasureRecord())
    waiting = {"state": er.STATE_WAITING, "mode": "cool", "index": 9, "fitting": False}
    heat = {"state": er.STATE_WAITING, "mode": "heat", "index": 8, "fitting": False}
    reader._readings = [heat, waiting]
    _keep(reader, "heat", 0.08, 0.0004)
    assert heat["state"] == er.STATE_ENDED and waiting["state"] == er.STATE_WAITING


def test_kept_values_of_both_modes_survive_a_save_and_a_load():
    reader = er.EventReader(MeasureRecord())
    _keep(reader, "heat", 0.08, 0.0004)
    _keep(reader, "cool", 0.05, 0.0006)
    saved = json.loads(json.dumps(reader.save()))
    other = er.EventReader(MeasureRecord())
    other.load(saved)
    assert other.kept_a_per_min("heat") == pytest.approx(0.08) and other.kept_a_per_min("cool") == pytest.approx(0.05)
    assert other.kept_lag_s("cool") == pytest.approx(300.0) and other.kept["cool"]["at_s"] is not None


def test_a_period_in_the_other_mode_opens_its_own_readings():
    reader = er.EventReader(MeasureRecord())
    _keep(reader, "heat", 0.08, 0.0004)
    event = MagicMock(closed=True, mode="cool", outcome="peak")
    event.phase = {"t_start": 10.0, "t_cut": 20.0, "search": MagicMock(peak_delay_s=5.0)}
    reader._record.enabled = True
    reader._record.feed(0.0, 60.0, 0.5, 10.0, [(5.0, 20.0)])
    reader._take(event, 30.0)
    assert reader._readings[-1]["reason"] != "values kept"
    event.mode = "heat"
    reader._take(event, 30.0)
    assert reader._readings[-1]["reason"] == "values kept"


def test_pulses_are_counted_per_mode():
    class _Dt:
        tin_history = []
    pulse, now = MeasurePulse(), 0.0
    for k in range(PULSE_MAX_COUNT + 1):
        pulse.update(now, 20.0, 19.9, VThermHvacMode_HEAT, False, True, True, False, _Dt())
        assert pulse.active is (k < PULSE_MAX_COUNT)
        pulse.interrupt(now, "test")
        now += PULSE_MIN_GAP_S + 1.0
    assert pulse.count == PULSE_MAX_COUNT and pulse.count_for("heat") == PULSE_MAX_COUNT and pulse.count_for("cool") == 0
    pulse.update(now, 20.0, 20.1, VThermHvacMode_COOL, True, True, True, False, _Dt())
    assert pulse.active and pulse.count == PULSE_MAX_COUNT + 1 and pulse.count_for("cool") == 1
    other = MeasurePulse()
    other.load(json.loads(json.dumps(pulse.save(now))))
    assert other.count_for("heat") == PULSE_MAX_COUNT and other.count_for("cool") == 1


def test_the_estimator_takes_the_a_of_each_mode_and_the_b_of_the_first_hand_over():
    algo = _make(learning_start="direct")
    start = algo._start
    start.reading._kept = {"heat": dict(mode="heat", a_per_min=0.08, lag_s=300.0, b_per_min=0.0004, pulses=[1, 2], at_s=0.0),
                           "cool": dict(mode="cool", a_per_min=0.05, lag_s=280.0, b_per_min=0.0009, pulses=[3, 4], at_s=0.0)}
    est = algo.est
    est.ensure_hvac_mode(VThermHvacMode_HEAT)
    start.hand_over(est, VThermHvacMode_HEAT)
    assert est.a == pytest.approx(0.08) and est.b == pytest.approx(0.0004) and est.ramp_count == 2 * KEPT_RAMP_MEASURES
    est.ensure_hvac_mode(VThermHvacMode_COOL)
    assert not est.usable_a                                            # the cooling channel starts blank
    start.hand_over(est, VThermHvacMode_COOL)
    assert est.a == pytest.approx(-0.05) and est.usable_a
    assert est.b == pytest.approx(0.0004)                              # b is shared: the first hand-over's value stays
    assert est.ramp_count == 2 * KEPT_RAMP_MEASURES                    # no second credit
    start.hand_over(est, VThermHvacMode_COOL)                          # once per mode
    est.ensure_hvac_mode(VThermHvacMode_HEAT)                          # the heating channel comes back with its value
    assert est.a == pytest.approx(0.08) and est.start.standing("a")
    saved = json.loads(json.dumps(est.start.save()))
    assert saved["modes"] == ["cool", "heat"]


def test_a_cool_period_after_values_kept_in_heat_gets_its_pulses():
    algo = _make(learning_start="direct")
    start = algo._start
    start.reading._kept = {"heat": dict(mode="heat", a_per_min=0.08, lag_s=300.0, b_per_min=0.0004, pulses=[1, 2], at_s=0.0)}
    start.pulse._mode_counts, start.pulse._count = {"heat": 2}, 2
    algo.est.ensure_hvac_mode(VThermHvacMode_COOL)
    start.reading.begin(VThermHvacMode_COOL)
    start.advance(1000.0, 20.0, 20.1, VThermHvacMode_COOL, 0.0)
    assert start.pulse.active and start.pulse.count_for("cool") == 1 and start.record.enabled is True


def test_the_published_start_summary_is_plain():
    algo = _make(learning_start="direct")
    start = algo._start
    live = algo.get_published_diagnostics()["learning"]["start"]
    assert live == {"mode": "direct", "pulses_done": 0, "pulse": "none", "values_kept": False, "values_kept_at": None,
                    "kept_a": None, "kept_b": None, "kept_lag_s": None}
    algo.est.ensure_hvac_mode(VThermHvacMode_HEAT)
    start.reading._kept = {"heat": dict(mode="heat", a_per_min=0.0812345, lag_s=301.4, b_per_min=0.0004123456, pulses=[1, 2], at_s=0.0)}
    for state, plain in (("idle", "none"), ("rest", "rest"), ("heat", "full power"), ("stop", "stop")):
        start.pulse._state = state
        assert algo.get_published_diagnostics()["learning"]["start"]["pulse"] == plain
    start.pulse._count, start.pulse._mode_counts = 3, {"heat": 2, "cool": 1}
    live = algo.get_published_diagnostics()["learning"]["start"]
    assert live["pulses_done"] == 2 and live["values_kept"] is True
    assert (live["kept_a"], live["kept_b"], live["kept_lag_s"]) == (0.08123, 0.000412, 301)
    assert isinstance(live["values_kept_at"], str) and "T" in live["values_kept_at"]
    algo.est.ensure_hvac_mode(VThermHvacMode_COOL)
    live = algo.get_published_diagnostics()["learning"]["start"]
    assert live["pulses_done"] == 1 and live["values_kept"] is False and live["kept_a"] is None
    assert _make().get_published_diagnostics()["learning"]["start"]["mode"] == "bootstrap"
