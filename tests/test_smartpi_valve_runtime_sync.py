"""Tests for SmartPI valve runtime synchronization."""

from __future__ import annotations

import time
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
from fakes.fake_cycle_scheduler import FakeReceiptCycleScheduler
from helpers import make_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("receipts", [False, True])
async def test_handler_syncs_applied_power_after_valve_mid_cycle_update(
    fake_handler_runtime, receipts: bool
) -> None:
    """A valve command applied mid-cycle must refresh SmartPI runtime power."""
    thermostat = fake_handler_runtime
    if receipts:
        thermostat.cycle_scheduler = FakeReceiptCycleScheduler()
    else:
        # Without receipts the handler reads the published valve position back.
        thermostat.valve_open_percent = 60
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler.on_scheduler_ready(thermostat.cycle_scheduler)
    handler._async_save = AsyncMock()

    algo = thermostat.prop_algorithm
    assert isinstance(algo, SmartPI)
    assert algo.receipts_supported is receipts

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
async def test_legacy_valve_update_without_published_position_commits_nothing(
    fake_handler_runtime,
) -> None:
    """Without receipts and without valve position nothing is committed."""
    thermostat = fake_handler_runtime
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler.on_scheduler_ready(thermostat.cycle_scheduler)
    handler._async_save = AsyncMock()
    algo = thermostat.prop_algorithm
    thermostat.cycle_scheduler.is_valve_mode = True
    thermostat.cycle_scheduler.is_cycle_running = True
    algo._committed_on_percent = 0.2
    algo.guards.check_guard_cut = MagicMock(return_value=GuardAction.NONE)
    algo.guards.check_guard_kick = MagicMock(return_value=GuardAction.NONE)

    def fake_calculate(*_args, **_kwargs) -> None:
        algo._set_linear_output(0.6)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
    ):
        await handler.control_heating(timestamp=None)

    assert "request_id" not in thermostat.cycle_scheduler.start_cycle.await_args.kwargs
    assert algo.committed_on_percent == pytest.approx(0.2)
    open_interval = algo._causal_power_trace.read_window(0.0, time.monotonic() + 1.0).discontinuities
    assert open_interval[-1].reason == "missing_valve_position"


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
async def test_legacy_handler_reads_back_the_published_valve_position(
    fake_handler_runtime,
) -> None:
    """Without receipts a running valve cycle commits the published position."""
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
        thermostat.valve_open_percent = round(algo.on_percent * 100)

    with (
        patch.object(type(algo), "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(algo, "calculate", side_effect=fake_calculate),
        patch.object(algo, "on_applied_power_updated") as applied,
    ):
        await handler.control_heating(timestamp=None)

    assert applied.call_args.args[0] == pytest.approx(
        thermostat.valve_open_percent / 100
    )


def _configure_valve_ownership_context(algo: SmartPI, power: float) -> None:
    """Set one complete frozen decomposition for a valve command."""
    algo._last_ff_result = FFResult(
        u_ff1=0.3,
        u_ff2=0.0,
        u_ff_final=0.3,
        u_ff3=0.0,
        u_ff_trim=0.0,
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


def _new_valve_algo(name: str) -> SmartPI:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=2,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name=name,
        enable_valve_linearization=True,
    )
    algo.configure_valve_linearization(True, valve_mode=True)
    return algo


@pytest.mark.asyncio
async def test_same_valve_power_binds_the_later_frozen_ownership() -> None:
    """An equal actuator command can still begin a distinct PI ownership segment."""
    algo = _new_valve_algo("valve-ownership")
    _configure_valve_ownership_context(algo, 0.73)
    published_power = round(algo.on_percent * 100) / 100
    first_id = algo.stage_command(VThermHvacMode_HEAT)

    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1.0):
        await algo.on_command_receipt(
            make_receipt(first_id, published_power, requested=algo.on_percent)
        )

    _configure_valve_ownership_context(algo, 0.73)
    algo.ctl.u_p = 0.2
    algo.ctl.u_i = 0.23
    frozen_i = algo.ctl.u_i
    second_id = algo.stage_command(VThermHvacMode_HEAT)
    algo.ctl.u_i = 0.4

    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=2.0):
        await algo.on_command_receipt(
            make_receipt(second_id, published_power, kind="update", requested=algo.on_percent)
        )

    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.BOUND
    trace = algo._causal_power_trace
    assert algo._fftrim_observer.physical_trace is trace
    assert trace._active_ownership is not None
    assert trace._active_ownership.u_i == pytest.approx(frozen_i)
    assert trace.active_ownership_segments[0].ownership.u_i == pytest.approx(
        0.33
    )
    assert trace._active_ownership.linear_committed_power == pytest.approx(
        algo.valve_curve.invert(published_power)
    )


@pytest.mark.asyncio
async def test_retained_and_deferred_valve_updates_are_not_owned() -> None:
    """A valve position held back by VT or set by its deferred update is unowned."""
    algo = _new_valve_algo("valve-retained")
    _configure_valve_ownership_context(algo, 0.5)
    request_id = algo.stage_command(VThermHvacMode_HEAT)
    await algo.on_command_receipt(make_receipt(request_id, 0.5))
    assert algo._causal_power_trace._active_ownership is not None

    retained_id = algo.stage_command(VThermHvacMode_HEAT)
    await algo.on_command_receipt(
        make_receipt(retained_id, 0.45, kind="update", requested=0.5, alterations=("valve_retained",))
    )
    binding = algo._command_ownership.last_binding
    assert binding.reason == "ownership_actuator_retained"
    assert algo._causal_power_trace._active_ownership is None
    assert algo.committed_on_percent == pytest.approx(0.45)

    await algo.on_command_receipt(make_receipt(None, 0.5, kind="update"))
    assert algo._command_ownership.last_binding.reason == "ownership_request_untagged"
    assert algo._causal_power_trace._active_ownership is None
    assert algo.committed_on_percent == pytest.approx(0.5)


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
    assert algo._last_u_applied == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_legacy_valve_cycle_start_commits_the_published_position_unowned(
    fake_handler_runtime,
) -> None:
    """Without receipts the cycle start commits the published valve position."""
    thermostat = fake_handler_runtime
    thermostat.cycle_scheduler.is_valve_mode = True
    thermostat.valve_open_percent = 13
    thermostat.cycle_min = 2
    handler = SmartPIHandler(thermostat)
    handler.init_algorithm()
    handler.on_scheduler_ready(thermostat.cycle_scheduler)
    algo = thermostat.prop_algorithm
    assert isinstance(algo, SmartPI)
    _configure_valve_ownership_context(algo, 0.132)

    callback = thermostat.cycle_scheduler._cycle_start_callbacks[0]
    await callback(15, 104, 0.125, VThermHvacMode_HEAT)

    assert algo.committed_on_percent == pytest.approx(0.13)
    assert algo._causal_power_trace._active_ownership is None
    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.NONE


@pytest.mark.asyncio
async def test_valve_receipts_bind_immediately_and_on_auto_repeat() -> None:
    """A valve cycle start and its automatic restart keep the request snapshot."""
    algo = _new_valve_algo("valve-repeat")
    _configure_valve_ownership_context(algo, 0.132)
    request_id = algo.stage_command(VThermHvacMode_HEAT)

    await algo.on_command_receipt(make_receipt(request_id, 0.13, requested=0.132))
    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.BOUND

    await algo.on_command_receipt(
        make_receipt(request_id, 0.13, requested=0.132, is_repeat=True)
    )
    assert algo._command_ownership.last_binding.status == CommandOwnershipBindingStatus.REUSED
    assert algo._causal_power_trace._active_ownership is not None


def test_handler_syncs_scheduler_delays(fake_handler_runtime) -> None:
    """The scheduler applies the SmartPI minimal delays."""
    thermostat = fake_handler_runtime
    thermostat.entry_infos.update(
        {
            "minimal_activation_delay": 20,
            "minimal_deactivation_delay": 30,
        }
    )
    thermostat.cycle_scheduler.min_activation_delay = 1
    thermostat.cycle_scheduler.min_deactivation_delay = 2
    handler = SmartPIHandler(thermostat)

    handler.init_algorithm()

    assert thermostat.cycle_scheduler.min_activation_delay == 20
    assert thermostat.cycle_scheduler.min_deactivation_delay == 30


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


@pytest.mark.parametrize("linearized", [False, True])
def test_valve_mode_ignores_min_activation_delay(linearized: bool) -> None:
    """Valve commands are neither raised nor cut by the switch ON-time delay."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=60,
        minimal_deactivation_delay=60,
        name="valve-min-on",
        enable_valve_linearization=linearized,
    )
    algo.configure_valve_linearization(linearized, valve_mode=True)

    assert algo.update_timing_constraints(0.0, 0.05) == pytest.approx(0.05)
    assert algo.forced_by_timing is False
    assert algo.update_timing_constraints(0.05, 0.99) == pytest.approx(0.99)
    assert algo.forced_by_timing is False


@pytest.mark.asyncio
async def test_missing_valve_position_keeps_its_cycle_out_of_the_stationary_trim() -> None:
    """A cycle with a missing valve reading cannot provide the realized power of a trim window."""
    from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
    from custom_components.vtherm_smartpi.smartpi.ff_trim import (
        CausalFFTrimObserver,
        FFTrimThermalSample,
    )

    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=30.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="valve-unobserved",
    )
    algo.configure_valve_linearization(False, valve_mode=True)
    clock = {"t": 0.0}
    with patch("time.monotonic", side_effect=lambda: clock["t"]):
        await algo.on_cycle_started(0.0, 0.0, 0.5, "heat", physical_on_percent=0.5)
        clock["t"] = 600.0
        algo.on_applied_power_updated(None, VThermHvacMode_HEAT)
        clock["t"] = 1800.0
        await algo.on_cycle_completed(e_eff=0.5, elapsed_ratio=1.0)

    window = algo._causal_power_trace.read_window(0.0, 1800.0)
    assert window.power_coverage_ratio == pytest.approx(1.0)
    assert [(d.start_monotonic, d.end_monotonic, d.reason) for d in window.discontinuities] == [
        (600.0, 1800.0, "missing_valve_position")
    ]

    observer = CausalFFTrimObserver(5.0, algo._causal_power_trace)
    for timestamp, temperature in ((120.0, 20.0), (720.0, 20.1), (1320.0, 19.9), (1920.0, 20.0)):
        observer.record_thermal_sample(
            FFTrimThermalSample(
                observed_monotonic=timestamp,
                measurement_id=str(timestamp),
                temperature=temperature,
                target=20.0,
                ff1=0.5,
                regime=GovernanceRegime.DEAD_BAND,
                i_mode="I:FREEZE(deadband)",
                u_pi=0.1,
                hvac_mode="heat",
                saturated=False,
                trajectory_active=False,
                ff3_active=False,
                setpoint_changed=False,
                outside_temperature_available=True,
                model_reliable=True,
                outside_temperature=10.0,
            ),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )
    result = observer.try_complete_window(
        a=0.1, b=0.005, deadtime_s=120.0, deadtime_reliable=True, current_trim=0.0
    )
    assert result is not None
    assert result.admissible is False
    assert result.reason == "causal_power_not_covered"
