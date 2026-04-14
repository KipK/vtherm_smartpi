
import pytest
from unittest.mock import MagicMock
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI

def test_smartpi_update_realized_power():
    """Test the update_realized_power method of SmartPI."""
    hass = MagicMock()

    algo = SmartPI(
        hass=hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestAlgo",
        max_on_percent=1.0,
        deadband_c=0.0
    )

    # Pre-set some state
    algo.Ki = 0.01  # Ensure Ki > KI_MIN so tracking logic runs
    algo.ctl.last_i_mode = "I:RUN"
    algo.deadband_mgr.in_deadband = False

    # 1. Test that u_prev is NOT modified by update_realized_power
    algo.u_prev = 0.5
    algo.update_realized_power(realized_percent=0.8, forced_by_timing=False, dt_min=5.0)

    assert algo._last_forced_by_timing is False
    assert algo.u_prev == 0.5  # u_prev unchanged (only finalize_cycle/calculate() sets it)

    # 2. Test forced by timing — tracking must be skipped
    algo.update_realized_power(realized_percent=0.0, forced_by_timing=True, dt_min=5.0)

    assert algo._last_forced_by_timing is False  # set by update_timing_constraints, not here
    assert algo._last_aw_du == 0.0
    assert algo.u_prev == 0.5  # u_prev still unchanged

    # 3. Test that reference is u_model = u_ff + Kp*e_p + Ki*I (not _last_u_limited)
    # When u_applied > u_model, thermal invariant clamps du to 0 (no upward AW).
    # To test du < 0 path: set u_applied < u_model.
    algo.Kp = 0.5
    algo.Ki = 0.05
    algo.ctl.u_ff = 0.0
    algo.ctl.last_error_p_db = 0.3   # Kp*e_p = 0.5*0.3 = 0.15
    algo.ctl.integral = 0.5       # Ki*I  = 0.05*0.5 = 0.025
    # u_model = 0 + 0.15 + 0.025 = 0.175
    # val = 0.1 → du = 0.1 - 0.175 = -0.075
    algo.update_realized_power(realized_percent=0.1, forced_by_timing=False, dt_min=1.0)

    assert abs(algo._last_aw_du - (-0.075)) < 1e-6, (
        f"Expected du=-0.075 (val - u_model), got {algo._last_aw_du}"
    )
