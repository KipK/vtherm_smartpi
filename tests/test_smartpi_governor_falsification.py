"""Focused tests for the pure governor prediction-falsification evidence."""

from dataclasses import FrozenInstanceError

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    REFERENCE_GOVERNOR_FALSIFICATION_COUNT,
    REFERENCE_GOVERNOR_FALSIFICATION_MAX_SLOPE_H,
)
from custom_components.vtherm_smartpi.smartpi.governor_falsification import (
    RETIRE_REASON,
    GovernorPredictionFalsification,
    PredictionFalsificationDecision,
)
from custom_components.vtherm_smartpi.smartpi.reference_governor import (
    ReferenceGovernorDecision,
)


def cap_decision(*, predicted: float, reason: str = "cap", coast: bool = False) -> ReferenceGovernorDecision:
    """Return constraining kernel evidence promising ``predicted`` at the horizon."""
    return ReferenceGovernorDecision(
        active=True,
        reason=reason,
        nominal_reference=20.5,
        admissible_reference=20.05,
        command_cap=0.3,
        predicted_terminal_temp=predicted,
        target_bound=predicted,
        reserve_candidates=None,
        dynamic_reserve_c=0.0,
        constraint_active=True,
        coast_required=coast,
    )


def unconstrained_decision() -> ReferenceGovernorDecision:
    return ReferenceGovernorDecision(
        active=False,
        reason="unconstrained",
        nominal_reference=20.5,
        admissible_reference=20.5,
        command_cap=0.5,
        predicted_terminal_temp=20.4,
        target_bound=20.55,
        reserve_candidates=None,
        dynamic_reserve_c=0.0,
        constraint_active=False,
        coast_required=False,
    )


def observe(
    tracker: GovernorPredictionFalsification,
    now: float,
    *,
    decision: ReferenceGovernorDecision | None = None,
    current: float = 20.0,
    target: float = 20.5,
    slope: float | None = 0.0,
    mode=VThermHvacMode_HEAT,
    deadtime: float = 300.0,
    remaining: float = 0.0,
    cycle: float = 4.0,
    tolerance: float = 0.05,
) -> PredictionFalsificationDecision:
    return tracker.observe(
        decision=cap_decision(predicted=target + (0.05 if mode == VThermHvacMode_HEAT else -0.05))
        if decision is None else decision,
        hvac_mode=mode,
        target_temp=target,
        current_temp=current,
        measured_slope_h=slope,
        remaining_cycle_min=remaining,
        stop_deadtime_s=deadtime,
        now_monotonic=now,
        cycle_min=cycle,
        tolerance_c=tolerance,
    )


def drive_until_retire(tracker, *, step: float = 60.0, limit: float = 3600.0, **kwargs):
    """Return the retiring decision and its time, or the last decision and time."""
    now = 0.0
    decision = observe(tracker, now, **kwargs)
    while not decision.retire and now < limit:
        now += step
        decision = observe(tracker, now, **kwargs)
    return decision, now


def test_decision_is_immutable_and_defaults_are_empty() -> None:
    decision = PredictionFalsificationDecision(False, "not_constraining")
    assert decision.consecutive_shortfalls == 0
    assert decision.shortfall_span_s == 0.0
    assert decision.last_shortfall_c is None
    with pytest.raises(FrozenInstanceError):
        decision.retire = True


def test_shortfalls_count_only_after_the_horizon_matures() -> None:
    tracker = GovernorPredictionFalsification()
    early = observe(tracker, 0.0)
    before_horizon = observe(tracker, 299.0)
    at_horizon = observe(tracker, 300.0)

    assert early.consecutive_shortfalls == 0
    assert early.last_shortfall_c is None
    assert before_horizon.consecutive_shortfalls == 0
    assert at_horizon.consecutive_shortfalls == 1
    assert at_horizon.last_shortfall_c == pytest.approx(0.55)
    assert at_horizon.reason == "shortfall_count_insufficient"


def test_remaining_cycle_extends_the_horizon() -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0, remaining=1.0)
    assert observe(tracker, 300.0, remaining=1.0).consecutive_shortfalls == 0
    assert observe(tracker, 360.0, remaining=1.0).consecutive_shortfalls == 1


def test_retires_after_count_and_span_of_deadtime_plus_one_cycle() -> None:
    tracker = GovernorPredictionFalsification()
    decision, now = drive_until_retire(tracker)

    assert decision.retire is True
    assert decision.reason == RETIRE_REASON
    assert decision.consecutive_shortfalls >= REFERENCE_GOVERNOR_FALSIFICATION_COUNT
    assert decision.shortfall_span_s >= 300.0 + 4.0 * 60.0
    # First shortfall matures at 300 s; the span must then cover 540 s.
    assert now == pytest.approx(840.0)
    assert decision.tolerance_c == pytest.approx(0.05)


def test_cool_mode_mirrors_heat_retirement() -> None:
    heat, heat_time = drive_until_retire(GovernorPredictionFalsification())
    cool, cool_time = drive_until_retire(
        GovernorPredictionFalsification(), mode=VThermHvacMode_COOL,
        current=21.0, target=20.5,
    )
    assert heat.retire and cool.retire
    assert heat_time == cool_time
    assert heat.last_shortfall_c == pytest.approx(cool.last_shortfall_c)


def test_measurement_meeting_the_promise_resets_the_run() -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    observe(tracker, 60.0)
    assert observe(tracker, 300.0).consecutive_shortfalls == 1
    # The promise was 20.55; a room at 20.52 is within the 0.05 tolerance.
    met = observe(tracker, 360.0, current=20.52)
    assert met.consecutive_shortfalls == 0
    assert met.shortfall_span_s == 0.0
    assert met.last_shortfall_c == pytest.approx(0.03)


def test_one_measurement_judges_only_the_freshest_matured_promise() -> None:
    tracker = GovernorPredictionFalsification()
    for now in (0.0, 10.0, 20.0, 30.0):
        observe(tracker, now)
    # All four promises mature at once; a single observation counts once.
    assert observe(tracker, 400.0).consecutive_shortfalls == 1


@pytest.mark.parametrize(
    "decision",
    [
        unconstrained_decision(),
        cap_decision(predicted=20.55, coast=True, reason="coast"),
        cap_decision(predicted=float("nan")),
    ],
)
def test_non_cap_decisions_clear_all_evidence(decision: ReferenceGovernorDecision) -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    assert observe(tracker, 300.0).consecutive_shortfalls == 1

    cleared = observe(tracker, 360.0, decision=decision)

    assert cleared.retire is False
    assert cleared.reason == "not_constraining"
    assert tracker.consecutive_shortfalls == 0
    assert tracker.pending_records == ()


@pytest.mark.parametrize("slope", [REFERENCE_GOVERNOR_FALSIFICATION_MAX_SLOPE_H + 0.01, 1.0])
def test_rising_room_in_demand_direction_blocks_retirement(slope: float) -> None:
    decision, _ = drive_until_retire(GovernorPredictionFalsification(), slope=slope, limit=1500.0)
    assert decision.retire is False
    assert decision.reason == "slope_blocks_retirement"
    assert decision.consecutive_shortfalls >= REFERENCE_GOVERNOR_FALSIFICATION_COUNT


def test_falling_room_or_missing_slope_is_handled_signed() -> None:
    falling, _ = drive_until_retire(GovernorPredictionFalsification(), slope=-0.5)
    assert falling.retire is True
    missing, _ = drive_until_retire(GovernorPredictionFalsification(), slope=None, limit=1500.0)
    assert missing.retire is False
    assert missing.reason == "slope_blocks_retirement"
    cool_rising, _ = drive_until_retire(
        GovernorPredictionFalsification(), mode=VThermHvacMode_COOL,
        current=21.0, target=20.5, slope=-0.5, limit=1500.0,
    )
    assert cool_rising.retire is False


def test_tolerance_floor_rejects_small_shortfalls() -> None:
    tracker = GovernorPredictionFalsification()
    decision, _ = drive_until_retire(tracker, current=20.51, limit=1500.0)
    assert decision.retire is False
    assert decision.consecutive_shortfalls == 0
    assert decision.last_shortfall_c == pytest.approx(0.04)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tolerance": -0.01},
        {"deadtime": 0.0},
        {"cycle": 0.0},
        {"remaining": -1.0},
        {"current": float("nan")},
        {"mode": None},
    ],
)
def test_invalid_evidence_clears_and_never_retires(overrides: dict) -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    assert observe(tracker, 300.0).consecutive_shortfalls == 1

    invalid = observe(tracker, 360.0, **overrides)

    assert invalid.retire is False
    assert invalid.reason == "invalid_evidence"
    assert tracker.consecutive_shortfalls == 0


def test_invalid_timestamp_clears_evidence() -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    invalid = tracker.observe(
        decision=cap_decision(predicted=20.55), hvac_mode=VThermHvacMode_HEAT,
        target_temp=20.5, current_temp=20.0, measured_slope_h=0.0,
        remaining_cycle_min=0.0, stop_deadtime_s=300.0, now_monotonic=float("nan"),
        cycle_min=4.0, tolerance_c=0.05,
    )
    assert invalid.reason == "invalid_evidence"
    assert tracker.pending_records == ()


def test_reset_drops_pending_promises_and_run() -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    observe(tracker, 300.0)
    tracker.reset()
    assert tracker.consecutive_shortfalls == 0
    assert tracker.pending_records == ()
    assert observe(tracker, 360.0).consecutive_shortfalls == 0


def test_duplicate_timestamp_does_not_add_a_second_promise() -> None:
    tracker = GovernorPredictionFalsification()
    observe(tracker, 0.0)
    observe(tracker, 0.0)
    assert len(tracker.pending_records) == 1
