"""
Bounded counters of a/b learning window outcomes for diagnostics.

``learn_last_reason`` only keeps the latest decision, which the next
calculation overwrites. Each closed learning window (or balance pool event)
records one normalised reason here so that the dominant causes of lost
windows stay visible over a sliding period.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from typing import Deque, Tuple

LEARNING_OUTCOMES_PERIOD_S = 86400.0
LEARNING_OUTCOMES_MAX_EVENTS = 1000
LEARNING_OUTCOMES_TOP_N = 8

_PARENTHESES = re.compile(r"\([^)]*\)")
_NUMBERS = re.compile(r"[-+]?\d+(\.\d+)?")
_SPACES = re.compile(r"\s+")


def normalise_reason(reason: str) -> str:
    """Drop the variable parts (values, counts) of a learning reason."""
    text = _NUMBERS.sub("#", _PARENTHESES.sub("", str(reason)))
    return _SPACES.sub(" ", text).strip(" ,")


class LearningOutcomes:
    """Sliding-period counts of learning outcomes per parameter."""

    def __init__(self) -> None:
        self._events: Deque[Tuple[float, str, str]] = deque(
            maxlen=LEARNING_OUTCOMES_MAX_EVENTS
        )

    def record(self, now: float, param: str, reason: str) -> None:
        """Record one outcome for parameter ``param`` ("a" or "b")."""
        self._events.append((now, param, normalise_reason(reason)))

    def clear(self) -> None:
        """Forget every recorded outcome."""
        self._events.clear()

    def summary(self, now: float) -> dict[str, list[list]]:
        """Return the most frequent outcomes of the period, per parameter."""
        while self._events and now - self._events[0][0] > LEARNING_OUTCOMES_PERIOD_S:
            self._events.popleft()
        result: dict[str, list[list]] = {}
        for param in ("a", "b"):
            counts = Counter(r for _, p, r in self._events if p == param)
            result[param] = [
                [reason, count]
                for reason, count in counts.most_common(LEARNING_OUTCOMES_TOP_N)
            ]
        return result
