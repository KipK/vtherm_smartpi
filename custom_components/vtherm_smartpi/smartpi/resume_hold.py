"""Integral holds over the effective dead time: after a heater start, and after a window, power-shedding or
heating-unavailable resume.

The hold lasts the effective dead time of the mode (smartpi/gains.py, ``effective_dead_time``): theta + tau_e / 2 of
the response model, else the reliable active dead time.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Callable

from .const import DEADBAND_HYSTERESIS, RECOVERY_ERROR_FLOOR_DEADBAND_RATIO


if TYPE_CHECKING:
    from .controller import SmartPIController


def deadtime_window_active(
    heat_hold_s: float | None,
    cool_hold_s: float | None,
    t_heat_episode_start: float | None,
    t_cool_episode_start: float | None,
    now: float,
) -> bool:
    """Return True while an active episode is younger than the hold of its mode (None: no hold)."""
    for hold_s, t_start in ((heat_hold_s, t_heat_episode_start), (cool_hold_s, t_cool_episode_start)):
        if hold_s is not None and isinstance(t_start, (int, float)) and now - t_start < hold_s:
            return True
    return False


def recovery_error_floor_c(deadband_c: float) -> float:
    """Return the residual error (°C) under which a recovery counts as closed."""
    return max(
        max(deadband_c, 0.0) * RECOVERY_ERROR_FLOOR_DEADBAND_RATIO,
        max(deadband_c, 0.0) + max(DEADBAND_HYSTERESIS, 0.0),
    )


class ResumeHoldSource(str, Enum):
    """Interruption whose resume holds the integral during the dead time."""

    NONE = "none"
    WINDOW_RESUME = "window_resume"
    POWER_SHEDDING_RESUME = "power_shedding_resume"
    HEATING_UNAVAILABLE_RESUME = "heating_unavailable_resume"


class ResumeDeadtimeHold:
    """Temporary I:HOLD after a window, power-shedding or heating-unavailable resume.

    The hold covers the effective dead time of the resumed episode, so that the
    integral does not accumulate the error while the emitter has not answered
    yet. A resume whose residual error is already small ends the hold before
    the dead time window starts.
    """

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        """Drop any pending or running resume hold."""
        self.source: ResumeHoldSource = ResumeHoldSource.NONE
        self.started: bool = False

    def start(self, source: ResumeHoldSource) -> None:
        """Request a hold for the resume `source`; it starts on the next update."""
        self.source = source
        self.started = False

    @property
    def active(self) -> bool:
        """Return True while a resume hold is pending or running."""
        return self.source != ResumeHoldSource.NONE or self.started

    @staticmethod
    def _residual_error_large(
        *,
        error_i: float,
        in_core_deadband: bool,
        deadband_c: float,
    ) -> bool:
        """Return True when the resumed error is still above the recovery floor."""
        if in_core_deadband or error_i <= 0.0:
            return False
        return abs(error_i) > recovery_error_floor_c(deadband_c)

    def _end(self, ctl: SmartPIController) -> None:
        """Release the controller hold of this resume and forget it."""
        if ctl.integral_hold_mode == self.source.value:
            ctl.clear_integral_hold()
        self.source = ResumeHoldSource.NONE
        self.started = False

    def update(
        self,
        *,
        error_i: float,
        in_core_deadband: bool,
        deadband_c: float,
        hold_s: float | None,
        ctl: SmartPIController,
        in_deadtime_window: Callable[[], bool],
    ) -> None:
        """Manage the temporary I:HOLD window after a resume; ``hold_s`` is the effective dead time of the mode
        (None without one: no hold)."""
        source = self.source
        if source == ResumeHoldSource.NONE:
            return

        if hold_s is None or hold_s <= 0.0:
            self._end(ctl)
            return

        if not self.started:
            ctl.start_integral_hold(source.value)

        if in_deadtime_window():
            self.started = True
            return

        if self.started:
            self._end(ctl)
            return

        if not self._residual_error_large(
            error_i=error_i,
            in_core_deadband=in_core_deadband,
            deadband_c=deadband_c,
        ):
            self._end(ctl)
