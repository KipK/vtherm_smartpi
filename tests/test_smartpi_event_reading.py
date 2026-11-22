"""Reader of the measurement events: the bounded record, the fit job and its application, save and restart."""

import json
import math
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import event_reading as er
from custom_components.vtherm_smartpi.smartpi.event_fit import run_fit
from custom_components.vtherm_smartpi.smartpi.learning_start import LearningStart
from custom_components.vtherm_smartpi.smartpi.measure_event import EVENT_CLOSED, MeasureEvent, MeasureEventDetector, MeasureRecord
from custom_components.vtherm_smartpi.smartpi.peak_search import PEAK_CONFIRMED, PEAK_NONE

STEP_S = 60.0
A_PER_MIN, LAG_S = 0.1, 300.0


class _Rig:
    """Room with a first-order emitter, observed every minute, with its record and reader attached to the detector."""

    def __init__(self, source: str = "pulse") -> None:
        self.record = MeasureRecord()
        self.record.enabled = True
        self.reader = er.EventReader(self.record)
        self.detector = MeasureEventDetector("test", lambda: source)
        self.detector.record, self.detector.reader = self.record, self.reader
        self.temp, self.heat, self.now = 20.0, 0.0, 1000.0
        self.history: deque = deque()
        self.estimator, self.lag = MagicMock(), MagicMock()

    def calculation(self, u: float, sync: bool = True) -> None:
        """One calculation: the step of the reader, the observation of the detector, then the job runs."""
        for _ in range(6):
            self.heat += (u - self.heat) * (1.0 - math.exp(-10.0 / LAG_S))
            self.temp += A_PER_MIN * self.heat * 10.0 / 60.0 - 0.001 * (self.temp - 15.0) * 10.0 / 60.0
        self.now += STEP_S
        self.step()
        self.history.append((self.now, round(self.temp, 2)))
        self.detector.observe(now=self.now, dt_s=STEP_S, u=u, hvac_mode=VThermHvacMode_HEAT,
                              tin_history=self.history, context="regulation", ext=10.0)
        if sync:
            self.reader.run_queued_sync()

    def step(self) -> None:
        self.reader.step(self.now, VThermHvacMode_HEAT, False, self.lag, self.detector.phase_start)

    def run(self, u: float, minutes: int, sync: bool = True) -> None:
        for _ in range(minutes):
            self.calculation(u, sync)

    def pulse(self, sync: bool = True) -> None:
        self.run(0.0, 60, sync)
        self.run(1.0, 40, sync)
        self.run(0.0, 300, sync)


def _readings(rig: _Rig) -> list:
    return rig.reader.snapshot()["readings"]


def test_record_keeps_the_change_points_only():
    record = MeasureRecord()
    record.enabled = True
    history = [(900.0, 20.0)]
    for k in range(1, 121):                  # nothing moves for two hours
        record.feed(1000.0 + 60.0 * k, 60.0, 0.0, 10.0, history)
    rows = record.snapshot(1000.0 + 7200.0)
    assert len(rows["power"]) == 2           # the value and the closing point
    assert len(rows["room"]) == 1 and len(rows["ext"]) == 1
    history.append((8000.0, 20.2))
    record.feed(8200.0, 60.0, 1.0, 10.0005, history)
    rows = record.snapshot(8200.0)
    assert len(rows["power"]) == 3 and len(rows["room"]) == 2 and len(rows["ext"]) == 1   # 0.0005 is under the step


def test_record_is_not_fed_while_disabled():
    rig = _Rig()
    rig.record.enabled = False
    rig.run(1.0, 10)
    assert rig.record.rows == 0 and rig.record.t0 is None


def test_record_is_trimmed_to_the_lead_and_the_preceding_hours():
    rig = _Rig()
    rig.run(0.0, 20 * 60)
    assert rig.record.t0 == pytest.approx(rig.now - er.READING_LEAD_S - er.RECORD_PRE_S)
    assert rig.record.snapshot(rig.now)["room"][0][0] == 0.0     # the held room value at the front is kept


def test_record_follows_the_phase_in_progress_and_the_window_of_an_open_reading():
    rig = _Rig()
    rig.run(0.0, 9 * 60)
    rig.run(1.0, 40)
    assert rig.detector.phase_start is not None
    # the phase starts at the observation after the step that trimmed: at most one calculation of the preceding hours is gone
    assert rig.record.t0 == pytest.approx(rig.detector.phase_start - er.READING_LEAD_S - er.RECORD_PRE_S, abs=STEP_S + 1.0)
    rig.run(0.0, 300, sync=False)
    reading = _readings(rig)[0]
    assert reading["state"] == er.STATE_WAITING
    assert rig.record.t0 == pytest.approx(reading["window_start_s"] - er.RECORD_PRE_S, abs=STEP_S + 1.0)


def test_span_and_point_bounds_are_the_designed_ones():
    assert er.RECORD_SPAN_S == pytest.approx(30.5 * 3600.0)
    assert er.RECORD_MAX_POINTS == 6000


def test_a_channel_over_its_cap_ends_the_oldest_open_reading(monkeypatch):
    rig = _Rig()
    rig.pulse(sync=False)
    assert _readings(rig)[0]["state"] == er.STATE_WAITING
    monkeypatch.setattr(er, "RECORD_MAX_POINTS", 40)
    for k in range(60):                      # room changes faster than the reading closes
        rig.record._put("room", rig.now + k, 20.0 + 0.01 * (k + 1))
    rig.step()
    assert _readings(rig)[0]["state"] == er.STATE_ENDED and _readings(rig)[0]["reason"] == "record full"
    assert rig.record.rows <= 40


def test_a_channel_over_its_cap_without_reading_loses_its_oldest_points(monkeypatch):
    rig = _Rig()
    rig.run(0.0, 5)
    monkeypatch.setattr(er, "RECORD_MAX_POINTS", 20)
    for k in range(100):
        rig.record._put("ext", rig.now + k, 10.0 + 0.01 * k)
    rig.step()
    assert rig.record.rows <= 20


def test_record_save_and_load_keep_the_points():
    rig = _Rig()
    rig.pulse()
    saved = json.loads(json.dumps(rig.record.save()))
    restored = MeasureRecord()
    restored.load(saved)
    before, after = rig.record.snapshot(rig.now), restored.snapshot(rig.now)
    for name in ("power", "room", "ext"):
        assert len(after[name]) == len(before[name]) and len(before[name]) > (name != "ext")
        for (t1, v1), (t2, v2) in zip(before[name], after[name]):
            assert abs(t1 - t2) <= 1.0 + 1e-6 and abs(v1 - v2) <= 0.0005 + 1e-9
    assert all(isinstance(point[0], int) for point in saved["power"])


def test_record_load_ignores_a_malformed_state():
    record = MeasureRecord()
    for bad in (None, 3, {"origin": "x"}, {"origin": 1000, "power": [[0, 1.0]], "room": 4, "ext": []},
                {"origin": 1000, "power": [[5, 1.0], [2, 0.0]], "room": [], "ext": []}):
        record.load(bad)
        assert record.rows == 0 and record.t0 is None


def test_a_pulse_event_opens_a_reading_and_queues_its_fit_at_once():
    rig = _Rig()
    rig.pulse(sync=False)
    reading = _readings(rig)[0]
    assert reading["state"] == er.STATE_WAITING and reading["fitting"] is True
    assert reading["heat_start_s"] + 2400.0 < reading["first_end_s"] <= rig.now   # the window ends when the event closes
    assert rig.reader.active is True


def test_the_first_window_ends_at_the_close_of_the_event_after_the_peak():
    rig = _Rig()
    closed = []
    read_events = rig.reader.read_events
    rig.reader.read_events = lambda events, now: (closed.extend((e, now) for e in events if e.closed), read_events(events, now))[1]
    rig.pulse(sync=False)
    (event, close_s), = closed
    peak_s = event.phase["t_cut"] + event.phase["search"].peak_delay_s
    reading = _readings(rig)[0]
    assert event.outcome == PEAK_CONFIRMED and peak_s < close_s - 600.0     # the confirmation waits well after the peak
    assert reading["first_end_s"] == pytest.approx(close_s) and reading["next_end_s"] == pytest.approx(close_s)
    rig.reader.run_queued_sync()
    rig.step()
    assert _readings(rig)[0]["fits"][0]["end_s"] == pytest.approx(close_s)   # the first fit reads the whole window


def _closed_event(t_cut: float, peak_delay_s: float, outcome: str) -> MeasureEvent:
    phase = {"t_start": t_cut - 600.0, "t_cut": t_cut, "source": "pulse", "search": SimpleNamespace(peak_delay_s=peak_delay_s)}
    return MeasureEvent(EVENT_CLOSED, str(VThermHvacMode_HEAT), "", outcome, phase)


def test_the_first_window_is_never_shorter_than_up_to_the_peak_and_ends_with_the_event_without_a_peak():
    for outcome, peak_delay_s, close_s, expected in (
        (PEAK_CONFIRMED, 450.0, 3000.0, 3000.0),     # the peak is before the close
        (PEAK_CONFIRMED, 900.0, 800.0, 1300.0),      # a peak that lies after the close (never happens in order) is kept
        (PEAK_NONE, 0.0, 3000.0, 3000.0),            # no peak: the end of the event
    ):
        rig = _Rig()
        rig.record.t0 = 0.0
        rig.reader.read_events([_closed_event(400.0, peak_delay_s, outcome)], close_s)
        assert _readings(rig)[0]["first_end_s"] == pytest.approx(expected)


def test_a_hysteresis_event_opens_a_reading_like_a_pulse_event():
    rig = _Rig(source="bootstrap")
    rig.pulse(sync=False)
    reading = _readings(rig)[0]
    assert reading["state"] == er.STATE_WAITING and reading["fitting"] is True and rig.reader.active is True


def test_other_sources_open_no_reading():
    for source in ("regulation", "calibration"):
        rig = _Rig(source=source)
        rig.pulse()
        assert _readings(rig) == [] and rig.reader.active is False


def test_without_a_response_lag_the_kept_lag_is_given_to_nobody():
    rig = _Rig(source="bootstrap")
    rig.reader._kept = {"mode": "heat", "a_per_min": 0.05, "lag_s": 300.0, "b_per_min": 0.0005, "pulses": [1, 2], "at_s": 0.0}
    lag = MagicMock()
    rig.reader.step(rig.now, VThermHvacMode_HEAT, False, None, None)
    rig.reader.step(rig.now, VThermHvacMode_HEAT, False, lag, None)
    lag.give_lag.assert_called_once_with(300.0)
    assert rig.reader.kept_lag_s(VThermHvacMode_HEAT) == 300.0       # kept and published all the same


def test_a_fit_is_applied_at_the_next_calculation():
    rig = _Rig()
    rig.pulse(sync=False)
    assert rig.reader.active
    rig.reader.run_queued_sync()
    reading = _readings(rig)[0]
    assert reading["fits"] == [] and reading["fitting"] is True and not rig.reader.active   # run, not applied
    rig.step()
    reading = _readings(rig)[0]
    assert len(reading["fits"]) == 1 and reading["fitting"] is False
    assert reading["fits"][0]["a_per_min"] == pytest.approx(A_PER_MIN, rel=0.3)


def test_a_reading_completes_over_successive_fits_one_job_at_a_time():
    rig = _Rig()
    rig.pulse()
    rig.run(0.0, 120)
    reading = _readings(rig)[0]
    assert reading["state"] in (er.STATE_COMPLETE, er.STATE_ABANDONED)
    assert 1 <= len(reading["fits"]) <= er.READING_MAX_FITS
    assert reading["result"]["lag_s"] == pytest.approx(LAG_S, rel=0.35)


def test_a_reset_or_a_change_of_mode_discards_the_fit_in_flight():
    rig = _Rig()
    rig.pulse(sync=False)
    rig.reader.run_queued_sync()
    rig.reader.reset()
    rig.step()
    assert _readings(rig) == [] and rig.reader.kept is None
    rig = _Rig()
    rig.pulse(sync=False)
    rig.reader.run_queued_sync()
    rig.reader.begin(VThermHvacMode_COOL)    # the mode was HEAT: dropped
    rig.reader.begin(VThermHvacMode_HEAT)
    rig.step()
    reading = _readings(rig)[0]
    assert reading["state"] == er.STATE_ENDED and reading["reason"] == "mode change" and reading["fits"] == []
    assert rig.record.rows == 0 or rig.record.t0 is not None


def test_the_job_of_an_interrupted_pulse_gives_no_reading():
    rig = _Rig()
    rig.run(0.0, 60)
    rig.run(1.0, 40)
    rig.reader.interrupt(rig.now, "power shedding")
    rig.run(0.0, 300)
    assert _readings(rig)[0]["state"] == er.STATE_NONE and _readings(rig)[0]["reason"] == "thermostat stopped"


def _restored(rig: _Rig):
    state = json.loads(json.dumps({"reading": rig.reader.save(), "record": rig.record.save()}))
    other = _Rig()
    other.reader.load(state["reading"])
    other.record.load(state["record"])
    other.record.enabled = True
    other.history, other.now, other.temp, other.heat = rig.history, rig.now, rig.temp, rig.heat
    return other


def test_a_queued_fit_is_queued_again_after_a_bridged_restart():
    rig = _Rig()
    rig.pulse(sync=False)
    job = rig.reader._job
    other = _restored(rig)
    other.reader.restart(True)
    assert other.reader._job is None and _readings(other)[0]["state"] == er.STATE_WAITING
    other.step()
    again = other.reader._job
    assert again is not None and again["index"] == job["index"] and again["t_end"] == pytest.approx(job["t_end"], abs=1.0)
    for name in ("power", "room", "ext"):
        assert len(again["snap"][name]) == len(job["snap"][name])
    other.reader.run_queued_sync()
    other.step()
    assert len(_readings(other)[0]["fits"]) == 1


def test_a_restored_reading_without_its_record_ends_instead_of_failing():
    rig = _Rig()
    rig.pulse(sync=False)
    other = _restored(rig)
    other.record.clear()
    other.reader.restart(True)
    other.step()
    assert _readings(other)[0]["state"] == er.STATE_ENDED and _readings(other)[0]["reason"] == "no record"
    assert other.reader.active is False


def test_an_unbridged_restart_ends_the_readings_and_drops_the_record():
    rig = _Rig()
    rig.pulse(sync=False)
    other = _restored(rig)
    other.reader.restart(False)
    reading = _readings(other)[0]
    assert reading["state"] == er.STATE_ENDED and reading["reason"] == "interrupted by restart"
    assert other.record.rows == 0 and other.reader.active is False


def test_complete_readings_and_kept_values_survive_an_unbridged_restart():
    rig = _Rig()
    rig.reader._done.append({"index": 1, "mode": "heat", "a_per_min": 0.1, "lag_s": 300.0, "b_per_min": 0.001})
    rig.reader._index = 1
    other = _restored(rig)
    other.reader.restart(False)
    assert other.reader._done == rig.reader._done
    rig.reader._keep(rig.reader._done[0], dict(rig.reader._done[0], index=2), rig.now)
    kept = json.loads(json.dumps(rig.reader.save()))
    other.reader.load(kept)
    assert other.reader.kept_a_per_min("heat") == pytest.approx(0.1) and other.reader.kept_lag_s("heat") == pytest.approx(300.0)
    assert other.reader.wants_pulses is False


def test_the_kept_values_of_an_earlier_state_load():
    """A state saved before the readings were read on events holds the kept values only."""
    reader = er.EventReader(MeasureRecord())
    reader.load({"kept": {"mode": "heat", "a_per_min": 0.05, "lag_s": 400.0, "b_per_min": 0.0005, "pulses": [2, 3]},
                 "at_wall": 1.0e9})
    assert reader.kept_a_per_min("heat") == pytest.approx(0.05) and reader.kept_b_per_min("heat") == pytest.approx(0.0005)


def test_learning_start_saves_the_record_and_the_readings_and_reads_an_earlier_key():
    algo = MagicMock()
    start = LearningStart("direct", algo)
    assert algo.learn_win.events.reader is start.reading and algo.learn_win.events.record is start.record
    start.record.enabled = True
    start.record.feed(1000.0, 60.0, 0.5, 10.0, [(900.0, 20.0)])
    start.reading._index = 1
    state = json.loads(json.dumps(start.save(1000.0)))
    assert "measure_events" in state and "event_reading" in state
    other = LearningStart("direct", MagicMock())
    other.load(state)
    assert other.record.rows >= 1 and other.reading.save()["index"] == 1
    earlier = LearningStart("direct", MagicMock())
    earlier.load({"pulse_reading": {"kept": {"mode": "heat", "a_per_min": 0.05, "lag_s": 400.0}, "at_wall": 1.0e9}})
    assert earlier.reading.kept_lag_s("heat") == pytest.approx(400.0)


def test_the_bootstrap_start_attaches_its_reader_and_saves_nothing_before_a_reading():
    algo = MagicMock()
    start = LearningStart("bootstrap", algo)
    assert algo.learn_win.events.reader is start.reading and algo.learn_win.events.record is start.record
    assert start.save(1000.0) == {}
    start.record.enabled = True
    start.record.feed(1000.0, 60.0, 0.5, 10.0, [(900.0, 20.0)])
    start.reading._index = 1
    state = json.loads(json.dumps(start.save(1000.0)))
    assert "measure_events" in state and "event_reading" in state and "measure_pulse" not in state
    other = LearningStart("bootstrap", MagicMock())
    other.load(state)
    assert other.record.rows >= 1 and other.reading.save()["index"] == 1


def test_a_synthetic_snapshot_is_fitted_without_the_reader():
    # first-order emitter, simulated on a 10 s step
    room, temp, heat = [[0.0, 20.0]], 20.0, 0.0
    for k in range(2000):
        u = 1.0 if 180 <= k < 360 else 0.0
        heat += (u - heat) * (1.0 - math.exp(-10.0 / LAG_S))
        temp += A_PER_MIN * heat * 10.0 / 60.0
        if abs(temp - room[-1][1]) > 0.01:
            room.append([k * 10.0 + 10.0, round(temp, 2)])
    snap = {"power": [[0.0, 0.0], [1800.0, 1.0], [3600.0, 0.0], [20000.0, 0.0]], "room": room,
            "ext": [[0.0, 10.0]], "cooling": False, "start": 0.0, "end": 20000.0}
    result = run_fit(snap)
    assert result["a_per_min"] == pytest.approx(A_PER_MIN, rel=0.25)
    assert result["lag_s"] == pytest.approx(LAG_S, rel=0.35)
    assert run_fit(dict(snap, ext=[])) is None
