"""Timestamp conversion utilities for Smart-PI persistence.

This module provides utilities for converting between monotonic timestamps
(used for internal timing) and wall clock timestamps (used for persistence).
"""

import time


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
