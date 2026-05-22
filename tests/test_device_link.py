"""Tests for SmartPI device registry links."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.const import (
    CONF_PROP_FUNCTION,
    PROP_FUNCTION_SMART_PI,
)
from custom_components.vtherm_smartpi.smartpi import device_link


def test_target_uses_smartpi_reads_vt_config_entry() -> None:
    """The target activity check should use the VT config entry."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(
            unique_id="vt-salon",
            data={CONF_PROP_FUNCTION: PROP_FUNCTION_SMART_PI},
        )
    ]

    assert device_link.target_uses_smartpi(hass, "vt-salon") is True


def test_target_uses_smartpi_rejects_other_algorithm() -> None:
    """The target activity check should reject a non-SmartPI VT config entry."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(
            unique_id="vt-salon",
            data={CONF_PROP_FUNCTION: "hysteresis"},
        )
    ]

    assert device_link.target_uses_smartpi(hass, "vt-salon") is False


def test_bind_and_unbind_config_entry_to_target_device(monkeypatch) -> None:
    """SmartPI should add and remove its config entry on the target device."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(device_id="device-id")
    device_registry = MagicMock()
    monkeypatch.setattr(device_link.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(device_link.dr, "async_get", lambda _hass: device_registry)

    device_link.bind_config_entry_to_target_device(
        hass,
        "smartpi-entry-id",
        "vt-salon",
        "climate.salon",
    )

    device_registry.async_update_device.assert_called_once_with(
        "device-id",
        add_config_entry_id="smartpi-entry-id",
    )

    device_registry.async_update_device.reset_mock()

    device_link.unbind_config_entry_from_target_device(
        hass,
        "smartpi-entry-id",
        "vt-salon",
        "climate.salon",
    )

    device_registry.async_update_device.assert_called_once_with(
        "device-id",
        remove_config_entry_id="smartpi-entry-id",
    )
