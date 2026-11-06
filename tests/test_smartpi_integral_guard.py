"""Tests for the SmartPI positive-integral recovery guard."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import INTEGRAL_GUARD_SOFT_RELEASE_SCALE
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.integral_guard import (
    IntegralGuardDecision,
    IntegralGuardSource,
    ResumeDeadtimeHold,
    SmartPIIntegralGuard,
    constrain_guarded_integral_delta,
)


def test_integral_guard_releases_immediately_in_core_deadband():
    """Entering the configured deadband must clear the recovery guard."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE)

    decision = guard.decide(
        raw_error=0.04,
        release_error=0.04,
        deadband_c=0.05,
        in_core_deadband=True,
        signed_recovery_slope_h=0.20,
    )

    assert decision.block_positive is False
    assert guard.active is False
    assert decision.mode_suffix == "released_core_deadband"


def test_integral_guard_waits_for_slope_collapse_on_slow_system():
    """A slow but still-rising system must keep the guard until the rise clearly collapses."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.WINDOW_RESUME)

    decision = guard.decide(
        raw_error=0.50,
        release_error=0.50,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.60,
    )
    assert decision.block_positive is True

    decision = guard.decide(
        raw_error=0.09,
        release_error=0.09,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.25,
    )
    assert decision.block_positive is True

    for _ in range(2):
        decision = guard.decide(
            raw_error=0.09,
            release_error=0.09,
            deadband_c=0.05,
            in_core_deadband=False,
            signed_recovery_slope_h=0.18,
        )
        assert decision.block_positive is True

    decision = guard.decide(
        raw_error=0.09,
        release_error=0.09,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.18,
    )
    assert decision.block_positive is False
    assert guard.active is False
    assert decision.mode_suffix == "released_stabilized"


def test_integral_guard_blocks_positive_growth_without_blocking_negative_side():
    """The guard must block only the positive side of the integral signal."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)

    positive = guard.decide(
        raw_error=0.30,
        release_error=0.30,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.10,
    )
    assert positive.block_positive is True
    assert positive.block_negative is False

    negative = guard.decide(
        raw_error=-0.02,
        release_error=-0.02,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=-0.01,
    )
    assert negative.block_positive is False
    assert negative.block_negative is False
    assert guard.active is False


def test_integral_guard_releases_from_servo_error_even_if_raw_error_stays_larger():
    """A filtered setpoint trajectory must be able to release the guard near stabilization."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE)

    decision = guard.decide(
        raw_error=0.50,
        release_error=0.50,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.40,
    )
    assert decision.block_positive is True

    for _ in range(2):
        decision = guard.decide(
            raw_error=0.20,
            release_error=0.08,
            deadband_c=0.05,
            in_core_deadband=False,
            signed_recovery_slope_h=0.10,
        )
        assert decision.block_positive is True

    decision = guard.decide(
        raw_error=0.20,
        release_error=0.08,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.10,
    )
    assert decision.block_positive is False
    assert decision.block_negative is False
    assert guard.active is False
    assert decision.mode_suffix == "released_stabilized"


def test_integral_guard_releases_immediately_when_release_error_is_small_and_slope_is_missing():
    """A missing slope must not keep the guard armed once the release error is already small."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.OFF_RESUME)

    decision = guard.decide(
        raw_error=0.09,
        release_error=0.09,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=None,
    )

    assert decision.block_positive is False
    assert decision.block_negative is False
    assert guard.active is False
    assert decision.mode_suffix == "released_missing_slope"


def test_integral_guard_can_block_negative_side_for_setpoint_reduction():
    """A signed demand reduction must preserve integral by blocking negative integration."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=False)

    decision = guard.decide(
        raw_error=-0.20,
        release_error=-0.08,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.10,
    )

    assert decision.block_positive is False
    assert decision.block_negative is True
    assert decision.mode_suffix == IntegralGuardSource.SETPOINT_CHANGE.value


def test_integral_guard_uses_absolute_slope_floor_when_relative_threshold_is_too_low():
    """A very small peak slope must still use the absolute stabilization floor."""
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.OFF_RESUME)

    decision = guard.decide(
        raw_error=0.40,
        release_error=0.40,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.20,
    )
    assert decision.block_positive is True

    for _ in range(2):
        decision = guard.decide(
            raw_error=0.09,
            release_error=0.09,
            deadband_c=0.05,
            in_core_deadband=False,
            signed_recovery_slope_h=0.13,
        )
        assert decision.block_positive is True

    for _ in range(2):
        decision = guard.decide(
            raw_error=0.09,
            release_error=0.09,
            deadband_c=0.05,
            in_core_deadband=False,
            signed_recovery_slope_h=0.11,
        )
        assert decision.block_positive is True

    decision = guard.decide(
        raw_error=0.09,
        release_error=0.09,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=0.11,
    )

    assert decision.block_positive is False
    assert decision.block_negative is False
    assert guard.active is False
    assert decision.mode_suffix == "released_stabilized"


def test_positive_guard_allows_opposing_integral_to_unwind_without_crossing_zero():
    """A positive guard must neutralize negative memory but not build positive memory."""
    delta, mode = constrain_guarded_integral_delta(
        integral=-0.4,
        requested_delta=0.7,
        block_positive=True,
        block_negative=False,
    )

    assert delta == 0.4
    assert mode == "unwind"

    delta, mode = constrain_guarded_integral_delta(
        integral=0.0,
        requested_delta=0.7,
        block_positive=True,
        block_negative=False,
    )

    assert delta == 0.0
    assert mode == "blocked"


def test_negative_guard_allows_opposing_integral_to_unwind_without_crossing_zero():
    """A negative guard must neutralize positive memory but not build negative memory."""
    delta, mode = constrain_guarded_integral_delta(
        integral=0.4,
        requested_delta=-0.7,
        block_positive=False,
        block_negative=True,
    )

    assert delta == -0.4
    assert mode == "unwind"

    delta, mode = constrain_guarded_integral_delta(
        integral=0.0,
        requested_delta=-0.7,
        block_positive=False,
        block_negative=True,
    )

    assert delta == 0.0
    assert mode == "blocked"


def _setpoint_guard() -> SmartPIIntegralGuard:
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=True)
    return guard


def _decide(guard: SmartPIIntegralGuard, slope_h: float, dt_min: float = 1.0, stall_min: float = 30.0):
    return guard.decide(
        raw_error=1.0,
        release_error=1.0,
        deadband_c=0.05,
        in_core_deadband=False,
        signed_recovery_slope_h=slope_h,
        dt_min=dt_min,
        stall_min=stall_min,
    )


def test_opposing_memory_is_kept_while_the_recovery_progresses():
    """The integral keeps the model bias it carries while the room rises."""
    guard = _setpoint_guard()
    for _ in range(120):
        decision = _decide(guard, slope_h=0.8)
    assert decision.block_positive is True
    assert decision.allow_unwind is False

    delta, mode = constrain_guarded_integral_delta(
        integral=-0.4, requested_delta=0.7, block_positive=True, block_negative=False,
        allow_unwind=decision.allow_unwind,
    )
    assert (delta, mode) == (0.0, "blocked")


def test_opposing_memory_unwinds_once_the_recovery_stalls():
    """A recovery stalled below the target lets the opposing memory unwind."""
    guard = _setpoint_guard()
    for _ in range(10):
        _decide(guard, slope_h=0.8)
    for _ in range(29):
        decision = _decide(guard, slope_h=0.05)
    assert decision.allow_unwind is False
    decision = _decide(guard, slope_h=0.05)
    assert decision.block_positive is True
    assert decision.allow_unwind is True

    # A recovery that moves again restarts the stall count.
    decision = _decide(guard, slope_h=0.8)
    assert decision.allow_unwind is False


def test_without_stall_duration_the_opposing_memory_unwinds_at_once():
    guard = _setpoint_guard()
    assert _decide(guard, slope_h=0.8, stall_min=0.0).allow_unwind is True


def test_a_long_stalled_recovery_releases_the_guard():
    """P and FF settled short of the target: only the integral can close the error."""
    guard = _setpoint_guard()
    for _ in range(10):
        _decide(guard, slope_h=0.8)
    for _ in range(59):
        decision = _decide(guard, slope_h=0.05)
    assert decision.block_positive is True
    assert guard.active is True

    decision = _decide(guard, slope_h=0.05)
    assert decision.block_positive is False
    assert decision.block_negative is False
    assert decision.mode_suffix == "released_stalled"
    assert guard.active is False
    assert guard._last_release_reason == "stalled_above_threshold"


def test_a_progressing_recovery_restarts_the_stall_release():
    guard = _setpoint_guard()
    for _ in range(50):
        _decide(guard, slope_h=0.05)
    _decide(guard, slope_h=0.8)
    for _ in range(50):
        decision = _decide(guard, slope_h=0.05)
    assert decision.block_positive is True
    assert guard.active is True


def test_a_stalled_setpoint_decrease_releases_the_negative_guard():
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=False)
    for _ in range(60):
        decision = _decide_err(guard, -1.0, 0.05)
    assert decision.block_negative is False
    assert decision.mode_suffix == "released_stalled"
    assert guard.active is False


def test_a_stalled_disturbance_recovery_keeps_its_plateau_release():
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.DISTURBANCE_RECOVERY)
    for _ in range(200):
        decision = _decide(guard, slope_h=0.05)
    assert decision.block_positive is True
    assert guard.active is True
    assert guard.soft_release_active is False


def test_without_stall_duration_the_guard_is_not_released_by_a_stall():
    guard = _setpoint_guard()
    for _ in range(200):
        decision = _decide(guard, slope_h=0.05, stall_min=0.0)
    assert decision.block_positive is True
    assert guard.active is True


def _stall(guard: SmartPIIntegralGuard, minutes: int) -> IntegralGuardDecision:
    decision = None
    for _ in range(minutes):
        decision = _decide(guard, slope_h=0.05)
    return decision


def test_a_resume_in_the_same_direction_keeps_the_stall_clock():
    """A shedding interruption does not restart the stall of a recovery."""
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    assert guard.active is False
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)
    assert guard._stall_min == 40.0
    decision = _stall(guard, 20)
    assert decision.mode_suffix == "released_stalled"
    assert guard.active is False


def test_an_off_resume_on_an_active_guard_keeps_the_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.arm(IntegralGuardSource.OFF_RESUME)
    assert guard._stall_min == 40.0


def test_a_resume_in_the_other_direction_restarts_the_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    guard.arm(IntegralGuardSource.WINDOW_RESUME, block_positive=False)
    assert guard._stall_min == 0.0


def test_a_new_setpoint_restarts_the_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE)
    assert guard._stall_min == 0.0


def test_a_resume_hold_ending_without_guard_forgets_the_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    guard.drop_suspended_stall()
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)
    assert guard._stall_min == 0.0


def test_a_released_guard_forgets_the_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    guard.clear("core_deadband")
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)
    assert guard._stall_min == 0.0


def _hold_update(hold: ResumeDeadtimeHold, guard: SmartPIIntegralGuard, error_i: float) -> None:
    dt_est = MagicMock()
    dt_est.active_deadtime.return_value = (None, False)
    hold.update(
        error_i=error_i,
        in_core_deadband=False,
        deadband_c=0.05,
        hvac_mode=VThermHvacMode_HEAT,
        dt_est=dt_est,
        ctl=MagicMock(),
        guard=guard,
        in_deadtime_window=lambda: False,
    )


def test_the_resume_hold_rearms_with_the_kept_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    hold = ResumeDeadtimeHold()
    hold.start(IntegralGuardSource.POWER_SHEDDING_RESUME)
    _hold_update(hold, guard, error_i=0.6)
    assert guard.source == IntegralGuardSource.POWER_SHEDDING_RESUME
    assert guard._stall_min == 40.0


def test_the_resume_hold_without_guard_drops_the_kept_stall_clock():
    guard = _setpoint_guard()
    _stall(guard, 40)
    guard.suspend()
    hold = ResumeDeadtimeHold()
    hold.start(IntegralGuardSource.POWER_SHEDDING_RESUME)
    _hold_update(hold, guard, error_i=0.05)
    assert guard.active is False
    assert guard._suspended_stall is None


def _decide_err(guard: SmartPIIntegralGuard, error: float, slope_h: float, *, in_core_deadband: bool = False):
    return guard.decide(
        raw_error=error,
        release_error=error,
        deadband_c=0.05,
        in_core_deadband=in_core_deadband,
        signed_recovery_slope_h=slope_h,
        dt_min=1.0,
        stall_min=30.0,
    )


def test_recovering_the_dip_of_an_interruption_keeps_the_stall_clock():
    """After a resume, rising back to the earlier error is not recovery progress."""
    guard = _setpoint_guard()
    for _ in range(40):
        _decide_err(guard, 0.6, 0.05)
    guard.suspend()
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)
    for error in (0.8, 0.75, 0.7, 0.65, 0.6):
        _decide_err(guard, error, 1.0)
    assert guard._stall_min == 40.0
    for _ in range(19):
        decision = _decide_err(guard, 0.6, 0.05)
    assert guard.active is True
    decision = _decide_err(guard, 0.6, 0.05)
    assert decision.mode_suffix == "released_stalled"


def test_progress_beyond_the_earlier_error_restarts_the_stall_clock():
    guard = _setpoint_guard()
    for _ in range(40):
        _decide_err(guard, 0.6, 0.05)
    guard.suspend()
    guard.arm(IntegralGuardSource.POWER_SHEDDING_RESUME)
    _decide_err(guard, 0.5, 1.0)
    assert guard._stall_min == 0.0


def _soft_released_guard(error: float = 0.6, *, block_positive: bool = True) -> SmartPIIntegralGuard:
    guard = SmartPIIntegralGuard("test")
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=block_positive)
    decision = None
    for _ in range(60):
        decision = _decide_err(guard, error, 0.05)
    assert decision.mode_suffix == "released_stalled"
    return guard


def test_a_stalled_release_integrates_at_the_soft_rate():
    """The integral takes over the stalled error at a reduced rate, not at once."""
    guard = _soft_released_guard()
    assert guard.soft_release_active is True
    decision = _decide_err(guard, 0.5, 0.3)
    assert decision.block_positive is False
    assert decision.block_negative is False
    assert decision.mode_suffix == "soft_release"
    assert decision.integral_scale == pytest.approx(INTEGRAL_GUARD_SOFT_RELEASE_SCALE)
    assert 0.0 < INTEGRAL_GUARD_SOFT_RELEASE_SCALE < 1.0


def test_the_soft_release_ends_in_the_core_deadband():
    guard = _soft_released_guard()
    decision = _decide_err(guard, 0.03, 0.1, in_core_deadband=True)
    assert decision.integral_scale == 1.0
    assert guard.soft_release_active is False
    # Leaving the deadband again does not bring the soft rate back.
    assert _decide_err(guard, 0.2, 0.0).integral_scale == 1.0


def test_the_soft_release_ends_when_the_error_changes_sign():
    guard = _soft_released_guard()
    decision = _decide_err(guard, -0.06, -0.1)
    assert decision.integral_scale == 1.0
    assert guard.soft_release_active is False


def test_a_new_setpoint_ends_the_soft_release():
    guard = _soft_released_guard()
    guard.arm(IntegralGuardSource.SETPOINT_CHANGE)
    assert guard.active is True
    assert guard.soft_release_active is False
    assert _decide_err(guard, 0.6, 0.05).block_positive is True


def test_a_shedding_resume_in_the_same_direction_keeps_the_soft_release():
    """A recovery already found stalled is not guarded again by each interruption."""
    guard = _soft_released_guard()
    guard.suspend()
    hold = ResumeDeadtimeHold()
    hold.start(IntegralGuardSource.POWER_SHEDDING_RESUME)
    _hold_update(hold, guard, error_i=0.7)
    assert guard.active is False
    assert guard.soft_release_active is True
    decision = _decide_err(guard, 0.7, 0.0)
    assert decision.block_positive is False
    assert decision.integral_scale == pytest.approx(INTEGRAL_GUARD_SOFT_RELEASE_SCALE)


def test_an_off_resume_in_the_same_direction_keeps_the_soft_release():
    guard = _soft_released_guard()
    guard.arm(IntegralGuardSource.OFF_RESUME)
    assert guard.active is False
    assert guard.soft_release_active is True


def test_a_resume_in_the_other_direction_ends_the_soft_release():
    guard = _soft_released_guard()
    guard.suspend()
    guard.arm(IntegralGuardSource.WINDOW_RESUME, block_positive=False)
    assert guard.active is True
    assert guard.soft_release_active is False


def test_a_disturbance_recovery_ends_the_soft_release():
    guard = _soft_released_guard()
    guard.arm(IntegralGuardSource.DISTURBANCE_RECOVERY)
    assert guard.active is True
    assert guard.soft_release_active is False


def test_a_stalled_setpoint_decrease_releases_softly_downward():
    guard = _soft_released_guard(-0.6, block_positive=False)
    assert guard.soft_release_active is True
    decision = _decide_err(guard, -0.4, -0.2)
    assert decision.mode_suffix == "soft_release"
    assert decision.integral_scale == pytest.approx(INTEGRAL_GUARD_SOFT_RELEASE_SCALE)
    assert _decide_err(guard, 0.06, 0.1).integral_scale == 1.0
    assert guard.soft_release_active is False


def test_a_reset_ends_the_soft_release():
    guard = _soft_released_guard()
    guard.reset()
    assert guard.soft_release_active is False


def _controller_integral_after(scale: float) -> float:
    ctl = SmartPIController("test")
    ctl.compute_pwm(
        error=0.5,
        error_p=0.5,
        kp=0.5,
        ki=0.005,
        u_ff=0.3,
        dt_min=10.0,
        in_deadband=False,
        integrator_hold=False,
        deadband_c=0.05,
        integral_scale=scale,
    )
    return ctl.integral


def test_the_controller_scales_the_integral_update():
    assert _controller_integral_after(1.0) == pytest.approx(5.0)
    assert _controller_integral_after(INTEGRAL_GUARD_SOFT_RELEASE_SCALE) == pytest.approx(
        5.0 * INTEGRAL_GUARD_SOFT_RELEASE_SCALE
    )
