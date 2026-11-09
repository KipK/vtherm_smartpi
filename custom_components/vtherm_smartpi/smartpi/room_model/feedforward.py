"""Holding power of the feed-forward from the published room model (pure Python).

In heating with PWM heaters, a published room model with a qualified
energy-balance ratio r in force (manager.py) replaces the learned b / a in the
holding power FF1:

- walls identified and tracking ok ("walls"):
  u = r (T_sp - T_ext) + delta_wall / a, the wall extra power of the tracking
  in both signs;
- walls identified, tracking not ok ("static"): u = r (T_sp - T_ext);
- simple model ("simple"): u = r (T_sp - T_ext).

The static gain is the energy-balance ratio, never the fitted b_eq / a: in a
room with heavy walls the fitted b_eq = alpha - c is a small difference of
two terms that depend on the emitter time, while the multi-day balance does
not. The fit gives the wall dynamics (delta_wall / a). The same ratio is the
equilibrium of the braking prediction.

The holding power is clamped to [0, 1] and applies at full weight (no warmup
ramp); the unmeasured disturbance d is not used. The learned holding power
stays in use ("learned") without a published model, while a published model
waits for its first qualified energy-balance ratio (the wall term does not act
either), in cooling and with a valve. The energy-balance ratio alone, without
a published model, is never used: without the wall term it gives the wrong
holding power in rooms with heavy walls.

The holding power has two parts, handled differently at a discontinuity:

- Static part (the learned holding power for the learned source). Its
  discrete changes are moved into the integral so that the total command
  does not jump: I <- I - dU / Ki, within the integral limits. They are a
  change between the learned source and a room model source, a change of
  model structure, a new publication of the parameters and a new
  energy-balance ratio in force. Only the step of
  the static part is moved, evaluated with the wall part applied at this
  cycle.
- Wall part. It is never moved into the integral. When the wall term in
  force changes discretely (switched on, switched off by the tracking or by a
  change of source, new wall parameters; not a new energy-balance ratio
  alone), the applied wall part goes
  linearly over RM_WALL_FADE_S from the value applied last to its target:
  the live wall term of a healthy tracking, or 0 when it is off. A fade-out
  never reads the tracking again. In heating, a fade-out goes on over the
  learned holding power when the learned source takes over.

The continuous evolution of the wall term, of the setpoint and of the
outdoor temperature is not transferred: it is the action of the
feed-forward. The blend is saved with the source, on the wall clock, so that
a restart goes on with the same applied wall part.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from ..const import clamp
from .const import RM_WALL_EXTRA_MAX, RM_WALL_FADE_S
from .model import RoomParams
from .observer import HEALTH_OK

SOURCE_LEARNED = "learned"
SOURCE_WALLS = "walls"
SOURCE_STATIC = "static"
SOURCE_SIMPLE = "simple"
_SOURCES = (SOURCE_LEARNED, SOURCE_WALLS, SOURCE_STATIC, SOURCE_SIMPLE)

# Plain values of the diagnostics
_PLAIN = {
    SOURCE_LEARNED: "learned model",
    SOURCE_WALLS: "room model (air and walls)",
    SOURCE_STATIC: "room model (static part)",
    SOURCE_SIMPLE: "room model (simple)",
}

# Same bound as the learned feed-forward gain b / a
FF_GAIN_MAX = 3.0


@dataclass(frozen=True)
class HoldingSource:
    """Source of the holding power for one cycle.

    gain: static gain in force, the energy-balance ratio (0 for the learned source).
    wall: live wall extra power delta_wall / a of the tracking (fraction of
        full power), the target of the applied wall part; None without the
        wall term.
    params: published parameters the source comes from (None for the learned source).
    """

    kind: str
    gain: float = 0.0
    wall: float | None = None
    params: RoomParams | None = None

    @property
    def room(self) -> bool:
        return self.kind != SOURCE_LEARNED

    @property
    def plain(self) -> str:
        return _PLAIN[self.kind]

    def static(self, target_temp: float, ext_temp: float | None) -> float:
        """Static part gain (T_sp - T_ext), unclamped; 0 without outdoor temperature."""
        if ext_temp is None:
            return 0.0
        return self.gain * (target_temp - ext_temp)

    def power(self, target_temp: float, ext_temp: float | None, wall: float | None = None) -> float:
        """Room model holding power, 0 without outdoor temperature.

        wall: wall part to add; None adds the source's own live wall term.
        """
        if ext_temp is None:
            return 0.0
        if wall is None:
            wall = self.wall if self.wall is not None else 0.0
        return clamp(self.static(target_temp, ext_temp) + wall, 0.0, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "gain": self.gain, "wall": self.wall,
                "params": self.params.to_dict() if self.params is not None else None}

    @classmethod
    def from_dict(cls, data: Any) -> "HoldingSource | None":
        if not isinstance(data, dict) or data.get("kind") not in _SOURCES:
            return None
        kind = data["kind"]
        if kind == SOURCE_LEARNED:
            return LEARNED
        try:
            gain = float(data["gain"])
            wall = data.get("wall")
            wall = None if wall is None else float(wall)
        except (KeyError, TypeError, ValueError):
            return None
        params = RoomParams.from_dict(data.get("params"))
        if params is None or (kind == SOURCE_WALLS) != (wall is not None):
            return None
        return cls(kind, gain, wall, params)


LEARNED = HoldingSource(SOURCE_LEARNED)


def static_gain(room_model, hvac_mode, valve: bool) -> float | None:
    """Static gain in force: the qualified energy-balance ratio once a room model is published, in heating with
    PWM heaters; None otherwise (no publication, no qualified ratio yet, cooling, valve)."""
    if room_model is None or valve or hvac_mode != VThermHvacMode_HEAT:
        return None
    p = room_model.params
    if p is None or p.a <= 0.0 or p.b_eq <= 0.0:
        return None
    r = room_model.eb_ratio
    if r is None or not r > 0.0:
        return None
    return clamp(r, 0.0, FF_GAIN_MAX)


def select_source(room_model, hvac_mode, valve: bool) -> HoldingSource:
    """Holding power source for this cycle."""
    gain = static_gain(room_model, hvac_mode, valve)
    if gain is None:
        return LEARNED
    p = room_model.params
    if not p.full:
        return HoldingSource(SOURCE_SIMPLE, gain, None, p)
    obs = room_model.observer
    wall = obs.wall_extra() if obs.health() == HEALTH_OK else None
    if wall is None:
        return HoldingSource(SOURCE_STATIC, gain, None, p)
    return HoldingSource(SOURCE_WALLS, gain, wall, p)


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


class HoldingTransfer:
    """Holding power source in force, the bumpless transfer of its static part and the wall part blend."""

    def __init__(self) -> None:
        self.ref: HoldingSource | None = None
        self.last_delta = 0.0
        self.wall_applied = 0.0
        self._fade_from = 0.0
        self._fade_start: float | None = None

    def reset(self) -> None:
        """Forget the source in force and the applied wall part (PI memory reset): the next source starts
        without transfer, its wall part from 0."""
        self.ref = None
        self.last_delta = 0.0
        self.wall_applied = 0.0
        self._fade_from = 0.0
        self._fade_start = None

    @property
    def room_in_force(self) -> bool:
        return self.ref is not None and self.ref.room

    @property
    def source(self) -> HoldingSource:
        return self.ref if self.ref is not None else LEARNED

    @property
    def fading(self) -> bool:
        return self._fade_start is not None

    @property
    def wall_shown(self) -> float | None:
        """Applied wall part for the diagnostics, None when no wall term is in force or fading."""
        if self.source.wall is None and not self.fading and self.wall_applied == 0.0:
            return None
        return self.wall_applied

    def _law_change(self, new: HoldingSource) -> bool:
        """A change of source or of published parameters: the wall term in force changes discretely."""
        old = self.ref
        if old is None:
            return False
        if old.kind != new.kind:
            return True
        return old.room and old.params != new.params

    def discrete_change(self, new: HoldingSource) -> bool:
        """A change of source, of published parameters or of the static gain in force (never the learned
        source's own evolution)."""
        old = self.ref
        return self._law_change(new) or (old is not None and old.room and old.gain != new.gain)

    def _blend(self, new: HoldingSource, now: float, heat: bool) -> None:
        """Move the applied wall part towards the wall term of the new source."""
        old = self.ref
        if not new.room and not heat:
            # Out of heating the wall term has no meaning: nothing is applied.
            self.wall_applied, self._fade_from, self._fade_start = 0.0, 0.0, None
            return
        if (old is None and new.wall is not None) or self._law_change(new):
            self._fade_from, self._fade_start = self.wall_applied, now
        target = new.wall if new.wall is not None else 0.0
        if self._fade_start is None:
            self.wall_applied = target
            return
        elapsed = now - self._fade_start
        if elapsed < 0.0:
            # Clock set back: the blend starts over from the value applied last.
            self._fade_from, self._fade_start, elapsed = self.wall_applied, now, 0.0
        if elapsed >= RM_WALL_FADE_S:
            self.wall_applied, self._fade_start = target, None
            return
        self.wall_applied = self._fade_from + (target - self._fade_from) * elapsed / RM_WALL_FADE_S

    def _total(self, src: HoldingSource, target_temp: float, ext_temp: float | None, learned_power: float,
               wall: float) -> float:
        """Holding power of a source with the given wall part (the learned one: its holding power plus the wall
        part, with an outdoor temperature)."""
        if src.room:
            return src.power(target_temp, ext_temp, wall)
        if ext_temp is None or wall == 0.0:
            return learned_power
        return clamp(learned_power + wall, 0.0, 1.0)

    def update(self, new: HoldingSource, *, now: float, learned_power: float, target_temp: float,
               ext_temp: float | None, heat: bool = True) -> float:
        """Take the new source and blend the wall part; return the static step dU to transfer (0 without a
        discrete change).

        now: wall clock time (s) of this cycle, the time base of the blend.
        learned_power: learned holding power (after FF2) at this cycle, the static part of the learned source.
            Both sides of the step are evaluated at the current setpoint and outdoor temperature with the wall
            part applied at this cycle, so the step holds the static part only.
        heat: heating mode; the wall part is dropped on the learned source outside heating.
        """
        old = self.ref
        change = self.discrete_change(new)
        self._blend(new, now, heat)
        delta = 0.0
        if change:
            w = self.wall_applied
            delta = (self._total(new, target_temp, ext_temp, learned_power, w)
                     - self._total(old, target_temp, ext_temp, learned_power, w))
        self.ref = new
        self.last_delta = delta
        return delta

    def save(self) -> dict[str, Any] | None:
        return self.ref.to_dict() if self.ref is not None else None

    def save_wall(self) -> dict[str, Any]:
        return {"applied": self.wall_applied, "from": self._fade_from, "start": self._fade_start}

    def load(self, data: Any, wall: Any = None) -> None:
        """Restore the source in force and the wall part blend.

        A state without the source was saved on the learned source. A state without a valid blend applied the
        wall term of its source in full.
        """
        self.ref = LEARNED if data is None else HoldingSource.from_dict(data)
        self.last_delta = 0.0
        own = self.ref.wall if self.ref is not None and self.ref.wall is not None else 0.0
        self.wall_applied, self._fade_from, self._fade_start = own, 0.0, None
        if not isinstance(wall, dict):
            return
        applied, start = _finite(wall.get("applied")), wall.get("start")
        fade_from = _finite(wall.get("from"))
        start = None if start is None else _finite(start)
        if applied is None or fade_from is None or (wall.get("start") is not None and start is None):
            return
        if abs(applied) > RM_WALL_EXTRA_MAX or abs(fade_from) > RM_WALL_EXTRA_MAX:
            return
        self.wall_applied, self._fade_from, self._fade_start = applied, fade_from, start
