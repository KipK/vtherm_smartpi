"""Command ownership bound to the applied-command receipts of the scheduler.

SmartPI tags each scheduler request with an opaque, strictly increasing
``request_id`` and freezes the control decomposition that produced it. The
cycle scheduler echoes that identifier in an applied-command receipt for each
command it really applies (vtherm_api ``AppliedCommandReceipt``). A receipt is
owned only when it names a known request, keeps its HVAC mode, and carries no
alteration; ownership is never reconstructed from controller state.
"""

from __future__ import annotations

import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from vtherm_api.command_receipt import CommandAlteration

if TYPE_CHECKING:
    from vtherm_api.command_receipt import AppliedCommandReceipt

# Requests kept for late receipts: the scheduler may apply an older pending request.
MAX_TRACKED_REQUESTS = 32


class CommandOwnershipBindingStatus(str, Enum):
    """State of a request-to-receipt ownership binding."""

    NONE = "none"
    PENDING = "pending"
    BOUND = "bound"
    REUSED = "reused"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CommandOwnershipSnapshot:
    """Frozen SmartPI decomposition for one scheduler request."""

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
    regime: str | None
    i_mode: str | None
    constraint_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandOwnershipBinding:
    """Result of staging a request or of binding one applied-command receipt."""

    status: CommandOwnershipBindingStatus
    request_id: int | None = None
    snapshot: CommandOwnershipSnapshot | None = None
    reason: str | None = None
    kind: str | None = None
    requested_power: float | None = None
    realized_power: float | None = None
    realized_on_time_sec: int | None = None
    realized_off_time_sec: int | None = None
    alterations: tuple[str, ...] = ()

    @property
    def owned_snapshot(self) -> CommandOwnershipSnapshot | None:
        """Return the snapshot only when the receipt is owned."""
        if self.status in (
            CommandOwnershipBindingStatus.BOUND,
            CommandOwnershipBindingStatus.REUSED,
        ):
            return self.snapshot
        return None


class CommandOwnershipTracker:
    """Map request identifiers to frozen snapshots and bind receipts to them.

    Identifiers start at ``time.monotonic_ns()`` so a request staged by a
    previous instance, still pending in the scheduler, is never mistaken for a
    request of this instance. The last ``MAX_TRACKED_REQUESTS`` requests are
    kept, plus the currently bound one. A value of ``None`` marks a request
    staged without a complete decomposition; a string marks a sticky rejection.
    """

    def __init__(self) -> None:
        self._next_id = time.monotonic_ns()
        self._requests: OrderedDict[int, CommandOwnershipSnapshot | str | None] = (
            OrderedDict()
        )
        self._bound: tuple[int, CommandOwnershipSnapshot] | None = None
        self.rejection_counts: Counter[str] = Counter()
        self._last_binding = CommandOwnershipBinding(
            status=CommandOwnershipBindingStatus.NONE
        )

    @property
    def last_binding(self) -> CommandOwnershipBinding:
        """Return the latest staging, binding, or rejection result."""
        return self._last_binding

    @property
    def last_staged_id(self) -> int:
        """Return the identifier of the latest staged request (base - 1 when none)."""
        return self._next_id - 1

    def stage(
        self,
        snapshot: CommandOwnershipSnapshot | None,
        requested_power: float,
    ) -> int:
        """Register the request about to be submitted and return its identifier."""
        request_id = self._next_id
        self._next_id += 1
        self._requests[request_id] = snapshot
        while len(self._requests) > MAX_TRACKED_REQUESTS:
            self._requests.popitem(last=False)
        self._last_binding = CommandOwnershipBinding(
            status=CommandOwnershipBindingStatus.PENDING,
            request_id=request_id,
            snapshot=snapshot,
            requested_power=float(requested_power),
        )
        return request_id

    def bind_receipt(self, receipt: AppliedCommandReceipt) -> CommandOwnershipBinding:
        """Bind one applied-command receipt to the snapshot of its request."""
        request_id = receipt.request_id
        if request_id is None:
            entry: CommandOwnershipSnapshot | str | None = "ownership_request_untagged"
        elif request_id in self._requests:
            entry = self._requests[request_id]
        elif self._bound is not None and self._bound[0] == request_id:
            entry = self._bound[1]
        else:
            entry = "ownership_request_unknown"

        alterations = receipt.alterations
        if isinstance(entry, str):
            reason: str | None = entry
        elif entry is None:
            reason = "ownership_request_missing"
        elif entry.hvac_mode != str(receipt.hvac_mode):
            reason = "ownership_context_changed"
        elif CommandAlteration.VALVE_RETAINED in alterations:
            reason = "ownership_actuator_retained"
        elif (
            CommandAlteration.MIN_ON_OFF in alterations
            or "timing" in entry.constraint_flags
        ):
            reason = "scheduler_timing"
        else:
            reason = None

        if reason is None:
            # A pending switch request is applied by the automatic restart
            # (is_repeat), so reuse is judged on the identifier.
            status = (
                CommandOwnershipBindingStatus.REUSED
                if self._bound is not None and self._bound[0] == request_id
                else CommandOwnershipBindingStatus.BOUND
            )
            self._bound = (request_id, entry)
        else:
            self._bound = None
            if request_id in self._requests:
                # A rejected request stays rejected for all its later receipts.
                self._requests[request_id] = reason
            self.rejection_counts[reason] += 1
            status = CommandOwnershipBindingStatus.REJECTED

        self._last_binding = CommandOwnershipBinding(
            status=status,
            request_id=request_id,
            snapshot=entry if isinstance(entry, CommandOwnershipSnapshot) else None,
            reason=reason,
            kind=str(receipt.kind),
            requested_power=float(receipt.requested_on_percent),
            realized_power=float(receipt.applied_on_percent),
            realized_on_time_sec=int(receipt.on_time_sec),
            realized_off_time_sec=int(receipt.off_time_sec),
            alterations=tuple(sorted(str(item) for item in alterations)),
        )
        return self._last_binding

    def reset(self, reason: str | None = None) -> None:
        """Forget all requests; identifiers keep increasing."""
        self._requests.clear()
        self._bound = None
        self._last_binding = CommandOwnershipBinding(
            status=(
                CommandOwnershipBindingStatus.REJECTED
                if reason is not None
                else CommandOwnershipBindingStatus.NONE
            ),
            reason=reason,
        )
