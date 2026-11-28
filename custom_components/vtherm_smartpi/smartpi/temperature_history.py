"""Recent room temperatures, raw and filtered, read by the learning.

``raw`` holds the measured temperature: the learning slopes, the anticipation, the pulses and the calibration read it
(their robustness rules count the value changes of the sensor, which a filtered signal would multiply). ``filtered``
holds the low-pass temperature of the dead-time detection. A sample is added when the value changed or 60 seconds
elapsed; both keep the samples of the last ``TIN_HISTORY_RETENTION_MIN`` minutes, ``TIN_HISTORY_MAX_SAMPLES`` at most.
Times are monotonic seconds; the saved form is in wall-clock time.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Tuple

from .const import TIN_HISTORY_MAX_SAMPLES, TIN_HISTORY_RETENTION_MIN
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

_RETENTION_S = TIN_HISTORY_RETENTION_MIN * 60.0


class TemperatureHistory:
    """The raw and the filtered room temperature of the last hours, as (monotonic time, value) samples."""

    def __init__(self) -> None:
        self.raw: Deque[Tuple[float, float]] = deque(maxlen=TIN_HISTORY_MAX_SAMPLES)
        self.filtered: Deque[Tuple[float, float]] = deque(maxlen=TIN_HISTORY_MAX_SAMPLES)

    @staticmethod
    def _append(history: Deque[Tuple[float, float]], now: float, value: float) -> None:
        """Append a sample when the value changed or 60 seconds elapsed; drop the samples past the retention.

        The history spans the retention as long as the sample cap is not reached, i.e. while samples arrive on
        average at most every 10 s. Unchanged values within a minute are not repeated (high-frequency heartbeats),
        so the changes keep their own times.
        """
        if not history or abs(value - history[-1][1]) > 0.001 or now - history[-1][0] >= 60.0:
            history.append((now, value))
        oldest_kept = now - _RETENTION_S
        while history and history[0][0] < oldest_kept:
            history.popleft()

    def append(self, now: float, filtered: float, raw: float | None = None) -> None:
        """One measurement: the filtered value and the raw one (the filtered value when not given)."""
        self._append(self.raw, now, filtered if raw is None else raw)
        self._append(self.filtered, now, filtered)

    def clear(self) -> None:
        self.raw.clear()
        self.filtered.clear()

    def save(self) -> dict:
        """Both histories in wall-clock time, under their saved keys."""
        return {
            "tin_history": [(monotonic_to_wall_ts(t), v) for t, v in self.raw],
            "tin_lp_history": [(monotonic_to_wall_ts(t), v) for t, v in self.filtered],
        }

    def load(self, state: dict) -> None:
        """Restore what ``save`` wrote (a missing history stays empty)."""
        for key, history in (("tin_history", self.raw), ("tin_lp_history", self.filtered)):
            history.clear()
            for t_wall, v in state.get(key, []):
                t_mono = wall_to_monotonic_ts(t_wall)
                if t_mono is not None:
                    history.append((t_mono, v))
