"""Config flow for vtherm_smartpi."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers import selector
from homeassistant.helpers import entity_registry as er
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN

from .const import (
    CONF_CONN_DOOR_SENSOR,
    CONF_CONN_NEIGHBOR_VTHERM,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_SMART_PI_CONNECTIONS,
    CONF_SMART_PI_POWER_SENSOR,
    CONF_SMART_PI_DEADBAND,
    CONF_SMART_PI_DEADBAND_ALLOW_P,
    CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE,
    CONF_SMART_PI_DEBUG,
    CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION,
    CONF_SMART_PI_HYSTERESIS_OFF,
    CONF_SMART_PI_HYSTERESIS_ON,
    CONF_SMART_PI_KNEE_DEMAND,
    CONF_SMART_PI_KNEE_VALVE,
    CONF_SMART_PI_MAX_VALVE,
    CONF_SMART_PI_MIN_VALVE,
    CONF_SMART_PI_RELEASE_TAU_FACTOR,
    CONF_SMART_PI_USE_FF3,
    CONF_SMART_PI_USE_SETPOINT_FILTER,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from .smartpi.device_link import target_uses_smartpi

ERROR_INVALID_VALVE_CURVE = "invalid_valve_curve"
ERROR_CONNECTION_INCOMPLETE = "connection_incomplete"
ERROR_CONNECTION_SELF = "connection_self"
ERROR_CONNECTION_DUPLICATE = "connection_duplicate"
ERROR_CONNECTION_NOT_SMARTPI = "connection_not_smartpi"
CONF_ADD_ANOTHER_CONNECTION = "add_another_connection"
BINARY_SENSOR_DOMAIN = "binary_sensor"
SENSOR_DOMAIN = "sensor"
THERMOSTAT_TYPE_VALVE = "thermostat_over_valve"
THERMOSTAT_TYPE_CLIMATE = "thermostat_over_climate"
AUTO_REGULATION_VALVE = "auto_regulation_valve"
CONF_THERMOSTAT_TYPE_KEY = "thermostat_type"
CONF_AUTO_REGULATION_MODE_KEY = "auto_regulation_mode"


def build_main_options_schema(
    defaults: dict[str, Any],
    *,
    include_valve_linearization: bool,
) -> vol.Schema:
    """Build the SmartPI main options schema."""
    schema = {
        vol.Optional(
            CONF_MINIMAL_ACTIVATION_DELAY,
            default=defaults[CONF_MINIMAL_ACTIVATION_DELAY],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=3600,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Optional(
            CONF_MINIMAL_DEACTIVATION_DELAY,
            default=defaults[CONF_MINIMAL_DEACTIVATION_DELAY],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=3600,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Optional(
            CONF_SMART_PI_DEADBAND,
            default=defaults[CONF_SMART_PI_DEADBAND],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=2.0,
                step=0.01,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_SMART_PI_HYSTERESIS_ON,
            default=defaults[CONF_SMART_PI_HYSTERESIS_ON],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=2.0,
                step=0.01,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_SMART_PI_HYSTERESIS_OFF,
            default=defaults[CONF_SMART_PI_HYSTERESIS_OFF],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=2.0,
                step=0.01,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_SMART_PI_RELEASE_TAU_FACTOR,
            default=defaults[CONF_SMART_PI_RELEASE_TAU_FACTOR],
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=5.0,
                step=0.01,
                mode=selector.NumberSelectorMode.BOX,
            )
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
            CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE,
            default=defaults[CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE],
        ): bool,
        vol.Optional(
            CONF_SMART_PI_DEBUG,
            default=defaults[CONF_SMART_PI_DEBUG],
        ): bool,
    }
    if include_valve_linearization:
        schema[
            vol.Optional(
                CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION,
                default=defaults[CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION],
            )
        ] = bool
    return vol.Schema(schema)


def build_valve_curve_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the SmartPI valve curve schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_SMART_PI_MIN_VALVE,
                default=defaults[CONF_SMART_PI_MIN_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=20,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SMART_PI_KNEE_DEMAND,
                default=defaults[CONF_SMART_PI_KNEE_DEMAND],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=95,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SMART_PI_KNEE_VALVE,
                default=defaults[CONF_SMART_PI_KNEE_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=50,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SMART_PI_MAX_VALVE,
                default=defaults[CONF_SMART_PI_MAX_VALVE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def build_options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the SmartPI defaults schema."""
    return build_main_options_schema(defaults, include_valve_linearization=False)


def build_user_target_schema() -> vol.Schema:
    """Build the SmartPI target thermostat schema."""
    return vol.Schema(
        {
            vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
            )
        }
    )


def build_connections_schema(
    *,
    power_sensor_default: str | None = None,
) -> vol.Schema:
    """Build the room-coupling step schema (power sensor + one connection).

    Connections are added one at a time: fill a neighbour + door and tick
    "add another" to declare more. The power sensor value is preserved across
    iterations via its suggested value.
    """
    power_field = vol.Optional(CONF_SMART_PI_POWER_SENSOR)
    if power_sensor_default:
        power_field = vol.Optional(
            CONF_SMART_PI_POWER_SENSOR,
            description={"suggested_value": power_sensor_default},
        )
    return vol.Schema(
        {
            power_field: selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=SENSOR_DOMAIN,
                    device_class="power",
                )
            ),
            vol.Optional(CONF_CONN_NEIGHBOR_VTHERM): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
            ),
            vol.Optional(CONF_CONN_DOOR_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=BINARY_SENSOR_DOMAIN)
            ),
            vol.Optional(CONF_ADD_ANOTHER_CONNECTION, default=False): bool,
        }
    )


def _resolve_neighbor_unique_id(hass: Any, entity_id: str | None) -> str | None:
    """Resolve a climate entity_id to its VTherm unique id."""
    if not entity_id:
        return None
    registry = er.async_get(hass)
    reg_entry = registry.async_get(entity_id)
    if reg_entry is None:
        return None
    return reg_entry.unique_id


def _validate_connection(
    hass: Any,
    self_unique_id: str | None,
    neighbor_unique_id: str | None,
    existing: list[dict[str, Any]],
) -> str | None:
    """Return an error key if the connection is invalid, else None."""
    if not neighbor_unique_id:
        return ERROR_CONNECTION_INCOMPLETE
    if self_unique_id is not None and neighbor_unique_id == self_unique_id:
        return ERROR_CONNECTION_SELF
    if any(
        conn.get(CONF_CONN_NEIGHBOR_VTHERM) == neighbor_unique_id for conn in existing
    ):
        return ERROR_CONNECTION_DUPLICATE
    if not target_uses_smartpi(hass, neighbor_unique_id):
        return ERROR_CONNECTION_NOT_SMARTPI
    return None


def _apply_connection_submission(
    hass: Any,
    user_input: dict[str, Any],
    self_unique_id: str | None,
    pending_connections: list[dict[str, Any]],
) -> tuple[dict[str, str], str | None, bool]:
    """Process a connections-step submission.

    Appends a valid connection to *pending_connections* in place. Returns
    ``(errors, power_sensor, add_another)``.
    """
    errors: dict[str, str] = {}
    power = user_input.get(CONF_SMART_PI_POWER_SENSOR)
    neighbor_entity = user_input.get(CONF_CONN_NEIGHBOR_VTHERM)
    door = user_input.get(CONF_CONN_DOOR_SENSOR)
    add_another = bool(user_input.get(CONF_ADD_ANOTHER_CONNECTION))

    if neighbor_entity or door:
        if not (neighbor_entity and door):
            errors["base"] = ERROR_CONNECTION_INCOMPLETE
        else:
            neighbor_uid = _resolve_neighbor_unique_id(hass, neighbor_entity)
            err = _validate_connection(
                hass, self_unique_id, neighbor_uid, pending_connections
            )
            if err:
                errors["base"] = err
            else:
                pending_connections.append(
                    {
                        CONF_CONN_NEIGHBOR_VTHERM: neighbor_uid,
                        CONF_CONN_DOOR_SENSOR: door,
                    }
                )
    return errors, power, add_another


def build_user_settings_schema(defaults: dict[str, Any], is_valve: bool) -> vol.Schema:
    """Build the SmartPI per-thermostat settings schema."""
    schema = dict(
        build_main_options_schema(
            defaults,
            include_valve_linearization=is_valve,
        ).schema
    )
    return vol.Schema(schema)


def _schema_defaults(user_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return form defaults merged with the latest submitted values."""
    defaults = dict(DEFAULT_OPTIONS)
    if user_input is not None:
        defaults.update(user_input)
    return defaults


def _is_valve_state(state: Any) -> bool:
    """Return whether a VTherm state exposes a valve command space."""
    attributes = getattr(state, "attributes", {}) or {}
    configuration = attributes.get("configuration") or {}
    if _is_valve_config(configuration):
        return True
    return (
        attributes.get("vtherm_over_valve") is not None
        or attributes.get("vtherm_over_climate_valve", {}).get("have_valve_regulation")
        is True
    )


def _is_valve_config(config: dict[str, Any]) -> bool:
    """Return whether a VTherm config entry targets valve command space."""
    thermostat_type = config.get(CONF_THERMOSTAT_TYPE_KEY) or config.get("type")
    return (
        thermostat_type == THERMOSTAT_TYPE_VALVE
        or (
            thermostat_type == THERMOSTAT_TYPE_CLIMATE
            and config.get(CONF_AUTO_REGULATION_MODE_KEY) == AUTO_REGULATION_VALVE
        )
        or config.get("have_valve_regulation") is True
    )


def _is_valve_entity(hass: Any, entity_id: str, target_unique_id: str) -> bool:
    """Return whether a registered VTherm entity targets valve command space."""
    registry = er.async_get(hass)
    reg_entry = registry.async_get(entity_id)
    if reg_entry is not None and reg_entry.config_entry_id is not None:
        config_entry = hass.config_entries.async_get_entry(reg_entry.config_entry_id)
        if config_entry is not None and _is_valve_config(config_entry.data):
            return True

    state = hass.states.get(entity_id)
    if state is not None:
        return _is_valve_state(state)

    for entry in registry.entities.values():
        if entry.domain != CLIMATE_DOMAIN or entry.unique_id != target_unique_id:
            continue
        fallback_state = hass.states.get(entry.entity_id)
        return fallback_state is not None and _is_valve_state(fallback_state)
    return False


def _validate_valve_curve_config(config: dict[str, Any]) -> dict[str, str]:
    """Validate cross-field valve curve constraints."""
    try:
        min_valve = float(config[CONF_SMART_PI_MIN_VALVE])
        knee_demand = float(config[CONF_SMART_PI_KNEE_DEMAND])
        knee_valve = float(config[CONF_SMART_PI_KNEE_VALVE])
        max_valve = float(config[CONF_SMART_PI_MAX_VALVE])
    except (KeyError, TypeError, ValueError):
        return {"base": ERROR_INVALID_VALVE_CURVE}

    if (
        0.0 <= min_valve < knee_valve < max_valve <= 100.0
        and 0.0 < knee_demand < 100.0
    ):
        return {}
    return {"base": ERROR_INVALID_VALVE_CURVE}


class SmartPIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manage SmartPI plugin config entries."""

    VERSION = 1
    _pending_thermostat_data: dict[str, Any] | None = None
    _pending_thermostat_entity_id: str | None = None
    _pending_thermostat_is_valve: bool = False
    _pending_thermostat_title: str | None = None
    _pending_connections: list[dict[str, Any]] | None = None
    _pending_power_sensor: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Create default plugin settings on first install."""
        if not self._async_current_entries():
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="SmartPI defaults",
                data=dict(DEFAULT_OPTIONS),
            )

        return await self.async_step_thermostat(user_input)

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
        """Select the target thermostat."""
        if user_input is not None:
            entity_id = user_input.get(CONF_TARGET_VTHERM)
            registry = er.async_get(self.hass)
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.unique_id is None:
                return self.async_show_form(
                    step_id="thermostat",
                    data_schema=build_user_target_schema(),
                    errors={CONF_TARGET_VTHERM: "invalid_entity"},
                )

            target_unique_id = reg_entry.unique_id
            await self.async_set_unique_id(f"{DOMAIN}-{target_unique_id}")
            self._abort_if_unique_id_configured()

            self._pending_thermostat_data = {CONF_TARGET_VTHERM: target_unique_id}
            self._pending_thermostat_entity_id = entity_id
            self._pending_thermostat_is_valve = _is_valve_entity(
                self.hass,
                entity_id,
                target_unique_id,
            )
            state = self.hass.states.get(entity_id)
            self._pending_thermostat_title = state.name if state is not None else entity_id
            return await self.async_step_thermostat_settings()

        return self.async_show_form(
            step_id="thermostat",
            data_schema=build_user_target_schema(),
        )

    async def async_step_thermostat_settings(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle the per-thermostat main settings entry."""
        data = dict(self._pending_thermostat_data or {})

        if user_input is not None:
            data.update(user_input)
            self._pending_thermostat_data = data
            if (
                self._pending_thermostat_is_valve
                and user_input.get(CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION)
            ):
                return self.async_show_form(
                    step_id="thermostat_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(user_input)),
                )

            return await self.async_step_connections()

        return self.async_show_form(
            step_id="thermostat_settings",
            data_schema=build_user_settings_schema(
                _schema_defaults(data),
                self._pending_thermostat_is_valve,
            ),
        )

    async def async_step_thermostat_valve_curve(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle the per-thermostat valve curve entry."""
        data = dict(self._pending_thermostat_data or {})

        if user_input is not None:
            data.update(user_input)
            errors = _validate_valve_curve_config(_schema_defaults(data))
            if errors:
                return self.async_show_form(
                    step_id="thermostat_valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(data)),
                    errors=errors,
                )
            self._pending_thermostat_data = data
            return await self.async_step_connections()

        return self.async_show_form(
            step_id="thermostat_valve_curve",
            data_schema=build_valve_curve_schema(_schema_defaults(data)),
        )

    async def async_step_connections(self, user_input: dict[str, Any] | None = None):
        """Declare this room's power sensor and inter-room connections."""
        data = dict(self._pending_thermostat_data or {})
        if self._pending_connections is None:
            self._pending_connections = list(
                data.get(CONF_SMART_PI_CONNECTIONS, []) or []
            )
            self._pending_power_sensor = data.get(CONF_SMART_PI_POWER_SENSOR)

        if user_input is not None:
            errors, power, add_another = _apply_connection_submission(
                self.hass,
                user_input,
                data.get(CONF_TARGET_VTHERM),
                self._pending_connections,
            )
            if power is not None:
                self._pending_power_sensor = power
            if errors:
                return self.async_show_form(
                    step_id="connections",
                    data_schema=build_connections_schema(
                        power_sensor_default=self._pending_power_sensor
                    ),
                    errors=errors,
                )
            if add_another:
                return self.async_show_form(
                    step_id="connections",
                    data_schema=build_connections_schema(
                        power_sensor_default=self._pending_power_sensor
                    ),
                )
            data[CONF_SMART_PI_POWER_SENSOR] = self._pending_power_sensor
            data[CONF_SMART_PI_CONNECTIONS] = self._pending_connections
            return self.async_create_entry(
                title=(
                    self._pending_thermostat_title
                    or self._pending_thermostat_entity_id
                ),
                data=data,
            )

        return self.async_show_form(
            step_id="connections",
            data_schema=build_connections_schema(
                power_sensor_default=self._pending_power_sensor
            ),
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
        self._pending_options_data: dict[str, Any] | None = None
        self._pending_connections: list[dict[str, Any]] | None = None
        self._pending_power_sensor: str | None = None

    async def _finish_or_connections(self, data: dict[str, Any]):
        """Route to the connections step for per-thermostat entries, else finish."""
        self._pending_options_data = data
        if self._config_entry.data.get(CONF_TARGET_VTHERM):
            return await self.async_step_connections()
        return self.async_create_entry(title="", data=data)

    async def async_step_connections(self, user_input: dict[str, Any] | None = None):
        """Edit this room's power sensor and inter-room connections."""
        data = dict(self._pending_options_data or {})
        self_uid = self._config_entry.data.get(CONF_TARGET_VTHERM)
        if self._pending_connections is None:
            self._pending_connections = list(
                data.get(CONF_SMART_PI_CONNECTIONS, []) or []
            )
            self._pending_power_sensor = data.get(CONF_SMART_PI_POWER_SENSOR)

        if user_input is not None:
            errors, power, add_another = _apply_connection_submission(
                self.hass, user_input, self_uid, self._pending_connections
            )
            if power is not None:
                self._pending_power_sensor = power
            if errors:
                return self.async_show_form(
                    step_id="connections",
                    data_schema=build_connections_schema(
                        power_sensor_default=self._pending_power_sensor
                    ),
                    errors=errors,
                )
            if add_another:
                return self.async_show_form(
                    step_id="connections",
                    data_schema=build_connections_schema(
                        power_sensor_default=self._pending_power_sensor
                    ),
                )
            data[CONF_SMART_PI_POWER_SENSOR] = self._pending_power_sensor
            data[CONF_SMART_PI_CONNECTIONS] = self._pending_connections
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="connections",
            data_schema=build_connections_schema(
                power_sensor_default=self._pending_power_sensor
            ),
        )

    def _is_valve_target_entry(self) -> bool:
        """Return whether the edited entry targets a valve thermostat."""
        target_unique_id = self._config_entry.data.get(CONF_TARGET_VTHERM)
        if target_unique_id is None:
            return False

        registry = er.async_get(self.hass)
        for entry in registry.entities.values():
            if entry.domain != CLIMATE_DOMAIN or entry.unique_id != target_unique_id:
                continue
            return _is_valve_entity(
                self.hass,
                entry.entity_id,
                target_unique_id,
            )
        return False

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle the options flow."""
        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)
        is_valve_target = self._is_valve_target_entry()

        if user_input is not None:
            data = dict(defaults)
            data.update(user_input)
            self._pending_options_data = data
            if (
                is_valve_target
                and user_input.get(CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION)
            ):
                return self.async_show_form(
                    step_id="valve_curve",
                    data_schema=build_valve_curve_schema(data),
                )
            return await self._finish_or_connections(data)

        return self.async_show_form(
            step_id="init",
            data_schema=build_main_options_schema(
                defaults,
                include_valve_linearization=is_valve_target,
            ),
        )

    async def async_step_valve_curve(self, user_input: dict[str, Any] | None = None):
        """Handle the options valve curve flow."""
        defaults = dict(DEFAULT_OPTIONS)
        defaults.update(self._config_entry.options or self._config_entry.data)
        data = dict(self._pending_options_data or defaults)

        if user_input is not None:
            data.update(user_input)
            errors = _validate_valve_curve_config(_schema_defaults(data))
            if errors:
                return self.async_show_form(
                    step_id="valve_curve",
                    data_schema=build_valve_curve_schema(_schema_defaults(data)),
                    errors=errors,
                )
            return await self._finish_or_connections(data)

        return self.async_show_form(
            step_id="valve_curve",
            data_schema=build_valve_curve_schema(_schema_defaults(data)),
        )
