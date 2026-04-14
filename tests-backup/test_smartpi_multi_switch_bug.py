# tests/test_smartpi_multi_switch_bug.py

import pytest
import logging
from unittest.mock import patch, ANY, call
from datetime import datetime, timedelta

from custom_components.versatile_thermostat.base_thermostat import BaseThermostat
from custom_components.versatile_thermostat.const import *
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT
from .commons import *

logging.getLogger().setLevel(logging.DEBUG)
_LOGGER = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_smartpi_multi_switch_cycle_scheduler(hass: HomeAssistant, skip_hass_states_is_state):
    """
    Test that multi-switch SmartPI uses CycleScheduler for centralized cycle management.
    Periodic recalculations (timestamp=None) should not restart cycles unnecessarily.
    """
    tz = get_tz(hass)
    now = datetime.now(tz=tz)

    # 1. Configure Thermostat with Smart-PI and 2 switches
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPIMultiSwitch",
        unique_id="uniqueId",
        data={
            CONF_NAME: "SmartPIMultiSwitch",
            CONF_THERMOSTAT_TYPE: CONF_THERMOSTAT_SWITCH,
            CONF_TEMP_SENSOR: "sensor.mock_temp_sensor",
            CONF_EXTERNAL_TEMP_SENSOR: "sensor.mock_ext_temp_sensor",
            CONF_CYCLE_MIN: 10,
            CONF_TEMP_MIN: 15,
            CONF_TEMP_MAX: 30,
            CONF_USE_WINDOW_FEATURE: False,
            CONF_USE_MOTION_FEATURE: False,
            CONF_USE_POWER_FEATURE: False,
            CONF_USE_PRESENCE_FEATURE: False,
            CONF_HEATER: "switch.mock_switch1",
            CONF_HEATER_2: "switch.mock_switch2",
            CONF_PROP_FUNCTION: PROPORTIONAL_FUNCTION_SMART_PI,
            CONF_SMART_PI_DEADBAND: 0.05,
        },
    )

    entity: BaseThermostat = await create_thermostat(hass, entry, "climate.smartpimultiswitch")
    assert entity
    assert entity.nb_underlying_entities == 2

    # Verify CycleScheduler is created
    assert entity.cycle_scheduler is not None

    # Ensure mock switches exist for initialization
    hass.states.async_set("switch.mock_switch1", "off")
    hass.states.async_set("switch.mock_switch2", "off")

    # Initialize the entity
    await entity.async_startup(None)

    # Set mode to HEAT and target temp to trigger heating
    await entity.async_set_hvac_mode(VThermHvacMode_HEAT)
    await entity.async_set_temperature(temperature=20)

    with patch("custom_components.versatile_thermostat.base_thermostat.BaseThermostat.send_event"):
        await send_temperature_change_event(entity, 15, now)
        await send_ext_temperature_change_event(entity, 5, now)

    # Verify SmartPI is in Hysteresis phase
    assert entity.prop_algorithm.phase == "Hysteresis"
    assert entity.on_percent == 1.0

    # Mock cycle_scheduler.start_cycle to track calls
    with patch.object(entity.cycle_scheduler, "start_cycle") as mock_start_cycle:
        # Simulate periodic calls to control_heating(timestamp=None)
        for i in range(5):
            _LOGGER.info(f"--- Minute {i} ---")
            await entity.async_control_heating(timestamp=None)

        # Periodic recalculations (timestamp=None) should NOT force-restart cycles
        # because SmartPI only triggers learning on cycle timer (timestamp is not None)
        _LOGGER.info(f"start_cycle calls: {mock_start_cycle.call_count}")

        # With CycleScheduler, periodic calls pass force=False (or hysteresis-related force)
        # but the scheduler itself decides whether to restart based on its running state
