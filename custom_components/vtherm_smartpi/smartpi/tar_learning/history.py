"""Bounded history of the signals the T_ar learning needs (pure Python).

Times are epoch seconds. The history keeps, over the last
TAR_HISTORY_RETENTION_S:
- the indoor temperature at each change of value;
- the heater on/off edges. A PWM heater's edges are rebuilt from the applied
  cycles (on from the cycle start for power x cycle). A valve's applied power
  is recorded as a level held until its next change (pw_t, pw_v); its heater
  starts are the cycle starts with a positive level and the rises of the
  level, its heater cuts the falls of the level to zero;
- the outdoor temperature, the setpoint, the SmartPI phase and the published
  parameters (b, reliable cool dead time, response lag) at each change;
- the invalid intervals.
Every list is capped; the zero-order-hold timelines keep their last sample
before the retention cutoff.

Data quality: samples enter only through record(), applied_cycle() and
applied_level(), where
explicit deterministic rules mark intervals invalid (``gaps``):
- no observation for more than TAR_GAP_S (data gap, downtime);
- an unavailable indoor or outdoor temperature, or a command that is not known;
- the downtime of a restart that is not bridged (see restart_bridge.py), from
  the last observation before it to the first one after it;
- a physically impossible jump of the temperature (the sample is dropped; a new
  level confirmed by TAR_SPIKE_CONFIRM samples is kept, with the jump invalid);
- the periods when SmartPI is not in control (reason given by the caller:
  window open, power shedding, safety mode, HVAC not heating, command overridden).
Invalid intervals are never filled in: they break nights, identification bins
and morning steps like a data gap.

Restarts: the heater state at the stop is saved with the history. Over a
bridged restart it is held until the first command after it (the actuator
keeps its last state) and the downtime is only a missing sample. Otherwise the
on period is closed at the last observation and the downtime is invalid. Every
restart leaves a marker (stop, resume, bridged) in ``restarts``.
"""
from __future__ import annotations

import bisect
from typing import Any

from .const import (
    TAR_GAP_S,
    TAR_SPIKE_CONFIRM,
    TAR_SPIKE_MIN_C,
    TAR_SPIKE_RATE_C_PER_H,
    TAR_HISTORY_MAX_EDGES,
    TAR_HISTORY_MAX_EVENTS,
    TAR_HISTORY_MAX_LEVELS,
    TAR_HISTORY_MAX_OUTDOOR,
    TAR_HISTORY_MAX_TEMPERATURES,
    TAR_HISTORY_RETENTION_S,
)

_TEMP_DIGITS = 3
_TIME_DIGITS = 1
_LEVEL_DIGITS = 4


def _cap(lst: list, n: int) -> None:
    if len(lst) > n:
        del lst[: len(lst) - n]


def is_temperature_jump(dv: float, dt: float) -> bool:
    """A change of dv (C) within dt (s) that is not physical for a room temperature."""
    dt = max(dt, 1.0)
    return dv > TAR_SPIKE_MIN_C and dv / dt * 3600.0 > TAR_SPIKE_RATE_C_PER_H


class TarHistory:
    """Signals of one room over the retention window."""

    def __init__(self, valve: bool = False) -> None:
        # Actuator of the recorded heater signal: PWM edges, or valve levels
        self.valve = bool(valve)
        self.pw_t: list[float] = []
        self.pw_v: list[float] = []
        self.temp_t: list[float] = []
        self.temp_v: list[float] = []
        self.ext_t: list[float] = []
        self.ext_v: list[float] = []
        self.on: list[float] = []
        self.off: list[float] = []
        self.setpoints: list[tuple[float, float]] = []
        self.phases: list[tuple[float, str]] = []
        self.params: list[tuple[float, float | None, float | None, float | None]] = []
        self.gaps: list[tuple[float, float]] = []
        self.first_t: float | None = None
        self.last_t: float | None = None
        # Data quality state
        self.invalid_since: float | None = None
        self.invalid_reason: str | None = None
        # Restart markers: (last observation before the stop, first one after it, bridged)
        self.restarts: list[tuple[float, float, bool]] = []
        # A restart waits for its first observation; its decision (True: bridged) once known
        self._restart_pending = False
        self._restart_bridged: bool | None = None
        # Heater state saved with the history (None: not saved) and the time of that save
        self._heater_saved = False
        self._saved_t: float | None = None
        self._spike: list[tuple[float, float]] = []
        # Heater state rebuilt from the applied cycles
        self._heater_on = False
        self._cycle_start: float | None = None
        self._off_due: float | None = None
        self._level = 0.0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _invalid(self, a: float, b: float) -> None:
        if b > a:
            self.gaps.append((round(a, _TIME_DIGITS), round(b, _TIME_DIGITS)))
            _cap(self.gaps, TAR_HISTORY_MAX_EVENTS)

    def _touch(self, t: float) -> None:
        bridged = None
        if self._restart_pending:
            if self._restart_bridged is None:
                self.resolve_restart(False, t)
            bridged = self._restart_bridged
            self._restart_pending = False
            self._restart_bridged = None
            self._saved_t = None
            if self.last_t is not None:
                self.restarts.append((round(self.last_t, _TIME_DIGITS), round(t, _TIME_DIGITS), bridged))
                _cap(self.restarts, TAR_HISTORY_MAX_EVENTS)
        if bridged is False and self.last_t is not None:
            # A restart that is not bridged: only the downtime itself is invalid.
            # The actuator state over it is unknown, but a margin around it would
            # mark valid observations invalid without adding any information.
            self._invalid(self.last_t, t)
        elif self.last_t is not None and t - self.last_t > TAR_GAP_S:
            self._invalid(self.last_t, t)
        if self.first_t is None:
            self.first_t = t
        if self.last_t is None or t > self.last_t:
            self.last_t = t

    def record(
        self,
        t: float,
        *,
        temp: float | None,
        ext: float | None,
        setpoint: float | None,
        phase: str | None,
        b: float | None,
        dtc: float | None,
        lag: float | None,
        excluded: str | None = None,
    ) -> float | None:
        """Record one observation; returns the previous setpoint when the setpoint changed.

        excluded: why SmartPI is not in control (None when it is).
        """
        if self.last_t is not None and t < self.last_t:
            return None
        self._advance(t)
        self._touch(t)
        reasons: list[str] = []
        if excluded:
            reasons.append(excluded)
        if temp is None:
            reasons.append("indoor temperature unavailable")
        elif not self._record_temperature(t, float(temp)):
            reasons.append("temperature jump")
        if ext is None:
            reasons.append("outdoor temperature unavailable")
        if reasons:
            if self.invalid_since is None:
                self.invalid_since = t
                self.invalid_reason = reasons[0]
        elif self.invalid_since is not None:
            self._invalid(self.invalid_since, t)
            self.invalid_since = None
            self.invalid_reason = None
        if ext is not None:
            v = round(float(ext), _TEMP_DIGITS)
            if not self.ext_v or self.ext_v[-1] != v:
                self.ext_t.append(round(t, _TIME_DIGITS))
                self.ext_v.append(v)
                _cap(self.ext_t, TAR_HISTORY_MAX_OUTDOOR)
                _cap(self.ext_v, TAR_HISTORY_MAX_OUTDOOR)
        prev_sp = None
        if setpoint is not None:
            sp = round(float(setpoint), _TEMP_DIGITS)
            if not self.setpoints or self.setpoints[-1][1] != sp:
                prev_sp = self.setpoints[-1][1] if self.setpoints else None
                self.setpoints.append((round(t, _TIME_DIGITS), sp))
                _cap(self.setpoints, TAR_HISTORY_MAX_EVENTS)
        if phase is not None and (not self.phases or self.phases[-1][1] != phase):
            self.phases.append((round(t, _TIME_DIGITS), phase))
            _cap(self.phases, TAR_HISTORY_MAX_EVENTS)
        p = (b, dtc, lag)
        if not self.params or self.params[-1][1:] != p:
            self.params.append((round(t, _TIME_DIGITS), *p))
            _cap(self.params, TAR_HISTORY_MAX_EVENTS)
        return prev_sp

    def _record_temperature(self, t: float, temp: float) -> bool:
        """Keep a valid temperature at each change of value; False for a rejected jump."""
        v = round(temp, _TEMP_DIGITS)
        jump = False
        if self.temp_v and self.temp_v[-1] != v:
            jump = is_temperature_jump(abs(v - self.temp_v[-1]), t - self.temp_t[-1])
        if jump:
            # Not physical: dropped, unless a new level is confirmed by consecutive samples.
            if self._spike and abs(self._spike[-1][1] - v) > TAR_SPIKE_MIN_C:
                self._spike = []
            self._spike.append((t, v))
            if len(self._spike) < TAR_SPIKE_CONFIRM:
                return False
        if self._spike:
            # the jump ends (spike) or its new level is confirmed: the jump stays invalid
            self._invalid(self._spike[0][0], t)
            self._spike = []
        if not self.temp_v or self.temp_v[-1] != v:
            self.temp_t.append(round(t, _TIME_DIGITS))
            self.temp_v.append(v)
            _cap(self.temp_t, TAR_HISTORY_MAX_TEMPERATURES)
            _cap(self.temp_v, TAR_HISTORY_MAX_TEMPERATURES)
        return True

    def invalid_intervals(self, now: float | None = None) -> list[tuple[float, float]]:
        """Closed invalid intervals, plus the one still open at now."""
        out = list(self.gaps)
        if self.invalid_since is not None:
            end = now if now is not None else (self.last_t if self.last_t is not None else self.invalid_since)
            out.append((self.invalid_since, max(end, self.invalid_since + 1.0)))
        return out

    def _edge(self, t: float, on: bool) -> None:
        t = round(t, _TIME_DIGITS)
        lst = self.on if on else self.off
        lst.append(t)
        _cap(lst, TAR_HISTORY_MAX_EDGES)
        self._heater_on = on

    def _advance(self, now: float) -> None:
        """Close the on period of the running cycle when its off time has passed."""
        if self._heater_on and self._off_due is not None and self._off_due < now:
            self._edge(self._off_due, False)
            self._off_due = None

    def applied_cycle(self, t: float, power: float, cycle_s: float, cycle_start: bool) -> None:
        """Rebuild the heater edges from an applied command (power 0..1)."""
        if self.last_t is not None and t < self.last_t - 1.0:
            return
        self._settle_restart(t)
        self._advance(t)
        power = min(max(float(power), 0.0), 1.0)
        if cycle_start:
            self._cycle_start = t
        start = self._cycle_start if self._cycle_start is not None else t
        off_at = start + power * cycle_s
        if power <= 0.0 or off_at <= t:
            if self._heater_on:
                self._edge(t, False)
            self._off_due = None
            return
        if not self._heater_on:
            self._edge(t, True)
        self._off_due = off_at if power < 1.0 else start + cycle_s + 1.0

    def applied_level(self, t: float, power: float, cycle_start: bool) -> None:
        """Record an applied valve command (heat-equivalent power 0..1), held until the next one."""
        if self.last_t is not None and t < self.last_t - 1.0:
            return
        self._settle_restart(t)
        level = round(min(max(float(power), 0.0), 1.0), _LEVEL_DIGITS)
        tr = round(t, _TIME_DIGITS)
        if level > 0.0 and (cycle_start or level > self._level):
            self.on.append(tr)
            _cap(self.on, TAR_HISTORY_MAX_EDGES)
        elif level <= 0.0 < self._level:
            self.off.append(tr)
            _cap(self.off, TAR_HISTORY_MAX_EDGES)
        if not self.pw_v or self.pw_v[-1] != level:
            self.pw_t.append(tr)
            self.pw_v.append(level)
            _cap(self.pw_t, TAR_HISTORY_MAX_LEVELS)
            _cap(self.pw_v, TAR_HISTORY_MAX_LEVELS)
        self._level = level

    def power_levels(self) -> tuple[list[float], list[float]] | None:
        """Valve power timeline (times, levels), None for a PWM heater."""
        return (list(self.pw_t), list(self.pw_v)) if self.valve else None

    def flush(self, now: float) -> None:
        """Close the on period of the running cycle when it ended before now."""
        self._advance(now)

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    @property
    def restart_pending(self) -> bool:
        """A restart waits for its decision (the history holds observations before it)."""
        return self._restart_pending and self._restart_bridged is None and self.last_t is not None

    @property
    def heater_saved(self) -> bool:
        """The heater state at the stop was saved with the history."""
        return self._heater_saved

    def resolve_restart(self, bridged: bool, t_resume: float) -> None:
        """Apply the restart decision to the heater state, before the first observation after it.

        Bridged: the heater state at the stop (time of the save) is held until the
        first command after the restart, without any edge. Otherwise the on period
        (or the valve level) is closed at the last observation.
        """
        if not self._restart_pending or self._restart_bridged is not None:
            return
        last_t = self.last_t
        if bridged and self._heater_saved and last_t is not None:
            self._restart_bridged = True
            # Only the stop itself (the save) is known: an on time planned to end
            # before it has ended, a heater on at the stop stays on.
            stop = min(max(self._saved_t if self._saved_t is not None else last_t, last_t), max(t_resume, last_t))
            self._advance(stop)
            if self._heater_on:
                self._off_due = None
            return
        self._restart_bridged = False
        if last_t is None:
            return
        t_cut = round(last_t, _TIME_DIGITS)
        if self.valve:
            if self.pw_v and self.pw_v[-1] > 0.0:
                t_cut = round(max(self.pw_t[-1], last_t), _TIME_DIGITS)
                self.pw_t.append(t_cut)
                self.pw_v.append(0.0)
                self.off.append(t_cut)
        elif len(self.on) > len(self.off):
            self.off.append(round(max(self.on[-1], last_t), _TIME_DIGITS))
        self._heater_on = False
        self._off_due = None
        self._cycle_start = None
        self._level = 0.0

    def _settle_restart(self, t: float) -> None:
        """A command before the first observation of an undecided restart: the restart is not bridged."""
        if self._restart_pending and self._restart_bridged is None:
            self.resolve_restart(False, t)

    def restarts_between(self, a: float, b: float) -> list[tuple[float, float, bool]]:
        """Restart markers whose stop or resume lies in [a, b), or that span it."""
        return [r for r in self.restarts if r[0] < b and r[1] >= a]

    def mark_interrupted(self, t: float) -> None:
        """Close the heater state when the observation stops (restart, HVAC off)."""
        self._advance(t)
        if self._heater_on:
            self._edge(min(t, self._off_due or t), False)
        self._off_due = None
        self._cycle_start = None

    # ------------------------------------------------------------------
    # Pruning and queries
    # ------------------------------------------------------------------
    def prune(self, now: float) -> None:
        cutoff = now - TAR_HISTORY_RETENTION_S
        i = bisect.bisect_left(self.temp_t, cutoff)
        del self.temp_t[:i]
        del self.temp_v[:i]
        i = max(0, bisect.bisect_left(self.ext_t, cutoff) - 1)
        del self.ext_t[:i]
        del self.ext_v[:i]
        del self.on[: bisect.bisect_left(self.on, cutoff)]
        del self.off[: bisect.bisect_left(self.off, cutoff)]
        i = max(0, bisect.bisect_left(self.pw_t, cutoff) - 1)
        del self.pw_t[:i]
        del self.pw_v[:i]
        for lst in (self.setpoints, self.phases, self.params):
            i = max(0, bisect.bisect_left([x[0] for x in lst], cutoff) - 1)
            del lst[:i]
        self.gaps = [g for g in self.gaps if g[1] >= cutoff]
        self.restarts = [r for r in self.restarts if r[1] >= cutoff]
        if self.first_t is not None and self.first_t < cutoff:
            self.first_t = cutoff

    def gap_overlaps(self, a: float, b: float) -> bool:
        """True when [a, b) holds an invalid interval or lies outside the recorded span."""
        if self.first_t is None or self.last_t is None or a < self.first_t or b > self.last_t + TAR_GAP_S:
            return True
        return any(g0 < b and g1 > a for g0, g1 in self.invalid_intervals())

    def setpoint_at(self, t: float) -> float | None:
        v = None
        for ts, sp in self.setpoints:
            if ts <= t:
                v = sp
            else:
                break
        return v

    def is_empty(self) -> bool:
        return not self.temp_t

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self, saved_t: float | None = None) -> dict[str, Any]:
        """Plain data of the history; saved_t: time of the save (the heater state at a stop)."""
        if self._restart_pending and self._saved_t is not None:
            # Saved again before the first observation: the stop is still the first one.
            saved_t = self._saved_t
        return {
            "temp_t": self.temp_t,
            "temp_v": self.temp_v,
            "ext_t": self.ext_t,
            "ext_v": self.ext_v,
            "on": self.on,
            "off": self.off,
            "valve": self.valve,
            "pw_t": self.pw_t,
            "pw_v": self.pw_v,
            "setpoints": [list(x) for x in self.setpoints],
            "phases": [list(x) for x in self.phases],
            "params": [list(x) for x in self.params],
            "gaps": [list(x) for x in self.gaps],
            "first_t": self.first_t,
            "last_t": self.last_t,
            "invalid_since": self.invalid_since,
            "invalid_reason": self.invalid_reason,
            "restarts": [list(x) for x in self.restarts],
            "heater": {
                "on": self._heater_on,
                "cycle_start": self._cycle_start,
                "off_due": self._off_due,
                "level": self._level,
                "saved_t": saved_t,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TarHistory":
        h = cls()
        if not isinstance(data, dict):
            return h
        try:
            h.temp_t = [float(x) for x in data.get("temp_t", [])]
            h.temp_v = [float(x) for x in data.get("temp_v", [])]
            h.ext_t = [float(x) for x in data.get("ext_t", [])]
            h.ext_v = [float(x) for x in data.get("ext_v", [])]
            h.on = [float(x) for x in data.get("on", [])]
            h.off = [float(x) for x in data.get("off", [])]
            h.valve = bool(data.get("valve", False))
            h.pw_t = [float(x) for x in data.get("pw_t", [])]
            h.pw_v = [float(x) for x in data.get("pw_v", [])]
            h.setpoints = [(float(a), float(b)) for a, b in data.get("setpoints", [])]
            h.phases = [(float(a), str(b)) for a, b in data.get("phases", [])]
            h.params = [(float(x[0]), x[1], x[2], x[3]) for x in data.get("params", [])]
            h.gaps = [(float(a), float(b)) for a, b in data.get("gaps", [])]
            h.first_t = data.get("first_t")
            h.last_t = data.get("last_t")
            if data.get("invalid_since") is not None and h.last_t is not None:
                since = float(data["invalid_since"])
                h.gaps.append((since, max(float(h.last_t), since + 1.0)))
            h.restarts = [(float(r[0]), float(r[1]), bool(r[2])) for r in data.get("restarts", [])]
            # The restart is decided before the first observation (resolve_restart).
            h._restart_pending = h.last_t is not None
            if len(h.temp_t) != len(h.temp_v) or len(h.ext_t) != len(h.ext_v) or len(h.pw_t) != len(h.pw_v):
                return cls()
            heater = data.get("heater")
            if isinstance(heater, dict):
                h._heater_on = bool(heater.get("on"))
                for key, attr in (("cycle_start", "_cycle_start"), ("off_due", "_off_due"), ("saved_t", "_saved_t")):
                    v = heater.get(key)
                    setattr(h, attr, None if v is None else float(v))
                h._level = float(heater.get("level") or 0.0)
                h._heater_saved = True
        except (TypeError, ValueError, IndexError):
            return cls()
        return h
