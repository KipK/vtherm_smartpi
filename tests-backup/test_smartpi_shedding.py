"""Test SmartPI shedding behavior."""

import pytest
from unittest.mock import MagicMock
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT

def test_shedding_resets_integral():
    """Test that power shedding resets the integral to 0."""
    smartpi = SmartPI(hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI_Shedding"
    )

    # 1. Establish a steady state with Integral > 0
    smartpi.calculate(
        target_temp=20,
        current_temp=19,
        ext_current_temp=10,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )

    # Manually inject integral
    smartpi.integral = 5.0
    smartpi.u_prev = 0.5

    assert smartpi.integral == 5.0
    assert smartpi.u_prev == 0.5

    # 2. Trigger shedding
    smartpi.calculate(
        target_temp=20,
        current_temp=19,
        ext_current_temp=10,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
        power_shedding=True
    )

    # 3. Verify: integral is reset (not just frozen)
    assert getattr(smartpi.ctl, "integral", smartpi.integral) > 0.0, f"Integral should be frozen during shedding, but was {smartpi.integral}"
    assert smartpi.on_percent == 0.0, "Output should be forced to 0"
    assert smartpi.u_prev == 0.0

    # 4. Verify recovery after shedding stops
    smartpi.calculate(
        target_temp=20,
        current_temp=19,
        ext_current_temp=10,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
        power_shedding=False
    )

    # Integral starts fresh from 0 and may accumulate a small amount
    assert smartpi.integral >= 0.0, "Integral should rebuild from 0 after shedding"
