"""Reading of the measurement events as a whole.

While the learning stage lasts, the detector of the measurement events (``smartpi/measure_event.py``) records the power
applied to the emitter, the room temperature and the outdoor temperature (``MeasureRecord``). Each event that a
measurement pulse (``smartpi/measure_pulse.py``, direct start) or the on/off hysteresis (bootstrap start) drove, a
heating phase and the temperature peak that follows its cut, is read over a window of that record with a model of the
whole chain (``smartpi/event_fit.py``).

The reader is fed by the detector: a closed event opens a reading. The window of a reading starts ``READING_LEAD_S``
before the heating step and first ends when the event closes (the peak confirmed, or the end of the event when it has
no peak), so it holds the samples the confirmation waited for and is never shorter than up to the peak. The
fit runs as a job outside the regulation: the reader queues it (a plain-data snapshot of the record), the handler runs
it in an executor (``async_run``; ``run_queued_sync`` without a handler) and the next calculation applies the result.
One job is queued at a time per thermostat. After each fit the window is extended to ``READING_WINDOW_LAGS`` times the
lag the fit found (no shorter than the first window, no longer than 24 hours), up to ``READING_MAX_FITS`` fits. The
reading is complete when the window covers ``READING_COVERAGE`` of ``READING_WINDOW_LAGS`` lags of its last fit,
abandoned when it reaches the longest window or the last fit without that.

Two complete readings of the same mode agree when their ``a`` and their ``L`` each differ by at most
``READING_AGREE_REL`` of the larger. As soon as two readings of the same blank start and of the same mode agree, their
means are kept for that mode: they are saved, never change afterwards and are dropped by a learning reset. The values
are kept per mode, like the ``a`` channel of the a/b estimator, so a period in the other mode starts without kept values
and runs its own readings. The kept ``a`` and the mean loss coefficient ``b`` of the two readings are what
``LearningStart`` gives to the a/b estimator as starting values (``smartpi/starting_values.py``): ``a`` of each mode
once, ``b`` (a property of the room, shared by the modes) with the first hand-over only. With
the direct start the kept values also feed the gains before the first model (``LearningStart.pre_model_a``) and the
anticipation horizon (``smartpi/anticipation.py``). With the bootstrap start the kept lag is kept and published with
the readings, nothing reads it.

The record is trimmed from the front to ``RECORD_PRE_S`` before the start of the oldest window still open (or of the
phase in progress), so that the emitter filter of a fit starts well before its window, and to ``RECORD_SPAN_S`` at
most; when a channel holds more than ``RECORD_MAX_POINTS`` points the oldest open reading ends. The record, the readings
in progress and the kept values are saved. A restart that the restart bridge bridges resumes them (a job in flight is
queued again); otherwise the readings in progress end and the record is dropped. A thermostat stop, a change of mode,
the start of a calibration or a learning reset ends the readings in progress; a change of the setpoint or the
publication of the model does not.

The events of the pulses of a calibration (smartpi/calibration.py) are read the same way but form a set of their own:
they are tested among themselves only, and two agreeing readings give the calibration reading of that mode, which is
published only (never kept as starting values, never given to the learning). A new calibration drops the previous
calibration readings; the end of a calibration ends its readings in progress.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from . import job_abort
from .calibration import readings_agree_on
from .const import EVENT_MIN_ON_S, EVENT_MIN_RISE_C
from .event_fit import readings_agree, run_fit, run_response_fit
from .measure_event import (
    MODE_COOL,
    RECORD_MAX_POINTS,
    SOURCE_BOOTSTRAP,
    SOURCE_CALIBRATION,
    SOURCE_PULSE,
    SOURCE_REGULATION,
    MeasureEvent,
    MeasureRecord,
    _integer,
    _number,
)
from .peak_search import PEAK_CONFIRMED, PEAK_NONE
from .quiet_periods.solar import astral_available, dark_intervals, validate_position
from .response_model import PEAK_SLOPE_MAX_S, ResponseModel, peak_sample
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

_LOGGER = logging.getLogger(__name__)

READING_LEAD_S = 1800.0          # the window starts this long before the heating step
READING_WINDOW_LAGS = 6.0        # target length of the window after the heating step, in lags of the last fit
READING_COVERAGE = 0.9           # share of the target the window must cover for the reading to be complete
READING_MAX_WINDOW_S = 24.0 * 3600.0
READING_MAX_FITS = 8
RECORD_PRE_S = 6.0 * 3600.0      # record kept before the window of a reading (1.5 times the largest fitted inertia)
RECORD_SPAN_S = RECORD_PRE_S + READING_LEAD_S + READING_MAX_WINDOW_S

STATE_WAITING = "waiting"        # the window is shorter than its target, or its fit is queued
STATE_COMPLETE = "complete"
STATE_ABANDONED = "abandoned"
STATE_ENDED = "ended"            # ended without result (restart, stop, mode, calibration, reset, values kept, record full)
STATE_NONE = "none"              # the event gave no reading

_TIME_KEYS = ("heat_start_s", "first_end_s", "window_start_s", "next_end_s")


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


class EventReader:
    """Readings of the events, kept values and the fit job. Times are monotonic seconds, as ``SmartPI.calculate`` reads
    them. ``record`` is the record of the detector the reader is attached to."""

    _fit = staticmethod(run_fit)   # the fit a job runs

    def __init__(self, record: MeasureRecord) -> None:
        self._record = record
        self._abort = threading.Event()
        self._running = False
        self.reset()

    def reset(self) -> None:
        """Blank start: no record, no reading, no kept values, no job."""
        self._record.clear()
        self._mode: str | None = None
        self._readings: list[dict[str, Any]] = []
        self._index = 0                         # readings opened
        self._done: list[dict[str, Any]] = []   # complete readings, in the order they opened
        self._tests: list[dict[str, Any]] = []
        self._kept: dict[str, dict[str, Any]] = {}   # by mode
        self._cal_kept: dict[str, dict[str, Any]] = {}   # the agreeing reading of the calibration, by mode
        self._interrupted_at: float | None = None
        self._job: dict[str, Any] | None = None     # the fit queued or running (None: none)
        self._result: tuple[int, Any] | None = None  # (reading index, result) waiting for the next calculation
        self.version = 0                        # grows at each event worth an audit line

    # -- read-only view ------------------------------------------------------------------------------------------
    @property
    def kept(self) -> dict[str, dict[str, Any]]:
        """The kept values by mode (empty before any are kept)."""
        return {mode: dict(values) for mode, values in self._kept.items()}

    def kept_for(self, hvac_mode: object) -> dict[str, Any] | None:
        """The kept values measured in ``hvac_mode``, None when none are kept for it."""
        if hvac_mode is None:
            return None
        return self._kept.get(str(hvac_mode))

    def kept_a_per_min(self, hvac_mode: object) -> float | None:
        kept = self.kept_for(hvac_mode)
        return None if kept is None else float(kept["a_per_min"])

    def kept_b_per_min(self, hvac_mode: object) -> float | None:
        """Mean loss coefficient of the two agreeing readings (1/min); None when no values are kept in that mode or
        the kept values were saved without it."""
        kept = self.kept_for(hvac_mode)
        return None if kept is None or kept.get("b_per_min") is None else float(kept["b_per_min"])

    def kept_lag_s(self, hvac_mode: object) -> float | None:
        """Mean emitter lag of the two agreeing readings (s) in that mode; None when none are kept."""
        kept = self.kept_for(hvac_mode)
        return None if kept is None else float(kept["lag_s"])

    def calibration_reading(self, hvac_mode: object) -> dict[str, Any] | None:
        """The agreeing reading of the calibration in ``hvac_mode``, None before."""
        return None if hvac_mode is None else self._cal_kept.get(str(hvac_mode))

    @property
    def calibration_in_progress(self) -> bool:
        """True while a reading of a calibration pulse waits for its window or its fit."""
        return any(r["state"] == STATE_WAITING and r.get("calibration") for r in self._readings)

    def start_calibration(self) -> None:
        """A calibration starts: the readings of the start in progress end, the previous calibration readings are
        dropped."""
        self._end_in_progress("calibration", calibration=False)
        self._end_in_progress("calibration ended", calibration=True)
        self._done = [d for d in self._done if not d.get("calibration")]
        self._cal_kept = {}
        self.version += 1

    def end_calibration(self, now: float) -> None:
        """The calibration ends: its readings in progress end."""
        self._end_in_progress("calibration ended", now, calibration=True)

    def wants_pulses(self, hvac_mode: object) -> bool:
        """True while no values are kept in ``hvac_mode``: the pulses go on (three at most per mode, six hours apart)."""
        return self.kept_for(hvac_mode) is None

    @property
    def in_progress(self) -> bool:
        return any(r["state"] == STATE_WAITING for r in self._readings)

    def wants_record(self, learning_stage: bool, hvac_mode: object) -> bool:
        """True while the record is needed: before values are kept in ``hvac_mode``, during the learning stage or a
        reading."""
        return self.kept_for(hvac_mode) is None and (learning_stage or self.in_progress)

    @property
    def record_rows(self) -> int:
        return self._record.rows

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version, "record_rows": self._record.rows, "record_start_s": self._record.t0,
            "kept": self.kept, "tests": [dict(t) for t in self._tests],
            "readings": [{**r, "fits": [dict(f) for f in r["fits"]]} for r in self._readings],
        }

    # -- the fit job -----------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a queued fit waits to be run (the handler's loop looks at it)."""
        return self._job is not None and not self._job["started"] and not self._abort.is_set()

    def stop(self) -> None:
        """End the fit in progress and start no other (removal, reload)."""
        self._abort.set()

    async def async_run(self, hass: Any) -> None:
        """Run the queued fit in the executor; the next calculation applies its result."""
        job = self._job
        if self._running or job is None or job["started"] or self._abort.is_set():
            return
        job["started"] = self._running = True
        try:
            result = await hass.async_add_executor_job(job_abort.run, self._abort, self._fit, job["snap"])
        except job_abort.JobAborted:
            return
        except Exception:  # noqa: BLE001 - a failed fit must never stop the regulation
            _LOGGER.exception("event reading: fit failed")
            result = False
        finally:
            self._running = False
        self._finish(job, result)

    def run_queued_sync(self) -> None:
        """Run the queued fit here (no handler): the next calculation applies its result."""
        job = self._job
        if job is None or job["started"]:
            return
        job["started"] = True
        try:
            result = self._fit(job["snap"])
        except Exception:  # noqa: BLE001 - a reading must never disturb the regulation
            _LOGGER.exception("event reading: fit failed")
            result = False
        self._finish(job, result)

    def _finish(self, job: dict[str, Any], result: Any) -> None:
        """Keep the result of ``job`` for the next calculation, unless the readings were reset or ended meanwhile."""
        if self._job is job:
            self._job, self._result = None, (job["index"], dict(result, end_s=job["t_end"]) if result else result)

    def _queue_due(self, now: float) -> None:
        """Queue the fit of the first reading whose window end has come, when no fit is queued or waiting to apply."""
        if self._job is not None or self._result is not None:
            return
        for r in self._readings:
            if r["state"] == STATE_WAITING and now >= r["next_end_s"]:
                if self._record.t0 is None:             # a restored reading whose record was not restored
                    self._end(r, "no record", now)
                    continue
                snap = self._record.snapshot(r["next_end_s"])
                snap.update(cooling=r["cooling"], start=max(r["window_start_s"] - snap["origin"], 0.0),
                            end=r["next_end_s"] - snap["origin"], heat=r["heat_start_s"] - snap["origin"])
                r["fitting"] = True
                self._job = {"index": r["index"], "t_end": r["next_end_s"], "snap": snap, "started": False}
                return

    # -- the record ----------------------------------------------------------------------------------------------
    def begin(self, hvac_mode: object) -> None:
        """First call of a calculation: a change of mode drops the record and ends the readings."""
        if hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            mode = str(hvac_mode)
            if self._mode is not None and mode != self._mode:
                self._end_in_progress("mode change")
                self._record.clear()
            self._mode = mode

    def _trim(self, now: float, phase_start: float | None) -> None:
        """Trim the record from the front; end the oldest open reading while a channel holds too many points."""
        rec = self._record
        while rec.t0 is not None:
            starts = [now - READING_LEAD_S] + [r["window_start_s"] for r in self._readings if r["state"] == STATE_WAITING]
            if phase_start is not None:
                starts.append(phase_start - READING_LEAD_S)
            rec.trim(max(min(starts) - RECORD_PRE_S, now - RECORD_SPAN_S))
            cap = rec.cap_front(RECORD_MAX_POINTS)
            if cap is None:
                return
            open_ = [r for r in self._readings if r["state"] == STATE_WAITING]
            if not open_:
                rec.trim(cap)
                return
            self._end(open_[0], "record full", now)

    # -- the readings --------------------------------------------------------------------------------------------
    def _end(self, reading: dict[str, Any], reason: str, now: float | None) -> None:
        reading["state"], reading["reason"], reading["end_s"], reading["fitting"] = STATE_ENDED, reason, now, False
        if self._job is not None and self._job["index"] == reading["index"]:
            self._job = None
        self.version += 1

    def _end_in_progress(
        self, reason: str, now: float | None = None, mode: str | None = None, calibration: bool | None = None,
    ) -> None:
        for r in self._readings:
            if (
                r["state"] == STATE_WAITING and (mode is None or r["mode"] == mode)
                and (calibration is None or bool(r.get("calibration")) == calibration)
            ):
                self._end(r, reason, now)

    def interrupt(self, now: float, reason: str) -> None:
        """The thermostat stops (off, window, shedding): the readings in progress end without result, and so does the
        reading of the event that was running."""
        self._end_in_progress(reason, now)
        self._interrupted_at = now
        self._record.stop(now)

    def block(self, now: float, reason: str) -> None:
        """The learning is blocked (smartpi/learning_block.py): the readings in progress end without result; the record
        goes on."""
        self._end_in_progress(reason, now)

    def restart(self, bridged: bool) -> None:
        """The restart decision: not bridged, the readings in progress end and the record is dropped."""
        if not bridged:
            self._end_in_progress("interrupted by restart")
            self._record.clear()

    def step(self, now: float, phase_start: float | None) -> None:
        """Once per calculation: apply the result of the fit that ran, queue the next one that is due, trim the record
        and keep agreeing values."""
        if self._result is not None:
            index, result = self._result
            self._result = None
            reading = next((r for r in self._readings if r["index"] == index), None)
            if reading is not None and reading["state"] == STATE_WAITING:
                self._fit_done(reading, result, now)
        self._queue_due(now)
        self._trim(now, phase_start)

    def read_events(self, events: list[MeasureEvent], now: float) -> None:
        """The events the detector closed or dropped at ``now``: a closed one that a pulse (of the start or of a
        calibration) or the hysteresis drove opens a reading, whose first fit is queued at once."""
        for event in events:
            if event.closed and event.source in (SOURCE_PULSE, SOURCE_BOOTSTRAP, SOURCE_CALIBRATION):
                self._take(event, now)
        self._queue_due(now)

    def _take(self, event: MeasureEvent, now: float) -> None:
        """A pulse or hysteresis event closed: open its reading, unless a stop cut it short, values are kept or the
        record does not reach back to its heating step."""
        phase = event.phase
        heat_start = phase["t_start"]
        peak = None if event.outcome == PEAK_NONE else phase["t_cut"] + phase["search"].peak_delay_s
        first_end = now if peak is None else max(now, peak)   # the close of the event, never before the peak
        self._index += 1
        entry: dict[str, Any] = {
            "index": self._index, "mode": event.mode, "cooling": event.mode == MODE_COOL, "heat_start_s": heat_start,
            "first_end_s": first_end, "window_start_s": None, "state": STATE_NONE, "reason": None,
            "fits": [], "result": None, "next_end_s": None, "end_s": None, "fitting": False,
        }
        calibration = event.source == SOURCE_CALIBRATION
        if calibration:
            entry["calibration"] = True
        self._readings.append(entry)
        self.version += 1
        rec = self._record
        if self._interrupted_at is not None and heat_start <= self._interrupted_at < now:
            entry["reason"] = "thermostat stopped"
        elif event.mode in (self._cal_kept if calibration else self._kept):
            entry["reason"] = "values kept"
        elif rec.t0 is None or rec.t0 > heat_start:
            entry["reason"] = "no record"
        else:
            entry["state"] = STATE_WAITING
            entry["window_start_s"] = max(rec.t0, heat_start - READING_LEAD_S)
            entry["next_end_s"] = entry["first_end_s"]

    def _fit_done(self, reading: dict[str, Any], result: Any, now: float) -> None:
        """The fit of a reading ran: decide complete, abandoned or wait for the next window end."""
        reading["fitting"] = False
        self.version += 1
        if result is False:
            reading["state"], reading["reason"], reading["end_s"] = STATE_ABANDONED, "fit error", now
            return
        if result is None:
            reading["state"], reading["reason"], reading["end_s"] = STATE_ABANDONED, "no fit", now
            return
        reading["fits"].append(result)
        reading["result"] = result
        heat_start = reading["heat_start_s"]
        window = result["end_s"] - heat_start
        first = reading["first_end_s"] - heat_start
        want = READING_WINDOW_LAGS * result["lag_s"]
        if window >= READING_COVERAGE * want - 1e-6:
            reading["state"], reading["end_s"] = STATE_COMPLETE, now
            self._complete(reading, now)
            return
        target = min(max(want, first), READING_MAX_WINDOW_S)
        if window >= READING_COVERAGE * target - 1e-6:
            reading["state"], reading["reason"], reading["end_s"] = STATE_ABANDONED, "24 hours", now
        elif len(reading["fits"]) >= READING_MAX_FITS:
            reading["state"], reading["reason"], reading["end_s"] = STATE_ABANDONED, "eight fits", now
        else:
            reading["next_end_s"] = heat_start + target
            reading["target_s"] = target

    def _complete(self, reading: dict[str, Any], now: float) -> None:
        """A reading is complete: test it against the earlier ones of the blank start, keep an agreeing pair."""
        result = reading["result"]
        done = {"index": reading["index"], "mode": reading["mode"], "a_per_min": result["a_per_min"],
                "lag_s": result["lag_s"], "b_per_min": result["b_per_min"]}
        if reading.get("calibration"):
            done["calibration"] = True
        self._done.append(done)
        self._done.sort(key=lambda d: d["index"])
        for j in range(len(self._done)):
            for i in range(j):
                first, second = self._done[i], self._done[j]
                if (
                    first["mode"] != second["mode"] or reading["index"] not in (first["index"], second["index"])
                    or bool(first.get("calibration")) != bool(second.get("calibration"))
                ):
                    continue
                agree = readings_agree(first["a_per_min"], first["lag_s"], second["a_per_min"], second["lag_s"])
                self._tests.append({
                    "pulses": [first["index"], second["index"]],
                    "a_rel": abs(first["a_per_min"] - second["a_per_min"]) / max(first["a_per_min"], second["a_per_min"], 1e-12),
                    "lag_rel": abs(first["lag_s"] - second["lag_s"]) / max(first["lag_s"], second["lag_s"], 1e-12),
                    "agree": agree,
                })
                if agree:
                    self._keep(first, second, now)
                    return

    def _keep(self, first: dict[str, Any], second: dict[str, Any], now: float) -> None:
        calibration = bool(first.get("calibration"))
        store = self._cal_kept if calibration else self._kept
        kept = store[first["mode"]] = {
            "mode": first["mode"], "a_per_min": 0.5 * (first["a_per_min"] + second["a_per_min"]),
            "lag_s": 0.5 * (first["lag_s"] + second["lag_s"]),
            "b_per_min": 0.5 * (first["b_per_min"] + second["b_per_min"]), "pulses": [first["index"], second["index"]], "at_s": now,
        }
        if calibration:   # the calibration corrects the loss coefficient only when the two readings agree on it too
            kept["b_agree"] = readings_agree_on(first["b_per_min"], second["b_per_min"])
        self._end_in_progress("values kept", now, first["mode"], calibration)
        self.version += 1
        _LOGGER.info("event reading: %s in %s (a %.4f C/min, lag %.0f s)", "calibration reading" if calibration else "values kept",
                     kept["mode"], kept["a_per_min"], kept["lag_s"])

    # -- persistence -----------------------------------------------------------------------------------------------
    def save(self) -> dict[str, Any] | None:
        """The kept values by mode, the complete readings of the modes without kept values and the readings in progress;
        the fit queued or running is not saved (a restored reading queues it again). None when there is nothing to save."""
        saved: dict[str, Any] = {}
        for key, store in (("kept", self._kept), ("calibration_kept", self._cal_kept)):
            if store:
                saved[key] = {mode: {k: v for k, v in kept.items() if k != "at_s"} | {"at_wall": monotonic_to_wall_ts(kept["at_s"])}
                              for mode, kept in store.items()}
        waiting = [r for r in self._readings if r["state"] == STATE_WAITING]
        cal_done = any(d.get("calibration") for d in self._done)
        if self._index and (waiting or not self._kept or cal_done):
            saved.update({"index": self._index, "mode": self._mode,
                          "done": [dict(d) for d in self._done
                                   if d["mode"] not in (self._cal_kept if d.get("calibration") else self._kept)],
                          "readings": [self._save_reading(r) for r in waiting]})
        return saved or None

    @staticmethod
    def _save_reading(reading: dict[str, Any]) -> dict[str, Any]:
        saved = {k: v for k, v in reading.items() if k not in ("fitting", "end_s", "fits", "result")}
        for key in _TIME_KEYS:
            saved[key] = monotonic_to_wall_ts(reading[key])
        saved["fits"] = [dict(f, end_s=monotonic_to_wall_ts(f["end_s"])) for f in reading["fits"]]
        return saved

    def _load_reading(self, raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("mode"), str) or _integer(raw.get("index")) is None:
            return None
        times = {key: wall_to_monotonic_ts(raw.get(key)) for key in _TIME_KEYS}
        fits = raw.get("fits")
        if any(t is None for t in times.values()) or not isinstance(fits, list):
            return None
        for fit in fits:
            if not isinstance(fit, dict) or not all(_number(fit.get(k)) is not None for k in ("a_per_min", "lag_s", "end_s")):
                return None
        reading = {"index": raw["index"], "mode": raw["mode"], "cooling": raw["mode"] == MODE_COOL, **times,
                   "state": STATE_WAITING, "reason": None, "fits": [dict(f, end_s=wall_to_monotonic_ts(f["end_s"])) for f in fits],
                   "end_s": None, "fitting": False, "result": None}
        if raw.get("calibration") is True:
            reading["calibration"] = True
        reading["result"] = reading["fits"][-1] if fits else None
        if _number(raw.get("target_s")) is not None:
            reading["target_s"] = float(raw["target_s"])
        return reading

    def load(self, saved: object) -> None:
        self.reset()
        if not isinstance(saved, dict):
            return
        kept = saved.get("kept")
        if isinstance(kept, dict) and isinstance(kept.get("mode"), str):   # one set of values, no mode key
            kept = {kept["mode"]: dict(kept, at_wall=saved.get("at_wall"))}
        self._load_kept(kept, self._kept)
        self._load_kept(saved.get("calibration_kept"), self._cal_kept)
        index = _integer(saved.get("index"))
        if index is None or index < 0:
            return
        self._index = index
        self._mode = saved["mode"] if isinstance(saved.get("mode"), str) else None
        for raw in saved.get("done") if isinstance(saved.get("done"), list) else []:
            if (isinstance(raw, dict) and isinstance(raw.get("mode"), str) and _integer(raw.get("index")) is not None
                    and all(_finite(raw.get(k)) for k in ("a_per_min", "lag_s", "b_per_min"))):
                done = {k: raw[k] for k in ("index", "mode", "a_per_min", "lag_s", "b_per_min")}
                if raw.get("calibration") is True:
                    done["calibration"] = True
                self._done.append(done)
        for raw in saved.get("readings") if isinstance(saved.get("readings"), list) else []:
            reading = self._load_reading(raw)
            if reading is not None:
                self._readings.append(reading)

    @staticmethod
    def _load_kept(kept: object, store: dict[str, dict[str, Any]]) -> None:
        for mode, values in (kept.items() if isinstance(kept, dict) else ()):
            if (
                isinstance(values, dict) and isinstance(mode, str) and _finite(values.get("a_per_min")) and values["a_per_min"] > 0.0
                and _finite(values.get("lag_s")) and values["lag_s"] > 0.0
            ):
                pulses = values.get("pulses")
                b_kept = values.get("b_per_min")
                store[mode] = {
                    "mode": mode, "a_per_min": float(values["a_per_min"]), "lag_s": float(values["lag_s"]),
                    "b_per_min": float(b_kept) if _finite(b_kept) and b_kept >= 0.0 else None,
                    "pulses": list(pulses) if isinstance(pulses, list) else [], "at_s": wall_to_monotonic_ts(values.get("at_wall")),
                }
                if values.get("b_agree") is True:
                    store[mode]["b_agree"] = True


MODEL_READINGS_KEPT = 8          # ended readings of the model kept for the audit
READING_DARK_LAGS = 3.0          # dark part of the window after the heating step, in emitter lags


def _slope_per_min(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (C/min) of three points or more, None otherwise."""
    if len(points) < 3:
        return None
    n = len(points)
    tm = sum(t for t, _ in points) / n
    vm = sum(v for _, v in points) / n
    sxx = sum((t - tm) ** 2 for t, _ in points)
    return None if sxx <= 0.0 else 60.0 * sum((t - tm) * (v - vm) for t, v in points) / sxx


class ModelReader(EventReader):
    """Readings of the response model (smartpi/response_model.py): the readings of the base reader, on a record of
    their own that runs in every stage, for every closed event of the detector, fitted by ``run_response_fit``.

    Pulse, bootstrap and calibration events are read as the base reader reads them, whatever values are kept.
    Regulation events are read after a rest of ``EVENT_MIN_REST_S`` (the event has no reason), with
    ``EVENT_MIN_ON_S`` of full-power-equivalent heating and a rise of ``EVENT_MIN_RISE_C`` to the
    peak, while SmartPI is in control over the window (no stop, no exclusion seen by the thermostat), and only with a
    known home position. The window of a regulation reading ends ``READING_WINDOW_LAGS`` emitter lags after the
    heating step (the published lag, else the lag of its last fit), whenever the regulated phase closes.

    With a known home position, the window of an event whose heating step starts in darkness ends at the next sunrise;
    the event is in daylight when its heating step is not in darkness or when the darkness after it lasts less than
    ``READING_DARK_LAGS`` emitter lags (the published lag, else the lag of its fit). An event in daylight is fitted
    and shown with its flag and given to the model as such (smartpi/response_model.py: used only while the mode has
    no other event). Without a home position no event is in daylight. Each confirmed peak gives a peak sample when the
    model and ``b`` are published.
    """

    _fit = staticmethod(run_response_fit)

    def __init__(self, model: ResponseModel) -> None:
        self.model = model
        self.position = None
        super().__init__(MeasureRecord())
        self._record.enabled = True

    @property
    def record(self) -> MeasureRecord:
        return self._record

    def reset(self) -> None:
        super().reset()
        self._excluded_at: float | None = None     # last calculation at which SmartPI was not in control
        self._b: float | None = None               # published loss coefficient (1/min)

    def set_position(self, latitude: Any, longitude: Any) -> None:
        """Position of the home; unknown when not valid or when the solar position cannot be computed."""
        pos = validate_position(latitude, longitude)
        self.position = pos if pos is not None and astral_available() else None

    def note_control(self, now: float, excluded: str | None, b: float | None) -> None:
        """Once per calculation: why SmartPI's command is not the applied one (None when it is) and the published
        loss coefficient."""
        if excluded:
            self._excluded_at = now
        self._b = b

    def wants_record(self, learning_stage: bool, hvac_mode: object) -> bool:
        return True

    def read_events(self, events: list[MeasureEvent], now: float) -> None:
        """Every closed event: its peak sample, then its reading when it is admitted."""
        for event in events:
            if not event.closed:
                continue
            self._peak(event)
            reason = self._refusal(event)
            if reason is not None:
                self.model.refuse(reason)
                continue
            self._take(event, now)
            entry = self._readings[-1]
            entry["source"] = event.source
            if entry["state"] == STATE_NONE:
                self.model.refuse(entry["reason"])
                continue
            dark_end = None if self.position is None else self._dark_end(entry["heat_start_s"])
            entry["daytime"] = self.position is not None and dark_end is None
            ends = [entry["first_end_s"]] + ([] if dark_end is None else [dark_end])
            lag = self.model.emitter_lag_s(event.mode)
            if event.source == SOURCE_REGULATION and lag is not None:
                ends.append(entry["heat_start_s"] + READING_WINDOW_LAGS * lag)
            entry["first_end_s"] = entry["next_end_s"] = min(ends)
            if dark_end is not None:
                entry["dark_end_s"] = dark_end
        ended = [r for r in self._readings if r["state"] != STATE_WAITING]
        self._readings = [r for r in self._readings if r["state"] == STATE_WAITING or r in ended[-MODEL_READINGS_KEPT:]]
        self._queue_due(now)

    def _refusal(self, event: MeasureEvent) -> str | None:
        """Why a regulation event is not read (plain words), None when it is read."""
        if event.source != SOURCE_REGULATION:
            return None
        if event.reason:
            return event.reason
        phase, search = event.phase, event.phase["search"]
        if phase["on_s"] < EVENT_MIN_ON_S:
            return "heating too short"
        t_peak = phase["t_cut"] + search.peak_delay_s
        baseline = phase["base_value"] + phase["base_slope"] * (t_peak - phase["t_start"])
        if (-1.0 if event.mode == MODE_COOL else 1.0) * (search.peak_value - baseline) < EVENT_MIN_RISE_C:
            return "temperature rise too small"
        start = phase["t_start"] - READING_LEAD_S
        if self._excluded_at is not None and self._excluded_at >= start:
            return "SmartPI not in control"
        if self._interrupted_at is not None and self._interrupted_at >= start:
            return "thermostat stopped"
        return "home position unknown" if self.position is None else None

    def _peak(self, event: MeasureEvent) -> None:
        """Peak sample of a confirmed peak (published only)."""
        values = self.model.values(event.mode)
        if event.outcome != PEAK_CONFIRMED or values is None or self._b is None or self._b <= 0.0:
            return
        phase, sign = event.phase, -1.0 if event.mode == MODE_COOL else 1.0
        t_cut = phase["t_cut"]
        slope = _slope_per_min(self._record.points("room", t_cut - min(values["tau_e_s"], PEAK_SLOPE_MAX_S), t_cut))
        temp = phase["cut_temp"] if phase["cut_temp"] is not None else self._record.value_at("room", t_cut)
        ext = self._record.value_at("ext", t_cut)
        if temp is None or ext is None:
            return
        tau = peak_sample(values["theta_s"], phase["search"].peak_delay_s, None if slope is None else sign * slope,
                          self._b * sign * (temp - ext))
        if tau is not None:
            self.model.add_peak(event.mode, tau)

    def _queue_due(self, now: float) -> None:
        """The base reader's queueing; the snapshot also carries theta and tau_e of the model in force (prediction)."""
        job = self._job
        super()._queue_due(now)
        if self._job is not None and self._job is not job:
            mode = next(r["mode"] for r in self._readings if r["index"] == self._job["index"])
            values = self.model.values(mode)
            self._job["snap"]["model"] = None if values is None else (values["theta_s"], values["tau_e_s"])

    def _dark_end(self, t: float) -> float | None:
        """End (monotonic) of the darkness that holds ``t`` at the home position, None when ``t`` is not in darkness."""
        w = monotonic_to_wall_ts(t)
        try:
            end = None
            for a, b in sorted(dark_intervals(self.position, w, w + READING_MAX_WINDOW_S)):
                if a <= (w if end is None else end) < b:     # blocks of a long darkness follow each other
                    end = b
            return None if end is None else t + (end - w)
        except Exception:  # noqa: BLE001 - without the solar position the window is not known to be dark
            _LOGGER.debug("response model: darkness unknown", exc_info=True)
            return None

    def _fit_done(self, reading: dict[str, Any], result: Any, now: float) -> None:
        """The fit of a reading ran. The window after the heating step targets ``READING_WINDOW_LAGS`` lags of the last
        fit (no shorter than the first window), cut at the sunrise and at 24 hours; a regulation reading targets that
        many published lags (lags of its last fit without a published model) and a longer window is fitted again
        shorter. Complete when the window covers ``READING_COVERAGE`` of the target, abandoned at 24 hours or at the
        last fit without that."""
        if not result:
            super()._fit_done(reading, result, now)
            self.model.refuse(reading["reason"])
            return
        reading["fitting"] = False
        self.version += 1
        reading["fits"].append(result)
        reading["result"] = result
        heat = reading["heat_start_s"]
        window = result["end_s"] - heat
        want = READING_WINDOW_LAGS * result["lag_s"]
        regulation = reading.get("source", SOURCE_REGULATION) == SOURCE_REGULATION
        if regulation:
            lag = self.model.emitter_lag_s(reading["mode"])
            want = want if lag is None else READING_WINDOW_LAGS * lag
        else:
            want = max(want, reading["first_end_s"] - heat)
        capped = min(want, READING_MAX_WINDOW_S)
        target = min(capped, reading.get("dark_end_s", float("inf")) - heat)
        covered = window >= READING_COVERAGE * target - 1e-6
        too_long = regulation and window > target / READING_COVERAGE + 1e-6
        if too_long and len(reading["fits"]) < READING_MAX_FITS:
            reading["next_end_s"], reading["target_s"] = heat + target, target
        elif covered and not too_long and (target < capped or window >= READING_COVERAGE * want - 1e-6):
            reading["state"], reading["end_s"] = STATE_COMPLETE, now
            self._complete(reading, now)
        elif (covered and not too_long) or len(reading["fits"]) >= READING_MAX_FITS:
            reading["state"], reading["end_s"] = STATE_ABANDONED, now
            reading["reason"] = "eight fits" if too_long or not covered else "24 hours"
            self.model.refuse(reading["reason"])
        else:
            reading["next_end_s"], reading["target_s"] = heat + target, target

    def _complete(self, reading: dict[str, Any], now: float) -> None:
        """A complete reading goes to the model, with its daylight flag; a regulation reading only with SmartPI in
        control over its window."""
        source = reading.get("source", SOURCE_REGULATION)
        result = reading["result"]
        if source == SOURCE_REGULATION and self._excluded_at is not None and self._excluded_at >= reading["window_start_s"]:
            self.model.refuse("SmartPI not in control")
            return
        daytime = bool(reading.get("daytime"))
        if "dark_end_s" in reading:
            lag = self.model.emitter_lag_s(reading["mode"])
            lag = result.get("lag_s", 0.0) if lag is None else lag
            daytime = reading["dark_end_s"] - reading["heat_start_s"] < READING_DARK_LAGS * lag - 1e-6
        reading["daytime"] = daytime
        reading["outcome"] = self.model.add(reading["mode"], source, result, now, daytime)

    @staticmethod
    def _save_reading(reading: dict[str, Any]) -> dict[str, Any]:
        saved = EventReader._save_reading(reading)
        if "dark_end_s" in reading:
            saved["dark_end_s"] = monotonic_to_wall_ts(reading["dark_end_s"])
        return saved

    def _load_reading(self, raw: object) -> dict[str, Any] | None:
        reading = super()._load_reading(raw)
        if reading is not None:
            reading["source"] = raw["source"] if isinstance(raw.get("source"), str) else SOURCE_REGULATION
            reading["daytime"] = raw.get("daytime") is True
            dark_end = wall_to_monotonic_ts(raw.get("dark_end_s"))
            if dark_end is not None:
                reading["dark_end_s"] = dark_end
        return reading
