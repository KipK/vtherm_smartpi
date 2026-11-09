"""Restart bridge: one decision on whether a stop of SmartPI can be bridged (pure Python).

At an integration reload or a Home Assistant restart, Versatile Thermostat sends
no command to the heater at the stop, and none at the start in HEAT before its
first cycle: over a short stop the heater keeps its last commanded state. Such a
stop is then only a gap of a few samples in the observations, and the work in
progress (heater state of the learning histories, measurements, learning
windows, integral guard) can go on as if there had been no restart.

``decide`` compares what was saved before the stop (``RestartBefore``) with what
is seen at the first tick after it (``RestartAfter``) and returns one
``RestartDecision``. A stop is bridgeable when all of these hold:

- the downtime, from the oldest last tick of the saved stores to the first tick,
  is at most RESTART_BRIDGE_MAX_GAP_S (wall clock); a store written long before
  the stop (lost debounced write) makes the downtime long;
- the HVAC mode is the same before and after, HEAT or COOL, with the same
  cycle configuration;
- the actuator state was saved: the committed power, and in HEAT also the
  heater state of every learning store that holds a history;
- no exclusion is active before or after (window, power shedding, safety mode,
  command overridden or unknown);
- the first indoor temperature passes the temperature jump rule of the
  learning history against the last one;
- the wall clock did not jump (no negative downtime, no tick in the future).

Versatile Thermostat behaves the same way at a stop and at a start in HEAT and
in COOL. The learning histories (T_ar, room model) are HEAT-only: they apply the
bridge only to a restart in HEAT (``RestartDecision.heat_bridged``).

Every component consults the decision; none decides on its own. Otherwise the
components keep their conservative restart handling.

``RestartBridge`` keeps the context of the last tick for the next save and the
decision of the current session (diagnostics and later rules).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .const import RESTART_BRIDGE_MAX_GAP_S
from .tar_learning.history import is_temperature_jump

_HEAT = str(VThermHvacMode_HEAT)
_ACTIVE_MODES = (_HEAT, str(VThermHvacMode_COOL))

# Why a stop is not bridged (plain words, published as such)
REASON_NO_SNAPSHOT = "no saved state of the last tick"
REASON_CLOCK = "clock jump"
REASON_LONG = "stop too long"
REASON_HEATER = "heater state not saved"
REASON_NOT_ACTIVE_BEFORE = "not heating or cooling before the stop"
REASON_NOT_ACTIVE_AFTER = "not heating or cooling after the restart"
REASON_MODE_CHANGED = "HVAC mode changed across the stop"
REASON_CYCLE = "cycle settings changed"
REASON_TEMPERATURE_UNKNOWN = "indoor temperature unknown"
REASON_TEMPERATURE_JUMP = "temperature jump across the stop"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class RestartBefore:
    """What the saved stores tell about the last tick before the stop (wall-clock times)."""

    last_ticks: tuple[float, ...]
    # Heater state saved with every store (HEAT), committed power saved (any mode)
    heater_known: bool
    hvac_mode: str | None
    cycle: tuple | None
    excluded: str | None
    temp: float | None
    temp_since: float | None
    committed_known: bool = True


@dataclass(frozen=True)
class RestartAfter:
    """What is seen at the first tick after the restart."""

    now: float
    hvac_mode: str | None
    cycle: tuple | None
    excluded: str | None
    temp: float | None


@dataclass(frozen=True)
class RestartDecision:
    """The decision on one restart, shared by every component."""

    bridged: bool
    t_stop: float | None
    t_resume: float
    downtime_s: float | None
    reason: str | None
    # HVAC mode on both sides of a bridged restart, None otherwise
    hvac_mode: str | None = None

    @property
    def heat_bridged(self) -> bool:
        """Bridged in HEAT: the HEAT-only learning histories hold their state over the stop."""
        return self.bridged and self.hvac_mode == _HEAT

    def public(self) -> dict[str, Any]:
        """Plain diagnostics of the restart."""
        return {
            "bridged": self.bridged,
            "downtime_s": None if self.downtime_s is None else int(round(self.downtime_s)),
            "reason": self.reason,
        }


def decide(before: RestartBefore | None, after: RestartAfter) -> RestartDecision:
    """Decide whether the stop between the last saved tick and the first tick after it is bridged."""
    now = after.now
    if before is None or not before.last_ticks:
        return RestartDecision(False, None, now, None, REASON_NO_SNAPSHOT)
    t_stop = min(before.last_ticks)
    downtime = now - t_stop

    def refuse(reason: str) -> RestartDecision:
        return RestartDecision(False, t_stop, now, downtime, reason)

    if downtime < 0.0 or max(before.last_ticks) > now:
        return RestartDecision(False, t_stop, now, None, REASON_CLOCK)
    if downtime > RESTART_BRIDGE_MAX_GAP_S:
        return refuse(REASON_LONG)
    if before.hvac_mode not in _ACTIVE_MODES:
        return refuse(REASON_NOT_ACTIVE_BEFORE)
    if after.hvac_mode not in _ACTIVE_MODES:
        return refuse(REASON_NOT_ACTIVE_AFTER)
    if after.hvac_mode != before.hvac_mode:
        return refuse(REASON_MODE_CHANGED)
    # The learning stores hold a heater history in HEAT only; any mode needs the committed power.
    if not (before.heater_known if before.hvac_mode == _HEAT else before.committed_known):
        return refuse(REASON_HEATER)
    if before.cycle is None or before.cycle != after.cycle:
        return refuse(REASON_CYCLE)
    if before.excluded:
        return refuse(f"{before.excluded} before the stop")
    if after.excluded:
        return refuse(f"{after.excluded} after the restart")
    if before.temp is None or after.temp is None:
        return refuse(REASON_TEMPERATURE_UNKNOWN)
    since = before.temp_since if before.temp_since is not None else t_stop
    if is_temperature_jump(abs(after.temp - before.temp), now - since):
        return refuse(REASON_TEMPERATURE_JUMP)
    return RestartDecision(True, t_stop, now, downtime, None, before.hvac_mode)


def stop_time(saved_t: float | None, last_t: float, t_resume: float) -> float:
    """Instant of the stop from a store written at saved_t: within [last_t, t_resume].

    A store is written at the stop (reload, Home Assistant stop): the heater
    state it holds is the one kept over the downtime.
    """
    t = last_t if saved_t is None else saved_t
    return min(max(t, last_t), max(t_resume, last_t))


class RestartBridge:
    """Context of the last tick for the next save, and the restart decision of the session."""

    def __init__(self) -> None:
        # Context of the last tick of this session (saved with the algorithm state)
        self._tick: dict[str, Any] | None = None
        # Last exclusion reported by the thermostat (window, command overridden...)
        self._excluded_vt: str | None = None
        # Context loaded from the store, until the restart is decided
        self._saved: dict[str, Any] | None = None
        self.pending = False
        self.decision: RestartDecision | None = None

    @property
    def bridged(self) -> bool:
        return self.decision is not None and self.decision.bridged

    # ------------------------------------------------------------------
    # Live ticks
    # ------------------------------------------------------------------
    def note_tick(self, now: float, *, hvac_mode: Any, temp: float | None, cycle: tuple,
                  excluded: str | None) -> None:
        """Record the context of a regulation tick (wall-clock time)."""
        prev = self._tick
        temp_v = _finite(temp)
        since = now
        if prev is not None and temp_v is not None and prev.get("temp") == temp_v:
            since = prev.get("temp_since", now)
        self._tick = {
            "tick": now,
            "hvac_mode": None if hvac_mode is None else str(hvac_mode),
            "cycle": list(cycle),
            "excluded": excluded or self._excluded_vt,
            "temp": temp_v,
            "temp_since": since,
        }

    def note_exclusion(self, excluded: str | None) -> None:
        """Why SmartPI was not in control at the last tick, as seen by the thermostat."""
        self._excluded_vt = excluded or None
        if self._tick is not None and excluded:
            self._tick["excluded"] = excluded

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self) -> dict[str, Any] | None:
        """Context of the last tick; the loaded one while the restart is not decided yet."""
        if self.pending:
            return dict(self._saved) if self._saved is not None else None
        return dict(self._tick) if self._tick is not None else None

    def load(self, data: Any) -> None:
        """Keep the saved context until the first tick decides the restart."""
        self.pending = True
        self.decision = None
        self._saved = data if isinstance(data, dict) and _finite(data.get("tick")) is not None else None

    @property
    def saved(self) -> dict[str, Any] | None:
        return self._saved

    def before(self, store_ticks: list[tuple[float | None, bool]]) -> RestartBefore | None:
        """The saved context, with the last tick and heater state of the other stores.

        store_ticks: (last tick, heater state saved) of each other store that holds data.
        """
        saved = self._saved
        if saved is None:
            return None
        ticks = [float(saved["tick"])]
        committed_known = saved.get("committed") is not None
        heater_known = committed_known
        for tick, known in store_ticks:
            value = _finite(tick)
            if value is None:
                heater_known = False
                continue
            ticks.append(value)
            heater_known = heater_known and bool(known)
        cycle = saved.get("cycle")
        excluded = saved.get("excluded")
        return RestartBefore(
            last_ticks=tuple(ticks),
            heater_known=heater_known,
            hvac_mode=saved.get("hvac_mode") if isinstance(saved.get("hvac_mode"), str) else None,
            cycle=tuple(cycle) if isinstance(cycle, list) else None,
            excluded=excluded if isinstance(excluded, str) and excluded else None,
            temp=_finite(saved.get("temp")),
            temp_since=_finite(saved.get("temp_since")),
            committed_known=committed_known,
        )

    def resolve(self, decision: RestartDecision) -> None:
        """Keep the decision of the session; the saved context is no longer pending."""
        self.decision = decision
        self.pending = False

    def public(self) -> dict[str, Any] | None:
        """Plain diagnostics of the last restart, None before any."""
        return None if self.decision is None else self.decision.public()
