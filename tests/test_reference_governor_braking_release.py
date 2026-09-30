"""Contracts for non-authoritative terminal braking-release evaluation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from math import nan

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import DEADBAND_HYSTERESIS, TrajectoryPhase
from custom_components.vtherm_smartpi.smartpi.controller import PIOutputSnapshot
from custom_components.vtherm_smartpi.smartpi.deadband_output import ProportionalState
from custom_components.vtherm_smartpi.smartpi.reference_governor import (
    PredictionSegment, predict_segmented_1r1c,
)
from custom_components.vtherm_smartpi.smartpi.reference_governor_authority import (
    ReferenceGovernorAuthorityContext,
)
from custom_components.vtherm_smartpi.smartpi.braking_release import evaluate_braking_release


def _inputs(mode=VThermHvacMode_HEAT):
    """Use rounded late-RELEASE observations and a declared immediate P branch."""
    direction = -1.0 if mode == VThermHvacMode_COOL else 1.0
    return dict(
        context=ReferenceGovernorAuthorityContext(
            target_temp=19.5, nominal_reference=19.5,
            current_temp=19.5 - direction * 0.07,
            ext_temp=19.5 - direction * 14.5, hvac_mode=mode,
            a=direction * 0.180385, b=0.003060, committed_u=0.2875,
            remaining_cycle_min=0.985, stop_deadtime_s=494.2342618,
            measured_slope_h=-direction * 0.45,
        ),
        pi_snapshot=PIOutputSnapshot(
            0.280137, 0.052887531, 0.245985,
            ProportionalState(0.05, False, True, 0, None),
        ),
        phase=TrajectoryPhase.RELEASE, profile_nominal=19.5,
        deadband_c=0.05, cycle_min=4.0,
        previous_requested_power=0.287468, previous_projected_power=0.2875,
        guard_active=False, context_is_current=True,
        post_pi_branch_complete=True, zero_delay_switch=True,
    )


def _forecast(context, power):
    return predict_segmented_1r1c(
        current_temp=context.current_temp, ext_temp=context.ext_temp,
        a=context.a, b=context.b, hvac_mode=context.hvac_mode,
        segments=(PredictionSegment(context.remaining_cycle_min, context.committed_u),
                  PredictionSegment(context.stop_deadtime_s / 60.0, power)),
    )


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_late_release_proposes_only_conditional_model_admission(mode):
    inputs = _inputs(mode)
    before = deepcopy(inputs)
    result = evaluate_braking_release(**inputs)
    direction = -1.0 if mode == VThermHvacMode_COOL else 1.0

    assert result.release_candidate
    assert result.model_admissible
    assert not result.authoritative
    assert result.reason == "release_candidate"
    assert result.raw_requested_power == pytest.approx(0.311478696)
    assert result.raw_projected_power == pytest.approx(74.0 / 240.0)
    assert result.forecast_power == result.raw_requested_power
    assert result.prediction == _forecast(inputs["context"], result.forecast_power)
    assert result.signed_peak_above_target_c == pytest.approx(0.035247, abs=1e-5)
    assert result.dynamic_reserve_c == 0.0
    assert result.target_band_bound_c == pytest.approx(19.5 + direction * 0.05)
    assert len(result.command_deltas) == 6
    assert max(result.command_deltas) < 0.03
    assert inputs == before
    with pytest.raises(FrozenInstanceError):
        result.release_candidate = False


def test_heat_cool_mirror_preserves_peak_reserve_and_commands():
    heat = evaluate_braking_release(**_inputs(VThermHvacMode_HEAT))
    cool = evaluate_braking_release(**_inputs(VThermHvacMode_COOL))
    assert heat.release_candidate == cool.release_candidate
    assert heat.raw_requested_power == pytest.approx(cool.raw_requested_power, abs=1e-12)
    assert heat.raw_projected_power == cool.raw_projected_power
    assert heat.signed_peak_above_target_c == pytest.approx(cool.signed_peak_above_target_c, abs=1e-12)
    assert heat.dynamic_reserve_c == cool.dynamic_reserve_c
    assert heat.prediction.terminal_temp + cool.prediction.terminal_temp == pytest.approx(39.0)


def test_forecast_does_not_gain_admission_from_downward_switch_quantization():
    inputs = _inputs()
    result = evaluate_braking_release(**inputs)
    quantized_only = _forecast(inputs["context"], result.raw_projected_power)
    assert result.raw_projected_power < result.forecast_power
    assert result.prediction.terminal_temp > quantized_only.terminal_temp


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
@pytest.mark.parametrize("gap_kind", ["far", "inside", "outside", "target", "crossed"])
def test_terminal_scope_rejects_far_or_crossed_targets_despite_low_forecast(mode, gap_kind):
    inputs = _inputs(mode)
    direction = -1.0 if mode == VThermHvacMode_COOL else 1.0
    boundary = inputs["deadband_c"] + DEADBAND_HYSTERESIS
    gap = {"far": 0.2, "inside": boundary - 1e-6,
           "outside": boundary + 1e-6, "target": 0.0, "crossed": -0.01}[gap_kind]
    inputs["context"] = replace(inputs["context"],
                                current_temp=19.5 - direction * gap, committed_u=0.1)
    inputs["pi_snapshot"] = replace(inputs["pi_snapshot"], kp=0.0001, u_i=0.0, u_ff=0.1)
    inputs["previous_requested_power"] = 0.1
    inputs["previous_projected_power"] = 0.1
    snapshot = inputs["pi_snapshot"]
    raw_power = snapshot.u_i + snapshot.u_ff + snapshot.project_p(gap)
    forecast = _forecast(inputs["context"], raw_power)
    assert max(direction * (temperature - 19.5)
               for temperature in (inputs["context"].current_temp, *forecast.temperatures)) < 0.05
    assert abs(raw_power - 0.1) < 0.03

    result = evaluate_braking_release(**inputs)
    if gap_kind == "inside":
        assert result.release_candidate
    else:
        assert not result.release_candidate
        assert result.reason == "outside_terminal_approach_band"


@pytest.mark.parametrize(("feedforward", "expected"), [(1.5, 1.0), (-1.5, 0.0)])
def test_raw_pi_counterfactual_uses_the_existing_unit_command_bounds(feedforward, expected):
    inputs = _inputs()
    inputs["pi_snapshot"] = replace(inputs["pi_snapshot"], u_ff=feedforward)
    result = evaluate_braking_release(**inputs)
    assert result.raw_requested_power == expected
    assert result.raw_projected_power == expected
    assert result.forecast_power == expected
    assert not result.release_candidate


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_actual_raw_forecast_rejects_even_when_the_previous_capped_forecast_passes(mode):
    inputs = _inputs(mode)
    inputs["pi_snapshot"] = replace(inputs["pi_snapshot"], u_ff=0.35)
    result = evaluate_braking_release(**inputs)
    context = inputs["context"]
    direction = -1.0 if mode == VThermHvacMode_COOL else 1.0
    capped = _forecast(context, context.committed_u)
    assert max(direction * (temperature - context.target_temp)
               for temperature in capped.temperatures) <= inputs["deadband_c"]
    assert not result.release_candidate
    assert not result.model_admissible
    assert result.reason == "raw_forecast_outside_target_band"
    assert result.prediction == _forecast(context, result.raw_requested_power)


def test_peak_includes_the_committed_segment_not_just_terminal_temperature():
    inputs = _inputs()
    inputs["context"] = replace(inputs["context"], current_temp=19.49, committed_u=1.0)
    inputs["pi_snapshot"] = replace(inputs["pi_snapshot"], u_i=0.04, u_ff=0.0)
    result = evaluate_braking_release(**inputs)
    assert result.prediction.terminal_temp < 19.5
    assert result.prediction.temperatures[0] > 19.55
    assert result.signed_peak_above_target_c == pytest.approx(
        result.prediction.temperatures[0] - 19.5)
    assert not result.model_admissible


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_existing_signed_slope_reserve_can_reject_a_band_admissible_raw_prediction(mode):
    inputs = _inputs(mode)
    direction = -1.0 if mode == VThermHvacMode_COOL else 1.0
    inputs["context"] = replace(inputs["context"], measured_slope_h=direction * 0.45)
    result = evaluate_braking_release(**inputs)
    assert result.signed_peak_above_target_c < 0.05
    assert result.dynamic_reserve_c == pytest.approx(0.05)
    assert not result.model_admissible


@pytest.mark.parametrize("boundary", ["previous_requested_power", "previous_projected_power", "committed"])
def test_both_raw_commands_must_be_continuous_against_every_existing_boundary(boundary):
    inputs = _inputs()
    if boundary == "committed":
        inputs["context"] = replace(inputs["context"], committed_u=0.25)
    else:
        inputs[boundary] = 0.25
    result = evaluate_braking_release(**inputs)
    assert result.model_admissible
    assert not result.release_candidate
    assert result.reason == "command_jump"
    assert max(result.command_deltas) > 0.03


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("guard_active", True, "guard_active"),
    ("guard_active", 1, "invalid_boolean_evidence"),
    ("context_is_current", False, "stale_or_bypassed_context"),
    ("context_is_current", None, "invalid_boolean_evidence"),
    ("post_pi_branch_complete", False, "unsupported_post_pi_or_actuator_branch"),
    ("zero_delay_switch", False, "unsupported_post_pi_or_actuator_branch"),
    ("phase", TrajectoryPhase.TRACKING, "not_release_phase"),
    ("phase", "release", "not_release_phase"),
    ("profile_nominal", 19.4, "profile_context_mismatch"),
    ("cycle_min", 0.0, "invalid_command_or_horizon"),
    ("cycle_min", 1e308, "invalid_command_or_horizon"),
    ("previous_requested_power", nan, "invalid_numeric_evidence"),
    ("deadband_c", nan, "invalid_numeric_evidence"),
    ("deadband_c", 0.1, "snapshot_deadband_mismatch"),
])
def test_incomplete_invalid_or_unsupported_inputs_fail_closed(field, value, reason):
    inputs = _inputs()
    inputs[field] = value
    result = evaluate_braking_release(**inputs)
    assert not result.release_candidate
    assert not result.authoritative
    assert result.reason == reason


@pytest.mark.parametrize("change", [
    {"bypass_reason": "authority_bypass_model_unreliable"},
    {"a": nan}, {"b": 0.0}, {"remaining_cycle_min": 5.0},
    {"measured_slope_h": nan}, {"committed_u": True},
])
def test_invalid_frozen_context_fails_closed(change):
    inputs = _inputs()
    inputs["context"] = replace(inputs["context"], **change)
    assert not evaluate_braking_release(**inputs).release_candidate


def test_nonconverged_profile_and_incomplete_or_frozen_snapshots_are_rejected():
    inputs = _inputs()
    inputs["profile_nominal"] = 19.4
    inputs["context"] = replace(inputs["context"], nominal_reference=19.4)
    assert evaluate_braking_release(**inputs).reason == "profile_not_converged"
    inputs = _inputs()
    for snapshot in (None, replace(inputs["pi_snapshot"], u_i=nan)):
        assert evaluate_braking_release(**dict(inputs, pi_snapshot=snapshot)).reason == "invalid_pi_snapshot"
    frozen = replace(inputs["pi_snapshot"], p_state=replace(inputs["pi_snapshot"].p_state, freeze_deadband=True))
    assert evaluate_braking_release(**dict(inputs, pi_snapshot=frozen)).reason == "frozen_proportional_branch"
