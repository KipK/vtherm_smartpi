"""Search of the temperature peak that follows a cut of the heating power.

After the power falls to zero the room keeps moving in the direction of the heating (rising in HEAT, falling in
COOL) until the emitter and the room have delivered what was commanded. The extreme is found on bins of
``HEATING_BALANCE_PEAK_BIN_S``: a bin is closed by the first sample of a later bin, its value is the mean of its
samples, empty bins are skipped. The best bin is the earliest closed bin with the strictly highest mean (of
``sign * temperature``). The peak is confirmed when a closed bin lies ``HEATING_BALANCE_PEAK_CONFIRM_S`` or more after
the best one. It is at the middle of the best bin: ``peak_delay_s`` after the cut.

The caller reads the outcome:

- ``PEAK_CONFIRMED``: ``feed`` returned True;
- ``PEAK_CUT_SHORT``: the heating starts again before the confirmation (``cut_short``) and the best bin is not the
  last closed one, so the temperature was falling away from it;
- ``PEAK_NONE``: no peak (the best bin is the last one, no sample, or ``expired``: no confirmation within
  ``HEATING_BALANCE_PEAK_MAX_S`` of the cut).

Each caller applies its own acceptance of the outcomes. The search keeps its bins as plain numbers
(``fields``/``from_fields``) so that a caller can save it and resume it after a restart.

``feed`` takes a ``mark``: a number of the caller (such as an accumulated area) recorded for the best bin.
"""
from __future__ import annotations

from .const import (
    HEATING_BALANCE_PEAK_BIN_S,
    HEATING_BALANCE_PEAK_CONFIRM_S,
    HEATING_BALANCE_PEAK_MAX_S,
)

PEAK_CONFIRMED = "confirmed"
PEAK_CUT_SHORT = "cut short"
PEAK_NONE = "none"

_FIELDS = ("bin_k", "bin_sum", "bin_n", "best_k", "best_mean", "best_mark", "last_k")


class PeakSearch:
    """Peak search after one cut. Times are in the time base of the caller."""

    def __init__(self, t_cut: float, sign: float = 1.0) -> None:
        self.t_cut = float(t_cut)
        # +1 when the extreme looked for is a maximum (HEAT), -1 for a minimum (COOL).
        self.sign = -1.0 if sign < 0 else 1.0
        self.bin_k: int | None = None
        self.bin_sum = 0.0
        self.bin_n = 0
        self.best_k: int | None = None
        self.best_mean: float | None = None  # in the signed scale
        self.best_mark: float | None = None
        self.last_k: int | None = None

    # ------------------------------------------------------------------
    # Samples
    # ------------------------------------------------------------------

    def feed(self, t: float, value: float, mark: float = 0.0) -> bool:
        """Add a temperature sample taken after the cut; True when closing the bin in progress confirms the peak
        (the remaining samples are not used)."""
        v = self.sign * value
        k = max(0, int((t - self.t_cut) // HEATING_BALANCE_PEAK_BIN_S))
        if self.bin_k is None:
            self.bin_k, self.bin_sum, self.bin_n = k, v, 1
        elif k == self.bin_k:
            self.bin_sum += v
            self.bin_n += 1
        else:
            if self._close_bin(mark):
                return True
            self.bin_k, self.bin_sum, self.bin_n = k, v, 1
        return False

    def _close_bin(self, mark: float) -> bool:
        """Close the bin in progress; True when it confirms the best bin as the peak."""
        k = self.bin_k
        mean = self.bin_sum / self.bin_n
        if self.best_mean is None or mean > self.best_mean:
            self.best_k, self.best_mean, self.best_mark = k, mean, mark
        self.last_k = k
        return (k - self.best_k) * HEATING_BALANCE_PEAK_BIN_S >= HEATING_BALANCE_PEAK_CONFIRM_S

    def cut_short(self, mark: float = 0.0) -> str:
        """The heating starts again: close the bin in progress. The outcome is ``PEAK_CUT_SHORT`` when a best bin
        exists that is not the last one, else ``PEAK_NONE``."""
        if self.bin_k is None:
            return PEAK_NONE
        self._close_bin(mark)
        return PEAK_CUT_SHORT if self.best_k != self.last_k else PEAK_NONE

    def expired(self, t: float) -> bool:
        """True when ``t`` is at or beyond the longest wait for the peak."""
        return t - self.t_cut >= HEATING_BALANCE_PEAK_MAX_S

    # ------------------------------------------------------------------
    # Peak
    # ------------------------------------------------------------------

    @property
    def peak_delay_s(self) -> float:
        """Time from the cut to the middle of the best bin (s)."""
        return float((self.best_k + 0.5) * HEATING_BALANCE_PEAK_BIN_S)

    @property
    def peak_value(self) -> float:
        """Mean temperature of the best bin."""
        return self.sign * self.best_mean

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def fields(self) -> dict:
        """The bins as plain numbers."""
        return {key: getattr(self, key) for key in _FIELDS}

    @classmethod
    def from_fields(cls, t_cut: float, sign: float, fields: dict) -> "PeakSearch":
        """A search resumed from ``fields`` (already validated by the caller)."""
        search = cls(t_cut, sign)
        for key in _FIELDS:
            setattr(search, key, fields[key])
        return search
