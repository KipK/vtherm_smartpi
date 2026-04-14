# pylint: disable=unused-argument, line-too-long, too-many-lines
""" Test the Versatile Thermostat config flow for Smart-PI """

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import SOURCE_USER, ConfigEntry

from custom_components.versatile_thermostat.const import (
    DOMAIN,
    CONF_THERMOSTAT_TYPE,
    CONF_THERMOSTAT_SWITCH,
    CONF_NAME,
    CONF_TEMP_SENSOR,
    CONF_CYCLE_MIN,
    CONF_DEVICE_POWER,
    CONF_USE_MAIN_CENTRAL_CONFIG,
    CONF_UNDERLYING_LIST,
    CONF_HEATER_KEEP_ALIVE,
    CONF_PROP_FUNCTION,
    PROPORTIONAL_FUNCTION_SMART_PI,
    CONF_AC_MODE,
    CONF_INVERSE_SWITCH,
    CONF_USE_TPI_CENTRAL_CONFIG,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_SMART_PI_DEADBAND,
    CONF_SMART_PI_USE_FF3,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_TEMP_MIN,
    CONF_TEMP_MAX,
    CONF_TEMP_MAX,
    CONF_STEP_TEMPERATURE,
    CONF_SAFETY_DELAY_MIN,
    CONF_SAFETY_MIN_ON_PERCENT,
    CONF_SAFETY_DEFAULT_ON_PERCENT,
    CONF_USE_PRESETS_CENTRAL_CONFIG,
    CONF_USE_ADVANCED_CENTRAL_CONFIG,
)

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import

async def test_smart_pi_config_flow(
    hass: HomeAssistant, skip_hass_states_get
):
    """Test the config flow for Smart-PI including minimal delays"""
    
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == SOURCE_USER

    # 1. Type
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_THERMOSTAT_TYPE: CONF_THERMOSTAT_SWITCH,
        },
    )
    assert result["type"] == FlowResultType.MENU
    
    # 2. Main
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "main"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "SmartPIMock",
            CONF_TEMP_SENSOR: "sensor.mock_temp_sensor",
            CONF_CYCLE_MIN: 5,
            CONF_DEVICE_POWER: 1,
            CONF_USE_MAIN_CENTRAL_CONFIG: False,
            CONF_USE_CENTRAL_MODE: False,
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_EXTERNAL_TEMP_SENSOR: "sensor.mock_ext_temp_sensor",
            CONF_TEMP_MIN: 15,
            CONF_TEMP_MAX: 30,
            CONF_STEP_TEMPERATURE: 0.1,
        },
    )

    # 3. Type details (Smart-PI selection)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "type"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_UNDERLYING_LIST: ["switch.mock_switch"],
            CONF_HEATER_KEEP_ALIVE: 0,
            CONF_PROP_FUNCTION: PROPORTIONAL_FUNCTION_SMART_PI,
            CONF_AC_MODE: False,
            CONF_INVERSE_SWITCH: False,
        },
    )
    
    # Check that smart_pi is in menu
    assert result["type"] == FlowResultType.MENU
    assert "smart_pi" in result["menu_options"]

    # 4. Smart-PI configuration
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "smart_pi"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "smart_pi"
    
    # The first step is checking for central config. We say False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            # "use_smart_pi_central_config": False # Default
        },
    )
    
    # Now we should see the parameters form with new fields
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "smart_pi"
    
    # Verify the schema has the new fields (implicitly by submitting them)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SMART_PI_DEADBAND: 0.05,
            CONF_MINIMAL_ACTIVATION_DELAY: 30,
            CONF_MINIMAL_DEACTIVATION_DELAY: 15,
        },
    )

    assert result["type"] == FlowResultType.MENU

    # 5. Presets
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "presets"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_USE_PRESETS_CENTRAL_CONFIG: False}
    )
    assert result["type"] == FlowResultType.MENU

    # 6. Advanced
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "advanced"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], 
        user_input={
            CONF_USE_ADVANCED_CENTRAL_CONFIG: False,
        }
    )
    assert result["type"] == FlowResultType.FORM # It asks for values now
    
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SAFETY_DELAY_MIN: 60,
            CONF_SAFETY_MIN_ON_PERCENT: 0.5,
            CONF_SAFETY_DEFAULT_ON_PERCENT: 0.1,
        }
    )
    assert result["type"] == FlowResultType.MENU
    
    # Finalize
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "finalize"}
    )
    
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry_data = result["data"]
    
    assert entry_data[CONF_MINIMAL_ACTIVATION_DELAY] == 30
    assert entry_data[CONF_MINIMAL_DEACTIVATION_DELAY] == 15
    assert entry_data[CONF_PROP_FUNCTION] == PROPORTIONAL_FUNCTION_SMART_PI
    assert entry_data[CONF_SMART_PI_USE_FF3] is True

    await hass.async_block_till_done()
