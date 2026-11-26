"""One-tick tests of the discrete control law, in each regime, with hand-computed values.

The law (per tick, HEAT signs; COOL mirrors through the signed error) is written in `.agents/context/theory.md`.
A test here feeds chosen inputs and checks the value the formula gives, not the value the code gave once.
"""

from __future__ import annotations

import time as real_time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi import algo as algo_module
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.anticipation import (
    DeadbandGate,
    anticipated_error,
    correction_c,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    DEFAULT_DEADBAND_C,
    KI_MAX,
    KI_MIN,
    KP_MAX,
    KP_MIN,
    NEAR_BAND_TAPER_KI_MIN,
    NEAR_BAND_TAPER_KP_MIN,
    OVERSHOOT_I_CLAMP_EPS_C,
    GovernanceDecision,
    clamp,
)
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff
from custom_components.vtherm_smartpi.smartpi.gains import GainDelay, GainScheduler, imc_simc_gains

from helpers import force_smartpi_stable_mode

DB = DEFAULT_DEADBAND_C  # 0.05
H_S = 1800.0             # horizon (s) of the ramp tests
NOW = 50_000.0


# --- helpers ---------------------------------------------------------------------------------------------------------


def _ramp(rate_per_min: float, y_now: float, span_s: float = H_S):
    """Raw history (one sample a minute, the current tick not included) of a straight ramp ending at ``y_now`` at NOW."""
    slope = rate_per_min / 60.0
    times = [NOW - span_s + 60.0 * i for i in range(int(span_s // 60))]
    return [(t, y_now - slope * (NOW - t)) for t in times]


def _eps_heat(rate_per_min: float, y_now: float, target: float, horizon_s: float = H_S):
    """(c, epsilon) of one HEAT tick from the real anticipation functions."""
    c = correction_c(horizon_s, NOW, y_now, target, _ramp(rate_per_min, y_now), True)
    return c, anticipated_error(target - y_now, c, VThermHvacMode_HEAT)


def _pwm(ctl: SmartPIController, eps: float, *, kp=0.5, ki=0.01, u_ff=0.2, dt=1.0, in_deadband=False,
         hold=False, core=False, db=DB, u_max=1.0, allow_p=False, src="external", unwind_band=0.075):
    """One compute_pwm tick as ``SmartPI.calculate`` calls it: the same epsilon is the integral and the P error."""
    return ctl.compute_pwm(
        eps, eps, kp, ki, u_ff, dt, in_deadband, hold, deadband_c=db, core_deadband=core,
        deadband_allow_p=allow_p, integrator_hold_source=src, u_max=u_max, unwind_band_c=unwind_band,
    )


def _ctl(integral: float = 0.0) -> SmartPIController:
    ctl = SmartPIController("law")
    ctl.integral = integral
    return ctl


# --- error, fade ramp, clamp, deadband gate ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, expected_c, expected_eps",
    [
        (21.78, 0.0, 2.0),         # e = 2.00 > 2 c~: w = 0, epsilon = e
        (20.98, 0.0, 1.2),         # e = 1.20 = 2 c~: w = 0 at the edge
        (20.68, 0.3, 0.6),         # e = 0.90 = 1.5 c~: w = 1/2, epsilon = 2 e - 2 c~ = 0.6
        (20.38, 0.6, 0.0),         # e = 0.60 = c~: w = 1, epsilon = e - c~ = 0
        (20.08, 0.6, -0.3),        # e = 0.30 < c~: epsilon = e - c~
        (19.58, 0.6, -0.8),        # beyond the setpoint (e = -0.20): w = 1
    ],
)
def test_fade_ramp_gives_epsilon_two_e_minus_two_c(target, expected_c, expected_eps) -> None:
    """HEAT, room rising 0.02 C/min at 19.78 C, horizon 1800 s: c~ = 0.6, c = c~ w, epsilon = e - c with w = clamp(2 - e/c~, 0, 1)."""
    y = 19.78
    c, eps = _eps_heat(0.02, y, target)
    e = target - y
    assert c == pytest.approx(expected_c, abs=1e-9)
    assert eps == pytest.approx(expected_eps, abs=1e-9)
    c_tilde = 0.02 / 60.0 * H_S
    if c_tilde <= e <= 2.0 * c_tilde:
        assert eps == pytest.approx(2.0 * e - 2.0 * c_tilde, abs=1e-9)  # the doubled slope of the ramp
    elif e < c_tilde:
        assert eps == pytest.approx(e - c_tilde, abs=1e-9)
    else:
        assert eps == pytest.approx(e, abs=1e-9)


def test_fade_ramp_mirrors_in_cool() -> None:
    """COOL, room falling 0.02 C/min toward a target below it: the same epsilon as the HEAT case through the signed error."""
    y = 20.22
    target = 19.32  # e_raw = r - y = -0.90, c~ = -0.6
    c = correction_c(H_S, NOW, y, target, _ramp(-0.02, y), True)
    assert c == pytest.approx(-0.3, abs=1e-9)
    e_signed = -(target - y)  # sigma = -1
    assert e_signed == pytest.approx(0.9)
    eps = anticipated_error(e_signed, c, VThermHvacMode_COOL)
    assert eps == pytest.approx(0.6, abs=1e-9)  # 2 e - 2 |c~|


def test_correction_is_slope_times_horizon_clamped_to_one_degree() -> None:
    """c~ = clamp(s H, -1, +1): 0.05 C/min over 1800 s would be 1.5 C."""
    y = 19.0
    c = correction_c(H_S, NOW, y, 19.0 + 0.5, _ramp(0.05, y), True)  # e = 0.5 < 1: w = 1
    assert c == pytest.approx(1.0, abs=1e-9)
    c = correction_c(H_S, NOW, y, 19.0 + 1.5, _ramp(0.05, y), True)  # e = 1.5 = 1.5 c~: w = 1/2
    assert c == pytest.approx(0.5, abs=1e-9)


def test_correction_does_not_depend_on_the_setpoint_within_one_c() -> None:
    """Derivative on the measurement: a setpoint step moves e, not c, while the room is within c of the target."""
    y = 19.78
    c1 = correction_c(H_S, NOW, y, y + 0.30, _ramp(0.02, y), True)
    c2 = correction_c(H_S, NOW, y, y + 0.55, _ramp(0.02, y), True)
    assert c1 == pytest.approx(c2, abs=1e-12)


def test_anticipation_off_gives_the_plain_error() -> None:
    """No horizon, or a command driven on/off: c = 0, epsilon = e (HEAT and COOL)."""
    y = 19.78
    hist = _ramp(0.02, y)
    assert correction_c(0.0, NOW, y, 20.0, hist, True) == 0.0
    assert correction_c(H_S, NOW, y, 20.0, hist, False) == 0.0
    assert correction_c(H_S, NOW, y, 20.0, hist[-2:], True) == 0.0  # fewer than 3 samples
    assert correction_c(H_S, NOW, y, 20.0, hist[-10:], True) == 0.0  # span under half the window
    assert anticipated_error(0.22, 0.0, VThermHvacMode_HEAT) == 0.22
    assert anticipated_error(0.22, 0.0, VThermHvacMode_COOL) == 0.22


def test_deadband_gate_keeps_the_braking_until_it_dies_out_then_zeroes() -> None:
    gate = DeadbandGate()
    assert gate.apply(0.3, in_deadband=False) == 0.3           # outside: unchanged
    assert gate.apply(0.2, in_deadband=True) == 0.2            # inside, same sign: kept
    assert gate.apply(0.1, in_deadband=True) == 0.1
    assert gate.apply(0.0, in_deadband=True) == 0.0            # dies out: settled
    assert gate.apply(0.3, in_deadband=True) == 0.0            # settled: a quick sensor move is not extrapolated
    assert gate.apply(0.3, in_deadband=False) == 0.3           # left the deadband: back
    assert gate.apply(0.2, in_deadband=True) == 0.2
    assert gate.apply(-0.1, in_deadband=True) == 0.0           # sign flip: settled


# --- one tick of compute_pwm: the regimes ------------------------------------------------------------------------------


def test_p_only_tick() -> None:
    """dt = 0 (first tick): I stays, u = u_ff + Kp (eps - db) with eps = 0.30."""
    ctl = _ctl()
    u = _pwm(ctl, 0.30, dt=0.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == 0.0
    assert u == pytest.approx(0.2 + 0.5 * (0.30 - DB), abs=1e-12)
    assert ctl.u_p == pytest.approx(0.125)


def test_run_tick_integral_update_and_output() -> None:
    """I1 = I0 + eps dt, u = clamp(u_ff + Kp (|eps| - db) sign + Ki I1, 0, 1); the integral takes eps, not eps - db."""
    ctl = _ctl(4.0)
    u = _pwm(ctl, 0.30, dt=2.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(4.0 + 0.30 * 2.0, abs=1e-12)
    assert u == pytest.approx(0.2 + 0.5 * 0.25 + 0.01 * 4.6, abs=1e-12)
    assert ctl.u_i == pytest.approx(0.046)


def test_negative_epsilon_run_tick_mirrors_the_p_projection() -> None:
    ctl = _ctl(4.0)
    u = _pwm(ctl, -0.30, dt=1.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(3.7)
    assert u == pytest.approx(0.2 - 0.5 * 0.25 + 0.01 * 3.7, abs=1e-12)


def test_tick_inside_the_fade_ramp() -> None:
    """e = 0.9 = 1.5 c~: eps = 0.6, so P acts on 2 e - 2 c~ and the integral takes eps dt."""
    c, eps = _eps_heat(0.02, 19.78, 19.78 + 0.9)
    assert c == pytest.approx(0.3, abs=1e-9)
    assert eps == pytest.approx(0.6, abs=1e-9)
    ctl = _ctl(1.0)
    u = _pwm(ctl, eps, dt=1.0)
    assert ctl.integral == pytest.approx(1.6, abs=1e-9)
    assert u == pytest.approx(0.2 + 0.5 * 0.55 + 0.01 * 1.6, abs=1e-9)


def test_p_dead_zone_outside_the_core_deadband() -> None:
    """|eps| <= db: no P (also outside the core deadband); the integral still runs."""
    ctl = _ctl(2.0)
    u = _pwm(ctl, 0.05, dt=1.0)  # eps = db exactly, above the clamp epsilon 0.04
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.u_p == 0.0
    assert ctl.integral == pytest.approx(2.05)
    assert u == pytest.approx(0.2 + 0.01 * 2.05, abs=1e-12)


def test_core_deadband_freezes_the_integral_and_zeroes_p() -> None:
    ctl = _ctl(6.0)
    u = _pwm(ctl, 0.03, dt=1.0, in_deadband=True, core=True)
    assert ctl.last_i_mode == "I:FREEZE(deadband)"
    assert ctl.integral == 6.0
    assert ctl.u_p == 0.0
    assert u == pytest.approx(0.2 + 0.01 * 6.0, abs=1e-12)  # hold power: u_ff + Ki I
    assert ctl.u_hold == pytest.approx(u)


def test_core_deadband_clamps_the_integral_to_its_bound_without_moving_it_otherwise() -> None:
    ctl = _ctl(500.0)  # 2/Ki = 200
    _pwm(ctl, 0.0, in_deadband=True, core=True)
    assert ctl.integral == pytest.approx(200.0)


@pytest.mark.parametrize("hold_kind", ["external", "generic"])
def test_integral_hold_keeps_i_but_p_stays_active(hold_kind) -> None:
    ctl = _ctl(3.0)
    if hold_kind == "generic":
        ctl.start_integral_hold("window_resume")
        u = _pwm(ctl, 0.40, dt=1.0)
        assert ctl.last_i_mode == "I:HOLD(window_resume)"
    else:
        u = _pwm(ctl, 0.40, dt=1.0, hold=True, src="deadtime")
        assert ctl.last_i_mode == "I:HOLD"
        assert ctl.last_integral_hold_source == "deadtime"
    assert ctl.integral == 3.0
    assert u == pytest.approx(0.2 + 0.5 * 0.35 + 0.01 * 3.0, abs=1e-12)


def test_core_deadband_ends_a_generic_hold() -> None:
    ctl = _ctl(3.0)
    ctl.start_integral_hold("window_resume")
    _pwm(ctl, 0.01, core=True, in_deadband=True)
    assert not ctl.integral_hold_active
    assert ctl.last_i_mode == "I:FREEZE(deadband)"


def test_deadband_shell_hold_has_no_p_inside_the_dead_zone() -> None:
    """In the shell the algorithm passes a hold: |eps| <= db gives no P and the integral stays."""
    ctl = _ctl(3.0)
    u = _pwm(ctl, 0.06 - 0.02, in_deadband=True, hold=True, src="deadband_shell")  # eps = 0.04
    assert ctl.last_i_mode == "I:HOLD"
    assert ctl.integral == 3.0
    assert ctl.u_p == 0.0
    assert u == pytest.approx(0.2 + 0.03, abs=1e-12)


def test_clamp_blocks_positive_increments_only() -> None:
    """eps <= 0.04: no growth of I (CLAMP); a negative increment still passes."""
    ctl = _ctl(2.0)
    _pwm(ctl, OVERSHOOT_I_CLAMP_EPS_C)
    assert ctl.last_i_mode == "I:CLAMP(near_ovr)"
    assert ctl.integral == 2.0
    ctl = _ctl(2.0)
    _pwm(ctl, -0.10, dt=1.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(1.90)
    ctl = _ctl(2.0)
    _pwm(ctl, 0.0, dt=1.0)  # eps = 0: no increment to block
    assert ctl.integral == 2.0


@pytest.mark.parametrize(
    "eps, u_ff, mode, expected_u",
    [
        (0.40, 0.5, "I:SKIP(SAT_HI)", 1.0),   # 0.5 + 2 * 0.35 + 0.1 > 1, error > 0
        (-0.40, 0.0, "I:SKIP(SAT_LO)", 0.0),  # 0 + 2 * (-0.35) + 0.1 < 0, error < 0
    ],
)
def test_conditional_integration_skips_when_saturated_against_epsilon(eps, u_ff, mode, expected_u) -> None:
    ctl = _ctl(10.0)
    u = _pwm(ctl, eps, kp=2.0, ki=0.01, u_ff=u_ff, dt=1.0)
    assert ctl.last_i_mode == mode
    assert ctl.integral == 10.0
    assert ctl.sat_i is True
    assert u == expected_u


def test_saturation_with_the_error_the_other_way_still_integrates() -> None:
    """SAT_HI with eps < 0 is not 'against' the error: the integral runs and comes down."""
    ctl = _ctl(100.0)  # Ki I = 1.0: output saturated high by the integral
    _pwm(ctl, -0.20, kp=0.5, ki=0.01, u_ff=0.5, dt=1.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(99.8)


def test_unwind_within_one_and_a_half_sensor_steps_when_p_alone_saturates() -> None:
    """eps = 0.06 <= 0.075, FF + Ki I = 0.89 inside the limits, FF + Kp eps + Ki I > 1, I < 0: I moves toward 0, not past it."""
    ctl = _ctl(-1.0)
    u = _pwm(ctl, 0.06, kp=2.0, ki=0.01, u_ff=0.9, dt=1.0, db=0.0)
    assert ctl.last_i_mode == "I:UNWIND(SAT_HI)"
    assert ctl.integral == pytest.approx(-0.94, abs=1e-12)
    assert u == 1.0
    ctl = _ctl(-0.02)  # the unwind never crosses zero
    _pwm(ctl, 0.06, kp=2.0, ki=0.01, u_ff=0.9, dt=1.0, db=0.0)
    assert ctl.last_i_mode == "I:UNWIND(SAT_HI)"
    assert ctl.integral == 0.0
    ctl = _ctl(+1.0)  # same-sign integral: plain skip
    _pwm(ctl, 0.06, kp=2.0, ki=0.01, u_ff=0.9, dt=1.0, db=0.0)
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"
    assert ctl.integral == 1.0


def test_integral_is_bounded_by_two_over_ki() -> None:
    ctl = _ctl(250.0)  # beyond 2 / Ki = 200, saturated high by I but eps < 0: the integral runs and is clamped
    _pwm(ctl, -0.10, kp=0.1, ki=0.01, u_ff=0.5, dt=1.0)
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(200.0)
    assert ctl.u_cmd == 1.0


def test_back_calculation_toward_the_static_limit() -> None:
    """u_max = 0.6, request 1.0: I moves by beta du / Ki, beta = dt / max(Tt, dt), Tt = Kp / Ki = 100 min; I stays >= 0."""
    ctl = _ctl(10.0)
    u = _pwm(ctl, 0.8, kp=1.0, ki=0.01, u_ff=0.2, dt=2.0, u_max=0.6)
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"  # 0.2 + 0.75 + 0.1 = 1.05 > u_max
    assert u == 1.0
    du = ctl.track_output_limit(0.6, 1.0, 0.01, 2.0)
    assert du == pytest.approx(-0.4)
    assert ctl.integral == pytest.approx(10.0 + (2.0 / 100.0) * (-0.4) / 0.01, abs=1e-12)  # 9.2
    assert ctl.last_aw_du == pytest.approx(-0.4)
    assert ctl.u_i == pytest.approx(0.1)  # the decomposition that produced the command is kept

    ctl = _ctl(0.3)  # never across zero
    _pwm(ctl, 0.8, kp=1.0, ki=0.01, u_ff=0.2, dt=2.0, u_max=0.6)
    ctl.track_output_limit(0.6, 1.0, 0.01, 2.0)
    assert ctl.integral == 0.0

    ctl = _ctl(-5.0)  # an excess carried by P alone does not wind the integral the other way
    _pwm(ctl, 0.9, kp=1.5, ki=0.01, u_ff=0.4, dt=2.0, u_max=0.6)
    ctl.track_output_limit(0.6, 1.5, 0.01, 2.0)
    assert ctl.integral == -5.0


def test_back_calculation_is_off_in_hold_and_freeze_and_below_the_limit() -> None:
    ctl = _ctl(10.0)
    _pwm(ctl, 0.8, kp=1.0, ki=0.01, u_ff=0.2, u_max=0.6, hold=True)
    assert ctl.track_output_limit(0.6, 1.0, 0.01, 2.0) == 0.0
    assert ctl.integral == 10.0
    ctl = _ctl(10.0)
    _pwm(ctl, 0.2, kp=1.0, ki=0.01, u_ff=0.2, u_max=0.9)  # request 0.2 + 0.15 + 0.1 = 0.45 < u_max
    assert ctl.track_output_limit(0.9, 1.0, 0.01, 2.0) == 0.0


def test_gain_change_rescales_the_integral_so_that_ki_times_i_is_kept() -> None:
    ctl = _ctl(40.0)
    ctl.transfer_gains(0.010, 0.016, "law")
    assert ctl.integral == pytest.approx(25.0)
    assert 0.016 * ctl.integral == pytest.approx(0.010 * 40.0)


def test_rate_limit_and_boost_and_setpoint_bypass() -> None:
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="rate")
    smartpi._output_initialized = True
    smartpi._last_u_limited = 0.2
    assert smartpi._apply_soft_constraints(1.0, 2.0, False) == pytest.approx(0.2 + 0.25 * 2.0)
    assert smartpi._apply_soft_constraints(0.0, 2.0, False) == pytest.approx(0.0)
    assert smartpi._apply_soft_constraints(1.0, 2.0, True) == pytest.approx(1.0)  # setpoint change: bypass
    assert smartpi._apply_soft_constraints(1.0, 0.0, False) == pytest.approx(1.0)  # dt = 0: bypass


# --- gains and feed-forward --------------------------------------------------------------------------------------------


def test_imc_simc_gains_by_hand() -> None:
    """a = 0.01 C/min, L = 10 min, cycle 5 min, tau 5000 min: lambda = max(30, 10, 5) = 30; Kp = 1/(0.01 * 40) = 2.5;
    Ti = min(5000, 4 * 40) = 160; Ki = 2.5 / 160."""
    kp, ki = imc_simc_gains(0.01, 600.0, 5000.0, 5.0)
    assert kp == pytest.approx(2.5)
    assert ki == pytest.approx(2.5 / 160.0)
    # a valve: lambda = 4 L = 40, Kp = 1 / (0.01 * 50) = 2, Ti = min(5000, 200) = 200
    kp, ki = imc_simc_gains(0.01, 600.0, 5000.0, 5.0, valve_mode_enabled=True)
    assert (kp, ki) == (pytest.approx(2.0), pytest.approx(0.01))
    # lambda floor: L = 1 min, cycle 5 min -> lambda = max(3, 10, 5) = 10
    kp, ki = imc_simc_gains(0.1, 60.0, 5000.0, 5.0)
    assert kp == pytest.approx(1.0 / (0.1 * 11.0))
    # tau bounds Ti
    kp, ki = imc_simc_gains(0.01, 600.0, 100.0, 5.0)
    assert ki == pytest.approx(2.5 / 100.0)
    # clamps
    assert imc_simc_gains(1.0, 600.0, 5000.0, 5.0)[0] == KP_MIN
    assert imc_simc_gains(1e-5, 600.0, 5000.0, 5.0)[0] == KP_MAX
    assert imc_simc_gains(0.01, 600.0, 5000.0, 5.0)[1] <= KI_MAX
    assert imc_simc_gains(1.0, 3600.0, 1e9, 5.0)[1] == KI_MIN  # Kp floor 0.1, Ti = 960: 1e-4 -> floor


def test_near_band_taper_and_governance_freeze_in_the_scheduler() -> None:
    est = SimpleNamespace(a=0.01)
    scheduler = GainScheduler("law")
    kwargs = dict(tau_reliable=True, tau_min=5000.0, estimator=est, delay=GainDelay(600.0, "dead time"), cycle_min=5.0)
    base = scheduler.calculate(in_near_band=False, governance_decision=GovernanceDecision.ADAPT_ON, **kwargs)
    assert (base.kp, base.ki) == (pytest.approx(2.5), pytest.approx(2.5 / 160.0))
    rho = 0.4
    tapered = scheduler.calculate(in_near_band=True, near_band_ratio=rho, governance_decision=GovernanceDecision.ADAPT_ON,
                                  **kwargs)
    assert tapered.kp == pytest.approx(2.5 * (NEAR_BAND_TAPER_KP_MIN + (1 - NEAR_BAND_TAPER_KP_MIN) * rho))
    assert tapered.ki == pytest.approx(2.5 / 160.0 * (NEAR_BAND_TAPER_KI_MIN + (1 - NEAR_BAND_TAPER_KI_MIN) * rho))
    frozen = scheduler.calculate(in_near_band=False, governance_decision=GovernanceDecision.FREEZE, **kwargs)
    assert (frozen.kp, frozen.ki) == (tapered.kp, tapered.ki)  # the previous gains are kept
    scheduler = GainScheduler("law")  # no dead time: safe gains
    safe = scheduler.calculate(tau_reliable=True, tau_min=5000.0, estimator=est, delay=None, in_near_band=False,
                               governance_decision=GovernanceDecision.ADAPT_ON, cycle_min=5.0)
    assert (safe.kp, safe.ki) == (0.55, 0.010)


def test_feed_forward_is_the_static_holding_power_on_the_raw_setpoint() -> None:
    b_over_a = 0.1
    assert compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=18.0, warmup_scale=1.0).u_ff1 == pytest.approx(0.2)
    assert compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=5.0, warmup_scale=1.0).u_ff1 == 1.0  # clamped
    assert compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=18.0, warmup_scale=0.5).u_ff1 == pytest.approx(0.1)
    assert compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=25.0, warmup_scale=1.0).u_ff1 == 0.0
    assert compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=None, warmup_scale=1.0).u_ff1 == 0.0
    # COOL: k_ff < 0 and r < T_out give a positive holding power
    assert compute_ff(k_ff=-b_over_a, target_temp_ff=24.0, ext_temp=26.0, warmup_scale=1.0).u_ff1 == pytest.approx(0.2)
    # a step of the setpoint steps the feed-forward by (b/a) dr
    u1 = compute_ff(k_ff=b_over_a, target_temp_ff=20.0, ext_temp=15.0, warmup_scale=1.0).u_ff1
    u2 = compute_ff(k_ff=b_over_a, target_temp_ff=21.0, ext_temp=15.0, warmup_scale=1.0).u_ff1
    assert u2 - u1 == pytest.approx(b_over_a * 1.0)


# --- series PID with the derivative on the measurement ---------------------------------------------------------------------


@pytest.mark.parametrize("rate", [-0.01, 0.0, +0.01])
def test_series_pid_identity_on_a_straight_ramp(rate) -> None:
    """Straight ramp, w = 1, no dead-band projection, no saturation, every tick RUN: the discrete law is exactly
    u_k = u_ff + Kp [(r - y_k) - H s] + Ki [ sum_j (r - y_j) dt - H (y_k - y_before) ]
    (series PID, derivative on the measurement, Td = H, I-path on the anticipated error)."""
    kp, ki, u_ff = 0.4, 0.01, 0.2
    h_min = H_S / 60.0
    y_before = 19.0
    r = y_before + 0.25  # e stays below c~ = 0.3 on a rising ramp (w = 1); on a falling one the room moves away (w = 1)
    ctl = _ctl(0.0)
    sum_e = 0.0
    for k in range(1, 9):
        y = y_before + rate * k
        c = correction_c(H_S, NOW, y, r, _ramp(rate, y), True)
        assert c == pytest.approx(rate * h_min, abs=1e-9)  # c = s H, no fade
        eps = anticipated_error(r - y, c, VThermHvacMode_HEAT)
        u = _pwm(ctl, eps, kp=kp, ki=ki, u_ff=u_ff, dt=1.0, db=0.0)
        assert ctl.last_i_mode == "I:RUN"
        sum_e += r - y
        # Ki sum (H s) dt = Ki H (y_k - y_before) on a straight ramp: the derivative enters the integral exactly
        i_expected = sum_e - h_min * (y - y_before)
        assert ctl.integral == pytest.approx(i_expected, abs=1e-9)
        assert u == pytest.approx(clamp(u_ff + kp * ((r - y) - c) + ki * i_expected, 0.0, 1.0), abs=1e-9)
    # the gain on y of the parallel form is Kp + Ki H = Kp (1 + H / Ti), Ti = Kp / Ki
    assert kp + ki * h_min == pytest.approx(kp * (1.0 + h_min / (kp / ki)))


def test_series_identity_needs_w_one_and_no_dead_band() -> None:
    """Outside w = 1 the P gain on the slope is not Kp H: in the ramp [c, 2c] it is 2 Kp H (eps = 2 e - 2 c)."""
    y = 19.78
    t1, t2 = y + 0.9, y + 0.9 + 0.1  # e = 1.5 c~ and e = 1.5 c~ + 0.1
    _, e1 = _eps_heat(0.02, y, t1)
    _, e2 = _eps_heat(0.02, y, t2)
    assert (e2 - e1) == pytest.approx(2.0 * 0.1, abs=1e-9)  # d eps / d e = 2 in the ramp
    # and on the slope: d eps / d c~ = -2
    c_a = correction_c(H_S, NOW, y, t1, _ramp(0.02, y), True)
    c_b = correction_c(H_S, NOW, y, t1, _ramp(0.021, y), True)
    eps_a = anticipated_error(t1 - y, c_a, VThermHvacMode_HEAT)
    eps_b = anticipated_error(t1 - y, c_b, VThermHvacMode_HEAT)
    assert (eps_b - eps_a) / (0.001 / 60.0 * H_S) == pytest.approx(-2.0, abs=1e-6)


# --- the anticipation enters the integral ------------------------------------------------------------------------------------------


def test_run_integral_takes_e_minus_c() -> None:
    """The anticipation enters the integral through epsilon: dI = (e - c) dt on every integrating tick."""
    ctl = _ctl(0.0)
    e, c, dt = 0.45, 0.15, 3.0
    _pwm(ctl, e - c, dt=dt)
    assert ctl.integral == pytest.approx((e - c) * dt)
    assert ctl.integral != pytest.approx(e * dt)


def test_non_integrating_modes_lose_the_braking_part_of_the_integral() -> None:
    """Ten ticks with e = 0.45 and c = 0.15 (eps = 0.30), Ki = 0.01.
    RUN: I = 3.0 = e part 4.5 + c part -1.5. FREEZE, HOLD and SKIP integrate nothing: the c part (-1.5 C min, i.e.
    -0.015 of power) is lost with the e part. CLAMP blocks only the positive increments, so a negative eps still
    lets the c part in."""
    e, c = 0.45, 0.15

    def run(**kw) -> float:
        ctl = _ctl(0.0)
        for _ in range(10):
            _pwm(ctl, e - c, **kw)
        return ctl.integral

    assert run() == pytest.approx(10 * (e - c))                  # RUN: 3.0 = 4.5 (e part) - 1.5 (c part)
    assert run(hold=True) == 0.0                                  # HOLD
    assert run(in_deadband=True, core=True) == 0.0               # FREEZE
    assert run(kp=2.0, u_ff=0.8) == 0.0                          # SKIP(SAT_HI): 0.8 + 2 * 0.25 > 1
    # CLAMP: eps in (0, 0.04] adds nothing, eps < 0 adds eps dt: the braking passes, the positive part does not
    ctl = _ctl(0.0)
    for _ in range(10):
        _pwm(ctl, 0.04)
    assert ctl.integral == 0.0
    for _ in range(10):
        _pwm(ctl, -0.02)
    assert ctl.integral == pytest.approx(-0.2)


# --- one full tick through SmartPI.calculate ----------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clk = _Clock()
    monkeypatch.setattr(algo_module, "time", SimpleNamespace(monotonic=clk.monotonic, time=real_time.time))
    return clk


def _smartpi(horizon_s: float | None) -> SmartPI:
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                      name="law")
    force_smartpi_stable_mode(smartpi)
    if horizon_s is not None:
        from custom_components.vtherm_smartpi.smartpi.measure_event import MODE_HEAT, SOURCE_PULSE

        smartpi._start.model.add(
            MODE_HEAT, SOURCE_PULSE,
            {"theta_s": 0.25 * horizon_s, "tau_e_s": 0.75 * horizon_s, "s_s": horizon_s, "split_ok": True}, 0.0,
        )
    return smartpi


def _drive(smartpi: SmartPI, clk: _Clock, temps, target: float) -> list[dict]:
    clk.now = 10_000.0
    rows = []
    for temp in temps:
        clk.now += 60.0
        smartpi.calculate(target_temp=target, current_temp=temp, ext_current_temp=5.0, slope=None,
                          hvac_mode=VThermHvacMode_HEAT)
        rows.append({
            "t": temp, "u_cmd": smartpi.u_cmd, "integral": smartpi.integral, "c": smartpi.anticipation_c,
            "mode": smartpi.ctl.last_i_mode, "kp": smartpi.Kp, "ki": smartpi.Ki, "u_ff": smartpi.ctl.u_ff,
            "error": smartpi.error, "error_p": smartpi.error_p,
        })
    return rows


def test_calculate_tick_equals_the_law_with_anticipation(clock: _Clock) -> None:
    """Rising 0.01 C/min toward 19.9 C, horizon 1800 s (c~ = 0.3): at every tick epsilon = e - c~ w, the integral
    adds epsilon dt and u = clamp(u_ff + Kp eps^p + Ki I). The ticks cover w = 0 (far), the fade ramp and w = 1."""
    temps = [round(19.0 + 0.01 * i, 4) for i in range(45)]
    target = 19.9
    rows = _drive(_smartpi(1800.0), clock, temps, target)
    seen_ramp = False
    prev_i = 0.0
    for k, row in enumerate(rows):
        e = target - row["t"]
        # the slope window is exact on a straight ramp once the history spans half the window (900 s) with 3 samples
        c_tilde = 0.3 if k >= 15 else 0.0
        w = clamp(2.0 - e / c_tilde, 0.0, 1.0) if c_tilde else 0.0
        c = c_tilde * w
        eps = e - c
        assert row["c"] == pytest.approx(c, abs=1e-6), k
        assert row["error"] == pytest.approx(e, abs=1e-9)
        assert row["error_p"] == pytest.approx(eps, abs=1e-6)
        if k > 0:
            assert row["mode"] == "I:RUN"
            assert row["integral"] == pytest.approx(prev_i + eps * 1.0, abs=1e-6), k
        eps_p = 0.0 if abs(eps) <= DB else (abs(eps) - DB) * (1.0 if eps > 0 else -1.0)
        assert row["u_cmd"] == pytest.approx(clamp(row["u_ff"] + row["kp"] * eps_p + row["ki"] * row["integral"], 0.0, 1.0),
                                             abs=1e-6)
        seen_ramp = seen_ramp or (c_tilde and c_tilde <= e <= 2 * c_tilde)
        prev_i = row["integral"]
    assert seen_ramp


def test_calculate_tick_with_anticipation_off_is_the_plain_pi(clock: _Clock) -> None:
    """No horizon: epsilon = e, I += e dt, u = clamp(u_ff + Kp (e - db) + Ki I)."""
    temps = [round(19.0 + 0.01 * i, 4) for i in range(40)]
    target = 19.9
    rows = _drive(_smartpi(None), clock, temps, target)
    prev_i = 0.0
    for k, row in enumerate(rows):
        e = target - row["t"]
        assert row["c"] == 0.0
        assert row["error_p"] == pytest.approx(e, abs=1e-9)
        if k > 0:
            assert row["integral"] == pytest.approx(prev_i + e, abs=1e-9)
        assert row["u_cmd"] == pytest.approx(
            clamp(row["u_ff"] + row["kp"] * (e - DB) + row["ki"] * row["integral"], 0.0, 1.0), abs=1e-9)
        prev_i = row["integral"]
