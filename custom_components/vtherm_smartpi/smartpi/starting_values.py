"""Starting values of the room model: the interface between the readings of the measurement events and the a/b estimator.

A blank start may receive values of ``a`` (heating rate) and ``b`` (loss coefficient) measured before the learning has
published its own. ``a`` is given once per mode and blank start, ``b`` (shared by the modes) with the first hand-over
only (``give``). They stand in for the learning's own values and are released (``release``) when the learning has its
own:

- ``a`` at the learning's first own publication of ``a``;
- ``b`` when the learning's own time constant becomes reliable (``ABEstimator.tau_reliability`` read on its own samples).

A released quantity takes the learning's own value. A learning reset drops what stands, and so does the reset of the
mode-dependent channel of ``a``. A starting value is not a measurement: no history and no counter moves with it.

The feed-forward ramp counts accepted measures; the values of the first hand-over add ``KEPT_RAMP_MEASURES`` for each
quantity given in this blank start. That credit is latched at the give and does not shrink at a release, and the ramp reads the larger of
the credit and the accepted measures (``ramp_count``).
"""
from __future__ import annotations

from typing import Any

KEPT_RAMP_MEASURES = 5      # measures of the feed-forward ramp a quantity given in the blank start stands for
QUANTITIES = ("a", "b")


class StartingValues:
    """The values standing for ``a`` and ``b``, what was given in this blank start and the credit it earned."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Blank start: nothing stands, nothing has been given."""
        self._value: dict[str, float] = {}      # the quantities that stand, with their value (``a`` signed)
        self.mode: str | None = None            # HVAC mode of the first hand-over
        self._modes: set[str] = set()           # HVAC modes ``give`` has been called for in this blank start
        self.credit = 0                         # measures the ramp credits for the quantities given
        self.own_b = False                      # the learning's own time constant has been reliable

    @property
    def handed(self) -> bool:
        """``give`` has been called in this blank start."""
        return bool(self._modes)

    def handed_for(self, mode: str) -> bool:
        """``give`` has been called for ``mode`` in this blank start."""
        return mode in self._modes

    def standing(self, quantity: str) -> bool:
        """Whether the starting value of ``quantity`` stands (given and not yet released or dropped)."""
        return quantity in self._value

    def value(self, quantity: str) -> float | None:
        return self._value.get(quantity)

    def give(self, a: float | None, b: float | None, mode: str) -> bool:
        """Give the starting values (None: none for that quantity), once per mode and blank start; False when already
        handed over for ``mode``. Only the first hand-over of the blank start gives ``b`` and earns the credit, which is
        ``KEPT_RAMP_MEASURES`` for each value given."""
        if mode in self._modes:
            return False
        first = not self._modes
        self._modes.add(mode)
        if first:
            self.mode = mode
        for quantity, value in (("a", a), ("b", b if first else None)):
            if value is not None:
                self._value[quantity] = value
                self.credit += KEPT_RAMP_MEASURES if first else 0
        return True

    def release(self, quantity: str) -> bool:
        """The learning has its own value of ``quantity``: the starting value stops standing. True when it stood."""
        return self._value.pop(quantity, None) is not None

    def stand(self, quantity: str, value: float) -> None:
        """A starting value that was set aside with its HVAC mode stands again."""
        self._value[quantity] = value

    def ramp_count(self, accepted: int) -> int:
        """Measures the feed-forward ramp reads: the larger of the credit and the accepted measures."""
        return max(self.credit, accepted)

    def save(self) -> dict[str, Any]:
        return {"a": self._value.get("a"), "b": self._value.get("b"), "mode": self.mode, "handed": self.handed,
                "modes": sorted(self._modes), "credit": self.credit}

    def load(self, saved: object) -> None:
        """Restore what ``save`` wrote; anything malformed leaves a blank start (the readings give again)."""
        self.reset()
        if not isinstance(saved, dict) or saved.get("handed") is not True:
            return
        credit = saved.get("credit")
        if not isinstance(credit, int) or isinstance(credit, bool) or credit < 0:
            return
        values = {}
        for quantity in QUANTITIES:
            raw = saved.get(quantity)
            if raw is None:
                continue
            if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw != raw or abs(raw) == float("inf"):
                return
            values[quantity] = float(raw)
        self.credit, self._value = credit, values
        self.mode = saved["mode"] if isinstance(saved.get("mode"), str) else None
        modes = saved.get("modes")
        self._modes = {m for m in modes if isinstance(m, str)} if isinstance(modes, list) else set()
        if not self._modes:                      # a state saved with one hand-over and no list of modes
            self._modes = {self.mode or ""}
