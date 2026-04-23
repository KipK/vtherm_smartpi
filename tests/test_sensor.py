"""Tests for the SmartPI diagnostic sensor platform."""

from __future__ import annotations

from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_smartpi.const import (
    CONF_PROP_FUNCTION,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SIGNAL_SMARTPI_TARGET_UPDATED,
)
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.sensor import async_setup_entry
from custom_components.vtherm_smartpi.sensor import SmartPIDiagnosticSensor

VT_DOMAIN = "versatile_thermostat"


class DummySmartPI(SmartPI):
    """Minimal SmartPI test double exposing only published phase data."""

    def __init__(self, phase: SmartPIPhase) -> None:
        self._phase = phase
        self._debug_mode = False

    @property
    def phase(self) -> SmartPIPhase:
        return self._phase

    def get_published_diagnostics(self):
        return {"control": {"phase": self.phase.value}}


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


@pytest.mark.asyncio
async def test_global_entry_adds_default_bound_diagnostic_sensor_when_vtherm_becomes_smartpi(
    hass,
) -> None:
    """The global entry must add diagnostics when a default-bound thermostat becomes SmartPI."""
    global_entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    global_entry.add_to_hass(hass)

    async_add_entities = Mock()

    await async_setup_entry(hass, global_entry, async_add_entities)

    async_add_entities.assert_not_called()

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-added",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-added",
        suggested_object_id="vt_added",
        config_entry=vt_entry,
    )

    async_dispatcher_send(hass, SIGNAL_SMARTPI_TARGET_UPDATED, "vt-added")

    async_add_entities.assert_called_once()
    created_entities = async_add_entities.call_args.args[0]
    assert len(created_entities) == 1
    assert created_entities[0].unique_id == "smartpi_diag_vt-added"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_state"),
    [
        (SmartPIPhase.HYSTERESIS, "bootstrap_hysteresis"),
        (SmartPIPhase.STABLE, "stable"),
        (SmartPIPhase.CALIBRATION, "calibration"),
    ],
)
async def test_diagnostic_sensor_state_reflects_smartpi_phase(
    hass,
    phase: SmartPIPhase,
    expected_state: str,
) -> None:
    """The diagnostic sensor state must expose the SmartPI top-level phase."""
    climate_entity_id = "climate.test_vtherm"
    hass.states.async_set(climate_entity_id, "heat")
    hass.data["climate"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id=climate_entity_id,
                prop_algorithm=DummySmartPI(phase),
            )
        ]
    )

    sensor = SmartPIDiagnosticSensor(hass, climate_entity_id, "test-vtherm", None)

    sensor._update_from_climate()

    assert sensor.native_value == expected_state
