"""Tests for the runtime power applied by the SmartPI handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.command_ownership import (
    CommandOwnershipBindingStatus,
)
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.feedforward import FFResult
from custom_components.vtherm_smartpi.smartpi.guards import GuardAction
from helpers import force_smartpi_stable_mode


def _make_handler(thermostat) -> tuple[SmartPIHandler, SmartPI]:
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler._async_save = AsyncMock()
    algo = thermostat.prop_algorithm
    assert isinstance(algo, SmartPI)
    algo.guards.check_guard_cut = MagicMock(return_value=GuardAction.NONE)
    algo.guards.check_guard_kick = MagicMock(return_value=GuardAction.NONE)
    algo.deadband_mgr._last_deadband_changed = False
    algo.deadband_mgr._last_near_band_changed = False
    force_smartpi_stable_mode(algo)
    return handler, algo


async def _control(handler: SmartPIHandler, algo: SmartPI, power: float = 0.6) -> None:
    def fake_calculate(*_args, **_kwargs) -> None:
        # One internally consistent control decomposition so ownership can stage.
        algo._last_ff_result = FFResult(
            u_ff1=0.3, u_ff2=0.0, u_ff_final=0.3, u_ff3=0.0,
            u_ff_trim=0.0, u_ff_eff=0.3, ff_reason="ff_none",
        )
        algo.ctl.u_ff, algo.ctl.u_p, algo.ctl.u_i = 0.3, 0.1, power - 0.4
        algo.ctl.u_cmd = power
        algo.ctl.last_sat = "NO_SAT"
        algo.ctl.last_i_mode = "I:FREEZE(deadband)"
        algo._last_u_cmd = algo._last_u_limited = algo._last_u_applied = power
        algo._set_linear_output(power)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
    ):
        await handler.control_heating(timestamp=None)


@pytest.mark.asyncio
async def test_normal_mode_applies_and_stages_the_smartpi_request(fake_handler_runtime) -> None:
    """Without runtime override the scheduler receives the SmartPI request."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)

    await _control(handler, algo)

    assert thermostat.cycle_scheduler.start_cycle.await_args.args[1] == pytest.approx(0.6)
    binding = algo._command_ownership.last_binding
    assert binding.status == CommandOwnershipBindingStatus.PENDING
    assert binding.snapshot is not None


@pytest.mark.asyncio
async def test_safety_mode_applies_the_safety_default_without_ownership(fake_handler_runtime) -> None:
    """In VT safety mode the safety power is applied and never owned by SmartPI."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    await _control(handler, algo)
    assert algo._command_ownership.pending is not None

    thermostat.safety_default_on_percent = 0.25
    await _control(handler, algo)

    assert thermostat.cycle_scheduler.start_cycle.await_args.args[1] == pytest.approx(0.25)
    assert algo.on_percent == pytest.approx(0.6)
    assert algo._command_ownership.pending is None
    assert algo._command_ownership.last_binding.reason == "runtime_power_override"

    await algo.on_cycle_started(150, 450, 0.25, VThermHvacMode_HEAT)
    binding = algo._command_ownership.last_binding
    assert binding.status == CommandOwnershipBindingStatus.REJECTED
    assert binding.reason == "ownership_request_missing"

    thermostat.safety_default_on_percent = None
    await _control(handler, algo)
    assert thermostat.cycle_scheduler.start_cycle.await_args.args[1] == pytest.approx(0.6)
    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.PENDING


@pytest.mark.asyncio
async def test_unknown_runtime_power_keeps_the_current_switch_state(fake_handler_runtime) -> None:
    """A None runtime power leaves the scheduler and the switch untouched."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)

    with patch.object(
        type(thermostat), "on_percent", new_callable=PropertyMock, return_value=None
    ):
        await _control(handler, algo)

    thermostat.cycle_scheduler.start_cycle.assert_not_awaited()
    thermostat.cycle_scheduler.cancel_cycle.assert_not_awaited()
    thermostat.async_underlying_entity_turn_off.assert_not_awaited()
