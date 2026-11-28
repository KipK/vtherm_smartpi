"""Measurement event detector: HEAT and COOL phases, source tag, mode changes, restart."""

import math
from collections import deque

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.measure_event import (
    EVENT_DROPPED,
    MeasureEventDetector,
    MeasureRecord,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.peak_search import PEAK_CONFIRMED

STEP_S = 60.0


class _Rig:
    """Room with a first-order emitter observed every minute; sign -1 cools."""

    def __init__(self, sign: float = 1.0, source=None) -> None:
        self.sign = sign
        self.mode = VThermHvacMode_HEAT if sign > 0 else VThermHvacMode_COOL
        self.temp = 20.0
        self.heat = 0.0
        self.now = 1000.0
        self.history: deque = deque()
        self.detector = MeasureEventDetector("test", source)
        self.events: list = []

    def run(self, u: float, minutes: float, mode=None) -> None:
        for _ in range(int(round(minutes))):
            for _ in range(6):
                self.heat += (u - self.heat) * (1.0 - math.exp(-10.0 / 300.0))
                self.temp += self.sign * 0.1 * self.heat * 10.0 / 60.0 - 0.001 * (self.temp - (15.0 if self.sign > 0 else 25.0)) * 10.0 / 60.0
            self.now += STEP_S
            self.history.append((self.now, round(self.temp, 2)))
            self.events += self.detector.observe(
                now=self.now, dt_s=STEP_S, u=u, hvac_mode=mode or self.mode,
                tin_history=self.history, context="regulation",
            )

    def pulse(self) -> None:
        self.run(0.0, 60)
        self.run(1.0, 40)
        self.run(0.0, 300)


def test_heat_pulse_gives_a_confirmed_closed_event_with_a_rise():
    rig = _Rig()
    rig.pulse()
    closed = [e for e in rig.events if e.closed]
    assert len(closed) == 1
    event = closed[0]
    assert event.outcome == PEAK_CONFIRMED and event.reason == ""
    search = event.phase["search"]
    assert search.peak_value > event.phase["base_value"]
    assert event.mode == str(VThermHvacMode_HEAT)


def test_cool_pulse_gives_a_closed_event_on_the_minimum():
    rig = _Rig(sign=-1.0)
    rig.pulse()
    closed = [e for e in rig.events if e.closed]
    assert len(closed) == 1
    event = closed[0]
    assert event.mode == str(VThermHvacMode_COOL)
    assert event.outcome == PEAK_CONFIRMED
    assert event.phase["search"].sign == -1.0
    assert event.phase["search"].peak_value < event.phase["base_value"]


def test_source_is_read_at_the_start_of_the_phase():
    sources = iter(["pulse", "bootstrap", "regulation"])
    rig = _Rig(source=lambda: next(sources))
    rig.pulse()
    event = [e for e in rig.events if e.closed][0]
    assert event.source == "pulse"
    assert event.phase["context"] == "regulation"


def test_source_defaults_to_regulation():
    rig = _Rig()
    rig.pulse()
    assert [e for e in rig.events if e.closed][0].source == "regulation"


def test_mode_change_drops_the_phase_of_the_previous_mode():
    rig = _Rig()
    rig.run(0.0, 60)
    rig.run(1.0, 10)
    rig.run(1.0, 5, mode=VThermHvacMode_COOL)
    dropped = [e for e in rig.events if e.kind == EVENT_DROPPED]
    assert len(dropped) == 1
    assert dropped[0].mode == str(VThermHvacMode_HEAT)
    assert dropped[0].reason == "heating mode left"
    assert rig.detector.phase_stage(str(VThermHvacMode_HEAT)) is None
    assert rig.detector.phase_stage(str(VThermHvacMode_COOL)) == "heating"


def test_unbridged_restart_drops_the_saved_phase_and_a_bridged_one_resumes_it():
    rig = _Rig()
    rig.run(0.0, 60)
    rig.run(1.0, 40)
    rig.run(0.0, 25)
    context = rig.detector.restart_context()
    saved = MeasureEventDetector.save_context(context)
    assert saved["phase"]["source"] == "regulation"
    assert saved["phase"]["bin_n"] > 0
    for bridged in (True, False):
        detector = MeasureEventDetector("again")
        detector.load_context(saved)
        events = detector.resolve_restart(bridged)
        if bridged:
            assert events == []
            assert detector.phase_stage(str(VThermHvacMode_HEAT)) == "cut"
        else:
            assert [e.reason for e in events] == ["interrupted by restart"]


def test_the_learning_window_saves_and_resumes_the_heating_phase_of_its_detector():
    """The learning window owns the detector: the heating phase in progress is saved, resumed on a bridged restart and
    dropped otherwise; a state saved under the former key of the heating balance is read too; COOL phases are not
    saved."""
    rig = _Rig()
    rig.run(0.0, 60)
    rig.run(1.0, 40)
    rig.run(0.0, 25)
    window = LearningWindowManager("t")
    window._events = rig.detector
    saved = window.save_state()["event_restart"]
    assert saved["phase"]["stage"] == "cut" and saved["phase"]["bin_n"] > 0
    for state in ({"event_restart": saved}, {"heating_balance": {"results": [], "restart": saved}}):
        for bridged in (True, False):
            other = LearningWindowManager("t")
            other.load_state(state)
            other.resume_after_restart(bridged)
            assert other.events.phase_stage(str(VThermHvacMode_HEAT)) == ("cut" if bridged else None)
    other.reset_all()
    assert other.events.restart_context() is None
    cool = _Rig(sign=-1.0)
    cool.run(0.0, 60)
    cool.run(1.0, 40)
    window._events = cool.detector
    assert window.save_state()["event_restart"] is None


# -- the power of the record: real on and off edges of a switch, the applied fraction otherwise -----------------------

def _power(record, t_end: float) -> list:
    return [[round(t, 3), v] for t, v in record.snapshot(t_end)["power"][:-1]]


def _fed(cycles, u: float, now: float = 1600.0, dt_s: float = 600.0):
    record = MeasureRecord()
    record.enabled = True
    for cycle in cycles:
        record.note_cycle(*cycle)
    record.feed(now, dt_s, u, 10.0, [(900.0, 20.0)])
    return record


def test_record_holds_the_on_and_off_edges_of_a_switch_cycle():
    record = _fed([(1000.0, 180.0, 600.0)], 0.3)
    assert _power(record, 1600.0) == [[0.0, 1.0], [180.0, 0.0]]    # on from the cycle start for 180 s, then off


def test_record_holds_the_edges_of_every_cycle_of_an_interval():
    record = _fed([(1000.0, 180.0, 600.0), (1600.0, 300.0, 600.0)], 0.3, now=2200.0, dt_s=1200.0)
    assert _power(record, 2200.0) == [[0.0, 1.0], [180.0, 0.0], [600.0, 1.0], [900.0, 0.0]]


def test_record_holds_a_full_and_an_empty_cycle_as_one_level():
    assert _power(_fed([(1000.0, 600.0, 600.0)], 1.0), 1600.0) == [[0.0, 1.0]]
    assert _power(_fed([(1000.0, 0.0, 600.0)], 0.0), 1600.0) == [[0.0, 0.0]]
    # a restart of a full cycle a moment late is no break
    assert _power(_fed([(1000.0, 600.0, 600.0), (1601.0, 600.0, 600.0)], 1.0, now=2200.0, dt_s=1200.0), 2200.0) == [[0.0, 1.0]]


def test_record_holds_the_fraction_without_noted_cycles_or_when_they_do_not_cover_the_interval():
    assert _power(_fed([], 0.3), 1600.0) == [[0.0, 0.3]]
    assert _power(_fed([(1000.0, 180.0, 600.0)], 0.3, now=2200.0, dt_s=1200.0), 2200.0) == [[0.0, 0.3]]   # no cycle after 1600
    assert _power(_fed([(1100.0, 180.0, 600.0)], 0.3), 1600.0) == [[0.0, 0.3]]                           # nothing before 1100


def test_record_forgets_the_noted_cycles_at_a_stop_and_keeps_them_through_a_clear():
    record = _fed([(1000.0, 180.0, 600.0)], 0.3)
    record.clear()
    assert len(record._cycles) == 1
    record.stop(1600.0)
    assert record._cycles == []


def test_record_keeps_a_bounded_number_of_cycles_and_replaces_a_repeated_start():
    record = MeasureRecord()
    for k in range(20):
        record.note_cycle(1000.0 + 600.0 * k, 100.0, 600.0)
    record.note_cycle(1000.0 + 600.0 * 19, 200.0, 600.0)
    assert len(record._cycles) <= 8 and record._cycles[-1][1] == 200.0
