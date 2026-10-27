from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .const import (
    KI_MIN,
    OVERSHOOT_I_CLAMP_EPS_C,
    clamp
)
from .deadband_output import ProportionalState
from .integral_guard import constrain_guarded_integral_delta
from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL

# Integral modes in which the PI drives the actuator, so its integral tracks output limits.
_TRACKED_I_MODES = ("I:RUN", "I:SKIP", "I:GUARD", "I:CLAMP")


@dataclass(frozen=True)
class IntegralPowerTransferResult:
    """Result of moving one signed power contribution out of the integral."""

    applied: bool
    reason: str
    requested_power: float
    applied_power: float
    old_i_power: float
    new_i_power: float


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
        self.u_p: float = 0.0
        self.u_i: float = 0.0
        self.u_pi: float = 0.0
        self.u_cmd: float = 0.0     # Requested (clamped 0-1)
        self.u_hold: float = 0.0    # Held deadband power
        self.u_limited: float = 0.0 # After rate/max limit
        self.u_applied: float = 0.0 # Output after timing constraints
        
        # Diagnostics
        self.last_error_p_db: float = 0.0
        self.last_i_mode: str = "init"
        self.last_integral_hold_source: str = "none"
        self.last_sat: str = "init"
        self.sat_p: bool = False
        self.sat_i: bool = False
        self.deadband_power_source: str = "none"
        self.deadband_p_mode: str = "init"
        self.integral_hold_mode: str = "none"
        self._deadband_edge_count: int = 0
        self._deadband_edge_sign: float | None = None
        self.last_aw_du: float = 0.0  # tracked excess of the last tick (<= 0)

        # Hysteresis State
        self.hysteresis_state: str = "off"

    def reset(self):
        self.integral = 0.0
        self.u_prev = 0.0
        self.u_ff = 0.0
        self.u_p = 0.0
        self.u_i = 0.0
        self.u_pi = 0.0
        self.u_hold = 0.0
        self.last_error_p_db = 0.0
        self.sat_p = False
        self.sat_i = False
        self.deadband_power_source = "none"
        self.deadband_p_mode = "init"
        self.integral_hold_mode = "none"
        self.last_integral_hold_source = "none"
        self._deadband_edge_count = 0
        self._deadband_edge_sign = None
        self.last_aw_du = 0.0
        self.hysteresis_state = "off"

    @property
    def integral_hold_active(self) -> bool:
        """Return True when integral protection hold is active."""
        return self.integral_hold_mode != "none"

    def start_integral_hold(self, mode: str) -> None:
        """Activate the generic integral hold state."""
        if not mode or mode == "none":
            return
        self.integral_hold_mode = mode

    def clear_integral_hold(self) -> None:
        """Release the generic integral hold state."""
        self.integral_hold_mode = "none"
        
        
    def load_state(self, state: dict):
        if not state:
            return
        self.integral = float(state.get("integral") or 0.0)
        self.last_integral_hold_source = "none"
        # Note: other internal diagnositcs not critical to restore
        
    def save_state(self) -> dict:
        return {
            "integral": self.integral,
        }

    def calculate_hysteresis(
        self,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode,
        hyst_off: float,
        hyst_on: float
    ) -> float:
        """Simple hysteresis control.

        HEAT: ON at SP - hyst_on, OFF at SP + hyst_off.
        COOL (exact mirror): ON at SP + hyst_on, OFF at SP - hyst_off.
        """
        on_percent = 0.0
        
        if hvac_mode == VThermHvacMode_COOL:
            if current_temp <= target_temp - hyst_off:
                on_percent = 0.0
                self.hysteresis_state = "off"
            elif current_temp >= target_temp + hyst_on:
                on_percent = 1.0
                self.hysteresis_state = "on"
            else:
                self.hysteresis_state = "band"
                on_percent = None  # No change
        else: # HEAT
            if current_temp >= target_temp + hyst_off:
                on_percent = 0.0
                self.hysteresis_state = "off"
            elif current_temp <= target_temp - hyst_on:
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
        in_deadband: bool,
        integrator_hold: bool,
        deadband_c: float,
        core_deadband: bool | None = None,
        block_positive_integral: bool = False,
        block_negative_integral: bool = False,
        positive_integral_guard_mode: str = "recovery_guard",
        allow_guard_unwind: bool = True,
        deadband_allow_p: bool = False,
        integrator_hold_source: str = "external",
        u_max: float = 1.0,
        unwind_band_c: float | None = None,
    ) -> float:
        """
        Main PID Calculation logic.
        Updates self.integral, self.u_pi, self.u_cmd.
        Returns u_cmd (clamped 0-1). Conditional integration treats `u_max`,
        the static upper output limit in linear demand space, as saturation.
        Within `unwind_band_c` of the setpoint (about one sensor step), when
        the output without the proportional term stays within its limits, the
        saturation comes from the proportional response to one sensor step,
        not from a transient or an actuator limit: conditional integration
        still lets the integral unwind toward zero there, so that a quantized
        measurement does not freeze a steady offset (deadband on one level,
        saturation on the next).
        """
        self.last_integral_hold_source = "none"
        self.sat_i = False
        freeze_deadband = in_deadband if core_deadband is None else core_deadband

        if self.integral_hold_active and freeze_deadband:
            self.clear_integral_hold()

        p_state = ProportionalState(
            deadband_c=deadband_c,
            freeze_deadband=freeze_deadband,
            deadband_allow_p=deadband_allow_p,
            edge_count=self._deadband_edge_count,
            edge_sign=self._deadband_edge_sign,
        )
        (
            error_p_db,
            self.deadband_p_mode,
            self._deadband_edge_count,
            self._deadband_edge_sign,
        ) = p_state.project(error_p)
        self.last_error_p_db = error_p_db
        
        i_max = 2.0 / max(ki, KI_MIN)
        u_pi = 0.0
        
        # --- Pre-calculate saturation state universally ---
        u_pi_pre = kp * error_p_db + ki * self.integral
        u_raw_pre = u_ff + u_pi_pre
        
        if u_raw_pre > u_max:
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
            # Deadband mode: integral is frozen; P term is damped near the edge when allowed.
            # No extra maintenance logic is applied on top of the PI state.
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
                self.last_integral_hold_source = self.integral_hold_mode
            elif integrator_hold:
                u_pi = kp * error_p_db + ki * self.integral
                self.last_i_mode = "I:HOLD"
                self.last_integral_hold_source = str(integrator_hold_source)
            else:
                # Conditional Integration
                near_setpoint = unwind_band_c is not None and 0.0 < abs(error) <= unwind_band_c
                saturated_against = (sat == "SAT_HI" and error > 0) or (
                    sat == "SAT_LO" and error < 0
                )
                # One sensor step from the setpoint, a saturation produced by
                # the proportional term alone may still unwind the integral
                # toward zero (never across it): winding further into the
                # saturation stays blocked, and an output held at its limit by
                # FF + I keeps the integral, which then carries the model bias.
                u_without_p = u_ff + ki * self.integral
                saturated_by_p = 0.0 <= u_without_p <= u_max
                unwind_only = (
                    saturated_against
                    and near_setpoint
                    and saturated_by_p
                    and self.integral * error < 0.0
                )
                if saturated_against and not unwind_only:
                    self.last_i_mode = f"I:SKIP({sat})"
                    u_pi = u_pi_pre
                    self.sat_i = True
                else:
                    d_integral = error * dt_min
                    self.last_i_mode = "I:RUN"
                    if unwind_only:
                        d_integral = (
                            max(d_integral, -self.integral)
                            if self.integral > 0.0
                            else min(d_integral, -self.integral)
                        )
                        self.last_i_mode = f"I:UNWIND({sat})"

                    d_integral, guard_delta_mode = constrain_guarded_integral_delta(
                        integral=self.integral,
                        requested_delta=d_integral,
                        block_positive=block_positive_integral,
                        block_negative=block_negative_integral,
                        allow_unwind=allow_guard_unwind,
                    )
                    if guard_delta_mode == "blocked":
                        self.last_i_mode = f"I:GUARD({positive_integral_guard_mode})"
                    elif guard_delta_mode == "unwind":
                        self.last_i_mode = f"I:GUARD_UNWIND({positive_integral_guard_mode})"

                    # Signed overshoot clamp — once the signed error is near/beyond
                    # zero we do not allow the integral to grow further.
                    if error <= OVERSHOOT_I_CLAMP_EPS_C:
                        if d_integral > 0.0:
                            d_integral = 0.0
                            self.last_i_mode = "I:CLAMP(near_ovr)"
                            
                    self.integral += d_integral
                    self.integral = clamp(self.integral, -i_max, i_max)
                    u_pi = kp * error_p_db + ki * self.integral

        self.u_p = kp * error_p_db
        self.u_i = ki * self.integral
        self.u_pi = u_pi
        self.u_ff = u_ff

        if not freeze_deadband:
            self.u_hold = 0.0

        # In deadband the explicit hold is the frozen PI: `u_ff + u_pi`.
        self.u_cmd = clamp(u_ff + u_pi, 0.0, 1.0)

        return self.u_cmd

    def track_output_limit(
        self,
        u_max: float,
        kp: float,
        ki: float,
        dt_min: float,
    ) -> float:
        """Back-calculate the integral toward a known upper limit below 1.

        The limit is `u_max` (max_on_percent in linear space). The excess of
        this tick's PI request over it drains the integral with Tt = Ti = Kp/Ki,
        down to zero at most: an excess carried by the proportional term alone
        does not wind the integral in the opposite direction. The [0, 1]
        saturation stays with conditional integration. Only the integral
        moves: u_i keeps the decomposition that produced the command.
        Returns the tracked excess (<= 0).
        """
        self.last_aw_du = 0.0
        if dt_min <= 0.0 or not self.last_i_mode.startswith(_TRACKED_I_MODES):
            return 0.0
        requested = clamp(self.u_ff + self.u_pi, 0.0, 1.0)
        du = min(requested, u_max) - requested
        if du >= 0.0:
            return 0.0
        self._back_calculate(du, kp, ki, dt_min)
        self.last_aw_du = du
        return du

    def _back_calculate(self, du: float, kp: float, ki: float, dt_min: float) -> None:
        """Move the integral by beta * du / Ki toward zero only, never across it."""
        ki_eff = max(ki, KI_MIN)
        tt_min = max(kp, 1e-6) / ki_eff
        beta = dt_min / max(tt_min, dt_min)
        delta = beta * du / ki_eff
        if self.integral * delta >= 0.0:
            return
        integral = self.integral + delta
        self.integral = 0.0 if integral * self.integral < 0.0 else integral

    def apply_integral_power_transfer(
        self,
        *,
        ki: float,
        requested_power: float,
    ) -> IntegralPowerTransferResult:
        """Remove a signed power contribution from the integral atomically.

        A positive request removes positive integral power. A negative request
        removes negative integral power. The operation may only move the
        integral contribution toward zero and never crosses it.
        """
        if not isfinite(float(ki)) or float(ki) < KI_MIN:
            return IntegralPowerTransferResult(
                False,
                "invalid_ki",
                float(requested_power),
                0.0,
                0.0,
                0.0,
            )
        if not isfinite(float(requested_power)):
            return IntegralPowerTransferResult(
                False,
                "invalid_i_transfer",
                float(requested_power),
                0.0,
                float(ki) * self.integral,
                float(ki) * self.integral,
            )

        ki_value = float(ki)
        requested = float(requested_power)
        old_i_power = ki_value * self.integral
        if abs(requested) <= 1e-12:
            return IntegralPowerTransferResult(
                True,
                "no_i_transfer",
                requested,
                0.0,
                old_i_power,
                old_i_power,
            )
        if old_i_power * requested <= 0.0:
            return IntegralPowerTransferResult(
                False,
                "i_transfer_sign",
                requested,
                0.0,
                old_i_power,
                old_i_power,
            )
        if abs(requested) > abs(old_i_power) + 1e-9:
            return IntegralPowerTransferResult(
                False,
                "i_transfer_capacity",
                requested,
                0.0,
                old_i_power,
                old_i_power,
            )

        new_i_power = old_i_power - requested
        if old_i_power * new_i_power < -1e-12:
            return IntegralPowerTransferResult(
                False,
                "i_transfer_crosses_zero",
                requested,
                0.0,
                old_i_power,
                old_i_power,
            )

        new_integral = new_i_power / ki_value
        i_max = 2.0 / ki_value
        if not isfinite(new_integral) or not -i_max <= new_integral <= i_max:
            return IntegralPowerTransferResult(
                False,
                "i_transfer_clamped",
                requested,
                0.0,
                old_i_power,
                old_i_power,
            )

        self.integral = new_integral
        self.u_i = new_i_power
        self.u_pi = self.u_p + self.u_i
        if self.last_i_mode == "I:FREEZE(deadband)":
            self.u_hold = self.u_ff + self.u_pi
        return IntegralPowerTransferResult(
            True,
            "i_transfer_applied",
            requested,
            requested,
            old_i_power,
            new_i_power,
        )

    def finalize_cycle(
        self,
        u_limited: float,
        u_applied: float,
    ) -> None:
        """Record the latest output without committing u_prev."""
        self.u_applied = u_applied
        self.u_limited = u_limited
