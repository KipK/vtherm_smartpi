"""FF trim runtime coordination for SmartPI.

Freezes the control decomposition of each scheduler request for command
ownership, freezes the trim on unreliable regimes and after a reboot,
routes the causal and periodic FF trim observer results through the
persistent trim safeguards, applies bumpless trim/integral transactions at
the post-anti-windup cycle boundary, and feeds the distinct VT thermal
measurements to both observers.

SmartPI owns the trim, the observers, the causal power trace and the
controller; the coordinator reads and updates them through the SmartPI
instance it is built with and owns only the transient transfer state
published under ``feedforward.fftrim`` and the post-reboot freeze countdown.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from .causal_power_trace import ControlOwnershipSnapshot
from .command_ownership import CommandOwnershipSnapshot
from .const import (
    ABConfidenceState,
    FF_TRIM_REBOOT_FREEZE_CYCLES,
    FF_TRIM_TRANSFER_COMMAND_EPSILON,
    FF_TRIM_TRANSFER_MAX_POWER_RANGE,
    GovernanceRegime,
    SmartPICalibrationPhase,
    SmartPIPhase,
    clamp,
)
from .ff_trim import (
    CausalFFTrimResult,
    FFTrimPersistentResult,
    FFTrimThermalSample,
    FFTrimWindowProposal,
    prepare_bumpless_transfer,
)
from .valve_curve import applied_output_deviates

if TYPE_CHECKING:
    from ..algo import SmartPI

_LOGGER = logging.getLogger(__name__)


class FFTrimCoordinator:
    """FF trim observation routing and bumpless transfer for one SmartPI."""

    def __init__(self, spi: SmartPI) -> None:
        self._spi = spi
        # Post-reboot freeze countdown (cycles remaining before unfreeze);
        # a learning reset keeps it.
        self.reboot_freeze_remaining: int = 0
        self.reset()

    def reset(self) -> None:
        """Clear the transient transfer state and the last trim update outcome."""
        self.last_ff_trim_delta: float = 0.0
        self.last_reject_reason: str = "none"
        self.last_update_reason: str = "none"
        self.windows_since_update: int = 0
        self.observation_mode: str = "stationary"
        self.transfer_state: str = "idle"
        self.transfer_reason: str = "none"
        self.requested_trim_delta: float = 0.0
        self.stored_trim_delta: float = 0.0
        self.applied_trim_delta: float = 0.0
        self.transferable_i_power: float = 0.0
        self.requested_i_transfer: float = 0.0
        self.applied_i_transfer: float = 0.0
        self.net_command_delta: float = 0.0
        # Request id staged when a bumpless transfer mutated the command;
        # engagement is proven by an owned receipt of a strictly later request.
        self.transfer_pending_request_id: int | None = None
        self.transfer_quality: str = "unavailable"
        self.pending_persistent: FFTrimPersistentResult | None = None
        self.pending_quality: str = "unavailable"

    def freeze_after_reboot(self) -> None:
        """Freeze trim learning for the first ticks after a state restore."""
        self._spi._ff_trim.freeze("reboot")
        self.reboot_freeze_remaining = FF_TRIM_REBOOT_FREEZE_CYCLES

    def update_trim_freeze(self, regime: GovernanceRegime) -> None:
        """Count down the reboot freeze, then freeze or release the trim for this tick."""
        spi = self._spi
        # Post-reboot freeze countdown
        if self.reboot_freeze_remaining > 0:
            self.reboot_freeze_remaining -= 1
            if self.reboot_freeze_remaining == 0:
                spi._ff_trim.unfreeze()
                _LOGGER.debug("%s - FFv2: post-reboot freeze lifted", spi._name)

        # Freeze conditions for trim
        ab_state = spi._ab_confidence.state
        is_saturated = regime == GovernanceRegime.SATURATED
        is_perturbed = regime == GovernanceRegime.PERTURBED
        is_degraded = regime == GovernanceRegime.DEGRADED

        if self.reboot_freeze_remaining > 0:
            spi._ff_trim.freeze("reboot")
        elif ab_state in (ABConfidenceState.AB_BAD, ABConfidenceState.AB_DEGRADED):
            spi._ff_trim.freeze(f"ab_{ab_state.value}")
        elif spi._last_ff_result is not None and spi._last_ff_result.warmup_scale < 1.0:
            spi._ff_trim.freeze("ff_warmup")
        elif is_saturated and spi.ctl.last_sat == "SAT_HI":
            spi._ff_trim.freeze("saturated_high")
        elif is_saturated and spi.ctl.last_sat != "SAT_LO":
            spi._ff_trim.freeze("saturated")
        elif is_perturbed:
            spi._ff_trim.freeze("perturbed")
        elif is_degraded:
            spi._ff_trim.freeze("degraded")
        else:
            spi._ff_trim.unfreeze()

    def reset_observation_runtime(self) -> None:
        """Reset causal power, measurement identity, and logical windows."""
        spi = self._spi
        spi._causal_power_trace.reset()
        spi._thermal_measurements.reset()
        spi._fftrim_observer.reset_runtime(reset_physical_trace=False)
        spi._fftrim_periodic_observer.reset_runtime()
        self.observation_mode = "stationary"

    def on_cycle_completed(
        self,
        *,
        cycle_end_now: float,
        e_eff: float | None,
        elapsed_ratio: float,
    ) -> None:
        """Commit the completed cycle, then close or invalidate the trim windows."""
        spi = self._spi
        spi._causal_power_trace.complete_applied_cycle(
            now_monotonic=cycle_end_now,
            realized_linear_power=(
                clamp(float(e_eff), 0.0, 1.0) if e_eff is not None else None
            ),
            use_valve_trace=spi._valve_mode_enabled,
        )

        deadtime_s, deadtime_reliable = self.deadtime_context(spi._last_hvac_mode)
        if e_eff is None:
            spi._causal_power_trace.mark_discontinuity(
                cycle_end_now,
                "missing_e_eff",
            )
            self.pending_persistent = None
            spi._fftrim_periodic_observer.invalidate(
                "missing_e_eff",
                now_monotonic=cycle_end_now,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )
            result = spi._fftrim_observer.invalidate(
                "missing_e_eff",
                now_monotonic=cycle_end_now,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )
            self.apply_observer_result(result, count_window=False)
        elif elapsed_ratio < 1.0:
            spi._causal_power_trace.mark_discontinuity(
                cycle_end_now,
                "partial_cycle",
            )
            self.pending_persistent = None
            spi._fftrim_periodic_observer.invalidate(
                "partial_cycle",
                now_monotonic=cycle_end_now,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )
            result = spi._fftrim_observer.invalidate(
                "partial_cycle",
                now_monotonic=cycle_end_now,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )
            self.apply_observer_result(result, count_window=False)
        else:
            self._apply_pending_bumpless_at_cycle_boundary()
            self.try_complete_window(
                deadtime_s=deadtime_s,
                deadtime_reliable=deadtime_reliable,
                allow_bumpless=True,
            )
            self.try_complete_periodic_window(
                deadtime_s=deadtime_s,
                deadtime_reliable=deadtime_reliable,
            )

    def _constraint_flags(self, actual_ff3: float) -> tuple[str, ...]:
        """Return downstream constraints attached to the current command."""
        spi = self._spi
        flags: list[str] = []
        if spi.ctl.last_sat == "SAT_HI":
            flags.append("saturated_high")
        elif spi.ctl.last_sat == "SAT_LO":
            flags.append("saturated_low")
        if spi.sp_mgr.trajectory_active:
            flags.append("trajectory")
        current_phase = spi.phase
        if current_phase != SmartPIPhase.STABLE:
            flags.append(f"phase_{current_phase.value}")
        if spi.guards.guard_cut_active or spi.guards.guard_kick_active:
            flags.append("guard")
        if spi._setpoint_changed_in_cycle:
            flags.append("setpoint_changed")
        if spi.resume_hold.active:
            flags.append("recovery")
        if spi._last_forced_by_timing:
            flags.append("timing")
        if (
            abs(spi._last_u_cmd - spi._last_u_limited)
            > FF_TRIM_TRANSFER_COMMAND_EPSILON
        ):
            flags.append("output_limited")
        if (
            abs(spi._last_u_limited - spi._last_u_applied)
            > FF_TRIM_TRANSFER_COMMAND_EPSILON
        ):
            flags.append("timing_output")
        if abs(actual_ff3) > FF_TRIM_TRANSFER_COMMAND_EPSILON:
            flags.append("ff3")
        return tuple(sorted(set(flags)))

    def build_command_snapshot(
        self,
        hvac_mode: VThermHvacMode,
    ) -> CommandOwnershipSnapshot | None:
        """Freeze the control decomposition that produced an actuator request."""
        spi = self._spi
        ff_result = spi._last_ff_result
        if ff_result is None:
            return None

        trim = spi._ff_trim.u_ff_trim if ff_result.trim_applied else 0.0
        visible_ff = clamp(ff_result.u_ff1 + trim, 0.0, 1.0)
        actual_ff3 = spi.ctl.u_ff - visible_ff
        return CommandOwnershipSnapshot(
            hvac_mode=str(hvac_mode),
            u_ff1=float(ff_result.u_ff1),
            trim_stored=float(spi._ff_trim.u_ff_trim),
            u_ff_visible=float(visible_ff),
            u_ff3=float(actual_ff3),
            u_p=float(spi.ctl.u_p),
            u_i=float(spi.ctl.u_i),
            ki=float(spi.Ki),
            gain_generation=spi.gain_scheduler.generation,
            u_cmd=float(spi._last_u_cmd),
            u_limited=float(spi._last_u_limited),
            regime=spi.gov.regime,
            i_mode=spi.ctl.last_i_mode,
            constraint_flags=self._constraint_flags(actual_ff3),
        )

    @staticmethod
    def to_control_ownership_snapshot(
        snapshot: CommandOwnershipSnapshot | None,
        linear_committed_power: float,
        *,
        quality: str,
    ) -> ControlOwnershipSnapshot | None:
        """Convert an accepted frozen binding for the FF trim observer."""
        if snapshot is None:
            return None
        return ControlOwnershipSnapshot(
            u_ff1=snapshot.u_ff1,
            trim_stored=snapshot.trim_stored,
            u_ff_visible=snapshot.u_ff_visible,
            u_ff3=snapshot.u_ff3,
            u_p=snapshot.u_p,
            u_i=snapshot.u_i,
            ki=snapshot.ki,
            gain_generation=snapshot.gain_generation,
            u_cmd=snapshot.u_cmd,
            u_limited=snapshot.u_limited,
            linear_committed_power=float(linear_committed_power),
            regime=snapshot.regime,
            i_mode=snapshot.i_mode,
            quality=quality,
            constraint_flags=snapshot.constraint_flags,
        )

    def deadtime_context(
        self,
        hvac_mode: VThermHvacMode | None,
    ) -> tuple[float | None, bool]:
        """Return the reliable dead time matching the active HVAC mode."""
        spi = self._spi
        if hvac_mode == VThermHvacMode_HEAT:
            return (
                spi.dt_est.deadtime_heat_s,
                spi.dt_est.deadtime_heat_reliable,
            )
        if hvac_mode == VThermHvacMode_COOL:
            return (
                spi.dt_est.deadtime_cool_s,
                spi.dt_est.deadtime_cool_reliable,
            )
        return None, False

    def try_complete_window(
        self,
        *,
        deadtime_s: float | None,
        deadtime_reliable: bool,
        allow_bumpless: bool = False,
    ) -> None:
        """Apply one complete causal window when enough evidence is available."""
        spi = self._spi
        result = spi._fftrim_observer.try_complete_window(
            a=spi.est.a,
            b=spi.est.b,
            deadtime_s=deadtime_s,
            deadtime_reliable=deadtime_reliable,
            current_trim=spi._ff_trim.u_ff_trim,
        )
        if result is not None:
            self.apply_observer_result(
                result,
                count_window=True,
                allow_bumpless=allow_bumpless,
            )

    def try_complete_periodic_window(
        self,
        *,
        deadtime_s: float | None,
        deadtime_reliable: bool,
    ) -> None:
        """Evaluate one phase-closed periodic window at a cycle boundary."""
        spi = self._spi
        window = spi._fftrim_periodic_observer.try_close_window(
            earliest_power_start=spi._causal_power_trace.earliest_power_start,
            deadtime_s=deadtime_s,
            deadtime_reliable=deadtime_reliable,
        )
        if window is None:
            return

        result = spi._fftrim_observer.evaluate_periodic_window(
            window.samples,
            a=spi.est.a,
            b=spi.est.b,
            deadtime_s=window.deadtime_s,
            current_trim=spi._ff_trim.u_ff_trim,
        )
        if result is None:
            spi._fftrim_periodic_observer.state = "waiting_deadtime"
            spi._fftrim_periodic_observer.last_reject_reason = (
                "causal_power_not_covered"
            )
            return

        spi._fftrim_periodic_observer.complete_window(
            window,
            admissible=result.admissible,
            reason=result.reason,
        )
        spi._fftrim_observer.publish_external_result(result)
        self.apply_observer_result(
            result,
            count_window=True,
        )

    def apply_observer_result(
        self,
        result: CausalFFTrimResult,
        *,
        count_window: bool,
        allow_bumpless: bool = False,
    ) -> bool:
        """Route an observer result through the persistent trim safeguards."""
        spi = self._spi
        observation_mode = result.observation_mode
        self.observation_mode = observation_mode
        if not result.admissible:
            self.pending_persistent = None
            spi._ff_trim.clear_pending(observation_mode)
            self._clear_transfer_values()
            self.transfer_state = "observer_rejected"
            self.transfer_reason = result.reason
            self.transfer_pending_request_id = None
            self.last_reject_reason = result.reason
            self.last_update_reason = "skipped"
            spi._fftrim_observer.last_update_reason = "skipped"
            if count_window:
                self.windows_since_update += 1
            return False

        correction = (
            result.decomposed_correction
            if result.transfer_eligible
            else result.correction
        )
        mean_ff1 = result.mean_ff1
        if correction is None or mean_ff1 is None:
            spi._ff_trim.clear_pending(observation_mode)
            self._clear_transfer_values()
            self.transfer_state = "observer_rejected"
            self.transfer_reason = "incomplete_observer_result"
            self.transfer_pending_request_id = None
            self.last_reject_reason = "incomplete_observer_result"
            self.last_update_reason = "skipped"
            spi._fftrim_observer.last_update_reason = "skipped"
            return False

        proposal = FFTrimWindowProposal(
            correction=float(correction),
            mean_ff1=float(mean_ff1),
            physical_power_deficit=result.physical_power_deficit,
            integral_bias=result.mean_i_power,
            transfer_eligible=result.transfer_eligible,
            transfer_reason=result.transfer_reason,
            observation_mode=observation_mode,
        )
        persistent = spi._ff_trim.collect_persistent(proposal)
        self.last_ff_trim_delta = float(correction)
        self.last_update_reason = persistent.reason
        self.last_reject_reason = "none"
        spi._fftrim_observer.last_update_reason = persistent.reason
        self.transfer_quality = result.transfer_quality
        if not persistent.ready:
            self._clear_transfer_values()
            self.transfer_pending_request_id = None
            self.transfer_state = (
                "pending" if result.transfer_eligible else "ineligible"
            )
            self.transfer_reason = (
                persistent.reason
                if result.transfer_eligible
                else result.transfer_reason
            )
            if count_window:
                self.windows_since_update += 1
            return False

        if persistent.transfer_eligible:
            if allow_bumpless:
                updated = self.apply_bumpless_persistent_result(
                    persistent=persistent,
                    quality=result.transfer_quality,
                )
            else:
                self.pending_persistent = persistent
                self.pending_quality = result.transfer_quality
                self._clear_transfer_values()
                self.transfer_pending_request_id = None
                self.transfer_state = "awaiting_cycle_boundary"
                self.transfer_reason = "awaiting_post_aw_boundary"
                self.last_update_reason = "awaiting_cycle_boundary"
                spi._fftrim_observer.last_update_reason = (
                    "awaiting_cycle_boundary"
                )
                if count_window:
                    self.windows_since_update += 1
                return False
        else:
            trim_update = spi._ff_trim.apply_persistent_result(
                persistent,
                u_ff_ab=float(mean_ff1),
            )
            updated = trim_update.updated
            self.last_update_reason = trim_update.reason
            spi._fftrim_observer.last_update_reason = trim_update.reason
            self.requested_trim_delta = trim_update.requested_trim_delta
            self.stored_trim_delta = trim_update.stored_trim_delta
            self.applied_trim_delta = trim_update.visible_ff_delta
            self.transferable_i_power = 0.0
            self.requested_i_transfer = 0.0
            self.applied_i_transfer = 0.0
            self.net_command_delta = trim_update.visible_ff_delta
            self.transfer_state = "causal_update_without_transfer"
            self.transfer_reason = result.transfer_reason
            self.transfer_pending_request_id = None
            if updated:
                self.record_applied_transaction(
                    observation_mode=observation_mode
                )

        if updated:
            self.windows_since_update = 0
            self._reset_windows_after_update()
        elif count_window:
            self.windows_since_update += 1
        return updated

    def apply_bumpless_persistent_result(
        self,
        *,
        persistent: FFTrimPersistentResult,
        quality: str,
    ) -> bool:
        """Apply a persistent trim/I transaction after final context checks."""
        spi = self._spi
        ff_result = spi._last_ff_result
        deficit = persistent.median_physical_power_deficit
        i_bias = persistent.median_integral_bias
        if ff_result is None or deficit is None or i_bias is None:
            return self.reject_bumpless_transfer("incomplete_transfer_result")

        rejection = self.current_bumpless_context_rejection()
        if rejection is not None:
            return self.reject_bumpless_transfer(rejection)
        if (
            abs(ff_result.u_ff1 - persistent.median_ff1)
            > FF_TRIM_TRANSFER_MAX_POWER_RANGE
        ):
            return self.reject_bumpless_transfer("ff1_changed")

        visible_ff = clamp(
            ff_result.u_ff1 + spi._ff_trim.u_ff_trim,
            0.0,
            1.0,
        )
        current_i_power = spi.Ki * spi.ctl.integral
        current_raw_command = visible_ff + spi.ctl.u_p + current_i_power
        plan = prepare_bumpless_transfer(
            current_trim=spi._ff_trim.u_ff_trim,
            current_ff1=ff_result.u_ff1,
            current_i_power=current_i_power,
            observed_i_bias=float(i_bias),
            physical_power_deficit=float(deficit),
            current_ki=spi.Ki,
            current_raw_command=current_raw_command,
        )
        self.requested_trim_delta = plan.requested_trim_delta
        self.stored_trim_delta = plan.stored_trim_delta
        self.applied_trim_delta = plan.visible_ff_delta
        self.transferable_i_power = plan.transferable_i_power
        self.requested_i_transfer = plan.requested_i_transfer
        self.applied_i_transfer = plan.applied_i_transfer
        self.net_command_delta = plan.net_command_delta
        self.transfer_quality = quality
        if not plan.applicable:
            return self.reject_bumpless_transfer(plan.reason, clear_values=False)

        old_controller_ff = spi.ctl.u_ff
        if not spi._ff_trim.commit_bumpless_plan(plan):
            return self.reject_bumpless_transfer("trim_state_changed")

        new_visible_ff = visible_ff + plan.visible_ff_delta
        spi.ctl.u_ff = new_visible_ff
        integral_result = spi.ctl.apply_integral_power_transfer(
            ki=spi.Ki,
            requested_power=plan.applied_i_transfer,
        )
        if (
            not integral_result.applied
            or abs(integral_result.applied_power - plan.applied_i_transfer) > 1e-12
        ):
            spi._ff_trim.rollback_bumpless_plan(plan)
            spi.ctl.u_ff = old_controller_ff
            return self.reject_bumpless_transfer(integral_result.reason)

        spi._last_ff_result = replace(
            ff_result,
            u_ff2=plan.new_trim,
            u_ff_trim=plan.new_trim,
            u_ff_final=new_visible_ff,
            u_ff_eff=new_visible_ff,
        )
        spi.ctl.u_cmd = clamp(
            new_visible_ff + spi.ctl.u_p + spi.ctl.u_i,
            0.0,
            1.0,
        )
        spi.ctl.u_hold = spi.ctl.u_cmd
        spi._last_u_ff = new_visible_ff
        spi._last_u_pi = spi.ctl.u_pi
        spi._last_u_cmd = spi.ctl.u_cmd
        self.last_ff_trim_delta = plan.visible_ff_delta
        self.last_update_reason = "updated_bumpless"
        spi._fftrim_observer.last_update_reason = "updated_bumpless"
        self.transfer_state = "applied"
        self.transfer_reason = plan.reason
        self.transfer_pending_request_id = spi._command_ownership.last_staged_id
        self.record_applied_transaction(
            observation_mode=persistent.observation_mode
        )
        spi._ff_trim.clear_pending()
        self.pending_persistent = None
        return True

    def _apply_pending_bumpless_at_cycle_boundary(self) -> None:
        """Apply a prepared transaction at the cycle boundary."""
        persistent = self.pending_persistent
        if persistent is None:
            return
        quality = self.pending_quality
        self.pending_persistent = None
        if self.apply_bumpless_persistent_result(
            persistent=persistent,
            quality=quality,
        ):
            self.windows_since_update = 0
            self._reset_windows_after_update()

    def current_bumpless_context_rejection(self) -> str | None:
        """Revalidate strict deadband ownership at the mutation instant."""
        spi = self._spi
        if spi.gov.regime != GovernanceRegime.DEAD_BAND:
            return "application_not_deadband"
        if spi.ctl.last_i_mode != "I:FREEZE(deadband)":
            return "application_i_not_frozen"
        if spi.ctl.last_sat != "NO_SAT":
            return "application_saturated"
        if spi.sp_mgr.trajectory_active:
            return "application_trajectory"
        if spi.calibration_mgr.state != SmartPICalibrationPhase.IDLE:
            return "application_calibration"
        if spi.guards.guard_cut_active or spi.guards.guard_kick_active:
            return "application_guard"
        if spi._setpoint_changed_in_cycle:
            return "application_setpoint_changed"
        if spi.resume_hold.active:
            return "application_recovery"
        if spi.ff3_state.active_cycle or abs(spi.ff3_state.u_cycle) > 1e-9:
            return "application_ff3"
        if spi._last_forced_by_timing:
            return "application_timing"
        if (
            abs(spi._last_u_cmd - spi._last_u_limited)
            > FF_TRIM_TRANSFER_COMMAND_EPSILON
        ):
            return "application_output_limited"
        if applied_output_deviates(
            spi.valve_curve,
            spi._valve_mode_enabled,
            spi._last_u_limited,
            spi._last_u_applied,
            FF_TRIM_TRANSFER_COMMAND_EPSILON,
        ):
            return "application_timing_output"
        return None

    def reject_bumpless_transfer(
        self,
        reason: str,
        *,
        clear_values: bool = True,
    ) -> bool:
        """Reject both sides of one ownership transaction."""
        spi = self._spi
        spi._ff_trim.clear_pending()
        self.pending_persistent = None
        self.last_update_reason = f"transfer_rejected_{reason}"
        spi._fftrim_observer.last_update_reason = self.last_update_reason
        self.transfer_state = "rejected"
        self.transfer_reason = reason
        self.transfer_pending_request_id = None
        if clear_values:
            self._clear_transfer_values()
        return False

    def _clear_transfer_values(self) -> None:
        """Clear deltas when no concrete transaction currently owns them."""
        self.requested_trim_delta = 0.0
        self.stored_trim_delta = 0.0
        self.applied_trim_delta = 0.0
        self.transferable_i_power = 0.0
        self.requested_i_transfer = 0.0
        self.applied_i_transfer = 0.0
        self.net_command_delta = 0.0

    def record_applied_transaction(
        self,
        *,
        observation_mode: str,
    ) -> None:
        """Retain the latest successfully applied FF trim transaction."""
        spi = self._spi
        spi._ff_trim.record_applied_transaction(
            observation_mode=observation_mode,
            state=self.transfer_state,
            reason=self.transfer_reason,
            quality=self.transfer_quality,
            requested_trim_delta=self.requested_trim_delta,
            stored_trim_delta=self.stored_trim_delta,
            applied_trim_delta=self.applied_trim_delta,
            transferable_i_power=self.transferable_i_power,
            requested_i_transfer=self.requested_i_transfer,
            applied_i_transfer=self.applied_i_transfer,
            net_command_delta=self.net_command_delta,
        )

    def _reset_windows_after_update(self) -> None:
        """Wash out both logical windows after the trim reference changes."""
        spi = self._spi
        deadtime_s, deadtime_reliable = self.deadtime_context(
            spi._last_hvac_mode
        )
        washout_s = (
            float(deadtime_s)
            if deadtime_reliable and deadtime_s is not None
            else 0.0
        )
        now_monotonic = time.monotonic()
        spi._fftrim_observer.reset_after_trim_update(
            now_monotonic=now_monotonic,
            washout_s=washout_s,
        )
        spi._fftrim_periodic_observer.reset_after_trim_update(
            now_monotonic=now_monotonic,
            washout_s=washout_s,
        )

    def record_thermal_measurement(
        self,
        *,
        now_monotonic: float,
        measurement_id: Any | None,
        current_temp: float,
        target_temp: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode,
        setpoint_changed: bool,
    ) -> None:
        """Acquire one distinct VT measurement, then apply FF-trim gates."""
        spi = self._spi
        measurement = spi._thermal_measurements.observe(
            now_monotonic=now_monotonic,
            measurement_id=measurement_id,
            indoor_temperature=current_temp,
            outside_temperature=ext_current_temp,
        )
        if measurement is None:
            return

        deadtime_s, deadtime_reliable = self.deadtime_context(hvac_mode)
        mode = (
            "heat"
            if hvac_mode == VThermHvacMode_HEAT
            else "cool"
            if hvac_mode == VThermHvacMode_COOL
            else str(hvac_mode)
        )
        ff_result = spi._last_ff_result
        if ff_result is None:
            return

        sample = FFTrimThermalSample(
            observed_monotonic=measurement.observed_monotonic,
            measurement_id=measurement.measurement_id,
            temperature=measurement.indoor_temperature,
            target=float(target_temp),
            ff1=float(ff_result.u_ff1),
            regime=spi.gov.regime,
            i_mode=spi.ctl.last_i_mode,
            u_pi=spi.ctl.u_pi,
            hvac_mode=mode,
            saturated=spi.ctl.last_sat != "NO_SAT",
            trajectory_active=spi.sp_mgr.trajectory_active,
            ff3_active=spi.ff3_state.active_cycle,
            setpoint_changed=setpoint_changed,
            outside_temperature_available=(
                measurement.outside_temperature is not None
            ),
            model_reliable=(
                spi._ab_confidence.state == ABConfidenceState.AB_OK
                and spi.est.tau_reliability().reliable
            ),
            outside_temperature=measurement.outside_temperature,
            trim_frozen_reason=(
                spi._ff_trim.freeze_reason if spi._ff_trim.frozen else None
            ),
            saturation_state=spi.ctl.last_sat,
        )
        spi._fftrim_periodic_observer.record_thermal_sample(
            sample,
            deadtime_s=deadtime_s,
            deadtime_reliable=deadtime_reliable,
        )
        result = spi._fftrim_observer.record_thermal_sample(
            sample,
            deadtime_s=deadtime_s,
            deadtime_reliable=deadtime_reliable,
        )
        if result is not None:
            self.apply_observer_result(result, count_window=False)
            return

        self.try_complete_window(
            deadtime_s=deadtime_s,
            deadtime_reliable=deadtime_reliable,
        )

    def invalidate_context(
        self,
        reason: str,
        *,
        now_monotonic: float,
        hvac_mode: VThermHvacMode | None,
    ) -> None:
        """Reject the current window and wash out its delayed thermal response."""
        spi = self._spi
        spi.reset_command_ownership("ownership_discontinuity")
        deadtime_s, deadtime_reliable = self.deadtime_context(hvac_mode)
        spi._fftrim_periodic_observer.invalidate(
            reason,
            now_monotonic=now_monotonic,
            washout_s=deadtime_s if deadtime_reliable else 0.0,
        )
        result = spi._fftrim_observer.invalidate(
            reason,
            now_monotonic=now_monotonic,
            washout_s=deadtime_s if deadtime_reliable else 0.0,
        )
        self.apply_observer_result(result, count_window=False)
