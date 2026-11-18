"""Setpoint bookkeeping for Smart-PI: the setpoint boost."""
from __future__ import annotations

import logging
from typing import Optional

from .const import SETPOINT_BOOST_THRESHOLD, SETPOINT_BOOST_ERROR_MIN

_LOGGER = logging.getLogger(__name__)


class SmartPISetpointManager:
    """Hold the boost state of a setpoint change.

    The regulation always works on the raw target. The boost lifts the output
    rate limit after a significant setpoint change until the error is small.
    """

    def __init__(self, name: str):
        self._name = name
        # Boost state
        self.boost_active: bool = False
        self.prev_setpoint_for_boost: Optional[float] = None

    def reset(self) -> None:
        """Reset internal state."""
        self.boost_active = False
        self.prev_setpoint_for_boost = None

    def load_state(self, state: dict) -> None:
        """Load state from persistence; unknown keys are ignored."""
        if not state:
            return

        self.boost_active = bool(state.get("setpoint_boost_active", False))

        ps = state.get("prev_setpoint_for_boost")
        if ps is not None:
            self.prev_setpoint_for_boost = float(ps)

    def save_state(self) -> dict:
        """Save state for persistence."""
        return {
            "setpoint_boost_active": self.boost_active,
            "prev_setpoint_for_boost": self.prev_setpoint_for_boost,
        }

    def update_boost_state(self, target_temp: float, error: float) -> bool:
        """Update the boost state from setpoint changes."""
        if self.prev_setpoint_for_boost is None:
            self.prev_setpoint_for_boost = target_temp

        sp_delta = target_temp - self.prev_setpoint_for_boost

        # Activate boost on significant change
        if abs(sp_delta) >= SETPOINT_BOOST_THRESHOLD:
            self.boost_active = True
            self.prev_setpoint_for_boost = target_temp
            _LOGGER.debug("%s - Boost activate: delta=%.2f", self._name, sp_delta)
        elif abs(sp_delta) > 0.01:
            # Just track
            self.prev_setpoint_for_boost = target_temp
            self.boost_active = False

        # Deactivate boost when error is small
        if self.boost_active and abs(error) < SETPOINT_BOOST_ERROR_MIN:
            self.boost_active = False
            _LOGGER.debug("%s - Boost deactivate: error=%.3f", self._name, abs(error))

        return self.boost_active
