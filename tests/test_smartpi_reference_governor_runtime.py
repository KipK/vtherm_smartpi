"""Focused tests for the authoritative SmartPI reference-governor runtime."""

from dataclasses import FrozenInstanceError

import pytest

from custom_components.vtherm_smartpi.smartpi.reference_governor import (
    ReferenceGovernorDecision,
)
from custom_components.vtherm_smartpi.smartpi.reference_governor_runtime import (
    ReferenceGovernorPhase,
    ReferenceGovernorRuntime,
    ReferenceGovernorRuntimeDecision,
)


def kernel(
    reason: str,
    *,
    nominal: float = 22.0,
    admissible: float | None = None,
    cap: float | None = 1.0,
    active: bool = False,
    constraint_active: bool = False,
    coast: bool = False,
    predicted: float | None = 21.0,
    target_bound: float | None = 21.9,
    reserve: float = 0.1,
) -> ReferenceGovernorDecision:
    """Build compact kernel evidence for state-machine tests."""
    return ReferenceGovernorDecision(
        active=active,
        reason=reason,
        nominal_reference=nominal,
        admissible_reference=nominal if admissible is None else admissible,
        command_cap=cap,
        predicted_terminal_temp=predicted,
        target_bound=target_bound,
        reserve_candidates=None,
        dynamic_reserve_c=reserve,
        constraint_active=constraint_active,
        coast_required=coast,
    )


def constrained(*, nominal: float = 22.0, admissible: float = 21.5) -> ReferenceGovernorDecision:
    """Return valid cap evidence."""
    return kernel(
        "cap",
        nominal=nominal,
        admissible=admissible,
        cap=0.4,
        active=True,
        constraint_active=True,
    )


def coast_constrained() -> ReferenceGovernorDecision:
    """Return valid coast evidence."""
    return kernel(
        "coast",
        admissible=21.0,
        cap=0.0,
        active=True,
        constraint_active=True,
        coast=True,
    )


def unconstrained(*, nominal: float = 22.0) -> ReferenceGovernorDecision:
    """Return valid positive-demand release evidence."""
    return kernel("unconstrained", nominal=nominal, cap=1.0, predicted=21.0, target_bound=21.9)


def no_positive_demand(*, nominal: float = 22.0) -> ReferenceGovernorDecision:
    """Return the only accepted no-demand bypass evidence."""
    return kernel(
        "no_positive_demand",
        nominal=nominal,
        cap=None,
        predicted=None,
        target_bound=None,
        reserve=0.0,
    )


def test_runtime_outputs_are_immutable_and_carry_kernel_evidence() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    evidence = constrained()
    output = runtime.consume(evidence, 1.0, 10.0)

    assert isinstance(output, ReferenceGovernorRuntimeDecision)
    assert hasattr(output, "__slots__")
    assert output.kernel_decision is evidence
    assert output.kernel_evidence is evidence
    with pytest.raises(FrozenInstanceError):
        output.phase = ReferenceGovernorPhase.IDLE


@pytest.mark.parametrize("decision", [constrained(), coast_constrained()])
def test_valid_constraining_evidence_enters_governed(decision: ReferenceGovernorDecision) -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)

    output = runtime.consume(decision, 1.0, 10.0)

    assert output.phase is ReferenceGovernorPhase.GOVERNED
    assert output.command_cap == decision.command_cap
    assert output.shaping_active is True


@pytest.mark.parametrize(
    ("reason", "active", "constraint_active", "coast"),
    [
        ("cap", False, True, False),
        ("cap", True, False, False),
        ("cap", True, True, True),
        ("coast", False, True, True),
        ("coast", True, False, True),
        ("coast", True, True, False),
    ],
)
def test_malformed_constraining_evidence_aborts_without_a_cap(
    reason: str,
    active: bool,
    constraint_active: bool,
    coast: bool,
) -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    malformed = kernel(
        reason,
        nominal=23.0,
        admissible=21.0,
        cap=0.2,
        active=active,
        constraint_active=constraint_active,
        coast=coast,
    )

    output = runtime.consume(malformed, 2.0, 10.0)

    assert output.phase is ReferenceGovernorPhase.IDLE
    assert output.command_cap is None
    assert output.effective_reference == 23.0
    assert runtime.armed is False


def test_idle_arms_without_shaping_then_governed_and_handoff_complete() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)

    idle = runtime.consume(unconstrained(), 60.0, 10.0)
    assert idle.phase is ReferenceGovernorPhase.IDLE
    assert runtime.armed is True
    assert idle.effective_reference == 22.0
    assert idle.command_cap is None
    assert idle.shaping_active is False

    governed = runtime.consume(constrained(), 120.0, 10.0)
    assert governed.phase is ReferenceGovernorPhase.GOVERNED
    assert governed.effective_reference == 21.5
    assert governed.command_cap == 0.4
    assert governed.shaping_active is True

    first = runtime.consume(unconstrained(), 180.0, 10.0)
    second = runtime.consume(unconstrained(), 480.0, 10.0)
    complete = runtime.consume(unconstrained(), 780.0, 10.0)
    assert first.phase is ReferenceGovernorPhase.HANDOFF
    assert second.phase is ReferenceGovernorPhase.HANDOFF
    assert second.handoff_ready is False
    assert complete.phase is ReferenceGovernorPhase.IDLE
    assert complete.handoff_ready is True
    assert complete.reason == "handoff_complete"
    assert complete.command_cap is None
    assert complete.effective_reference == 22.0
    assert runtime.armed is False


def test_duplicate_and_regressive_timestamps_are_not_handoff_evidence() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 0.0, 10.0)

    first = runtime.consume(unconstrained(), 60.0, 10.0)
    duplicate = runtime.consume(unconstrained(), 60.0, 10.0)
    regressive = runtime.consume(unconstrained(), 30.0, 10.0)
    second = runtime.consume(unconstrained(), 360.0, 10.0)
    not_yet = runtime.consume(unconstrained(), 659.0, 10.0)
    complete = runtime.consume(unconstrained(), 660.0, 10.0)

    assert first.phase is ReferenceGovernorPhase.HANDOFF
    assert duplicate.phase is ReferenceGovernorPhase.HANDOFF
    assert regressive.phase is ReferenceGovernorPhase.HANDOFF
    assert second.phase is ReferenceGovernorPhase.HANDOFF
    assert not_yet.phase is ReferenceGovernorPhase.HANDOFF
    assert complete.handoff_ready is True


def test_handoff_requires_three_evaluations_spanning_one_full_cycle() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 0.0, 10.0)

    assert runtime.consume(unconstrained(), 1.0, 10.0).phase is ReferenceGovernorPhase.HANDOFF
    assert runtime.consume(unconstrained(), 300.0, 10.0).phase is ReferenceGovernorPhase.HANDOFF
    result = runtime.consume(unconstrained(), 601.0, 10.0)
    assert result.phase is ReferenceGovernorPhase.IDLE
    assert result.handoff_ready is True


@pytest.mark.parametrize("cycle_min", [0.0, -1.0, float("nan"), float("inf"), None, "invalid"])
def test_invalid_cycle_is_rejected_during_handoff(cycle_min: object) -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    runtime.consume(unconstrained(), 2.0, 10.0)

    output = runtime.consume(unconstrained(nominal=23.0), 3.0, cycle_min)

    assert output.phase is ReferenceGovernorPhase.IDLE
    assert output.reason == "invalid_cycle"
    assert output.effective_reference == 23.0
    assert output.command_cap is None
    assert runtime.armed is False


def test_non_positive_cycle_does_not_abort_unconstrained_idle_arming() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)

    output = runtime.consume(unconstrained(), 1.0, 0.0)

    assert output.phase is ReferenceGovernorPhase.IDLE
    assert output.effective_reference == 22.0
    assert runtime.armed is True


def test_reconstraint_during_handoff_returns_to_governed_immediately() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    runtime.consume(unconstrained(), 2.0, 10.0)
    runtime.consume(unconstrained(), 300.0, 10.0)

    result = runtime.consume(constrained(admissible=21.2), 301.0, 10.0)
    assert result.phase is ReferenceGovernorPhase.GOVERNED
    assert result.effective_reference == 21.2
    assert result.command_cap == 0.4
    assert result.handoff_ready is False


def test_changed_target_rearms_from_governed_or_handoff() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)

    runtime.arm(23.0, 2.0)
    idle = runtime.consume(unconstrained(nominal=23.0), 3.0, 10.0)
    assert idle.phase is ReferenceGovernorPhase.IDLE
    assert runtime.armed is True
    assert runtime.target_temp == 23.0

    runtime.consume(constrained(nominal=23.0, admissible=22.5), 4.0, 10.0)
    runtime.consume(unconstrained(nominal=23.0), 5.0, 10.0)
    runtime.arm(24.0, 6.0)
    reset_handoff = runtime.consume(unconstrained(nominal=24.0), 7.0, 10.0)
    assert reset_handoff.phase is ReferenceGovernorPhase.IDLE
    assert runtime.armed is True
    assert runtime.target_temp == 24.0


def test_invalid_or_bypass_evidence_aborts_without_a_stale_cap() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)

    invalid = runtime.consume(kernel("invalid_model", nominal=23.0, admissible=20.0, cap=None), 2.0, 10.0)
    assert invalid.phase is ReferenceGovernorPhase.IDLE
    assert invalid.reason == "invalid_model"
    assert invalid.effective_reference == 23.0
    assert invalid.command_cap is None
    assert invalid.shaping_active is False
    assert runtime.armed is False

    bypass = runtime.consume(kernel("shadow_bypass_cool", nominal=24.0, cap=None), 3.0, 10.0)
    assert bypass.reason == "shadow_bypass_cool"
    assert bypass.command_cap is None


def test_abort_uses_prior_armed_target_as_last_reference_fallback() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(23.5, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    invalid = kernel(
        "invalid_model",
        nominal=float("nan"),
        admissible=float("inf"),
        cap=None,
    )

    output = runtime.consume(invalid, 2.0, 10.0)

    assert output.effective_reference == 23.5
    assert output.command_cap is None
    assert runtime.armed is False


def test_no_positive_demand_is_accepted_only_as_a_physical_no_op() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    handoff = runtime.consume(no_positive_demand(), 1.0, 10.0)
    assert handoff.phase is ReferenceGovernorPhase.HANDOFF
    assert runtime.armed is True

    malformed = runtime.consume(
        kernel("no_positive_demand", cap=0.2, predicted=None, target_bound=None, reserve=0.0),
        2.0,
        10.0,
    )
    assert malformed.reason == "no_positive_demand"
    assert malformed.command_cap is None
    assert runtime.armed is False


def test_armed_idle_no_positive_demand_completes_time_confirmed_handoff() -> None:
    """Reaching target without a prior cap must still close the armed episode."""
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)

    first = runtime.consume(no_positive_demand(), 60.0, 10.0)
    second = runtime.consume(no_positive_demand(), 360.0, 10.0)
    complete = runtime.consume(no_positive_demand(), 660.0, 10.0)

    assert first.phase is ReferenceGovernorPhase.HANDOFF
    assert second.phase is ReferenceGovernorPhase.HANDOFF
    assert complete.phase is ReferenceGovernorPhase.IDLE
    assert complete.handoff_ready is True
    assert runtime.armed is False


def test_completed_runtime_cannot_reactivate_until_changed_target_is_armed() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    runtime.consume(unconstrained(), 2.0, 10.0)
    runtime.consume(unconstrained(), 301.0, 10.0)
    runtime.consume(unconstrained(), 602.0, 10.0)

    later_constraint = runtime.consume(constrained(), 603.0, 10.0)
    assert later_constraint.phase is ReferenceGovernorPhase.IDLE
    assert later_constraint.command_cap is None
    assert later_constraint.shaping_active is False

    runtime.arm(23.0, 604.0)
    rearmed = runtime.consume(constrained(nominal=23.0, admissible=22.5), 605.0, 10.0)
    assert rearmed.phase is ReferenceGovernorPhase.GOVERNED
    assert rearmed.command_cap == 0.4


def test_same_target_arm_is_idempotent_unless_forced() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    runtime.arm(22.0, 2.0)
    still_governed = runtime.consume(unconstrained(), 3.0, 10.0)
    assert still_governed.phase is ReferenceGovernorPhase.HANDOFF

    runtime.arm(22.0, 4.0, force=True)
    reset_to_idle = runtime.consume(unconstrained(), 5.0, 10.0)
    assert reset_to_idle.phase is ReferenceGovernorPhase.IDLE
    assert runtime.armed is True


def test_reset_and_load_state_clear_all_runtime_evidence() -> None:
    runtime = ReferenceGovernorRuntime()
    runtime.arm(22.0, 0.0)
    runtime.consume(constrained(), 1.0, 10.0)
    runtime.reset()
    after_reset = runtime.consume(constrained(), 2.0, 10.0)
    assert after_reset.command_cap is None
    assert runtime.armed is False

    runtime.arm(22.0, 3.0)
    runtime.consume(constrained(), 4.0, 10.0)
    runtime.load_state({"phase": "governed", "command_cap": 0.1})
    after_load = runtime.consume(constrained(), 5.0, 10.0)
    assert after_load.command_cap is None
    assert after_load.phase is ReferenceGovernorPhase.IDLE
    assert runtime.armed is False
