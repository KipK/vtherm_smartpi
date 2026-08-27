"""Unit tests for pure command projection and ownership value objects."""

from dataclasses import replace

import pytest

from custom_components.vtherm_smartpi.cycle_utils import calculate_cycle_times
from custom_components.vtherm_smartpi.smartpi.command_ownership import (
    CommandOwnershipBinding,
    CommandOwnershipBindingStatus,
    CommandOwnershipSnapshot,
    CommandOwnershipTracker,
    project_cycle_command,
    project_switch_repeat_command,
    project_valve_actuator_power,
)


def _snapshot(
    power: float = 0.5,
    *,
    hvac_mode: str = "heat",
    u_i: float = 0.09,
    constraint_flags: tuple[str, ...] = (),
    minimal_activation_delay: int = 0,
) -> CommandOwnershipSnapshot:
    """Build a consistent frozen switch command for tracker tests."""
    return CommandOwnershipSnapshot(
        projection=project_cycle_command(
            power,
            cycle_min=2,
            minimal_activation_delay=minimal_activation_delay,
        ),
        hvac_mode=hvac_mode,
        u_ff1=0.3,
        trim_stored=0.01,
        u_ff_visible=0.31,
        u_ff3=0.0,
        u_p=power - 0.31 - u_i,
        u_i=u_i,
        ki=0.02,
        gain_generation=4,
        u_cmd=power,
        u_limited=power,
        linear_command=power,
        regime="dead_band",
        i_mode="I:FREEZE(deadband)",
        constraint_flags=constraint_flags,
    )


def test_projection_keeps_normal_second_quantization_distinct_from_timing() -> None:
    """A truncated scheduler duration remains a valid, unconstrained projection."""
    projection = project_cycle_command(0.132, cycle_min=2)

    assert projection.on_time_sec == 15
    # The legacy helper truncates ON and OFF independently, leaving one second
    # unallocated for this fractional request.
    assert projection.off_time_sec == 104
    assert projection.projected_power == pytest.approx(0.125)
    assert projection.forced_by_timing is False


@pytest.mark.parametrize(
    ("requested_power", "expected_on", "expected_off"),
    ((-0.2, 0, 120), (0.0, 0, 120), (1.0, 120, 0), (1.2, 120, 0)),
)
def test_projection_clamps_power_at_scheduler_bounds(
    requested_power: float,
    expected_on: int,
    expected_off: int,
) -> None:
    """Out-of-range requests retain their diagnostic value but project safely."""
    projection = project_cycle_command(requested_power, cycle_min=2)

    assert projection.requested_power == requested_power
    assert projection.on_time_sec == expected_on
    assert projection.off_time_sec == expected_off
    assert projection.forced_by_timing is False


def test_projection_marks_short_on_time_as_timing_constrained() -> None:
    """A minimum activation delay forces a physical OFF command."""
    projection = project_cycle_command(
        0.1,
        cycle_min=2,
        minimal_activation_delay=20,
    )

    assert (projection.on_time_sec, projection.off_time_sec) == (0, 120)
    assert projection.projected_power == 0.0
    assert projection.forced_by_timing is True


def test_projection_marks_short_off_time_as_timing_constrained() -> None:
    """A minimum deactivation delay forces a physical ON command."""
    projection = project_cycle_command(
        0.9,
        cycle_min=2,
        minimal_deactivation_delay=20,
    )

    assert (projection.on_time_sec, projection.off_time_sec) == (120, 0)
    assert projection.projected_power == 1.0
    assert projection.forced_by_timing is True


def test_legacy_cycle_helper_returns_projection_cycle_times() -> None:
    """The compatibility tuple keeps the legacy scheduler-facing behavior."""
    projection = project_cycle_command(0.132, cycle_min=2)

    assert calculate_cycle_times(0.132, 2) == (
        projection.on_time_sec,
        projection.off_time_sec,
        projection.forced_by_timing,
    )


def test_switch_repeat_projection_matches_scheduler_double_quantization() -> None:
    """An automatic repeat reprojects the first realized PWM ratio."""
    direct = project_cycle_command(0.228005, cycle_min=2)

    repeat = project_switch_repeat_command(direct)

    assert (direct.on_time_sec, direct.off_time_sec) == (27, 92)
    assert direct.projected_power == pytest.approx(0.225)
    assert (repeat.on_time_sec, repeat.off_time_sec) == (27, 93)
    assert repeat.projected_power == pytest.approx(0.225)


def test_tracker_binds_reprojected_automatic_switch_repeat() -> None:
    """A deferred switch request owns the scheduler's repeat callback."""
    tracker = CommandOwnershipTracker()
    direct = project_cycle_command(0.228005, cycle_min=2)
    staged = tracker.stage(replace(_snapshot(0.228005), projection=direct))

    binding = tracker.bind_started_cycle(
        on_time_sec=27,
        off_time_sec=93,
        realized_power=0.225,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.snapshot is staged
    assert binding.snapshot is not None
    assert binding.snapshot.projection == direct
    assert binding.snapshot.projection.requested_power == pytest.approx(0.228005)


def test_tracker_reuses_reprojected_automatic_switch_repeat() -> None:
    """A repeat without a new submission keeps the last proven snapshot."""
    tracker = CommandOwnershipTracker()
    direct = project_cycle_command(0.228005, cycle_min=2)
    staged = tracker.stage(replace(_snapshot(0.228005), projection=direct))
    tracker.bind_started_cycle(
        on_time_sec=27,
        off_time_sec=92,
        realized_power=0.225,
        hvac_mode="heat",
    )
    tracker.complete_active()

    binding = tracker.bind_started_cycle(
        on_time_sec=27,
        off_time_sec=93,
        realized_power=0.225,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REUSED
    assert binding.snapshot is staged


def test_tracker_reuses_successive_scheduler_reprojections() -> None:
    """A reused switch snapshot follows every exact scheduler re-projection."""
    tracker = CommandOwnershipTracker()
    direct = project_cycle_command(0.072223, cycle_min=3)
    first_repeat = project_switch_repeat_command(direct)
    second_repeat = project_switch_repeat_command(first_repeat)
    staged = tracker.stage(replace(_snapshot(0.072223), projection=direct))

    initial = tracker.bind_started_cycle(
        on_time_sec=13,
        off_time_sec=166,
        realized_power=13 / 180,
        hvac_mode="heat",
    )
    tracker.complete_active()
    first_reuse = tracker.bind_started_cycle(
        on_time_sec=12,
        off_time_sec=167,
        realized_power=12 / 180,
        hvac_mode="heat",
    )
    tracker.complete_active()
    second_reuse = tracker.bind_started_cycle(
        on_time_sec=12,
        off_time_sec=168,
        realized_power=12 / 180,
        hvac_mode="heat",
    )

    assert (direct.on_time_sec, first_repeat.on_time_sec, second_repeat.on_time_sec) == (
        13,
        12,
        12,
    )
    assert (
        direct.off_time_sec,
        first_repeat.off_time_sec,
        second_repeat.off_time_sec,
    ) == (166, 167, 168)
    assert initial.status == CommandOwnershipBindingStatus.BOUND
    assert first_reuse.status == CommandOwnershipBindingStatus.REUSED
    assert second_reuse.status == CommandOwnershipBindingStatus.REUSED
    assert second_reuse.snapshot is staged


def test_tracker_rejects_switch_off_duration_outside_direct_and_repeat() -> None:
    """Only the exact scheduler repeat may differ from the direct projection."""
    tracker = CommandOwnershipTracker()
    direct = project_cycle_command(0.228005, cycle_min=2)
    tracker.stage(replace(_snapshot(0.228005), projection=direct))

    binding = tracker.bind_started_cycle(
        on_time_sec=27,
        off_time_sec=91,
        realized_power=0.225,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_commit_mismatch"


def test_ownership_value_objects_keep_a_frozen_projection() -> None:
    """Bindings retain the submitted snapshot without consulting live state."""
    snapshot = CommandOwnershipSnapshot(
        projection=project_cycle_command(0.5, cycle_min=2),
        hvac_mode="heat",
        u_ff1=0.3,
        trim_stored=0.01,
        u_ff_visible=0.31,
        u_ff3=0.0,
        u_p=0.1,
        u_i=0.09,
        ki=0.02,
        gain_generation=4,
        u_cmd=0.5,
        u_limited=0.5,
        linear_command=0.5,
        regime="stable",
        i_mode="I:FREEZE(deadband)",
    )
    binding = CommandOwnershipBinding(
        status=CommandOwnershipBindingStatus.PENDING,
        snapshot=snapshot,
    )

    assert binding.snapshot is snapshot
    assert binding.snapshot.projection.projected_power == 0.5


def test_tracker_binds_normal_scheduler_quantization() -> None:
    """Integer-second truncation must bind the frozen request."""
    tracker = CommandOwnershipTracker()
    snapshot = _snapshot(0.132)
    staged = tracker.stage(snapshot)

    binding = tracker.bind_started_cycle(
        on_time_sec=15,
        off_time_sec=104,
        realized_power=0.125,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.snapshot is staged


def test_tracker_binds_an_actuator_projection_without_comparing_linear_power() -> None:
    """A nonlinear valve command must validate in actuator command space."""
    tracker = CommandOwnershipTracker()
    actuator_projection = project_cycle_command(0.15, cycle_min=2)
    snapshot = replace(
        _snapshot(0.8),
        projection=actuator_projection,
        linear_command=0.8,
        actuator_command=0.15,
    )
    tracker.stage(snapshot)

    binding = tracker.bind_started_cycle(
        on_time_sec=actuator_projection.on_time_sec,
        off_time_sec=actuator_projection.off_time_sec,
        realized_power=actuator_projection.projected_power,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND


def test_tracker_keeps_scheduler_and_published_valve_power_separate() -> None:
    """A valve may publish an integer-percent command after PWM quantization."""
    tracker = CommandOwnershipTracker()
    projection = project_cycle_command(0.132, cycle_min=2)
    snapshot = replace(
        _snapshot(0.132),
        projection=projection,
        actuator_command=0.132,
        expected_actuator_power=project_valve_actuator_power(0.132),
    )
    tracker.stage(snapshot)

    binding = tracker.bind_started_cycle(
        on_time_sec=15,
        off_time_sec=104,
        realized_power=0.13,
        scheduler_realized_power=0.125,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.realized_power == pytest.approx(0.13)
    assert binding.scheduler_realized_power == pytest.approx(0.125)


def test_tracker_rejects_retained_valve_opening() -> None:
    """Valve dpercent filtering must not inherit ownership from a stale opening."""
    tracker = CommandOwnershipTracker()
    projection = project_cycle_command(0.132, cycle_min=2)
    tracker.stage(
        replace(
            _snapshot(0.132),
            projection=projection,
            expected_actuator_power=0.13,
        )
    )

    binding = tracker.bind_started_cycle(
        on_time_sec=15,
        off_time_sec=104,
        realized_power=0.11,
        scheduler_realized_power=0.125,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_actuator_mismatch"


def test_tracker_replaces_pending_request_with_latest_snapshot() -> None:
    """Only the latest mid-cycle request may own the next scheduler repeat."""
    tracker = CommandOwnershipTracker()
    first = tracker.stage(_snapshot(0.4, u_i=0.08))
    latest = _snapshot(0.6, u_i=0.12)
    staged_latest = tracker.stage(latest)

    binding = tracker.bind_started_cycle(
        on_time_sec=72,
        off_time_sec=48,
        realized_power=0.6,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert first is not None
    assert staged_latest is not None
    assert first.request_sequence == 1
    assert staged_latest.request_sequence == 2
    assert binding.snapshot is staged_latest
    assert binding.snapshot.u_i == pytest.approx(0.12)


def test_tracker_reuses_only_an_identical_automatic_repeat() -> None:
    """An unchanged repeat may reuse the last proven frozen ownership."""
    tracker = CommandOwnershipTracker()
    snapshot = _snapshot(0.5)
    staged = tracker.stage(snapshot)
    tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="heat",
    )
    tracker.complete_active()

    binding = tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REUSED
    assert binding.snapshot is staged
    assert binding.snapshot.request_sequence == 1


def test_tracker_rejects_changed_repeat_without_fallback() -> None:
    """A different callback without a request must discard the old binding."""
    tracker = CommandOwnershipTracker()
    tracker.stage(_snapshot(0.5))
    tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="heat",
    )

    binding = tracker.bind_started_cycle(
        on_time_sec=61,
        off_time_sec=59,
        realized_power=61 / 120,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_commit_mismatch"
    assert tracker.active is None


def test_unowned_pending_request_blocks_previous_binding_reuse() -> None:
    """A submitted request without decomposition must fail closed."""
    tracker = CommandOwnershipTracker()
    tracker.stage(_snapshot(0.5))
    tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="heat",
    )
    tracker.stage(None)

    binding = tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_request_missing"


def test_tracker_rejects_timing_constraint_and_hvac_change() -> None:
    """Timing-forced and cross-mode cycles must never become owned."""
    tracker = CommandOwnershipTracker()
    timing_snapshot = _snapshot(
        0.1,
        minimal_activation_delay=20,
    )
    tracker.stage(timing_snapshot)

    timing_binding = tracker.bind_started_cycle(
        on_time_sec=0,
        off_time_sec=120,
        realized_power=0.0,
        hvac_mode="heat",
    )

    assert timing_binding.status == CommandOwnershipBindingStatus.REJECTED
    assert timing_binding.reason == "scheduler_timing"

    tracker.stage(_snapshot(0.5, hvac_mode="heat"))
    mode_binding = tracker.bind_started_cycle(
        on_time_sec=60,
        off_time_sec=60,
        realized_power=0.5,
        hvac_mode="cool",
    )

    assert mode_binding.status == CommandOwnershipBindingStatus.REJECTED
    assert mode_binding.reason == "ownership_context_changed"


def test_partial_cycle_invalidation_preserves_next_pending_request() -> None:
    """A cancelled active cycle must not erase a request already staged to replace it."""
    tracker = CommandOwnershipTracker()
    tracker.stage(_snapshot(0.4))
    tracker.bind_started_cycle(
        on_time_sec=48,
        off_time_sec=72,
        realized_power=0.4,
        hvac_mode="heat",
    )
    pending = _snapshot(0.7)
    staged_pending = tracker.stage(pending)

    tracker.invalidate_active("partial_cycle")
    assert tracker.last_staged_sequence == 2
    binding = tracker.bind_started_cycle(
        on_time_sec=84,
        off_time_sec=36,
        realized_power=0.7,
        hvac_mode="heat",
    )

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.snapshot is staged_pending
    assert binding.snapshot.request_sequence == 2
