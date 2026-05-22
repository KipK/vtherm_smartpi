"""Device registry helpers for SmartPI config entries."""

from __future__ import annotations

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..const import CONF_PROP_FUNCTION, PROP_FUNCTION_SMART_PI

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
    entity_id: str | None = None,
) -> str | None:
    """Return the HA device id for the target thermostat."""
    registry = er.async_get(hass)

    if entity_id:
        reg_entry = registry.async_get(entity_id)
        if reg_entry is not None and reg_entry.device_id:
            return reg_entry.device_id

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


def bind_config_entry_to_target_device(
    hass: HomeAssistant,
    config_entry_id: str | None,
    target_unique_id: str,
    entity_id: str | None = None,
) -> None:
    """Link a SmartPI config entry to the target thermostat device."""
    if config_entry_id is None:
        return
    device_id = get_target_device_id(hass, target_unique_id, entity_id)
    if not device_id:
        return
    dr.async_get(hass).async_update_device(
        device_id,
        add_config_entry_id=config_entry_id,
    )


def unbind_config_entry_from_target_device(
    hass: HomeAssistant,
    config_entry_id: str | None,
    target_unique_id: str,
    entity_id: str | None = None,
) -> None:
    """Unlink a SmartPI config entry from the target thermostat device."""
    if config_entry_id is None:
        return
    device_id = get_target_device_id(hass, target_unique_id, entity_id)
    if not device_id:
        return
    dr.async_get(hass).async_update_device(
        device_id,
        remove_config_entry_id=config_entry_id,
    )
