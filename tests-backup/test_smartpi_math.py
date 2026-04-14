"""Test SmartPI learning logic and frequency."""
import logging
from datetime import timedelta, datetime
from unittest.mock import patch, MagicMock
import time

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.versatile_thermostat.const import (
    CONF_CYCLE_MIN,
    CONF_PROP_FUNCTION,
    PROPORTIONAL_FUNCTION_SMART_PI,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
)
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT

from .commons import create_thermostat, send_temperature_change_event

from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.versatile_thermostat.const import (
    DOMAIN, 
    CONF_NAME, 
    CONF_THERMOSTAT_TYPE
)

@pytest.fixture
async def smartpi_thermostat(hass: HomeAssistant):
    """Create a SmartPI thermostat fixture."""
    entry_data = {
        CONF_NAME: "Test SmartPI",
        CONF_PROP_FUNCTION: PROPORTIONAL_FUNCTION_SMART_PI,
        CONF_CYCLE_MIN: 5,
        CONF_EXTERNAL_TEMP_SENSOR: "sensor.external_temp",
        CONF_TEMP_SENSOR: "sensor.indoor_temp",
        CONF_THERMOSTAT_TYPE: "thermostat_over_switch",  # Missing in orig dict
        "thermostat_over_switch": "switch.test_heater",
        # Minimal defaults
        "minimal_activation_delay": 0,
        "minimal_deactivation_delay": 0,
    }
    
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test SmartPI",
        data=entry_data,
        unique_id="test_smartpi_uid"
    )
    
    # Create the underlying switch
    from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
    from tests.commons import MockSwitch, register_mock_entity
    
    mock_switch = MockSwitch(hass, "test_heater", "Test Heater")
    mock_switch.entity_id = "switch.test_heater"
    await register_mock_entity(hass, mock_switch, SWITCH_DOMAIN)
    
    entity = await create_thermostat(hass, mock_entry, "climate.test_smartpi")
    if entity is None:
        # Retry search
        from tests.commons import search_entity
        from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
        for _ in range(10):
            await hass.async_block_till_done()
            entity = search_entity(hass, "climate.test_smartpi", CLIMATE_DOMAIN)
            if entity:
                break
    return entity



async def test_smartpi_math_with_mocked_time(hass: HomeAssistant):
    """Test math with controlled time."""
    with patch('custom_components.versatile_thermostat.prop_algo_smartpi.time.monotonic') as mock_time:
        mock_time.return_value = 1000.0
        
        algo = SmartPI(
            hass=MagicMock(),
            cycle_min=10, 
            name="test_pi",
            minimal_activation_delay=0,
            minimal_deactivation_delay=0
        )
        algo.est = MagicMock()
        algo.est.learn_ok_count_b = 0  # must be int, not MagicMock, for '<' comparison
        # Simulate deadtimes already learned so the bootstrap gate does not block A/B collection
        algo.dt_est.deadtime_heat_reliable = True
        algo.dt_est.deadtime_heat_s = 30.0
        algo.dt_est.deadtime_cool_reliable = True
        algo.dt_est.deadtime_cool_s = 30.0

        # Remove len mocking, just need dt_est history
        # from custom_components.versatile_thermostat.prop_algo_smartpi import AB_HISTORY_SIZE
        # algo.est.a_meas_hist.__len__.return_value = AB_HISTORY_SIZE + 1
        # algo.est.b_meas_hist.__len__.return_value = AB_HISTORY_SIZE + 1

        # Populate dt_est history for Robust calculation (required for learning to proceed)
        # Use simple decrease 20 -> 19 over 10 mins approx
        start_t = 1000.0
        for i in range(11):
            t = start_t + i * 60.0
            temp = 20.0 - (i / 10.0)
            algo.dt_est._tin_history.append((t, temp))
        
        start_ts_dt = datetime.fromtimestamp(1000.0) # mock time is 1000
        algo._current_cycle_params = {
            "timestamp": start_ts_dt,
            "on_percent": 0.0,
            "temp_in": 20.0,
            "temp_ext": 0.0,
            "hvac_mode": VThermHvacMode_HEAT
        }
        algo._cycle_start_date = start_ts_dt
        
        # 1. Start learning window at T=1000 + epsilon (e.g. 1 sec)
        # minimal dt > 0.001 min. 0.001 min = 0.06 sec.
        # Let's use 10 sec = 0.16 min.
        mock_time.return_value = 1010.0

        # Clear any pre-built history so the slope gate finds nothing on the first
        # tick and keeps the window open (extending).  The real history for Case 1
        # submission is rebuilt below (lines ~138-144) before the second call.
        algo.dt_est._tin_history.clear()

        algo.update_learning(
            dt_min=10.0/60.0, # 10 sec
            current_temp=20.0, # minimal change
            ext_temp=0.0,
            u_active=0.0, # OFF
            setpoint_changed=False
        )
        assert algo.learn_win_active is True
        assert algo.learn_T_int_start == 20.0
        
        # Advance time by 20 mins (1200s); tin_history built below provides enough samples
        mock_time.return_value = 2200.0
        
        # End cycle: Temp dropped to 19.0 over 20 mins
        # dT = -1.0, dt = 20min -> dT/dt = -0.05
        # History generation for 20 mins
        algo.dt_est._tin_history.clear() 
        # Start 1000. End 2200.
        start_t = 1000.0
        for i in range(21):
            t = start_t + i * 60.0
            temp = 20.0 - (i / 20.0) # 20.0 -> 19.0
            algo.dt_est._tin_history.append((t, temp))

        # Call update_learning to trigger learn
        algo.update_learning(
            dt_min=1190.0/60.0, # 19m 50s
            current_temp=19.0,
            ext_temp=0.0,
            u_active=0.0,
            setpoint_changed=False
        )
        
        if algo.est.learn.call_count == 0:
            print(f"DEBUG: learn_last_reason: {algo.est.learn_last_reason}")
            
        # Should learn B
        algo.est.learn.assert_called_once()
        call_args = algo.est.learn.call_args[1]
        assert call_args['u'] == 0.0
        # Slope is -0.05
        assert abs(call_args['dT_int_per_min'] - (-0.05)) < 0.005
        
        # Reset
        algo.est.learn.reset_mock()
        
        # Case 2: High power, heating up -> Learn A
        
        start_ts_2 = datetime.fromtimestamp(2200.0)
        algo._current_cycle_params = {
            "timestamp": start_ts_2,
            "on_percent": 1.0, 
            "temp_in": 19.0,
            "temp_ext": 0.0,
            "hvac_mode": VThermHvacMode_HEAT
        }
        algo._cycle_start_date = start_ts_2
        
        # Start call for Case 2 (10 sec later)
        mock_time.return_value = 2210.0
        algo.update_learning(
            dt_min=10.0/60.0,
            current_temp=19.0,
            ext_temp=0.0,
            u_active=1.0, # ON
            setpoint_changed=False
        )
        assert algo.learn_T_int_start == 19.0
        
        # Advance time by 10 mins (600s) -> 2800.0
        # ON duration is 8 mins min, so 10 mins is OK.
        mock_time.return_value = 2800.0 
        
        # Populate history for ON cycle (heating 19->20)
        start_t_2 = 2200.0
        for i in range(11):
            t = start_t_2 + i * 60.0
            temp = 19.0 + (i/10.0)
            algo.dt_est._tin_history.append((t, temp))
        
        # End cycle: Temp rose to 20.0
        # dT = +1.0, dT/dt = 0.1
        
        algo.update_learning(
            dt_min=10.0,
            current_temp=20.0,
            ext_temp=0.0,
            u_active=1.0,
            setpoint_changed=False
        )
        
        algo.est.learn.assert_called_once()
        call_args = algo.est.learn.call_args[1]
        assert call_args['u'] == 1.0
        assert abs(call_args['dT_int_per_min'] - 0.1) < 0.03
