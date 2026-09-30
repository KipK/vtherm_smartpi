"""Pure predictive reference-governor mathematics for SmartPI.

The module works in physical temperature coordinates at its public
boundaries.  HVAC direction is applied only where a demand-direction
comparison is required, so HEAT and COOL share the same equations.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Literal

from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL, VThermHvacMode_HEAT
from .controller import PIOutputSnapshot
from .deadband_output import ProportionalState
from .thermal_model import propagate_1r1c

ReserveSelector = Literal["slope", "model", "max"]
_RESERVE_SELECTORS = frozenset(("slope", "model", "max"))


@dataclass(frozen=True, slots=True)
class PredictionSegment:
    """One constant-command segment of a 1R1C prediction."""

    horizon_min: float
    command: float


@dataclass(frozen=True, slots=True)
class SegmentedPrediction:
    """Temperature at each segment boundary and at the terminal horizon."""

    temperatures: tuple[float, ...]
    terminal_temp: float
    total_horizon_min: float


@dataclass(frozen=True, slots=True)
class SignedModel:
    """1R1C model expressed in positive demand-direction coordinates."""

    direction: float
    a_signed: float
    b: float


@dataclass(frozen=True, slots=True)
class DynamicReserveCandidates:
    """Reserve values from measured slope, model travel, and their maximum."""

    slope_reserve_c: float
    model_reserve_c: float
    reserve_c: float


@dataclass(frozen=True, slots=True)
class ReferenceGovernorPolicy:
    """Explicit policy values used by one pure governor evaluation."""

    reserve_selector: ReserveSelector
    rho_margin: float
    margin_max_c: float
    prediction_horizon_min: float
    command_comparison_epsilon: float
    reference_comparison_epsilon_c: float


@dataclass(frozen=True, slots=True)
class CommandCapDecision:
    """Bounded command cap and the properties needed by the governor."""

    raw_cap: float
    command_cap: float
    coast_required: bool
    constraint_active: bool


@dataclass(frozen=True, slots=True)
class PReferenceInversion:
    """Result of converting a command cap into a proportional reference."""

    command_cap: float
    available_p: float
    proportional_error: float
    raw_error: float
    reference: float


@dataclass(frozen=True, slots=True)
class ReferenceGovernorInput:
    """Explicit observations consumed by the pure governor kernel."""

    target_temp: float
    nominal_reference: float
    current_temp: float
    ext_temp: float
    hvac_mode: VThermHvacMode
    a: float
    b: float
    committed_u: float
    next_cycle_u: float
    remaining_cycle_min: float
    cycle_min: float
    stop_deadtime_s: float
    measured_slope_h: float | None
    pi_snapshot: PIOutputSnapshot
    thermal_bias_c_per_min: float = 0.0


@dataclass(frozen=True, slots=True)
class ReferenceGovernorDecision:
    """Pure governor decision, including its mathematical evidence."""

    active: bool
    reason: str
    nominal_reference: float
    admissible_reference: float
    command_cap: float | None
    predicted_terminal_temp: float | None
    target_bound: float | None
    reserve_candidates: DynamicReserveCandidates | None
    dynamic_reserve_c: float
    constraint_active: bool
    coast_required: bool


def _finite(value: object) -> float | None:
    """Return a finite float, or None for an invalid numeric value."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _direction(hvac_mode: VThermHvacMode | None) -> float | None:
    """Return +1 for HEAT, -1 for COOL, and None for unsupported modes."""
    if hvac_mode == VThermHvacMode_HEAT:
        return 1.0
    if hvac_mode == VThermHvacMode_COOL:
        return -1.0
    return None


def normalize_signed(value: float, hvac_mode: VThermHvacMode) -> float | None:
    """Normalize a physical delta into the active demand direction."""
    numeric = _finite(value)
    direction = _direction(hvac_mode)
    if numeric is None or direction is None:
        return None
    return direction * numeric


def signed_demand_delta(
    target_temp: float,
    current_temp: float,
    hvac_mode: VThermHvacMode,
) -> float | None:
    """Return ``direction * (target_temp - current_temp)``."""
    target = _finite(target_temp)
    current = _finite(current_temp)
    if target is None or current is None:
        return None
    return normalize_signed(target - current, hvac_mode)


def normalize_model(
    a: float,
    b: float,
    hvac_mode: VThermHvacMode,
) -> SignedModel | None:
    """Validate physical model signs and return its signed representation.

    The physical contract is ``a > 0`` for HEAT, ``a < 0`` for COOL, and
    ``b > 0`` for both modes.  The returned active gain is always positive.
    """
    a_value = _finite(a)
    b_value = _finite(b)
    direction = _direction(hvac_mode)
    if a_value is None or b_value is None or direction is None:
        return None
    if b_value <= 0.0 or direction * a_value <= 0.0:
        return None
    signed_a = direction * a_value
    if not isfinite(signed_a):
        return None
    return SignedModel(direction, signed_a, b_value)


def _propagate(
    *,
    current_temp: float,
    ext_temp: float,
    a: float,
    b: float,
    horizon_min: float,
    command: float,
    thermal_bias_c_per_min: float,
) -> float | None:
    """Propagate one constant-command affine 1R1C segment."""
    if horizon_min == 0.0:
        return current_temp
    try:
        terminal = propagate_1r1c(
            temperature=current_temp,
            external_temperature=ext_temp,
            a=a,
            b=b,
            power=command,
            duration_min=horizon_min,
            bias=thermal_bias_c_per_min,
        )
    except (OverflowError, ZeroDivisionError):
        return None
    if not isfinite(terminal):
        return None
    return terminal


def predict_segmented_1r1c(
    *,
    current_temp: float,
    ext_temp: float,
    a: float,
    b: float,
    hvac_mode: VThermHvacMode,
    segments: Iterable[PredictionSegment],
    thermal_bias_c_per_min: float = 0.0,
) -> SegmentedPrediction | None:
    """Predict a sequence of constant-command 1R1C segments exactly.

    Segment durations may be zero.  Negative durations, out-of-range
    commands, invalid models, and nonfinite intermediates fail closed.
    """
    current = _finite(current_temp)
    external = _finite(ext_temp)
    thermal_bias = _finite(thermal_bias_c_per_min)
    model = normalize_model(a, b, hvac_mode)
    if current is None or external is None or thermal_bias is None or model is None:
        return None
    try:
        segment_values = tuple(segments)
    except TypeError:
        return None

    validated: list[tuple[float, float]] = []
    for segment in segment_values:
        if not isinstance(segment, PredictionSegment):
            return None
        horizon = _finite(segment.horizon_min)
        command = _finite(segment.command)
        if (
            horizon is None
            or command is None
            or horizon < 0.0
            or command < 0.0
            or command > 1.0
        ):
            return None
        validated.append((horizon, command))

    temperature = current
    temperatures: list[float] = []
    total_horizon = 0.0
    for horizon, command in validated:
        temperature = _propagate(
            current_temp=temperature,
            ext_temp=external,
            a=model.direction * model.a_signed,
            b=model.b,
            horizon_min=horizon,
            command=command,
            thermal_bias_c_per_min=thermal_bias,
        )
        if temperature is None:
            return None
        total_horizon += horizon
        if not isfinite(total_horizon):
            return None
        temperatures.append(temperature)

    return SegmentedPrediction(tuple(temperatures), temperature, total_horizon)


def _bounded_reserve(value: float, margin_max_c: float) -> float | None:
    """Clamp a nonnegative reserve without allowing nonfinite arithmetic."""
    if not isfinite(value):
        return None
    return min(max(value, 0.0), margin_max_c)


def compute_dynamic_reserve_candidates(
    *,
    hvac_mode: VThermHvacMode,
    measured_slope_h: float | None,
    predicted_temperature_change_c: float | None,
    stop_deadtime_s: float,
    rho_margin: float,
    margin_max_c: float,
) -> DynamicReserveCandidates | None:
    """Return slope, model, and maximum-prudent dynamic reserve candidates.

    The model change is expected to have been predicted over the same
    slowdown horizon as ``stop_deadtime_s``.  Missing optional evidence
    contributes zero; invalid supplied evidence fails closed.
    """
    deadtime = _finite(stop_deadtime_s)
    rho = _finite(rho_margin)
    margin_max = _finite(margin_max_c)
    direction = _direction(hvac_mode)
    if (
        deadtime is None
        or rho is None
        or margin_max is None
        or direction is None
        or deadtime < 0.0
        or rho < 0.0
        or margin_max < 0.0
    ):
        return None

    slope = 0.0 if measured_slope_h is None else _finite(measured_slope_h)
    model_change = (
        0.0
        if predicted_temperature_change_c is None
        else _finite(predicted_temperature_change_c)
    )
    if slope is None or model_change is None:
        return None

    signed_slope = direction * slope
    signed_model_change = direction * model_change
    try:
        slope_value = rho * max(signed_slope, 0.0) * deadtime / 3600.0
        model_value = rho * max(signed_model_change, 0.0)
    except OverflowError:
        return None
    slope_reserve = _bounded_reserve(slope_value, margin_max)
    model_reserve = _bounded_reserve(model_value, margin_max)
    if slope_reserve is None or model_reserve is None:
        return None
    return DynamicReserveCandidates(
        slope_reserve_c=slope_reserve,
        model_reserve_c=model_reserve,
        reserve_c=max(slope_reserve, model_reserve),
    )


def select_dynamic_reserve(
    candidates: DynamicReserveCandidates,
    selector: str,
) -> float | None:
    """Select one observable reserve candidate without applying hidden policy."""
    if not isinstance(candidates, DynamicReserveCandidates):
        return None
    if selector == "slope":
        return candidates.slope_reserve_c
    if selector == "model":
        return candidates.model_reserve_c
    if selector == "max":
        return candidates.reserve_c
    return None


def resolve_command_cap(
    *,
    current_temp: float,
    target_bound: float,
    passive_temp: float,
    gain_u: float,
    hvac_mode: VThermHvacMode,
    requested_u: float,
    command_comparison_epsilon: float,
) -> CommandCapDecision | None:
    """Solve and bound the command that reaches a directional temperature bound."""
    current = _finite(current_temp)
    bound = _finite(target_bound)
    passive = _finite(passive_temp)
    physical_gain = _finite(gain_u)
    requested = _finite(requested_u)
    comparison_epsilon = _finite(command_comparison_epsilon)
    direction = _direction(hvac_mode)
    if (
        current is None
        or bound is None
        or passive is None
        or physical_gain is None
        or requested is None
        or comparison_epsilon is None
        or direction is None
        or direction * physical_gain <= 0.0
        or not 0.0 <= requested <= 1.0
        or comparison_epsilon < 0.0
    ):
        return None

    signed_gain = direction * physical_gain
    raw_cap = (direction * (bound - current) - direction * (passive - current)) / signed_gain
    if not isfinite(raw_cap):
        return None
    command_cap = min(max(raw_cap, 0.0), 1.0)
    constraint_active = command_cap < requested - comparison_epsilon
    return CommandCapDecision(
        raw_cap=raw_cap,
        command_cap=command_cap,
        coast_required=raw_cap <= comparison_epsilon,
        constraint_active=constraint_active,
    )


def invert_p_reference_from_cap(
    *,
    command_cap: float,
    current_temp: float,
    hvac_mode: VThermHvacMode,
    pi_snapshot: PIOutputSnapshot,
) -> PReferenceInversion | None:
    """Invert the controller's affine P path, including its deadzone.

    At exactly zero proportional contribution, the closest reference to the
    current temperature is returned because a deadzone has a non-unique
    inverse there. The snapshot carries the effective post-compute I/FF terms.
    """
    cap = _finite(command_cap)
    current = _finite(current_temp)
    if not _valid_pi_snapshot(pi_snapshot):
        return None
    kp_value = _finite(pi_snapshot.kp)
    i_power = _finite(pi_snapshot.u_i)
    feedforward = _finite(pi_snapshot.u_ff)
    threshold = _finite(pi_snapshot.p_state.threshold)
    direction = _direction(hvac_mode)
    if (
        cap is None
        or current is None
        or kp_value is None
        or i_power is None
        or feedforward is None
        or threshold is None
        or direction is None
        or not 0.0 <= cap <= 1.0
        or kp_value <= 0.0
        or threshold < 0.0
    ):
        return None

    available_p = cap - feedforward - i_power
    proportional_error = available_p / kp_value
    if not isfinite(available_p) or not isfinite(proportional_error):
        return None
    if proportional_error > 0.0:
        raw_error = proportional_error + threshold
    elif proportional_error < 0.0:
        raw_error = proportional_error - threshold
    else:
        raw_error = 0.0
    reference = current + direction * raw_error
    if not isfinite(raw_error) or not isfinite(reference):
        return None
    return PReferenceInversion(
        command_cap=cap,
        available_p=available_p,
        proportional_error=proportional_error,
        raw_error=raw_error,
        reference=reference,
    )


def _valid_pi_snapshot(value: object) -> bool:
    """Reject incomplete or non-finite controller evidence before projection."""
    if not isinstance(value, PIOutputSnapshot) or not isinstance(value.p_state, ProportionalState):
        return False
    state = value.p_state
    numeric = (value.kp, value.u_i, value.u_ff, state.deadband_c)
    if not all(type(item) in (int, float) and _finite(item) is not None for item in numeric):
        return False
    return (
        value.kp > 0.0
        and state.deadband_c >= 0.0
        and type(state.freeze_deadband) is bool
        and type(state.deadband_allow_p) is bool
        and type(state.edge_count) is int and state.edge_count >= 0
        and (state.edge_sign is None or type(state.edge_sign) in (int, float)
             and state.edge_sign in (-1.0, 1.0))
    )


def _invalid_decision(governor_input: ReferenceGovernorInput, reason: str) -> ReferenceGovernorDecision:
    """Return an explicit no-decision result for invalid aggregate input."""
    nominal = _finite(governor_input.nominal_reference)
    current = _finite(governor_input.current_temp)
    fallback = current if current is not None else nominal
    if fallback is None:
        fallback = 0.0
    return ReferenceGovernorDecision(
        active=False,
        reason=reason,
        nominal_reference=nominal if nominal is not None else fallback,
        admissible_reference=fallback,
        command_cap=None,
        predicted_terminal_temp=None,
        target_bound=None,
        reserve_candidates=None,
        dynamic_reserve_c=0.0,
        constraint_active=False,
        coast_required=False,
    )


def _predict_two_segments(
    *,
    current_temp: float,
    ext_temp: float,
    a: float,
    b: float,
    hvac_mode: VThermHvacMode,
    first_horizon: float,
    first_command: float,
    second_horizon: float,
    second_command: float,
    thermal_bias_c_per_min: float,
) -> SegmentedPrediction | None:
    """Predict the committed segment followed by one candidate segment."""
    return predict_segmented_1r1c(
        current_temp=current_temp,
        ext_temp=ext_temp,
        a=a,
        b=b,
        hvac_mode=hvac_mode,
        thermal_bias_c_per_min=thermal_bias_c_per_min,
        segments=(
            PredictionSegment(first_horizon, first_command),
            PredictionSegment(second_horizon, second_command),
        ),
    )


def evaluate_reference_governor(
    governor_input: ReferenceGovernorInput,
    policy: ReferenceGovernorPolicy,
) -> ReferenceGovernorDecision:
    """Evaluate one predictive decision without applying runtime activation policy.

    The selector and all timing/margin values are supplied by the immutable
    policy so this kernel does not choose runtime activation policy.
    """
    if not isinstance(governor_input, ReferenceGovernorInput):
        raise TypeError("governor_input must be ReferenceGovernorInput")
    if not isinstance(policy, ReferenceGovernorPolicy):
        raise TypeError("policy must be ReferenceGovernorPolicy")
    if (
        not isinstance(policy.reserve_selector, str)
        or policy.reserve_selector not in _RESERVE_SELECTORS
    ):
        return _invalid_decision(governor_input, "unsupported_reserve_selector")

    numeric_fields = (
        governor_input.target_temp,
        governor_input.nominal_reference,
        governor_input.current_temp,
        governor_input.ext_temp,
        governor_input.committed_u,
        governor_input.next_cycle_u,
        governor_input.remaining_cycle_min,
        governor_input.cycle_min,
        governor_input.stop_deadtime_s,
        governor_input.thermal_bias_c_per_min,
        policy.rho_margin,
        policy.margin_max_c,
        policy.prediction_horizon_min,
        policy.command_comparison_epsilon,
        policy.reference_comparison_epsilon_c,
    )
    normalized_fields = tuple(_finite(value) for value in numeric_fields)
    if any(value is None for value in normalized_fields):
        return _invalid_decision(governor_input, "invalid_numeric_input")
    (
        target_temp,
        nominal_reference,
        current_temp,
        ext_temp,
        committed_u,
        next_cycle_u,
        remaining_cycle_min,
        cycle_min,
        stop_deadtime_s,
        thermal_bias_c_per_min,
        rho_margin,
        margin_max_c,
        prediction_horizon,
        command_comparison_epsilon,
        reference_comparison_epsilon_c,
    ) = normalized_fields
    if any(
        value < 0.0 or value > 1.0
        for value in (committed_u, next_cycle_u)
    ):
        return _invalid_decision(governor_input, "invalid_command")
    if any(
        value < 0.0
        for value in (
            remaining_cycle_min,
            cycle_min,
            stop_deadtime_s,
            prediction_horizon,
            command_comparison_epsilon,
            reference_comparison_epsilon_c,
        )
    ):
        return _invalid_decision(governor_input, "invalid_horizon")
    if not _valid_pi_snapshot(governor_input.pi_snapshot):
        return _invalid_decision(governor_input, "invalid_pi_snapshot")
    model = normalize_model(governor_input.a, governor_input.b, governor_input.hvac_mode)
    if model is None:
        return _invalid_decision(governor_input, "invalid_model")
    a_value = _finite(governor_input.a)
    b_value = _finite(governor_input.b)
    if a_value is None or b_value is None:
        return _invalid_decision(governor_input, "invalid_model")

    demand = signed_demand_delta(target_temp, current_temp, governor_input.hvac_mode)
    if demand is None:
        return _invalid_decision(governor_input, "invalid_demand")
    if demand <= 0.0:
        return ReferenceGovernorDecision(
            active=False,
            reason="no_positive_demand",
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

    nominal_prediction = _predict_two_segments(
        current_temp=current_temp,
        ext_temp=ext_temp,
        a=a_value,
        b=b_value,
        hvac_mode=governor_input.hvac_mode,
        first_horizon=remaining_cycle_min,
        first_command=committed_u,
        second_horizon=prediction_horizon,
        second_command=next_cycle_u,
        thermal_bias_c_per_min=thermal_bias_c_per_min,
    )
    passive_prediction = _predict_two_segments(
        current_temp=current_temp,
        ext_temp=ext_temp,
        a=a_value,
        b=b_value,
        hvac_mode=governor_input.hvac_mode,
        first_horizon=remaining_cycle_min,
        first_command=committed_u,
        second_horizon=prediction_horizon,
        second_command=0.0,
        thermal_bias_c_per_min=thermal_bias_c_per_min,
    )
    if nominal_prediction is None or passive_prediction is None:
        return _invalid_decision(governor_input, "prediction_failed")

    model_change = nominal_prediction.terminal_temp - current_temp
    reserves = compute_dynamic_reserve_candidates(
        hvac_mode=governor_input.hvac_mode,
        measured_slope_h=governor_input.measured_slope_h,
        predicted_temperature_change_c=model_change,
        stop_deadtime_s=stop_deadtime_s,
        rho_margin=rho_margin,
        margin_max_c=margin_max_c,
    )
    if reserves is None:
        return _invalid_decision(governor_input, "invalid_reserve_evidence")
    selected_reserve = select_dynamic_reserve(reserves, policy.reserve_selector)
    if selected_reserve is None:
        return _invalid_decision(governor_input, "unsupported_reserve_selector")
    direction = model.direction
    target_bound = target_temp - direction * selected_reserve

    gain_prediction = _predict_two_segments(
        current_temp=current_temp,
        ext_temp=ext_temp,
        a=a_value,
        b=b_value,
        hvac_mode=governor_input.hvac_mode,
        first_horizon=remaining_cycle_min,
        first_command=committed_u,
        second_horizon=prediction_horizon,
        second_command=1.0,
        thermal_bias_c_per_min=thermal_bias_c_per_min,
    )
    if gain_prediction is None:
        return _invalid_decision(governor_input, "prediction_failed")
    gain_u = (gain_prediction.terminal_temp - passive_prediction.terminal_temp)
    cap_decision = resolve_command_cap(
        current_temp=current_temp,
        target_bound=target_bound,
        passive_temp=passive_prediction.terminal_temp,
        gain_u=gain_u,
        hvac_mode=governor_input.hvac_mode,
        requested_u=next_cycle_u,
        command_comparison_epsilon=command_comparison_epsilon,
    )
    if cap_decision is None:
        return _invalid_decision(governor_input, "invalid_gain_horizon")

    inversion = invert_p_reference_from_cap(
        command_cap=cap_decision.command_cap,
        current_temp=current_temp,
        hvac_mode=governor_input.hvac_mode,
        pi_snapshot=governor_input.pi_snapshot,
    )
    if inversion is None:
        return _invalid_decision(governor_input, "p_inversion_failed")

    nominal_error = direction * (nominal_reference - current_temp)
    cap_error = min(max(direction * (inversion.reference - current_temp), 0.0), max(nominal_error, 0.0))
    admissible_reference = current_temp + direction * cap_error
    if abs(
        governor_input.pi_snapshot.project_p(nominal_error)
        - governor_input.pi_snapshot.project_p(cap_error)
    ) <= command_comparison_epsilon:
        admissible_reference = nominal_reference
    reference_constraint = (
        direction * (nominal_reference - admissible_reference)
        > reference_comparison_epsilon_c
    )
    constraint_active = cap_decision.constraint_active or reference_constraint
    reason = "coast" if cap_decision.coast_required else ("cap" if constraint_active else "unconstrained")
    terminal_prediction = _predict_two_segments(
        current_temp=current_temp,
        ext_temp=ext_temp,
        a=a_value,
        b=b_value,
        hvac_mode=governor_input.hvac_mode,
        first_horizon=remaining_cycle_min,
        first_command=committed_u,
        second_horizon=prediction_horizon,
        second_command=cap_decision.command_cap,
        thermal_bias_c_per_min=thermal_bias_c_per_min,
    )
    if terminal_prediction is None:
        return _invalid_decision(governor_input, "prediction_failed")
    return ReferenceGovernorDecision(
        active=constraint_active,
        reason=reason,
        nominal_reference=nominal_reference,
        admissible_reference=admissible_reference,
        command_cap=cap_decision.command_cap,
        predicted_terminal_temp=terminal_prediction.terminal_temp,
        target_bound=target_bound,
        reserve_candidates=reserves,
        dynamic_reserve_c=selected_reserve,
        constraint_active=constraint_active,
        coast_required=cap_decision.coast_required,
    )
