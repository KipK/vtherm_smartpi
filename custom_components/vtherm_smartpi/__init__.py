"""The vtherm_smartpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import CONF_PROP_FUNCTION, DATA_FACTORY_REGISTERED, DOMAIN, PROP_FUNCTION_SMART_PI
from .factory import SmartPIHandlerFactory

VT_DOMAIN = "versatile_thermostat"

_LOGGER = get_vtherm_logger(__name__)


def _ensure_domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return the plugin data storage in hass."""
    return hass.data.setdefault(DOMAIN, {})


def _register_factory(hass: HomeAssistant) -> bool:
    """Register the SmartPI factory in the shared VT API."""
    data = _ensure_domain_data(hass)
    if data.get(DATA_FACTORY_REGISTERED) is True:
        return True

    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.warning("Unable to register SmartPI factory because VThermAPI is unavailable")
        return False

    factory = SmartPIHandlerFactory()
    existing_factory = api.get_prop_algorithm(factory.name)
    if existing_factory is None:
        api.register_prop_algorithm(factory)

    data[DATA_FACTORY_REGISTERED] = True
    return True


def _unregister_factory(hass: HomeAssistant) -> None:
    """Unregister the SmartPI factory from the shared VT API."""
    api = VThermAPI.get_vtherm_api(hass)
    if api is not None:
        api.unregister_prop_algorithm(PROP_FUNCTION_SMART_PI)
    _ensure_domain_data(hass)[DATA_FACTORY_REGISTERED] = False


async def _reload_smartpi_vtherms(hass: HomeAssistant) -> None:
    """Reload VT entries that currently target the SmartPI proportional function."""
    reload_tasks = [
        hass.config_entries.async_reload(entry.entry_id)
        for entry in hass.config_entries.async_entries(VT_DOMAIN)
        if entry.data.get(CONF_PROP_FUNCTION) == PROP_FUNCTION_SMART_PI
    ]
    if reload_tasks:
        await asyncio.gather(*reload_tasks)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up vtherm_smartpi from YAML."""
    del config
    _register_factory(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_smartpi from a config entry."""
    _ensure_domain_data(hass)[entry.entry_id] = entry.entry_id
    _register_factory(hass)
    await _reload_smartpi_vtherms(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_smartpi config entry."""
    data = _ensure_domain_data(hass)
    data.pop(entry.entry_id, None)

    if not [key for key in data if key != DATA_FACTORY_REGISTERED]:
        _unregister_factory(hass)

    await _reload_smartpi_vtherms(hass)
    return True
