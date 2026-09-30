"""Authoritative prepare/resolve coordinator for the SmartPI governor."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from .const import (
    REFERENCE_GOVERNOR_COMMAND_EPSILON,
    REFERENCE_GOVERNOR_MARGIN_MAX_C,
    REFERENCE_GOVERNOR_REFERENCE_EPSILON_C,
    REFERENCE_GOVERNOR_RESERVE_SELECTOR,
    REFERENCE_GOVERNOR_RHO_MARGIN,
)
from .controller import PIOutputSnapshot
from .reference_governor import (
    ReferenceGovernorDecision,
    ReferenceGovernorInput,
    ReferenceGovernorPolicy,
    evaluate_reference_governor,
)
from .reference_governor_runtime import (
    ReferenceGovernorPhase,
    ReferenceGovernorRuntime,
    ReferenceGovernorRuntimeDecision,
)


@dataclass(frozen=True, slots=True)
class ReferenceGovernorAuthorityContext:
    """Immutable per-cycle observations captured before the PI calculation."""

    target_temp: float
    nominal_reference: float
    current_temp: float
    ext_temp: float
    hvac_mode: VThermHvacMode | None
    a: float
    b: float
    committed_u: float
    remaining_cycle_min: float
    stop_deadtime_s: float
    measured_slope_h: float | None
    bypass_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceGovernorAuthorityDecision:
    """Immutable authoritative output with both runtime and kernel evidence."""

    phase: ReferenceGovernorPhase
    effective_reference: float
    command_cap: float | None
    shaping_active: bool
    handoff_ready: bool
    reason: str
    kernel_decision: ReferenceGovernorDecision
    runtime_decision: ReferenceGovernorRuntimeDecision

    @property
    def kernel_evidence(self) -> ReferenceGovernorDecision:
        """Return the pure kernel evidence carried by this result."""
        return self.kernel_decision

    @property
    def runtime_evidence(self) -> ReferenceGovernorRuntimeDecision:
        """Return the runtime state-machine evidence carried by this result."""
        return self.runtime_decision


class ReferenceGovernorAuthority:
    """Coordinate one prepared kernel evaluation with runtime activation."""

    def __init__(self) -> None:
        self._runtime = ReferenceGovernorRuntime()
        self._pending_context: ReferenceGovernorAuthorityContext | None = None
        self._episode_target: float | None = None

    @property
    def pending_context(self) -> ReferenceGovernorAuthorityContext | None:
        """Return the pending context, if a cycle was prepared but not resolved."""
        return self._pending_context

    @property
    def runtime(self) -> ReferenceGovernorRuntime:
        """Return the owned transient runtime state machine."""
        return self._runtime

    def prepare(
        self,
        *,
        target_temp: object,
        nominal_reference: object,
        current_temp: object,
        hvac_mode: VThermHvacMode | None,
        trajectory_active: bool,
        trajectory_source: str,
        a: object,
        b: object,
        ext_temp: object,
        committed_u: object,
        remaining_cycle_min: object,
        stop_deadtime_s: object,
        measured_slope_h: object | None,
        model_reliable: bool,
        deadtime_reliable: bool,
        now_monotonic: object,
    ) -> ReferenceGovernorAuthorityContext:
        """Capture one cycle, or capture an explicit fail-open bypass."""
        self._pending_context = None
        target = self._finite(target_temp)
        nominal = self._finite(nominal_reference)
        reference = nominal if nominal is not None else target
        if reference is None:
            reference = 0.0

        bypass_reason = self._validation_reason(
            target=target,
            nominal=nominal,
            current=self._finite(current_temp),
            external=self._finite(ext_temp),
            hvac_mode=hvac_mode,
            trajectory_active=trajectory_active,
            trajectory_source=trajectory_source,
            model_reliable=model_reliable,
            a=self._finite(a),
            b=self._finite(b),
            deadtime_reliable=deadtime_reliable,
            deadtime=self._finite(stop_deadtime_s),
            committed=self._finite(committed_u),
            remaining=self._finite(remaining_cycle_min),
            slope=None if measured_slope_h is None else self._finite(measured_slope_h),
            slope_input=measured_slope_h,
            now=self._finite(now_monotonic),
        )

        external = self._finite(ext_temp)
        context = ReferenceGovernorAuthorityContext(
            target_temp=target if target is not None else reference,
            nominal_reference=reference,
            current_temp=self._finite(current_temp) or 0.0,
            ext_temp=external if external is not None else 0.0,
            hvac_mode=hvac_mode,
            a=self._finite(a) or 1.0,
            b=self._finite(b) or 0.01,
            committed_u=self._finite(committed_u) or 0.0,
            remaining_cycle_min=self._finite(remaining_cycle_min) or 0.0,
            stop_deadtime_s=self._finite(stop_deadtime_s) or 1.0,
            measured_slope_h=(
                None if measured_slope_h is None else self._finite(measured_slope_h)
            ),
            bypass_reason=bypass_reason,
        )
        self._pending_context = context

        if target is not None:
            target_changed = self._episode_target != target
            if target_changed:
                self._episode_target = target
            if bypass_reason is not None:
                self._runtime.reset()
            elif target_changed or self._runtime.target_temp is None:
                self._runtime.arm(target, self._finite(now_monotonic) or 0.0)
        elif bypass_reason is not None:
            self._runtime.reset()
        return context

    def resolve(
        self,
        requested_u: object,
        now_monotonic: object,
        cycle_min: object,
        pi_snapshot: PIOutputSnapshot,
        *,
        thermal_bias_c_per_min: float = 0.0,
    ) -> ReferenceGovernorAuthorityDecision | None:
        """Evaluate the pending context once using the true pre-cap command."""
        context = self._pending_context
        self._pending_context = None
        if context is None:
            return None
        if context.bypass_reason is not None:
            return self._bypass_result(
                context.bypass_reason, context.nominal_reference, now_monotonic
            )

        requested = self._finite(requested_u)
        cycle = self._finite(cycle_min)
        if requested is None or not 0.0 <= requested <= 1.0:
            return self._bypass_result(
                "authority_invalid_requested_u", context.nominal_reference, now_monotonic
            )
        if cycle is None or cycle <= 0.0:
            return self._bypass_result(
                "authority_invalid_cycle", context.nominal_reference, now_monotonic
            )
        if pi_snapshot is None:
            return self._bypass_result(
                "authority_missing_pi_snapshot", context.nominal_reference, now_monotonic
            )

        kernel_decision = evaluate_reference_governor(
            ReferenceGovernorInput(
                target_temp=context.target_temp,
                nominal_reference=context.nominal_reference,
                current_temp=context.current_temp,
                ext_temp=context.ext_temp,
                hvac_mode=context.hvac_mode,
                a=context.a,
                b=context.b,
                committed_u=context.committed_u,
                next_cycle_u=requested,
                remaining_cycle_min=context.remaining_cycle_min,
                cycle_min=cycle,
                stop_deadtime_s=context.stop_deadtime_s,
                measured_slope_h=context.measured_slope_h,
                pi_snapshot=pi_snapshot,
                thermal_bias_c_per_min=thermal_bias_c_per_min,
            ),
            ReferenceGovernorPolicy(
                reserve_selector=REFERENCE_GOVERNOR_RESERVE_SELECTOR,
                rho_margin=REFERENCE_GOVERNOR_RHO_MARGIN,
                margin_max_c=REFERENCE_GOVERNOR_MARGIN_MAX_C,
                prediction_horizon_min=context.stop_deadtime_s / 60.0,
                command_comparison_epsilon=REFERENCE_GOVERNOR_COMMAND_EPSILON,
                reference_comparison_epsilon_c=REFERENCE_GOVERNOR_REFERENCE_EPSILON_C,
            ),
        )
        runtime_decision = self._runtime.consume(kernel_decision, now_monotonic, cycle)
        return self._result(kernel_decision, runtime_decision)

    def reset(self) -> None:
        """Clear pending context, episode identity, and runtime evidence."""
        self._pending_context = None
        self._episode_target = None
        self._runtime.reset()

    def load_state(self, state: object | None = None) -> None:
        """Clear all transient state at a load boundary; nothing is restored."""
        del state
        self.reset()

    @staticmethod
    def _finite(value: object) -> float | None:
        """Return a finite number, excluding booleans."""
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric if isfinite(numeric) else None

    @classmethod
    def _validation_reason(cls, **values: object) -> str | None:
        """Return the first explicit reason for rejecting a prepared context."""
        if values["hvac_mode"] not in (
            VThermHvacMode_HEAT,
            VThermHvacMode_COOL,
        ):
            return "authority_bypass_unsupported_mode"
        if values["trajectory_active"] is not True:
            return "authority_bypass_trajectory_inactive"
        if values["trajectory_source"] != "setpoint":
            return "authority_bypass_disturbance"
        if values["target"] is None or values["nominal"] is None or values["current"] is None:
            return "authority_invalid_temperature"
        if values["external"] is None:
            return "authority_bypass_missing_ext_temp"
        if values["model_reliable"] is not True:
            return "authority_bypass_model_unreliable"
        if values["a"] is None or values["b"] is None or values["b"] <= 0.0:
            return "authority_invalid_model"
        if (
            values["hvac_mode"] == VThermHvacMode_HEAT
            and values["a"] <= 0.0
        ) or (
            values["hvac_mode"] == VThermHvacMode_COOL
            and values["a"] >= 0.0
        ):
            return "authority_invalid_model"
        if values["deadtime_reliable"] is not True:
            return "authority_bypass_deadtime_unreliable"
        if values["deadtime"] is None or values["deadtime"] <= 0.0:
            return "authority_invalid_deadtime"
        if values["committed"] is None or not 0.0 <= values["committed"] <= 1.0:
            return "authority_invalid_command"
        if values["remaining"] is None or values["remaining"] < 0.0:
            return "authority_invalid_cycle"
        if values["slope"] is None and values.get("slope_input") is not None:
            return "authority_invalid_slope"
        if values["now"] is None:
            return "authority_invalid_timestamp"
        return None

    def _bypass_result(
        self,
        reason: str,
        nominal_reference: float,
        now_monotonic: object,
    ) -> ReferenceGovernorAuthorityDecision:
        """Reset runtime and publish a no-cap result for invalid context."""
        self._runtime.reset()
        decision = ReferenceGovernorDecision(
            active=False,
            reason=reason,
            nominal_reference=nominal_reference,
            admissible_reference=nominal_reference,
            command_cap=None,
            predicted_terminal_temp=None,
            target_bound=None,
            reserve_candidates=None,
            dynamic_reserve_c=0.0,
            constraint_active=False,
            coast_required=False,
        )
        safe_now = self._finite(now_monotonic)
        runtime_decision = self._runtime.consume(
            decision,
            0.0 if safe_now is None else safe_now,
            1.0,
        )
        return self._result(decision, runtime_decision)

    @staticmethod
    def _result(
        kernel_decision: ReferenceGovernorDecision,
        runtime_decision: ReferenceGovernorRuntimeDecision,
    ) -> ReferenceGovernorAuthorityDecision:
        """Combine kernel and runtime evidence without exposing mutable state."""
        return ReferenceGovernorAuthorityDecision(
            phase=runtime_decision.phase,
            effective_reference=runtime_decision.effective_reference,
            command_cap=runtime_decision.command_cap,
            shaping_active=runtime_decision.shaping_active,
            handoff_ready=runtime_decision.handoff_ready,
            reason=runtime_decision.reason,
            kernel_decision=kernel_decision,
            runtime_decision=runtime_decision,
        )
