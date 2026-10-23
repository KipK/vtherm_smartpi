"""Tests for the SmartPI config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.vtherm_smartpi.const import (
    CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE,
    CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION,
    CONF_SMART_PI_KNEE_DEMAND,
    CONF_SMART_PI_KNEE_VALVE,
    CONF_SMART_PI_MAX_VALVE,
    CONF_SMART_PI_MIN_VALVE,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.vtherm_smartpi.config_flow import (
    _validate_valve_curve_config,
    build_options_schema,
    build_valve_curve_schema,
)

VT_DOMAIN = "versatile_thermostat"


def _schema_keys(schema) -> set[str]:
    """Return normalized voluptuous schema keys."""
    return {getattr(key, "schema", key) for key in schema.schema}


def _mock_config_entry_lookup(hass, monkeypatch, config_entry: MockConfigEntry) -> None:
    """Expose a config entry to the registry lookup without setting up its domain."""
    original_async_get_entry = hass.config_entries.async_get_entry

    def async_get_entry(entry_id: str):
        if entry_id == config_entry.entry_id:
            return config_entry
        return original_async_get_entry(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_get_entry", async_get_entry)


@pytest.mark.asyncio
async def test_first_user_step_creates_default_entry(hass) -> None:
    """The first flow run must create the default global entry immediately."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartPI defaults"
    assert result["data"] == DEFAULT_OPTIONS


@pytest.mark.asyncio
async def test_user_step_shows_thermostat_form_when_entry_already_exists(hass) -> None:
    """Later flow runs must open the thermostat form directly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat"


@pytest.mark.asyncio
async def test_user_step_recreates_global_entry_when_only_dedicated_entries_exist(hass) -> None:
    """The global entry must be recreated when it was deleted but dedicated entries remain."""
    dedicated = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated",
        unique_id=f"{DOMAIN}-switch-target",
        data={CONF_TARGET_VTHERM: "switch-target"},
    )
    dedicated.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartPI defaults"
    assert result["data"] == DEFAULT_OPTIONS
    assert result["result"].unique_id == DOMAIN


def test_global_options_schema_hides_valve_linearization() -> None:
    """Global defaults must not expose valve-only linearization settings."""
    schema_keys = _schema_keys(build_options_schema(DEFAULT_OPTIONS))

    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION not in schema_keys


def test_global_options_schema_exposes_pwm_cycle_force_option() -> None:
    """Global defaults must expose the PWM cycle force setting."""
    schema_keys = _schema_keys(build_options_schema(DEFAULT_OPTIONS))

    assert CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE in schema_keys
    assert DEFAULT_OPTIONS[CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE] is False


@pytest.mark.asyncio
async def test_thermostat_flow_hides_valve_linearization_for_non_valve(hass) -> None:
    """The per-thermostat settings form must hide valve fields for switch-like targets."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "switch-target",
        suggested_object_id="switch_target",
    )
    hass.states.async_set(
        climate_entry.entity_id,
        "heat",
        {"configuration": {"type": "thermostat_over_switch"}},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"target_vtherm_unique_id": climate_entry.entity_id},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_settings"
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION not in schema_keys


@pytest.mark.asyncio
async def test_thermostat_flow_shows_valve_linearization_from_vtherm_config_entry(
    hass,
    monkeypatch,
) -> None:
    """Valve targets must be detected from the VT config entry when state is not ready."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="valve-config-target",
        data={"thermostat_type": "thermostat_over_valve"},
    )
    _mock_config_entry_lookup(hass, monkeypatch, vt_entry)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-config-target",
        suggested_object_id="valve_config_target",
        config_entry=vt_entry,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TARGET_VTHERM: climate_entry.entity_id},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_settings"
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION in schema_keys


@pytest.mark.asyncio
async def test_thermostat_flow_shows_valve_curve_step_for_valve(hass) -> None:
    """Valve targets must get the valve curve step when linearization is enabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SmartPI defaults",
        unique_id=DOMAIN,
        data=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-target",
        suggested_object_id="valve_target",
    )
    hass.states.async_set(
        climate_entry.entity_id,
        "heat",
        {"configuration": {"type": "thermostat_over_valve"}},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"target_vtherm_unique_id": climate_entry.entity_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "thermostat_valve_curve"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SMART_PI_MIN_VALVE: 15.0,
            CONF_SMART_PI_KNEE_DEMAND: 80.0,
            CONF_SMART_PI_KNEE_VALVE: 15.0,
            CONF_SMART_PI_MAX_VALVE: 100.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SMART_PI_MIN_VALVE: "valve_min_not_below_knee"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SMART_PI_MIN_VALVE: 7.0,
            CONF_SMART_PI_KNEE_DEMAND: 80.0,
            CONF_SMART_PI_KNEE_VALVE: 15.0,
            CONF_SMART_PI_MAX_VALVE: 100.0,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION] is True


def _valve_curve(min_valve, knee_demand, knee_valve, max_valve) -> dict[str, float]:
    """Return a valve curve form payload."""
    return {
        CONF_SMART_PI_MIN_VALVE: min_valve,
        CONF_SMART_PI_KNEE_DEMAND: knee_demand,
        CONF_SMART_PI_KNEE_VALVE: knee_valve,
        CONF_SMART_PI_MAX_VALVE: max_valve,
    }


def test_valve_curve_schema_accepts_high_minimum_opening() -> None:
    """A valve that only heats above a high opening must be configurable."""
    schema = build_valve_curve_schema(DEFAULT_OPTIONS)
    payload = _valve_curve(28.0, 80.0, 35.0, 100.0)

    assert schema(payload) == payload
    assert _validate_valve_curve_config(payload) == {}


@pytest.mark.parametrize(
    "field",
    [
        CONF_SMART_PI_MIN_VALVE,
        CONF_SMART_PI_KNEE_DEMAND,
        CONF_SMART_PI_KNEE_VALVE,
        CONF_SMART_PI_MAX_VALVE,
    ],
)
@pytest.mark.parametrize("value", [-1.0, 101.0])
def test_valve_curve_schema_rejects_values_outside_percent_range(field, value) -> None:
    """Each valve curve value must stay a percentage."""
    schema = build_valve_curve_schema(DEFAULT_OPTIONS)
    payload = _valve_curve(7.0, 80.0, 15.0, 100.0)
    payload[field] = value

    with pytest.raises(vol.Invalid):
        schema(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            _valve_curve(35.0, 80.0, 35.0, 100.0),
            {CONF_SMART_PI_MIN_VALVE: "valve_min_not_below_knee"},
        ),
        (
            _valve_curve(7.0, 80.0, 60.0, 60.0),
            {CONF_SMART_PI_KNEE_VALVE: "valve_knee_not_below_max"},
        ),
        (
            _valve_curve(7.0, 0.0, 15.0, 100.0),
            {CONF_SMART_PI_KNEE_DEMAND: "knee_demand_out_of_range"},
        ),
        (
            _valve_curve(7.0, 100.0, 15.0, 100.0),
            {CONF_SMART_PI_KNEE_DEMAND: "knee_demand_out_of_range"},
        ),
        (
            _valve_curve(50.0, 100.0, 40.0, 30.0),
            {
                CONF_SMART_PI_MIN_VALVE: "valve_min_not_below_knee",
                CONF_SMART_PI_KNEE_VALVE: "valve_knee_not_below_max",
                CONF_SMART_PI_KNEE_DEMAND: "knee_demand_out_of_range",
            },
        ),
    ],
)
def test_valve_curve_validation_reports_the_field_to_correct(payload, expected) -> None:
    """Invalid valve curves must point at the field the user has to change."""
    assert _validate_valve_curve_config(payload) == expected


@pytest.mark.asyncio
async def test_options_flow_shows_valve_linearization_from_dedicated_target_config(
    hass,
    monkeypatch,
) -> None:
    """Dedicated valve options must expose the linearization switch."""
    plugin_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dedicated SmartPI",
        unique_id=f"{DOMAIN}-valve-options-target",
        data={CONF_TARGET_VTHERM: "valve-options-target", **DEFAULT_OPTIONS},
    )
    plugin_entry.add_to_hass(hass)

    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id="valve-options-target",
        data={
            "thermostat_type": "thermostat_over_climate",
            "auto_regulation_mode": "auto_regulation_valve",
        },
    )
    _mock_config_entry_lookup(hass, monkeypatch, vt_entry)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        VT_DOMAIN,
        "valve-options-target",
        suggested_object_id="valve_options_target",
        config_entry=vt_entry,
    )

    result = await hass.config_entries.options.async_init(plugin_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    schema_keys = _schema_keys(result["data_schema"])
    assert CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION in schema_keys
