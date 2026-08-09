"""Device registry helpers for SmartPI config entries."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import helper_integration

from ..const import (
    CONF_PROP_FUNCTION,
    DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
)

VT_DOMAIN = "versatile_thermostat"


def get_target_vt_entry(
    hass: HomeAssistant,
    target_unique_id: str,
) -> ConfigEntry | None:
    """Return the VT config entry for a thermostat unique id."""
    for vt_entry in hass.config_entries.async_entries(VT_DOMAIN):
        if vt_entry.unique_id == target_unique_id:
            return vt_entry

    registry = er.async_get(hass)
    climate_entity_id = registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        VT_DOMAIN,
        target_unique_id,
    )
    if not climate_entity_id:
        return None

    climate_entry = registry.async_get(climate_entity_id)
    if climate_entry is None or climate_entry.config_entry_id is None:
        return None

    vt_entry = hass.config_entries.async_get_entry(climate_entry.config_entry_id)
    if vt_entry is None or vt_entry.domain != VT_DOMAIN:
        return None

    return vt_entry


def target_uses_smartpi(hass: HomeAssistant, target_unique_id: str) -> bool:
    """Return whether the target thermostat currently uses SmartPI."""
    vt_entry = get_target_vt_entry(hass, target_unique_id)
    if vt_entry is None:
        return False
    return vt_entry.data.get(CONF_PROP_FUNCTION) == PROP_FUNCTION_SMART_PI


def get_target_device_id(
    hass: HomeAssistant,
    target_unique_id: str,
) -> str | None:
    """Return the HA device id for the target thermostat."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        VT_DOMAIN,
        target_unique_id,
    )
    if not entity_id:
        return None

    reg_entry = registry.async_get(entity_id)
    if reg_entry is not None and reg_entry.device_id:
        return reg_entry.device_id
    return None


def cleanup_config_entry_devices(
    hass: HomeAssistant,
    config_entry_id: str,
    target_unique_ids: Iterable[str],
) -> None:
    """Remove helper-owned duplicates and preserve links to target devices."""
    target_devices = {
        target_unique_id: device_id
        for target_unique_id in target_unique_ids
        if (device_id := get_target_device_id(hass, target_unique_id)) is not None
    }
    target_device_ids = set(target_devices.values())

    entity_registry = er.async_get(hass)
    for target_unique_id, device_id in target_devices.items():
        diagnostic_entity_id = entity_registry.async_get_entity_id(
            SENSOR_DOMAIN,
            DOMAIN,
            f"{DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX}_{target_unique_id}",
        )
        if diagnostic_entity_id is None:
            continue
        diagnostic_entry = entity_registry.async_get(diagnostic_entity_id)
        if (
            diagnostic_entry is not None
            and diagnostic_entry.config_entry_id == config_entry_id
            and diagnostic_entry.device_id != device_id
        ):
            entity_registry.async_update_entity(
                diagnostic_entity_id,
                device_id=device_id,
            )

    if remove_helper_devices := getattr(
        helper_integration,
        "async_remove_helper_devices",
        None,
    ):
        for device_id in sorted(target_device_ids):
            remove_helper_devices(
                hass,
                helper_config_entry_id=config_entry_id,
                source_device_id=device_id,
            )

        remove_helper_devices(
            hass,
            helper_config_entry_id=config_entry_id,
            source_device_id=None,
            remove_all_devices=True,
            keep_device_ids=target_device_ids,
        )
        return

    device_registry = dr.async_get(hass)
    legacy_device_ids = {
        device.id
        for device in dr.async_entries_for_config_entry(
            device_registry,
            config_entry_id,
        )
    }
    for device_id in sorted(target_device_ids | legacy_device_ids):
        helper_integration.async_remove_helper_config_entry_from_source_device(
            hass,
            helper_config_entry_id=config_entry_id,
            source_device_id=device_id,
        )
