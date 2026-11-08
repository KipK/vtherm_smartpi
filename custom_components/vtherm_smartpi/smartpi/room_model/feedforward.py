"""Holding power of the feed-forward from the published room model (pure Python).

In heating with PWM heaters, a published room model replaces the learned
b / a in the holding power FF1:

- walls identified and tracking ok ("walls"):
  u = (b_eq / a)(T_sp - T_ext) + delta_wall / a, the wall extra power of the
  tracking in both signs;
- walls identified, tracking not ok ("static"): u = (b_eq / a)(T_sp - T_ext);
- simple model ("simple"): u = (b_eq / a)(T_sp - T_ext).

The holding power is clamped to [0, 1] and applies at full weight (no warmup
ramp); the unmeasured disturbance d is not used. Without a published model,
in cooling or with a valve, the learned holding power stays in use
("learned").

Discrete changes of the holding power are moved into the integral so that the
total command does not jump: I <- I - dU / Ki, within the integral limits.
These changes are a change of source (including the wall term switched on or
off by the tracking) and a new publication of the parameters. The continuous
evolution of the wall term, of the setpoint and of the outdoor temperature is
not transferred: it is the action of the feed-forward.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from ..const import clamp
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

    gain: b_eq / a of the published model (0 for the learned source).
    wall: wall extra power delta_wall / a in force (fraction of full power),
        None without the wall term.
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

    def power(self, target_temp: float, ext_temp: float | None) -> float:
        """Room model holding power, 0 without outdoor temperature."""
        if ext_temp is None:
            return 0.0
        raw = self.gain * (target_temp - ext_temp)
        if self.wall is not None:
            raw += self.wall
        return clamp(raw, 0.0, 1.0)

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
    """b_eq / a of the published room model in heating with PWM heaters, None otherwise."""
    if room_model is None or valve or hvac_mode != VThermHvacMode_HEAT:
        return None
    p = room_model.params
    if p is None or p.a <= 0.0 or p.b_eq <= 0.0:
        return None
    return clamp(p.b_eq / p.a, 0.0, FF_GAIN_MAX)


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


class HoldingTransfer:
    """Holding power source in force and the bumpless transfer of its discrete changes."""

    def __init__(self) -> None:
        self.ref: HoldingSource | None = None
        self.last_delta = 0.0

    def reset(self) -> None:
        """Forget the source in force: the next one starts without transfer (PI memory reset)."""
        self.ref = None
        self.last_delta = 0.0

    @property
    def room_in_force(self) -> bool:
        return self.ref is not None and self.ref.room

    @property
    def source(self) -> HoldingSource:
        return self.ref if self.ref is not None else LEARNED

    def discrete_change(self, new: HoldingSource) -> bool:
        """A change of source or of published parameters (never the learned source's own evolution)."""
        old = self.ref
        if old is None:
            return False
        if old.kind != new.kind:
            return True
        return old.room and old.params != new.params

    def update(self, new: HoldingSource, *, new_power: float, learned_power: float,
               target_temp: float, ext_temp: float | None) -> float:
        """Take the new source; return the holding power step dU to transfer (0 without discrete change).

        new_power: holding power of the new source (after FF2) at this cycle.
        learned_power: learned holding power (after FF2) at this cycle, the outgoing side when the
            learned source was in force. A room model source is evaluated at the current setpoint and
            outdoor temperature with the wall term of the previous cycle.
        """
        delta = 0.0
        if self.discrete_change(new):
            old = self.ref
            old_power = old.power(target_temp, ext_temp) if old.room else learned_power
            delta = new_power - old_power
        self.ref = new
        self.last_delta = delta
        return delta

    def save(self) -> dict[str, Any] | None:
        return self.ref.to_dict() if self.ref is not None else None

    def load(self, data: Any) -> None:
        """Restore the source in force; a state without it was saved on the learned source."""
        self.ref = LEARNED if data is None else HoldingSource.from_dict(data)
        self.last_delta = 0.0
