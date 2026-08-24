"""Tests for SmartPI Home Assistant services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.vtherm_smartpi import _register_services
from custom_components.vtherm_smartpi.const import (
    ATTR_LEARNING_ENABLED,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SERVICE_SET_SMARTPI_LEARNING,
)


async def test_set_learning_service_routes_required_boolean(monkeypatch) -> None:
    """The HA service must validate and route the learning boolean."""
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.service_helper.async_extract_entity_ids",
        AsyncMock(return_value={"climate.test"}),
    )
    entity = MagicMock()
    entity.entity_id = "climate.test"
    entity.proportional_function = PROP_FUNCTION_SMART_PI
    entity.service_set_smartpi_learning = AsyncMock()
    component = MagicMock()
    component.get_entity.return_value = entity

    hass = MagicMock()
    hass.data = {"climate": component}
    _register_services(hass)

    registration = next(
        call
        for call in hass.services.async_register.call_args_list
        if call.args[:2] == (DOMAIN, SERVICE_SET_SMARTPI_LEARNING)
    )
    service_handler = registration.args[2]
    schema = registration.kwargs["schema"]
    call = MagicMock()
    call.target = None
    call.data = {
        "entity_id": "climate.test",
        ATTR_LEARNING_ENABLED: False,
    }

    await service_handler(call)

    entity.service_set_smartpi_learning.assert_awaited_once_with(False)
    assert schema(call.data)[ATTR_LEARNING_ENABLED] is False
    with pytest.raises(vol.Invalid):
        schema({"entity_id": "climate.test"})
