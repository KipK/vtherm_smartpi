"""Tests for SmartPI integral coherence after bumpless refactor.

The integral is the slow PI memory for disturbance rejection only.
Setpoint changes, gain changes, and FF changes must NOT rewrite the integral.
"""

import pytest
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController


def test_positive_integral_growth_remains_available_below_target():
    """Positive integration must remain available below target."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0

    ctl.compute_pwm(
        error=0.7,
        error_p=0.7,
        kp=0.1,
        ki=0.01,
        u_ff=0.3,
        dt_min=1.0,
        in_deadband=False,
        integrator_hold=False,
        deadband_c=0.05,
    )

    assert ctl.integral == pytest.approx(0.7)
    assert ctl.last_i_mode == "I:RUN"


def test_positive_guard_unwinds_negative_integral_before_blocking_growth():
    """Setpoint recovery may neutralize opposing memory without crossing zero."""
    ctl = SmartPIController("test")
    ctl.integral = -0.4

    common = {
        "error": 0.7,
        "error_p": 0.7,
        "kp": 0.1,
        "ki": 0.01,
        "u_ff": 0.3,
        "dt_min": 1.0,
        "in_deadband": False,
        "integrator_hold": False,
        "deadband_c": 0.05,
        "block_positive_integral": True,
        "positive_integral_guard_mode": "setpoint_change",
    }

    ctl.compute_pwm(**common)

    assert ctl.integral == 0.0
    assert ctl.last_i_mode == "I:GUARD_UNWIND(setpoint_change)"

    ctl.compute_pwm(**common)

    assert ctl.integral == 0.0
    assert ctl.last_i_mode == "I:GUARD(setpoint_change)"


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
        in_deadband=True,
        integrator_hold=False,
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
        in_deadband=True,
        integrator_hold=False,
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
        in_deadband=True,
        integrator_hold=False,
        deadband_c=0.1,
    )

    # No micro-leak, no overshoot bleed: integral stays at 2.0
    assert ctl.integral == pytest.approx(2.0, rel=1e-6)

    # u_cmd = u_ff + u_pi (0.25 + 0.1 * 2.0 = 0.45)
    assert ctl.u_cmd == pytest.approx(0.45, rel=1e-6)
    assert ctl.u_hold == pytest.approx(0.45)
    assert ctl.deadband_power_source == "ff_plus_pi"


def test_deadband_allow_p_uses_damped_error_not_raw_error():
    """Allowed P inside core deadband is persistent and damped by the quiet zone."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0

    kwargs = {
        "error": 0.10,
        "error_p": 0.10,
        "kp": 1.0,
        "ki": 0.1,
        "u_ff": 0.3,
        "dt_min": 10.0,
        "in_deadband": True,
        "integrator_hold": False,
        "deadband_c": 0.1,
        "deadband_allow_p": True,
    }

    ctl.compute_pwm(**kwargs)

    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_error_p_db == pytest.approx(0.0)
    assert ctl.u_pi == pytest.approx(0.0)
    assert ctl.u_cmd == pytest.approx(0.3)
    assert ctl.deadband_p_mode == "deadband_edge_pending"

    ctl.compute_pwm(**kwargs)

    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_error_p_db == pytest.approx(0.025)
    assert ctl.u_pi == pytest.approx(0.025)
    assert ctl.u_cmd == pytest.approx(0.325)
    assert ctl.deadband_p_mode == "deadband_edge"


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
        in_deadband=False,
        integrator_hold=False,
        deadband_c=0.0,
    )

    assert ctl.u_hold == 0.0
    # u_cmd = clamp(u_ff + u_pi, 0, 1)
    u_pi_expected = 1.0 * 0.5 + 0.1 * ctl.integral
    u_raw = 0.3 + u_pi_expected
    assert pytest.approx(ctl.u_cmd, abs=0.01) == min(max(u_raw, 0.0), 1.0)


def test_integral_hold_blocks_integration_until_deadband():
    """An active integral hold must keep the integral frozen until deadband."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.start_integral_hold("window_resume")

    u_cmd = ctl.compute_pwm(
        error=1.0,
        error_p=1.0,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        in_deadband=False,
        integrator_hold=False,
        deadband_c=0.1,
    )

    assert ctl.integral_hold_mode == "window_resume"
    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_i_mode == "I:HOLD(window_resume)"
    assert u_cmd == pytest.approx(1.0)


def test_integral_hold_stays_active_in_near_band():
    """An integral hold must remain active until deadband becomes active."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.start_integral_hold("window_resume")

    ctl.compute_pwm(
        error=0.2,
        error_p=0.2,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        in_deadband=False,
        integrator_hold=False,
        deadband_c=0.1,
    )

    assert ctl.integral_hold_mode == "window_resume"
    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_i_mode == "I:HOLD(window_resume)"


def test_integral_hold_releases_in_deadband():
    """An integral hold must release when deadband becomes active."""
    ctl = SmartPIController("test")
    ctl.integral = 0.0
    ctl.start_integral_hold("window_resume")

    ctl.compute_pwm(
        error=0.05,
        error_p=0.05,
        kp=1.0,
        ki=0.1,
        u_ff=0.2,
        dt_min=10.0,
        in_deadband=True,
        integrator_hold=False,
        deadband_c=0.1,
    )

    assert ctl.integral_hold_mode == "none"
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
        in_deadband=True,
        integrator_hold=False,
        deadband_c=0.05,
        core_deadband=False,
    )

    assert ctl.integral == pytest.approx(0.7)
    assert ctl.last_i_mode == "I:RUN"


def test_negative_guard_unwinds_positive_integral_after_setpoint_reduction():
    """A setpoint reduction guard must neutralize opposing integral memory."""
    ctl = SmartPIController("test")
    ctl.integral = 3.0

    common = {
        "error": -0.2,
        "error_p": -0.2,
        "kp": 1.0,
        "ki": 0.1,
        "u_ff": 0.2,
        "dt_min": 10.0,
        "in_deadband": False,
        "integrator_hold": False,
        "deadband_c": 0.05,
        "block_negative_integral": True,
        "positive_integral_guard_mode": "setpoint_change",
    }

    ctl.compute_pwm(**common)

    assert ctl.integral == pytest.approx(1.0)
    assert ctl.last_i_mode == "I:GUARD_UNWIND(setpoint_change)"

    ctl.compute_pwm(**common)

    assert ctl.integral == pytest.approx(0.0)
    assert ctl.last_i_mode == "I:GUARD_UNWIND(setpoint_change)"

    ctl.compute_pwm(**common)

    assert ctl.integral == pytest.approx(0.0)
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
        in_deadband=False,
        integrator_hold=True,
        deadband_c=0.05,
        block_negative_integral=True,
        positive_integral_guard_mode="setpoint_change",
    )

    assert ctl.integral == pytest.approx(3.0)
    assert ctl.last_i_mode == "I:HOLD"
