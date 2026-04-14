"""Sensor platform for vtherm_smartpi."""

import logging
from enum import Enum

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN

from .const import CONF_TARGET_VTHERM, DOMAIN
from .algo import SmartPI

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the SmartPI sensor platform."""
    if entry.unique_id == DOMAIN:
        return

    target_unique_id = entry.data.get(CONF_TARGET_VTHERM)
    if not target_unique_id:
        return

    registry = er.async_get(hass)
    
    # Try looking for versatile thermostat first
    climate_entity_id = registry.async_get_entity_id(CLIMATE_DOMAIN, "versatile_thermostat", target_unique_id)
    
    if not climate_entity_id:
        # Fallback to checking all climates if needed
        return

    climate_entry = registry.async_get(climate_entity_id)

    async_add_entities([SmartPIDiagnosticSensor(hass, climate_entity_id, target_unique_id, climate_entry)])


class SmartPIDiagnosticSensor(SensorEntity):
    """Diagnostic sensor for SmartPI."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:chart-timeline"

    def __init__(self, hass: HomeAssistant, climate_entity_id: str, unique_id_base: str, climate_entry):
        """Initialize the sensor."""
        self.hass = hass
        self._climate_entity_id = climate_entity_id
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
        self._update_from_climate()

    async def async_will_remove_from_hass(self):
        """Clean up on removal."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _async_climate_changed(self, event):
        """Handle climate state change."""
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

        self._attr_native_value = "active"
        self._attr_extra_state_attributes = algo.get_diagnostics()
