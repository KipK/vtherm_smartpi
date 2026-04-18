"""The vtherm_smartpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service as service_helper
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import (
    CONF_PROP_FUNCTION,
    CONF_TARGET_VTHERM,
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
DATA_SKIP_FULL_RELOAD = "skip_full_reload"


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
        entity_ids = set(await service_helper.async_extract_entity_ids(call))

        call_target = getattr(call, "target", None)
        if isinstance(call_target, dict):
            target_entity_ids = call_target.get("entity_id")
            if isinstance(target_entity_ids, str):
                entity_ids.add(target_entity_ids)
            elif isinstance(target_entity_ids, list):
                entity_ids.update(
                    entity_id for entity_id in target_entity_ids if isinstance(entity_id, str)
                )

        explicit_entity_ids = call.data.get("entity_id")
        if isinstance(explicit_entity_ids, str):
            entity_ids.add(explicit_entity_ids)
        elif isinstance(explicit_entity_ids, list):
            entity_ids.update(
                entity_id for entity_id in explicit_entity_ids if isinstance(entity_id, str)
            )

        component = hass.data.get(CLIMATE_DOMAIN)
        if not component or not entity_ids:
            return

        for entity_id in sorted(entity_ids):
            entity = component.get_entity(entity_id) if hasattr(component, "get_entity") else None
            if entity is None:
                entity = next(
                    (candidate for candidate in list(component.entities) if candidate.entity_id == entity_id),
                    None,
                )
            if entity is None:
                continue

            if getattr(entity, "proportional_function", None) != PROP_FUNCTION_SMART_PI:
                continue
            handler = getattr(entity, method_name, None)
            if handler is None:
                algo_handler = getattr(entity, "_algo_handler", None)
                handler = getattr(algo_handler, method_name, None) if algo_handler is not None else None
            if handler is None:
                _LOGGER.warning(
                    "Service %s not available on %s", method_name, entity.entity_id
                )
                continue
            await handler()

    async def _handle_reset_learning(call) -> None:
        """Handle SmartPI learning reset with a real async callable."""
        await _call_on_vtherms(call, "service_reset_smart_pi_learning")

    async def _handle_force_calibration(call) -> None:
        """Handle SmartPI calibration forcing with a real async callable."""
        await _call_on_vtherms(call, "service_force_smartpi_calibration")

    async def _handle_reset_integral(call) -> None:
        """Handle SmartPI integral reset with a real async callable."""
        await _call_on_vtherms(call, "service_reset_smartpi_integral")

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SMARTPI_LEARNING,
        _handle_reset_learning,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_SMARTPI_CALIBRATION,
        _handle_force_calibration,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SMARTPI_INTEGRAL,
        _handle_reset_integral,
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


def _get_dedicated_target_unique_ids(hass: HomeAssistant) -> set[str]:
    """Return VT unique ids that have a dedicated SmartPI config entry."""
    return {
        plugin_entry.data[CONF_TARGET_VTHERM]
        for plugin_entry in hass.config_entries.async_entries(DOMAIN)
        if plugin_entry.data.get(CONF_TARGET_VTHERM)
    }


async def _reload_smartpi_vtherms_for_target(
    hass: HomeAssistant,
    target_unique_id: str,
) -> None:
    """Reload the VT entry bound to the given thermostat unique id."""
    registry = er.async_get(hass)
    climate_entity_id = registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        "versatile_thermostat",
        target_unique_id,
    )
    if not climate_entity_id:
        return

    climate_entry = registry.async_get(climate_entity_id)
    if climate_entry is None or climate_entry.config_entry_id is None:
        return

    await hass.config_entries.async_reload(climate_entry.config_entry_id)


async def _reload_smartpi_vtherms_using_defaults(hass: HomeAssistant) -> None:
    """Reload SmartPI VT entries that do not have a dedicated plugin entry."""
    dedicated_target_unique_ids = _get_dedicated_target_unique_ids(hass)
    component = hass.data.get(CLIMATE_DOMAIN)
    if not component:
        return

    registry = er.async_get(hass)
    vt_entry_ids: set[str] = set()
    for entity in component.entities:
        if getattr(entity, "proportional_function", None) != PROP_FUNCTION_SMART_PI:
            continue
        if getattr(entity, "unique_id", None) in dedicated_target_unique_ids:
            continue

        climate_entry = registry.async_get(entity.entity_id)
        if climate_entry is None or climate_entry.config_entry_id is None:
            continue
        vt_entry_ids.add(climate_entry.config_entry_id)

    if vt_entry_ids:
        await asyncio.gather(
            *(hass.config_entries.async_reload(entry_id) for entry_id in vt_entry_ids)
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up vtherm_smartpi from YAML."""
    del config
    _register_factory(hass)
    _register_services(hass)
    return True


from homeassistant.const import Platform

PLATFORMS = [Platform.SENSOR]


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload affected VT entries when plugin options change."""
    _ensure_domain_data(hass)[f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}"] = True
    await hass.config_entries.async_reload(entry.entry_id)

    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if target_unique_id:
        await _reload_smartpi_vtherms_for_target(hass, target_unique_id)
        return

    await _reload_smartpi_vtherms_using_defaults(hass)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_smartpi from a config entry."""
    data = _ensure_domain_data(hass)
    data[entry.entry_id] = entry.entry_id
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _register_factory(hass)
    _register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    skip_full_reload = data.pop(f"{DATA_SKIP_FULL_RELOAD}_{entry.entry_id}", False)
    # Only reload VT entries when HA is already running (live install/update).
    # During initial startup, VT handles deferred algorithm init via async_startup
    # which fires after EVENT_HOMEASSISTANT_STARTED — reloading here would force
    # entity re-creation before the recorder has restored previous states.
    if hass.state == CoreState.running and not skip_full_reload:
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
