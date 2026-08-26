"""Pure command projection and ownership value objects.

The scheduler interface exposes cycle durations rather than a causal command
identifier.  These structures keep the requested command, its locally
projected realization, and the frozen control context explicit so runtime
binding can be added without reconstructing ownership from controller state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandOwnershipBindingStatus(str, Enum):
    """State of a request-to-realization ownership binding."""

    NONE = "none"
    PENDING = "pending"
    BOUND = "bound"
    REUSED = "reused"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CycleCommandProjection:
    """Locally reproducible physical result of one scheduler command."""

    requested_power: float
    clamped_power: float
    cycle_duration_sec: int
    on_time_sec: int
    off_time_sec: int
    projected_power: float
    forced_by_timing: bool


@dataclass(frozen=True)
class CommandOwnershipSnapshot:
    """Frozen SmartPI decomposition for one projected actuator command."""

    projection: CycleCommandProjection
    hvac_mode: str
    u_ff1: float
    trim_stored: float
    u_ff_visible: float
    u_ff3: float
    u_p: float
    u_i: float
    ki: float
    gain_generation: int
    u_cmd: float
    u_limited: float
    linear_command: float
    regime: str | None
    i_mode: str | None
    constraint_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandOwnershipBinding:
    """Result of comparing a frozen request with a realized scheduler cycle."""

    status: CommandOwnershipBindingStatus
    snapshot: CommandOwnershipSnapshot | None
    reason: str | None = None
    realized_power: float | None = None
    realized_on_time_sec: int | None = None
    realized_off_time_sec: int | None = None


def project_cycle_command(
    on_percent: float,
    cycle_min: int,
    minimal_activation_delay: int | None = 0,
    minimal_deactivation_delay: int | None = 0,
) -> CycleCommandProjection:
    """Project the cycle that the existing switch scheduler can realize.

    This intentionally preserves the legacy truncation and timing branches.
    A timing constraint is distinct from normal integer-second quantization.
    """
    min_on = minimal_activation_delay if minimal_activation_delay is not None else 0
    min_off = (
        minimal_deactivation_delay if minimal_deactivation_delay is not None else 0
    )
    clamped_power = max(0.0, min(1.0, on_percent))

    cycle_duration_sec = cycle_min * 60
    on_time_sec = clamped_power * cycle_duration_sec
    forced_by_timing = False

    if on_time_sec > 0 and on_time_sec < min_on:
        on_time_sec = 0
        forced_by_timing = True

    off_time_sec = cycle_duration_sec - on_time_sec

    if on_time_sec < cycle_duration_sec and off_time_sec < min_off:
        on_time_sec = cycle_duration_sec
        off_time_sec = 0
        forced_by_timing = True

    realized_on_time = int(on_time_sec)
    realized_off_time = int(off_time_sec)
    projected_power = (
        realized_on_time / cycle_duration_sec if cycle_duration_sec else 0.0
    )
    return CycleCommandProjection(
        requested_power=on_percent,
        clamped_power=clamped_power,
        cycle_duration_sec=cycle_duration_sec,
        on_time_sec=realized_on_time,
        off_time_sec=realized_off_time,
        projected_power=projected_power,
        forced_by_timing=forced_by_timing,
    )
