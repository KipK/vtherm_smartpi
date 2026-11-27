"""Automatic calibration of a mode whose response model still holds only the values carried over by an upgrade.

A thermostat upgraded from an earlier version starts with a response model seeded from its saved dead time (source
``previous version``, no event). When that state has lasted ``AUTO_CALIBRATION_WAIT_S`` in the current mode, SmartPI asks
for one calibration by itself, through the path and the refusal rules of the service (smartpi/learning_start.py
``request_calibration``), at a quiet window (smartpi/pulse_timing.py, the two-day history required) and at most
``AUTO_CALIBRATION_ATTEMPTS`` times per mode, ``AUTO_CALIBRATION_RETRY_S`` apart. It stops for good once the mode has a
measured event, and does nothing without a known home position. Wall-clock epoch seconds.
"""
from __future__ import annotations

from typing import Any

from .timestamp_utils import normalize_wall_timestamp

AUTO_CALIBRATION_WAIT_S = 3.0 * 86400.0   # the carried-over state must have lasted this long
AUTO_CALIBRATION_ATTEMPTS = 3             # automatic calibrations started per mode, at most
AUTO_CALIBRATION_RETRY_S = 20.0 * 3600.0  # minimum time between two automatic starts in a mode

STATUS_NOT_NEEDED = "not needed"
STATUS_WAIT_UPDATE = "waiting: 3 days after the update"
STATUS_WAIT_NIGHT = "waiting: quiet night"
STATUS_IN_PROGRESS = "in progress"
STATUS_ATTEMPTS = "attempts used"
STATUS_NO_POSITION = "home position unknown"


class AutoCalibration:
    """Per mode (``"heat"``, ``"cool"``): when the carried-over state was first seen, the attempts and the last one."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.mode: str | None = None   # mode of the last observation (runtime)
        self.seen: dict[str, float] = {}
        self.attempts: dict[str, int] = {}
        self.last: dict[str, float] = {}

    def observe(self, wall: float, mode: str, carried: bool) -> None:
        """Once per calculation: remember when the carried-over state of ``mode`` was first seen, forget it when the
        mode has a measured event."""
        self.mode = mode
        if carried:
            self.seen.setdefault(mode, wall)
        else:
            self.seen.pop(mode, None)

    def due(self, wall: float, mode: str) -> bool:
        """True when the state of ``mode`` has lasted long enough, attempts are left and the last one is old enough."""
        seen, last = self.seen.get(mode), self.last.get(mode)
        return (seen is not None and wall - seen >= AUTO_CALIBRATION_WAIT_S
                and self.attempts.get(mode, 0) < AUTO_CALIBRATION_ATTEMPTS
                and (last is None or wall - last >= AUTO_CALIBRATION_RETRY_S))

    def started(self, wall: float, mode: str) -> None:
        """An automatic calibration of ``mode`` started."""
        self.attempts[mode] = self.attempts.get(mode, 0) + 1
        self.last[mode] = wall

    def status(self, wall: float, in_progress: bool, position_known: bool) -> str:
        """Plain status of the current mode for the published diagnostics (``in_progress``: an automatic calibration is
        running)."""
        mode = self.mode
        if mode is None or mode not in self.seen:
            return STATUS_NOT_NEEDED
        if in_progress:
            return STATUS_IN_PROGRESS
        if self.attempts.get(mode, 0) >= AUTO_CALIBRATION_ATTEMPTS:
            return STATUS_ATTEMPTS
        if not position_known:
            return STATUS_NO_POSITION
        return STATUS_WAIT_UPDATE if wall - self.seen[mode] < AUTO_CALIBRATION_WAIT_S else STATUS_WAIT_NIGHT

    def save(self) -> dict[str, Any] | None:
        if not (self.seen or self.attempts or self.last):
            return None
        return {"seen": dict(self.seen), "attempts": dict(self.attempts), "last": dict(self.last)}

    def load(self, saved: object) -> None:
        """Restore what ``save`` wrote; anything else starts clean."""
        self.reset()
        if not isinstance(saved, dict):
            return
        for key, target in (("seen", self.seen), ("last", self.last)):
            for mode, value in (saved.get(key) if isinstance(saved.get(key), dict) else {}).items():
                if isinstance(mode, str) and (wall := normalize_wall_timestamp(value)) is not None:
                    target[mode] = wall
        for mode, n in (saved.get("attempts") if isinstance(saved.get("attempts"), dict) else {}).items():
            if isinstance(mode, str) and isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                self.attempts[mode] = min(n, AUTO_CALIBRATION_ATTEMPTS)
