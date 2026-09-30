"""Tests for the neutral causal trace of physically committed power."""

import pytest

from custom_components.vtherm_smartpi.smartpi.causal_power_trace import (
    AppliedPowerSegment,
    CausalPowerTrace,
    ControlOwnershipSnapshot,
)
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.ff_trim import CausalFFTrimObserver


def _ownership(power: float) -> ControlOwnershipSnapshot:
    """Build a compact ownership snapshot for trace tests."""
    return ControlOwnershipSnapshot(
        u_ff1=power,
        trim_stored=0.0,
        u_ff_visible=power,
        u_ff3=0.0,
        u_p=0.0,
        u_i=0.0,
        ki=0.02,
        gain_generation=1,
        u_cmd=power,
        u_limited=power,
        linear_committed_power=power,
        regime=GovernanceRegime.DEAD_BAND,
        i_mode="I:FREEZE(deadband)",
    )


def test_switch_cycle_commits_only_realized_average_at_completion() -> None:
    """A switch cycle is unavailable until its realized duty is known."""
    trace = CausalPowerTrace()
    trace.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.8,
        ownership=_ownership(0.8),
        quality="switch_cycle_average",
    )

    assert trace.read_window(0.0, 60.0).status == "pending"
    assert trace.power_segments == ()

    trace.complete_applied_cycle(
        now_monotonic=60.0,
        realized_linear_power=0.35,
        use_valve_trace=False,
    )

    window = trace.read_window(0.0, 60.0)
    assert window.status == "complete"
    assert window.mean_linear_power == pytest.approx(0.35)
    assert window.power_coverage_ratio == pytest.approx(1.0)
    assert window.power_segments[0].quality == "switch_cycle_average"
    assert window.ownership_coverage_ratio == pytest.approx(1.0)


def test_valve_cycle_commits_segmented_linear_power_and_ownership() -> None:
    """Valve changes retain their physical durations and command owners."""
    trace = CausalPowerTrace()
    trace.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.2,
        ownership=_ownership(0.2),
        quality="valve_segmented_linear",
    )
    trace.update_applied_power(
        now_monotonic=30.0,
        linear_power=0.8,
        ownership=_ownership(0.8),
    )
    trace.complete_applied_cycle(
        now_monotonic=60.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )

    window = trace.read_window(0.0, 60.0)
    assert window.status == "complete"
    assert window.mean_linear_power == pytest.approx(0.5)
    assert [item.linear_power for item in window.power_segments] == [0.2, 0.8]
    assert all(
        item.quality == "valve_segmented_linear"
        for item in window.power_segments
    )
    assert window.ownership_coverage_ratio == pytest.approx(1.0)
    assert [
        item.ownership.linear_committed_power
        for item in window.ownership_segments
    ] == [0.2, 0.8]


def test_window_reports_pending_gap_and_staleness_separately() -> None:
    """Consumers can distinguish an unfinished tail, a hole, and old data."""
    pending_trace = CausalPowerTrace()
    pending_trace.record_applied_power(AppliedPowerSegment(0.0, 30.0, 0.2))
    pending = pending_trace.read_window(0.0, 60.0)
    assert pending.status == "pending"
    assert pending.power_coverage_ratio == pytest.approx(0.5)
    assert pending.max_power_gap_s == pytest.approx(30.0)

    gap_trace = CausalPowerTrace()
    gap_trace.record_applied_power(AppliedPowerSegment(0.0, 20.0, 0.2))
    gap_trace.record_applied_power(AppliedPowerSegment(40.0, 60.0, 0.8))
    gap = gap_trace.read_window(0.0, 60.0)
    assert gap.status == "gap"
    assert gap.power_coverage_ratio == pytest.approx(2.0 / 3.0)
    assert gap.max_power_gap_s == pytest.approx(20.0)

    stale_trace = CausalPowerTrace()
    stale_trace.record_applied_power(AppliedPowerSegment(0.0, 60.0, 0.5))
    stale_trace.record_applied_power(AppliedPowerSegment(80.0, 100.0, 0.7))
    stale = stale_trace.read_window(
        0.0,
        60.0,
        now_monotonic=120.0,
        max_age_s=20.0,
    )
    assert stale.status == "stale"
    assert stale.is_stale is True
    assert stale.age_s == pytest.approx(60.0)
    assert stale.last_committed_end_monotonic == pytest.approx(100.0)


def test_legacy_short_gap_fill_is_explicit_provenance() -> None:
    """The preserved five-second fill policy remains visible to consumers."""
    trace = CausalPowerTrace()
    trace.record_applied_power(AppliedPowerSegment(0.0, 20.0, 0.2))
    trace.record_applied_power(AppliedPowerSegment(24.0, 60.0, 0.8))

    window = trace.read_window(
        0.0,
        60.0,
        now_monotonic=120.0,
        max_age_s=20.0,
    )
    assert window.status == "imputed"
    assert window.is_stale is True
    assert window.power_coverage_ratio == pytest.approx(1.0)
    assert len(window.discontinuities) == 1
    assert window.discontinuities[0].reason == "legacy_gap_fill"
    assert window.discontinuities[0].start_monotonic == pytest.approx(20.0)
    assert window.discontinuities[0].end_monotonic == pytest.approx(24.0)


def test_discontinuity_and_reset_have_explicit_lifecycle() -> None:
    """Invalid evidence is marked, while a context reset starts a new epoch."""
    trace = CausalPowerTrace()
    trace.record_applied_power(AppliedPowerSegment(0.0, 60.0, 0.5))
    trace.mark_discontinuity(60.0, "partial_cycle")

    window = trace.read_window(0.0, 60.0)
    assert window.status == "discontinuity"
    assert window.discontinuities[0].reason == "partial_cycle"

    previous_epoch = trace.epoch
    trace.reset()

    assert trace.epoch == previous_epoch + 1
    assert trace.power_segments == ()
    assert trace.ownership_segments == ()
    assert trace.read_window(0.0, 60.0).status == "pending"


def test_logical_trim_resets_do_not_clear_shared_physical_evidence() -> None:
    """Window rejection and trim washout are separate from trace lifecycle."""
    trace = CausalPowerTrace()
    observer = CausalFFTrimObserver(cycle_min=5.0, physical_trace=trace)
    trace.record_applied_power(AppliedPowerSegment(0.0, 60.0, 0.5))
    initial_epoch = trace.epoch

    observer.invalidate("trajectory_active", now_monotonic=60.0, washout_s=30.0)
    observer.reset_after_trim_update(now_monotonic=60.0, washout_s=30.0)
    observer.reset_runtime(reset_physical_trace=False)

    assert trace.epoch == initial_epoch
    assert trace.read_window(0.0, 60.0).status == "complete"

    observer.reset_runtime()

    assert trace.epoch == initial_epoch + 1
    assert trace.power_segments == ()
