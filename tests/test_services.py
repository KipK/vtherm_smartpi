"""Tests for SmartPI Home Assistant services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.vtherm_smartpi import _register_services
from custom_components.vtherm_smartpi.const import (
    ATTR_LEARNING_ENABLED,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SERVICE_FORCE_SMARTPI_CALIBRATION,
    SERVICE_RESET_SMARTPI_INTEGRAL,
    SERVICE_RESET_SMARTPI_LEARNING,
    SERVICE_SET_SMARTPI_LEARNING,
)


def _make_entity(entity_id: str, proportional_function: str = PROP_FUNCTION_SMART_PI) -> MagicMock:
    """Build a fake climate entity exposing every SmartPI service method."""
    entity = MagicMock()
    entity.entity_id = entity_id
    entity.proportional_function = proportional_function
    entity.service_set_smartpi_learning = AsyncMock()
    entity.service_reset_smart_pi_learning = AsyncMock()
    entity.service_force_smartpi_calibration = AsyncMock()
    entity.service_reset_smartpi_integral = AsyncMock()
    return entity


def _register(monkeypatch, entities: dict[str, MagicMock], extracted: set[str]):
    """Register the services on a fake hass and return it with the registrations."""
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.service_helper.async_extract_entity_ids",
        AsyncMock(return_value=extracted),
    )
    component = MagicMock()
    component.get_entity.side_effect = entities.get
    hass = MagicMock()
    hass.data = {"climate": component}
    _register_services(hass)
    registrations = {
        call.args[1]: call
        for call in hass.services.async_register.call_args_list
        if call.args[0] == DOMAIN
    }
    return hass, registrations


def _service_call(hass, service: str, data: dict) -> ServiceCall:
    """Build a real ServiceCall."""
    return ServiceCall(hass, DOMAIN, service, data)


async def test_set_learning_service_routes_required_boolean(monkeypatch) -> None:
    """The HA service must validate and route the learning boolean."""
    entity = _make_entity("climate.test")
    hass, registrations = _register(monkeypatch, {"climate.test": entity}, {"climate.test"})
    registration = registrations[SERVICE_SET_SMARTPI_LEARNING]
    service_handler = registration.args[2]
    schema = registration.kwargs["schema"]
    data = {
        "entity_id": "climate.test",
        ATTR_LEARNING_ENABLED: False,
    }

    validated = schema(data)
    await service_handler(_service_call(hass, SERVICE_SET_SMARTPI_LEARNING, validated))

    entity.service_set_smartpi_learning.assert_awaited_once_with(False)
    assert validated[ATTR_LEARNING_ENABLED] is False
    with pytest.raises(vol.Invalid):
        schema({"entity_id": "climate.test"})


@pytest.mark.parametrize(
    ("service", "method"),
    [
        (SERVICE_RESET_SMARTPI_LEARNING, "service_reset_smart_pi_learning"),
        (SERVICE_FORCE_SMARTPI_CALIBRATION, "service_force_smartpi_calibration"),
        (SERVICE_RESET_SMARTPI_INTEGRAL, "service_reset_smartpi_integral"),
    ],
)
async def test_schema_less_services_accept_standard_target_fields_only(
    monkeypatch, service: str, method: str
) -> None:
    """Services without own fields must accept target fields and reject anything else."""
    entity = _make_entity("climate.test")
    hass, registrations = _register(monkeypatch, {"climate.test": entity}, {"climate.test"})
    registration = registrations[service]
    schema = registration.kwargs["schema"]

    validated = schema({"entity_id": "climate.test"})
    await registration.args[2](_service_call(hass, service, validated))

    getattr(entity, method).assert_awaited_once_with()
    schema({"area_id": "living_room"})
    with pytest.raises(vol.Invalid):
        schema({"entity_id": "climate.test", "unknown": 1})
    with pytest.raises(vol.Invalid):
        schema({})


async def test_service_skips_non_smartpi_entities(monkeypatch) -> None:
    """Only SmartPI VTherms of the resolved targets must be called."""
    smartpi = _make_entity("climate.smartpi")
    other = _make_entity("climate.other", proportional_function="tpi")
    hass, registrations = _register(
        monkeypatch,
        {"climate.smartpi": smartpi, "climate.other": other},
        {"climate.smartpi", "climate.other"},
    )
    registration = registrations[SERVICE_RESET_SMARTPI_INTEGRAL]

    await registration.args[2](_service_call(hass, SERVICE_RESET_SMARTPI_INTEGRAL, {}))

    smartpi.service_reset_smartpi_integral.assert_awaited_once_with()
    other.service_reset_smartpi_integral.assert_not_awaited()


@pytest.mark.parametrize(
    "extracted",
    [
        set(),
        {"climate.unknown"},
        {"climate.other"},
    ],
)
async def test_service_raises_when_no_target_is_a_smartpi_vtherm(
    monkeypatch, extracted: set[str]
) -> None:
    """A call resolving to no SmartPI VTherm must raise a translated validation error."""
    other = _make_entity("climate.other", proportional_function="tpi")
    hass, registrations = _register(monkeypatch, {"climate.other": other}, extracted)
    registration = registrations[SERVICE_RESET_SMARTPI_LEARNING]

    with pytest.raises(ServiceValidationError) as err:
        await registration.args[2](_service_call(hass, SERVICE_RESET_SMARTPI_LEARNING, {}))

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "no_smartpi_target"
    other.service_reset_smart_pi_learning.assert_not_awaited()
