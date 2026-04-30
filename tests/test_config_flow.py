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
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.vtherm_smartpi.config_flow import build_options_schema

VT_DOMAIN = "versatile_thermostat"


def _schema_keys(schema) -> set[str]:
    """Return normalized voluptuous schema keys."""
    return {getattr(key, "schema", key) for key in schema.schema}


def _mock_config_entry_lookup(hass, monkeypatch, config_entry: MockConfigEntry) -> None:
    """Expose a config entry to the registry lookup without setting up its domain."""
    original_async_get_entry = hass.config_entries.async_get_entry

    def async_get_entry(entry_id: str):
        if entry_id == config_entry.entry_id:
            return config_entry
        return original_async_get_entry(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_get_entry", async_get_entry)


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


def test_global_options_schema_hides_valve_linearization() -> None:
    """Global defaults must not expose valve-only linearization settings."""
    schema_keys = _schema_keys(build_options_schema(DEFAULT_OPTIONS))

    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION not in schema_keys


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
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION not in schema_keys


@pytest.mark.asyncio
async def test_thermostat_flow_shows_valve_linearization_from_vtherm_config_entry(
    hass,
    monkeypatch,
) -> None:
    """Valve targets must be detected from the VT config entry when state is not ready."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="valve-config-target",
        data={"thermostat_type": "thermostat_over_valve"},
    )
    _mock_config_entry_lookup(hass, monkeypatch, vt_entry)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-config-target",
        suggested_object_id="valve_config_target",
        config_entry=vt_entry,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TARGET_VTHERM: climate_entry.entity_id},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_settings"
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION in schema_keys


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


@pytest.mark.asyncio
async def test_options_flow_shows_valve_linearization_from_dedicated_target_config(
    hass,
    monkeypatch,
) -> None:
    """Dedicated valve options must expose the linearization switch."""
    plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-valve-options-target",
        data={CONF_TARGET_VTHERM: "valve-options-target", **DEFAULT_OPTIONS},
    )
    plugin_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="valve-options-target",
        data={
            "thermostat_type": "thermostat_over_climate",
            "auto_regulation_mode": "auto_regulation_valve",
        },
    )
    _mock_config_entry_lookup(hass, monkeypatch, vt_entry)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-options-target",
        suggested_object_id="valve_options_target",
        config_entry=vt_entry,
    )

    result = await hass.config_entries.options.async_init(plugin_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION in schema_keys
