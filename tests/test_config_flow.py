"""Tests for the SmartPI config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_smartpi.const import (
    CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION,
    CONF_SMART_PI_KNEE_DEMAND,
    CONF_SMART_PI_KNEE_VALVE,
    CONF_SMART_PI_MAX_VALVE,
    CONF_SMART_PI_MIN_VALVE,
    DEFAULT_OPTIONS,
    DOMAIN,
)

VT_DOMAIN = "versatile_thermostat"


@pytest.mark.asyncio
async def test_first_user_step_creates_default_entry(hass) -> None:
    """The first flow run must create the default global entry immediately."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartPI defaults"
    assert result["data"] == DEFAULT_OPTIONS


@pytest.mark.asyncio
async def test_user_step_shows_thermostat_form_when_entry_already_exists(hass) -> None:
    """Later flow runs must open the thermostat form directly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat"


@pytest.mark.asyncio
async def test_thermostat_flow_hides_valve_linearization_for_non_valve(hass) -> None:
    """The per-thermostat settings form must hide valve fields for switch-like targets."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "switch-target",
        suggested_object_id="switch_target",
    )
    hass.states.async_set(
        climate_entry.entity_id,
        "heat",
        {"configuration": {"type": "thermostat_over_switch"}},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"target_vtherm_unique_id": climate_entry.entity_id},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_settings"
    schema_keys = {
        getattr(key, "schema", key)
        for key in result["data_schema"].schema
    }
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION not in schema_keys


@pytest.mark.asyncio
async def test_thermostat_flow_shows_valve_curve_step_for_valve(hass) -> None:
    """Valve targets must get the valve curve step when linearization is enabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-target",
        suggested_object_id="valve_target",
    )
    hass.states.async_set(
        climate_entry.entity_id,
        "heat",
        {"configuration": {"type": "thermostat_over_valve"}},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"target_vtherm_unique_id": climate_entry.entity_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_valve_curve"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SMART_PI_MIN_VALVE: 15.0,
            CONF_SMART_PI_KNEE_DEMAND: 80.0,
            CONF_SMART_PI_KNEE_VALVE: 15.0,
            CONF_SMART_PI_MAX_VALVE: 100.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_valve_curve"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SMART_PI_MIN_VALVE: 7.0,
            CONF_SMART_PI_KNEE_DEMAND: 80.0,
            CONF_SMART_PI_KNEE_VALVE: 15.0,
            CONF_SMART_PI_MAX_VALVE: 100.0,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION] is True
