"""The vtherm_smartpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.helpers import service as service_helper
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import (
    CONF_PROP_FUNCTION,
    DATA_FACTORY_REGISTERED,
    DATA_SERVICES_REGISTERED,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SERVICE_FORCE_SMARTPI_CALIBRATION,
    SERVICE_RESET_SMARTPI_INTEGRAL,
    SERVICE_RESET_SMARTPI_LEARNING,
)
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


def _register_services(hass: HomeAssistant) -> None:
    """Register SmartPI services on the plugin domain."""
    data = _ensure_domain_data(hass)
    if data.get(DATA_SERVICES_REGISTERED) is True:
        return

    async def _call_on_vtherms(call, method_name: str) -> None:
        entity_ids = service_helper.async_extract_entity_ids(hass, call)
        component = hass.data.get(CLIMATE_DOMAIN)
        if not component:
            return
        for entity in list(component.entities):
            if entity.entity_id not in entity_ids:
                continue
            if getattr(entity, "proportional_function", None) != PROP_FUNCTION_SMART_PI:
                continue
            handler = getattr(entity, method_name, None)
            if handler is None:
                _LOGGER.warning(
                    "Service %s not available on %s", method_name, entity.entity_id
                )
                continue
            await handler()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SMARTPI_LEARNING,
        lambda call: _call_on_vtherms(call, "service_reset_smart_pi_learning"),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_SMARTPI_CALIBRATION,
        lambda call: _call_on_vtherms(call, "service_force_smartpi_calibration"),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SMARTPI_INTEGRAL,
        lambda call: _call_on_vtherms(call, "service_reset_smartpi_integral"),
    )

    data[DATA_SERVICES_REGISTERED] = True


def _unregister_services(hass: HomeAssistant) -> None:
    """Unregister SmartPI services from the plugin domain."""
    hass.services.async_remove(DOMAIN, SERVICE_RESET_SMARTPI_LEARNING)
    hass.services.async_remove(DOMAIN, SERVICE_FORCE_SMARTPI_CALIBRATION)
    hass.services.async_remove(DOMAIN, SERVICE_RESET_SMARTPI_INTEGRAL)
    _ensure_domain_data(hass)[DATA_SERVICES_REGISTERED] = False


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
    _register_services(hass)
    return True


from homeassistant.const import Platform

PLATFORMS = [Platform.SENSOR]


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_smartpi from a config entry."""
    _ensure_domain_data(hass)[entry.entry_id] = entry.entry_id
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _register_factory(hass)
    _register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Only reload VT entries when HA is already running (live install/update).
    # During initial startup, VT handles deferred algorithm init via async_startup
    # which fires after EVENT_HOMEASSISTANT_STARTED — reloading here would force
    # entity re-creation before the recorder has restored previous states.
    if hass.state == CoreState.running:
        await _reload_smartpi_vtherms(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_smartpi config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    data = _ensure_domain_data(hass)
    data.pop(entry.entry_id, None)

    if not [key for key in data if key not in (DATA_FACTORY_REGISTERED, DATA_SERVICES_REGISTERED)]:
        _unregister_factory(hass)
        _unregister_services(hass)

    await _reload_smartpi_vtherms(hass)
    return True
