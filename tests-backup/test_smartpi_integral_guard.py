"""Tests for the SmartPI positive-integral recovery guard."""

from custom_components.versatile_thermostat.smartpi.integral_guard import (
    IntegralGuardSource,
    SmartPIIntegralGuard,
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
