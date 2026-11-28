"""Room history: the recorded signals of the room model and their quiet periods (event loop side, pure Python).

The manager records the room signals (history.py) at each regulation tick and
applied command, and keeps the catalogue of the quiet periods of that history
(quiet_periods: no sun, constant setpoint, valid data). The room model reads
both (room_model/manager.py): the history for its energy balance and its
identification, the catalogue for its identification windows. The regulation
reads neither.

The catalogue holds the closed and complete quiet periods with their keys and
origins, so a period keeps its key when the history is pruned and over a
restart (the room model keys its windows by period). Without the position of
the home no period is found. The history and the catalogue have their own store.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from ..quiet_periods import (
    Position,
    astral_available,
    build_episodes,
    dark_intervals,
    local_iso,
    summary,
    validate_position,
)
from .clock import LocalClock
from .const import HISTORY_MAX_PERIODS, HISTORY_VALVE_MODE_SUPPORTED
from .history import RoomHistory

_LOGGER = logging.getLogger(__name__)

STATUS_CHECKING = "checking_dependencies"
STATUS_ACTIVE = "active"
STATUS_UNAVAILABLE = "dependencies_unavailable"

# 2: quiet periods (keys by period, catalogue of periods, position of the evidence)
# 3: the history and the catalogue only; the other fields of earlier formats are ignored
STATE_VERSION = 3
_PRUNE_EVERY_S = 3600.0
# The catalogue of the quiet periods is rebuilt at most this often
_CATALOGUE_EVERY_S = 600.0
# A position closer than this (degrees) to the one of the catalogue is the same home
_POSITION_TOL_DEG = 1e-3
# Published reason when the home position is unknown
_NO_POSITION_TEXT = "home position unknown: the absence of sun cannot be checked"


def _moved(a: Position, b: Position) -> bool:
    return (abs(a.latitude - b.latitude) > _POSITION_TOL_DEG
            or abs(a.longitude - b.longitude) > _POSITION_TOL_DEG)


class RoomHistoryManager:
    """Recorded signals and quiet periods of one thermostat."""

    def __init__(self, name: str, time_zone: str | None) -> None:
        self._name = name
        self._tz = time_zone
        # Time zone of the dates of the diagnostics only: no selection uses the local time
        self._clock = LocalClock(time_zone)
        self.status = STATUS_CHECKING
        self.supported = True
        self.history = RoomHistory()
        self._last_prune = 0.0
        self.dirty = False
        # Position of the home (None: unknown, no quiet period) and the one the catalogue was found with
        self.position: Position | None = None
        self._evidence_position: Position | None = None
        # (position, UTC day) of the last sun scan done in the executor
        self._sun_scanned: tuple[Position, int] | None = None
        self._reset_periods()

    def _reset_periods(self) -> None:
        # Closed and complete quiet periods (oldest first), keyed by "id"
        self.episodes: list[dict] = []
        self._cat_t: float | None = None

    def set_position(self, latitude, longitude) -> None:
        """Position of the home (Home Assistant configuration); a value that is not valid means unknown."""
        pos = validate_position(latitude, longitude)
        if pos is not None and not astral_available():
            _LOGGER.warning("%s - room history: the solar position is unavailable (astral)", self._name)
            pos = None
        if pos is None and self.position is None and latitude is not None:
            _LOGGER.debug("%s - room history: home position unknown, no quiet period", self._name)
        self.position = pos
        self._check_position()
        self._cat_t = None

    def _check_position(self) -> None:
        ev = self._evidence_position
        if self.position is not None and ev is not None and _moved(ev, self.position):
            _LOGGER.info("%s - room history: the home position changed, the quiet periods are forgotten",
                         self._name)
            self._reset_periods()
            self._evidence_position = None
            self.dirty = True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_dependencies(self, available: bool) -> None:
        """Result of the numpy/scipy check: without them nothing is recorded."""
        if available:
            self.status = STATUS_ACTIVE
            return
        if self.status != STATUS_UNAVAILABLE:
            _LOGGER.warning("%s - room model: numpy/scipy are unavailable, the room model stays off", self._name)
        self.status = STATUS_UNAVAILABLE
        self.history = RoomHistory()

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE and self.supported

    def stop(self) -> None:
        """Nothing runs for long: the sun scan of a day ends on its own."""

    async def async_run(self, hass, now: float | None = None) -> None:
        """Scan the sun over the history in the executor, once a day: the catalogue then finds it in the cache.

        A polar night can take about a second to search; it must not run in the event loop.
        """
        h = self.history
        now = time.time() if now is None else now
        if not self.active or self.position is None or h.first_t is None:
            return
        key = (self.position, int(now // 86400.0))
        if key == self._sun_scanned:
            return
        self._sun_scanned = key
        await hass.async_add_executor_job(dark_intervals, self.position, float(h.first_t), float(max(now, h.first_t)))

    def reset(self) -> None:
        """Forget the history and the quiet periods."""
        self.history = RoomHistory()
        self._reset_periods()
        self._evidence_position = None
        self.dirty = True

    # ------------------------------------------------------------------
    # Recording (called from the regulation, must stay cheap)
    # ------------------------------------------------------------------
    def observe(self, now: float, *, hvac_mode, temp: float | None, ext: float | None, setpoint: float | None,
                valve: bool = False, excluded: str | None = None) -> None:
        """Record one regulation tick.

        valve: the heater is a valve (applied power recorded as a level), not a PWM switch.
        excluded: why SmartPI is not in control of the heater (window open, power
        shedding, safety mode, command overridden...), None when it is; the
        tick is then recorded as invalid.
        """
        if valve and not HISTORY_VALVE_MODE_SUPPORTED:
            if self.supported:
                self.supported = False
                self.history = RoomHistory()
            return
        self.supported = True
        if self.status == STATUS_UNAVAILABLE:
            return
        if bool(valve) != self.history.valve:
            # The recorded heater signal changes kind: the history restarts.
            self.history = RoomHistory(valve=bool(valve))
        if hvac_mode != VThermHvacMode_HEAT and not excluded:
            excluded = "hvac not heating"
        self.history.record(now, temp=temp, ext=ext, setpoint=setpoint, excluded=excluded)
        if now - self._last_prune > _PRUNE_EVERY_S:
            self.history.prune(now)
            self._last_prune = now
        self.dirty = True

    def on_applied(self, now: float, power: float, cycle_start: bool, cycle_min: float, hvac_mode,
                   valve: bool = False) -> None:
        """Record an applied command: a PWM cycle (power x cycle on) or a valve level (heat-equivalent power)."""
        if self.status == STATUS_UNAVAILABLE or not self.supported or bool(valve) != self.history.valve:
            return
        if valve:
            # A valve outside HEAT delivers no heating power.
            self.history.applied_level(now, power if hvac_mode == VThermHvacMode_HEAT else 0.0, cycle_start)
            return
        if hvac_mode != VThermHvacMode_HEAT:
            return
        self.history.applied_cycle(now, power, max(float(cycle_min) * 60.0, 60.0), cycle_start)

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    def restart_context(self) -> tuple[float, bool] | None:
        """(last observation, heater state saved) of a restart to decide, None without history."""
        h = self.history
        if not h.restart_pending:
            return None
        return float(h.last_t), h.heater_saved

    def on_restart(self, bridged: bool, t_resume: float) -> None:
        """Apply the restart decision (restart_bridge.py) before the first observation after it."""
        self.history.resolve_restart(bridged, t_resume)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def signals_snapshot(self, now: float) -> dict:
        """Recorded signals as plain data, for a computation run in an executor."""
        h = self.history
        h.flush(now)
        return {
            "tz": self._tz,
            "pub_t": list(h.temp_t), "pub_v": list(h.temp_v),
            "ext_t": list(h.ext_t), "ext_v": list(h.ext_v),
            "on": list(h.on), "off": list(h.off), "gaps": h.invalid_intervals(now),
            "first_t": h.first_t, "last_t": h.last_t, "t_last": now, "pw": h.power_levels(),
        }

    def _refresh(self, now: float) -> None:
        """Extend the catalogue of the quiet periods (at most every _CATALOGUE_EVERY_S)."""
        if self.position is None:
            return
        if self._cat_t is not None and now - self._cat_t < _CATALOGUE_EVERY_S:
            return
        self._cat_t = now
        h = self.history
        h.flush(now)
        eps = build_episodes(self.position, h.setpoints, h.valid_spans(now), h.first_t, h.last_t, h.on,
                             (h.temp_t, h.temp_v), h.restarts)
        known = {e["id"] for e in self.episodes}
        last_end = self.episodes[-1]["end"] if self.episodes else -math.inf
        for e in eps:
            # The catalogue only grows forward: a closed period is never selected again differently
            if not e["settled"] or not e["complete"] or e["id"] in known or e["start"] < last_end - 1e-6:
                continue
            self.episodes.append(summary(e))
            last_end = e["end"]
            self._evidence_position = self.position
            self.dirty = True
        del self.episodes[:-HISTORY_MAX_PERIODS]

    def quiet_periods(self, now: float) -> list[dict]:
        """Closed quiet periods of the catalogue (oldest first), none without a position.

        The entries are the catalogue's own records: they must not be modified.
        """
        if self.history.is_empty():
            return []
        self._refresh(now)
        return list(self.episodes)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        """Quiet periods and data quality, published inside the room model block."""
        h = self.history
        last = self.episodes[-1] if self.episodes else None
        periods: dict[str, Any] = {
            "reason": _NO_POSITION_TEXT if self.position is None else None,
            "count": len(self.episodes),
            "last": None,
        }
        if last is not None:
            sel = last["comps"][last["sel"]]
            tz = self._clock._tz  # pylint: disable=protected-access
            periods["last"] = {"start": local_iso(last["quiet"][0], tz), "end": local_iso(last["quiet"][1], tz),
                               "regulated_from": local_iso(sel[4], tz)}
        return {
            "quiet_periods": periods,
            "data_invalid_reason": h.invalid_reason,
            "history_h": (round((h.last_t - h.first_t) / 3600.0, 1)
                          if h.first_t is not None and h.last_t is not None else 0.0),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_state(self) -> dict[str, Any]:
        self.dirty = False
        return {
            "version": STATE_VERSION,
            "position": self._evidence_position.to_list() if self._evidence_position else None,
            "history": self.history.to_dict(time.time()),
            "episodes": self.episodes,
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        """Load a saved state: the history of every format, the quiet periods of the formats 2 and 3.

        A later format is ignored. The values the emitter lag learning of earlier versions
        kept in the same store (comfort steps, regulated nights, windows, fast and refined
        values) are ignored.
        """
        if not isinstance(state, dict) or state.get("version") not in (1, 2, STATE_VERSION):
            return
        try:
            history = RoomHistory.from_dict(state.get("history"))
            episodes = [] if state.get("version") == 1 else [dict(x) for x in state.get("episodes") or []]
            for e in episodes:
                if not (isinstance(e.get("id"), str) and isinstance(e.get("comps"), list)
                        and isinstance(e.get("sel"), int) and isinstance(e.get("end"), (int, float))):
                    raise ValueError("malformed quiet period")
            pos = state.get("position")
            evidence = validate_position(*pos) if isinstance(pos, list) and len(pos) == 2 else None
        except (TypeError, ValueError) as err:
            _LOGGER.warning("%s - room history state ignored: %s", self._name, err)
            return
        self.history = history
        self._reset_periods()
        self.episodes = episodes
        self._evidence_position = evidence if episodes else None
        self._check_position()
