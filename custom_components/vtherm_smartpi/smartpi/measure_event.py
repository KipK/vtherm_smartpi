"""Detector of the measurement events: a heating phase and the peak that follows its cut.

The detector watches the power applied to the emitter and the room temperature samples of the dead-time estimator.
An event is a rest, then a phase of power above ``_POWER_ON``, then a cut. Cuts shorter than
``HEATING_BALANCE_PHASE_MIN_OFF_S`` belong to the same phase. After the cut the peak of the temperature is
searched with ``peak_search.PeakSearch`` (a maximum in HEAT, a minimum in COOL). While the phase runs the
detector accumulates what its consumers need: the baseline fitted over the rest, the area between the temperature
and that baseline, the temperature at the cut and the energy applied.

Each observation returns the events that closed or were dropped during it:

- a closed event (``kind == "closed"``) holds the phase, the peak search and the outcome of the search
  (``PEAK_CONFIRMED``, ``PEAK_CUT_SHORT``, ``PEAK_NONE``). ``reason`` tells why the event gives no measure
  (empty for a usable one);
- a dropped event (``kind == "dropped"``) is a phase lost to a mode change, an observation gap or a restart.

Every event carries the ``mode`` it was observed in, the ``context`` given at the start of its phase (the learning
phase of the caller) and a ``source`` read from a callable of the owner at the start of the phase: ``pulse``
(a measurement pulse drives the command), ``bootstrap`` (the hysteresis drives it), ``calibration`` or
``regulation``. The detector does not use the source; each consumer picks the events it reads.

The phase in progress is saved with the rest it follows and resumed after a restart that the restart bridge
bridges (``resolve_restart``).

The detector also feeds the record of the measurement events (``MeasureRecord``) and hands the events to the reader
that owns the record (``smartpi/event_reading.py``) when one is attached. The record holds the power as the emitter
state: for a switch, the on and off edges of the cycles the scheduler applied (``MeasureRecord.note_cycle``, from the
applied-command receipts); without them, or for a valve, the power the detector observed over each interval.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Tuple

from .const import (
    HEATING_BALANCE_BASELINE_SPAN_S,
    HEATING_BALANCE_MIN_REST_S,
    HEATING_BALANCE_PHASE_MIN_OFF_S,
)
from .peak_search import PEAK_CONFIRMED, PEAK_NONE, PeakSearch
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode

SOURCE_PULSE = "pulse"
SOURCE_BOOTSTRAP = "bootstrap"
SOURCE_CALIBRATION = "calibration"
SOURCE_REGULATION = "regulation"

MODE_HEAT = str(VThermHvacMode_HEAT)
MODE_COOL = str(VThermHvacMode_COOL)

EVENT_CLOSED = "closed"
EVENT_DROPPED = "dropped"

_POWER_ON = 0.01
# Two observed intervals are contiguous within this tolerance (s).
_CONTIGUITY_TOL_S = 1.0
# Half width of the window around the cut averaged for the temperature at the cut (s).
_CUT_WINDOW_S = 150.0
# Smallest number of samples of the baseline fit.
_BASELINE_MIN_SAMPLES = 4

_HEATING = "heating"
_CUT = "cut"

# No observation yet (a restored context is kept for the first observation).
_UNSET = "unset"

# Saved phase: field -> kind. "time" fields are saved in wall-clock time. The last seven are the bins of the peak
# search (``best_area`` is its mark).
_PHASE_FIELDS = {
    "stage": "str",
    "context": "str",
    "source": "str?",
    "reason": "str?",
    "t_start": "time",
    "rest_s": "num?",
    "base_value": "num",
    "base_slope": "num",
    "u_max": "num",
    "on_s": "num",
    "area": "num",
    "last_t": "time",
    "last_dev": "num",
    "t_cut": "time?",
    "area_cut": "num?",
    "cut_temp": "num?",
    "bin_k": "int?",
    "bin_sum": "num",
    "bin_n": "int",
    "best_k": "int?",
    "best_mean": "num?",
    "best_area": "num?",
    "last_k": "int?",
}
# Saved field of the peak search -> field of ``PeakSearch.fields``.
_SEARCH_FIELDS = {
    "bin_k": "bin_k", "bin_sum": "bin_sum", "bin_n": "bin_n", "best_k": "best_k",
    "best_mean": "best_mean", "best_area": "best_mark", "last_k": "last_k",
}


@dataclass
class MeasureEvent:
    """A phase that closed or was dropped (see the module documentation)."""

    kind: str
    mode: str
    reason: str = ""
    outcome: str = PEAK_NONE
    phase: dict | None = None

    @property
    def closed(self) -> bool:
        return self.kind == EVENT_CLOSED

    @property
    def source(self) -> str:
        return SOURCE_REGULATION if self.phase is None else self.phase["source"]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def samples_after(
    history: Iterable[Tuple[float, float]], t_from: float, t_to: float
) -> list[Tuple[float, float]]:
    """Samples with t_from < t <= t_to, oldest first, reading the history from its end."""
    try:
        reverse = reversed(history)  # type: ignore[arg-type]
    except TypeError:
        reverse = reversed(list(history))
    out: list[Tuple[float, float]] = []
    for t, v in reverse:
        if t <= t_from:
            break
        if t <= t_to:
            out.append((t, v))
    out.reverse()
    return out


def _baseline(
    history: Iterable[Tuple[float, float]], t_from: float, t_start: float
) -> Tuple[float, float] | None:
    """Least-squares line over [t_from, t_start]: (value at t_start, slope per s)."""
    points = samples_after(history, t_from - 1e-9, t_start)
    if len(points) < _BASELINE_MIN_SAMPLES:
        return None
    n = len(points)
    tm = sum(t for t, _ in points) / n
    vm = sum(v for _, v in points) / n
    sxx = sum((t - tm) ** 2 for t, _ in points)
    if sxx <= 0.0:
        return None
    slope = sum((t - tm) * (v - vm) for t, v in points) / sxx
    return vm + slope * (t_start - tm), slope


RECORD_MAX_POINTS = 6000          # points per channel
_RECORD_STEP = 0.001              # smallest change of a value that is recorded
_CYCLES_KEPT = 8                  # applied switch cycles kept for the edges
_CYCLE_TOL_S = 2.0                # two applied cycles are contiguous within this gap (s)
_CHANNELS = ("power", "room", "ext")


class MeasureRecord:
    """Record of what the readings of the events need: the power applied to the emitter, the raw room temperature and the
    outdoor temperature, each as the list of its change points (a value holds until the next point).

    Times are monotonic seconds. The record is fed by the detector at each observation while ``enabled``; the reader
    that owns it trims it from the front (``trim``) and cuts a snapshot for a fit (``snapshot``). ``t0`` is the
    beginning of the record: the held value of each channel at ``t0`` is its last point at or before it.

    The power channel holds the emitter state. For a switch, the owner notes each applied cycle (``note_cycle``) and an
    observation whose interval the noted cycles cover entirely records their on (1) and off (0) edges; any other
    observation records the power it was given, held over the interval. The noted cycles are not part of the record
    (``clear``, ``save`` and ``load`` leave them); a stop forgets them.
    """

    def __init__(self) -> None:
        self.enabled = False
        self._cycles: list[tuple[float, float, float]] = []   # (start, on time, duration) of the applied switch cycles
        self.clear()

    def clear(self) -> None:
        """Forget every point."""
        self.t0: float | None = None
        self._ch: dict[str, tuple[list[float], list[float]]] = {name: ([], []) for name in _CHANNELS}
        self._seen: float | None = None     # time of the last room sample of the history taken

    @property
    def rows(self) -> int:
        """Points of the longest channel."""
        return max(len(ts) for ts, _ in self._ch.values())

    def feed(
        self, now: float, dt_s: float, u: float, ext: float | None, history: Iterable[Tuple[float, float]]
    ) -> None:
        """One observation: the power ``u`` applied over [now - dt_s, now], the outdoor temperature and the room
        samples of ``history`` that are new."""
        if not self.enabled:
            return
        t_step = now - dt_s
        if self.t0 is None:
            self.t0 = t_step
        for t, v in self._power_points(t_step, now, u):
            self._put("power", t, v)
        if ext is not None:
            self._put("ext", now, ext)
        if self._seen is None:
            for t, v in reversed(list(history)):
                if t <= t_step:
                    self._put("room", t_step, v)
                    break
            self._seen = t_step
        for t, v in samples_after(history, self._seen, now):
            self._put("room", t, v)
            self._seen = t

    def note_cycle(self, start: float, on_s: float, duration_s: float) -> None:
        """A switch cycle the scheduler applied: from ``start`` (monotonic s) the emitter is on for ``on_s`` seconds, then
        off until the end of the cycle (``duration_s``)."""
        if self._cycles and start <= self._cycles[-1][0]:
            self._cycles.pop()
        self._cycles.append((start, on_s, duration_s))
        del self._cycles[:-_CYCLES_KEPT]

    def _heater_on(self, t: float) -> bool | None:
        """Emitter state at ``t`` from the noted cycles: True (on), False (off), None when no noted cycle covers ``t``."""
        for start, on_s, duration_s in reversed(self._cycles):
            if start <= t:
                if t >= start + duration_s + _CYCLE_TOL_S:
                    return None
                return on_s >= duration_s - 1e-6 or t < start + on_s
        return None

    def _power_points(self, t_step: float, now: float, u: float) -> list[tuple[float, float]]:
        """Change points of the power over [t_step, now]: the on and off edges of the noted cycles when they cover the
        whole interval, else the constant ``u``."""
        times = {t_step}
        for start, on_s, duration_s in self._cycles:
            times.update(t for t in (start, start + on_s, start + duration_s + _CYCLE_TOL_S) if t_step < t < now)
        points: list[tuple[float, float]] = []
        for t in sorted(times):
            on = self._heater_on(t)
            if on is None:
                return [(t_step, u)]
            points.append((t, 1.0 if on else 0.0))
        return points

    def _put(self, name: str, t: float, v: float) -> None:
        ts, vs = self._ch[name]
        if ts and abs(v - vs[-1]) <= _RECORD_STEP:
            return
        if ts and t <= ts[-1]:
            vs[-1] = v          # the same instant: the later value wins
            return
        ts.append(t)
        vs.append(v)

    def stop(self, now: float) -> None:
        """The emitter stops being driven (off, window, shedding): no power from ``now`` on."""
        self._cycles.clear()
        if self.enabled and self.t0 is not None:
            self._put("power", now, 0.0)

    def trim(self, front: float) -> None:
        """Drop what lies before ``front``, keeping each channel's held value there."""
        if self.t0 is None or front <= self.t0:
            return
        self.t0 = front
        for ts, vs in self._ch.values():
            i = bisect_right(ts, front) - 1
            if i > 0:
                del ts[:i], vs[:i]

    def cap_front(self, max_points: int) -> float | None:
        """The front that leaves at most ``max_points`` points in every channel, None when none is over."""
        fronts = [ts[len(ts) - max_points + 1] for ts, _ in self._ch.values() if len(ts) > max_points]
        return max(fronts) if fronts else None

    def snapshot(self, t_end: float) -> dict[str, Any]:
        """The points up to ``t_end`` as plain data, seconds from ``t0``; the power ends with a point at ``t_end``."""
        out: dict[str, Any] = {"origin": self.t0}
        for name, (ts, vs) in self._ch.items():
            i0, i1 = max(bisect_right(ts, self.t0) - 1, 0), bisect_right(ts, t_end)
            out[name] = [[max(t, self.t0) - self.t0, v] for t, v in zip(ts[i0:i1], vs[i0:i1])]
        if out["power"]:
            out["power"].append([t_end - self.t0, out["power"][-1][1]])
        return out

    def save(self) -> dict | None:
        """Times as whole seconds (rounded down, as the origin is) from a wall-clock origin, values to 0.001; None when empty.

        Rounding every time the same way keeps the order of the points against the origin: the held value at the front
        stays at or before it.
        """
        if self.t0 is None or not any(ts for ts, _ in self._ch.values()):
            return None
        origin = int(monotonic_to_wall_ts(self.t0))
        saved: dict[str, Any] = {"origin": origin}
        for name, (ts, vs) in self._ch.items():
            saved[name] = [[int(monotonic_to_wall_ts(t)) - origin, round(v, 3)] for t, v in zip(ts, vs)]
        saved["seen"] = None if self._seen is None else int(monotonic_to_wall_ts(self._seen)) - origin
        return saved

    def load(self, saved: object) -> None:
        """Restore what ``save`` wrote; anything malformed leaves the record empty."""
        self.clear()
        if not isinstance(saved, dict) or _integer(saved.get("origin")) is None:
            return
        origin = saved["origin"]
        channels: dict[str, tuple[list[float], list[float]]] = {}
        for name in _CHANNELS:
            points = saved.get(name)
            if not isinstance(points, list):
                return
            ts: list[float] = []
            vs: list[float] = []
            for point in points:
                if not isinstance(point, list) or len(point) != 2 or _integer(point[0]) is None or _number(point[1]) is None:
                    return
                t = wall_to_monotonic_ts(origin + point[0])
                if t is None or (ts and t < ts[-1]):
                    return
                ts.append(t)
                vs.append(float(point[1]))
            channels[name] = (ts, vs)
        self._ch = channels
        self.t0 = wall_to_monotonic_ts(origin)
        seen = saved.get("seen")
        self._seen = None if _integer(seen) is None else wall_to_monotonic_ts(origin + seen)



class MeasureEventDetector:
    """Detect the heating (or cooling) phases and their peaks from the applied power and the room temperature."""

    def __init__(self, name: str, source: Callable[[], str] | None = None) -> None:
        self._name = name
        # Tells the source of an event at the start of its phase (None: regulation).
        self.source = source
        # The record fed at each observation and the reader of the events, attached by their owner (None: none).
        self.record: MeasureRecord | None = None
        self.reader: Any = None
        self._clear_runtime()

    def _clear_runtime(self) -> None:
        self._last_now: float | None = None
        # Start of the current rest (monotonic s), None when unknown.
        self._rest_start: float | None = None
        # Phase in progress (see _PHASE_FIELDS; the peak search is under "search"), None while resting.
        self._phase: dict | None = None
        # Context saved before a restart, applied on the first observation.
        self._restore: dict | None = None
        # Mode of the phase and the rest in progress: MODE_HEAT, MODE_COOL, None (no tracked mode) or _UNSET.
        self._mode: str | None = _UNSET

    def clear(self) -> None:
        """Forget the phase in progress and the saved context."""
        self._clear_runtime()

    @property
    def phase_start(self) -> float | None:
        """Start (monotonic s) of the phase in progress, None without one."""
        return None if self._phase is None else self._phase["t_start"]

    def phase_stage(self, mode: str) -> str | None:
        """Stage ("heating" or "cut") of the phase in progress in ``mode``, None without one."""
        if self._phase is None or self._mode != mode:
            return None
        return self._phase["stage"]

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        now: float,
        dt_s: float,
        u: float,
        hvac_mode: "VThermHvacMode | None",
        tin_history: Iterable[Tuple[float, float]],
        context: str,
        ext: float | None = None,
    ) -> list[MeasureEvent]:
        """Observe the power applied over [now - dt_s, now].

        tin_history holds the (monotonic time, temperature) samples of the dead time estimator; context names
        the learning phase of the caller ("bootstrap", "calibration" or "regulation"); ext is the outdoor
        temperature. Returns the events closed or dropped by this observation, which the reader gets too.
        """
        events = self._observe(now, dt_s, u, hvac_mode, tin_history, context, ext)
        if self.reader is not None:
            self.reader.read_events(events, now)
        return events

    def _observe(
        self,
        now: float,
        dt_s: float,
        u: float,
        hvac_mode: "VThermHvacMode | None",
        tin_history: Iterable[Tuple[float, float]],
        context: str,
        ext: float | None,
    ) -> list[MeasureEvent]:
        events: list[MeasureEvent] = []
        mode = None
        if hvac_mode is not None and str(hvac_mode) in (MODE_HEAT, MODE_COOL):
            mode = str(hvac_mode)
        if mode is None:
            self._restore = None
            self._drop_phase(events, "heating mode left")
            self._last_now = None
            self._rest_start = None
            self._mode = None
            return events
        if self._restore is not None and self._restore["mode"] != mode:
            self._restore = None
        if self._mode not in (_UNSET, mode):
            self._drop_phase(events, "heating mode left")
            self._last_now = None
            self._rest_start = None
        self._mode = mode
        if dt_s <= 0.0:
            return events
        if self._last_now is not None and now <= self._last_now:
            return events
        t_step = now - dt_s
        if self._restore is not None:
            # No restart decision: the stop is not bridged.
            events.extend(self.resolve_restart(False))
        if self._last_now is not None and t_step > self._last_now + _CONTIGUITY_TOL_S:
            self._drop_phase(events, "observation gap")
            self._rest_start = None
        self._last_now = now
        if self.record is not None:
            self.record.feed(now, dt_s, u, ext, tin_history)
        on = u > _POWER_ON
        history = tin_history

        phase = self._phase
        if phase is not None and phase["stage"] == _HEATING and not on:
            phase["stage"] = _CUT
            phase["t_cut"] = t_step
            phase["area_cut"] = phase["area"]
            phase["search"] = PeakSearch(t_step, self._sign())
        if phase is not None and phase["stage"] == _CUT:
            self._advance_cut(phase, now, dt_s, t_step, u, on, history, events)
        elif phase is not None:
            self._advance_heating(phase, now, dt_s, u, history)
        if self._phase is None:
            if on:
                self._start_phase(t_step, now, dt_s, u, history, context)
            elif self._rest_start is None:
                self._rest_start = t_step
        return events

    def _sign(self) -> float:
        return -1.0 if self._mode == MODE_COOL else 1.0

    def _advance_heating(
        self, phase: dict, now: float, dt_s: float, u: float, history: Iterable[Tuple[float, float]]
    ) -> None:
        phase["on_s"] += u * dt_s
        phase["u_max"] = max(phase["u_max"], u)
        self._feed(phase, samples_after(history, phase["last_t"], now))

    def _start_phase(
        self,
        t_start: float,
        now: float,
        dt_s: float,
        u: float,
        history: Iterable[Tuple[float, float]],
        context: str,
    ) -> None:
        rest_s = None if self._rest_start is None else t_start - self._rest_start
        reason: str | None = None
        base = None
        if rest_s is None or rest_s < HEATING_BALANCE_MIN_REST_S:
            reason = "rest before heating too short"
        else:
            span = min(HEATING_BALANCE_BASELINE_SPAN_S, rest_s)
            base = _baseline(history, t_start - span, t_start)
            if base is None:
                reason = "baseline samples insufficient"
        self._rest_start = None
        self._phase = {
            "stage": _HEATING,
            "context": context,
            "source": self.source() if self.source is not None else SOURCE_REGULATION,
            "reason": reason,
            "t_start": t_start,
            "rest_s": rest_s,
            "base_value": 0.0 if base is None else base[0],
            "base_slope": 0.0 if base is None else base[1],
            "u_max": u,
            "on_s": 0.0,
            "area": 0.0,
            "last_t": t_start,
            "last_dev": 0.0,
            "t_cut": None,
            "area_cut": None,
            "cut_temp": None,
            "search": None,
        }
        self._advance_heating(self._phase, now, dt_s, u, history)

    def _feed(self, phase: dict, samples: list[Tuple[float, float]]) -> bool:
        """Accumulate the area under the baseline and, after the cut, the peak bins.

        Returns True once the peak is confirmed (the remaining samples are not used).
        """
        search = phase["search"]
        for t, v in samples:
            if search is not None and search.feed(t, v, phase["area"]):
                return True
            dev = v - (phase["base_value"] + phase["base_slope"] * (t - phase["t_start"]))
            phase["area"] += 0.5 * (dev + phase["last_dev"]) * (t - phase["last_t"]) / 60.0
            phase["last_t"], phase["last_dev"] = t, dev
        return False

    def _advance_cut(
        self,
        phase: dict,
        now: float,
        dt_s: float,
        t_step: float,
        u: float,
        on: bool,
        history: Iterable[Tuple[float, float]],
        events: list[MeasureEvent],
    ) -> None:
        t_cut = phase["t_cut"]
        search = phase["search"]
        if on and t_step - t_cut < HEATING_BALANCE_PHASE_MIN_OFF_S:
            # The cut was short: the same heating phase continues.
            phase["stage"] = _HEATING
            for key in ("t_cut", "area_cut", "cut_temp", "search"):
                phase[key] = None
            self._advance_heating(phase, now, dt_s, u, history)
            return
        if phase["cut_temp"] is None and now >= t_cut + _CUT_WINDOW_S:
            phase["cut_temp"] = self._temperature_at(history, t_cut)
        limit = t_step if on else now
        if self._feed(phase, samples_after(history, phase["last_t"], limit)):
            self._end_phase(phase, PEAK_CONFIRMED, "", events)
        elif on:
            self._end_phase(phase, search.cut_short(phase["area"]), "still rising at the next heating", events)
        elif search.expired(now):
            self._end_phase(phase, PEAK_NONE, "no peak within the wait", events)

    @staticmethod
    def _temperature_at(history: Iterable[Tuple[float, float]], t_cut: float) -> float | None:
        """Mean of the samples within the window around t_cut, else the last one before it."""
        near = [
            v for _, v in samples_after(history, t_cut - _CUT_WINDOW_S - 1e-9, t_cut + _CUT_WINDOW_S)
        ]
        if near:
            return sum(near) / len(near)
        before = [v for _, v in samples_after(history, t_cut - 5 * 3600.0, t_cut)]
        return before[-1] if before else None

    # ------------------------------------------------------------------
    # End of a phase
    # ------------------------------------------------------------------

    def _drop_phase(self, events: list[MeasureEvent], reason: str) -> None:
        if self._phase is not None:
            events.append(MeasureEvent(EVENT_DROPPED, self._mode, reason, PEAK_NONE, self._phase))
            self._phase = None

    def _end_phase(self, phase: dict, outcome: str, no_peak_reason: str, events: list[MeasureEvent]) -> None:
        """End the phase after its cut."""
        self._phase = None
        self._rest_start = phase["t_cut"]
        if phase["reason"] is not None:
            reason = phase["reason"]
        elif outcome == PEAK_NONE:
            reason = no_peak_reason
        else:
            reason = ""
        events.append(MeasureEvent(EVENT_CLOSED, self._mode, reason, outcome, phase))

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------

    def resolve_restart(self, bridged: bool) -> list[MeasureEvent]:
        """Apply the restart decision (restart_bridge.py) to the saved phase.

        Bridged: the phase resumes, and the first observation, which spans the
        stop, keeps it contiguous. Otherwise the phase is dropped and the start
        of the rest is unknown. Returns the dropped event, if any.
        """
        if self.reader is not None:
            self.reader.restart(bridged)
        restore = self._restore
        if restore is None:
            return []
        self._restore = None
        if bridged:
            self._phase = restore["phase"]
            self._rest_start = restore["rest_start"]
            self._last_now = None
            self._mode = restore["mode"]
            return []
        if restore["phase"] is not None:
            return [MeasureEvent(EVENT_DROPPED, restore["mode"], "interrupted by restart", PEAK_NONE,
                                 restore["phase"])]
        return []

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def restart_context(self) -> dict | None:
        """Return the phase or the rest to resume, in monotonic time, with its mode."""
        if self._restore is not None:
            # Not yet applied: no observation since the previous restart.
            return self._restore
        if self._phase is None and self._rest_start is None:
            return None
        return {"phase": self._phase, "rest_start": self._rest_start, "mode": self._mode}

    @staticmethod
    def save_context(context: dict) -> dict:
        """The saved form of a restart context (wall-clock times)."""
        phase = context["phase"]
        saved = None
        if phase is not None:
            search = phase["search"]
            fields = search.fields() if search is not None else PeakSearch(0.0).fields()
            saved = {}
            for key, kind in _PHASE_FIELDS.items():
                if key in _SEARCH_FIELDS:
                    saved[key] = fields[_SEARCH_FIELDS[key]]
                elif kind.startswith("time"):
                    saved[key] = monotonic_to_wall_ts(phase[key])
                else:
                    saved[key] = phase[key]
        return {"rest_start_wall": monotonic_to_wall_ts(context["rest_start"]), "phase": saved}

    @staticmethod
    def _load_phase(value: object) -> dict | None:
        """Validate a saved phase and convert it to monotonic time."""
        if not isinstance(value, dict):
            return None
        phase: dict = {}
        for key, kind in _PHASE_FIELDS.items():
            raw = value.get(key)
            optional = kind.endswith("?")
            base = kind.rstrip("?")
            if raw is None:
                if not optional:
                    return None
                phase[key] = None
                continue
            if base == "time":
                parsed = wall_to_monotonic_ts(raw)
            elif base == "num":
                parsed = _number(raw)
            elif base == "int":
                parsed = _integer(raw)
            elif base == "bool":
                parsed = raw if isinstance(raw, bool) else None
            else:
                parsed = raw if isinstance(raw, str) else None
            if parsed is None:
                return None
            phase[key] = parsed
        if phase["stage"] not in (_HEATING, _CUT) or phase["u_max"] <= _POWER_ON or phase["bin_n"] < 0:
            return None
        if phase["stage"] == _CUT and (
            phase["t_cut"] is None or phase["area_cut"] is None
            or (phase["bin_k"] is None) != (phase["bin_n"] == 0)
        ):
            return None
        if phase["best_k"] is not None and (
            phase["best_mean"] is None or phase["best_area"] is None or phase["last_k"] is None
        ):
            return None
        if phase["source"] is None:
            phase["source"] = SOURCE_REGULATION
        return phase

    def load_context(self, restart: object, mode: str = MODE_HEAT) -> None:
        """Keep a saved context (``save_context``) pending for the first observation.

        The phase is resumed or dropped by resolve_restart.
        """
        if not isinstance(restart, dict):
            return
        phase = restart.get("phase")
        loaded = self._load_phase(phase) if phase is not None else None
        if phase is not None and loaded is None:
            return
        rest_wall = restart.get("rest_start_wall")
        rest_start = wall_to_monotonic_ts(rest_wall) if rest_wall is not None else None
        if loaded is None and rest_start is None:
            return
        if loaded is not None:
            if loaded["stage"] == _CUT:
                loaded["search"] = PeakSearch.from_fields(
                    loaded["t_cut"], -1.0 if mode == MODE_COOL else 1.0,
                    {field: loaded[key] for key, field in _SEARCH_FIELDS.items()},
                )
            else:
                loaded["search"] = None
            for key in _SEARCH_FIELDS:
                del loaded[key]
        self._restore = {"phase": loaded, "rest_start": rest_start, "mode": mode}
