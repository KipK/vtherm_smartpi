"""Tests for SmartPI valve runtime synchronization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.command_ownership import (
    CommandOwnershipBindingStatus,
    project_cycle_command,
)
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.feedforward import FFResult
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
async def test_handler_passes_projected_valve_power_to_mid_cycle_binding(
    fake_handler_runtime,
) -> None:
    """A running valve cycle must use the scheduler-realizable command."""
    thermostat = fake_handler_runtime
    thermostat.cycle_scheduler.is_valve_mode = True
    thermostat.cycle_scheduler.is_cycle_running = True
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
        algo._set_linear_output(0.73)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
        patch.object(algo, "on_applied_power_updated") as applied,
    ):
        await handler.control_heating(timestamp=None)

    projection = project_cycle_command(algo.on_percent, thermostat.cycle_min)
    assert applied.call_args.kwargs["on_percent"] == pytest.approx(
        projection.projected_power
    )
    assert applied.call_args.kwargs["projection"] == projection


def _configure_valve_ownership_context(algo: SmartPI, power: float) -> None:
    """Set one complete frozen decomposition for a valve command."""
    algo._last_ff_result = FFResult(
        ff_raw=0.3,
        u_ff1=0.3,
        u_ff2=0.0,
        u_ff_final=0.3,
        u_ff3=0.0,
        u_db_nominal=0.3,
        u_ff_ab=0.3,
        u_ff_trim=0.0,
        u_ff_base=0.3,
        u_ff_eff=0.3,
        ff_reason="ff_none",
    )
    algo.ctl.u_ff = 0.3
    algo.ctl.u_p = 0.1
    algo.ctl.u_i = power - 0.4
    algo.ctl.u_cmd = power
    algo.ctl.last_sat = "NO_SAT"
    algo.ctl.last_i_mode = "I:FREEZE(deadband)"
    algo._last_u_cmd = power
    algo._last_u_limited = power
    algo._last_u_applied = power
    algo._set_linear_output(power)


@pytest.mark.asyncio
async def test_same_valve_power_binds_the_later_frozen_ownership() -> None:
    """An equal actuator command can still begin a distinct PI ownership segment."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=2,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="valve-ownership",
        enable_valve_linearization=True,
    )
    algo.configure_valve_linearization(True, valve_mode=True)
    _configure_valve_ownership_context(algo, 0.73)
    first_projection = project_cycle_command(algo.on_percent, cycle_min=2)
    algo.stage_valve_command(first_projection, VThermHvacMode_HEAT)

    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1.0):
        await algo.on_cycle_started(
            first_projection.on_time_sec,
            first_projection.off_time_sec,
            first_projection.projected_power,
            VThermHvacMode_HEAT,
        )

    _configure_valve_ownership_context(algo, 0.73)
    algo.ctl.u_p = 0.2
    algo.ctl.u_i = 0.23
    frozen_i = algo.ctl.u_i
    second_projection = project_cycle_command(algo.on_percent, cycle_min=2)
    assert second_projection.projected_power == pytest.approx(
        first_projection.projected_power
    )
    assert second_projection.projected_power != pytest.approx(algo.on_percent)
    algo.stage_valve_command(second_projection, VThermHvacMode_HEAT)
    algo.ctl.u_i = 0.4

    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=2.0):
        algo.on_applied_power_updated(
            on_percent=second_projection.projected_power,
            hvac_mode=VThermHvacMode_HEAT,
            projection=second_projection,
        )

    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.BOUND
    assert algo._fftrim_observer._active_ownership is not None
    assert algo._fftrim_observer._active_ownership.u_i == pytest.approx(frozen_i)
    assert algo._fftrim_observer._active_ownership_segments[0].ownership.u_i == pytest.approx(
        0.33
    )
    assert algo._fftrim_observer._active_ownership.linear_committed_power == pytest.approx(
        algo.valve_curve.invert(second_projection.projected_power)
    )


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


def test_non_valve_keeps_min_activation_zero_cut() -> None:
    """Switch-like outputs keep the existing minimum activation behavior."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=60,
        minimal_deactivation_delay=0,
        name="switch-min-on",
    )

    assert algo.update_timing_constraints(0.0, 0.1) == pytest.approx(0.0)
    assert algo.forced_by_timing is True


def test_valve_mode_raises_small_command_to_activation_floor() -> None:
    """Valve commands stay positive when the scheduler timing floor is reachable."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=60,
        minimal_deactivation_delay=0,
        name="valve-min-on",
    )
    algo.configure_valve_linearization(False, valve_mode=True)

    assert algo.update_timing_constraints(0.0, 0.1) == pytest.approx(0.2)
    assert algo.forced_by_timing is True


def test_linearized_valve_uses_actuator_activation_floor() -> None:
    """Valve linearization inverts the actuator floor back to PI demand space."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=30,
        minimal_deactivation_delay=0,
        name="linearized-valve-min-on",
        enable_valve_linearization=True,
    )
    algo.configure_valve_linearization(True, valve_mode=True)

    assert algo.update_timing_constraints(0.0, 0.1) == pytest.approx(0.3)
    assert algo.forced_by_timing is True
