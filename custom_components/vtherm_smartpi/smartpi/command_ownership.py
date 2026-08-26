"""Pure command projection and ownership value objects.

The scheduler interface exposes cycle durations rather than a causal command
identifier.  These structures keep the requested command, its locally
projected realization, and the frozen control context explicit so runtime
binding can be added without reconstructing ownership from controller state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


OWNERSHIP_MATCH_EPSILON = 1e-9


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
    request_sequence: int = 0
    constraint_flags: tuple[str, ...] = ()
    actuator_command: float | None = None
    expected_actuator_power: float | None = None


@dataclass(frozen=True)
class CommandOwnershipBinding:
    """Result of comparing a frozen request with a realized scheduler cycle."""

    status: CommandOwnershipBindingStatus
    snapshot: CommandOwnershipSnapshot | None
    reason: str | None = None
    realized_power: float | None = None
    scheduler_realized_power: float | None = None
    realized_on_time_sec: int | None = None
    realized_off_time_sec: int | None = None


class CommandOwnershipTracker:
    """Bind scheduler callbacks to the latest frozen command request.

    A pending request is tracked separately from its optional snapshot.  This
    prevents an unowned request from falling back to the previously bound
    command when the scheduler eventually commits it.
    """

    def __init__(self) -> None:
        self._pending_received = False
        self._last_request_sequence = 0
        self._pending: CommandOwnershipSnapshot | None = None
        self._active: CommandOwnershipSnapshot | None = None
        self._last_bound: CommandOwnershipSnapshot | None = None
        self._last_binding = CommandOwnershipBinding(
            status=CommandOwnershipBindingStatus.NONE,
            snapshot=None,
        )

    @property
    def pending(self) -> CommandOwnershipSnapshot | None:
        """Return the latest staged snapshot, if it is causally complete."""
        return self._pending

    @property
    def active(self) -> CommandOwnershipSnapshot | None:
        """Return the snapshot owning the currently started cycle."""
        return self._active

    @property
    def last_binding(self) -> CommandOwnershipBinding:
        """Return the latest staging, binding, reuse, or rejection result."""
        return self._last_binding

    @property
    def last_staged_sequence(self) -> int:
        """Return the sequence assigned to the latest submitted request."""
        return self._last_request_sequence

    def stage(
        self,
        snapshot: CommandOwnershipSnapshot | None,
    ) -> CommandOwnershipSnapshot | None:
        """Replace the request that the scheduler will use for its next cycle."""
        self._last_request_sequence += 1
        self._pending_received = True
        self._pending = (
            replace(snapshot, request_sequence=self._last_request_sequence)
            if snapshot is not None
            else None
        )
        self._last_binding = CommandOwnershipBinding(
            status=CommandOwnershipBindingStatus.PENDING,
            snapshot=self._pending,
        )
        return self._pending

    def bind_started_cycle(
        self,
        *,
        on_time_sec: float,
        off_time_sec: float,
        realized_power: float | None,
        hvac_mode: object,
        scheduler_realized_power: float | None = None,
    ) -> CommandOwnershipBinding:
        """Bind one physical cycle without consulting current controller state."""
        if self._pending_received:
            candidate = self._pending
            self._pending_received = False
            self._pending = None
            if candidate is None:
                return self._reject(
                    "ownership_request_missing",
                    snapshot=None,
                    realized_power=realized_power,
                    on_time_sec=on_time_sec,
                    off_time_sec=off_time_sec,
                )
            status = CommandOwnershipBindingStatus.BOUND
        else:
            candidate = self._last_bound
            if candidate is None:
                return self._reject(
                    "ownership_request_missing",
                    snapshot=None,
                    realized_power=realized_power,
                    on_time_sec=on_time_sec,
                    off_time_sec=off_time_sec,
                )
            status = CommandOwnershipBindingStatus.REUSED

        if candidate.hvac_mode != str(hvac_mode):
            return self._reject(
                "ownership_context_changed",
                snapshot=candidate,
                realized_power=realized_power,
                on_time_sec=on_time_sec,
                off_time_sec=off_time_sec,
            )

        projection = candidate.projection
        if (
            abs(float(on_time_sec) - projection.on_time_sec)
            > OWNERSHIP_MATCH_EPSILON
            or abs(float(off_time_sec) - projection.off_time_sec)
            > OWNERSHIP_MATCH_EPSILON
            or (
                scheduler_realized_power is not None
                and abs(float(scheduler_realized_power) - projection.projected_power)
                > OWNERSHIP_MATCH_EPSILON
            )
        ):
            return self._reject(
                "ownership_commit_mismatch",
                snapshot=candidate,
                realized_power=realized_power,
                on_time_sec=on_time_sec,
                off_time_sec=off_time_sec,
                scheduler_realized_power=scheduler_realized_power,
            )

        expected_actuator_power = candidate.expected_actuator_power
        if expected_actuator_power is None:
            expected_actuator_power = projection.projected_power
        if realized_power is None:
            return self._reject(
                "ownership_actuator_missing",
                snapshot=candidate,
                realized_power=None,
                on_time_sec=on_time_sec,
                off_time_sec=off_time_sec,
                scheduler_realized_power=scheduler_realized_power,
            )
        if (
            abs(float(realized_power) - expected_actuator_power)
            > OWNERSHIP_MATCH_EPSILON
        ):
            return self._reject(
                (
                    "ownership_actuator_mismatch"
                    if candidate.expected_actuator_power is not None
                    else "ownership_commit_mismatch"
                ),
                snapshot=candidate,
                realized_power=realized_power,
                on_time_sec=on_time_sec,
                off_time_sec=off_time_sec,
                scheduler_realized_power=scheduler_realized_power,
            )

        if projection.forced_by_timing or "timing" in candidate.constraint_flags:
            return self._reject(
                "scheduler_timing",
                snapshot=candidate,
                realized_power=realized_power,
                on_time_sec=on_time_sec,
                off_time_sec=off_time_sec,
                scheduler_realized_power=scheduler_realized_power,
            )

        self._active = candidate
        self._last_bound = candidate
        binding = CommandOwnershipBinding(
            status=status,
            snapshot=candidate,
            realized_power=float(realized_power),
            scheduler_realized_power=(
                float(scheduler_realized_power)
                if scheduler_realized_power is not None
                else None
            ),
            realized_on_time_sec=int(on_time_sec),
            realized_off_time_sec=int(off_time_sec),
        )
        self._last_binding = binding
        return binding

    def invalidate_active(self, reason: str = "ownership_discontinuity") -> None:
        """Invalidate reuse while preserving a request staged for the next cycle."""
        self._active = None
        self._last_bound = None
        if not self._pending_received:
            self._last_binding = CommandOwnershipBinding(
                status=CommandOwnershipBindingStatus.REJECTED,
                snapshot=None,
                reason=reason,
            )

    def complete_active(self) -> None:
        """Mark the current segment complete while retaining safe reuse state."""
        self._active = None

    def reset(self, reason: str | None = None) -> None:
        """Discard all transient command ownership state."""
        self._pending_received = False
        self._last_request_sequence = 0
        self._pending = None
        self._active = None
        self._last_bound = None
        self._last_binding = CommandOwnershipBinding(
            status=(
                CommandOwnershipBindingStatus.REJECTED
                if reason is not None
                else CommandOwnershipBindingStatus.NONE
            ),
            snapshot=None,
            reason=reason,
        )

    def _reject(
        self,
        reason: str,
        *,
        snapshot: CommandOwnershipSnapshot | None,
        realized_power: float | None,
        on_time_sec: float,
        off_time_sec: float,
        scheduler_realized_power: float | None = None,
    ) -> CommandOwnershipBinding:
        """Reject one callback and prevent reuse of any older binding."""
        self._active = None
        self._last_bound = None
        binding = CommandOwnershipBinding(
            status=CommandOwnershipBindingStatus.REJECTED,
            snapshot=snapshot,
            reason=reason,
            realized_power=(
                float(realized_power) if realized_power is not None else None
            ),
            scheduler_realized_power=(
                float(scheduler_realized_power)
                if scheduler_realized_power is not None
                else None
            ),
            realized_on_time_sec=int(on_time_sec),
            realized_off_time_sec=int(off_time_sec),
        )
        self._last_binding = binding
        return binding


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


def project_valve_actuator_power(requested_power: float) -> float:
    """Return the integer-percent actuator value published by VT valve climate."""
    clamped_power = max(0.0, min(1.0, requested_power))
    return round(clamped_power * 100) / 100
