"""Tests for the SmartPI diagnostic sensor platform."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_smartpi.const import (
    CONF_PROP_FUNCTION,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
)
from custom_components.vtherm_smartpi.sensor import async_setup_entry

VT_DOMAIN = "versatile_thermostat"


@pytest.mark.asyncio
async def test_global_entry_creates_only_default_bound_diagnostic_sensors(hass) -> None:
    """The global entry must create diagnostics for SmartPI thermostats without dedicated config."""
    vt_default_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-default",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_default_entry.add_to_hass(hass)

    vt_dedicated_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-dedicated",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_dedicated_entry.add_to_hass(hass)

    global_entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    global_entry.add_to_hass(hass)

    dedicated_plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-vt-dedicated",
        data={CONF_TARGET_VTHERM: "vt-dedicated"},
    )
    dedicated_plugin_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-default",
        suggested_object_id="vt_default",
        config_entry=vt_default_entry,
    )
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-dedicated",
        suggested_object_id="vt_dedicated",
        config_entry=vt_dedicated_entry,
    )

    async_add_entities = Mock()

    await async_setup_entry(hass, global_entry, async_add_entities)

    async_add_entities.assert_called_once()
    created_entities = async_add_entities.call_args.args[0]
    assert len(created_entities) == 1
    assert created_entities[0].unique_id == "smartpi_diag_vt-default"
