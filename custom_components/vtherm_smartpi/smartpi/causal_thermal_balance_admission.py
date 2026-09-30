"""Bounded admission of a persistent causal thermal-balance bias."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, expm1, isfinite

from .causal_power_trace import CausalPowerTrace
from .causal_thermal_balance import evaluate_causal_thermal_balance
from .thermal_measurement import ThermalMeasurement


@dataclass(frozen=True)
class ThermalBalanceContext:
    """Operating conditions; resolution must be a known positive sensor step."""

    hvac_mode: str
    a: float
    b: float
    deadtime_s: float
    deadtime_reliable: bool
    cycle_s: float
    sensor_resolution_c: float
    sensor_noise_bound_c: float
    model_revision: object
    setpoint_revision: object
    actuator_revision: object


@dataclass(frozen=True)
class ThermalBalanceAdmissionResult:
    """Current admitted value and reason for the latest observation."""

    reason: str
    thermal_bias_c_per_min: float | None
    equivalent_power_bias: float | None
    evidence_count: int
    pending: bool
    valid_until_monotonic: float | None
    min_window_s: float


@dataclass(frozen=True)
class _Evidence:
    start_monotonic: float
    end_monotonic: float
    bias: float
    uncertainty: float


class CausalThermalBalanceAdmission:
    """Admit only three disjoint, resolved and compatible estimates."""

    def __init__(
        self, *, max_measurement_age_s: float, admitted_ttl_s: float
    ) -> None:
        if not all(
            isfinite(value) and value > 0.0
            for value in (max_measurement_age_s, admitted_ttl_s)
        ):
            raise ValueError("expiration intervals must be finite and positive")
        self._max_measurement_age_s = float(max_measurement_age_s)
        self._admitted_ttl_s = float(admitted_ttl_s)
        self._context: ThermalBalanceContext | None = None
        self._epoch: int | None = None
        self._washout_until = float("-inf")
        self._anchor: ThermalMeasurement | None = None
        self._pending_end: ThermalMeasurement | None = None
        self._evidence: list[_Evidence] = []
        self._admitted_bias: float | None = None
        self._valid_until: float | None = None

    def reset(
        self,
        *,
        context: ThermalBalanceContext,
        power_trace: CausalPowerTrace,
        now_monotonic: float,
    ) -> ThermalBalanceAdmissionResult:
        """Invalidate all admission state after an external runtime reset."""
        now = float(now_monotonic)
        if not isfinite(now):
            raise ValueError("now_monotonic must be finite")
        self._reset(context, power_trace.epoch, now)
        return self._result("external_reset", context)

    def observe(
        self,
        *,
        measurement: ThermalMeasurement | None,
        context: ThermalBalanceContext,
        power_trace: CausalPowerTrace,
        now_monotonic: float,
    ) -> ThermalBalanceAdmissionResult:
        """Consume a distinct measurement, or retry a pending trace with None."""
        now = float(now_monotonic)
        if not isfinite(now):
            raise ValueError("now_monotonic must be finite")
        if measurement is not None and measurement.epoch != power_trace.epoch:
            self._reset(context, power_trace.epoch, now)
            return self._result("epoch_mismatch", context)

        expired = self._expire(now)
        if not self._valid_context(context):
            self._clear_evidence()
            self._admitted_bias = None
            self._valid_until = None
            return self._result("invalid_context", context)
        transition = self._transition(context, power_trace.epoch, now)
        if transition is not None:
            return self._result(transition, context)
        if now < self._washout_until:
            return self._result("deadtime_washout", context)
        if not context.deadtime_reliable:
            self._clear_evidence()
            return self._result("deadtime_unreliable", context)

        if self._pending_end is not None:
            pending = self._evaluate_pending(context, power_trace, now)
            if pending == "trace_pending":
                return self._result(pending, context)
            if measurement is None:
                return self._result(pending, context)
        if measurement is None:
            reason = "admission_expired" if expired else "no_measurement"
            return self._result(reason, context)
        if measurement.observed_monotonic > now + 1e-6:
            return self._result("measurement_in_future", context)
        if now - measurement.observed_monotonic > self._max_measurement_age_s:
            return self._result("measurement_stale", context)
        if measurement.observed_monotonic < self._washout_until:
            return self._result("deadtime_washout", context)
        if self._anchor is None:
            self._anchor = measurement
            return self._result("anchor_recorded", context)
        if measurement.observed_monotonic <= self._anchor.observed_monotonic:
            return self._result("measurement_not_newer", context)
        if measurement.observed_monotonic - self._anchor.observed_monotonic < self._min_window(context):
            return self._result("window_too_short", context)
        self._pending_end = measurement
        return self._result(self._evaluate_pending(context, power_trace, now), context)

    def _evaluate_pending(
        self, context: ThermalBalanceContext, trace: CausalPowerTrace, now: float
    ) -> str:
        start = self._anchor
        end = self._pending_end
        assert start is not None and end is not None
        result = evaluate_causal_thermal_balance(
            start=start,
            end=end,
            power_trace=trace,
            a=context.a,
            b=context.b,
            deadtime_s=context.deadtime_s,
            hvac_mode=context.hvac_mode,
            now_monotonic=now,
            max_measurement_age_s=self._max_measurement_age_s,
        )
        if result.reason == "trace_pending":
            return result.reason
        self._pending_end = None
        self._anchor = end
        if not result.admissible:
            self._evidence.clear()
            return result.reason
        assert result.thermal_bias_c_per_min is not None
        assert result.endpoint_residual_c is not None
        gain = -expm1(-context.b * result.duration_s / 60.0) / context.b
        measurement_uncertainty = (
            context.sensor_resolution_c / 2.0
            + context.sensor_noise_bound_c
        )
        endpoint_uncertainty = (
            measurement_uncertainty
            * (1.0 + exp(-context.b * result.duration_s / 60.0))
        )
        if abs(result.endpoint_residual_c) <= endpoint_uncertainty:
            self._evidence.clear()
            self._revoke_admission()
            return "effect_below_resolution"
        evidence = _Evidence(
            start.observed_monotonic,
            end.observed_monotonic,
            result.thermal_bias_c_per_min,
            endpoint_uncertainty / gain,
        )
        if self._evidence and (
            evidence.start_monotonic < self._evidence[-1].end_monotonic
            or evidence.bias * self._evidence[-1].bias <= 0.0
        ):
            self._evidence.clear()
            self._evidence.append(evidence)
            self._revoke_admission()
            return "inconsistent_sign"
        self._evidence.append(evidence)
        self._evidence = self._evidence[-3:]
        if len(self._evidence) < 3:
            return "insufficient_windows"
        lower = max(item.bias - item.uncertainty for item in self._evidence)
        upper = min(item.bias + item.uncertainty for item in self._evidence)
        if lower > upper:
            self._evidence.clear()
            self._evidence.append(evidence)
            self._revoke_admission()
            return "inconsistent_dispersion"
        self._admitted_bias = (lower + upper) / 2.0
        self._valid_until = end.observed_monotonic + self._admitted_ttl_s
        if self._expire(now):
            return "admission_expired"
        return "admitted"

    def _transition(
        self, context: ThermalBalanceContext, epoch: int, now: float
    ) -> str | None:
        previous = self._context
        if (
            previous is None
            or self._epoch != epoch
            or self._structural(previous) != self._structural(context)
        ):
            self._reset(context, epoch, now)
            return "structural_reset"
        if self._sampling(previous) != self._sampling(context):
            self._context = context
            self._clear_evidence()
            self._revoke_admission()
            self._washout_until = max(
                self._washout_until,
                now + max(context.deadtime_s, 0.0),
            )
            return "sampling_changed"
        if previous.setpoint_revision != context.setpoint_revision:
            self._context = context
            self._clear_evidence()
            self._washout_until = max(
                self._washout_until,
                now + max(context.deadtime_s, 0.0),
            )
            return "setpoint_changed"
        return None

    @staticmethod
    def _structural(context: ThermalBalanceContext) -> tuple[object, ...]:
        return (
            context.hvac_mode,
            context.a,
            context.b,
            context.deadtime_s,
            context.deadtime_reliable,
            context.model_revision,
            context.actuator_revision,
        )

    @staticmethod
    def _sampling(context: ThermalBalanceContext) -> tuple[float, float, float]:
        return (
            context.cycle_s,
            context.sensor_resolution_c,
            context.sensor_noise_bound_c,
        )

    @staticmethod
    def _valid_context(context: ThermalBalanceContext) -> bool:
        return (
            context.hvac_mode in ("heat", "cool")
            and all(
                isfinite(value)
                for value in (
                    context.a, context.b, context.deadtime_s,
                    context.cycle_s, context.sensor_resolution_c,
                    context.sensor_noise_bound_c,
                )
            )
            and context.b > 0.0
            and context.deadtime_s >= 0.0
            and context.cycle_s > 0.0
            and context.sensor_resolution_c > 0.0
            and context.sensor_noise_bound_c >= 0.0
            and context.model_revision is not None
            and context.setpoint_revision is not None
            and context.actuator_revision is not None
            and (
                context.a > 0.0
                if context.hvac_mode == "heat"
                else context.a < 0.0
            )
        )

    @staticmethod
    def _min_window(context: ThermalBalanceContext) -> float:
        return max(2.0 * context.deadtime_s, 6.0 * context.cycle_s, 1800.0)

    def _reset(self, context: ThermalBalanceContext, epoch: int, now: float) -> None:
        self._context = context
        self._epoch = epoch
        self._washout_until = now + max(context.deadtime_s, 0.0)
        self._clear_evidence()
        self._admitted_bias = None
        self._valid_until = None

    def _clear_evidence(self) -> None:
        self._anchor = None
        self._pending_end = None
        self._evidence.clear()

    def _expire(self, now: float) -> bool:
        if self._valid_until is not None and now > self._valid_until:
            self._revoke_admission()
            self._clear_evidence()
            return True
        return False

    def _revoke_admission(self) -> None:
        self._admitted_bias = None
        self._valid_until = None

    def _result(
        self,
        reason: str,
        context: ThermalBalanceContext,
    ) -> ThermalBalanceAdmissionResult:
        return ThermalBalanceAdmissionResult(
            reason=reason,
            thermal_bias_c_per_min=self._admitted_bias,
            equivalent_power_bias=(
                self._admitted_bias / context.a
                if self._admitted_bias is not None and context.a != 0.0 else None
            ),
            evidence_count=len(self._evidence),
            pending=self._pending_end is not None,
            valid_until_monotonic=self._valid_until,
            min_window_s=self._min_window(context),
        )
