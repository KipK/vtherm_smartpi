"""Tests for the SmartPI config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_smartpi.const import DEFAULT_OPTIONS, DOMAIN


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
