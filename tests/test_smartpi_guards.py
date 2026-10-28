"""Test SmartPI Guard Kick and Guard Cut Logic."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, PropertyMock
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.algo import SmartPI, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.const import SmartPICalibrationPhase
from custom_components.vtherm_smartpi.smartpi.guards import GuardAction, SmartPIGuards


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
    type(thermostat).on_percent = PropertyMock(
        side_effect=lambda: thermostat.prop_algorithm.on_percent
    )
    return thermostat


def _make_algo(guard_cut=GuardAction.NONE, guard_kick=GuardAction.NONE, on_percent=0.5):
    """Build a minimal SmartPI mock for guard tests."""
    algo = MagicMock(spec=SmartPI)
    algo.guards = MagicMock()
    algo.autocalib = MagicMock()
    algo.calibration_mgr = MagicMock()
    algo.deadband_mgr = MagicMock()
    algo.deadband_mgr.deadband_changed = False
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


@pytest.mark.asyncio
async def test_smartpi_deadband_transition_does_not_force_cycle_by_default():
    """Deadband transitions must not restart PWM cycles unless enabled."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 20.0
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.deadband_mgr.deadband_changed = True
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_deadband_transition_can_force_cycle_when_enabled():
    """Deadband transitions may restart PWM cycles when the option is enabled."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 20.0
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    handler._allow_pwm_cycle_force = True
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.deadband_mgr.deadband_changed = True
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is True


@pytest.mark.asyncio
async def test_smartpi_near_band_transition_does_not_force_cycle_by_default():
    """Near-band transitions must not restart PWM cycles unless enabled."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 20.0
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.deadband_mgr.near_band_changed = True
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is False


@pytest.mark.asyncio
async def test_smartpi_near_band_transition_can_force_cycle_when_enabled():
    """Near-band transitions may restart PWM cycles when the option is enabled."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 20.0
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.cycle_scheduler.is_cycle_running = True

    handler = SmartPIHandler(thermostat)
    handler._allow_pwm_cycle_force = True
    algo = _make_algo(on_percent=0.15)
    algo.calculate = MagicMock()
    algo.deadband_mgr.near_band_changed = True
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    thermostat.cycle_scheduler.start_cycle.assert_called_once()
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is True


def _cut(guards, current_temp, device_active=True, hvac_mode=VThermHvacMode_HEAT):
    return guards.check_guard_cut(
        current_temp=current_temp,
        target_temp=20.0,
        near_band_above=0.2,
        is_device_active=device_active,
        hvac_mode=hvac_mode,
        is_calibration=False,
    )


def test_guard_cut_threshold_does_not_depend_on_near_band_state():
    """The cut triggers above SP + near_band_above while the device is active, holds, then resets."""
    guards = SmartPIGuards()

    assert _cut(guards, 20.2) == GuardAction.NONE
    assert _cut(guards, 20.25, device_active=False) == GuardAction.NONE
    assert _cut(guards, 20.25) == GuardAction.CUT_TRIGGER
    assert guards.guard_cut_active is True
    assert guards.guard_cut_count == 1
    assert _cut(guards, 20.16) == GuardAction.CUT_MAINTAIN
    assert _cut(guards, 20.15) == GuardAction.CUT_RESET
    assert guards.guard_cut_active is False


def test_guard_cut_is_inactive_in_cool():
    """COOL keeps no guard cut."""
    guards = SmartPIGuards()

    assert _cut(guards, 19.0, hvac_mode=VThermHvacMode_COOL) == GuardAction.NONE
    assert _cut(guards, 21.0, hvac_mode=VThermHvacMode_COOL) == GuardAction.NONE
    assert guards.guard_cut_active is False


@pytest.mark.asyncio
async def test_handler_guard_cut_triggers_outside_filtered_near_band():
    """A temperature above SP + near_band_above with the heater on forces the cut even when the
    filtered near-band flag is False (slow overshoot)."""
    thermostat = _make_thermostat()
    thermostat.current_temperature = 20.3
    thermostat.current_outdoor_temperature = 10.0
    thermostat.last_temperature_slope = 0.0
    thermostat.power_manager = None
    thermostat.is_device_active = True
    thermostat.cycle_scheduler.is_cycle_running = False

    handler = SmartPIHandler(thermostat)
    algo = _make_algo(on_percent=0.0)
    algo.guards = SmartPIGuards()
    algo.in_near_band = False
    algo.deadband_mgr.near_band_above_deg = 0.2
    algo.deadband_mgr.near_band_below_deg = 0.4
    algo.calculated_on_percent = 0.0
    algo.calculate = MagicMock()
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    assert algo.guards.guard_cut_active is True
    assert algo._last_restart_reason == "guard_cut"
    _, _, force = thermostat.cycle_scheduler.start_cycle.call_args[0]
    assert force is True


def test_guard_cut_does_not_act_in_the_bootstrap():
    """The bootstrap heats up to its own hysteresis threshold, beyond SP + near_band_above."""
    guards = SmartPIGuards()
    action = guards.check_guard_cut(
        current_temp=20.4, target_temp=20.0, near_band_above=0.2, is_device_active=True,
        hvac_mode=VThermHvacMode_HEAT, is_calibration=False, is_bootstrap=True,
    )
    assert action == GuardAction.NONE
    assert guards.guard_cut_active is False


def test_a_running_guard_cut_is_released_in_the_bootstrap():
    guards = SmartPIGuards()
    assert _cut(guards, 20.25) == GuardAction.CUT_TRIGGER
    action = guards.check_guard_cut(
        current_temp=20.4, target_temp=20.0, near_band_above=0.2, is_device_active=True,
        hvac_mode=VThermHvacMode_HEAT, is_calibration=False, is_bootstrap=True,
    )
    assert action == GuardAction.CUT_RESET
    assert guards.guard_cut_active is False


def test_guard_kick_does_not_act_in_the_bootstrap():
    guards = SmartPIGuards()
    action = guards.check_guard_kick(
        current_temp=19.5, target_temp=20.0, near_band_below=0.3, in_near_band=True, on_percent=0.0,
        hvac_mode=VThermHvacMode_HEAT, is_calibration=False, is_bootstrap=True,
    )
    assert action == GuardAction.NONE
    assert guards.guard_kick_active is False


@pytest.mark.asyncio
async def test_handler_tells_the_guards_about_the_bootstrap():
    thermostat = _make_thermostat()
    handler = SmartPIHandler(thermostat)
    handler._async_save = AsyncMock()
    algo = _make_algo()
    algo.calculate = MagicMock()
    type(algo).phase = PropertyMock(return_value=SmartPIPhase.HYSTERESIS)
    thermostat.prop_algorithm = algo

    await handler.control_heating(timestamp=datetime.now())

    assert algo.guards.check_guard_cut.call_args.kwargs["is_bootstrap"] is True
    assert algo.guards.check_guard_kick.call_args.kwargs["is_bootstrap"] is True


def test_guard_cut_resets_one_and_a_half_sensor_steps_below_its_threshold():
    """A reading flickering between two sensor levels cannot re-trigger the cut."""
    guards = SmartPIGuards()
    guards.sensor_step_c = 0.1
    assert _cut(guards, 20.3) == GuardAction.CUT_TRIGGER
    # One step below the reading, within the former 0.05 C reset: still cut.
    assert _cut(guards, 20.1) == GuardAction.CUT_MAINTAIN
    assert _cut(guards, 20.06) == GuardAction.CUT_MAINTAIN
    assert _cut(guards, 20.05) == GuardAction.CUT_RESET


def test_guard_kick_reset_does_not_depend_on_the_sensor_step():
    guards = SmartPIGuards()
    guards.sensor_step_c = 0.1
    kick = dict(target_temp=20.0, near_band_below=0.3, in_near_band=True, on_percent=0.5,
                hvac_mode=VThermHvacMode_HEAT, is_calibration=False)
    assert guards.check_guard_kick(current_temp=19.6, **kick) == GuardAction.KICK_TRIGGER
    assert guards.check_guard_kick(current_temp=19.75, **kick) == GuardAction.KICK_RESET
