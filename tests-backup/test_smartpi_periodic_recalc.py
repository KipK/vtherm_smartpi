""" Test the SmartPI periodic recalculation timer """
import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.components.climate import HVACMode
from homeassistant.util import dt as dt_util

# Correct import for firing time changed events in tests
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.versatile_thermostat.base_thermostat import BaseThermostat
from custom_components.versatile_thermostat.const import (
    DOMAIN,
    CONF_NAME,
    CONF_THERMOSTAT_TYPE,
    CONF_THERMOSTAT_SWITCH,
    CONF_TEMP_SENSOR,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_CYCLE_MIN,
    CONF_TEMP_MIN,
    CONF_TEMP_MAX,
    CONF_USE_WINDOW_FEATURE,
    CONF_USE_MOTION_FEATURE,
    CONF_USE_POWER_FEATURE,
    CONF_USE_PRESENCE_FEATURE,
    CONF_UNDERLYING_LIST,
    CONF_PROP_FUNCTION,
    PROPORTIONAL_FUNCTION_SMART_PI,
    CONF_TPI_COEF_INT,
    CONF_TPI_COEF_EXT,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
)
from custom_components.versatile_thermostat.smartpi.const import SMARTPI_RECALC_INTERVAL_SEC

from .commons import create_thermostat

@pytest.mark.parametrize("expected_lingering_tasks", [True])
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_smartpi_periodic_recalc(
    hass: HomeAssistant, skip_hass_states_is_state: None, skip_turn_on_off_heater
):
    """Test that SmartPI recalculates periodically independently of sensor updates."""
    
    # 1. Setup VTherm with SmartPI
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPIRecalcTest",
        unique_id="smartpi_recalc_test",
        data={
            CONF_NAME: "SmartPIRecalcTest",
            CONF_THERMOSTAT_TYPE: CONF_THERMOSTAT_SWITCH,
            CONF_TEMP_SENSOR: "sensor.mock_temp_sensor",
            CONF_EXTERNAL_TEMP_SENSOR: "sensor.mock_ext_temp_sensor",
            CONF_CYCLE_MIN: 10, # 10 min cycle
            CONF_TEMP_MIN: 15,
            CONF_TEMP_MAX: 30,
            CONF_USE_WINDOW_FEATURE: False,
            CONF_USE_MOTION_FEATURE: False,
            CONF_USE_POWER_FEATURE: False,
            CONF_USE_PRESENCE_FEATURE: False,
            CONF_UNDERLYING_LIST: ["switch.mock_switch"],
            CONF_PROP_FUNCTION: PROPORTIONAL_FUNCTION_SMART_PI,
            CONF_TPI_COEF_INT: 0.6,
            CONF_TPI_COEF_EXT: 0.01,
            CONF_MINIMAL_ACTIVATION_DELAY: 30,
            CONF_MINIMAL_DEACTIVATION_DELAY: 0,
        },
    )

    hass.states.async_set("switch.mock_switch", "off")

    entity: BaseThermostat = await create_thermostat(
        hass, entry, "climate.smartpirecalctest"
    )
    assert entity
    assert entity._prop_algorithm
    assert entity._proportional_function == PROPORTIONAL_FUNCTION_SMART_PI
    
    
    # Initialize the entity (simulating VTherm API startup)
    await entity.async_startup(None)
    
    # Spy on async_control_heating method (this is what the periodic timer calls)
    with patch.object(entity, 'async_control_heating', wraps=entity.async_control_heating) as mock_control:
        
        # 2. Set to HEAT mode to start the timer
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        await hass.async_block_till_done()

        # Timer should be started
        assert entity._smartpi_recalc_timer_remove is not None, \
            f"Timer not started. Mode={entity.vtherm_hvac_mode}, " \
            f"PropFunc={entity._proportional_function}, " \
            f"SmartPI={entity._prop_algorithm}"
        
        # Reset mock to ignore initial calls during startup/mode change
        mock_control.reset_mock()
        
        # 3. Advance time by SMARTPI_RECALC_INTERVAL_SEC + 1 second
        # We need to fire time change event
        future = dt_util.utcnow() + timedelta(seconds=SMARTPI_RECALC_INTERVAL_SEC + 1)
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done()
        
        # 4. Verify async_control_heating was called with timestamp=None
        assert mock_control.called, "async_control_heating() should be called by the periodic timer"
        assert mock_control.call_count >= 1
        
        # Reset for next check
        mock_control.reset_mock()
        
        # 5. Advance time again
        future = future + timedelta(seconds=SMARTPI_RECALC_INTERVAL_SEC + 1)
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done()
        
        assert mock_control.called, "async_control_heating() should be called again"
        
        # 6. Turn OFF and verify timer is stopped
        await entity.async_set_hvac_mode(HVACMode.OFF)
        await hass.async_block_till_done()
        
        assert entity._smartpi_recalc_timer_remove is None
        
        # Reset mock
        mock_control.reset_mock()
        
        # 7. Advance time and verify NO call
        future = future + timedelta(seconds=SMARTPI_RECALC_INTERVAL_SEC + 1)
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done()
        
        assert not mock_control.called, "async_control_heating() should NOT be called when OFF"

    entity.remove_thermostat()
