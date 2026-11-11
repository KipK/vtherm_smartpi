"""Periods used to identify the room model (pure Python).

This module is the single place that decides which recorded periods the
identification fits. The periods come from the shared catalogue of quiet
periods (quiet_periods: no sun, constant setpoint, valid data), kept by the
T_ar learning: an identification window holds three closed periods, and each
period is fitted on its identification interval (its quiet part, continued by
the response to the setpoint rise that ends it while it is still dark). The
periods in which the observer detected heat gains or losses are left out, and
each interval is cut by the invalid intervals of the recorded history. Each
piece gets its own initial air and wall state in the fit: two periods are
never joined.
"""
from __future__ import annotations

from ..tar_learning.const import ID_WIN_PERIODS
from .const import RM_MIN_SEGMENT_S


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


def interval(period: dict) -> tuple[float, float]:
    """Identification interval of a catalogue period (its quiet part when the interval is not recorded)."""
    a, b = period.get("ident") or period["quiet"]
    return float(a), float(b)


def windows(periods: list[dict]) -> list[dict]:
    """Identification windows of the closed periods (oldest first): one per period that closes a run of
    ID_WIN_PERIODS (the same windows as the T_ar learning), keyed by that period, with the key of the window
    of the periods just before it (disjoint), None for the first ones."""
    out = []
    n = ID_WIN_PERIODS
    for k in range(n - 1, len(periods)):
        three = periods[k - n + 1: k + 1]
        out.append({"id": periods[k]["id"], "nights": [p["id"] for p in three],
                    "intervals": [list(interval(p)) for p in three],
                    "t_end": interval(periods[k])[1],
                    "prev": periods[k - n]["id"] if k >= 2 * n - 1 else None})
    return out


def fit_periods(window: dict, first_t: float, last_t: float, invalid: list[tuple[float, float]],
                excluded: list[str] | tuple = ()) -> list[tuple[float, float, str]]:
    """Fit periods (start, end, period key) of a window, at least RM_MIN_SEGMENT_S long."""
    out = []
    skip = set(excluded)
    for key, (a, b) in zip(window["nights"], window["intervals"]):
        if key in skip:
            continue
        a, b = max(float(a), first_t), min(float(b), last_t)
        if b <= a:
            continue
        for x, y in _subtract(a, b, invalid):
            if y - x >= RM_MIN_SEGMENT_S:
                out.append((x, y, key))
    return out
