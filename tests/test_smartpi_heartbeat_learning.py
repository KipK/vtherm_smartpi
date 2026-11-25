"""Test SmartPI Heartbeat Learning Mechanism."""
import pytest
from unittest.mock import MagicMock, patch
import time
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT

def test_heartbeat_accumulation():
    """Verify that learning window accumulates over multiple calculate calls."""
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="TestHB")
    # A response model of a fast emitter (settling waits of seconds) so that the learning stage opens A/B windows
    smartpi._start.model.seed(str(VThermHvacMode_HEAT), 0.0, 1.0)

    # Initial state
    # Pre-warmup to stabilize power output (avoid power instability reset)
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.0,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )
    # We advanced time? No, it used current time.
    # Now reset last_calculate_time for the test sequence
    smartpi._last_calculate_time = time.monotonic() - 60.0
    # Clear the reboot learning freeze: this test doesn't concern reboot behavior
    smartpi.learn_win.set_learning_resume_ts(None)
    
    # 1. Start accumulation
    # Call calculate: should start window (using the power from warmup)
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.0,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )
    
    assert smartpi.learn_win_active
    assert smartpi.learn_t_int_s > 0
    first_duration = smartpi.learn_t_int_s
    
    # 2. Add more time (simulation)
    # Patch time to advance 60s
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic") as mock_time:
        mock_time.return_value = smartpi._last_calculate_time + 60.0
        
        smartpi.calculate(
            target_temp=20.0,
            current_temp=19.0, # Keep temp constant to ensure power constant
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
    assert smartpi.learn_t_int_s > first_duration
    assert smartpi.learn_win_active

def test_heartbeat_learning_trigger_on_slope_quality():
    """Verify that learning submits once OLS slope is robust."""
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="TestHB_Trigger")
    # A response model of a fast emitter (settling waits of seconds) so that the learning stage opens A/B windows
    smartpi._start.model.seed(str(VThermHvacMode_HEAT), 0.0, 1.0)
    smartpi.learn_win.set_learning_resume_ts(None)

    # Pre-populate the temperature history with 20 samples, 1 min apart, rising temperature.
    # (i=0 = oldest = 19 min ago, i=19 = most recent = now)
    # Amplitude = 0.95°C >> DT_DERIVATIVE_MIN_ABS (0.03°C); slope ≈ +0.05°C/min.
    base_now = time.monotonic()
    smartpi.learn_win.temperatures.raw.extend(
        (base_now - (19 - i) * 60, 19.0 + i * 0.05) for i in range(20)
    )

    # Single call with 20-min window: start_ts goes back 20 min, all 20 samples in range.
    # The slope gate should pass immediately and the window should submit.
    smartpi.update_learning(20.0, 20.0, 10.0, 1.0)

    # Slope is robust: window must have submitted and reset.
    assert not smartpi.learn_win_active
    assert "learned" in smartpi.est.learn_last_reason or "skip" in smartpi.est.learn_last_reason

def test_learning_rejects_change_tick_and_reopens_during_ascent():
    """Reject the causal change tick, then allow A during early ascent."""
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="TestHB_Reset")
    # A response model of a fast emitter (settling waits of seconds) so that the learning stage opens A/B windows
    smartpi._start.model.seed(str(VThermHvacMode_HEAT), 0.0, 1.0)

    # Start window
    smartpi.update_learning(1.0, 19.0, 10.0, 1.0, hvac_mode=VThermHvacMode_HEAT)
    assert smartpi.learn_win_active

    # The exact change tick cannot belong to a window spanning two regimes.
    smartpi.update_learning(1.0, 19.0, 10.0, 1.0, setpoint_changed=True, hvac_mode=VThermHvacMode_HEAT)
    assert not smartpi.learn_win_active
    assert smartpi.est.learn_last_reason == "skip: setpoint transition"

    # Once the change tick has passed, the strong ascent is valid A excitation.
    smartpi.update_learning(1.0, 19.0, 10.0, 1.0, setpoint_changed=False, hvac_mode=VThermHvacMode_HEAT)
    assert smartpi.learn_win_active

def test_heartbeat_integration_in_calculate():
    """Verify calculate() calls update_learning()."""
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="TestHB_Integration")
    
    # Mock update_learning to verify call
    smartpi.update_learning = MagicMock()
    
    # First run (dt=0) -> No call
    smartpi.calculate(target_temp=20, current_temp=19, ext_current_temp=10, slope=0, hvac_mode=VThermHvacMode_HEAT)
    smartpi.update_learning.assert_not_called()
    
    # Second run (dt > 0)
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic") as mock_time:
        mock_time.return_value = smartpi._last_calculate_time + 60.0
        smartpi.calculate(target_temp=20, current_temp=19, ext_current_temp=10, slope=0, hvac_mode=VThermHvacMode_HEAT)
        
    smartpi.update_learning.assert_called_once()
