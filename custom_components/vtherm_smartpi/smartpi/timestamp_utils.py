"""Timestamp conversion utilities for Smart-PI persistence.

This module provides utilities for converting between monotonic timestamps
(used for internal timing) and wall clock timestamps (used for persistence).
"""

import time
from math import isfinite


def normalize_wall_timestamp(value: object) -> float | None:
    """Return a finite positive wall-clock timestamp or None."""
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(timestamp) or timestamp <= 0.0:
        return None
    return timestamp


def elapsed_wall_minutes(
    since_wall_ts: object,
    now_wall_ts: object | None = None,
) -> float | None:
    """Return non-negative elapsed wall-clock minutes from a persisted timestamp."""
    since = normalize_wall_timestamp(since_wall_ts)
    now = normalize_wall_timestamp(time.time() if now_wall_ts is None else now_wall_ts)
    if since is None or now is None:
        return None
    return max(now - since, 0.0) / 60.0


def convert_monotonic_to_wall_ts(monotonic_ts: float | None) -> float | None:
    """Convert a monotonic timestamp to wall clock time for persistence.

    Args:
        monotonic_ts: Monotonic timestamp or None

    Returns:
        Wall clock timestamp (time.time()) or None if already None or expired
    """
    if monotonic_ts is None:
        return None
    remaining = monotonic_ts - time.monotonic()
    if remaining > 0:
        return time.time() + remaining
    return None


def convert_wall_to_monotonic_ts(wall_ts: float | None) -> float | None:
    """Convert a wall clock timestamp to monotonic timestamp.

    Args:
        wall_ts: Wall clock timestamp (time.time()) or None

    Returns:
        Monotonic timestamp or None if already None or expired
    """
    if wall_ts is None:
        return None
    delay = wall_ts - time.time()
    if delay > 0:
        return time.monotonic() + delay
    return None
