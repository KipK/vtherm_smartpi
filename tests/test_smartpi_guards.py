"""Test SmartPI Guard Kick and Guard Cut Logic."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, PropertyMock
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.algo import SmartPI, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.const import SmartPICalibrationPhase
from custom_components.vtherm_smartpi.smartpi.guards import GuardAction


def _make_thermostat():
    """Build a minimal thermostat mock for guard tests."""
    thermostat = MagicMock()
    thermostat.hass = MagicMock()
    thermostat.name = "TestThermostat"
    thermostat.cycle_min = 10
    thermostat.minimal_activation_delay = 0
    thermostat.minimal_deactivation_delay = 0
    thermostat.vtherm_hvac_mode = VThermHvacMode_HEAT
    thermostat.target_temperature = 20.0
    thermostat.underlyings = []
    thermostat.cycle_scheduler = MagicMock()
    thermostat.cycle_scheduler.start_cycle = AsyncMock()
    return thermostat


def _make_algo(guard_cut=GuardAction.NONE, guard_kick=GuardAction.NONE, on_percent=0.5):
    """Build a minimal SmartPI mock for guard tests."""
    algo = MagicMock(spec=SmartPI)
    algo.guards = MagicMock()
    algo.autocalib = MagicMock()
    algo.calibration_mgr = MagicMock()
    algo.deadband_mgr = MagicMock()
    algo.deadband_mgr.near_band_changed = False
    algo.guards.check_guard_cut.return_value = guard_cut
    algo.guards.check_guard_kick.return_value = guard_kick
    algo.on_percent = on_percent
    type(algo).calibration_state = PropertyMock(return_value=SmartPICalibrationPhase.IDLE)
    # phase must not equal CALIBRATION or HYSTERESIS so guard-forced value is not masked
    type(algo).phase = PropertyMock(return_value=SmartPIPhase.STABLE)
    return algo


@pytest.mark.asyncio
async def test_smartpi_guard_kick_trigger():
    """Test that Guard Kick triggers correctly (Force Cycle)."""
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    algo = _make_algo(guard_kick=GuardAction.KICK_TRIGGER)
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    # start_cycle must have been called with force=True
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is True


@pytest.mark.asyncio
async def test_smartpi_guard_kick_reset():
    """Test that Guard Kick resets (informational, no force)."""
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    algo = _make_algo(guard_kick=GuardAction.KICK_RESET)
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    # start_cycle must have been called with force=False
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_guard_kick_antiloop():
    """Test that Guard Kick respects anti-loop (MAINTAIN, no force)."""
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    algo = _make_algo(guard_kick=GuardAction.KICK_MAINTAIN)
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    # start_cycle must have been called with force=False
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_guard_cut_trigger():
    """Test that Guard Cut triggers (Force Cycle with 0%)."""
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    handler._async_save = AsyncMock()
    algo = _make_algo(guard_cut=GuardAction.CUT_TRIGGER, on_percent=0.0)
    algo.calculate = MagicMock()
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    # Guard Cut must have been checked
    algo.guards.check_guard_cut.assert_called_once()
    # Calculate must have been called
    algo.calculate.assert_called_once()
    # start_cycle must have been called with force=True
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is True


@pytest.mark.asyncio
async def test_smartpi_guard_cut_maintain():
    """Test that Guard Cut maintains OFF state (no force)."""
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    handler._async_save = AsyncMock()
    algo = _make_algo(guard_cut=GuardAction.CUT_MAINTAIN, on_percent=0.0)
    algo.calculate = MagicMock()
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    # Guard Cut must have been checked
    algo.guards.check_guard_cut.assert_called_once()
    # Calculate must have been called
    algo.calculate.assert_called_once()
    # start_cycle must have been called with force=False
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_deadband_entry_with_ff3_does_not_force_cycle():
    """Entering deadband with an active FF3 must not restart the cycle on its own."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 19.95
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.in_deadband = False
    algo.in_near_band = True
    algo.deadband_c = 0.10
    algo.u_ff3 = 0.05
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    algo.calculate.assert_called_once()
    assert algo.calculate.call_args.kwargs["cycle_boundary"] is False
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_deadband_entry_without_ff3_does_not_force_cycle():
    """Deadband entry without FF3 must not restart the cycle on its own."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 19.95
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.in_deadband = False
    algo.in_near_band = True
    algo.deadband_c = 0.10
    algo.u_ff3 = 0.0
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    algo.calculate.assert_called_once()
    assert algo.calculate.call_args.kwargs["cycle_boundary"] is False
    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False
