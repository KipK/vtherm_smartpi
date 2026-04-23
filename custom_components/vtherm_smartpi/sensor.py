"""Sensor platform for vtherm_smartpi."""

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN

from .const import (
    CONF_PROP_FUNCTION,
    CONF_TARGET_VTHERM,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SIGNAL_SMARTPI_TARGET_UPDATED,
)
from .algo import SmartPI
from .smartpi.const import SmartPIPhase

_LOGGER = logging.getLogger(__name__)
VT_DOMAIN = "versatile_thermostat"


def _get_dedicated_target_unique_ids(hass: HomeAssistant) -> set[str]:
    """Return thermostat unique ids that already have a dedicated plugin entry."""
    return {
        plugin_entry.data[CONF_TARGET_VTHERM]
        for plugin_entry in hass.config_entries.async_entries(DOMAIN)
        if plugin_entry.data.get(CONF_TARGET_VTHERM)
    }


def _get_climate_entry(
    hass: HomeAssistant,
    target_unique_id: str,
):
    """Resolve the VT climate registry entry for a thermostat unique id."""
    registry = er.async_get(hass)
    climate_entity_id = registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        VT_DOMAIN,
        target_unique_id,
    )
    if not climate_entity_id:
        return None, None

    climate_entry = registry.async_get(climate_entity_id)
    return climate_entity_id, climate_entry


def _get_default_target_unique_ids(hass: HomeAssistant) -> list[str]:
    """Return SmartPI thermostat unique ids that inherit the global entry."""
    dedicated_target_unique_ids = _get_dedicated_target_unique_ids(hass)
    registry = er.async_get(hass)
    target_unique_ids: list[str] = []

    for reg_entry in registry.entities.values():
        if reg_entry.domain != CLIMATE_DOMAIN:
            continue
        if reg_entry.platform != VT_DOMAIN:
            continue
        if reg_entry.unique_id in dedicated_target_unique_ids:
            continue
        if reg_entry.config_entry_id is None:
            continue

        vt_entry = hass.config_entries.async_get_entry(reg_entry.config_entry_id)
        if vt_entry is None or vt_entry.domain != VT_DOMAIN:
            continue
        if vt_entry.data.get(CONF_PROP_FUNCTION) != PROP_FUNCTION_SMART_PI:
            continue

        target_unique_ids.append(reg_entry.unique_id)

    return target_unique_ids


def _get_diagnostic_state(algo: SmartPI) -> str:
    """Return the top-level diagnostic state for the SmartPI sensor."""
    phase = algo.phase
    if phase == SmartPIPhase.HYSTERESIS:
        return "bootstrap_hysteresis"
    if phase == SmartPIPhase.CALIBRATION:
        return "calibration"
    if phase == SmartPIPhase.STABLE:
        return "stable"
    return str(phase).lower().replace(" ", "_")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the SmartPI sensor platform."""
    if entry.unique_id == DOMAIN:
        target_unique_ids = _get_default_target_unique_ids(hass)
        tracked_unique_ids = set(target_unique_ids)

        @callback
        def _async_add_default_target(target_unique_id: str | None = None) -> None:
            """Add diagnostics for a default-bound thermostat when it becomes SmartPI."""
            default_target_unique_ids = _get_default_target_unique_ids(hass)
            candidates = (
                [target_unique_id]
                if target_unique_id is not None
                else default_target_unique_ids
            )
            new_entities: list[SmartPIDiagnosticSensor] = []
            for candidate_unique_id in candidates:
                if candidate_unique_id in tracked_unique_ids:
                    continue
                if candidate_unique_id not in default_target_unique_ids:
                    continue
                climate_entity_id, climate_entry = _get_climate_entry(
                    hass, candidate_unique_id
                )
                if not climate_entity_id:
                    continue
                tracked_unique_ids.add(candidate_unique_id)
                new_entities.append(
                    SmartPIDiagnosticSensor(
                        hass,
                        climate_entity_id,
                        candidate_unique_id,
                        climate_entry,
                    )
                )

            if new_entities:
                async_add_entities(new_entities)

        entry.async_on_unload(
            async_dispatcher_connect(
                hass, SIGNAL_SMARTPI_TARGET_UPDATED, _async_add_default_target
            )
        )
    else:
        target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
        target_unique_ids = [target_unique_id] if target_unique_id else []

    entities: list[SmartPIDiagnosticSensor] = []
    for target_unique_id in target_unique_ids:
        climate_entity_id, climate_entry = _get_climate_entry(hass, target_unique_id)
        if not climate_entity_id:
            continue
        entities.append(
            SmartPIDiagnosticSensor(
                hass,
                climate_entity_id,
                target_unique_id,
                climate_entry,
            )
        )

    if entities:
        async_add_entities(entities)


class SmartPIDiagnosticSensor(SensorEntity):
    """Diagnostic sensor for SmartPI."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Persist one history sample per SmartPI publication, even when the state
    # stays "active" and the compact attributes happen to be unchanged.
    _attr_force_update = True
    _attr_icon = "mdi:chart-timeline"

    def __init__(self, hass: HomeAssistant, climate_entity_id: str, unique_id_base: str, climate_entry):
        """Initialize the sensor."""
        self.hass = hass
        self._climate_entity_id = climate_entity_id
        self._unique_id_base = unique_id_base
        self._attr_unique_id = f"smartpi_diag_{unique_id_base}"
        self._attr_name = "SmartPI Diagnostics"
        self._attr_native_value = "unknown"
        self._attr_extra_state_attributes = {}
        self._unsub = None
        
        # Link to the underlying device if possible
        if climate_entry and climate_entry.device_id:
            device_registry = dr.async_get(hass)
            device = device_registry.async_get(climate_entry.device_id)
            if device:
                self._attr_device_info = {
                    "identifiers": device.identifiers,
                    "name": device.name,
                    "manufacturer": device.manufacturer,
                    "model": device.model,
                }

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        self._unsub = async_track_state_change_event(
            self.hass, [self._climate_entity_id], self._async_climate_changed
        )
        self._unsub_diag = async_dispatcher_connect(
            self.hass, f"smartpi_diag_update_{self._unique_id_base}", self._async_force_update
        )
        self._update_from_climate()

    async def async_will_remove_from_hass(self):
        """Clean up on removal."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        if hasattr(self, '_unsub_diag') and self._unsub_diag:
            self._unsub_diag()
            self._unsub_diag = None

    @callback
    def _async_climate_changed(self, event):
        """Handle climate state change."""
        self._update_from_climate()
        self.async_write_ha_state()

    @callback
    def _async_force_update(self):
        """Handle forced diagnostic update from SmartPI handler."""
        self._update_from_climate()
        self.async_write_ha_state()

    @callback
    def _update_from_climate(self):
        """Extract SmartPI attributes from the running climate entity."""
        state = self.hass.states.get(self._climate_entity_id)
        if not state:
            return
        
        # We can grab the actual algorithm instance from the climate object directly
        # since it's instantiated there.
        component = self.hass.data.get(CLIMATE_DOMAIN)
        if not component:
            return
            
        vtherm_entity = next((e for e in component.entities if e.entity_id == self._climate_entity_id), None)
        
        if not vtherm_entity:
            return
            
        algo = getattr(vtherm_entity, "prop_algorithm", None)
        if not algo or not isinstance(algo, SmartPI):
            self._attr_native_value = "inactive"
            return

        self._attr_native_value = _get_diagnostic_state(algo)
        if getattr(algo, "_debug_mode", False):
            self._attr_extra_state_attributes = algo.get_debug_diagnostics() or algo.get_published_diagnostics()
        else:
            self._attr_extra_state_attributes = algo.get_published_diagnostics()
