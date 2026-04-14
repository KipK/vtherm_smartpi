"""Persistent drift helpers for Smart-PI A/B learning."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque
import statistics

from .const import AB_DRIFT_BUFFER_MAXLEN


@dataclass
class DriftCandidate:
    """Rejected candidate kept for persistent-drift analysis."""

    value: float
    seq: int
    side: int


@dataclass
class DriftChannelState:
    """Per-parameter persistent-drift state."""

    state: str = "NORMAL"
    reject_streak: int = 0
    drift_buffer: Deque[DriftCandidate] = field(
        default_factory=lambda: deque(maxlen=AB_DRIFT_BUFFER_MAXLEN)
    )
    recenter_cycles_left: int = 0
    virtual_center: float | None = None
    last_candidate_center: float | None = None
    last_candidate_mad: float | None = None
    last_shift: float | None = None
    last_side: int | None = None
    last_gate_center: float | None = None
    last_reason: str = "init"


@dataclass
class DriftDecision:
    """Decision returned by the drift-aware gate."""

    accepted: bool
    accepted_reason: str
    used_recenter: bool
    gate_center: float | None
    candidate_center: float | None
    candidate_mad: float | None
    shift: float | None
    side: int | None


def robust_median(values: list[float]) -> float | None:
    """Return the median of values or None for an empty list."""

    if not values:
        return None
    try:
        return float(statistics.median(values))
    except statistics.StatisticsError:
        return None


def robust_mad(values: list[float], center: float | None = None) -> float | None:
    """Return the median absolute deviation or None if unavailable."""

    if len(values) < 2:
        return None
    if center is None:
        center = robust_median(values)
    if center is None:
        return None
    try:
        return float(statistics.median(abs(v - center) for v in values))
    except statistics.StatisticsError:
        return None


def sign_nonzero(x: float, eps: float = 1e-12) -> int:
    """Return the sign of x with a dead-zone around zero."""

    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


def clip(x: float, low: float, high: float) -> float:
    """Clamp x into [low, high]."""

    return max(low, min(high, x))


def clear_channel_state(channel: DriftChannelState) -> None:
    """Reset a channel to its nominal state while preserving buffer capacity."""

    channel.state = "NORMAL"
    channel.reject_streak = 0
    channel.drift_buffer.clear()
    channel.recenter_cycles_left = 0
    channel.virtual_center = None
    channel.last_candidate_center = None
    channel.last_candidate_mad = None
    channel.last_shift = None
    channel.last_side = None
    channel.last_gate_center = None
    channel.last_reason = "init"


def serialize_channel_state(channel: DriftChannelState) -> dict[str, Any]:
    """Serialize a drift channel to JSON-friendly primitives."""

    return {
        "state": channel.state,
        "reject_streak": channel.reject_streak,
        "drift_buffer": [
            {"value": c.value, "seq": c.seq, "side": c.side}
            for c in channel.drift_buffer
        ],
        "recenter_cycles_left": channel.recenter_cycles_left,
        "virtual_center": channel.virtual_center,
        "last_candidate_center": channel.last_candidate_center,
        "last_candidate_mad": channel.last_candidate_mad,
        "last_shift": channel.last_shift,
        "last_side": channel.last_side,
        "last_gate_center": channel.last_gate_center,
        "last_reason": channel.last_reason,
    }


def deserialize_channel_state(
    state: dict[str, Any],
    *,
    buffer_maxlen: int,
) -> DriftChannelState:
    """Deserialize a drift channel with best-effort validation."""

    channel = DriftChannelState()
    channel.drift_buffer = deque(maxlen=buffer_maxlen)
    if not isinstance(state, dict):
        return channel

    try:
        channel.state = str(state.get("state", "NORMAL"))
        channel.reject_streak = int(state.get("reject_streak", 0))
        channel.recenter_cycles_left = int(state.get("recenter_cycles_left", 0))
        channel.virtual_center = (
            float(state["virtual_center"])
            if state.get("virtual_center") is not None
            else None
        )
        channel.last_candidate_center = (
            float(state["last_candidate_center"])
            if state.get("last_candidate_center") is not None
            else None
        )
        channel.last_candidate_mad = (
            float(state["last_candidate_mad"])
            if state.get("last_candidate_mad") is not None
            else None
        )
        channel.last_shift = (
            float(state["last_shift"])
            if state.get("last_shift") is not None
            else None
        )
        channel.last_side = (
            int(state["last_side"])
            if state.get("last_side") is not None
            else None
        )
        channel.last_gate_center = (
            float(state["last_gate_center"])
            if state.get("last_gate_center") is not None
            else None
        )
        channel.last_reason = str(state.get("last_reason", "init"))
        for item in state.get("drift_buffer", []):
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            seq = item.get("seq")
            side = item.get("side")
            if value is None or seq is None or side is None:
                continue
            channel.drift_buffer.append(
                DriftCandidate(value=float(value), seq=int(seq), side=int(side))
            )
    except (TypeError, ValueError):
        return DriftChannelState()

    if channel.state not in {"NORMAL", "DRIFT_SUSPECTED", "RECENTERING"}:
        channel.state = "NORMAL"
    return channel


def append_drift_candidate(
    *,
    channel: DriftChannelState,
    value: float,
    seq: int,
    hist_center: float,
) -> None:
    """Append a rejected candidate to the drift buffer."""

    channel.drift_buffer.append(
        DriftCandidate(
            value=value,
            seq=seq,
            side=sign_nonzero(value - hist_center),
        )
    )


def detect_persistent_drift(
    *,
    channel: DriftChannelState,
    hist_center: float,
    hist_mad_eff: float,
    min_count: int,
    max_buffer_mad_factor: float,
    min_shift_factor: float,
    seq_gap_max: int,
) -> tuple[bool, dict[str, Any]]:
    """Return True when the rejected candidates describe a plausible regime shift."""

    values = [candidate.value for candidate in channel.drift_buffer]
    if len(values) < min_count:
        return False, {"reason": "DRIFT_NOT_ENOUGH_CANDIDATES"}

    seqs = [candidate.seq for candidate in channel.drift_buffer]
    for prev, curr in zip(seqs, seqs[1:]):
        if (curr - prev) > seq_gap_max:
            return False, {"reason": "DRIFT_NOT_ENOUGH_CANDIDATES"}

    sides = [sign_nonzero(value - hist_center) for value in values]
    nonzero_sides = [side for side in sides if side != 0]
    if len(nonzero_sides) < min_count or any(side != nonzero_sides[0] for side in nonzero_sides):
        return False, {"reason": "DRIFT_MIXED_SIDES"}

    candidate_center = robust_median(values)
    candidate_mad = robust_mad(values, candidate_center) or 0.0
    if candidate_center is None:
        return False, {"reason": "DRIFT_NOT_ENOUGH_CANDIDATES"}

    channel.last_candidate_center = candidate_center
    channel.last_candidate_mad = candidate_mad
    channel.last_shift = candidate_center - hist_center
    channel.last_side = nonzero_sides[0]

    if candidate_mad > (max_buffer_mad_factor * hist_mad_eff):
        return False, {
            "reason": "DRIFT_BUFFER_TOO_DISPERSED",
            "cand_center": candidate_center,
            "cand_mad": candidate_mad,
            "shift": channel.last_shift,
            "side": channel.last_side,
        }

    if abs(channel.last_shift) < (min_shift_factor * hist_mad_eff):
        return False, {
            "reason": "DRIFT_SHIFT_TOO_SMALL",
            "cand_center": candidate_center,
            "cand_mad": candidate_mad,
            "shift": channel.last_shift,
            "side": channel.last_side,
        }

    return True, {
        "reason": "PERSISTENT_DRIFT_DETECTED",
        "cand_center": candidate_center,
        "cand_mad": candidate_mad,
        "shift": channel.last_shift,
        "side": channel.last_side,
    }


def compute_recenter_virtual_center(
    *,
    current_center: float,
    candidate_center: float,
    hist_mad_eff: float,
    alpha: float,
    step_max_factor: float,
) -> float:
    """Return a bounded virtual center used during recentering."""

    step_raw = alpha * (candidate_center - current_center)
    step_max = step_max_factor * hist_mad_eff
    step = clip(step_raw, -step_max, step_max)
    return current_center + step
