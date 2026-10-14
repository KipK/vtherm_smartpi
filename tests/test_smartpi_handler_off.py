"""Test SmartPIHandler.control_heating behavior when the thermostat is OFF."""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_OFF
from fakes.fake_thermostat_runtime import FakeThermostatRuntime


async def _run_off_control_heating(
    *, is_device_active: bool, is_cycle_running: bool
) -> FakeThermostatRuntime:
    """Drive control_heating in OFF mode and return the thermostat fake."""
    thermostat = FakeThermostatRuntime()
    thermostat.vtherm_hvac_mode = VThermHvacMode_OFF
    thermostat.cycle_min = 5
    thermostat.is_device_active = is_device_active
    thermostat.cycle_scheduler.is_cycle_running = is_cycle_running

    handler = SmartPIHandler(thermostat)

    with (
        patch.object(handler, "_async_save", new_callable=AsyncMock),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    ):
        await handler.control_heating(force=False)

    return thermostat


@pytest.mark.asyncio
async def test_off_cancels_running_cycle_when_device_already_inactive():
    """HVAC OFF must cancel the master cycle even during an already-off PWM phase."""
    thermostat = await _run_off_control_heating(
        is_device_active=False, is_cycle_running=True
    )

    assert thermostat._on_time_sec == 0
    assert thermostat._off_time_sec == 300
    thermostat.cycle_scheduler.cancel_cycle.assert_awaited_once()
    thermostat.async_underlying_entity_turn_off.assert_not_awaited()


@pytest.mark.asyncio
async def test_off_turns_off_active_device_without_cancelling_cycle():
    """HVAC OFF with an active device turns the underlying off."""
    thermostat = await _run_off_control_heating(
        is_device_active=True, is_cycle_running=True
    )

    thermostat.async_underlying_entity_turn_off.assert_awaited_once()
    thermostat.cycle_scheduler.cancel_cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_off_does_nothing_when_device_inactive_and_no_cycle_running():
    """HVAC OFF with an inactive device and no running cycle has nothing to stop."""
    thermostat = await _run_off_control_heating(
        is_device_active=False, is_cycle_running=False
    )

    thermostat.cycle_scheduler.cancel_cycle.assert_not_awaited()
    thermostat.async_underlying_entity_turn_off.assert_not_awaited()
