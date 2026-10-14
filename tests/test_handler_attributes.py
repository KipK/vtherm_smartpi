"""Tests for SmartPI thermostat attributes."""

from unittest.mock import AsyncMock, MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.const import (
    DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX,
    DOMAIN,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.smartpi.autocalib import AutoCalibEvent


def test_update_attributes_publishes_diagnostic_entity_id(monkeypatch):
    """Expose the SmartPI diagnostic sensor entity id in specific_states."""
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.smartpi_diagnostics"
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.handler.er.async_get",
        lambda hass: registry,
    )

    thermostat = MagicMock()
    thermostat.hass = MagicMock()
    thermostat.unique_id = "vt-test"
    thermostat.prop_algorithm = object()
    thermostat._attr_extra_state_attributes = {"specific_states": {}}

    handler = SmartPIHandler(thermostat)

    handler.update_attributes()

    assert thermostat._attr_extra_state_attributes["specific_states"][
        "regulation_diagnostics"
    ] == "sensor.smartpi_diagnostics"
    assert thermostat._attr_extra_state_attributes["specific_states"][
        "smartpi_learning_enabled"
    ] is True
    registry.async_get_entity_id.assert_called_once_with(
        "sensor",
        DOMAIN,
        f"{DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX}_vt-test",
    )


async def test_set_learning_service_publishes_and_persists(monkeypatch):
    """The handler service must publish and save the learning flag immediately."""
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.handler.er.async_get",
        lambda hass: MagicMock(async_get_entity_id=MagicMock(return_value=None)),
    )
    dispatcher_send = MagicMock()
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.handler.async_dispatcher_send",
        dispatcher_send,
    )

    thermostat = MagicMock()
    thermostat.hass = MagicMock()
    thermostat.entity_id = "climate.test"
    thermostat.unique_id = "vt-test"
    thermostat._attr_extra_state_attributes = {"specific_states": {}}
    thermostat.prop_algorithm = SmartPI(
        hass=thermostat.hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Handler learning test",
    )
    handler = SmartPIHandler(thermostat)
    handler._async_save = AsyncMock()

    await handler.service_set_smartpi_learning(False)

    assert thermostat.prop_algorithm.learning_enabled is False
    assert thermostat._attr_extra_state_attributes["specific_states"][
        "smartpi_learning_enabled"
    ] is False
    thermostat.async_write_ha_state.assert_called_once_with()
    handler._async_save.assert_awaited_once_with()
    dispatcher_send.assert_called_once_with(
        thermostat.hass,
        "smartpi_diag_update_vt-test",
    )
    thermostat.hass.bus.async_fire.assert_called_once_with(
        "vtherm_smartpi_event",
        {
            "entity_id": "climate.test",
            "type": "learning_enabled_changed",
            "learning_enabled": False,
        },
    )


def _autocalib_thermostat() -> MagicMock:
    """Build a thermostat whose entity id differs from its name."""
    thermostat = MagicMock()
    thermostat.hass = MagicMock()
    thermostat.entity_id = "climate.salon"
    thermostat.name = "Salon"
    return thermostat


def test_autocalib_event_carries_entity_id_and_name():
    """AutoCalib events fire with the climate entity in entity_id and the name in name."""
    thermostat = _autocalib_thermostat()
    handler = SmartPIHandler(thermostat)
    event = AutoCalibEvent(
        event_type="smartpi_autocalib_success",
        payload={"name": "Salon", "x": 1},
    )

    handler._fire_autocalib_event(event)

    thermostat.hass.bus.async_fire.assert_called_once_with(
        "smartpi_autocalib_success",
        {"entity_id": "climate.salon", "name": "Salon", "x": 1},
    )
    # The event emitted by AutoCalibTrigger is not mutated.
    assert "entity_id" not in event.payload


async def test_force_calibration_service_fires_autocalib_event_with_entity_id(monkeypatch):
    """The force calibration service fires the AutoCalib event with the climate entity id."""
    thermostat = _autocalib_thermostat()
    thermostat.prop_algorithm = SmartPI(
        hass=thermostat.hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Salon",
    )
    handler = SmartPIHandler(thermostat)
    handler._async_save = AsyncMock()
    handler.control_heating = AsyncMock()
    handler.update_attributes = MagicMock()
    monkeypatch.setattr(
        thermostat.prop_algorithm,
        "force_calibration",
        lambda: AutoCalibEvent(
            event_type="smartpi_autocalib_triggered",
            payload={"name": "Salon", "triggered_params": ["manual_request"]},
        ),
    )

    await handler.service_force_smartpi_calibration()

    thermostat.hass.bus.async_fire.assert_any_call(
        "smartpi_autocalib_triggered",
        {
            "entity_id": "climate.salon",
            "name": "Salon",
            "triggered_params": ["manual_request"],
        },
    )
