"""
Smart-PI Gain Scheduler Module.

Manages Kp/Ki gain calculation with IMC-based tuning and near-band adjustments.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from .const import (
    GAIN_LAMBDA_CYCLE_FACTOR,
    GAIN_LAMBDA_DEADTIME_FACTOR,
    GAIN_LAMBDA_MIN_MIN,
    GAIN_LAMBDA_VALVE_DEADTIME_FACTOR,
    GAIN_TI_LAMBDA_FACTOR,
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


def imc_simc_gains(
    a_abs: float,
    deadtime_s: float,
    tau_min: float,
    cycle_min: float,
    valve_mode_enabled: bool = False,
) -> tuple[float, float]:
    """Kp and Ki of the IMC/SIMC rule for a heating rate ``a_abs`` (C/min at full power), a dead time
    ``deadtime_s`` and a time constant ``tau_min`` (``math.inf`` when no loss coefficient is known: the integral
    time is then the bound of a slow room, 4 (lambda + L))."""
    L_min = float(deadtime_s) / 60.0
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
    kp_calc = 1.0 / (abs(float(a_abs)) * (lambda_min + L_min))
    kp = clamp(kp_calc, KP_MIN, KP_MAX)
    # SIMC (Skogestad 2003): the integral time of a lag-dominant
    # (integrating) process is bounded by 4 (lambda + L), so that load
    # disturbances are rejected within the closed-loop time scale
    # instead of the room time constant.
    ti = min(float(tau_min), GAIN_TI_LAMBDA_FACTOR * (lambda_min + L_min))
    ki = clamp(kp / ti, KI_MIN, KI_MAX)
    return kp, ki


def pre_model_gains(
    a_abs: float,
    deadtime_s: float,
    cycle_min: float,
    valve_mode_enabled: bool = False,
) -> tuple[float, float]:
    """Gains of the learning stage from a measured heating rate and dead time, without a loss coefficient: the
    rule of a slow room, neither gain above its fallback value."""
    kp, ki = imc_simc_gains(a_abs, deadtime_s, math.inf, cycle_min, valve_mode_enabled)
    return min(kp, KP_SAFE), min(ki, KI_SAFE)


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
        self._origin = "fallback"

    def reset(self) -> None:
        """Reset gains to safe defaults."""
        self._kp = KP_SAFE
        self._ki = KI_SAFE
        self._kp_source = "safe"
        self._ki_source = "safe"
        self._origin = "fallback"
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
        hvac_mode: VThermHvacMode = VThermHvacMode_HEAT,
        pre_model_a: float | None = None,
    ) -> GainResult:
        """Calculate Kp and Ki gains based on model and conditions.
        
        Args:
            tau_reliable: Whether the time constant estimate is reliable.
            tau_min: Minimum time constant in minutes.
            estimator: ABEstimator instance with 'a' attribute (gain coefficient).
            dt_est: DeadTimeEstimator with physical heat/cool response dead times.
            in_near_band: Whether currently in near-band region.
            near_band_ratio: |error| / near-band width, clamped to [0, 1].
            governance_decision: Current governance decision for gain adaptation.
            cycle_min: PWM cycle duration in minutes.
            valve_mode_enabled: Whether valve-mode tuning should be applied.
            hvac_mode: Active mono-mode HVAC contract for A and dead time.
            pre_model_a: Heating rate (C/min at full power) measured before the first model is published;
                with a reliable dead time after a start it gives the gains of the learning stage
                (``pre_model_gains``) in place of the fallback ones. None: no such rate.
            
        Returns:
            GainResult with calculated kp, ki, and their sources.
        """
        if hvac_mode == VThermHvacMode_COOL:
            active_deadtime_s = dt_est.deadtime_cool_s
            active_deadtime_reliable = dt_est.deadtime_cool_reliable
            model_sign_valid = estimator.a < -1e-6
        else:
            active_deadtime_s = dt_est.deadtime_heat_s
            active_deadtime_reliable = dt_est.deadtime_heat_reliable
            model_sign_valid = estimator.a > 1e-6

        has_complete_model = (
            tau_reliable
            and tau_min > 0.0
            and model_sign_valid
            and active_deadtime_reliable
            and active_deadtime_s is not None
            and active_deadtime_s > 1.0
        )

        pre_model_used = (
            not has_complete_model
            and pre_model_a is not None
            and pre_model_a > 0.0
            and active_deadtime_reliable
            and active_deadtime_s is not None
            and active_deadtime_s > 1.0
        )
        if pre_model_used:
            kp, ki = pre_model_gains(pre_model_a, float(active_deadtime_s), cycle_min, valve_mode_enabled)
            kp_source = "measurement_pulses"
            ki_source = "measurement_pulses"
        elif not has_complete_model:
            kp = KP_SAFE
            ki = KI_SAFE
            kp_source = "safe"
            ki_source = "safe"
        else:
            kp, ki = imc_simc_gains(
                estimator.a, float(active_deadtime_s), tau_min, cycle_min, valve_mode_enabled
            )
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
            ki = clamp(ki * ki_taper, KI_MIN, KI_MAX)
            # Mark source as near-band adjusted
            kp_source = f"{kp_source}_nearband"
            ki_source = f"{ki_source}_nearband"
        
        # The learning stage holds the gains only while it has nothing to adapt them to
        if pre_model_used and governance_decision == GovernanceDecision.FREEZE:
            governance_decision = GovernanceDecision.ADAPT_ON

        # Apply Governance Freeze against the previously applied gains
        if governance_decision in (GovernanceDecision.HARD_FREEZE, GovernanceDecision.FREEZE):
            kp = self._kp
            ki = self._ki
            kp_source = "frozen"
            ki_source = "frozen"
        elif governance_decision == GovernanceDecision.SOFT_FREEZE_DOWN:
            kp = min(kp, self._kp)
            ki = min(ki, self._ki)
            kp_source = f"{kp_source}_softfreeze"
            ki_source = f"{ki_source}_softfreeze"
        
        if governance_decision not in (GovernanceDecision.HARD_FREEZE, GovernanceDecision.FREEZE):
            self._origin = "pulse" if pre_model_used else ("model" if has_complete_model else "fallback")

        # Store current values
        self._kp = kp
        self._ki = ki
        self._kp_source = kp_source
        self._ki_source = ki_source
        
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
            
        if "ki" in state and state["ki"] is not None:
            self._ki = float(state["ki"])
            
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
    def origin(self) -> str:
        """What the applied gains come from: "fallback" (safe values), "pulse" (heating rate of a measurement
        pulse, before the first model) or "model" (complete model). A frozen gain keeps the origin it had."""
        return self._origin

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
