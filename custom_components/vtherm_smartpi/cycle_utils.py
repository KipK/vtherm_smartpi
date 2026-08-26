"""Cycle helpers used by the SmartPI handler."""

from __future__ import annotations

from .smartpi.command_ownership import project_cycle_command


def calculate_cycle_times(
    on_percent: float,
    cycle_min: int,
    minimal_activation_delay: int | None = 0,
    minimal_deactivation_delay: int | None = 0,
) -> tuple[int, int, bool]:
    """Convert on_percent to on/off cycle times."""
    projection = project_cycle_command(
        on_percent=on_percent,
        cycle_min=cycle_min,
        minimal_activation_delay=minimal_activation_delay,
        minimal_deactivation_delay=minimal_deactivation_delay,
    )
    return (
        projection.on_time_sec,
        projection.off_time_sec,
        projection.forced_by_timing,
    )
