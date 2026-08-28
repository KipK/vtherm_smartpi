"""Tests for the SmartPI diagnostic sensor platform."""

from __future__ import annotations

from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_smartpi.const import (
    CONF_PROP_FUNCTION,
    CONF_SMART_PI_DEBUG,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SIGNAL_SMARTPI_TARGET_UPDATED,
)
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.sensor import async_setup_entry
from custom_components.vtherm_smartpi.sensor import (
    SmartPIDiagnosticSensor,
    SmartPIRecordedDiagnosticSensor,
)

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
        return {
            "control": {"phase": self.phase.value},
            "temperature": {"indoor": 20.5},
            "setpoint": {"filtered_setpoint": 21.0},
            "power": {
                "applied_percent": 30.0,
                "command_percent": 35.0,
                "pi_percent": 20.0,
                "ff_percent": 15.0,
            },
            "model": {"a": 0.05, "b": 0.001},
        }


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
    assert type(created_entities[0]) is SmartPIDiagnosticSensor


@pytest.mark.asyncio
async def test_debug_entry_records_live_diagnostics(hass) -> None:
    """A debug entry must use the sensor profile that records live diagnostics."""
    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-debug",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)
    plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}-vt-debug",
        data={CONF_TARGET_VTHERM: "vt-debug"},
        options={CONF_SMART_PI_DEBUG: True},
    )
    plugin_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-debug",
        suggested_object_id="vt_debug",
        config_entry=vt_entry,
    )
    async_add_entities = Mock()

    await async_setup_entry(hass, plugin_entry, async_add_entities)

    created_entities = async_add_entities.call_args.args[0]
    assert len(created_entities) == 1
    assert type(created_entities[0]) is SmartPIRecordedDiagnosticSensor
    assert created_entities[0]._unrecorded_attributes == frozenset()


@pytest.mark.asyncio
async def test_dedicated_entry_adds_diagnostic_sensor_when_target_becomes_smartpi(
    hass,
) -> None:
    """A dedicated entry must add diagnostics when its target is resolved later."""
    dedicated_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-vt-dedicated-late",
        data={CONF_TARGET_VTHERM: "vt-dedicated-late"},
    )
    dedicated_entry.add_to_hass(hass)

    async_add_entities = Mock()

    await async_setup_entry(hass, dedicated_entry, async_add_entities)

    async_add_entities.assert_not_called()

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-dedicated-late",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-dedicated-late",
        suggested_object_id="vt_dedicated_late",
        config_entry=vt_entry,
    )

    async_dispatcher_send(hass, SIGNAL_SMARTPI_TARGET_UPDATED, "vt-dedicated-late")

    async_add_entities.assert_called_once()
    created_entities = async_add_entities.call_args.args[0]
    assert len(created_entities) == 1
    assert created_entities[0].unique_id == "smartpi_diag_vt-dedicated-late"


@pytest.mark.asyncio
async def test_dedicated_entry_removes_diagnostic_sensor_when_target_is_not_smartpi(
    hass,
) -> None:
    """A dedicated entry must not keep diagnostics for another algorithm."""
    dedicated_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-vt-migrated",
        data={CONF_TARGET_VTHERM: "vt-migrated"},
    )
    dedicated_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-migrated",
        data={CONF_PROP_FUNCTION: "hysteresis"},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-migrated",
        suggested_object_id="vt_migrated",
        config_entry=vt_entry,
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "smartpi_diag_vt-migrated",
        suggested_object_id="smartpi_diag_vt_migrated",
        config_entry=dedicated_entry,
    )

    async_add_entities = Mock()

    await async_setup_entry(hass, dedicated_entry, async_add_entities)

    async_add_entities.assert_not_called()
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "smartpi_diag_vt-migrated")
        is None
    )


@pytest.mark.asyncio
async def test_dedicated_entry_removes_tracked_diagnostic_when_target_stops_using_smartpi(
    hass,
) -> None:
    """A tracked diagnostic entity must be removed when its target changes algorithm."""
    dedicated_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-vt-tracked",
        data={CONF_TARGET_VTHERM: "vt-tracked"},
    )
    dedicated_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-tracked",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-tracked",
        suggested_object_id="vt_tracked",
        config_entry=vt_entry,
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "smartpi_diag_vt-tracked",
        suggested_object_id="smartpi_diag_vt_tracked",
        config_entry=dedicated_entry,
    )

    async_add_entities = Mock()

    await async_setup_entry(hass, dedicated_entry, async_add_entities)

    async_add_entities.assert_called_once()

    hass.config_entries.async_update_entry(
        vt_entry,
        data={CONF_PROP_FUNCTION: "hysteresis"},
    )
    async_dispatcher_send(hass, SIGNAL_SMARTPI_TARGET_UPDATED, "vt-tracked")

    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "smartpi_diag_vt-tracked")
        is None
    )


@pytest.mark.asyncio
async def test_dedicated_entry_recreates_tracked_diagnostic_when_registry_entry_disappears(
    hass,
) -> None:
    """A tracked diagnostic entity must be recreated when its registry entry is missing."""
    dedicated_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-vt-recreated",
        data={CONF_TARGET_VTHERM: "vt-recreated"},
    )
    dedicated_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-recreated",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-recreated",
        suggested_object_id="vt_recreated",
        config_entry=vt_entry,
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "smartpi_diag_vt-recreated",
        suggested_object_id="smartpi_diag_vt_recreated",
        config_entry=dedicated_entry,
    )

    async_add_entities = Mock()

    await async_setup_entry(hass, dedicated_entry, async_add_entities)

    async_add_entities.assert_called_once()

    registry.async_remove("sensor.smartpi_diag_vt_recreated")
    async_dispatcher_send(hass, SIGNAL_SMARTPI_TARGET_UPDATED, "vt-recreated")

    assert async_add_entities.call_count == 2
    recreated_entities = async_add_entities.call_args.args[0]
    assert len(recreated_entities) == 1
    assert recreated_entities[0].unique_id == "smartpi_diag_vt-recreated"


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
async def test_legacy_global_entry_without_unique_id_adds_default_bound_sensor(
    hass,
) -> None:
    """A defaults entry without a normalized unique id must still track SmartPI targets."""
    global_entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        data=dict(DEFAULT_OPTIONS),
    )
    global_entry.add_to_hass(hass)

    async_add_entities = Mock()

    await async_setup_entry(hass, global_entry, async_add_entities)

    async_add_entities.assert_not_called()

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-legacy-global",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-legacy-global",
        suggested_object_id="vt_legacy_global",
        config_entry=vt_entry,
    )

    async_dispatcher_send(hass, SIGNAL_SMARTPI_TARGET_UPDATED, "vt-legacy-global")

    async_add_entities.assert_called_once()
    created_entities = async_add_entities.call_args.args[0]
    assert len(created_entities) == 1
    assert created_entities[0].unique_id == "smartpi_diag_vt-legacy-global"


def test_diagnostic_sensor_links_directly_to_vt_device(hass) -> None:
    """The diagnostic sensor should link without declaring source device info."""
    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="vt-linked",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    source_device = device_registry.async_get_or_create(
        config_entry_id=vt_entry.entry_id,
        identifiers={(VT_DOMAIN, "vt-linked")},
    )
    entity_registry = er.async_get(hass)
    climate_entry = entity_registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "vt-linked",
        suggested_object_id="vt_linked",
        config_entry=vt_entry,
        device_id=source_device.id,
    )

    sensor = SmartPIDiagnosticSensor(hass, climate_entry.entity_id, "vt-linked")

    assert sensor.device_entry is source_device
    assert sensor.device_info is None


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

    sensor = SmartPIDiagnosticSensor(hass, climate_entity_id, "test-vtherm")

    sensor._update_from_climate()

    assert sensor.native_value == expected_state


def test_diagnostic_sensor_builds_stable_envelope_and_deduplicates(hass) -> None:
    """The sensor must publish the v2 envelope only when its content changes."""
    climate_entity_id = "climate.test_vtherm"
    hass.states.async_set(climate_entity_id, "heat")
    live = DummySmartPI(SmartPIPhase.STABLE)
    hass.data["climate"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id=climate_entity_id,
                prop_algorithm=live,
            )
        ]
    )
    sensor = SmartPIDiagnosticSensor(hass, climate_entity_id, "test-vtherm")

    assert sensor._update_from_climate() is True
    assert sensor.extra_state_attributes == {
        "schema_version": 2,
        "live": live.get_published_diagnostics(),
        "history": {
            "temperature": {"indoor": 20.5},
            "setpoint": {"filtered_setpoint": 21.0},
            "power": {
                "applied_percent": 30.0,
                "command_percent": 35.0,
                "pi_percent": 20.0,
                "ff_percent": 15.0,
            },
            "model": {"a": 0.05, "b": 0.001},
        },
    }
    assert sensor.force_update is False
    assert sensor._unrecorded_attributes == frozenset({"live"})
    assert sensor._update_from_climate() is False


@pytest.mark.asyncio
async def test_diagnostic_sensor_removes_registry_entry_when_climate_uses_another_algorithm(
    hass,
) -> None:
    """The diagnostic entity must disappear when the target stops using SmartPI."""
    plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-test-vtherm",
        data={CONF_TARGET_VTHERM: "test-vtherm"},
    )
    plugin_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="test-vtherm",
        data={CONF_PROP_FUNCTION: "hysteresis"},
    )
    vt_entry.add_to_hass(hass)

    climate_entity_id = "climate.test_vtherm"
    hass.states.async_set(climate_entity_id, "heat")
    hass.data["climate"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id=climate_entity_id,
                prop_algorithm=object(),
            )
        ]
    )

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "smartpi_diag_test-vtherm",
        suggested_object_id="smartpi_diag_test_vtherm",
        config_entry=plugin_entry,
    )

    sensor = SmartPIDiagnosticSensor(hass, climate_entity_id, "test-vtherm")

    sensor._update_from_climate()

    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "smartpi_diag_test-vtherm")
        is None
    )


@pytest.mark.asyncio
async def test_diagnostic_sensor_keeps_registry_entry_when_smartpi_runtime_is_not_ready(
    hass,
) -> None:
    """The diagnostic entity must stay registered while the SmartPI runtime is pending."""
    plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-test-vtherm",
        data={CONF_TARGET_VTHERM: "test-vtherm"},
    )
    plugin_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="test-vtherm",
        data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
    )
    vt_entry.add_to_hass(hass)

    climate_entity_id = "climate.test_vtherm"
    hass.states.async_set(climate_entity_id, "off")
    hass.data["climate"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id=climate_entity_id,
                prop_algorithm=None,
            )
        ]
    )

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "smartpi_diag_test-vtherm",
        suggested_object_id="smartpi_diag_test_vtherm",
        config_entry=plugin_entry,
    )

    sensor = SmartPIDiagnosticSensor(hass, climate_entity_id, "test-vtherm")

    assert sensor._update_from_climate() is True

    assert sensor.native_value == "inactive"
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "smartpi_diag_test-vtherm")
        == "sensor.smartpi_diag_test_vtherm"
    )
