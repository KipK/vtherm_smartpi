"""Positive-integral guard for Smart-PI recovery phases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL
from .const import (
    DEADBAND_HYSTERESIS,
    INTEGRAL_GUARD_RELEASE_ERROR_RATIO,
    INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H,
    INTEGRAL_GUARD_RELEASE_PERSISTENCE,
    INTEGRAL_GUARD_RELEASE_SLOPE_RATIO,
    INTEGRAL_GUARD_RELEASE_STALL_RATIO,
    INTEGRAL_GUARD_RESUME_PROGRESS_C,
    INTEGRAL_GUARD_SOFT_RELEASE_SCALE,
)


if TYPE_CHECKING:
    from .controller import SmartPIController
    from .deadtime_estimator import DeadTimeEstimator


class IntegralGuardSource(str, Enum):
    """Recovery source that temporarily blocks positive integral growth."""

    NONE = "none"
    SETPOINT_CHANGE = "setpoint_change"
    OFF_RESUME = "off_resume"
    WINDOW_RESUME = "window_resume"
    POWER_SHEDDING_RESUME = "power_shedding_resume"
    DISTURBANCE_RECOVERY = "disturbance_recovery"


def integral_guard_release_error_threshold(deadband_c: float) -> float:
    """Return the error threshold below which guard release may be evaluated."""
    return max(
        max(deadband_c, 0.0) * INTEGRAL_GUARD_RELEASE_ERROR_RATIO,
        max(deadband_c, 0.0) + max(DEADBAND_HYSTERESIS, 0.0),
    )


def signed_recovery_slope_h(
    slope_h: float | None,
    hvac_mode: VThermHvacMode | None,
) -> float | None:
    """Return a signed slope where positive means moving toward the target."""
    if slope_h is None:
        return None
    if hvac_mode == VThermHvacMode_COOL:
        return -float(slope_h)
    return float(slope_h)


RESUME_SOURCES = frozenset(
    {
        IntegralGuardSource.OFF_RESUME,
        IntegralGuardSource.WINDOW_RESUME,
        IntegralGuardSource.POWER_SHEDDING_RESUME,
    }
)


@dataclass(slots=True)
class IntegralGuardDecision:
    """Current authorization state for positive integral growth."""

    block_positive: bool
    block_negative: bool
    mode_suffix: str
    # Whether memory opposing the recovery may unwind toward zero.
    allow_unwind: bool = True
    # Factor applied to the integral update (1.0 outside a soft release).
    integral_scale: float = 1.0


def constrain_guarded_integral_delta(
    *,
    integral: float,
    requested_delta: float,
    block_positive: bool,
    block_negative: bool,
    allow_unwind: bool = True,
) -> tuple[float, str]:
    """Constrain an integral update while allowing opposing memory to unwind.

    A directional guard protects against new accumulation in one direction. It
    must not trap integral memory that already points in the opposite direction
    when the recovery stalls, because neutralizing that memory is then required
    to remove a steady-state error. While the recovery progresses, that memory
    is the model bias the integral carries and is kept (``allow_unwind`` False).

    Returns the allowed delta and one of ``run``, ``unwind``, or ``blocked``.
    """
    if block_positive and requested_delta > 0.0:
        if integral < 0.0 and allow_unwind:
            return min(requested_delta, -integral), "unwind"
        return 0.0, "blocked"

    if block_negative and requested_delta < 0.0:
        if integral > 0.0 and allow_unwind:
            return max(requested_delta, -integral), "unwind"
        return 0.0, "blocked"

    return requested_delta, "run"


class SmartPIIntegralGuard:
    """Track recovery phases and re-enable positive integral only after stabilization."""

    def __init__(self, name: str):
        self._name = name
        self.reset()

    @property
    def active(self) -> bool:
        """Return True when positive integral growth is still blocked."""
        return self._source != IntegralGuardSource.NONE

    @property
    def source(self) -> IntegralGuardSource:
        """Return the current guard source."""
        return self._source

    def reset(self) -> None:
        """Reset the guard state."""
        self._source = IntegralGuardSource.NONE
        self._block_positive = True
        self._stable_cycles = 0
        self._peak_signed_recovery_slope_h = 0.0
        self._stall_min = 0.0
        self._last_release_reason = "reset"
        # Last |error| seen beyond the release threshold while armed
        self._last_abs_error: float | None = None
        # Error before an interruption; recovering up to it is not progress
        self._resume_error_ref: float | None = None
        self._suspended_stall: tuple[bool, float, float | None] | None = None
        # Direction (+1 / -1) of a stalled recovery released softly, 0 when none
        self._soft_sign = 0

    @property
    def soft_release_active(self) -> bool:
        """Return True while a stalled recovery is released at a reduced integral rate."""
        return self._soft_sign != 0

    def suspend(self) -> None:
        """Deactivate the guard for a resume and keep its stall clock.

        A resume that re-arms the guard in the same direction continues the
        stall clock: the interruption does not show that the recovery moved
        again. A soft release in progress is kept as well.
        """
        carried = (self._block_positive, self._stall_min, self._last_abs_error) if self.active else None
        soft_sign = self._soft_sign
        self.reset()
        self._suspended_stall = carried
        self._soft_sign = soft_sign

    def drop_suspended_stall(self) -> None:
        """Forget the stall clock kept by ``suspend``."""
        self._suspended_stall = None

    def arm(self, source: IntegralGuardSource, *, block_positive: bool = True) -> None:
        """Arm the guard for the given recovery source.

        A resume in the direction of a soft release in progress does not arm
        the guard: the recovery was already found stalled and the integral
        keeps closing it at the reduced rate.
        """
        if source == IntegralGuardSource.NONE:
            self.clear("none")
            return
        direction = 1 if block_positive else -1
        if source in RESUME_SOURCES and not self.active and self._soft_sign == direction:
            self._suspended_stall = None
            return
        kept = None
        if source in RESUME_SOURCES:
            if self.active and self._block_positive == block_positive:
                kept = (self._stall_min, self._last_abs_error)
            elif self._suspended_stall is not None and self._suspended_stall[0] == block_positive:
                kept = self._suspended_stall[1:]
        self._suspended_stall = None
        self._soft_sign = 0
        if self._source != source or self._block_positive != block_positive:
            self._stable_cycles = 0
            self._peak_signed_recovery_slope_h = 0.0
            self._stall_min = 0.0
            self._resume_error_ref = None
            if kept is not None:
                self._stall_min, self._resume_error_ref = kept
        self._source = source
        self._block_positive = block_positive
        self._last_release_reason = "armed"

    def clear(self, reason: str) -> None:
        """Release the guard and clear transient tracking."""
        self._source = IntegralGuardSource.NONE
        self._stable_cycles = 0
        self._peak_signed_recovery_slope_h = 0.0
        self._stall_min = 0.0
        self._last_release_reason = reason
        self._last_abs_error = None
        self._resume_error_ref = None
        self._suspended_stall = None

    def decide(
        self,
        *,
        raw_error: float,
        release_error: float,
        deadband_c: float,
        in_core_deadband: bool,
        signed_recovery_slope_h: float | None,
        dt_min: float = 0.0,
        stall_min: float = 0.0,
    ) -> IntegralGuardDecision:
        """Return whether positive integral growth must remain blocked.

        Memory opposing the recovery may unwind only once the recovery has
        stalled for ``stall_min`` minutes: error still beyond the release
        threshold while the signed recovery slope stays at or below the
        release slope. ``stall_min`` 0 lets it unwind at once.

        A recovery still stalled after ``INTEGRAL_GUARD_RELEASE_STALL_RATIO``
        times ``stall_min`` releases the guard: proportional and feed-forward
        action have settled short of the target and only the integral can
        close the remaining error. The release is soft: the integral runs at
        ``INTEGRAL_GUARD_SOFT_RELEASE_SCALE`` of its rate until the error
        reaches the core deadband or changes sign, so that it does not
        overshoot the holding power while the room lags behind it. A
        disturbance recovery keeps its own release at the end of its plateau.
        """
        if not self.active:
            if self._soft_sign != 0:
                if in_core_deadband or raw_error * self._soft_sign <= 0.0:
                    self._soft_sign = 0
                else:
                    return IntegralGuardDecision(
                        False,
                        False,
                        "soft_release",
                        integral_scale=INTEGRAL_GUARD_SOFT_RELEASE_SCALE,
                    )
            return IntegralGuardDecision(False, False, "off")

        if in_core_deadband:
            self.clear("core_deadband")
            return IntegralGuardDecision(False, False, "released_core_deadband")

        if self._block_positive and raw_error <= 0.0:
            self.clear("signed_error_non_positive")
            return IntegralGuardDecision(False, False, "released_signed_error")

        if not self._block_positive and raw_error >= 0.0:
            self.clear("signed_error_non_positive")
            return IntegralGuardDecision(False, False, "released_signed_error")

        release_error_threshold = integral_guard_release_error_threshold(deadband_c)
        if abs(release_error) > release_error_threshold:
            self._stable_cycles = 0
            if signed_recovery_slope_h is not None and signed_recovery_slope_h > self._peak_signed_recovery_slope_h:
                self._peak_signed_recovery_slope_h = signed_recovery_slope_h
            abs_error = abs(release_error)
            self._last_abs_error = abs_error
            if (
                self._resume_error_ref is not None
                and abs_error < self._resume_error_ref - INTEGRAL_GUARD_RESUME_PROGRESS_C
            ):
                self._resume_error_ref = None
            if signed_recovery_slope_h is None or signed_recovery_slope_h <= self._release_slope():
                self._stall_min += max(dt_min, 0.0)
            elif self._resume_error_ref is None:
                self._stall_min = 0.0
            # else: recovering the dip of an interruption is not progress; the
            # stall clock holds until the error falls below its earlier level
            if (
                stall_min > 0.0
                and self._stall_min >= INTEGRAL_GUARD_RELEASE_STALL_RATIO * stall_min
                and self._source != IntegralGuardSource.DISTURBANCE_RECOVERY
            ):
                soft_sign = 1 if self._block_positive else -1
                self.clear("stalled_above_threshold")
                self._soft_sign = soft_sign
                return IntegralGuardDecision(False, False, "released_stalled")
            return IntegralGuardDecision(
                self._block_positive,
                not self._block_positive,
                self._source.value,
                allow_unwind=self._stall_min >= stall_min,
            )

        if signed_recovery_slope_h is None:
            self.clear("released_missing_slope")
            return IntegralGuardDecision(False, False, "released_missing_slope")

        if signed_recovery_slope_h > self._peak_signed_recovery_slope_h:
            self._peak_signed_recovery_slope_h = signed_recovery_slope_h

        release_slope = self._release_slope()
        if signed_recovery_slope_h <= release_slope:
            self._stable_cycles += 1
        else:
            self._stable_cycles = 0

        if self._stable_cycles >= INTEGRAL_GUARD_RELEASE_PERSISTENCE:
            self.clear("stabilized")
            return IntegralGuardDecision(False, False, "released_stabilized")

        return IntegralGuardDecision(
            self._block_positive,
            not self._block_positive,
            self._source.value,
            allow_unwind=self._stall_min >= stall_min,
        )

    def _release_slope(self) -> float:
        """Signed recovery slope (C/h) at or below which the recovery has collapsed."""
        return max(
            self._peak_signed_recovery_slope_h * INTEGRAL_GUARD_RELEASE_SLOPE_RATIO,
            INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H,
        )


class ResumeDeadtimeHold:
    """Temporary I:HOLD after a window or power-shedding resume.

    The hold covers the active dead time of the resumed episode; afterwards the
    directional integral guard is armed only when the residual error is still
    large.
    """

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        """Drop any pending or running resume hold."""
        self.source: IntegralGuardSource = IntegralGuardSource.NONE
        self.started: bool = False

    def start(self, source: IntegralGuardSource) -> None:
        """Request a hold for the resume `source`; it starts on the next update."""
        self.source = source
        self.started = False

    @property
    def active(self) -> bool:
        """Return True while a resume hold is pending or running."""
        return self.source != IntegralGuardSource.NONE or self.started

    @staticmethod
    def _should_arm_guard_after_deadtime(
        *,
        error_i: float,
        in_core_deadband: bool,
        deadband_c: float,
    ) -> bool:
        """Return True when a post-deadtime residual error still justifies a guard."""
        if in_core_deadband or error_i <= 0.0:
            return False
        return abs(error_i) > integral_guard_release_error_threshold(deadband_c)

    def update(
        self,
        *,
        error_i: float,
        in_core_deadband: bool,
        deadband_c: float,
        hvac_mode: VThermHvacMode,
        dt_est: DeadTimeEstimator,
        ctl: SmartPIController,
        guard: SmartPIIntegralGuard,
        in_deadtime_window: Callable[[], bool],
    ) -> None:
        """Manage the temporary I:HOLD window after a window/shedding resume."""
        source = self.source
        if source == IntegralGuardSource.NONE:
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
            if ctl.integral_hold_mode == source.value:
                ctl.clear_integral_hold()
            if self._should_arm_guard_after_deadtime(
                error_i=error_i,
                in_core_deadband=in_core_deadband,
                deadband_c=deadband_c,
            ):
                guard.arm(source)
            else:
                guard.drop_suspended_stall()
            self.source = IntegralGuardSource.NONE
            self.started = False
            return

        if not self.started:
            ctl.start_integral_hold(source.value)

        if in_deadtime_window():
            self.started = True
            return

        if self.started:
            if ctl.integral_hold_mode == source.value:
                ctl.clear_integral_hold()
            if self._should_arm_guard_after_deadtime(
                error_i=error_i,
                in_core_deadband=in_core_deadband,
                deadband_c=deadband_c,
            ):
                guard.arm(source)
            else:
                guard.drop_suspended_stall()
            self.source = IntegralGuardSource.NONE
            self.started = False
            return

        if not self._should_arm_guard_after_deadtime(
            error_i=error_i,
            in_core_deadband=in_core_deadband,
            deadband_c=deadband_c,
        ):
            guard.drop_suspended_stall()
            if ctl.integral_hold_mode == source.value:
                ctl.clear_integral_hold()
            self.source = IntegralGuardSource.NONE
            self.started = False
