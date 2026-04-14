"""Test for SmartPI Negative Integral Bug (Window close)."""
import pytest
from unittest.mock import MagicMock
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.smartpi.const import AB_HISTORY_SIZE
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF

def test_smartpi_window_negative_integral_bug():
    """
    Test reproduction and fix for the Negative Integral Spike on Window Close.
    
    Scenario:
    1. Thermostat in HEAT mode, steady state, inside deadband.
    2. Window Opens -> HVAC_MODE_OFF.
    3. Window Closes -> HVAC_MODE_HEAT (Resume).
    4. Temperature has dropped significantly (Exit Deadband).
    
    Expected Behavior (Fixed):
    - Upon resume, the algorithm should NOT trigger Bumpless Transfer because deadband state should have been reset to False.
    - Integral should start at 0.0 (or be calculated normally), NOT jump to a massive negative value.
    
    Bug Behavior (Unfixed):
    - Deadband state remains True during OFF.
    - Resume triggers "Deadband Exit" -> Bumpless Transfer.
    - u_prev is 0.0 (from OFF).
    - Calculated Integral = (0 - Kp*Error)/Ki  -> Massive Negative Value (e.g. -800).
    """
    
    # Setup SmartPI
    hass = MagicMock()
    # High Ki to make the potential negative spike obvious, Low Kp
    algo = SmartPI(
        hass=hass, 
        cycle_min=10, 
        minimal_activation_delay=0, 
        minimal_deactivation_delay=0,
        name="TestVTherm",
        deadband_c=0.5
    )

    # FORCE RELIABLE STATE (Simulate trained thermostat)
    algo._tau_reliable = True
    algo.est = MagicMock()
    algo.est.a_meas_hist.__len__.return_value = AB_HISTORY_SIZE + 1
    algo.est.b_meas_hist.__len__.return_value = AB_HISTORY_SIZE + 1
    algo.est.tau_reliability = MagicMock(return_value=MagicMock(reliable=True, tau_min=30))
    # Mock phase property or set internal attribute if possible. 
    # SmartPI.phase is a property based on learn counts.
    # We can just mock the property on the class or instance if needed, 
    # OR simpler: just manually override _tau_reliable AFTER calculate starts? 
    # No, calculate overrides it.
    
    # Let's mock the 'est' object properly
    algo.est.learn_ok_count_a = 20
    algo.est.learn_ok_count_b = 20
    algo.est.learn_ok_count = 50
    algo.est.a = 0.001
    algo.est.b = 0.001
    
    # Force deadtime reliability to avoid automatic CALIBRATION phase
    algo.dt_est.deadtime_heat_reliable = True
    algo.dt_est.deadtime_cool_reliable = True
    algo.dt_est.deadtime_heat_s = 600.0
    algo.dt_est.deadtime_cool_s = 600.0
    import time
    algo.calibration_mgr.last_calibration_time = time.time()
    
    # 1. Initialize in HEAT mode, reach Deadband
    # Target = 20, Current = 20 -> Error = 0
    algo.calculate(
        target_temp=20.0, 
        current_temp=20.0, 
        ext_current_temp=10.0, 
        slope=0, 
        hvac_mode=VThermHvacMode_HEAT
    )
    
    # Verify we are in deadband
    assert algo.in_deadband is True
    # Simulate some integral accumulation (though 0 error means 0 change, let's say it had some)
    algo.integral = 0.5
    algo.calculate(
        target_temp=20.0, 
        current_temp=20.0, 
        ext_current_temp=10.0, 
        slope=0, 
        hvac_mode=VThermHvacMode_HEAT
    )
    # Check internal flags
    assert algo.in_deadband is True
    
    # 2. Window Open -> OFF
    # During OFF, integral is reset to 0.
    # CRITICAL: deadband state MUST be reset to False.
    algo.calculate(
        target_temp=20.0, 
        current_temp=18.0, # Temp drops
        ext_current_temp=10.0, 
        slope=0, 
        hvac_mode=VThermHvacMode_OFF
    )
    
    assert getattr(algo.ctl, "integral", algo.integral) == 0.5
    assert algo._on_percent == 0.0
    
    # VERIFY FIX: Check if deadband state was reset
    # If this fails, the bug is present
    assert algo.in_deadband is False, "Bug Detected: deadband state was not reset to False during OFF mode"
    
    # 3. Window Close -> HEAT (Resume)
    # Temp has dropped to 15.0 (Cold!)
    # Target 20.0 -> Error = +5.0
    # Current = 15.0 -> Outside Deadband (0.5)
    algo.calculate(
        target_temp=20.0, 
        current_temp=15.0, 
        ext_current_temp=10.0, 
        slope=0, 
        hvac_mode=VThermHvacMode_HEAT
    )
    
    # 4. Check Result
    # Error = 5.0
    # Kp (approx) = 0.01 * aggressiveness (simplified, actual Kp depends on logic)
    # If Bumpless Transfer triggered:
    # i_bumpless = (u_prev - u_ff - Kp*e) / Ki = (0 - 0 - positive) / Ki = NEGATIVE
    
    print(f"Integral after resume: {algo.integral}")
    print(f"On Percent after resume: {algo.on_percent}")
    
    # Integral should be POSITIVE (accumulating error) or 0 (start), definitely NOT negative
    # With dt=0 (first run logic might skip integral), but subsequent calls would show.
    # But bumpless transfer happens INSTANTLY on deadband exit.
    
    assert algo.integral >= 0.0, f"Integral spiked to negative value: {algo.integral}. Bumpless transfer wrongly triggered!"
    
    # Ensure it's heating (Power > 0)
    # FeedForward might be 0 (no learning), but Proportional term should be active
    # P = Kp * 5.0. 
    # If Integral is -800, then P + I < 0 -> Output 0.
    # If Integral is 0, P > 0 -> Output > 0.
    
    assert algo.on_percent > 0.0, "Thermostat should be heating after window close!"
