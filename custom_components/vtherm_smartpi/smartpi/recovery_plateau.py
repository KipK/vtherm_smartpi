"""Plateau detection for a guarded recovery that no longer progresses."""
from __future__ import annotations

from collections import deque

from .const import (
    INTEGRAL_GUARD_PLATEAU_BLOCK_CYCLES,
    INTEGRAL_GUARD_PLATEAU_BLOCKS,
    INTEGRAL_GUARD_PLATEAU_PROGRESS_BLOCKS,
    INTEGRAL_GUARD_PLATEAU_PROGRESS_MIN_C,
    INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H,
)


def plateau_block_min(cycle_min: float) -> float:
    """Return the block length (minutes) of the plateau test for a PWM cycle."""
    return max(float(cycle_min), 0.0) * INTEGRAL_GUARD_PLATEAU_BLOCK_CYCLES


def plateau_tolerance_c(block_min: float) -> float:
    """Return the largest block-to-block change of the mean error on a plateau.

    It is the release slope floor applied over one block: a mean error that
    moves less than this per block neither recovers nor diverges.
    """
    return INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H * max(float(block_min), 0.0) / 60.0


class RecoveryPlateauDetector:
    """Detect a recovery stuck on a plateau above its release error.

    The signed error (positive toward the demand) is averaged over consecutive
    blocks of ``block_min`` minutes. Comparing block means rather than
    instantaneous slopes keeps the test valid when the room follows a limit
    cycle around a quantized measurement or an actuator step. A block is
    stationary when its mean stays above ``floor_c`` and differs from the mean
    of the previous block by at most ``tolerance_c``: the room neither
    recovers nor diverges. ``INTEGRAL_GUARD_PLATEAU_BLOCKS`` consecutive
    stationary blocks make a plateau. A transient disturbance (the error rises
    then falls) never gives stationary blocks.

    A slow recovery tail measured by a quantized sensor also gives flat block
    means for a while (the reading stays on one step). The plateau therefore
    also needs no net progress over ``INTEGRAL_GUARD_PLATEAU_PROGRESS_BLOCKS``
    blocks: the current mean must not have fallen by more than
    ``INTEGRAL_GUARD_PLATEAU_PROGRESS_MIN_C`` below the mean of that many
    blocks earlier.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the running block and the block history."""
        self._sum = 0.0
        self._duration_min = 0.0
        self._last_mean: float | None = None
        self._stationary_blocks = 0
        self._means: deque[float] = deque(maxlen=INTEGRAL_GUARD_PLATEAU_PROGRESS_BLOCKS + 1)
        self._detected = False

    @property
    def detected(self) -> bool:
        """Return True once the recovery sits on a plateau."""
        return self._detected

    def update(
        self,
        *,
        error: float,
        dt_min: float,
        block_min: float,
        floor_c: float,
    ) -> bool:
        """Accumulate one error sample and return whether a plateau is detected."""
        if block_min <= 0.0 or dt_min <= 0.0:
            return self.detected
        self._sum += float(error) * float(dt_min)
        self._duration_min += float(dt_min)
        if self._duration_min < block_min:
            return self.detected
        mean = self._sum / self._duration_min
        self._sum = 0.0
        self._duration_min = 0.0
        previous = self._last_mean
        self._last_mean = mean
        self._means.append(mean)
        if (
            previous is not None
            and mean > floor_c
            and abs(mean - previous) <= plateau_tolerance_c(block_min)
        ):
            self._stationary_blocks += 1
        else:
            self._stationary_blocks = 0
        no_net_progress = (
            len(self._means) == self._means.maxlen
            and mean - self._means[0] > -INTEGRAL_GUARD_PLATEAU_PROGRESS_MIN_C
        )
        self._detected = self._stationary_blocks >= INTEGRAL_GUARD_PLATEAU_BLOCKS and no_net_progress
        return self._detected
