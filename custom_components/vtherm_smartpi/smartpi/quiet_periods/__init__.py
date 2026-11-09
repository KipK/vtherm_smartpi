"""Quiet periods: stretches without sun, at a constant setpoint and with valid data.

Shared by the learning methods that need a room free of solar gains. Pure
Python: no Home Assistant, numpy or scipy import; the solar position comes from
the ``astral`` package.
"""
from __future__ import annotations

from .selection import (
    WHY_GAP,
    WHY_NO_POSITION,
    WHY_NO_REGULATED_START,
    WHY_REGULATED_SHORT,
    WHY_SETPOINT,
    WHY_SHORT,
    build_episodes,
    covered,
    episode_of_rise,
    intersect,
    local_iso,
    night_id,
    plateaus,
    quiet_intervals,
    regulated_span,
    setback_runs,
    step_component,
    summary,
    valid_spans,
)
from .solar import Position, astral_available, dark_intervals, sun_elevation, validate_position

__all__ = [
    "Position",
    "WHY_GAP",
    "WHY_NO_POSITION",
    "WHY_NO_REGULATED_START",
    "WHY_REGULATED_SHORT",
    "WHY_SETPOINT",
    "WHY_SHORT",
    "astral_available",
    "build_episodes",
    "covered",
    "dark_intervals",
    "episode_of_rise",
    "intersect",
    "local_iso",
    "night_id",
    "plateaus",
    "quiet_intervals",
    "regulated_span",
    "setback_runs",
    "step_component",
    "summary",
    "sun_elevation",
    "valid_spans",
    "validate_position",
]
