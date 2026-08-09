"""Tests for SmartPI device registry links."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import call, MagicMock

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


def test_cleanup_config_entry_devices_preserves_multiple_target_links(
    monkeypatch,
) -> None:
    """Modern cleanup should preserve each diagnostic target device link."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: {
        ("climate", "versatile_thermostat", "vt-bedroom"): "climate.bedroom",
        ("climate", "versatile_thermostat", "vt-living-room"): "climate.living_room",
        (
            "sensor",
            "vtherm_smartpi",
            "smartpi_diag_vt-bedroom",
        ): "sensor.smartpi_bedroom",
        (
            "sensor",
            "vtherm_smartpi",
            "smartpi_diag_vt-living-room",
        ): "sensor.smartpi_living_room",
    }.get((domain, platform, unique_id))
    registry.async_get.side_effect = lambda entity_id: {
        "climate.bedroom": SimpleNamespace(device_id="device-bedroom"),
        "climate.living_room": SimpleNamespace(device_id="device-living-room"),
        "sensor.smartpi_bedroom": SimpleNamespace(
            config_entry_id="smartpi-entry-id",
            device_id="fork-bedroom",
        ),
        "sensor.smartpi_living_room": SimpleNamespace(
            config_entry_id="smartpi-entry-id",
            device_id="fork-living-room",
        ),
    }.get(entity_id)
    remove_helper_devices = MagicMock()
    monkeypatch.setattr(device_link.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        device_link.helper_integration,
        "async_remove_helper_devices",
        remove_helper_devices,
        raising=False,
    )

    device_link.cleanup_config_entry_devices(
        hass,
        "smartpi-entry-id",
        ["vt-living-room", "vt-bedroom"],
    )

    assert registry.async_update_entity.call_args_list == [
        call("sensor.smartpi_living_room", device_id="device-living-room"),
        call("sensor.smartpi_bedroom", device_id="device-bedroom"),
    ]
    assert remove_helper_devices.call_args_list == [
        call(
            hass,
            helper_config_entry_id="smartpi-entry-id",
            source_device_id="device-bedroom",
        ),
        call(
            hass,
            helper_config_entry_id="smartpi-entry-id",
            source_device_id="device-living-room",
        ),
        call(
            hass,
            helper_config_entry_id="smartpi-entry-id",
            source_device_id=None,
            remove_all_devices=True,
            keep_device_ids={"device-bedroom", "device-living-room"},
        ),
    ]


def test_cleanup_config_entry_devices_uses_legacy_helper(monkeypatch) -> None:
    """Legacy cleanup should unlink current and stale helper-owned devices."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
        "climate.bedroom" if domain == "climate" else None
    )
    registry.async_get.return_value = SimpleNamespace(device_id="device-bedroom")
    device_registry = MagicMock()
    legacy_cleanup = MagicMock()
    monkeypatch.setattr(device_link.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(device_link.dr, "async_get", lambda _hass: device_registry)
    monkeypatch.setattr(
        device_link.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [SimpleNamespace(id="stale-device")],
    )
    monkeypatch.delattr(
        device_link.helper_integration,
        "async_remove_helper_devices",
        raising=False,
    )
    monkeypatch.setattr(
        device_link.helper_integration,
        "async_remove_helper_config_entry_from_source_device",
        legacy_cleanup,
    )

    device_link.cleanup_config_entry_devices(
        hass,
        "smartpi-entry-id",
        ["vt-bedroom"],
    )

    assert legacy_cleanup.call_args_list == [
        call(
            hass,
            helper_config_entry_id="smartpi-entry-id",
            source_device_id="device-bedroom",
        ),
        call(
            hass,
            helper_config_entry_id="smartpi-entry-id",
            source_device_id="stale-device",
        ),
    ]


def test_cleanup_config_entry_devices_sweeps_without_current_target(
    monkeypatch,
) -> None:
    """Modern cleanup should remove helper devices when no target device exists."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    remove_helper_devices = MagicMock()
    monkeypatch.setattr(device_link.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        device_link.helper_integration,
        "async_remove_helper_devices",
        remove_helper_devices,
        raising=False,
    )

    device_link.cleanup_config_entry_devices(
        hass,
        "smartpi-entry-id",
        ["vt-without-device"],
    )

    remove_helper_devices.assert_called_once_with(
        hass,
        helper_config_entry_id="smartpi-entry-id",
        source_device_id=None,
        remove_all_devices=True,
        keep_device_ids=set(),
    )
