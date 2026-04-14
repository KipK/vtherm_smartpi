"""Config flow for vtherm_smartpi."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers import selector
from homeassistant.helpers import entity_registry as er
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN

from .const import (
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_SMART_PI_DEADBAND,
    CONF_SMART_PI_DEADBAND_ALLOW_P,
    CONF_SMART_PI_DEBUG,
    CONF_SMART_PI_HYSTERESIS_OFF,
    CONF_SMART_PI_HYSTERESIS_ON,
    CONF_SMART_PI_RELEASE_TAU_FACTOR,
    CONF_SMART_PI_USE_FF3,
    CONF_SMART_PI_USE_SETPOINT_FILTER,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
    NAME,
)


def build_options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the SmartPI defaults schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_MINIMAL_ACTIVATION_DELAY,
                default=defaults[CONF_MINIMAL_ACTIVATION_DELAY],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3600, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
            ),
            vol.Optional(
                CONF_MINIMAL_DEACTIVATION_DELAY,
                default=defaults[CONF_MINIMAL_DEACTIVATION_DELAY],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3600, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
            ),
            vol.Optional(
                CONF_SMART_PI_DEADBAND,
                default=defaults[CONF_SMART_PI_DEADBAND],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=2.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_SMART_PI_HYSTERESIS_ON,
                default=defaults[CONF_SMART_PI_HYSTERESIS_ON],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=2.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_SMART_PI_HYSTERESIS_OFF,
                default=defaults[CONF_SMART_PI_HYSTERESIS_OFF],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=2.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_SMART_PI_RELEASE_TAU_FACTOR,
                default=defaults[CONF_SMART_PI_RELEASE_TAU_FACTOR],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=5.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_SMART_PI_USE_SETPOINT_FILTER,
                default=defaults[CONF_SMART_PI_USE_SETPOINT_FILTER],
            ): bool,
            vol.Optional(
                CONF_SMART_PI_USE_FF3,
                default=defaults[CONF_SMART_PI_USE_FF3],
            ): bool,
            vol.Optional(
                CONF_SMART_PI_DEADBAND_ALLOW_P,
                default=defaults[CONF_SMART_PI_DEADBAND_ALLOW_P],
            ): bool,
            vol.Optional(
                CONF_SMART_PI_DEBUG,
                default=defaults[CONF_SMART_PI_DEBUG],
            ): bool,
        }
    )

def build_user_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the SmartPI per-thermostat schema."""
    schema = build_options_schema(defaults).schema.copy()
    schema.update(
        {
            vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
            )
        }
    )
    return vol.Schema(schema)


class SmartPIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a singleton SmartPI plugin entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Show the configuration scope menu."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["thermostat", "global"],
        )

    async def async_step_global(self, user_input: dict[str, Any] | None = None):
        """Handle the global defaults entry."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="SmartPI defaults", data=user_input)

        return self.async_show_form(
            step_id="global",
            data_schema=build_options_schema(DEFAULT_OPTIONS),
        )

    async def async_step_thermostat(self, user_input: dict[str, Any] | None = None):
        """Handle the per-thermostat entry."""
        if user_input is not None:
            entity_id = user_input.get(CONF_TARGET_VTHERM)
            registry = er.async_get(self.hass)
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.unique_id is None:
                return self.async_show_form(
                    step_id="thermostat",
                    data_schema=build_user_schema(DEFAULT_OPTIONS),
                    errors={CONF_TARGET_VTHERM: "invalid_entity"},
                )

            target_unique_id = reg_entry.unique_id
            await self.async_set_unique_id(f"{DOMAIN}-{target_unique_id}")
            self._abort_if_unique_id_configured()

            data = dict(user_input)
            data[CONF_TARGET_VTHERM] = target_unique_id
            state = self.hass.states.get(entity_id)
            title = state.name if state is not None else entity_id
            return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="thermostat",
            data_schema=build_user_schema(DEFAULT_OPTIONS),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return SmartPIOptionsFlow(config_entry)


class SmartPIOptionsFlow(OptionsFlow):
    """Edit SmartPI plugin defaults."""

    def __init__(self, config_entry) -> None:
        """Store the config entry being edited."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle the options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)
        return self.async_show_form(
            step_id="init",
            data_schema=build_options_schema(defaults),
        )
