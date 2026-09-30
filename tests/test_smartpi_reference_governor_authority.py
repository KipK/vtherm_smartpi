"""Focused tests for the authoritative reference-governor coordinator."""

from dataclasses import FrozenInstanceError

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.controller import PIOutputSnapshot
from custom_components.vtherm_smartpi.smartpi.deadband_output import ProportionalState
from custom_components.vtherm_smartpi.smartpi.reference_governor_authority import (
    ReferenceGovernorAuthority,
    ReferenceGovernorAuthorityContext,
    ReferenceGovernorAuthorityDecision,
)
from custom_components.vtherm_smartpi.smartpi.reference_governor_runtime import (
    ReferenceGovernorPhase,
)


def prepare_values(**overrides: object) -> dict[str, object]:
    """Return valid HEAT setpoint observations for one authority cycle."""
    values: dict[str, object] = {
        "target_temp": 20.5,
        "nominal_reference": 20.4,
        "current_temp": 20.0,
        "hvac_mode": VThermHvacMode_HEAT,
        "trajectory_active": True,
        "trajectory_source": "setpoint",
        "a": 0.4,
        "b": 0.02,
        "ext_temp": 10.0,
        "committed_u": 0.0,
        "remaining_cycle_min": 0.0,
        "stop_deadtime_s": 300.0,
        "measured_slope_h": 0.0,
        "model_reliable": True,
        "deadtime_reliable": True,
        "now_monotonic": 0.0,
    }
    values.update(overrides)
    return values


def prepare(authority: ReferenceGovernorAuthority, **overrides: object) -> ReferenceGovernorAuthorityContext:
    """Prepare valid observations with concise test call sites."""
    return authority.prepare(**prepare_values(**overrides))


def snapshot() -> PIOutputSnapshot:
    return PIOutputSnapshot(
        kp=1.0, u_i=0.0, u_ff=0.0,
        p_state=ProportionalState(0.05, False, False, 0, None),
    )


def resolve(authority: ReferenceGovernorAuthority, requested: object, now: object, cycle: object):
    return authority.resolve(requested, now, cycle, snapshot())


def test_prepare_records_independent_nominal_reference_and_is_immutable() -> None:
    authority = ReferenceGovernorAuthority()

    context = prepare(authority)

    assert isinstance(context, ReferenceGovernorAuthorityContext)
    assert context.target_temp == 20.5
    assert context.nominal_reference == 20.4
    assert context.nominal_reference != context.target_temp
    with pytest.raises(FrozenInstanceError):
        context.nominal_reference = 20.5


def test_resolve_uses_post_compute_snapshot_after_prepare() -> None:
    authority = ReferenceGovernorAuthority()
    context = prepare(authority)
    effective = PIOutputSnapshot(
        2.0, 0.1, 0.2, ProportionalState(0.10, False, True, 0, None)
    )

    result = authority.resolve(0.9, 1.0, 10.0, effective)

    assert context.bypass_reason is None
    assert result.command_cap is not None
    expected_error = max((result.command_cap - 0.3) / 2.0 + 0.075, 0.0)
    assert result.kernel_decision.admissible_reference == pytest.approx(
        min(context.nominal_reference, context.current_temp + expected_error)
    )


def test_resolve_passes_explicit_bias_and_keeps_zero_default() -> None:
    baseline_authority = ReferenceGovernorAuthority()
    prepare(baseline_authority)
    baseline = baseline_authority.resolve(0.9, 1.0, 10.0, snapshot())

    zero_authority = ReferenceGovernorAuthority()
    prepare(zero_authority)
    explicit_zero = zero_authority.resolve(
        0.9, 1.0, 10.0, snapshot(), thermal_bias_c_per_min=0.0
    )

    biased_authority = ReferenceGovernorAuthority()
    prepare(biased_authority)
    biased = biased_authority.resolve(
        0.9, 1.0, 10.0, snapshot(), thermal_bias_c_per_min=0.02
    )

    assert baseline == explicit_zero
    assert baseline.kernel_decision.command_cap is not None
    assert biased.kernel_decision.command_cap is not None
    assert biased.kernel_decision.command_cap < baseline.kernel_decision.command_cap

    invalid_authority = ReferenceGovernorAuthority()
    prepare(invalid_authority)
    invalid = invalid_authority.resolve(
        0.9, 1.0, 10.0, snapshot(), thermal_bias_c_per_min=float("inf")
    )
    assert invalid.kernel_decision.reason == "invalid_numeric_input"
    assert invalid.command_cap is None


def test_missing_or_nonfinite_snapshot_bypasses_without_reusing_old_pi() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)
    missing = authority.resolve(0.9, 1.0, 10.0, None)
    assert missing.kernel_decision.reason == "authority_missing_pi_snapshot"
    assert missing.command_cap is None

    prepare(authority, now_monotonic=2.0)
    invalid = authority.resolve(
        0.9, 3.0, 10.0,
        PIOutputSnapshot(1.0, float("nan"), 0.0, ProportionalState(0.05, False, False, 0, None)),
    )
    assert invalid.kernel_decision.reason == "invalid_pi_snapshot"
    assert invalid.command_cap is None


def test_true_pre_cap_requested_command_changes_kernel_constraint() -> None:
    authority = ReferenceGovernorAuthority()

    prepare(authority)
    unconstrained = resolve(authority, 0.45, 1.0, 10.0)
    prepare(authority, now_monotonic=2.0)
    constrained = resolve(authority, 0.90, 3.0, 10.0)

    assert unconstrained.kernel_decision.constraint_active is False
    assert constrained.kernel_decision.constraint_active is True
    assert unconstrained.command_cap is None
    assert constrained.command_cap is not None
    assert constrained.phase is ReferenceGovernorPhase.GOVERNED


def test_resolve_is_one_shot_and_cannot_reuse_a_cap_context() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)

    first = resolve(authority, 0.90, 1.0, 10.0)
    second = resolve(authority, 0.90, 2.0, 10.0)

    assert first.command_cap is not None
    assert second is None
    assert authority.pending_context is None
    assert authority.runtime.armed is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"hvac_mode": None}, "authority_bypass_unsupported_mode"),
        ({"trajectory_source": "disturbance"}, "authority_bypass_disturbance"),
        ({"model_reliable": False}, "authority_bypass_model_unreliable"),
    ],
)
def test_unsupported_mode_disturbance_and_unreliable_model_bypass(
    overrides: dict[str, object], reason: str
) -> None:
    authority = ReferenceGovernorAuthority()
    context = prepare(authority, **overrides)

    result = resolve(authority, 0.90, 1.0, 10.0)

    assert context.bypass_reason == reason
    assert result.kernel_decision.reason == reason
    assert result.effective_reference == 20.4
    assert result.command_cap is None
    assert result.shaping_active is False
    assert result.phase is ReferenceGovernorPhase.IDLE
    assert authority.runtime.armed is False


def test_cool_context_uses_signed_model_and_enters_governed() -> None:
    authority = ReferenceGovernorAuthority()
    context = prepare(
        authority,
        target_temp=19.5,
        nominal_reference=19.6,
        current_temp=20.0,
        hvac_mode=VThermHvacMode_COOL,
        a=-0.4,
        ext_temp=30.0,
    )

    result = resolve(authority, 0.9, 1.0, 10.0)

    assert context.bypass_reason is None
    assert result is not None
    assert result.phase is ReferenceGovernorPhase.GOVERNED
    assert result.command_cap is not None
    assert result.command_cap < 0.9
    assert result.effective_reference >= context.target_temp


def test_reset_and_load_state_clear_pending_context_and_runtime() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)
    authority.reset()
    after_reset = resolve(authority, 0.90, 1.0, 10.0)
    assert after_reset is None
    assert authority.pending_context is None

    prepare(authority, now_monotonic=2.0)
    resolve(authority, 0.90, 3.0, 10.0)
    authority.load_state({"command_cap": 0.1})
    after_load = resolve(authority, 0.90, 4.0, 10.0)
    assert after_load is None
    assert authority.runtime.armed is False


def test_changed_target_rearms_and_replaces_nominal_context() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)
    first = resolve(authority, 0.90, 1.0, 10.0)

    next_context = prepare(
        authority,
        target_temp=21.5,
        nominal_reference=21.4,
        now_monotonic=2.0,
    )
    second = resolve(authority, 0.90, 3.0, 10.0)

    assert first.phase is ReferenceGovernorPhase.GOVERNED
    assert next_context.target_temp == 21.5
    assert next_context.nominal_reference == 21.4
    assert second.kernel_decision.nominal_reference == 21.4
    assert second.phase is ReferenceGovernorPhase.GOVERNED


def test_bypass_then_valid_same_target_rearms() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority, model_reliable=False)
    bypass = resolve(authority, 0.90, 1.0, 10.0)
    assert authority.runtime.armed is False

    prepare(authority, now_monotonic=2.0)
    recovered = resolve(authority, 0.90, 3.0, 10.0)

    assert bypass is not None
    assert bypass.command_cap is None
    assert authority.runtime.armed is True
    assert recovered.phase is ReferenceGovernorPhase.GOVERNED
    assert recovered.command_cap is not None


@pytest.mark.parametrize("cycle_min", [0.0, -1.0, float("nan"), float("inf"), None])
def test_invalid_cycle_is_rejected_before_kernel_evaluation(cycle_min: object) -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)

    result = resolve(authority, 0.90, 1.0, cycle_min)

    assert result is not None
    assert result.kernel_decision.reason == "authority_invalid_cycle"
    assert result.command_cap is None
    assert authority.runtime.armed is False


def test_handoff_completes_and_reconstraint_is_immediate() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)
    governed = resolve(authority, 0.90, 0.0, 10.0)

    prepare(authority, now_monotonic=1.0)
    first_handoff = resolve(authority, 0.45, 1.0, 10.0)
    prepare(authority, now_monotonic=301.0)
    second_handoff = resolve(authority, 0.45, 301.0, 10.0)
    prepare(authority, now_monotonic=302.0)
    reconstraint = resolve(authority, 0.90, 302.0, 10.0)

    assert governed.phase is ReferenceGovernorPhase.GOVERNED
    assert first_handoff.phase is ReferenceGovernorPhase.HANDOFF
    assert second_handoff.phase is ReferenceGovernorPhase.HANDOFF
    assert reconstraint.phase is ReferenceGovernorPhase.GOVERNED
    assert reconstraint.command_cap is not None

    prepare(authority, now_monotonic=303.0)
    resolve(authority, 0.45, 303.0, 10.0)
    prepare(authority, now_monotonic=603.0)
    resolve(authority, 0.45, 603.0, 10.0)
    prepare(authority, now_monotonic=903.0)
    complete = resolve(authority, 0.45, 903.0, 10.0)

    assert complete.phase is ReferenceGovernorPhase.IDLE
    assert complete.handoff_ready is True
    assert complete.command_cap is None
    assert authority.runtime.armed is False

    prepare(authority, now_monotonic=904.0)
    after_completion = resolve(authority, 0.90, 904.0, 10.0)
    assert after_completion.phase is ReferenceGovernorPhase.IDLE
    assert after_completion.command_cap is None


def test_authority_result_is_immutable_and_carries_both_evidence_layers() -> None:
    authority = ReferenceGovernorAuthority()
    prepare(authority)
    result = resolve(authority, 0.90, 1.0, 10.0)

    assert isinstance(result, ReferenceGovernorAuthorityDecision)
    assert result.kernel_evidence is result.kernel_decision
    assert result.runtime_evidence is result.runtime_decision
    with pytest.raises(FrozenInstanceError):
        result.command_cap = None
