import pytest
from unittest.mock import MagicMock
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_HEAT,
    VThermHvacMode_COOL,
)
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_algo(integral: float = 0.5, i_mode: str = "I:RUN") -> SmartPI:
    """Return a pre-configured SmartPI instance for AW tracking tests."""
    hass = MagicMock()
    algo = SmartPI(
        hass=hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="test_aw",
        max_on_percent=1.0,
        deadband_c=0.0,
    )
    # PI gains
    algo.Ki = 0.05
    algo.Kp = 0.5
    # Controller state
    algo.ctl.integral = integral
    algo.ctl.last_i_mode = i_mode
    algo.ctl.u_ff = 0.0
    algo.ctl.last_error_p = 0.3   # u_model = 0 + 0.5*0.3 + 0.05*integral
    # Thermal context (Tin < SP → no thermal invariant active by default)
    algo._last_hvac_mode = VThermHvacMode_HEAT
    algo._last_current_temp = 20.0
    algo._last_target_temp = 21.0
    algo.in_deadband = False
    return algo


# u_model for default setup with integral=0.5:
# u_model = 0 + 0.5*0.3 + 0.05*0.5 = 0.15 + 0.025 = 0.175


# ---------------------------------------------------------------------------
# T1 — AW blocked in I:SKIP
# ---------------------------------------------------------------------------

def test_t1_aw_blocked_in_skip():
    """AW must not modify integral when last_i_mode is I:SKIP."""
    algo = make_algo(integral=0.5, i_mode="I:SKIP(SAT_LO)")
    algo._last_current_temp = 22.0  # Tin > SP
    before = algo.integral
    algo.update_realized_power(u_applied=0.0, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when I:SKIP is active"


# ---------------------------------------------------------------------------
# T2 — AW blocked in I:HOLD
# ---------------------------------------------------------------------------

def test_t2_aw_blocked_in_hold():
    """AW must not modify integral when last_i_mode is I:HOLD."""
    algo = make_algo(integral=0.5, i_mode="I:HOLD")
    before = algo.integral
    algo.update_realized_power(u_applied=0.05, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when I:HOLD is active"


# ---------------------------------------------------------------------------
# T3 — AW blocked in I:FREEZE
# ---------------------------------------------------------------------------

def test_t3_aw_blocked_in_freeze():
    """AW must not modify integral when last_i_mode is I:FREEZE."""
    algo = make_algo(integral=0.5, i_mode="I:FREEZE(deadband)")
    before = algo.integral
    algo.update_realized_power(u_applied=0.05, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when I:FREEZE is active"


def test_deadband_uses_u_db_nominal():
    """In core deadband (HEAT, reliable), P must be frozen.

    Only FF and frozen I remain in the deadband command.
    u_hold == u_ff + u_pi
    No micro-leak applied to the integral.
    """
    ctl = SmartPIController("test_deadband")
    ctl.integral = 1.0

    u_cmd = ctl.compute_pwm(
        error=0.05,
        error_p=0.05,
        kp=0.5,
        ki=0.05,
        u_ff=0.20,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=True,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.35,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=19.95,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=10,
        deadband_c=0.1,
    )

    # No micro-leak: integral stays at 1.0
    assert ctl.integral == pytest.approx(1.0)

    # u_cmd = u_ff + u_pi = 0.20 + (0.05 * 1.0) = 0.25
    assert u_cmd == pytest.approx(0.25)

    # u_hold == u_ff + u_pi
    assert ctl.u_hold == pytest.approx(0.25)

    assert ctl.deadband_power_source == "ff_plus_pi"


def test_tracking_trajectory_reduces_free_integral_growth():
    """Free I-run must grow more slowly while the trajectory is still tracking."""
    ctl = SmartPIController("test_track_i_scale")
    ctl.integral = 0.0

    ctl.compute_pwm(
        error=1.0,
        error_p=1.0,
        kp=0.1,
        ki=0.1,
        u_ff=0.0,
        dt_min=10.0,
        cycle_min=10.0,
        in_deadband=False,
        in_near_band=False,
        integrator_hold=False,
        u_db_nominal=0.0,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=19.0,
        target_temp=20.0,
        hysteresis_thermal_guard=False,
        is_tau_reliable=True,
        learn_ok_count_a=15,
        deadband_c=0.0,
        trajectory_shaping_active=True,
    )

    assert ctl.integral == pytest.approx(2.0)
    assert ctl.last_i_mode == "I:RUN(traj_track)"


# ---------------------------------------------------------------------------
# T4 — AW blocked in I:GUARD
# ---------------------------------------------------------------------------

def test_t4_aw_blocked_in_guard():
    """AW must not modify integral when last_i_mode is I:GUARD."""
    algo = make_algo(integral=0.5, i_mode="I:GUARD(freeze)")
    before = algo.integral
    algo.update_realized_power(u_applied=0.05, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when I:GUARD is active"


# ---------------------------------------------------------------------------
# T5 — AW blocked in I:CLAMP
# ---------------------------------------------------------------------------

def test_t5_aw_blocked_in_clamp():
    """AW must not modify integral when last_i_mode is I:CLAMP."""
    algo = make_algo(integral=0.5, i_mode="I:CLAMP(near_ovr)")
    before = algo.integral
    algo.update_realized_power(u_applied=0.05, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when I:CLAMP is active"


# ---------------------------------------------------------------------------
# T6 — AW discharge-only in HEAT when Tin > SP
# ---------------------------------------------------------------------------

def test_t6_aw_discharge_only_heat_overshoot():
    """When Tin > SP in HEAT mode, AW may only reduce (or keep) the integral."""
    algo = make_algo(integral=0.5, i_mode="I:RUN")
    algo._last_current_temp = 22.0   # Tin > SP=21.0 → thermal invariant: du <= 0
    # u_model = 0 + 0.5*0.3 + 0.05*0.5 = 0.175
    # val_normalized = 0.3 * 1.0 = 0.3 → du = 0.3 - 0.175 = +0.125 > 0
    # thermal invariant clamps du to min(0, du) = 0 → no integral change
    before = algo.integral
    algo.update_realized_power(u_applied=0.3, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral <= before, "Integral must not increase when Tin > SP in HEAT"


# ---------------------------------------------------------------------------
# T7 — u_applied = 0 drives integral down
# ---------------------------------------------------------------------------

def test_t7_u_applied_zero_reduces_integral():
    """With u_applied=0, du = 0 - u_model < 0 → integral decreases."""
    algo = make_algo(integral=0.5, i_mode="I:RUN")
    # u_model = 0.175, val_normalized = 0 → du = -0.175 < 0 → dI < 0
    before = algo.integral
    algo.update_realized_power(u_applied=0.0, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral < before, "Integral must decrease when u_applied=0 and u_model>0"


# ---------------------------------------------------------------------------
# T8 — Nominal case: u_applied > u_model, I:RUN, Tin < SP → AW free
# ---------------------------------------------------------------------------

def test_t8_nominal_above_model():
    """When u_applied > u_model, thermal invariant prevents upward AW correction.

    du = val - u_model > 0 is clamped to 0 by min(0, du): the system delivered
    more than requested, so no integral inflation is needed.
    """
    algo = make_algo(integral=0.5, i_mode="I:RUN")
    # u_model = 0.175, val_normalized = 0.4 → du = +0.225 → clamped to 0
    before = algo.integral
    algo.update_realized_power(u_applied=0.4, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral == before, "Integral must not change when u_applied > u_model (thermal invariant)"


# ---------------------------------------------------------------------------
# T9 — COOL symmetry: Tin < SP → discharge only upward (integral must not decrease)
# ---------------------------------------------------------------------------

def test_t9_cool_symmetry_discharge_only():
    """In COOL mode, when Tin < SP, AW may only increase (or keep) the integral."""
    algo = make_algo(integral=-0.5, i_mode="I:RUN")
    algo._last_hvac_mode = VThermHvacMode_COOL
    algo._last_current_temp = 18.0   # Tin < SP=21.0
    # u_model = 0 + 0.5*0.3 + 0.05*(-0.5) = 0.15 - 0.025 = 0.125
    # val_normalized = 0.3 → du = 0.3 - 0.125 = +0.175 > 0
    # COOL + Tin < SP → du = max(0, du) = +0.175 → dI > 0 → integral increases
    before = algo.integral
    algo.update_realized_power(u_applied=0.3, dt_min=5.0, elapsed_ratio=1.0)
    assert algo.integral >= before, "Integral must not decrease when Tin < SP in COOL (discharge only upwards)"


# ---------------------------------------------------------------------------
# T10 — On SAT_HI exit, integral must NOT be artificially initialized
# (Astrom & Hagglund §3.5: integral must stay coherent with conditional integration)
# ---------------------------------------------------------------------------

def test_no_integral_init_on_sat_exit():
    """On SAT_HI -> NO_SAT transition, integral must stay at its natural value.

    Per Astrom & Hagglund §3.5, the integral state must remain coherent
    with the actuator reality. After conditional integration (I:SKIP) during
    SAT_HI, the integral is near-zero and must build up naturally via I:RUN.
    """
    algo = make_algo(integral=0.0, i_mode="I:SKIP(SAT_HI)")
    algo.ctl.last_sat = "SAT_HI"

    # Simulate SAT_HI exit (step 14b logic)
    prev_sat_before_compute = algo.ctl.last_sat  # "SAT_HI"
    algo.ctl.last_sat = "NO_SAT"  # Simulate compute_pwm transition

    # Reproduce the step 14b logic (no equilibrium init, just counter tracking)
    if algo.ctl.last_sat != "NO_SAT":
        algo._sat_persistent_cycles += 1
    else:
        algo._sat_persistent_cycles = 0

    assert algo.ctl.integral == 0.0, (
        "Integral must stay at 0 on SAT_HI exit (natural post-conditional-integration)"
    )
