"""Tests for SmartPI integral coherence after bumpless refactor.

The integral is the slow PI memory for disturbance rejection only.
Setpoint changes, gain changes, and FF changes must NOT rewrite the integral.
"""

import pytest
from custom_components.versatile_thermostat.vtherm_hvac_mode import (
    VThermHvacMode_HEAT,
    VThermHvacMode_COOL,
)
from custom_components.versatile_thermostat.smartpi.controller import SmartPIController


def test_small_setpoint_increase_preserves_integral():
    """A small setpoint increase must NOT modify the integral."""
    ctl = SmartPIController("test")
    ctl.integral = 3.0
    ctl.last_i_mode = "I:RUN"

    ctl.handle_setpoint_change(
        target_temp=20.3,
        last_target_temp=20.0,
        current_temp=19.8,
        hvac_mode=VThermHvacMode_HEAT,
        kp=1.0,
        ki=0.05,
    )

    assert ctl.integral == 3.0, "Small setpoint increase must preserve integral"
    assert ctl.hysteresis_thermal_guard is False


def test_small_setpoint_decrease_preserves_integral():
    """A small setpoint decrease must NOT modify the integral (only activates guard)."""
    ctl = SmartPIController("test")
    ctl.integral = 3.0

    ctl.handle_setpoint_change(
        target_temp=19.7,
        last_target_temp=20.0,
        current_temp=19.8,
        hvac_mode=VThermHvacMode_HEAT,
        kp=1.0,
        ki=0.05,
    )

    assert ctl.integral == 3.0, "Small setpoint decrease must preserve integral"
    assert ctl.hysteresis_thermal_guard is True


def test_large_setpoint_change_preserves_integral_without_recovery_hold():
    """A large setpoint change must preserve the integral without rewriting its mode."""
    ctl = SmartPIController("test")
    ctl.integral = 5.0

    new_e, new_e_p = ctl.handle_setpoint_change(
        target_temp=22.0,
        last_target_temp=20.0,
        current_temp=19.8,
        hvac_mode=VThermHvacMode_HEAT,
        kp=1.0,
        ki=0.05,
    )

    assert ctl.integral == 5.0, "Large setpoint change must preserve integral"
    assert ctl.servo_integral_hold is False
    assert new_e != 0.0
    assert new_e_p == new_e


def test_cool_mode_small_setpoint_preserves_integral():
    """In COOL mode, small setpoint changes must also preserve integral."""
    ctl = SmartPIController("test")
    ctl.integral = 2.0

    ctl.handle_setpoint_change(
        target_temp=24.3,
        last_target_temp=24.0,
        current_temp=25.0,
        hvac_mode=VThermHvacMode_COOL,
        kp=1.0,
        ki=0.05,
    )

    assert ctl.integral == 2.0, "COOL mode small setpoint must preserve integral"


def test_deadband_u_hold_and_u_cmd_from_nominal():
    """In core deadband, P must be frozen and only FF plus frozen I remain.

    No micro-leak: integral stays at 2.0.
    u_hold = 0.3 + 0.1 * 2.0 = 0.5
    u_cmd = 0.5.
    """
    ctl = SmartPIController("test")
    ctl.integral = 2.0

    ctl.compute_pwm(
        error=0.05,
        error_p=0.05,
        kp=1.0,
        ki=0.1,
        u_ff=0.3,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.45,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=19.95,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    # No micro-leak: integral unchanged
    assert ctl.integral == pytest.approx(2.0)

    # u_hold = u_ff + u_pi  (0.3 + 0.1 * 2.0 = 0.5)
    assert ctl.u_hold == pytest.approx(0.5)

    # u_cmd = u_ff + u_pi
    assert ctl.u_cmd == pytest.approx(0.5)


def test_deadband_heat_overshoot_freezes_p_in_deadband():
    """In HEAT core deadband overshoot, P must stay frozen with I."""
    ctl = SmartPIController("test")
    ctl.integral = 2.0

    ctl.compute_pwm(
        error=-0.05,
        error_p=-0.05,
        kp=1.0,
        ki=0.1,
        u_ff=0.3,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.45,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=20.05,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    # No micro-leak, no overshoot bleed: integral stays at 2.0
    assert ctl.integral == pytest.approx(2.0, rel=1e-6)

    # u_cmd = u_ff + u_pi (0.3 + 0.1 * 2.0 = 0.5)
    assert ctl.u_cmd == pytest.approx(0.5, rel=1e-6)
    assert ctl.u_hold == pytest.approx(0.5)
    assert ctl.deadband_power_source == "ff_plus_pi"


def test_deadband_cool_overshoot_freezes_p_in_deadband():
    """In COOL core deadband overshoot, P must stay frozen with I."""
    ctl = SmartPIController("test")
    ctl.integral = 2.0

    ctl.compute_pwm(
        error=-0.05,
        error_p=-0.05,
        kp=1.0,
        ki=0.1,
        u_ff=0.25,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.40,
        hvac_mode=VThermHvacMode_COOL,
        current_temp=23.95,
        target_temp=24.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    # No micro-leak, no overshoot bleed: integral stays at 2.0
    assert ctl.integral == pytest.approx(2.0, rel=1e-6)

    # u_cmd = u_ff + u_pi (0.25 + 0.1 * 2.0 = 0.45)
    assert ctl.u_cmd == pytest.approx(0.45, rel=1e-6)
    assert ctl.u_hold == pytest.approx(0.45)
    assert ctl.deadband_power_source == "ff_plus_pi"


def test_outside_deadband_u_hold_is_zero():
    """Outside deadband, u_hold must be 0 and u_cmd = u_ff + u_pi."""
    ctl = SmartPIController("test")
    ctl.integral = 1.0

    ctl.compute_pwm(
        error=0.5,
        error_p=0.5,
        kp=1.0,
        ki=0.1,
        u_ff=0.3,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=19.5,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.0,
    )

    assert ctl.u_hold == 0.0
    # u_cmd = clamp(u_ff + u_pi, 0, 1)
    u_pi_expected = 1.0 * 0.5 + 0.1 * ctl.integral
    u_raw = 0.3 + u_pi_expected
    assert pytest.approx(ctl.u_cmd, abs=0.01) == min(max(u_raw, 0.0), 1.0)


def test_servo_integral_hold_blocks_integration_until_deadband():
    """After a large setpoint change, integral must stay frozen until deadband."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.servo_integral_hold = True

    u_cmd = ctl.compute_pwm(
        error=1.0,
        error_p=1.0,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=18.0,
        target_temp=19.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    assert ctl.servo_integral_hold is True
    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_i_mode == "I:HOLD(servo_recovery)"
    assert u_cmd == pytest.approx(1.0)


def test_servo_integral_hold_stays_active_in_near_band():
    """Servo integral hold must remain active until deadband becomes active."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.servo_integral_hold = True

    ctl.compute_pwm(
        error=0.2,
        error_p=0.2,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=True,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=18.8,
        target_temp=19.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    assert ctl.servo_integral_hold is True
    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_i_mode == "I:HOLD(servo_recovery)"


def test_servo_integral_hold_releases_in_deadband():
    """Servo integral hold must release when deadband becomes active."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.servo_integral_hold = True

    ctl.compute_pwm(
        error=0.05,
        error_p=0.05,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=True,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=18.95,
        target_temp=19.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.1,
    )

    assert ctl.servo_integral_hold is False
    assert ctl.last_i_mode == "I:FREEZE(deadband)"


def test_core_deadband_unfreezes_integral_outside_latched_deadband_shell():
    """A latched deadband shell must not freeze PI once the raw error left the config deadband."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0

    ctl.compute_pwm(
        error=0.07,
        error_p=0.07,
        kp=1.0,
        ki=0.1,
        u_ff=0.0,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=True,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=19.93,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.05,
        core_deadband=False,
    )

    assert ctl.integral == pytest.approx(0.7)
    assert ctl.last_i_mode == "I:RUN"


def test_negative_guard_blocks_integral_drop_after_setpoint_reduction():
    """A setpoint reduction guard must preserve the integral while the room cools down."""
    ctl = SmartPIController("test")
    ctl.integral = 3.0

    ctl.compute_pwm(
        error=-0.2,
        error_p=-0.2,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=True,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=20.2,
        target_temp=20.0,
        hysteresis_thermal_guard=True,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.05,
        block_negative_integral=True,
        positive_integral_guard_mode="setpoint_change",
    )

    assert ctl.integral == pytest.approx(3.0)
    assert ctl.last_i_mode == "I:GUARD(setpoint_change)"


def test_negative_guard_prevents_hold_bleed_after_setpoint_reduction():
    """A protected setpoint reduction must not bleed the integral during a temporary hold."""
    ctl = SmartPIController("test")
    ctl.integral = 3.0

    ctl.compute_pwm(
        error=-0.2,
        error_p=-0.2,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=True,
        integrator_hold=True,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=20.2,
        target_temp=20.0,
        hysteresis_thermal_guard=True,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.05,
        block_negative_integral=True,
        positive_integral_guard_mode="setpoint_change",
    )

    assert ctl.integral == pytest.approx(3.0)
    assert ctl.last_i_mode == "I:HOLD"
