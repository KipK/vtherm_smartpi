"""Focused tests for the pure SmartPI reference-governor kernel."""

from dataclasses import FrozenInstanceError, replace
from math import exp

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
)
from custom_components.vtherm_smartpi.smartpi.controller import PIOutputSnapshot
from custom_components.vtherm_smartpi.smartpi.deadband_output import ProportionalState
from custom_components.vtherm_smartpi.smartpi.reference_governor import (
    PredictionSegment,
    ReferenceGovernorInput,
    ReferenceGovernorPolicy,
    compute_dynamic_reserve_candidates,
    evaluate_reference_governor,
    invert_p_reference_from_cap,
    normalize_model,
    normalize_signed,
    predict_segmented_1r1c,
    resolve_command_cap,
    select_dynamic_reserve,
    signed_demand_delta,
)


def pi_snapshot(kp: float, i_power: float, ff: float, threshold: float) -> PIOutputSnapshot:
    return PIOutputSnapshot(
        kp=kp, u_i=i_power, u_ff=ff,
        p_state=ProportionalState(threshold, False, False, 0, None),
    )


def test_inputs_and_decisions_are_immutable_and_slotted() -> None:
    value = ReferenceGovernorInput(
        target_temp=22.0,
        nominal_reference=21.5,
        current_temp=21.0,
        ext_temp=10.0,
        hvac_mode=VThermHvacMode_HEAT,
        a=0.4,
        b=0.02,
        committed_u=0.5,
        next_cycle_u=0.4,
        remaining_cycle_min=1.0,
        cycle_min=10.0,
        stop_deadtime_s=120.0,
        measured_slope_h=None,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.1, 0.05),
    )
    assert hasattr(value, "__slots__")
    with pytest.raises(FrozenInstanceError):
        value.target_temp = 23.0


def test_signed_normalization_preserves_heat_cool_symmetry() -> None:
    assert normalize_signed(2.0, VThermHvacMode_HEAT) == 2.0
    assert normalize_signed(-2.0, VThermHvacMode_COOL) == 2.0
    assert signed_demand_delta(22.0, 21.0, VThermHvacMode_HEAT) == 1.0
    assert signed_demand_delta(20.0, 21.0, VThermHvacMode_COOL) == 1.0
    assert normalize_model(0.4, 0.02, VThermHvacMode_HEAT).a_signed == pytest.approx(0.4)
    assert normalize_model(-0.4, 0.02, VThermHvacMode_COOL).a_signed == pytest.approx(0.4)


def test_model_sign_contract_fails_closed() -> None:
    assert normalize_model(0.0, 0.02, VThermHvacMode_HEAT) is None
    assert normalize_model(-0.4, 0.02, VThermHvacMode_HEAT) is None
    assert normalize_model(0.4, 0.02, VThermHvacMode_COOL) is None
    assert normalize_model(-0.4, 0.0, VThermHvacMode_COOL) is None
    assert normalize_model(0.4, 0.02, VThermHvacMode_OFF) is None


def test_segmented_prediction_matches_affine_formula() -> None:
    first_h, first_u = 10.0, 0.5
    second_h, second_u = 5.0, 1.0
    current, external, a, b = 20.0, 10.0, 0.4, 0.02
    first = external + (current - external) * exp(-b * first_h) + (a / b) * (1.0 - exp(-b * first_h)) * first_u
    expected = external + (first - external) * exp(-b * second_h) + (a / b) * (1.0 - exp(-b * second_h)) * second_u
    result = predict_segmented_1r1c(
        current_temp=current,
        ext_temp=external,
        a=a,
        b=b,
        hvac_mode=VThermHvacMode_HEAT,
        segments=(PredictionSegment(first_h, first_u), PredictionSegment(second_h, second_u)),
    )
    assert result is not None
    assert result.temperatures == pytest.approx((first, expected))
    assert result.terminal_temp == pytest.approx(expected)


def test_segmented_prediction_applies_signed_bias_to_each_segment() -> None:
    segments = (PredictionSegment(4.0, 0.3), PredictionSegment(6.0, 0.7))
    heat = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=segments,
        thermal_bias_c_per_min=0.03,
    )
    cool = predict_segmented_1r1c(
        current_temp=0.0, ext_temp=10.0, a=-0.4, b=0.02,
        hvac_mode=VThermHvacMode_COOL, segments=segments,
        thermal_bias_c_per_min=-0.03,
    )
    baseline = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=segments,
    )
    explicit_zero = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=segments,
        thermal_bias_c_per_min=0.0,
    )
    assert heat is not None and cool is not None and baseline is not None
    assert baseline == explicit_zero
    for index, horizon in enumerate((4.0, 10.0)):
        expected_shift = 0.03 / 0.02 * (1.0 - exp(-0.02 * horizon))
        assert heat.temperatures[index] - baseline.temperatures[index] == pytest.approx(
            expected_shift
        )
        assert heat.temperatures[index] + cool.temperatures[index] == pytest.approx(20.0)
    assert predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=segments,
        thermal_bias_c_per_min=float("inf"),
    ) is None


def test_prediction_is_symmetric_for_mirrored_heat_and_cool_plants() -> None:
    heat = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT,
        segments=(PredictionSegment(10.0, 0.6), PredictionSegment(5.0, 0.2)),
    )
    cool = predict_segmented_1r1c(
        current_temp=0.0, ext_temp=10.0, a=-0.4, b=0.02,
        hvac_mode=VThermHvacMode_COOL,
        segments=(PredictionSegment(10.0, 0.6), PredictionSegment(5.0, 0.2)),
    )
    assert heat is not None and cool is not None
    assert cool.terminal_temp == pytest.approx(20.0 - heat.terminal_temp)


def test_zero_and_small_horizons_are_continuous() -> None:
    result = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT,
        segments=(PredictionSegment(0.0, 1.0), PredictionSegment(1e-12, 0.0)),
    )
    assert result is not None
    assert result.temperatures[0] == 20.0
    assert result.terminal_temp == pytest.approx(20.0, abs=1e-9)


@pytest.mark.parametrize(
    "segment",
    [PredictionSegment(-1.0, 0.5), PredictionSegment(1.0, -0.1), PredictionSegment(1.0, 1.1)],
)
def test_prediction_rejects_invalid_segments(segment: PredictionSegment) -> None:
    assert predict_segmented_1r1c(
        current_temp=20.0, ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=(segment,),
    ) is None
    assert predict_segmented_1r1c(
        current_temp=float("nan"), ext_temp=10.0, a=0.4, b=0.02,
        hvac_mode=VThermHvacMode_HEAT, segments=(PredictionSegment(1.0, 0.5),),
    ) is None


def test_dynamic_reserve_is_zero_bounded_and_signed() -> None:
    flat = compute_dynamic_reserve_candidates(
        hvac_mode=VThermHvacMode_HEAT, measured_slope_h=0.0,
        predicted_temperature_change_c=0.0, stop_deadtime_s=300.0,
        rho_margin=1.0, margin_max_c=0.2,
    )
    fast_cool = compute_dynamic_reserve_candidates(
        hvac_mode=VThermHvacMode_COOL, measured_slope_h=-100.0,
        predicted_temperature_change_c=-1.0, stop_deadtime_s=3600.0,
        rho_margin=1.0, margin_max_c=0.2,
    )
    assert flat is not None and flat.reserve_c == 0.0
    assert fast_cool is not None
    assert fast_cool.slope_reserve_c == 0.2
    assert fast_cool.model_reserve_c == 0.2
    assert fast_cool.reserve_c == 0.2
    assert compute_dynamic_reserve_candidates(
        hvac_mode=VThermHvacMode_HEAT, measured_slope_h=float("inf"),
        predicted_temperature_change_c=0.0, stop_deadtime_s=10.0,
        rho_margin=1.0, margin_max_c=0.2,
    ) is None


def test_dynamic_reserve_selector_exposes_each_candidate() -> None:
    candidates = compute_dynamic_reserve_candidates(
        hvac_mode=VThermHvacMode_HEAT,
        measured_slope_h=0.72,
        predicted_temperature_change_c=0.18,
        stop_deadtime_s=600.0,
        rho_margin=1.0,
        margin_max_c=0.5,
    )
    assert candidates is not None
    assert candidates.slope_reserve_c == pytest.approx(0.12)
    assert candidates.model_reserve_c == pytest.approx(0.18)
    assert select_dynamic_reserve(candidates, "slope") == pytest.approx(0.12)
    assert select_dynamic_reserve(candidates, "model") == pytest.approx(0.18)
    assert select_dynamic_reserve(candidates, "max") == pytest.approx(0.18)
    assert select_dynamic_reserve(candidates, "unsupported") is None


@pytest.mark.parametrize(
    ("mode", "gain", "bound", "expected", "coast_bound", "free_bound"),
    [
        (VThermHvacMode_HEAT, 0.2, 20.1, 0.5, 19.0, 21.0),
        (VThermHvacMode_COOL, -0.2, 19.9, 0.5, 21.0, 19.0),
    ],
)
def test_command_cap_is_signed_and_bounded(
    mode, gain, bound, expected, coast_bound, free_bound
) -> None:
    result = resolve_command_cap(
        current_temp=20.0, target_bound=bound, passive_temp=20.0,
        gain_u=gain, hvac_mode=mode,
        requested_u=0.75, command_comparison_epsilon=1e-9,
    )
    assert result is not None
    assert result.raw_cap == pytest.approx(expected)
    assert result.command_cap == pytest.approx(expected)
    assert result.coast_required is False
    assert result.constraint_active is True
    coast = resolve_command_cap(
        current_temp=20.0, target_bound=coast_bound, passive_temp=20.0,
        gain_u=gain, hvac_mode=mode,
        requested_u=1.0, command_comparison_epsilon=1e-9,
    )
    free = resolve_command_cap(
        current_temp=20.0, target_bound=free_bound, passive_temp=20.0,
        gain_u=gain, hvac_mode=mode,
        requested_u=1.0, command_comparison_epsilon=1e-9,
    )
    assert coast is not None and coast.command_cap == 0.0 and coast.coast_required is True
    assert free is not None and free.command_cap == 1.0 and free.constraint_active is False


def test_command_comparison_epsilon_prevents_decimal_boundary_chatter() -> None:
    boundary = resolve_command_cap(
        current_temp=20.0, target_bound=20.1, passive_temp=20.0,
        gain_u=0.2, hvac_mode=VThermHvacMode_HEAT,
        requested_u=0.500000001, command_comparison_epsilon=1e-9,
    )
    constrained = resolve_command_cap(
        current_temp=20.0, target_bound=20.1, passive_temp=20.0,
        gain_u=0.2, hvac_mode=VThermHvacMode_HEAT,
        requested_u=0.6, command_comparison_epsilon=1e-9,
    )
    assert boundary is not None and boundary.command_cap == pytest.approx(0.5)
    assert boundary.constraint_active is False
    assert constrained is not None and constrained.constraint_active is True


def test_command_cap_rejects_zero_or_wrong_signed_gain() -> None:
    assert resolve_command_cap(
        current_temp=20.0, target_bound=20.1, passive_temp=20.0,
        gain_u=0.0, hvac_mode=VThermHvacMode_HEAT,
        requested_u=0.5, command_comparison_epsilon=1e-9,
    ) is None
    assert resolve_command_cap(
        current_temp=20.0, target_bound=20.1, passive_temp=20.0,
        gain_u=0.2, hvac_mode=VThermHvacMode_COOL,
        requested_u=0.5, command_comparison_epsilon=1e-9,
    ) is None


def test_p_reference_inversion_matches_deadzone_and_cool_symmetry() -> None:
    heat = invert_p_reference_from_cap(
        command_cap=0.4, current_temp=21.0, hvac_mode=VThermHvacMode_HEAT,
        pi_snapshot=pi_snapshot(2.0, 0.1, 0.1, 0.05),
    )
    cool = invert_p_reference_from_cap(
        command_cap=0.4, current_temp=21.0, hvac_mode=VThermHvacMode_COOL,
        pi_snapshot=pi_snapshot(2.0, 0.1, 0.1, 0.05),
    )
    assert heat is not None and cool is not None
    assert heat.proportional_error == pytest.approx(0.1)
    assert heat.raw_error == pytest.approx(0.15)
    assert heat.reference == pytest.approx(21.15)
    assert cool.reference == pytest.approx(20.85)

    quiet_zone = invert_p_reference_from_cap(
        command_cap=0.225, current_temp=21.0, hvac_mode=VThermHvacMode_HEAT,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.2, 0.075),
    )
    assert quiet_zone is not None
    assert quiet_zone.raw_error == pytest.approx(0.1)


def test_p_reference_inversion_handles_zero_and_decimal_boundaries() -> None:
    at_zero = invert_p_reference_from_cap(
        command_cap=0.2, current_temp=21.9, hvac_mode=VThermHvacMode_HEAT,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.2, 0.1),
    )
    at_edge = invert_p_reference_from_cap(
        command_cap=0.325, current_temp=21.9, hvac_mode=VThermHvacMode_HEAT,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.2, 0.1),
    )
    assert at_zero is not None and at_edge is not None
    assert at_zero.reference == 21.9
    assert at_edge.raw_error == pytest.approx(0.225)
    assert at_edge.reference == pytest.approx(22.125)
    assert invert_p_reference_from_cap(
        command_cap=float("nan"), current_temp=21.9, hvac_mode=VThermHvacMode_HEAT,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.1),
    ) is None


def test_aggregate_kernel_returns_cap_and_conservative_reference() -> None:
    decision = evaluate_reference_governor(
        ReferenceGovernorInput(
            target_temp=21.0, nominal_reference=21.0, current_temp=20.0,
            ext_temp=10.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
            committed_u=1.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
            cycle_min=10.0, stop_deadtime_s=300.0, measured_slope_h=0.0,
            pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
        ),
        ReferenceGovernorPolicy(
            reserve_selector="max", rho_margin=1.0, margin_max_c=0.2,
            prediction_horizon_min=5.0, command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    assert decision.command_cap is not None
    assert decision.admissible_reference <= decision.nominal_reference
    assert decision.target_bound is not None


@pytest.mark.parametrize("mode,a,external,bias", [
    (VThermHvacMode_HEAT, 0.4, 10.0, 0.02),
    (VThermHvacMode_COOL, -0.4, 30.0, -0.02),
])
def test_aggregate_bias_changes_cap_in_physical_direction(
    mode, a, external, bias
) -> None:
    direction = 1.0 if mode == VThermHvacMode_HEAT else -1.0
    value = ReferenceGovernorInput(
        target_temp=20.0 + direction * 0.5,
        nominal_reference=20.0 + direction * 0.4,
        current_temp=20.0, ext_temp=external, hvac_mode=mode,
        a=a, b=0.02, committed_u=0.0, next_cycle_u=0.9,
        remaining_cycle_min=0.0, cycle_min=10.0,
        stop_deadtime_s=300.0, measured_slope_h=0.0,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )
    policy = ReferenceGovernorPolicy("max", 1.0, 0.2, 5.0, 1e-9, 1e-9)
    baseline = evaluate_reference_governor(value, policy)
    explicit_zero = evaluate_reference_governor(
        replace(value, thermal_bias_c_per_min=0.0), policy
    )
    biased = evaluate_reference_governor(
        replace(value, thermal_bias_c_per_min=bias), policy
    )
    assert baseline == explicit_zero
    assert baseline.command_cap is not None and biased.command_cap is not None
    assert biased.command_cap < baseline.command_cap
    assert biased.predicted_terminal_temp is not None
    assert baseline.predicted_terminal_temp is not None
    assert biased.reserve_candidates is not None
    assert baseline.reserve_candidates is not None
    assert biased.reserve_candidates.model_reserve_c >= (
        baseline.reserve_candidates.model_reserve_c
    )
    invalid = evaluate_reference_governor(
        replace(value, thermal_bias_c_per_min=float("nan")), policy
    )
    assert invalid.reason == "invalid_numeric_input"
    assert invalid.command_cap is None


def test_explicit_prediction_horizon_can_exceed_cycle_and_changes_reserve() -> None:
    base = ReferenceGovernorInput(
        target_temp=30.0, nominal_reference=30.0, current_temp=20.0,
        ext_temp=20.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
        committed_u=0.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
        cycle_min=2.0, stop_deadtime_s=600.0, measured_slope_h=None,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )

    short = evaluate_reference_governor(
        base,
        ReferenceGovernorPolicy(
            reserve_selector="max", rho_margin=1.0, margin_max_c=10.0,
            prediction_horizon_min=base.cycle_min,
            command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    long_horizon = evaluate_reference_governor(
        base,
        ReferenceGovernorPolicy(
            reserve_selector="max", rho_margin=1.0, margin_max_c=10.0,
            prediction_horizon_min=10.0,
            command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )

    assert short.reserve_candidates is not None
    assert long_horizon.reserve_candidates is not None
    expected_short_change = (base.a / base.b) * (1.0 - exp(-base.b * base.cycle_min))
    expected_long_change = (base.a / base.b) * (1.0 - exp(-base.b * 10.0))
    assert short.reserve_candidates.model_reserve_c == pytest.approx(expected_short_change)
    assert long_horizon.reserve_candidates.model_reserve_c == pytest.approx(expected_long_change)
    assert long_horizon.predicted_terminal_temp > short.predicted_terminal_temp
    assert long_horizon.dynamic_reserve_c > short.dynamic_reserve_c


@pytest.mark.parametrize(
    ("selector", "expected_reserve"),
    [
        ("slope", pytest.approx(0.06)),
        ("model", pytest.approx((0.4 / 0.02) * (1.0 - exp(-0.02 * 1.0)) * 1.0)),
        ("max", pytest.approx((0.4 / 0.02) * (1.0 - exp(-0.02 * 1.0)) * 1.0)),
    ],
)
def test_aggregate_uses_selected_reserve_variant(selector, expected_reserve) -> None:
    decision = evaluate_reference_governor(
        ReferenceGovernorInput(
            target_temp=21.0, nominal_reference=21.0, current_temp=20.0,
            ext_temp=20.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
            committed_u=0.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
            cycle_min=1.0, stop_deadtime_s=600.0, measured_slope_h=0.36,
            pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
        ),
        ReferenceGovernorPolicy(
            reserve_selector=selector, rho_margin=1.0, margin_max_c=0.5,
            prediction_horizon_min=1.0, command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    assert decision.reserve_candidates is not None
    assert decision.dynamic_reserve_c == expected_reserve
    assert decision.reserve_candidates.slope_reserve_c == pytest.approx(0.06)
    assert decision.reserve_candidates.model_reserve_c == pytest.approx(
        (0.4 / 0.02) * (1.0 - exp(-0.02 * 1.0)) * 1.0
    )


def test_reference_comparison_epsilon_prevents_constraint_chatter() -> None:
    value = ReferenceGovernorInput(
        target_temp=21.0, nominal_reference=21.0, current_temp=20.0,
        ext_temp=20.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
        committed_u=0.0, next_cycle_u=0.1, remaining_cycle_min=0.0,
        cycle_min=1.0, stop_deadtime_s=600.0, measured_slope_h=4.8,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )
    common = dict(
        reserve_selector="slope",
        rho_margin=1.0,
        margin_max_c=0.8,
        prediction_horizon_min=1.0,
        command_comparison_epsilon=1e-9,
    )

    constrained = evaluate_reference_governor(
        value,
        ReferenceGovernorPolicy(
            **common,
            reference_comparison_epsilon_c=0.0,
        ),
    )
    tolerant = evaluate_reference_governor(
        value,
        ReferenceGovernorPolicy(
            **common,
            reference_comparison_epsilon_c=0.5,
        ),
    )

    assert constrained.active is True
    assert tolerant.active is False


def test_aggregate_rejects_unsupported_reserve_selector() -> None:
    value = ReferenceGovernorInput(
        target_temp=21.0, nominal_reference=21.0, current_temp=20.0,
        ext_temp=20.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
        committed_u=0.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
        cycle_min=1.0, stop_deadtime_s=600.0, measured_slope_h=0.0,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )
    decision = evaluate_reference_governor(
        value,
        ReferenceGovernorPolicy(
            reserve_selector="unsupported", rho_margin=1.0, margin_max_c=0.5,
            prediction_horizon_min=1.0, command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    assert decision.reason == "unsupported_reserve_selector"
    assert decision.reserve_candidates is None


def test_aggregate_bypasses_nonpositive_signed_demand() -> None:
    value = ReferenceGovernorInput(
        target_temp=20.0, nominal_reference=20.25, current_temp=21.0,
        ext_temp=20.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
        committed_u=1.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
        cycle_min=10.0, stop_deadtime_s=600.0, measured_slope_h=1.0,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )
    decision = evaluate_reference_governor(
        value,
        ReferenceGovernorPolicy(
            reserve_selector="max", rho_margin=1.0, margin_max_c=0.5,
            prediction_horizon_min=10.0, command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    assert decision.active is False
    assert decision.reason == "no_positive_demand"
    assert decision.command_cap is None
    assert decision.admissible_reference == value.nominal_reference
    assert decision.reserve_candidates is None


def test_aggregate_kernel_fails_closed_on_nonfinite_model_input() -> None:
    base = ReferenceGovernorInput(
        target_temp=21.0, nominal_reference=21.0, current_temp=20.0,
        ext_temp=10.0, hvac_mode=VThermHvacMode_HEAT, a=float("nan"), b=0.02,
        committed_u=1.0, next_cycle_u=1.0, remaining_cycle_min=0.0,
        cycle_min=10.0, stop_deadtime_s=300.0, measured_slope_h=None,
        pi_snapshot=pi_snapshot(1.0, 0.0, 0.0, 0.05),
    )
    decision = evaluate_reference_governor(
        base,
        ReferenceGovernorPolicy(
            reserve_selector="max", rho_margin=1.0, margin_max_c=0.2,
            prediction_horizon_min=5.0, command_comparison_epsilon=1e-9,
            reference_comparison_epsilon_c=1e-9,
        ),
    )
    assert decision.reason == "invalid_model"
    assert decision.command_cap is None
    assert decision.admissible_reference == base.current_temp


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        PIOutputSnapshot(1.0, 0.0, 0.0, None),
        PIOutputSnapshot(float("nan"), 0.0, 0.0, ProportionalState(0.05, False, False, 0, None)),
        PIOutputSnapshot(1.0, float("inf"), 0.0, ProportionalState(0.05, False, False, 0, None)),
        PIOutputSnapshot(10**1000, 0.0, 0.0, ProportionalState(0.05, False, False, 0, None)),
        PIOutputSnapshot(1.0, 10**1000, 0.0, ProportionalState(0.05, False, False, 0, None)),
        PIOutputSnapshot(1.0, 0.0, 0.0, ProportionalState(float("nan"), False, False, 0, None)),
        PIOutputSnapshot(1.0, 0.0, 0.0, ProportionalState(0.05, False, False, -1, None)),
    ],
)
def test_kernel_rejects_missing_or_invalid_pi_snapshot(invalid: object) -> None:
    base = ReferenceGovernorInput(
        target_temp=20.5, nominal_reference=20.4, current_temp=20.0,
        ext_temp=10.0, hvac_mode=VThermHvacMode_HEAT, a=0.4, b=0.02,
        committed_u=0.0, next_cycle_u=0.9, remaining_cycle_min=0.0,
        cycle_min=10.0, stop_deadtime_s=300.0, measured_slope_h=0.0,
        pi_snapshot=invalid,
    )
    decision = evaluate_reference_governor(
        base,
        ReferenceGovernorPolicy("max", 1.0, 0.2, 5.0, 1e-9, 1e-9),
    )
    assert decision.reason == "invalid_pi_snapshot"
    assert decision.command_cap is None


@pytest.mark.parametrize("mode,a,external", [
    (VThermHvacMode_HEAT, 0.4, 10.0),
    (VThermHvacMode_COOL, -0.4, 30.0),
])
def test_frozen_or_pending_p_cannot_invent_reference_constraint(mode, a, external) -> None:
    direction = 1.0 if mode == VThermHvacMode_HEAT else -1.0
    base = ReferenceGovernorInput(
        target_temp=20.0 + direction * 0.5,
        nominal_reference=20.0 + direction * 0.4,
        current_temp=20.0, ext_temp=external, hvac_mode=mode,
        a=a, b=0.02, committed_u=0.0, next_cycle_u=0.9,
        remaining_cycle_min=0.0, cycle_min=10.0,
        stop_deadtime_s=300.0, measured_slope_h=0.0,
        pi_snapshot=PIOutputSnapshot(1.0, 0.0, 0.0, ProportionalState(0.1, True, False, 0, None)),
    )
    policy = ReferenceGovernorPolicy("max", 1.0, 0.2, 5.0, 1e-9, 1e-9)
    for state in (
        base.pi_snapshot.p_state,
        ProportionalState(0.1, True, True, 0, None),
    ):
        decision = evaluate_reference_governor(
            replace(base, pi_snapshot=replace(base.pi_snapshot, p_state=state)), policy
        )
        assert decision.command_cap is not None
        assert decision.admissible_reference == pytest.approx(base.nominal_reference)
