"""Step of the temperature sensor, read on its recent samples."""
from __future__ import annotations

from typing import Iterable

SENSOR_STEP_WINDOW_S = 3600.0
SENSOR_STEP_FLOOR_C = 0.01


def sensor_step(history: Iterable[tuple[float, float]]) -> float:
    """Smallest change between two successive samples of the last hour of ``history`` ((time s, value) pairs, oldest
    first): the step of the sensor, ``SENSOR_STEP_FLOOR_C`` at least (also without any change)."""
    samples = list(history or [])
    if not samples:
        return SENSOR_STEP_FLOOR_C
    t_last = samples[-1][0]
    recent = [v for t, v in samples if t >= t_last - SENSOR_STEP_WINDOW_S]
    steps = [abs(b - a) for a, b in zip(recent, recent[1:]) if b != a]
    return max(min(steps), SENSOR_STEP_FLOOR_C) if steps else SENSOR_STEP_FLOOR_C
