"""Local clock: days and hours of the room's local time.

The learning methods use it only for the dates shown in their diagnostics,
and the energy balance of the room model for its whole local days. Times are
seconds on one axis (epoch seconds in the integration). A day is an integer index; ``at(day, hour)``
is the instant of that local hour of that day (hours may exceed 24 or be
negative), ``day_of(t)`` the day containing ``t``.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from .const import DAY, H


class LocalClock:
    """Local time of a time zone, on the epoch axis."""

    def __init__(self, tz: dt.tzinfo | str | None) -> None:
        if isinstance(tz, str):
            tz = ZoneInfo(tz)
        self._tz = tz or dt.timezone.utc

    def _local(self, t: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(t, self._tz)

    def tod_h(self, t: float) -> float:
        """Local hour of day of t, in [0, 24)."""
        d = self._local(t)
        return d.hour + d.minute / 60.0 + (d.second + d.microsecond * 1e-6) / 3600.0

    def tod_h_array(self, ts) -> list[float]:
        """Local hour of day of every time of ts."""
        return [self.tod_h(float(x)) for x in ts]

    def day_of(self, t: float) -> int:
        """Local date ordinal of t."""
        return self._local(t).date().toordinal()

    def day_label(self, day: int | None) -> str | None:
        """Local date of the day `day`, as YYYY-MM-DD."""
        return None if day is None else dt.date.fromordinal(int(day)).isoformat()

    def at(self, day: int, hour: float) -> float:
        """Epoch time of the local hour `hour` of the day `day` (date ordinal)."""
        extra_days = math.floor(hour / 24.0)
        h = hour - 24.0 * extra_days
        date = dt.date.fromordinal(day + extra_days)
        whole = int(math.floor(h))
        minutes = (h - whole) * 60.0
        local = dt.datetime.combine(date, dt.time(whole, 0), tzinfo=self._tz) + dt.timedelta(minutes=minutes)
        return local.timestamp()


class OffsetClock:
    """Clock of a time axis without time zone: t = 0 is the local hour `origin_h` of day 0."""

    def __init__(self, origin_h: float = 0.0) -> None:
        self._origin_h = float(origin_h)

    def tod_h(self, t: float) -> float:
        return (self._origin_h + t / H) % 24.0

    def tod_h_array(self, ts):
        import numpy as np  # pylint: disable=import-outside-toplevel

        return (self._origin_h + np.asarray(ts, dtype=float) / H) % 24.0

    def day_of(self, t: float) -> int:
        return int(math.floor((t + self._origin_h * H) / DAY))

    def day_label(self, day: int | None) -> int | None:
        """Day index: this axis has no calendar."""
        return None if day is None else int(day)

    def at(self, day: int, hour: float) -> float:
        return day * DAY + (hour - self._origin_h) * H
