"""Tests for SmartPI thermostat attributes."""

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.const import (
    DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX,
    DOMAIN,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler


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
    registry.async_get_entity_id.assert_called_once_with(
        "sensor",
        DOMAIN,
        f"{DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX}_vt-test",
    )
