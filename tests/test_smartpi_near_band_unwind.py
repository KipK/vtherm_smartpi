"""Conditional integration lets the integral unwind toward zero one sensor step from the setpoint.

With a quantized sensor, a reading one step above the setpoint saturates the
output low through the proportional term alone. Blocking the integral there
(and freezing it in the deadband on the setpoint level) would hold a steady
offset forever.
"""
import pytest

from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController

KP, KI = 2.0, 0.02
UNWIND_BAND = 0.15  # 1.5 steps of a 0.1 C sensor


def _tick(ctl: SmartPIController, error: float, unwind_band=UNWIND_BAND, dt_min: float = 1.0,
          u_ff: float = 0.02, u_max: float = 1.0) -> float:
    return ctl.compute_pwm(
        error=error, error_p=error, kp=KP, ki=KI, u_ff=u_ff, dt_min=dt_min,
        in_deadband=False, integrator_hold=False, deadband_c=0.05,
        unwind_band_c=unwind_band, u_max=u_max,
    )


def test_saturated_low_one_step_above_unwinds_a_positive_integral():
    ctl = SmartPIController("t")
    ctl.integral = 2.0
    _tick(ctl, -0.1)  # 0.02 - 0.2 + 0.04 < 0: saturated low by P
    assert ctl.last_sat == "SAT_LO"
    assert ctl.last_i_mode == "I:UNWIND(SAT_LO)"
    assert ctl.integral == pytest.approx(1.9)


def test_unwinding_stops_at_zero():
    ctl = SmartPIController("t")
    ctl.integral = 0.05
    _tick(ctl, -0.1)
    assert ctl.integral == 0.0
    _tick(ctl, -0.1)
    assert ctl.integral == 0.0
    assert ctl.last_i_mode == "I:SKIP(SAT_LO)"


def test_winding_into_the_saturation_stays_blocked():
    ctl = SmartPIController("t")
    ctl.integral = -1.0
    _tick(ctl, -0.1)
    assert ctl.last_i_mode == "I:SKIP(SAT_LO)"
    assert ctl.integral == -1.0


def test_beyond_one_sensor_step_the_saturation_blocks_the_integral():
    ctl = SmartPIController("t")
    ctl.integral = 2.0
    _tick(ctl, -0.3)
    assert ctl.last_i_mode == "I:SKIP(SAT_LO)"
    assert ctl.integral == 2.0


def test_without_unwind_band_the_conditional_integration_is_unchanged():
    ctl = SmartPIController("t")
    ctl.integral = 2.0
    _tick(ctl, -0.1, unwind_band=None)
    assert ctl.last_i_mode == "I:SKIP(SAT_LO)"
    assert ctl.integral == 2.0


def test_an_output_held_at_its_limit_by_ff_and_i_keeps_the_integral():
    """At max_on with FF + I beyond the limit, the saturation is not the P response."""
    ctl = SmartPIController("t")
    ctl.integral = -2.0  # Ki * I = -0.04 compensates a too strong FF
    _tick(ctl, 0.1, u_ff=0.6, u_max=0.5)  # u_ff + Ki * I = 0.56 > u_max
    assert ctl.last_sat == "SAT_HI"
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"
    assert ctl.integral == -2.0
