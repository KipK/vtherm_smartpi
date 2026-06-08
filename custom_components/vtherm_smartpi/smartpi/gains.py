"""
Smart-PI Gain Scheduler Module.

Manages Kp/Ki gain calculation with IMC-based tuning and near-band adjustments.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import (
    GAIN_LAMBDA_CYCLE_FACTOR,
    GAIN_LAMBDA_DEADTIME_FACTOR,
    GAIN_LAMBDA_MIN_MIN,
    GAIN_LAMBDA_VALVE_DEADTIME_FACTOR,
    KI_MAX,
    KI_MIN,
    KI_SAFE,
    NEAR_BAND_TAPER_KI_MIN,
    NEAR_BAND_TAPER_KP_MIN,
    KP_MAX,
    KP_MIN,
    KP_SAFE,
    GovernanceDecision,
    clamp,
)

if TYPE_CHECKING:
    from .ab_estimator import ABEstimator
    from .deadtime_estimator import DeadTimeEstimator

_LOGGER = logging.getLogger(__name__)


@dataclass
class GainResult:
    """Result of gain calculation."""
    kp: float
    ki: float
    kp_source: str
    ki_source: str


class GainScheduler:
    """
    Manages Kp/Ki gain calculation with IMC and near-band adjustments.
    
    The gain scheduler calculates appropriate proportional (Kp) and integral (Ki)
    gains based on:
    1. Explicit IMC/SIMC tuning when the A/B model and dead time are reliable
    2. Safe fallback gains when the complete model is unavailable
    3. Near-band gain reduction for stability near setpoint
    4. Governance freeze application for safe operation
    """

    def __init__(self, name: str):
        """Initialize the gain scheduler.
        
        Args:
            name: Identifier for logging purposes.
        """
        self._name = name
        # Initialize with safe defaults
        self._kp = KP_SAFE
        self._ki = KI_SAFE
        self._kp_source = "safe"
        self._ki_source = "safe"
        
        # Previous values for freeze/soft-freeze logic
        self._prev_kp = KP_SAFE
        self._prev_ki = KI_SAFE

    def reset(self) -> None:
        """Reset gains to safe defaults."""
        self._kp = KP_SAFE
        self._ki = KI_SAFE
        self._kp_source = "safe"
        self._ki_source = "safe"
        self._prev_kp = KP_SAFE
        self._prev_ki = KI_SAFE
        _LOGGER.debug("%s: GainScheduler reset to safe defaults", self._name)

    def calculate(
        self,
        tau_reliable: bool,
        tau_min: float,
        estimator: "ABEstimator",
        dt_est: "DeadTimeEstimator",
        in_near_band: bool,
        governance_decision: GovernanceDecision,
        near_band_ratio: float = 1.0,
        cycle_min: float = 10.0,
        valve_mode_enabled: bool = False,
    ) -> GainResult:
        """Calculate Kp and Ki gains based on model and conditions.
        
        Args:
            tau_reliable: Whether the time constant estimate is reliable.
            tau_min: Minimum time constant in minutes.
            estimator: ABEstimator instance with 'a' attribute (gain coefficient).
            dt_est: DeadTimeEstimator with deadtime_heat_s and deadtime_heat_reliable.
            in_near_band: Whether currently in near-band region.
            near_band_ratio: |error| / near-band width, clamped to [0, 1].
            governance_decision: Current governance decision for gain adaptation.
            cycle_min: PWM cycle duration in minutes.
            valve_mode_enabled: Whether valve-mode tuning should be applied.
            
        Returns:
            GainResult with calculated kp, ki, and their sources.
        """
        has_complete_model = (
            tau_reliable
            and tau_min > 0.0
            and estimator.a > 1e-6
            and dt_est.deadtime_heat_reliable
            and dt_est.deadtime_heat_s is not None
            and dt_est.deadtime_heat_s > 1.0
        )

        if not has_complete_model:
            kp = KP_SAFE
            ki = KI_SAFE
            kp_source = "safe"
            ki_source = "safe"
        else:
            L_min = float(dt_est.deadtime_heat_s) / 60.0
            cycle_min_eff = max(float(cycle_min), 0.0)
            deadtime_factor = (
                GAIN_LAMBDA_VALVE_DEADTIME_FACTOR
                if valve_mode_enabled
                else GAIN_LAMBDA_DEADTIME_FACTOR
            )
            lambda_min = max(
                deadtime_factor * L_min,
                GAIN_LAMBDA_CYCLE_FACTOR * cycle_min_eff,
                GAIN_LAMBDA_MIN_MIN,
            )
            kp_calc = 1.0 / (float(estimator.a) * (lambda_min + L_min))
            kp = clamp(kp_calc, KP_MIN, KP_MAX)
            ti = float(tau_min)
            ki = clamp(kp / ti, KI_MIN, KI_MAX)
            if valve_mode_enabled:
                kp_source = "imc_simc_valve"
                ki_source = "imc_simc_valve"
            else:
                kp_source = "imc_simc"
                ki_source = "imc_simc"
        
        # Apply Near Band factor for stability near setpoint
        if in_near_band:
            ratio = clamp(float(near_band_ratio), 0.0, 1.0)
            kp_taper = NEAR_BAND_TAPER_KP_MIN + (1.0 - NEAR_BAND_TAPER_KP_MIN) * ratio
            ki_taper = NEAR_BAND_TAPER_KI_MIN + (1.0 - NEAR_BAND_TAPER_KI_MIN) * ratio
            kp = clamp(kp * kp_taper, KP_MIN, KP_MAX)
            ki = clamp(min(ki * ki_taper, ki), KI_MIN, KI_MAX)
            # Mark source as near-band adjusted
            kp_source = f"{kp_source}_nearband"
            ki_source = f"{ki_source}_nearband"
        
        # Apply Governance Freeze
        if governance_decision == GovernanceDecision.HARD_FREEZE:
            kp = self._prev_kp
            ki = self._prev_ki
            kp_source = "frozen"
            ki_source = "frozen"
        elif governance_decision == GovernanceDecision.FREEZE:
            kp = self._prev_kp
            ki = self._prev_ki
            kp_source = "frozen"
            ki_source = "frozen"
        elif governance_decision == GovernanceDecision.SOFT_FREEZE_DOWN:
            kp = min(kp, self._prev_kp)
            ki = min(ki, self._prev_ki)
            kp_source = f"{kp_source}_softfreeze"
            ki_source = f"{ki_source}_softfreeze"
        
        # Store current values
        self._kp = kp
        self._ki = ki
        self._kp_source = kp_source
        self._ki_source = ki_source
        self._prev_kp = kp
        self._prev_ki = ki
        
        return GainResult(
            kp=kp,
            ki=ki,
            kp_source=self._kp_source,
            ki_source=self._ki_source,
        )

    def load_state(self, state: dict) -> None:
        """Load state from persistence dict.
        
        Args:
            state: Dictionary containing persisted state.
        """
        if not state:
            return
        
        if "kp" in state and state["kp"] is not None:
            self._kp = float(state["kp"])
            self._prev_kp = self._kp
            
        if "ki" in state and state["ki"] is not None:
            self._ki = float(state["ki"])
            self._prev_ki = self._ki
            
        if "kp_source" in state and state["kp_source"] is not None:
            self._kp_source = state["kp_source"]
            
        if "ki_source" in state and state["ki_source"] is not None:
            self._ki_source = state["ki_source"]
            
        _LOGGER.debug(
            "%s: GainScheduler loaded state: kp=%.4f, ki=%.6f",
            self._name,
            self._kp,
            self._ki,
        )

    def save_state(self) -> dict:
        """Save state to persistence dict.
        
        Returns:
            Dictionary with current state for persistence.
        """
        return {
            "kp": self._kp,
            "ki": self._ki,
            "kp_source": self._kp_source,
            "ki_source": self._ki_source,
        }

    # --- Properties for diagnostic access ---

    @property
    def kp(self) -> float:
        """Current proportional gain."""
        return self._kp

    @kp.setter
    def kp(self, value: float) -> None:
        self._kp = value

    @property
    def ki(self) -> float:
        """Current integral gain."""
        return self._ki

    @ki.setter
    def ki(self, value: float) -> None:
        self._ki = value

    @property
    def kp_source(self) -> str:
        """Source description for current Kp value."""
        return self._kp_source

    @kp_source.setter
    def kp_source(self, value: str) -> None:
        self._kp_source = value

    @property
    def ki_source(self) -> str:
        """Source description for current Ki value."""
        return self._ki_source

    @property
    def prev_kp(self) -> float:
        """Previous proportional gain (for freeze logic)."""
        return self._prev_kp

    @property
    def prev_ki(self) -> float:
        """Previous integral gain (for freeze logic)."""
        return self._prev_ki
