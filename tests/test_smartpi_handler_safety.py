"""Tests for the runtime power applied by the SmartPI handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.guards import GuardAction
from fakes.fake_cycle_scheduler import FakeReceiptCycleScheduler
from helpers import force_smartpi_stable_mode, make_receipt


def _make_handler(thermostat, receipts: bool = True) -> tuple[SmartPIHandler, SmartPI]:
    if receipts:
        thermostat.cycle_scheduler = FakeReceiptCycleScheduler()
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler.on_scheduler_ready(thermostat.cycle_scheduler)
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
async def test_normal_mode_applies_the_smartpi_request_untagged(fake_handler_runtime) -> None:
    """Without runtime override the scheduler receives the SmartPI request without a request id."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)

    await _control(handler, algo)

    start = thermostat.cycle_scheduler.start_cycle.await_args
    assert start.args[1] == pytest.approx(0.6)
    assert "request_id" not in start.kwargs
    assert algo.committed_on_percent == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_safety_mode_applies_the_safety_default(fake_handler_runtime) -> None:
    """In VT safety mode the safety power is applied and its receipt is committed."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    await _control(handler, algo)

    thermostat.safety_default_on_percent = 0.25
    await _control(handler, algo)

    start = thermostat.cycle_scheduler.start_cycle.await_args
    assert start.args[1] == pytest.approx(0.25)
    assert "request_id" not in start.kwargs
    assert algo.on_percent == pytest.approx(0.6)

    # VT applies the pending safety request at the next cycle start.
    await algo.on_command_receipt(make_receipt(0.25, cycle_duration_sec=600.0))
    assert algo.committed_on_percent == pytest.approx(0.25)

    thermostat.safety_default_on_percent = None
    await _control(handler, algo)
    start = thermostat.cycle_scheduler.start_cycle.await_args
    assert start.args[1] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_scheduler_without_receipts_commits_the_cycle_start(fake_handler_runtime) -> None:
    """An older VT scheduler has no receipts: the cycle start notification is committed."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat, receipts=False)
    assert handler._receipt_remove is None

    await _control(handler, algo)
    callback = thermostat.cycle_scheduler._cycle_start_callbacks[0]
    await callback(360, 240, 0.6, VThermHvacMode_HEAT)

    assert thermostat.cycle_scheduler.start_cycle.await_args.kwargs == {}
    assert algo.committed_on_percent == pytest.approx(0.6)


def test_remove_unregisters_the_receipt_callback(fake_handler_runtime) -> None:
    """Removing the handler stops the receipt delivery."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    scheduler = thermostat.cycle_scheduler
    assert scheduler._receipt_callbacks == [algo.on_command_receipt]
    assert handler._receipt_remove is not None
    handler._store = None

    handler.remove()

    assert scheduler._receipt_callbacks == []
    assert handler._receipt_remove is None


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


@pytest.mark.asyncio
async def test_safety_mode_flags_the_integral_hold_before_calculate(fake_handler_runtime) -> None:
    """The VT safety state, read before calculate, drives the integral hold flag."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    seen: list[bool] = []
    real_calculate = algo.calculate
    algo.calculate = lambda **kw: seen.append(algo.vt_safety_override)

    thermostat.safety_default_on_percent = 0.25
    await handler.control_heating(timestamp=None)
    thermostat.safety_default_on_percent = None
    await handler.control_heating(timestamp=None)

    algo.calculate = real_calculate
    assert seen == [True, False]
    assert algo.vt_safety_override is False


@pytest.mark.asyncio
async def test_max_on_clamp_mismatch_is_not_a_safety_hold(fake_handler_runtime) -> None:
    """A VT clamp of the runtime power outside safety mode never holds the integral."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    seen: list[bool] = []

    with patch.object(
        type(thermostat), "on_percent", new_callable=PropertyMock, return_value=0.5
    ):
        await _control(handler, algo)
        algo.calculate = lambda **kw: seen.append(algo.vt_safety_override)
        await handler.control_heating(timestamp=None)

    assert seen == [False]
    assert algo.vt_safety_override is False


@pytest.mark.asyncio
async def test_runtime_without_safety_manager_never_holds(fake_handler_runtime) -> None:
    """A runtime exposing no safety manager, or a non-boolean state, does not hold."""
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    seen: list[bool] = []
    algo.calculate = lambda **kw: seen.append(algo.vt_safety_override)

    for manager in (None, MagicMock()):
        with patch.object(
            type(thermostat), "safety_manager", new_callable=PropertyMock, return_value=manager
        ):
            await handler.control_heating(timestamp=None)

    assert seen == [False, False]
