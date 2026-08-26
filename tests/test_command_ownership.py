"""Unit tests for pure command projection and ownership value objects."""

import pytest

from custom_components.vtherm_smartpi.cycle_utils import calculate_cycle_times
from custom_components.vtherm_smartpi.smartpi.command_ownership import (
    CommandOwnershipBinding,
    CommandOwnershipBindingStatus,
    CommandOwnershipSnapshot,
    project_cycle_command,
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
