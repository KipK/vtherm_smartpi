"""Publication rules of the T_ar learning (pure Python).

- Fast value: the morning-step publication over the accepted and confirmed
  mornings (at least 3, median of the last 5, MAD / median <= 25 %).
- Lower bound: the median of the last 5 confirmed comfort steps cut by sunrise
  too early for an estimate; each gives TAR_CUT_BOUND_FACTOR x its fitted value.
  A bound never enters the fast value.
- Refined value: the emitter time constant tau_e of the multi-day
  identification, published only from evaluations whose six nights are
  regulated; any other evaluation is a diagnostic.

The regulation does not read these values: its gains read the dead time after a
start, its integral holds the response model (smartpi/gains.py). The refined value splits the emitter of the
room model when no response model is published (room_model/manager.py).
"""
from __future__ import annotations

import statistics

from .const import PUB_LAST, PUB_MAD_REL, PUB_MIN_ACCEPTED


def fast_publication(values: list[float]) -> float | None:
    """Value published after the accepted mornings `values` (oldest first), None if not published."""
    if len(values) < PUB_MIN_ACCEPTED:
        return None
    last = values[-PUB_LAST:]
    m = statistics.median(last)
    mad = statistics.median([abs(x - m) for x in last])
    if m > 0 and mad / m <= PUB_MAD_REL:
        return m
    return None


def bound_publication(values: list[float]) -> float | None:
    """Lower bound in force after the confirmed cut comfort steps `values` (oldest first), None without any."""
    last = [v for v in values[-PUB_LAST:] if v > 0.0]
    return float(statistics.median(last)) if last else None
