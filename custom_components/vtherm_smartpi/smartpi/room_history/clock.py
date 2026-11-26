"""Local clock: days and hours of the room's local time.

The energy balance of the room model uses it for its whole local days and the
room model for the dates of its records. Times are seconds on one axis (epoch
seconds in the integration). A day is an integer index; ``at(day, hour)``
is the instant of that local hour of that day (hours may exceed 24 or be
negative), ``day_of(t)`` the day containing ``t``.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo


class LocalClock:
    """Local time of a time zone, on the epoch axis."""

    def __init__(self, tz: dt.tzinfo | str | None) -> None:
        if isinstance(tz, str):
            tz = ZoneInfo(tz)
        self._tz = tz or dt.timezone.utc

    def _local(self, t: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(t, self._tz)

    def day_of(self, t: float) -> int:
        """Local date ordinal of t."""
        return self._local(t).date().toordinal()

    def at(self, day: int, hour: float) -> float:
        """Epoch time of the local hour `hour` of the day `day` (date ordinal)."""
        extra_days = math.floor(hour / 24.0)
        h = hour - 24.0 * extra_days
        date = dt.date.fromordinal(day + extra_days)
        whole = int(math.floor(h))
        minutes = (h - whole) * 60.0
        local = dt.datetime.combine(date, dt.time(whole, 0), tzinfo=self._tz) + dt.timedelta(minutes=minutes)
        return local.timestamp()

