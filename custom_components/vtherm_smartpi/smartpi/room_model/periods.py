"""Periods used to identify the room model (pure Python).

This module is the single place that decides which recorded periods the
identification fits. Today a period is a local night (NIGHT_FROM_H to
NIGHT_TO_H, no sun) of the identification window, cut by the invalid
intervals of the recorded history, without the nights in which the observer
detected heat gains or losses. Each period gets its own initial air and wall
state in the fit.
"""
from __future__ import annotations

from ..tar_learning.const import ID_WIN_DAYS, ID_WINDOW_END_H, NIGHT_FROM_H, NIGHT_TO_H
from .const import RM_MIN_SEGMENT_S


def window_bounds(clock, day: int) -> tuple[float, float]:
    """Identification window of a day: ID_WIN_DAYS days ending at ID_WINDOW_END_H local."""
    return clock.at(day - ID_WIN_DAYS, ID_WINDOW_END_H), clock.at(day, ID_WINDOW_END_H)


def _subtract(a: float, b: float, invalid: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Parts of [a, b) outside the invalid intervals."""
    parts = [(a, b)]
    for g0, g1 in sorted(invalid):
        nxt = []
        for x, y in parts:
            if g1 <= x or g0 >= y:
                nxt.append((x, y))
                continue
            if g0 > x:
                nxt.append((x, g0))
            if g1 < y:
                nxt.append((g1, y))
        parts = nxt
    return parts


def fit_periods(clock, day: int, first_t: float, last_t: float, invalid: list[tuple[float, float]],
                excluded_nights: list[int] | tuple = ()) -> list[tuple[float, float, int]]:
    """Fit periods (start, end, night) of the window of `day`, at least RM_MIN_SEGMENT_S long."""
    out = []
    excluded = set(int(n) for n in excluded_nights)
    for night in range(day - ID_WIN_DAYS, day):
        if night in excluded:
            continue
        a = max(clock.at(night, NIGHT_FROM_H), first_t)
        b = min(clock.at(night + 1, NIGHT_TO_H), last_t)
        if b <= a:
            continue
        for x, y in _subtract(a, b, invalid):
            if y - x >= RM_MIN_SEGMENT_S:
                out.append((x, y, night))
    return out
