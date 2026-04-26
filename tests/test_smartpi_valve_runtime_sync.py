"""Tests for SmartPI valve runtime synchronization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.guards import GuardAction


@pytest.mark.asyncio
async def test_handler_syncs_applied_power_after_valve_mid_cycle_update(
    fake_handler_runtime,
) -> None:
    """A valve command applied mid-cycle must refresh SmartPI runtime power."""
    thermostat = fake_handler_runtime
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler._async_save = AsyncMock()

    algo = thermostat.prop_algorithm
    assert isinstance(algo, SmartPI)

    thermostat.cycle_scheduler.is_valve_mode = True
    thermostat.cycle_scheduler.is_cycle_running = True

    algo._committed_on_percent = 0.2
    algo.u_prev = 0.2
    algo._last_u_applied = 0.2
    algo.guards.check_guard_cut = MagicMock(return_value=GuardAction.NONE)
    algo.guards.check_guard_kick = MagicMock(return_value=GuardAction.NONE)
    algo.deadband_mgr._last_deadband_changed = False
    algo.deadband_mgr._last_near_band_changed = False

    def fake_calculate(*_args, **_kwargs) -> None:
        algo._set_linear_output(0.6)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
    ):
        await handler.control_heating(timestamp=None)

    assert algo.committed_on_percent == pytest.approx(0.6)
    assert algo.u_prev == pytest.approx(0.6)
    assert algo.u_applied == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_handler_sends_linearized_valve_command(
    fake_handler_runtime,
) -> None:
    """A linear SmartPI demand must be mapped before reaching the scheduler."""
    thermostat = fake_handler_runtime
    thermostat.cycle_scheduler.is_valve_mode = True
    thermostat.entry_infos.update(
        {
            "smart_pi_enable_valve_linearization": True,
            "smart_pi_min_valve": 7.0,
            "smart_pi_knee_demand": 80.0,
            "smart_pi_knee_valve": 15.0,
            "smart_pi_max_valve": 100.0,
        }
    )
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler._async_save = AsyncMock()

    algo = thermostat.prop_algorithm
    assert isinstance(algo, SmartPI)
    algo.guards.check_guard_cut = MagicMock(return_value=GuardAction.NONE)
    algo.guards.check_guard_kick = MagicMock(return_value=GuardAction.NONE)
    algo.deadband_mgr._last_deadband_changed = False
    algo.deadband_mgr._last_near_band_changed = False

    def fake_calculate(*_args, **_kwargs) -> None:
        algo._set_linear_output(0.8)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
    ):
        await handler.control_heating(timestamp=None)

    thermostat.cycle_scheduler.start_cycle.assert_awaited_once()
    assert thermostat.cycle_scheduler.start_cycle.await_args.args[1] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_valve_feedback_is_projected_to_linear_state() -> None:
    """Actuator feedback must be converted before updating PI state."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="valve-feedback",
        enable_valve_linearization=True,
    )

    await algo.on_cycle_started(0.0, 0.0, 0.15, "heat")

    assert algo.committed_on_percent == pytest.approx(0.15)
    assert algo.linear_committed_on_percent == pytest.approx(0.8)
    assert algo.u_prev == pytest.approx(0.8)
    assert algo.linear_u_applied == pytest.approx(0.8)
