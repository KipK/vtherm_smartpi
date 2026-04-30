"""Setpoint management for Smart-PI late-braking trajectory shaping."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from math import exp
from typing import Optional

from .const import (
    LANDING_ENABLE_ERROR_THRESHOLD_C,
    LANDING_MIN_HORIZON_MIN,
    LANDING_NON_CONSTRAINING_PERSISTENCE,
    LANDING_RELEASE_SLOPE_H,
    LANDING_RELEASE_TIME_TO_DEADTIME_RATIO,
    LANDING_RELEASE_TIME_TO_TARGET_EPS_MIN,
    LANDING_SAFETY_MARGIN_C,
    LANDING_U_EPS,
    SETPOINT_BOOST_THRESHOLD,
    SETPOINT_BOOST_ERROR_MIN,
    ServoPhase,
    TrajectoryPhase,
    TRAJECTORY_ENABLE_ERROR_THRESHOLD,
    TRAJECTORY_COMPLETE_EPS_C,
    TRAJECTORY_BRAKE_GAIN,
    TRAJECTORY_BRAKE_RELEASE_HYST_C,
    TRAJECTORY_BUMPLESS_MAX_U_DELTA,
    TRAJECTORY_MIN_P_ERROR_RATIO,
    TRAJECTORY_RELEASE_TAU_FACTOR as DEFAULT_RELEASE_TAU_FACTOR,
    clamp,
)
from .trajectory import SmartPITrajectoryGenerator
from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL

_LOGGER = logging.getLogger(__name__)


def _signed_delta(value: float, reference: float, hvac_mode: VThermHvacMode | None) -> float:
    """Return the signed demand delta in the active HVAC direction."""
    delta = value - reference
    if hvac_mode == VThermHvacMode_COOL:
        return -delta
    return delta


@dataclass(slots=True)
class SetpointLandingDecision:
    """Setpoint-response landing decision in internal linear command space."""

    active: bool = False
    reason: str = "inactive"
    u_cap: float | None = None
    sp_for_p_cap: float | None = None
    predicted_temperature: float | None = None
    predicted_rise: float | None = None
    target_margin: float | None = None
    coast_required: bool = False
    release_allowed: bool = True
    u_cmd_before_cap: float | None = None
    u_cmd_after_cap: float | None = None


class SmartPISetpointManager:
    """Setpoint orchestration for Smart-PI.

    The manager keeps the raw setpoint for the integral path and produces a
    filtered setpoint for the proportional path through an analytical
    late-braking trajectory generator.
    """

    def __init__(self, name: str, enabled: bool = True,
                 release_tau_factor: float = DEFAULT_RELEASE_TAU_FACTOR):
        self._name = name
        self.enabled = enabled
        self._release_tau_factor = max(float(release_tau_factor), 0.01)

        # Tracks the latest raw target for change detection
        self.filtered_setpoint: Optional[float] = None
        # Actual SP_for_P value returned to the controller
        self.effective_setpoint: Optional[float] = None
        # Raw user target used to evaluate true target changes
        self._last_user_target_temp: Optional[float] = None

        # Boost state
        self.boost_active: bool = False
        self.prev_setpoint_for_boost: Optional[float] = None

        # Trajectory state
        self._trajectory = SmartPITrajectoryGenerator()
        self._pending_target_change_braking: bool = False
        self._last_model_ready: bool = False
        self._last_braking_needed: bool = False
        self._last_remaining_cycle_min: float = 0.0
        self._last_next_cycle_u_ref: float = 0.0
        self._last_bumpless_u_delta: float | None = None
        self._last_bumpless_ready: bool | None = None
        self._trajectory_source: str = "none"

        # Setpoint landing state (HEAT-only, transient — never persisted)
        self._landing_decision = SetpointLandingDecision()
        self._landing_residual_released: bool = False
        self._landing_non_constraining_count: int = 0
        self._landing_time_to_target_min: float | None = None
        self._landing_release_blocked_by_slope: bool = False

    @property
    def trajectory_active(self) -> bool:
        """True when the analytical trajectory is shaping SP_for_P."""
        return self._trajectory.active

    @property
    def trajectory_phase(self) -> TrajectoryPhase:
        """Current trajectory phase."""
        return self._trajectory.phase

    @property
    def trajectory_start_setpoint(self) -> float | None:
        """Starting point of the active trajectory."""
        return self._trajectory.start_setpoint

    @property
    def trajectory_target_setpoint(self) -> float | None:
        """Final target of the active trajectory."""
        return self._trajectory.target_setpoint

    @property
    def trajectory_tau_ref_min(self) -> float | None:
        """Reference time constant of the active trajectory."""
        return self._trajectory.tau_ref_min

    @property
    def trajectory_elapsed_s(self) -> float:
        """Elapsed runtime of the active trajectory."""
        return self._trajectory.elapsed_s

    @property
    def trajectory_pending_target_change_braking(self) -> bool:
        """True when a previous setpoint increase still arms late braking."""
        return self._pending_target_change_braking

    @property
    def trajectory_model_ready(self) -> bool:
        """Latest model readiness flag used by the trajectory manager."""
        return self._last_model_ready

    @property
    def trajectory_braking_needed(self) -> bool:
        """Latest braking-needed decision."""
        return self._last_braking_needed

    @property
    def trajectory_remaining_cycle_min(self) -> float:
        """Latest remaining cycle latency used by the braking prediction."""
        return self._last_remaining_cycle_min

    @property
    def trajectory_next_cycle_u_ref(self) -> float:
        """Latest expected next-cycle power used by the braking prediction."""
        return self._last_next_cycle_u_ref

    @property
    def trajectory_bumpless_u_delta(self) -> float | None:
        """Latest proportional command step evaluated for the release handoff."""
        return self._last_bumpless_u_delta

    @property
    def trajectory_bumpless_ready(self) -> bool | None:
        """Latest bumpless readiness decision for leaving the trajectory."""
        return self._last_bumpless_ready

    @property
    def trajectory_source(self) -> str:
        """Source of the currently active trajectory."""
        return self._trajectory_source

    # --- Setpoint landing properties ---
    @property
    def landing_active(self) -> bool:
        return self._landing_decision.active

    @property
    def landing_reason(self) -> str:
        return self._landing_decision.reason

    @property
    def landing_u_cap(self) -> float | None:
        return self._landing_decision.u_cap

    @property
    def landing_sp_for_p_cap(self) -> float | None:
        return self._landing_decision.sp_for_p_cap

    @property
    def landing_predicted_temperature(self) -> float | None:
        return self._landing_decision.predicted_temperature

    @property
    def landing_predicted_rise(self) -> float | None:
        return self._landing_decision.predicted_rise

    @property
    def landing_target_margin(self) -> float | None:
        return self._landing_decision.target_margin

    @property
    def landing_coast_required(self) -> bool:
        return self._landing_decision.coast_required

    @property
    def landing_release_allowed(self) -> bool:
        return self._landing_decision.release_allowed

    @property
    def landing_non_constraining_count(self) -> int:
        return self._landing_non_constraining_count

    @property
    def landing_time_to_target_min(self) -> float | None:
        return self._landing_time_to_target_min

    @property
    def landing_release_blocked_by_slope(self) -> bool:
        return self._landing_release_blocked_by_slope

    @property
    def landing_u_cmd_before_cap(self) -> float | None:
        return self._landing_decision.u_cmd_before_cap

    @property
    def landing_u_cmd_after_cap(self) -> float | None:
        return self._landing_decision.u_cmd_after_cap

    # Legacy aliases kept for compatibility with older tests / consumers.
    @property
    def servo_active(self) -> bool:
        return self.trajectory_active

    @property
    def servo_phase(self) -> ServoPhase:
        return ServoPhase.BOOST if self.trajectory_active else ServoPhase.NONE

    @property
    def servo_target_at_activation(self) -> float | None:
        return self.trajectory_start_setpoint

    @property
    def servo_step_amplitude(self) -> float | None:
        if self.trajectory_start_setpoint is None or self.trajectory_target_setpoint is None:
            return None
        return abs(self.trajectory_target_setpoint - self.trajectory_start_setpoint)

    @property
    def servo_landing_zone(self) -> None:
        return None

    def _reset_trajectory(self) -> None:
        """Reset the trajectory state only."""
        self._trajectory.reset()
        self._trajectory_source = "none"
        self._landing_decision = SetpointLandingDecision()
        self._landing_residual_released = False
        self._landing_non_constraining_count = 0
        self._reset_landing_release_safety()

    def _clear_pending_target_change_braking(self) -> None:
        """Forget any delayed braking request inherited from a setpoint increase."""
        self._pending_target_change_braking = False

    def set_passthrough(self, target_temp: float) -> None:
        """Force direct setpoint passthrough and clear trajectory state."""
        self.filtered_setpoint = target_temp
        self.effective_setpoint = target_temp
        self._reset_trajectory()

    def arm_pending_braking_after_resume(
        self,
        *,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode | None,
    ) -> None:
        """Keep late braking armed after an OFF resume when demand stays significant."""
        if _signed_delta(target_temp, current_temp, hvac_mode) >= TRAJECTORY_ENABLE_ERROR_THRESHOLD:
            self._pending_target_change_braking = True

    def reset(self):
        """Reset internal state."""
        self.filtered_setpoint = None
        self.effective_setpoint = None
        self._last_user_target_temp = None
        self.boost_active = False
        self.prev_setpoint_for_boost = None
        self._last_model_ready = False
        self._last_braking_needed = False
        self._last_remaining_cycle_min = 0.0
        self._last_next_cycle_u_ref = 0.0
        self._last_bumpless_u_delta = None
        self._last_bumpless_ready = None
        self._trajectory_source = "none"
        self._clear_pending_target_change_braking()
        self._reset_trajectory()

    def clear_runtime_transients(self) -> None:
        """Drop transient trajectory state that cannot survive a reboot safely."""
        self._last_model_ready = False
        self._last_braking_needed = False
        self._last_remaining_cycle_min = 0.0
        self._last_next_cycle_u_ref = 0.0
        self._last_bumpless_u_delta = None
        self._last_bumpless_ready = None
        self._clear_pending_target_change_braking()
        self._reset_trajectory()
        if self.effective_setpoint is not None:
            self.filtered_setpoint = self.effective_setpoint
        elif self.filtered_setpoint is not None:
            self.effective_setpoint = self.filtered_setpoint

    def load_state(self, state: dict):
        """Load state from persistence."""
        if not state:
            return

        fs = state.get("filtered_setpoint")
        if fs is not None:
            self.filtered_setpoint = float(fs)

        es = state.get("effective_setpoint")
        if es is not None:
            self.effective_setpoint = float(es)

        ut = state.get("last_user_target_temp")
        if ut is not None:
            self._last_user_target_temp = float(ut)

        self.boost_active = bool(state.get("setpoint_boost_active", False))

        ps = state.get("prev_setpoint_for_boost")
        if ps is not None:
            self.prev_setpoint_for_boost = float(ps)

        self._pending_target_change_braking = bool(
            state.get("trajectory_pending_target_change_braking", False)
        )
        self._trajectory_source = str(state.get("trajectory_source", "none"))
        self._trajectory.load_state(state)

    def save_state(self) -> dict:
        """Save state for persistence."""
        return {
            "filtered_setpoint": self.filtered_setpoint,
            "effective_setpoint": self.effective_setpoint,
            "last_user_target_temp": self._last_user_target_temp,
            "setpoint_boost_active": self.boost_active,
            "prev_setpoint_for_boost": self.prev_setpoint_for_boost,
            "trajectory_pending_target_change_braking": self._pending_target_change_braking,
            "trajectory_source": self._trajectory_source,
            # Legacy aliases kept during the transition to trajectory naming.
            "servo_active": self.servo_active,
            "servo_phase": self.servo_phase.value,
            "servo_target_at_activation": self.servo_target_at_activation,
            "servo_step_amplitude": self.servo_step_amplitude,
            **self._trajectory.save_state(),
        }

    def _compute_predicted_signed_change(
        self,
        *,
        current_temp: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode | None,
        a: float,
        b: float,
        u_ref: float,
        horizon_min: float,
        next_u_ref: float = 0.0,
        next_horizon_min: float = 0.0,
    ) -> float | None:
        """Return the exact signed thermal change predicted by the 1R1C model."""
        if ext_current_temp is None or b <= 0.0:
            return None

        # Passive cooling: no active refrigeration (a >= 0 in cool mode)
        if hvac_mode == VThermHvacMode_COOL and a >= 0.0:
            total_horizon_min = max(float(horizon_min), 0.0) + max(float(next_horizon_min), 0.0)
            if total_horizon_min <= 0.0:
                return None

            alpha = 1.0 - exp(-b * total_horizon_min)
            if alpha <= 0.0:
                return None

            predicted_change = (current_temp - ext_current_temp) * alpha
            return predicted_change if predicted_change > 0.0 else None

        # Active mode: full 1R1C model (HEAT with a > 0, COOL with a < 0)
        if hvac_mode != VThermHvacMode_COOL and a <= 0.0:
            return None

        predicted_temp = current_temp

        current_horizon_min = max(float(horizon_min), 0.0)
        if current_horizon_min > 0.0:
            alpha_current = exp(-b * current_horizon_min)
            steady_state_temp = ext_current_temp + (a * max(float(u_ref), 0.0)) / b
            predicted_temp = steady_state_temp + (predicted_temp - steady_state_temp) * alpha_current

        next_horizon_min = max(float(next_horizon_min), 0.0)
        if next_horizon_min > 0.0:
            alpha_next = exp(-b * next_horizon_min)
            steady_state_next = ext_current_temp + (a * max(float(next_u_ref), 0.0)) / b
            predicted_temp = steady_state_next + (predicted_temp - steady_state_next) * alpha_next

        predicted_change = predicted_temp - current_temp
        # In COOL mode the temperature drops; return the demand-direction magnitude
        if hvac_mode == VThermHvacMode_COOL:
            predicted_change = -predicted_change
        return predicted_change if predicted_change > 0.0 else None

    @staticmethod
    def _minimum_signed_p_error(
        *,
        signed_error: float,
        deadband_c: float,
    ) -> float:
        """Return the minimum positive signed P error kept during braking."""
        return max(
            TRAJECTORY_COMPLETE_EPS_C,
            max(deadband_c, 0.0),
            TRAJECTORY_MIN_P_ERROR_RATIO * signed_error,
        )

    @staticmethod
    def _signed_brake_target(
        *,
        target_temp: float,
        current_temp: float,
        braking_gap: float,
        min_signed_p_error: float,
        hvac_mode: VThermHvacMode | None,
    ) -> float:
        """Return a late-braking target that never inverts the signed P error."""
        if hvac_mode == VThermHvacMode_COOL:
            return max(
                target_temp,
                min(
                    target_temp + braking_gap,
                    current_temp - min_signed_p_error,
                ),
            )
        return min(
            target_temp,
            max(
                target_temp - braking_gap,
                current_temp + min_signed_p_error,
            ),
        )

    @staticmethod
    def _apply_signed_deadzone(signed_error: float, deadband_c: float) -> float:
        """Mirror the controller deadzone on the proportional path."""
        db_size = max(deadband_c, 0.0)
        if abs(signed_error) <= db_size:
            return 0.0
        return signed_error - (db_size if signed_error >= 0.0 else -db_size)

    def _bumpless_release_ready(
        self,
        *,
        target_temp: float,
        sp_for_p: float,
        current_temp: float,
        hvac_mode: VThermHvacMode | None,
        deadband_c: float,
        kp: float | None,
    ) -> bool:
        """Return True once releasing to the raw target is command-continuous enough."""
        if kp is None:
            return True

        raw_signed_error_p = _signed_delta(target_temp, current_temp, hvac_mode)
        filtered_signed_error_p = _signed_delta(sp_for_p, current_temp, hvac_mode)

        raw_error_p_db = self._apply_signed_deadzone(raw_signed_error_p, deadband_c)
        filtered_error_p_db = self._apply_signed_deadzone(filtered_signed_error_p, deadband_c)

        u_delta = abs(float(kp)) * abs(raw_error_p_db - filtered_error_p_db)
        self._last_bumpless_u_delta = u_delta
        self._last_bumpless_ready = u_delta <= TRAJECTORY_BUMPLESS_MAX_U_DELTA
        return self._last_bumpless_ready

    def _is_setpoint_release_locked(self) -> bool:
        """Return True when a setpoint trajectory must stay in release."""
        return (
            self.trajectory_active
            and self._trajectory_source == "setpoint"
            and self.trajectory_phase == TrajectoryPhase.RELEASE
        )

    @staticmethod
    def _temperature_release_ready(
        *,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode | None,
    ) -> bool:
        """Return True once the measured temperature is close enough to target."""
        return _signed_delta(target_temp, current_temp, hvac_mode) <= TRAJECTORY_COMPLETE_EPS_C

    @staticmethod
    def _predict_heat_temperature(
        *,
        current_temp: float,
        ext_current_temp: float,
        a: float,
        b: float,
        u_ref: float,
        horizon_min: float,
    ) -> tuple[float, float, float]:
        """Return predicted temperature, passive term, and command gain."""
        horizon = max(float(horizon_min), LANDING_MIN_HORIZON_MIN)
        alpha = exp(-b * horizon)
        passive = ext_current_temp + (current_temp - ext_current_temp) * alpha
        gain_u = (a / b) * (1.0 - alpha)
        predicted = passive + gain_u * max(float(u_ref), 0.0)
        return predicted, passive, gain_u

    def _reset_landing_release_safety(self) -> None:
        self._landing_time_to_target_min = None
        self._landing_release_blocked_by_slope = False

    def _compute_landing_release_slope_safety(
        self,
        signed_error: float,
        temperature_slope_h: float | None,
        deadtime_cool_s: float | None,
        deadtime_cool_reliable: bool,
    ) -> tuple[bool, float | None]:
        if signed_error <= 0.0:
            return True, None
        if temperature_slope_h is None or temperature_slope_h <= 0.0:
            return True, None
        if (
            not deadtime_cool_reliable
            or deadtime_cool_s is None
            or deadtime_cool_s <= 0.0
        ):
            return True, None

        time_to_target_min = signed_error / temperature_slope_h * 60.0
        deadtime_cool_min = deadtime_cool_s / 60.0
        safe_threshold_min = max(
            deadtime_cool_min * LANDING_RELEASE_TIME_TO_DEADTIME_RATIO,
            LANDING_RELEASE_TIME_TO_TARGET_EPS_MIN,
        )
        return time_to_target_min >= safe_threshold_min, time_to_target_min

    def _compute_landing_decision(
        self,
        *,
        target_temp: float,
        current_temp: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode | None,
        signed_error: float,
        a: float,
        b: float,
        u_ref: float,
        u_ff_eff: float,
        kp: float | None,
        ki: float | None,
        integral: float,
        deadtime_cool_s: float | None,
        deadtime_cool_reliable: bool,
        tau_reliable: bool,
        deadband_c: float,
        remaining_cycle_min: float,
        temperature_slope_h: float | None,
        sp_for_p: float | None = None,
    ) -> SetpointLandingDecision:
        """Compute the HEAT-only setpoint landing decision."""
        def _inactive(reason: str) -> SetpointLandingDecision:
            self._reset_landing_release_safety()
            return SetpointLandingDecision(reason=reason)

        if hvac_mode == VThermHvacMode_COOL:
            self._landing_non_constraining_count = 0
            return _inactive("cool_unsupported")
        if signed_error <= 0.0:
            self._landing_non_constraining_count = 0
            return _inactive("target_reached")
        if signed_error >= TRAJECTORY_ENABLE_ERROR_THRESHOLD:
            self._landing_residual_released = False
            self._landing_non_constraining_count = 0
        if signed_error > LANDING_ENABLE_ERROR_THRESHOLD_C:
            self._landing_non_constraining_count = 0
            return _inactive("outside_landing_band")
        if not tau_reliable:
            self._landing_non_constraining_count = 0
            return _inactive("tau_unreliable")
        if (
            not deadtime_cool_reliable
            or deadtime_cool_s is None
            or deadtime_cool_s <= 0.0
        ):
            self._landing_non_constraining_count = 0
            return _inactive("deadtime_unreliable")
        if ext_current_temp is None:
            self._landing_non_constraining_count = 0
            return _inactive("missing_ext_temp")
        if a <= 0.0 or b <= 0.0:
            self._landing_non_constraining_count = 0
            return _inactive("invalid_model")
        if kp is None or kp <= 0.0:
            self._landing_non_constraining_count = 0
            return _inactive("invalid_kp")
        if ki is None:
            self._landing_non_constraining_count = 0
            return _inactive("invalid_ki")
        if self._trajectory_source != "setpoint":
            self._landing_non_constraining_count = 0
            return _inactive("not_setpoint_trajectory")
        if not self.trajectory_active:
            self._landing_non_constraining_count = 0
            return _inactive("trajectory_inactive")
        if self._landing_residual_released:
            return _inactive("residual_release")

        slope_release_safe, time_to_target_min = self._compute_landing_release_slope_safety(
            signed_error=signed_error,
            temperature_slope_h=temperature_slope_h,
            deadtime_cool_s=deadtime_cool_s,
            deadtime_cool_reliable=deadtime_cool_reliable,
        )
        self._landing_time_to_target_min = time_to_target_min
        self._landing_release_blocked_by_slope = not slope_release_safe
        flat_enough = (
            temperature_slope_h is None
            or temperature_slope_h <= LANDING_RELEASE_SLOPE_H
        )
        time_safe = slope_release_safe and time_to_target_min is not None

        if (
            self.trajectory_phase == TrajectoryPhase.RELEASE
            and signed_error <= LANDING_SAFETY_MARGIN_C + TRAJECTORY_COMPLETE_EPS_C
            and flat_enough
        ):
            self._landing_residual_released = True
            return SetpointLandingDecision(reason="residual_release")

        deadtime_cool_min = deadtime_cool_s / 60.0
        h1 = max(float(remaining_cycle_min), 0.0)
        h2 = max(deadtime_cool_min, LANDING_MIN_HORIZON_MIN)
        target_margin = target_temp - LANDING_SAFETY_MARGIN_C

        t_after_h1, _, _ = self._predict_heat_temperature(
            current_temp=current_temp,
            ext_current_temp=ext_current_temp,
            a=a,
            b=b,
            u_ref=u_ref,
            horizon_min=h1,
        )

        _, passive, gain_u = self._predict_heat_temperature(
            current_temp=t_after_h1,
            ext_current_temp=ext_current_temp,
            a=a,
            b=b,
            u_ref=0.0,
            horizon_min=h2,
        )

        if gain_u <= 0.0:
            self._landing_non_constraining_count = 0
            return _inactive("invalid_gain")

        raw_cap = (target_margin - passive) / gain_u
        u_cap = clamp(raw_cap, 0.0, 1.0)
        coast_required = raw_cap <= LANDING_U_EPS
        predicted_temperature = passive + gain_u * u_cap
        predicted_rise = predicted_temperature - current_temp

        integral_term = float(ki) * float(integral)
        available_p = u_cap - float(u_ff_eff) - integral_term
        error_p_db_cap = max(available_p / float(kp), 0.0)
        if error_p_db_cap <= LANDING_U_EPS:
            raw_error_p_cap = 0.0
        else:
            raw_error_p_cap = max(deadband_c, 0.0) + error_p_db_cap
        sp_for_p_cap = current_temp + raw_error_p_cap

        slope_ok = (
            temperature_slope_h is not None
            and temperature_slope_h <= LANDING_RELEASE_SLOPE_H
        )

        reason = "coast" if coast_required else "cap"

        residual_zone = signed_error <= LANDING_SAFETY_MARGIN_C + TRAJECTORY_COMPLETE_EPS_C
        release_allowed = (
            not coast_required
            and residual_zone
            and (slope_ok or time_safe)
        )
        if sp_for_p is not None:
            if (
                reason == "cap"
                and residual_zone
                and self.trajectory_phase == TrajectoryPhase.RELEASE
                and (flat_enough or time_safe)
                and sp_for_p <= sp_for_p_cap
            ):
                self._landing_non_constraining_count += 1
                if self._landing_non_constraining_count >= LANDING_NON_CONSTRAINING_PERSISTENCE:
                    self._landing_residual_released = True
                    self._landing_non_constraining_count = 0
                    return SetpointLandingDecision(reason="non_constraining_release")
            else:
                self._landing_non_constraining_count = 0

        return SetpointLandingDecision(
            active=True,
            reason=reason,
            u_cap=u_cap,
            sp_for_p_cap=sp_for_p_cap,
            predicted_temperature=predicted_temperature,
            predicted_rise=predicted_rise,
            target_margin=target_margin,
            coast_required=coast_required,
            release_allowed=release_allowed,
        )

    def record_landing_command_cap(
        self, before: float | None, after: float | None
    ) -> None:
        """Record command values around the setpoint landing cap."""
        self._landing_decision.u_cmd_before_cap = before
        self._landing_decision.u_cmd_after_cap = after

    def filter_setpoint(
        self,
        target_temp: float,
        current_temp: float | None,
        hvac_mode: VThermHvacMode | None = None,
        a: float = 0.0,
        b: float = 0.0,
        ext_current_temp: float | None = None,
        u_ref: float = 1.0,
        deadtime_cool_s: float | None = None,
        deadtime_cool_reliable: bool = False,
        tau_reliable: bool = False,
        deadband_c: float = 0.0,
        kp: float | None = None,
        next_cycle_u_ref: float = 0.0,
        cycle_min: float = 0.0,
        remaining_cycle_min: float = 0.0,
        now_monotonic: float | None = None,
        allow_disturbance_trigger: bool = True,
        *,
        u_ff_eff: float = 0.0,
        ki: float | None = None,
        integral: float = 0.0,
        temperature_slope_h: float | None = None,
    ) -> float:
        """Return SP_for_P for the proportional path.

        The integral path must continue to use the raw setpoint.
        The proportional path keeps the raw setpoint until the predicted
        braking zone is reached, then applies a smooth late-braking trajectory.
        """
        if not self.enabled:
            self._clear_pending_target_change_braking()
            self.set_passthrough(target_temp)
            return target_temp

        if current_temp is None:
            return self.effective_setpoint if self.effective_setpoint is not None else target_temp

        if now_monotonic is None:
            now_monotonic = time.monotonic()

        self._last_model_ready = False
        self._last_braking_needed = False
        self._last_remaining_cycle_min = max(float(remaining_cycle_min), 0.0)
        self._last_next_cycle_u_ref = max(float(next_cycle_u_ref), 0.0)
        self._last_bumpless_u_delta = None
        self._last_bumpless_ready = None
        # Landing decision is recomputed every cycle; default is inactive.
        self._landing_decision = SetpointLandingDecision()
        self._reset_landing_release_safety()

        if self.filtered_setpoint is None:
            self.filtered_setpoint = target_temp
        if self.effective_setpoint is None:
            self.effective_setpoint = target_temp

        previous_target = self._last_user_target_temp
        if previous_target is None:
            previous_target = target_temp

        self._last_user_target_temp = target_temp
        self.filtered_setpoint = target_temp

        signed_error = _signed_delta(target_temp, current_temp, hvac_mode)

        target_changed = abs(target_temp - previous_target) > 0.01

        # A trajectory only applies to the latest target. Any real setpoint
        # change restarts from passthrough, and delayed braking is re-armed
        # solely from the final signed demand versus the current room state.
        if target_changed:
            if signed_error >= TRAJECTORY_ENABLE_ERROR_THRESHOLD:
                self._pending_target_change_braking = True
            else:
                self._clear_pending_target_change_braking()
            self.set_passthrough(target_temp)
            return target_temp

        # Already at or beyond target in the active direction.
        if signed_error <= 0.0:
            self._clear_pending_target_change_braking()
            self.set_passthrough(target_temp)
            return target_temp

        deadtime_cool_min = (
            (deadtime_cool_s / 60.0)
            if deadtime_cool_reliable and deadtime_cool_s is not None and deadtime_cool_s > 0.0
            else None
        )
        braking_delay_min = None
        predicted_change = None
        effective_rate = None
        model_ready = False
        if tau_reliable and deadtime_cool_min is not None:
            braking_delay_min = deadtime_cool_min + max(remaining_cycle_min, 0.0)
            next_cycle_horizon_min = 0.0
            if next_cycle_u_ref > 0.0:
                next_cycle_horizon_min = min(
                    max(float(cycle_min), 0.0),
                    deadtime_cool_min,
                )
            predicted_change = self._compute_predicted_signed_change(
                current_temp=current_temp,
                ext_current_temp=ext_current_temp,
                hvac_mode=hvac_mode,
                a=a,
                b=b,
                u_ref=u_ref,
                horizon_min=braking_delay_min,
                next_u_ref=next_cycle_u_ref,
                next_horizon_min=next_cycle_horizon_min,
            )
            total_prediction_horizon_min = braking_delay_min + next_cycle_horizon_min
            if predicted_change is not None and total_prediction_horizon_min > 0.0:
                effective_rate = predicted_change / total_prediction_horizon_min
                model_ready = effective_rate > 0.0
        self._last_model_ready = model_ready

        braking_gap = None
        braking_needed = False
        brake_target = target_temp
        tau_brake_min = None
        if (
            model_ready
            and deadtime_cool_min is not None
            and predicted_change is not None
            and effective_rate is not None
        ):
            braking_gap = TRAJECTORY_BRAKE_GAIN * predicted_change
            braking_window = signed_error <= braking_gap
            if self.trajectory_active:
                braking_needed = (
                    signed_error <= (braking_gap + TRAJECTORY_BRAKE_RELEASE_HYST_C)
                    and signed_error > TRAJECTORY_COMPLETE_EPS_C
                )
            else:
                braking_needed = (
                    braking_window
                    and signed_error >= TRAJECTORY_ENABLE_ERROR_THRESHOLD
                    and (
                        self._pending_target_change_braking
                        or allow_disturbance_trigger
                    )
                )
            min_signed_p_error = self._minimum_signed_p_error(
                signed_error=signed_error,
                deadband_c=deadband_c,
            )
            brake_target = self._signed_brake_target(
                target_temp=target_temp,
                current_temp=current_temp,
                braking_gap=braking_gap,
                min_signed_p_error=min_signed_p_error,
                hvac_mode=hvac_mode,
            )
            tau_brake_min = max(
                deadtime_cool_min,
                signed_error / max(effective_rate, 1e-6),
                1e-6,
            )
        self._last_braking_needed = braking_needed

        if not self.trajectory_active and not braking_needed:
            self.effective_setpoint = target_temp
            return target_temp

        entering_release = False
        release_locked = self._is_setpoint_release_locked()
        if not self.trajectory_active and braking_needed and tau_brake_min is not None:
            trajectory_source = (
                "setpoint"
                if self._pending_target_change_braking
                else "disturbance"
            )
            self._trajectory.start(
                start_setpoint=target_temp,
                target_setpoint=brake_target,
                tau_ref_min=tau_brake_min,
                now_monotonic=now_monotonic,
            )
            self._trajectory_source = trajectory_source
            self._clear_pending_target_change_braking()
        elif self.trajectory_active:
            if release_locked:
                trajectory_phase = TrajectoryPhase.RELEASE
            else:
                trajectory_phase = (
                    TrajectoryPhase.TRACKING if braking_needed else TrajectoryPhase.RELEASE
                )
            entering_release = (
                trajectory_phase == TrajectoryPhase.RELEASE
                and self.trajectory_phase != TrajectoryPhase.RELEASE
            )
            desired_target = brake_target if braking_needed else target_temp
            tau_target_min = tau_brake_min
            if release_locked:
                desired_target = target_temp
                tau_target_min = None
            elif not braking_needed:
                if entering_release:
                    current_tau_ref = (
                        self._trajectory.tau_ref_min
                        or tau_brake_min
                        or deadtime_cool_min
                        or 1e-6
                    )
                    tau_target_min = max(
                        current_tau_ref * self._release_tau_factor,
                        1e-6,
                    )
                else:
                    tau_target_min = None
            self._trajectory.set_target(
                desired_target,
                tau_ref_min=tau_target_min,
                phase=trajectory_phase,
            )
            if braking_needed and self._trajectory_source == "none":
                self._trajectory_source = "disturbance"

        if not self.trajectory_active:
            self.effective_setpoint = target_temp
            return target_temp

        sp_for_p = self._trajectory.update(now_monotonic=now_monotonic)
        if sp_for_p is None:
            self.effective_setpoint = target_temp
            return target_temp

        if (
            self.trajectory_phase == TrajectoryPhase.RELEASE
            and _signed_delta(sp_for_p, current_temp, hvac_mode) <= 0.0
        ):
            sp_for_p = current_temp + TRAJECTORY_COMPLETE_EPS_C
            if hvac_mode == VThermHvacMode_COOL:
                sp_for_p = current_temp - TRAJECTORY_COMPLETE_EPS_C
            self._trajectory.current_setpoint = sp_for_p

        self._landing_decision = self._compute_landing_decision(
            target_temp=target_temp,
            current_temp=current_temp,
            ext_current_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            signed_error=signed_error,
            a=a,
            b=b,
            u_ref=u_ref,
            u_ff_eff=u_ff_eff,
            kp=kp,
            ki=ki,
            integral=integral,
            deadtime_cool_s=deadtime_cool_s,
            deadtime_cool_reliable=deadtime_cool_reliable,
            tau_reliable=tau_reliable,
            deadband_c=deadband_c,
            remaining_cycle_min=remaining_cycle_min,
            temperature_slope_h=temperature_slope_h,
            sp_for_p=sp_for_p,
        )
        if (
            self._landing_decision.active
            and self._landing_decision.sp_for_p_cap is not None
        ):
            sp_for_p = min(sp_for_p, self._landing_decision.sp_for_p_cap)
            self._trajectory.current_setpoint = sp_for_p

        if (
            not braking_needed
            and not entering_release
            and self.trajectory_phase == TrajectoryPhase.RELEASE
            and abs(target_temp - sp_for_p) <= TRAJECTORY_COMPLETE_EPS_C
            and self._temperature_release_ready(
                target_temp=target_temp,
                current_temp=current_temp,
                hvac_mode=hvac_mode,
            )
            and self._bumpless_release_ready(
                target_temp=target_temp,
                sp_for_p=sp_for_p,
                current_temp=current_temp,
                hvac_mode=hvac_mode,
                deadband_c=deadband_c,
                kp=kp,
            )
            and self._landing_decision.release_allowed
        ):
            self._clear_pending_target_change_braking()
            self.set_passthrough(target_temp)
            return target_temp

        self.effective_setpoint = sp_for_p
        return sp_for_p

    def update_boost_state(  # pylint: disable=unused-argument
        self, target_temp: float, error: float, hvac_mode: VThermHvacMode
    ) -> bool:
        """Check and update boost state based on setpoint changes."""
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
