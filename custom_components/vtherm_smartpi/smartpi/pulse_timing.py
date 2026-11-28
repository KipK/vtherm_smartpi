"""When a measurement pulse may start: the quiet window.

A pulse holds the room away from its setpoint for hours, so it starts only when nothing else is expected to move the
setpoint: at night (darkness at the home position, smartpi/quiet_periods/solar.py), after the setpoint and the HVAC
mode have been left alone for ``QUIET_STABLE_S``, and, once the setpoint log covers the two previous days, when the
setpoint did not change in the same hours of those two days (a scheduled change repeats from one day to the next). The
hours looked at after the start last ``pulse_window_s``: six emitter lags, within ``WINDOW_MIN_S`` and ``WINDOW_MAX_S``
(three hours when the lag is unknown). All times are wall-clock epoch seconds.

``SetpointLog`` keeps the wall times of the setpoint and mode changes of the last ``LOG_KEEP_S``, bounded in size, and
the time it started to observe. It is saved with the state of the algorithm; a state saved without it starts a new log.
"""
from __future__ import annotations

from typing import Any, Iterable

from .quiet_periods.solar import dark_intervals
from .timestamp_utils import normalize_wall_timestamp

QUIET_STABLE_S = 3600.0              # the setpoint and the mode must have been unchanged this long
QUIET_DAYS = 2                       # previous days whose same hours must hold no setpoint change
WINDOW_MIN_S = 3.0 * 3600.0          # hours looked at after the start: six emitter lags within these bounds
WINDOW_MAX_S = 6.0 * 3600.0
WINDOW_LAG_FACTOR = 6.0
DAY_S = 86400.0
LOG_KEEP_S = 72.0 * 3600.0           # changes kept in the setpoint log
LOG_MAX = 256                        # and at most this many
LOG_GAP_S = 3600.0                   # no observation for this long: a change may have been missed, noted as one
_SAME = 1e-6                         # setpoints closer than this are the same


def pulse_window_s(lag_s: float | None) -> float:
    """Duration kept free of setpoint changes after the start: six emitter lags, within three to six hours."""
    lag = lag_s if isinstance(lag_s, (int, float)) and not isinstance(lag_s, bool) and lag_s > 0.0 else 0.0
    return min(max(WINDOW_LAG_FACTOR * lag, WINDOW_MIN_S), WINDOW_MAX_S)


class SetpointLog:
    """Wall times of the changes of the setpoint or of the HVAC mode over the last 72 hours."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.since: float | None = None          # when the observation began
        self.changes: list[float] = []
        self._target: float | None = None
        self._mode: str | None = None
        self._seen: float | None = None          # wall time of the last observation

    def note(self, wall: float, target: float | None, mode: object) -> None:
        """Once per calculation: record a change of ``target`` or of ``mode`` at ``wall``. A long gap since the last
        observation counts as a change (the setpoint may have moved meanwhile)."""
        if target is None:
            return
        mode = None if mode is None else str(mode)
        if self.since is None:
            self.since = wall
        else:
            gap = self._seen is not None and wall - self._seen > LOG_GAP_S
            moved = self._target is None or abs(float(target) - self._target) > _SAME or mode != self._mode
            if (gap or moved) and (not self.changes or self.changes[-1] < wall):
                self.changes.append(wall)
        self._target, self._mode, self._seen = float(target), mode, wall
        self.changes = [c for c in self.changes if c >= wall - LOG_KEEP_S][-LOG_MAX:]

    @property
    def last_change(self) -> float | None:
        """Wall time of the last change, the start of the observation when there is none."""
        if self.since is None:
            return None
        return max([self.since, *self.changes])

    def save(self) -> dict[str, Any] | None:
        if self.since is None:
            return None
        return {"since": self.since, "changes": list(self.changes), "target": self._target, "mode": self._mode,
                "seen": self._seen}

    def load(self, saved: object) -> None:
        """Restore what ``save`` wrote; anything else starts a new log."""
        self.reset()
        if not isinstance(saved, dict):
            return
        since = normalize_wall_timestamp(saved.get("since"))
        if since is None:
            return
        self.since = since
        changes = saved.get("changes")
        self.changes = sorted(c for c in (normalize_wall_timestamp(v) for v in (changes if isinstance(changes, list) else []))
                              if c is not None)[-LOG_MAX:]
        target = saved.get("target")
        self._target = float(target) if isinstance(target, (int, float)) and not isinstance(target, bool) else None
        self._mode = saved["mode"] if isinstance(saved.get("mode"), str) else None
        self._seen = normalize_wall_timestamp(saved.get("seen"))


def is_dark(position: Any, wall: float) -> bool:
    """True when it is certainly dark at ``position`` at ``wall``."""
    return any(a <= wall < b for a, b in dark_intervals(position, wall, wall + 1.0))


def quiet_window(
    wall: float, position: Any, log: SetpointLog, lag_s: float | None = None, require_history: bool = False,
) -> bool:
    """True when a pulse may start at ``wall``: it is dark at ``position``, the setpoint and the mode have not changed
    for ``QUIET_STABLE_S``, and, when the log covers the ``QUIET_DAYS`` previous days, none changed in the hours
    ``[wall - d days, wall - d days + pulse_window_s(lag_s)]`` of those days. Without that coverage the prediction is
    skipped, or the window is refused when ``require_history``."""
    if position is None or log.since is None or not is_dark(position, wall):
        return False
    if wall - log.last_change < QUIET_STABLE_S:
        return False
    if wall - log.since < QUIET_DAYS * DAY_S:
        return not require_history
    window = pulse_window_s(lag_s)
    return not any(
        _changed_between(log.changes, wall - d * DAY_S, wall - d * DAY_S + window) for d in range(1, QUIET_DAYS + 1))


def _changed_between(changes: Iterable[float], start: float, end: float) -> bool:
    return any(start <= c <= end for c in changes)
