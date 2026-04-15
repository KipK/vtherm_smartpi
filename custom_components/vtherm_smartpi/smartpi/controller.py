from __future__ import annotations

import logging

from .const import (
    ENABLE_PROPORTIONAL_DEADZONE,
    KI_MIN,
    INTEGRAL_LEAK,
    OVERSHOOT_I_CLAMP_EPS_C,
    SETPOINT_MODE_DELTA_C,
    TRAJECTORY_I_RUN_SCALE,
    clamp
)
from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL

_LOGGER = logging.getLogger(__name__)

class SmartPIController:
    """
    PID Controller for Smart-PI.
    
    Responsible for:
    1. PID Calculation (Proportional, Integral, FF).
    2. Integral management (Anti-windup, Leaks, Clamping).
    3. Hysteresis logic (Phase 1).
    4. Output saturation handling.
    """

    def __init__(self, name: str):
        self._name = name
        
        # PI State
        self.integral: float = 0.0
        self.u_prev: float = 0.0
        self.u_ff: float = 0.0
        self.u_pi: float = 0.0
        self.u_cmd: float = 0.0     # Requested (clamped 0-1)
        self.u_hold: float = 0.0    # Held deadband power
        self.u_limited: float = 0.0 # After rate/max limit
        self.u_applied: float = 0.0 # Output after timing constraints
        
        # Diagnostics
        self.last_error: float = 0.0
        self.last_error_p: float = 0.0
        self.last_error_p_db: float = 0.0
        self.last_i_mode: str = "init"
        self.last_sat: str = "init"
        self.sat_p: bool = False
        self.sat_i: bool = False
        self.deadband_power_source: str = "none"
        self.integral_hold_mode: str = "none"

        # Hysteresis State
        self.hysteresis_state: str = "off"
        self.hysteresis_thermal_guard: bool = False

    def reset(self):
        self.integral = 0.0
        self.u_prev = 0.0
        self.u_ff = 0.0
        self.u_pi = 0.0
        self.u_hold = 0.0
        self.last_error = 0.0
        self.last_error_p = 0.0
        self.last_error_p_db = 0.0
        self.sat_p = False
        self.sat_i = False
        self.deadband_power_source = "none"
        self.integral_hold_mode = "none"
        self.hysteresis_state = "off"
        self.hysteresis_thermal_guard = False

    @property
    def integral_hold_active(self) -> bool:
        """Return True when integral protection hold is active."""
        return self.integral_hold_mode != "none"

    @property
    def servo_integral_hold(self) -> bool:
        """Compatibility alias for the legacy servo-only hold flag."""
        return self.integral_hold_mode == "servo_recovery"

    @servo_integral_hold.setter
    def servo_integral_hold(self, value: bool) -> None:
        """Map legacy servo hold toggles to the generic hold state."""
        if value:
            self.start_integral_hold("servo_recovery")
        elif self.integral_hold_mode == "servo_recovery":
            self.clear_integral_hold()

    def start_integral_hold(self, mode: str) -> None:
        """Activate the generic integral hold state."""
        if not mode or mode == "none":
            return
        self.integral_hold_mode = mode

    def clear_integral_hold(self) -> None:
        """Release the generic integral hold state."""
        self.integral_hold_mode = "none"
        
        
    def handle_setpoint_change(
        self,
        target_temp: float,
        last_target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode,
        kp: float,
        ki: float,
    ) -> tuple[float, float]:
        """Handle integral and thermal guard on setpoint changes.

        Two-tier setpoint change handling:
        - Large change (>= SETPOINT_MODE_DELTA_C): preserve the integral but
          restart the setpoint-response state (error history + recovery hold)
        - Small change: Servo response handled by P + SP filter + FF;
          integral is preserved (Åström §3.5: integral is slow memory
          for disturbance rejection, not a servo transition variable).

        Args:
            target_temp: New setpoint (°C)
            last_target_temp: Previous setpoint (°C)
            current_temp: Current indoor temperature (°C)
            hvac_mode: Current HVAC mode
            kp: Proportional gain
            ki: Integral gain

        Returns:
            Tuple of (new_error, new_error_p) to apply to main class state
        """
        if last_target_temp is None:
            return 0.0, 0.0

        sp_delta = abs(target_temp - last_target_temp)
        if sp_delta <= 0.01:
            return 0.0, 0.0

        if sp_delta >= SETPOINT_MODE_DELTA_C:
            # Large demand changes restart the setpoint-response path without
            # erasing the disturbance-rejection memory stored in the integral.
            # Force reset error state to avoid "wrong" error history (e.g. sign flip)
            new_e = float(target_temp - current_temp)
            if hvac_mode == VThermHvacMode_COOL:
                new_e = -new_e

            self.last_error = new_e
            self.last_error_p = new_e

            _LOGGER.info(
                "%s - Large setpoint change detected (Δ=%.2f°C >= %.2f°C): preserve integral %.4f and restart the servo path",
                self._name, sp_delta, SETPOINT_MODE_DELTA_C, self.integral
            )

            # Check for decrease to activate thermal guard (even on large change)
            demand_reduction = (
                (target_temp < last_target_temp and hvac_mode != VThermHvacMode_COOL)
                or (target_temp > last_target_temp and hvac_mode == VThermHvacMode_COOL)
            )
            if demand_reduction:
                self.hysteresis_thermal_guard = True
                _LOGGER.info("%s - Thermal guard activated (Large Demand Reduction)", self._name)
            else:
                self.hysteresis_thermal_guard = False

            return new_e, new_e

        # Small change: only manage thermal guard, integral is untouched
        demand_reduction = (
            (target_temp < last_target_temp and hvac_mode != VThermHvacMode_COOL)
            or (target_temp > last_target_temp and hvac_mode == VThermHvacMode_COOL)
        )

        if demand_reduction:
            self.hysteresis_thermal_guard = True
            _LOGGER.info("%s - Small demand reduction (Δ=%.2f°C): Guard ON", self._name, sp_delta)
        else:
            self.hysteresis_thermal_guard = False
            _LOGGER.info("%s - Small demand increase (Δ=%.2f°C): Guard OFF", self._name, sp_delta)

        return 0.0, 0.0

    def load_state(self, state: dict):
        if not state:
            return
        self.integral = float(state.get("integral") or 0.0)
        self.hysteresis_thermal_guard = bool(state.get("hysteresis_thermal_guard") or False)
        self.integral_hold_mode = str(state.get("integral_hold_mode") or "none")
        # Note: other internal diagnositcs not critical to restore
        
    def save_state(self) -> dict:
        return {
            "integral": self.integral,
            "hysteresis_thermal_guard": self.hysteresis_thermal_guard,
            "integral_hold_mode": self.integral_hold_mode,
        }

    def calculate_hysteresis(
        self,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode,
        hyst_upper: float,
        hyst_lower: float
    ) -> float:
        """Simple hysteresis control."""
        on_percent = 0.0
        
        if hvac_mode == VThermHvacMode_COOL:
            if current_temp <= target_temp - hyst_lower:
                on_percent = 0.0
                self.hysteresis_state = "off"
            elif current_temp >= target_temp + hyst_upper:
                on_percent = 1.0
                self.hysteresis_state = "on"
            else:
                self.hysteresis_state = "band"
                on_percent = None  # No change
        else: # HEAT
            if current_temp >= target_temp + hyst_upper:
                on_percent = 0.0
                self.hysteresis_state = "off"
            elif current_temp <= target_temp - hyst_lower:
                on_percent = 1.0
                self.hysteresis_state = "on"
            else:
                self.hysteresis_state = "band"
                on_percent = None # No change
                
        # If in band, we need to know previous state. 
        # But this function is stateless regarding previous OUTPUT, only previous HYST STATE.
        # Actually in the main class it sets self._on_percent directly.
        # We will return None if "no change"
        return on_percent

    def compute_pwm(
        self,
        error: float,
        error_p: float,
        kp: float,
        ki: float,
        u_ff: float,
        dt_min: float,
        cycle_min: float,
        in_deadband: bool,
        in_near_band: bool,
        integrator_hold: bool,
        u_db_nominal: float,
        hvac_mode: VThermHvacMode,
        current_temp: float,
        target_temp: float,
        hysteresis_thermal_guard: bool,
        is_tau_reliable: bool,
        learn_ok_count_a: int,
        deadband_c: float,
        trajectory_shaping_active: bool = False,
        core_deadband: bool | None = None,
        block_positive_integral: bool = False,
        block_negative_integral: bool = False,
        positive_integral_guard_mode: str = "recovery_guard",
        deadband_allow_p: bool = False,
    ) -> float:
        """
        Main PID Calculation logic.
        Updates self.integral, self.u_pi, self.u_cmd.
        Returns u_cmd (clamped 0-1).
        """
        self.last_error = error
        self.last_error_p = error_p
        freeze_deadband = in_deadband if core_deadband is None else core_deadband

        if self.integral_hold_active and freeze_deadband:
            self.clear_integral_hold()

        # Optional deadzone on the proportional path.
        # When disabled, the controller uses the raw proportional error everywhere.
        db_size = max(deadband_c, 0.0)

        if freeze_deadband and not deadband_allow_p:
            # Inside the configured core deadband, both P and I must be frozen.
            error_p_db = 0.0
        elif not ENABLE_PROPORTIONAL_DEADZONE:
            error_p_db = error_p
        elif abs(error_p) <= db_size:
            error_p_db = 0.0
        else:
            error_p_db = error_p - (db_size if error_p >= 0.0 else -db_size)
        self.last_error_p_db = error_p_db
        
        i_max = 2.0 / max(ki, KI_MIN)
        u_pi = 0.0
        
        # --- Pre-calculate saturation state universally ---
        u_pi_pre = kp * error_p_db + ki * self.integral
        u_raw_pre = u_ff + u_pi_pre
        
        if u_raw_pre > 1.0:
            sat = "SAT_HI"
        elif u_raw_pre < 0.0:
            sat = "SAT_LO"
        else:
            sat = "NO_SAT"
        self.last_sat = sat
        
        # P-Saturation check
        u_raw_p = u_ff + kp * error_p_db
        self.sat_p = (u_raw_p > 1.0 + 1e-9 or u_raw_p < -1e-9)
        # ------------------------------------------------
        
        if freeze_deadband:
            # Deadband mode: integral is frozen; P term is included only when deadband_allow_p is set.
            # No extra maintenance governor is applied on top of the PI state.
            self.last_i_mode = "I:FREEZE(deadband)"

            self.integral = clamp(self.integral, -i_max, i_max)

            u_pi = kp * error_p_db + ki * self.integral

            self.u_hold = u_ff + u_pi
            self.deadband_power_source = "ff_plus_pi"
        else:
            self.deadband_power_source = "none"
            if self.integral_hold_active:
                u_pi = kp * error_p_db + ki * self.integral
                self.last_i_mode = f"I:HOLD({self.integral_hold_mode})"
            elif integrator_hold:
                u_pi = kp * error_p_db + ki * self.integral
                self.last_i_mode = "I:HOLD"
            else:
                # Conditional Integration
                if (sat == "SAT_HI" and error > 0) or (sat == "SAT_LO" and error < 0):
                    self.last_i_mode = f"I:SKIP({sat})"
                    u_pi = u_pi_pre
                    self.sat_i = True
                else:
                    self.sat_i = False
                    d_integral = error * dt_min
                    self.last_i_mode = "I:RUN"

                    if block_positive_integral and d_integral > 0.0:
                        d_integral = 0.0
                        self.last_i_mode = f"I:GUARD({positive_integral_guard_mode})"
                    elif block_negative_integral and d_integral < 0.0:
                        d_integral = 0.0
                        self.last_i_mode = f"I:GUARD({positive_integral_guard_mode})"

                    if trajectory_shaping_active and d_integral > 0.0:
                        d_integral *= TRAJECTORY_I_RUN_SCALE
                        self.last_i_mode = "I:RUN(traj_track)"
                    
                    # Signed overshoot clamp — once the signed error is near/beyond
                    # zero we do not allow the integral to grow further.
                    if error <= OVERSHOOT_I_CLAMP_EPS_C:
                        if d_integral > 0.0:
                            d_integral = 0.0
                            self.last_i_mode = "I:CLAMP(near_ovr)"
                            
                    # Thermal guard after demand reduction, symmetric in signed error.
                    if hysteresis_thermal_guard:
                        if d_integral > 0.0:
                            d_integral = 0.0
                            self.last_i_mode = "I:GUARD(freeze)"
                    
                    self.integral += d_integral
                    self.integral = clamp(self.integral, -i_max, i_max)
                    u_pi = kp * error_p_db + ki * self.integral

        self.u_pi = u_pi
        self.u_ff = u_ff

        if freeze_deadband:
            # Explicit hold using Frozen PI: `u_ff + u_pi`.
            # FF2 trim learns from the slope slowly.
            u_raw = u_ff + u_pi
        else:
            self.u_hold = 0.0
            u_raw = u_ff + u_pi

        self.u_cmd = clamp(u_raw, 0.0, 1.0)

        return self.u_cmd

    def finalize_cycle(
        self,
        u_limited: float,
        u_applied: float,
    ) -> None:
        """Record the latest output without committing u_prev."""
        self.u_applied = u_applied
        self.u_limited = u_limited
