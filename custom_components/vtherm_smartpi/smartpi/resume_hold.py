"""Integral hold after a window or power-shedding resume."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Callable

from ..hvac_mode import VThermHvacMode
from .recovery_plateau import recovery_error_floor_c


if TYPE_CHECKING:
    from .controller import SmartPIController
    from .deadtime_estimator import DeadTimeEstimator


class ResumeHoldSource(str, Enum):
    """Interruption whose resume holds the integral during the dead time."""

    NONE = "none"
    WINDOW_RESUME = "window_resume"
    POWER_SHEDDING_RESUME = "power_shedding_resume"


class ResumeDeadtimeHold:
    """Temporary I:HOLD after a window or power-shedding resume.

    The hold covers the active dead time of the resumed episode, so that the
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
        hvac_mode: VThermHvacMode,
        dt_est: DeadTimeEstimator,
        ctl: SmartPIController,
        in_deadtime_window: Callable[[], bool],
    ) -> None:
        """Manage the temporary I:HOLD window after a window/shedding resume."""
        source = self.source
        if source == ResumeHoldSource.NONE:
            return

        active_deadtime_s, active_deadtime_reliable = (
            dt_est.active_deadtime(hvac_mode)
        )
        active_deadtime_ready = (
            active_deadtime_reliable
            and active_deadtime_s is not None
            and active_deadtime_s > 0.0
        )
        if not active_deadtime_ready:
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
