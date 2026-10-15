"""Unit tests for the receipt-driven command ownership tracker."""

import pytest

from custom_components.vtherm_smartpi.smartpi.command_ownership import (
    MAX_TRACKED_REQUESTS,
    CommandOwnershipBindingStatus,
    CommandOwnershipSnapshot,
    CommandOwnershipTracker,
)
from helpers import make_receipt


def _snapshot(
    *,
    hvac_mode: str = "heat",
    u_i: float = 0.09,
    constraint_flags: tuple[str, ...] = (),
) -> CommandOwnershipSnapshot:
    """Build one frozen control decomposition."""
    return CommandOwnershipSnapshot(
        hvac_mode=hvac_mode,
        u_ff1=0.3,
        trim_stored=0.01,
        u_ff_visible=0.31,
        u_ff3=0.0,
        u_p=0.1,
        u_i=u_i,
        ki=0.02,
        gain_generation=4,
        u_cmd=0.5,
        u_limited=0.5,
        regime="dead_band",
        i_mode="I:FREEZE(deadband)",
        constraint_flags=constraint_flags,
    )


def test_stage_returns_strictly_increasing_identifiers() -> None:
    tracker = CommandOwnershipTracker()
    base = tracker.last_staged_id
    first = tracker.stage(_snapshot(), 0.5)
    second = tracker.stage(_snapshot(), 0.5)

    assert base < first < second
    assert tracker.last_staged_id == second
    assert tracker.last_binding.status == CommandOwnershipBindingStatus.PENDING
    assert tracker.last_binding.request_id == second
    assert tracker.last_binding.requested_power == 0.5


def test_identifiers_of_a_previous_instance_are_unknown() -> None:
    old = CommandOwnershipTracker()
    old_id = old.stage(_snapshot(), 0.5)
    tracker = CommandOwnershipTracker()
    tracker.stage(_snapshot(), 0.5)

    assert tracker.last_staged_id > old_id
    binding = tracker.bind_receipt(make_receipt(old_id, 0.5))
    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_request_unknown"


def test_known_request_binds_its_frozen_snapshot() -> None:
    tracker = CommandOwnershipTracker()
    snapshot = _snapshot(u_i=0.07)
    request_id = tracker.stage(snapshot, 0.132)

    binding = tracker.bind_receipt(make_receipt(request_id, 0.125, requested=0.132))

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.owned_snapshot is snapshot
    assert binding.request_id == request_id
    assert binding.kind == "cycle_start"
    assert binding.requested_power == 0.132
    assert binding.realized_power == 0.125
    assert binding.realized_on_time_sec == 15
    assert binding.realized_off_time_sec == 105


def test_automatic_repeat_is_reused_with_the_same_snapshot() -> None:
    tracker = CommandOwnershipTracker()
    snapshot = _snapshot()
    request_id = tracker.stage(snapshot, 0.5)
    tracker.bind_receipt(make_receipt(request_id, 0.5))

    binding = tracker.bind_receipt(make_receipt(request_id, 0.5, is_repeat=True))

    assert binding.status == CommandOwnershipBindingStatus.REUSED
    assert binding.owned_snapshot is snapshot


def test_pending_request_applied_by_the_restart_is_bound() -> None:
    """VT applies a pending switch request at the automatic restart (is_repeat)."""
    tracker = CommandOwnershipTracker()
    first_id = tracker.stage(_snapshot(), 0.5)
    tracker.bind_receipt(make_receipt(first_id, 0.5))
    pending = _snapshot(u_i=0.2)
    pending_id = tracker.stage(pending, 0.6)

    binding = tracker.bind_receipt(make_receipt(pending_id, 0.6, is_repeat=True))

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.owned_snapshot is pending


def test_older_pending_request_binds_its_own_snapshot() -> None:
    """A scheduler applying an older request attributes it to that request."""
    tracker = CommandOwnershipTracker()
    older = _snapshot(u_i=0.01)
    older_id = tracker.stage(older, 0.4)
    tracker.stage(_snapshot(u_i=0.02), 0.5)

    binding = tracker.bind_receipt(make_receipt(older_id, 0.4))

    assert binding.status == CommandOwnershipBindingStatus.BOUND
    assert binding.owned_snapshot is older


def test_untagged_and_unknown_receipts_are_rejected() -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(), 0.5)

    untagged = tracker.bind_receipt(make_receipt(None, 0.5, kind="update"))
    assert untagged.status == CommandOwnershipBindingStatus.REJECTED
    assert untagged.reason == "ownership_request_untagged"
    assert untagged.owned_snapshot is None

    unknown = tracker.bind_receipt(make_receipt(request_id + 100, 0.5))
    assert unknown.reason == "ownership_request_unknown"
    assert tracker.rejection_counts == {
        "ownership_request_untagged": 1,
        "ownership_request_unknown": 1,
    }


def test_request_without_decomposition_is_rejected() -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(None, 0.5)

    binding = tracker.bind_receipt(make_receipt(request_id, 0.5))

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_request_missing"


def test_hvac_mode_change_is_rejected() -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(hvac_mode="heat"), 0.5)

    binding = tracker.bind_receipt(make_receipt(request_id, 0.5, hvac_mode="cool"))

    assert binding.reason == "ownership_context_changed"
    assert binding.owned_snapshot is None


@pytest.mark.parametrize(
    ("alterations", "flags", "reason"),
    [
        (("min_on_off",), (), "scheduler_timing"),
        ((), ("timing",), "scheduler_timing"),
        (("valve_retained",), (), "ownership_actuator_retained"),
    ],
)
def test_alterations_and_timing_flag_are_rejected(alterations, flags, reason) -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(constraint_flags=flags), 0.02)

    binding = tracker.bind_receipt(make_receipt(request_id, 0.0, alterations=alterations))

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == reason
    assert binding.alterations == alterations


def test_rejection_is_sticky_per_request() -> None:
    """A clean repeat of an altered request stays rejected."""
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(), 0.13)
    tracker.bind_receipt(
        make_receipt(request_id, 0.10, kind="update", alterations=("valve_retained",))
    )

    binding = tracker.bind_receipt(make_receipt(request_id, 0.10, is_repeat=True))

    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_actuator_retained"


def test_bound_request_survives_eviction_of_the_bounded_map() -> None:
    tracker = CommandOwnershipTracker()
    snapshot = _snapshot()
    request_id = tracker.stage(snapshot, 0.5)
    tracker.bind_receipt(make_receipt(request_id, 0.5))
    for _ in range(MAX_TRACKED_REQUESTS + 5):
        tracker.stage(_snapshot(), 0.5)

    binding = tracker.bind_receipt(make_receipt(request_id, 0.5, is_repeat=True))

    assert binding.status == CommandOwnershipBindingStatus.REUSED
    assert binding.owned_snapshot is snapshot


def test_evicted_unbound_request_is_unknown() -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(), 0.5)
    for _ in range(MAX_TRACKED_REQUESTS):
        tracker.stage(_snapshot(), 0.5)

    assert tracker.bind_receipt(make_receipt(request_id, 0.5)).reason == (
        "ownership_request_unknown"
    )


def test_reset_forgets_requests_but_identifiers_keep_increasing() -> None:
    tracker = CommandOwnershipTracker()
    request_id = tracker.stage(_snapshot(), 0.5)
    tracker.bind_receipt(make_receipt(request_id, 0.5))

    tracker.reset("ownership_discontinuity")

    assert tracker.last_binding.status == CommandOwnershipBindingStatus.REJECTED
    assert tracker.last_binding.reason == "ownership_discontinuity"
    assert tracker.bind_receipt(make_receipt(request_id, 0.5, is_repeat=True)).reason == (
        "ownership_request_unknown"
    )
    assert tracker.stage(_snapshot(), 0.5) > request_id
    tracker.reset()
    assert tracker.last_binding.status == CommandOwnershipBindingStatus.NONE
