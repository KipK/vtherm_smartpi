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

Two complete readings agree when their ``a`` and their ``L`` each differ by at most ``READING_AGREE_REL`` of the
larger. As soon as two readings of the same blank start agree, their means are kept: they are saved, never change
afterwards and are dropped by a learning reset. The kept ``a`` and the mean loss coefficient ``b`` of the two readings
are what ``LearningStart`` gives, once, to the a/b estimator as starting values (``smartpi/starting_values.py``). With
the direct start the kept values also feed the gains before the first model (``LearningStart.pre_model_a``), the
anticipation horizon (``smartpi/anticipation.py``) and the learning of ``a`` through the response-lag estimator
(``ResponseLagEstimator.give_lag``), which counts as resolved with it: the learning of ``a`` then runs on its existing
condition. With the bootstrap start the kept lag is kept and published with the readings, nothing reads it.

The record is trimmed from the front to ``RECORD_PRE_S`` before the start of the oldest window still open (or of the
phase in progress), so that the emitter filter of a fit starts well before its window, and to ``RECORD_SPAN_S`` at
most; when a channel holds more than ``RECORD_MAX_POINTS`` points the oldest open reading ends. The record, the readings
in progress and the kept values are saved. A restart that the restart bridge bridges resumes them (a job in flight is
queued again); otherwise the readings in progress end and the record is dropped. A thermostat stop, a change of mode,
a calibration or a learning reset ends the readings in progress; a change of the setpoint or the publication of the
model does not.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from . import job_abort
from .event_fit import readings_agree, run_fit
from .measure_event import (
    MODE_COOL,
    RECORD_MAX_POINTS,
    SOURCE_BOOTSTRAP,
    SOURCE_PULSE,
    MeasureEvent,
    MeasureRecord,
    _integer,
    _number,
)
from .peak_search import PEAK_NONE
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
        self._kept: dict[str, Any] | None = None
        self._interrupted_at: float | None = None
        self._job: dict[str, Any] | None = None     # the fit queued or running (None: none)
        self._result: tuple[int, Any] | None = None  # (reading index, result) waiting for the next calculation
        self.version = 0                        # grows at each event worth an audit line

    # -- read-only view ------------------------------------------------------------------------------------------
    @property
    def kept(self) -> dict[str, Any] | None:
        return None if self._kept is None else dict(self._kept)

    def kept_for(self, hvac_mode: object) -> dict[str, Any] | None:
        """The kept values when they were measured in ``hvac_mode``."""
        if self._kept is None or hvac_mode is None:
            return None
        return self._kept if self._kept["mode"] == str(hvac_mode) else None

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

    @property
    def wants_pulses(self) -> bool:
        """True while no values are kept: the pulses go on (three at most, six hours apart)."""
        return self._kept is None

    @property
    def in_progress(self) -> bool:
        return any(r["state"] == STATE_WAITING for r in self._readings)

    def wants_record(self, learning_stage: bool) -> bool:
        """True while the record is needed: before values are kept, during the learning stage or a reading."""
        return self._kept is None and (learning_stage or self.in_progress)

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
            result = await hass.async_add_executor_job(job_abort.run, self._abort, run_fit, job["snap"])
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
            result = run_fit(job["snap"])
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
                            end=r["next_end_s"] - snap["origin"])
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

    def _end_in_progress(self, reason: str, now: float | None = None) -> None:
        for r in self._readings:
            if r["state"] == STATE_WAITING:
                self._end(r, reason, now)

    def interrupt(self, now: float, reason: str) -> None:
        """The thermostat stops (off, window, shedding): the readings in progress end without result, and so does the
        reading of the event that was running."""
        self._end_in_progress(reason, now)
        self._interrupted_at = now
        self._record.stop(now)

    def restart(self, bridged: bool) -> None:
        """The restart decision: not bridged, the readings in progress end and the record is dropped."""
        if not bridged:
            self._end_in_progress("interrupted by restart")
            self._record.clear()

    def step(
        self,
        now: float,
        hvac_mode: object,
        calibrating: bool,
        response_lag: Any,
        phase_start: float | None,
    ) -> None:
        """Once per calculation: apply the result of the fit that ran, queue the next one that is due, trim the record
        and keep agreeing values; give the kept lag to the response-lag estimator when one is passed."""
        if calibrating:
            self._end_in_progress("calibration", now)
        if self._result is not None:
            index, result = self._result
            self._result = None
            reading = next((r for r in self._readings if r["index"] == index), None)
            if reading is not None and reading["state"] == STATE_WAITING:
                self._fit_done(reading, result, now)
        self._queue_due(now)
        self._trim(now, phase_start)
        if response_lag is not None:
            response_lag.give_lag(self.kept_lag_s(hvac_mode))

    def read_events(self, events: list[MeasureEvent], now: float) -> None:
        """The events the detector closed or dropped at ``now``: a closed one that a pulse or the hysteresis drove opens
        a reading, whose first fit is queued at once."""
        for event in events:
            if event.closed and event.source in (SOURCE_PULSE, SOURCE_BOOTSTRAP):
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
        self._readings.append(entry)
        self.version += 1
        rec = self._record
        if self._interrupted_at is not None and heat_start <= self._interrupted_at < now:
            entry["reason"] = "thermostat stopped"
        elif self._kept is not None:
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
        self._done.append({"index": reading["index"], "mode": reading["mode"], "a_per_min": result["a_per_min"],
                           "lag_s": result["lag_s"], "b_per_min": result["b_per_min"]})
        self._done.sort(key=lambda d: d["index"])
        for j in range(len(self._done)):
            for i in range(j):
                first, second = self._done[i], self._done[j]
                if first["mode"] != second["mode"] or reading["index"] not in (first["index"], second["index"]):
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
        self._kept = {
            "mode": first["mode"], "a_per_min": 0.5 * (first["a_per_min"] + second["a_per_min"]),
            "lag_s": 0.5 * (first["lag_s"] + second["lag_s"]),
            "b_per_min": 0.5 * (first["b_per_min"] + second["b_per_min"]), "pulses": [first["index"], second["index"]], "at_s": now,
        }
        self._end_in_progress("values kept", now)
        self.version += 1
        _LOGGER.info("event reading: values kept (a %.4f C/min, lag %.0f s)", self._kept["a_per_min"], self._kept["lag_s"])

    # -- persistence -----------------------------------------------------------------------------------------------
    def save(self) -> dict[str, Any] | None:
        """The kept values, or the complete readings and the readings in progress; the fit queued or running is not
        saved (a restored reading queues it again). None when there is nothing to save."""
        if self._kept is not None:
            return {"kept": {k: v for k, v in self._kept.items() if k != "at_s"},
                    "at_wall": monotonic_to_wall_ts(self._kept["at_s"])}
        waiting = [r for r in self._readings if r["state"] == STATE_WAITING]
        if not self._index:
            return None
        return {"index": self._index, "mode": self._mode, "done": [dict(d) for d in self._done],
                "readings": [self._save_reading(r) for r in waiting]}

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
        reading["result"] = reading["fits"][-1] if fits else None
        if _number(raw.get("target_s")) is not None:
            reading["target_s"] = float(raw["target_s"])
        return reading

    def load(self, saved: object) -> None:
        self.reset()
        if not isinstance(saved, dict):
            return
        kept = saved.get("kept")
        if isinstance(kept, dict):
            if (
                isinstance(kept.get("mode"), str) and _finite(kept.get("a_per_min")) and kept["a_per_min"] > 0.0
                and _finite(kept.get("lag_s")) and kept["lag_s"] > 0.0
            ):
                pulses = kept.get("pulses")
                b_kept = kept.get("b_per_min")
                self._kept = {
                    "mode": kept["mode"], "a_per_min": float(kept["a_per_min"]), "lag_s": float(kept["lag_s"]),
                    "b_per_min": float(b_kept) if _finite(b_kept) and b_kept >= 0.0 else None,
                    "pulses": list(pulses) if isinstance(pulses, list) else [], "at_s": wall_to_monotonic_ts(saved.get("at_wall")),
                }
            return
        index = _integer(saved.get("index"))
        if index is None or index < 0:
            return
        self._index = index
        self._mode = saved["mode"] if isinstance(saved.get("mode"), str) else None
        for raw in saved.get("done") if isinstance(saved.get("done"), list) else []:
            if (isinstance(raw, dict) and isinstance(raw.get("mode"), str) and _integer(raw.get("index")) is not None
                    and all(_finite(raw.get(k)) for k in ("a_per_min", "lag_s", "b_per_min"))):
                self._done.append({k: raw[k] for k in ("index", "mode", "a_per_min", "lag_s", "b_per_min")})
        for raw in saved.get("readings") if isinstance(saved.get("readings"), list) else []:
            reading = self._load_reading(raw)
            if reading is not None:
                self._readings.append(reading)
