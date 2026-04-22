"""Tests for SmartPI valve runtime synchronization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    algo.phase = SmartPIPhase.STABLE
    algo.guards.check_guard_cut = MagicMock(return_value=GuardAction.NONE)
    algo.guards.check_guard_kick = MagicMock(return_value=GuardAction.NONE)
    algo.deadband_mgr.deadband_changed = False
    algo.deadband_mgr.near_band_changed = False

    def fake_calculate(*_args, **_kwargs) -> None:
        algo._on_percent = 0.6

    with patch.object(algo, "calculate", side_effect=fake_calculate):
        await handler.control_heating(timestamp=None)

    assert algo.committed_on_percent == pytest.approx(0.6)
    assert algo.u_prev == pytest.approx(0.6)
    assert algo.u_applied == pytest.approx(0.6)
