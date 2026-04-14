"""SmartPI Guard Manager."""
import logging
from typing import TypedDict, Optional

from ..hvac_mode import VThermHvacMode, VThermHvacMode_HEAT

_LOGGER = logging.getLogger(__name__)

# Re-defined here to avoid circular imports if necessary, 
# or import if available. Assuming available in parent const.
# NEAR_BAND_HYSTERESIS_C = 0.05 (We can pass this in or define it here)
NEAR_BAND_HYSTERESIS_C = 0.05

class GuardState(TypedDict):
    """Guard state for persistence."""
    guard_cut_active: bool
    guard_cut_count: int
    guard_kick_active: bool
    guard_kick_count: int

class GuardAction:
    """Action returned by check_guards."""
    NONE = "none"
    CUT_TRIGGER = "cut_trigger"
    CUT_MAINTAIN = "cut_maintain" 
    CUT_RESET = "cut_reset"
    KICK_TRIGGER = "kick_trigger"
    KICK_MAINTAIN = "kick_maintain"
    KICK_RESET = "kick_reset"

class SmartPIGuards:
    """Manages Guard Cut and Guard Kick safety features."""

    def __init__(self):
        """Initialize guards."""
        self._guard_cut_active: bool = False
        self._guard_cut_count: int = 0
        self._guard_kick_active: bool = False
        self._guard_kick_count: int = 0

    @property
    def guard_cut_active(self) -> bool:
        return self._guard_cut_active
    
    @property
    def guard_cut_count(self) -> int:
        return self._guard_cut_count
        
    @property
    def guard_kick_active(self) -> bool:
        return self._guard_kick_active
        
    @property
    def guard_kick_count(self) -> int:
        return self._guard_kick_count

    def reset(self, keep_counts: bool = True) -> None:
        """Reset active flags."""
        self._guard_cut_active = False
        self._guard_kick_active = False
        if not keep_counts:
            self._guard_cut_count = 0
            self._guard_kick_count = 0

    def check_guard_cut(
        self,
        current_temp: float,
        target_temp: float,
        near_band_above: float,
        in_near_band: bool,
        is_device_active: bool,
        hvac_mode: VThermHvacMode,
        is_calibration: bool
    ) -> str:
        """
        Check for Guard Cut condition.
        Returns: GuardAction.CUT_TRIGGER, CUT_MAINTAIN, CUT_RESET, or NONE.
        """
        # Pre-conditions
        if (
            current_temp is None 
            or target_temp is None 
            or hvac_mode != VThermHvacMode_HEAT
            or is_calibration
        ):
            return GuardAction.NONE

        threshold_above = target_temp + near_band_above
        reset_threshold = threshold_above - NEAR_BAND_HYSTERESIS_C

        if self._guard_cut_active:
            if current_temp <= reset_threshold:
                # Reset
                self._guard_cut_active = False
                _LOGGER.info(
                    "Guard cut reset: T=%.2f°C <= %.2f°C",
                    current_temp, reset_threshold,
                )
                return GuardAction.CUT_RESET
            else:
                # Maintain
                _LOGGER.debug("Guard cut active, maintaining OFF")
                return GuardAction.CUT_MAINTAIN

        elif (
            in_near_band
            and current_temp > threshold_above
            and is_device_active
        ):
            # Trigger
            self._guard_cut_active = True
            self._guard_cut_count += 1
            _LOGGER.warning(
                "Guard cut triggered (#%d): T=%.2f°C > %.2f°C (SP+NB). Forcing OFF.",
                self._guard_cut_count, current_temp, threshold_above,
            )
            return GuardAction.CUT_TRIGGER
            
        return GuardAction.NONE

    def check_guard_kick(
        self,
        current_temp: float,
        target_temp: float,
        near_band_below: float,
        in_near_band: bool,
        on_percent: float,
        hvac_mode: VThermHvacMode,
        is_calibration: bool
    ) -> str:
        """
        Check for Guard Kick condition.
        Returns: GuardAction.KICK_TRIGGER, KICK_MAINTAIN, KICK_RESET, or NONE.
        """
        # Pre-conditions
        if (
            current_temp is None 
            or target_temp is None 
            or hvac_mode != VThermHvacMode_HEAT
            or is_calibration
        ):
            return GuardAction.NONE

        threshold_below = target_temp - near_band_below
        reset_threshold_kick = threshold_below + NEAR_BAND_HYSTERESIS_C

        if self._guard_kick_active:
            if current_temp >= reset_threshold_kick:
                # Reset
                self._guard_kick_active = False
                _LOGGER.info(
                    "Guard kick reset: T=%.2f°C >= %.2f°C",
                    current_temp, reset_threshold_kick,
                )
                return GuardAction.KICK_RESET
            else:
                # Maintain
                return GuardAction.KICK_MAINTAIN

        elif (
            in_near_band
            and current_temp < threshold_below
            and on_percent < 0.99
        ):
            # Trigger
            self._guard_kick_active = True
            self._guard_kick_count += 1
            _LOGGER.warning(
                "Guard kick triggered (#%d): T=%.2f°C < %.2f°C (SP-NB). Forcing cycle restart.",
                self._guard_kick_count, current_temp, threshold_below,
            )
            return GuardAction.KICK_TRIGGER
            
        return GuardAction.NONE

    def save_state(self) -> GuardState:
        """Save state."""
        return {
            "guard_cut_count": self._guard_cut_count,
            "guard_kick_count": self._guard_kick_count,
        }

    def load_state(self, state: GuardState) -> None:
        """Load state."""
        self._guard_cut_count = int(state.get("guard_cut_count", 0))
        self._guard_kick_count = int(state.get("guard_kick_count", 0))
