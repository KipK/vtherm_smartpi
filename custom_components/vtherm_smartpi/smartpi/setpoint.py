"""Setpoint management for Smart-PI late-braking trajectory shaping."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .const import (
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
)
from .controller import PIOutputSnapshot
from .reference_governor_authority import (
    ReferenceGovernorAuthority,
    ReferenceGovernorAuthorityDecision,
)
from .trajectory import SmartPITrajectoryGenerator
from .thermal_model import propagate_1r1c
from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL, VThermHvacMode_HEAT

_LOGGER = logging.getLogger(__name__)


def _signed_delta(value: float, reference: float, hvac_mode: VThermHvacMode | None) -> float:
    """Return the signed demand delta in the active HVAC direction."""
    delta = value - reference
    if hvac_mode == VThermHvacMode_COOL:
        return -delta
    return delta


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

        # Public target value; the shaped P reference lives in effective_setpoint.
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
        self._reference_governor_authority = ReferenceGovernorAuthority()
        self._reference_governor_prepared_at: float | None = None
        self._reference_governor_authority_decision: (
            ReferenceGovernorAuthorityDecision | None
        ) = None
        self._reference_governor_authority_selected = False
        self._reference_governor_authority_episode = False
        self._governor_u_cmd_before_cap: float | None = None
        self._governor_u_cmd_after_cap: float | None = None

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
    def setpoint_landing_active(self) -> bool:
        """Return whether an authoritative user setpoint landing is active."""
        return (
            self._reference_governor_authority_episode
            and self._trajectory_source == "setpoint"
        )

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

    @property
    def reference_governor_authority_decision(
        self,
    ) -> ReferenceGovernorAuthorityDecision | None:
        """Return the latest authoritative post-PI governor decision."""
        return self._reference_governor_authority_decision

    @property
    def reference_governor_authority_pending(self) -> bool:
        """Return whether this cycle has an authoritative context to resolve."""
        return self._reference_governor_authority.pending_context is not None

    @property
    def reference_governor_authority_selected(self) -> bool:
        """Return whether this cycle uses the authoritative governor path."""
        return self._reference_governor_authority_selected

    @property
    def reference_governor_authority_nominal_reference(self) -> float | None:
        """Return the independent nominal reference prepared for this cycle."""
        context = self._reference_governor_authority.pending_context
        return None if context is None else context.nominal_reference

    @property
    def governor_active(self) -> bool:
        """Return whether the authoritative governor shapes this episode."""
        decision = self._reference_governor_authority_decision
        return decision.shaping_active if decision is not None else False

    @property
    def governor_phase(self) -> str:
        """Return the authoritative runtime phase."""
        decision = self._reference_governor_authority_decision
        return decision.phase.value if decision is not None else "idle"

    @property
    def governor_reason(self) -> str:
        """Return the latest authoritative decision reason."""
        decision = self._reference_governor_authority_decision
        return decision.reason if decision is not None else "inactive"

    @property
    def governor_nominal_reference(self) -> float | None:
        """Return the nominal reference evaluated by the governor."""
        decision = self._reference_governor_authority_decision
        return decision.kernel_decision.nominal_reference if decision is not None else None

    @property
    def governor_admissible_reference(self) -> float | None:
        """Return the reference admitted by the authoritative runtime."""
        decision = self._reference_governor_authority_decision
        return decision.effective_reference if decision is not None else None

    @property
    def governor_command_cap(self) -> float | None:
        """Return the authoritative command cap."""
        decision = self._reference_governor_authority_decision
        return decision.command_cap if decision is not None else None

    @property
    def governor_constraint_active(self) -> bool:
        """Return whether the kernel detected an active thermal constraint."""
        decision = self._reference_governor_authority_decision
        return (
            decision.kernel_decision.constraint_active
            if decision is not None
            else False
        )

    @property
    def governor_dynamic_reserve_c(self) -> float | None:
        """Return the dynamic thermal reserve selected by the kernel."""
        decision = self._reference_governor_authority_decision
        return decision.kernel_decision.dynamic_reserve_c if decision is not None else None

    @property
    def governor_predicted_terminal_temperature(self) -> float | None:
        """Return the terminal temperature predicted by the kernel."""
        decision = self._reference_governor_authority_decision
        return (
            decision.kernel_decision.predicted_terminal_temp
            if decision is not None
            else None
        )

    @property
    def governor_target_bound(self) -> float | None:
        """Return the signed-mode thermal target bound."""
        decision = self._reference_governor_authority_decision
        return decision.kernel_decision.target_bound if decision is not None else None

    @property
    def governor_coast_required(self) -> bool:
        """Return whether zero command is required by the kernel."""
        decision = self._reference_governor_authority_decision
        return decision.kernel_decision.coast_required if decision is not None else False

    @property
    def governor_handoff_ready(self) -> bool:
        """Return whether the runtime completed its non-constraining handoff."""
        decision = self._reference_governor_authority_decision
        return decision.handoff_ready if decision is not None else False

    @property
    def governor_u_cmd_before_cap(self) -> float | None:
        """Return the PI command observed before the governor cap."""
        return self._governor_u_cmd_before_cap

    @property
    def governor_u_cmd_after_cap(self) -> float | None:
        """Return the command emitted after the governor cap."""
        return self._governor_u_cmd_after_cap

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

    def _reset_trajectory(self) -> None:
        """Reset the trajectory state only."""
        self._trajectory.reset()
        self._trajectory_source = "none"
        self._reference_governor_authority.reset()
        self._reference_governor_prepared_at = None
        self._reference_governor_authority_decision = None
        self._reference_governor_authority_selected = False
        self._reference_governor_authority_episode = False
        self._governor_u_cmd_before_cap = None
        self._governor_u_cmd_after_cap = None

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
        self._reference_governor_authority.load_state()
        self._reference_governor_prepared_at = None
        self._reference_governor_authority_decision = None
        self._reference_governor_authority_selected = False
        self._reference_governor_authority_episode = False
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

            predicted_change = current_temp - propagate_1r1c(
                temperature=current_temp,
                external_temperature=ext_current_temp,
                a=0.0,
                b=b,
                power=0.0,
                duration_min=total_horizon_min,
            )
            return predicted_change if predicted_change > 0.0 else None

        # Active mode: full 1R1C model (HEAT with a > 0, COOL with a < 0).
        if hvac_mode != VThermHvacMode_COOL and a <= 0.0:
            return None

        predicted_temp = current_temp

        # The already committed command acts through the remaining cycle.
        current_horizon_min = max(float(horizon_min), 0.0)
        if current_horizon_min > 0.0:
            predicted_temp = propagate_1r1c(
                temperature=predicted_temp,
                external_temperature=ext_current_temp,
                a=a,
                b=b,
                power=max(float(u_ref), 0.0),
                duration_min=current_horizon_min,
            )

        # A queued next-cycle command can further change room temperature.
        next_horizon_min = max(float(next_horizon_min), 0.0)
        if next_horizon_min > 0.0:
            predicted_temp = propagate_1r1c(
                temperature=predicted_temp,
                external_temperature=ext_current_temp,
                a=a,
                b=b,
                power=max(float(next_u_ref), 0.0),
                duration_min=next_horizon_min,
            )

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

        # Compare P after the same deadzone used by the controller. This
        # handoff checks the reference change, not the total output command.
        raw_error_p_db = self._apply_signed_deadzone(raw_signed_error_p, deadband_c)
        filtered_error_p_db = self._apply_signed_deadzone(filtered_signed_error_p, deadband_c)

        u_delta = abs(float(kp)) * abs(raw_error_p_db - filtered_error_p_db)
        self._last_bumpless_u_delta = u_delta
        self._last_bumpless_ready = u_delta <= TRAJECTORY_BUMPLESS_MAX_U_DELTA
        return self._last_bumpless_ready

    @staticmethod
    def _temperature_release_ready(
        *,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode | None,
    ) -> bool:
        """Return True once the measured temperature is close enough to target."""
        return _signed_delta(target_temp, current_temp, hvac_mode) <= TRAJECTORY_COMPLETE_EPS_C

    def record_reference_governor_command(
        self, before: float | None, after: float | None
    ) -> None:
        """Record command values around the authoritative governor cap."""
        self._governor_u_cmd_before_cap = before
        self._governor_u_cmd_after_cap = after

    def resolve_reference_governor(
        self,
        *,
        requested_u: float,
        now_monotonic: float,
        cycle_min: float,
        pi_snapshot: PIOutputSnapshot,
    ) -> ReferenceGovernorAuthorityDecision | None:
        """Resolve the prepared governor context with the true PI request."""
        self._reference_governor_prepared_at = None
        result = self._reference_governor_authority.resolve(
            requested_u=requested_u,
            now_monotonic=now_monotonic,
            cycle_min=cycle_min,
            pi_snapshot=pi_snapshot,
        )
        if result is None:
            return None

        self._reference_governor_authority_decision = result
        if result.handoff_ready:
            target = self._last_user_target_temp
            if target is not None:
                # Completion belongs to the governor. Clear the nominal
                # trajectory only after its non-constraining handoff proof.
                self.set_passthrough(target)
                self._reference_governor_authority_decision = result
        return result

    def _authoritative_reference_for_cycle(
        self,
        *,
        nominal_reference: float,
        current_temp: float,
        hvac_mode: VThermHvacMode | None,
    ) -> float:
        """Apply the previous admissible reference without mutating the profile."""
        decision = self._reference_governor_authority_decision
        if decision is None or not decision.shaping_active:
            return nominal_reference
        direction = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
        nominal_error = max(
            direction * (nominal_reference - current_temp),
            0.0,
        )
        if nominal_error <= 0.0:
            return nominal_reference
        admissible_error = direction * (
            decision.effective_reference - current_temp
        )
        minimum_error = min(TRAJECTORY_COMPLETE_EPS_C, nominal_error)
        bounded_error = min(
            max(admissible_error, minimum_error),
            nominal_error,
        )
        return current_temp + direction * bounded_error

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
        temperature_slope_h: float | None = None,
    ) -> float:
        """Return SP_for_P for the proportional path.

        The integral path must continue to use the raw setpoint.
        The proportional path keeps the raw setpoint until the predicted
        braking zone is reached, then applies a smooth late-braking trajectory.
        """
        self._reference_governor_prepared_at = None
        if not self.enabled:
            self._clear_pending_target_change_braking()
            self.set_passthrough(target_temp)
            return target_temp

        self._reference_governor_authority_selected = False
        if current_temp is None:
            self._reference_governor_authority.reset()
            self._reference_governor_authority_decision = None
            self._governor_u_cmd_before_cap = None
            self._governor_u_cmd_after_cap = None
            return self.effective_setpoint if self.effective_setpoint is not None else target_temp

        if now_monotonic is None:
            now_monotonic = time.monotonic()

        self._last_model_ready = False
        self._last_braking_needed = False
        self._last_remaining_cycle_min = max(float(remaining_cycle_min), 0.0)
        self._last_next_cycle_u_ref = max(float(next_cycle_u_ref), 0.0)
        self._last_bumpless_u_delta = None
        self._last_bumpless_ready = None
        # Command diagnostics are refreshed after the PI request is resolved.
        self._governor_u_cmd_before_cap = None
        self._governor_u_cmd_after_cap = None

        if self.filtered_setpoint is None:
            self.filtered_setpoint = target_temp
        if self.effective_setpoint is None:
            self.effective_setpoint = target_temp

        previous_target = self._last_user_target_temp
        if previous_target is None:
            previous_target = target_temp

        self._last_user_target_temp = target_temp
        # Keep the requested target separate from the shaped P reference.
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

        # Keep an armed setpoint episode alive long enough for the runtime
        # to observe no_positive_demand and complete its HANDOFF sequence.
        preserve_governor_episode = (
            signed_error <= 0.0
            and self._reference_governor_authority_episode
            and self._trajectory_source == "setpoint"
            and self.trajectory_active
            and self._reference_governor_authority.runtime.armed
        )

        # Already at or beyond target in the active direction. A disarmed or
        # invalid episode remains fail-safe passthrough.
        if signed_error <= 0.0 and not preserve_governor_episode:
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
            # Predict across the current cycle remainder, the cooling delay,
            # and any queued power at the start of the following cycle.
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
                # Hysteresis avoids toggling an active trajectory at the edge
                # of the predicted braking window.
                braking_needed = (
                    signed_error <= (braking_gap + TRAJECTORY_BRAKE_RELEASE_HYST_C)
                    and signed_error > TRAJECTORY_COMPLETE_EPS_C
                )
            else:
                # A significant target change has already passed the arming
                # threshold. The signed model uses the same braking window in
                # HEAT and COOL; the governor owns the thermal reserve.
                setpoint_entry_error = signed_error
                setpoint_braking_window = (
                    self._pending_target_change_braking
                    and setpoint_entry_error <= braking_gap
                )
                disturbance_braking_window = (
                    allow_disturbance_trigger
                    and signed_error >= TRAJECTORY_ENABLE_ERROR_THRESHOLD
                    and braking_window
                )
                braking_needed = (
                    setpoint_braking_window or disturbance_braking_window
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
            # Keep the braking time constant at least as long as the cooling delay.
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
        release_locked = (
            self.trajectory_active
            and self._trajectory_source == "setpoint"
            and self.trajectory_phase == TrajectoryPhase.RELEASE
        )
        if not self.trajectory_active and braking_needed and tau_brake_min is not None:
            # Remember whether the trajectory responds to a user target or
            # a disturbance; only the former enters the reference governor.
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
            self._reference_governor_authority_episode = (
                trajectory_source == "setpoint"
                and hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL)
            )
            self._clear_pending_target_change_braking()
        elif self.trajectory_active:
            # A setpoint trajectory progresses monotonically through its
            # phases. The governor handles later constraint variations without
            # sending the nominal profile back from release to tracking.
            trajectory_phase = (
                TrajectoryPhase.RELEASE
                if release_locked
                else (
                    TrajectoryPhase.TRACKING
                    if braking_needed
                    else TrajectoryPhase.RELEASE
                )
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
                    # Retarget the generator from its current value so the
                    # filtered P reference returns smoothly to the raw target.
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
            # Keep the P reference on the demand side of the measured room
            # temperature during the final approach, without crossing the
            # user target.
            sp_for_p = current_temp + TRAJECTORY_COMPLETE_EPS_C
            if hvac_mode == VThermHvacMode_COOL:
                sp_for_p = current_temp - TRAJECTORY_COMPLETE_EPS_C
                sp_for_p = max(sp_for_p, target_temp)
            else:
                sp_for_p = min(sp_for_p, target_temp)
            self._trajectory.current_setpoint = sp_for_p

        if (
            self._trajectory_source == "setpoint"
            and hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL)
        ):
            self._reference_governor_authority_episode = True
        if self._reference_governor_authority_episode:
            authority_context = self._reference_governor_authority.prepare(
                target_temp=target_temp,
                nominal_reference=sp_for_p,
                current_temp=current_temp,
                hvac_mode=hvac_mode,
                trajectory_active=self.trajectory_active,
                trajectory_source=self._trajectory_source,
                a=a,
                b=b,
                ext_temp=ext_current_temp,
                committed_u=u_ref,
                remaining_cycle_min=remaining_cycle_min,
                stop_deadtime_s=deadtime_cool_s,
                measured_slope_h=temperature_slope_h,
                model_reliable=tau_reliable,
                deadtime_reliable=deadtime_cool_reliable,
                now_monotonic=now_monotonic,
            )
            self._reference_governor_authority_selected = True
            self._reference_governor_prepared_at = now_monotonic
        if self._reference_governor_authority_selected:
            # The profile remains an independent nominal proposal. The
            # previous post-PI decision supplies this cycle's P reference;
            # this cycle's true PI request will resolve the next decision.
            if authority_context.bypass_reason is None:
                sp_for_p = self._authoritative_reference_for_cycle(
                    nominal_reference=sp_for_p,
                    current_temp=current_temp,
                    hvac_mode=hvac_mode,
                )
            if hvac_mode == VThermHvacMode_COOL:
                sp_for_p = max(sp_for_p, target_temp)
            elif hvac_mode == VThermHvacMode_HEAT:
                sp_for_p = min(sp_for_p, target_temp)
            self.effective_setpoint = sp_for_p
            return sp_for_p

        # Finish only when both the room and P reference are near the target
        # and the handoff cannot create a large proportional output step.
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
