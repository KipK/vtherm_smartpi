"""Quiet periods: stretches without sun, at a constant setpoint and with valid data.

Used by the room model, which needs a room free of solar gains. Pure
Python: no Home Assistant, numpy or scipy import; the solar position comes from
the ``astral`` package.
"""
from __future__ import annotations

from .selection import (
    WHY_SHORT,
    build_episodes,
    covered,
    intersect,
    local_iso,
    night_id,
    plateaus,
    setback_runs,
    summary,
    valid_spans,
)
from .solar import Position, astral_available, dark_intervals, sun_elevation, validate_position

__all__ = [
    "Position",
    "WHY_SHORT",
    "astral_available",
    "build_episodes",
    "covered",
    "dark_intervals",
    "intersect",
    "local_iso",
    "night_id",
    "plateaus",
    "setback_runs",
    "summary",
    "sun_elevation",
    "valid_spans",
    "validate_position",
]
