"""Positive-integral guard for Smart-PI recovery phases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .const import (
    DEADBAND_HYSTERESIS,
    INTEGRAL_GUARD_RELEASE_ERROR_RATIO,
    INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H,
    INTEGRAL_GUARD_RELEASE_PERSISTENCE,
    INTEGRAL_GUARD_RELEASE_SLOPE_RATIO,
)


class IntegralGuardSource(str, Enum):
    """Recovery source that temporarily blocks positive integral growth."""

    NONE = "none"
    SETPOINT_CHANGE = "setpoint_change"
    OFF_RESUME = "off_resume"
    WINDOW_RESUME = "window_resume"
    POWER_SHEDDING_RESUME = "power_shedding_resume"
    DISTURBANCE_RECOVERY = "disturbance_recovery"


def integral_guard_release_error_threshold(deadband_c: float) -> float:
    """Return the error threshold below which guard release may be evaluated."""
    return max(
        max(deadband_c, 0.0) * INTEGRAL_GUARD_RELEASE_ERROR_RATIO,
        max(deadband_c, 0.0) + max(DEADBAND_HYSTERESIS, 0.0),
    )


@dataclass(slots=True)
class IntegralGuardDecision:
    """Current authorization state for positive integral growth."""

    block_positive: bool
    block_negative: bool
    mode_suffix: str


class SmartPIIntegralGuard:
    """Track recovery phases and re-enable positive integral only after stabilization."""

    def __init__(self, name: str):
        self._name = name
        self.reset()

    @property
    def active(self) -> bool:
        """Return True when positive integral growth is still blocked."""
        return self._source != IntegralGuardSource.NONE

    @property
    def source(self) -> IntegralGuardSource:
        """Return the current guard source."""
        return self._source

    def reset(self) -> None:
        """Reset the guard state."""
        self._source = IntegralGuardSource.NONE
        self._block_positive = True
        self._stable_cycles = 0
        self._peak_signed_recovery_slope_h = 0.0
        self._last_release_reason = "reset"

    def arm(self, source: IntegralGuardSource, *, block_positive: bool = True) -> None:
        """Arm the guard for the given recovery source."""
        if source == IntegralGuardSource.NONE:
            self.clear("none")
            return
        if self._source != source or self._block_positive != block_positive:
            self._stable_cycles = 0
            self._peak_signed_recovery_slope_h = 0.0
        self._source = source
        self._block_positive = block_positive
        self._last_release_reason = "armed"

    def clear(self, reason: str) -> None:
        """Release the guard and clear transient tracking."""
        self._source = IntegralGuardSource.NONE
        self._stable_cycles = 0
        self._peak_signed_recovery_slope_h = 0.0
        self._last_release_reason = reason

    def decide(
        self,
        *,
        raw_error: float,
        release_error: float,
        deadband_c: float,
        in_core_deadband: bool,
        signed_recovery_slope_h: float | None,
    ) -> IntegralGuardDecision:
        """Return whether positive integral growth must remain blocked."""
        if not self.active:
            return IntegralGuardDecision(False, False, "off")

        if in_core_deadband:
            self.clear("core_deadband")
            return IntegralGuardDecision(False, False, "released_core_deadband")

        if self._block_positive and raw_error <= 0.0:
            self.clear("signed_error_non_positive")
            return IntegralGuardDecision(False, False, "released_signed_error")

        if not self._block_positive and raw_error >= 0.0:
            self.clear("signed_error_non_positive")
            return IntegralGuardDecision(False, False, "released_signed_error")

        release_error_threshold = integral_guard_release_error_threshold(deadband_c)
        if abs(release_error) > release_error_threshold:
            self._stable_cycles = 0
            if signed_recovery_slope_h is not None and signed_recovery_slope_h > self._peak_signed_recovery_slope_h:
                self._peak_signed_recovery_slope_h = signed_recovery_slope_h
            return IntegralGuardDecision(
                self._block_positive,
                not self._block_positive,
                self._source.value,
            )

        if signed_recovery_slope_h is None:
            self.clear("released_missing_slope")
            return IntegralGuardDecision(False, False, "released_missing_slope")

        if signed_recovery_slope_h > self._peak_signed_recovery_slope_h:
            self._peak_signed_recovery_slope_h = signed_recovery_slope_h

        release_slope = max(
            self._peak_signed_recovery_slope_h * INTEGRAL_GUARD_RELEASE_SLOPE_RATIO,
            INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H,
        )
        if signed_recovery_slope_h <= release_slope:
            self._stable_cycles += 1
        else:
            self._stable_cycles = 0

        if self._stable_cycles >= INTEGRAL_GUARD_RELEASE_PERSISTENCE:
            self.clear("stabilized")
            return IntegralGuardDecision(False, False, "released_stabilized")

        return IntegralGuardDecision(
            self._block_positive,
            not self._block_positive,
            self._source.value,
        )

    def save_state(self) -> dict:
        """Serialize the guard state for persistence."""
        return {
            "source": self._source.value,
            "block_positive": self._block_positive,
            "stable_cycles": self._stable_cycles,
            "peak_signed_recovery_slope_h": self._peak_signed_recovery_slope_h,
            "last_release_reason": self._last_release_reason,
        }

    def load_state(self, state: dict) -> None:
        """Restore the guard state from persistence."""
        if not state:
            self.reset()
            return

        source = str(state.get("source", IntegralGuardSource.NONE.value))
        try:
            self._source = IntegralGuardSource(source)
        except ValueError:
            self._source = IntegralGuardSource.NONE
        self._block_positive = bool(state.get("block_positive", True))
        self._stable_cycles = max(int(state.get("stable_cycles", 0)), 0)
        self._peak_signed_recovery_slope_h = max(
            float(state.get("peak_signed_recovery_slope_h", 0.0)),
            0.0,
        )
        self._last_release_reason = str(state.get("last_release_reason", "loaded"))
