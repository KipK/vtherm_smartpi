"""The vtherm_smartpi integration."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service as service_helper
from vtherm_api.log_collector import get_vtherm_logger
from vtherm_api.vtherm_api import VThermAPI

from .const import (
    ATTR_LEARNING_ENABLED,
    CONF_TARGET_VTHERM,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SERVICE_FORCE_SMARTPI_CALIBRATION,
    SERVICE_RESET_SMARTPI_INTEGRAL,
    SERVICE_RESET_SMARTPI_LEARNING,
    SERVICE_SET_SMARTPI_LEARNING,
)
from .factory import SmartPIHandlerFactory
from .smartpi.compatibility import async_check_vtherm_version

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = get_vtherm_logger(__name__)


def _register_factory(hass: HomeAssistant, api_class: type[VThermAPI] = VThermAPI) -> None:
    """Register the SmartPI factory in the shared VT API."""
    api = api_class.get_vtherm_api(hass)
    if api is None:
        _LOGGER.warning("Unable to register SmartPI factory because VThermAPI is unavailable")
        return

    factory = SmartPIHandlerFactory()
    if api.get_prop_algorithm(factory.name) is None:
        api.register_prop_algorithm(factory)


def _keep_factory_registered(hass: HomeAssistant) -> None:
    """Register the SmartPI factory again when VT rebuilds its API after its last entry is unloaded."""
    # VT cannot run on a bare VThermAPI, so the API is rebuilt with the VT class, as VT does.
    api_class = type(VThermAPI.get_vtherm_api(hass))

    @callback
    def _async_config_entry_changed(_change, entry: ConfigEntry) -> None:
        if entry.domain == "versatile_thermostat" and entry.state is ConfigEntryState.SETUP_IN_PROGRESS:
            _register_factory(hass, api_class)

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_config_entry_changed)


def _register_services(hass: HomeAssistant) -> None:
    """Register SmartPI services on the plugin domain."""
    async def _call_on_vtherms(call, method_name: str, *args) -> None:
        entity_ids = await service_helper.async_extract_entity_ids(call)
        component = hass.data.get(CLIMATE_DOMAIN)
        matched = 0

        for entity_id in sorted(entity_ids if component else ()):
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
            matched += 1
            handler = getattr(entity, method_name, None)
            if handler is None:
                algo_handler = getattr(entity, "_algo_handler", None)
                handler = getattr(algo_handler, method_name, None) if algo_handler is not None else None
            if handler is None:
                _LOGGER.warning(
                    "Service %s not available on %s", method_name, entity.entity_id
                )
                continue
            await handler(*args)

        if not matched:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_smartpi_target",
            )

    async def _handle_reset_learning(call) -> None:
        """Handle SmartPI learning reset with a real async callable."""
        await _call_on_vtherms(call, "service_reset_smart_pi_learning")

    async def _handle_set_learning(call) -> None:
        """Handle SmartPI learning enablement changes."""
        await _call_on_vtherms(
            call,
            "service_set_smartpi_learning",
            call.data[ATTR_LEARNING_ENABLED],
        )

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
        schema=cv.make_entity_service_schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SMARTPI_LEARNING,
        _handle_set_learning,
        schema=cv.make_entity_service_schema(
            {vol.Required(ATTR_LEARNING_ENABLED): cv.boolean}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_SMARTPI_CALIBRATION,
        _handle_force_calibration,
        schema=cv.make_entity_service_schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SMARTPI_INTEGRAL,
        _handle_reset_integral,
        schema=cv.make_entity_service_schema({}),
    )


def _get_dedicated_target_unique_ids(hass: HomeAssistant) -> set[str]:
    """Return VT unique ids that have an enabled dedicated SmartPI config entry."""
    return {
        plugin_entry.data[CONF_TARGET_VTHERM]
        for plugin_entry in hass.config_entries.async_entries(
            DOMAIN, include_disabled=False
        )
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
    if not await async_check_vtherm_version(hass):
        return False
    _register_factory(hass)
    _keep_factory_registered(hass)
    _register_services(hass)
    return True


from homeassistant.const import Platform

PLATFORMS = [Platform.SENSOR]


async def _reload_affected_vtherms(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the VT entries whose effective SmartPI configuration depends on the entry."""
    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if target_unique_id:
        await _reload_smartpi_vtherms_for_target(hass, target_unique_id)
        return

    await _reload_smartpi_vtherms_using_defaults(hass)


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the plugin entry; its setup reloads the affected VT entries."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up vtherm_smartpi from a config entry."""

    async def _reload_affected_vtherms_if_disabled() -> None:
        """Reload the dependent VT entries once a disabled entry is fully unloaded."""
        if entry.disabled_by:
            await _reload_affected_vtherms(hass, entry)

    # Registered first so it runs last (unload callbacks run in reverse order),
    # after the platform and dispatcher listeners of this entry are gone.
    entry.async_on_unload(_reload_affected_vtherms_if_disabled)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Only reload VT entries when HA is already running (live install/update).
    # During initial startup, VT handles deferred algorithm init via async_startup
    # which fires after EVENT_HOMEASSISTANT_STARTED — reloading here would force
    # entity re-creation before the recorder has restored previous states.
    if hass.state == CoreState.running:
        await _reload_affected_vtherms(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a vtherm_smartpi config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the VT entries that were using the removed entry, now absent from the registry."""
    await _reload_affected_vtherms(hass, entry)
