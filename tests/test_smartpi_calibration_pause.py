"""A short stop of the thermostat suspends a calibration instead of cancelling it."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
)
from custom_components.vtherm_smartpi.smartpi.calibration import CalibrationManager
from custom_components.vtherm_smartpi.smartpi.const import (
    CALIBRATION_PAUSE_MAX_MIN,
    CALIBRATION_TIMEOUT_MIN,
    SmartPICalibrationPhase,
    SmartPICalibrationResult,
)
from test_smartpi_calibration import create_smartpi
from test_smartpi_handler_safety import _make_handler

COOL_DOWN = SmartPICalibrationPhase.COOL_DOWN
HEAT_UP = SmartPICalibrationPhase.HEAT_UP
COOL_DOWN_FINAL = SmartPICalibrationPhase.COOL_DOWN_FINAL
MIN = 60.0
START = 1_800_000_000.0

# Output of each phase in HEAT mode, with a temperature that keeps the phase
PHASE_OUTPUT = {COOL_DOWN: 0.0, HEAT_UP: 1.0, COOL_DOWN_FINAL: 0.0}
PHASE_TEMP = {COOL_DOWN: 20.2, HEAT_UP: 20.2, COOL_DOWN_FINAL: 20.2}


class Clock:
    """Wall and monotonic clocks moving together."""

    def __init__(self):
        self.now = START

    def advance(self, minutes: float) -> None:
        self.now += minutes * MIN

    def __enter__(self):
        self._patches = [
            patch("time.time", side_effect=lambda: self.now),
            patch("time.monotonic", side_effect=lambda: self.now - START + 1000.0),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


@pytest.fixture
def clock():
    with Clock() as c:
        yield c


def _manager(phase, mode=VThermHvacMode_HEAT):
    mgr = CalibrationManager("t")
    mgr.start_calibration(1.0)
    mgr._calibration_state = phase
    # One running tick records the mode
    _calc(mgr, mode)
    return mgr


def _calc(mgr, mode, dt_est=None):
    return mgr.calculate(
        target_temp=20.0,
        current_temp=20.2,
        hvac_mode=mode,
        dt_est=dt_est or MagicMock(),
        max_on_percent=1.0,
        ext_temp=None,
    )


@pytest.mark.parametrize("phase", [COOL_DOWN, HEAT_UP, COOL_DOWN_FINAL])
def test_short_stop_then_same_mode_continues_in_the_phase(clock, phase):
    mgr = _manager(phase)
    dt_est = MagicMock()

    stopped = _calc(mgr, VThermHvacMode_OFF, dt_est)
    assert stopped.on_percent == 0.0
    assert stopped.message == "paused"
    assert mgr.is_calibrating and mgr.is_suspended
    assert mgr.state == phase
    dt_est.update.assert_not_called()

    clock.advance(CALIBRATION_PAUSE_MAX_MIN - 1)
    still = _calc(mgr, VThermHvacMode_OFF, dt_est)
    assert still.message == "paused" and mgr.is_suspended

    clock.advance(1)
    back = _calc(mgr, VThermHvacMode_HEAT, dt_est)
    assert not mgr.is_suspended
    assert mgr.state == phase
    assert back.on_percent == PHASE_OUTPUT[phase]
    dt_est.reset_observation.assert_called_once_with(0.0)
    assert mgr.take_completed_result() is None


def test_cool_calibration_resumes_in_cool(clock):
    mgr = _manager(HEAT_UP, VThermHvacMode_COOL)
    _calc(mgr, VThermHvacMode_OFF)
    clock.advance(3)
    back = _calc(mgr, VThermHvacMode_COOL)
    assert mgr.state == HEAT_UP and back.on_percent == 0.0
    assert mgr.take_completed_result() is None


@pytest.mark.parametrize("phase", [COOL_DOWN, HEAT_UP, COOL_DOWN_FINAL])
def test_stop_longer_than_the_limit_cancels_when_the_mode_comes_back(clock, phase):
    mgr = _manager(phase)
    _calc(mgr, VThermHvacMode_OFF)
    clock.advance(CALIBRATION_PAUSE_MAX_MIN + 1)

    result = _calc(mgr, VThermHvacMode_HEAT)

    assert result.on_percent is None
    assert not mgr.is_calibrating and not mgr.is_suspended
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_stop_longer_than_the_limit_cancels_while_still_stopped(clock):
    mgr = _manager(HEAT_UP)
    _calc(mgr, VThermHvacMode_OFF)
    clock.advance(CALIBRATION_PAUSE_MAX_MIN + 1)

    _calc(mgr, VThermHvacMode_OFF)

    assert not mgr.is_calibrating
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_another_mode_cancels(clock):
    mgr = _manager(HEAT_UP, VThermHvacMode_HEAT)
    _calc(mgr, VThermHvacMode_OFF)
    clock.advance(2)

    _calc(mgr, VThermHvacMode_COOL)

    assert not mgr.is_calibrating
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_suspend_keeps_the_first_stop_time_and_ignores_an_idle_manager(clock):
    idle = CalibrationManager("t")
    idle.suspend()
    assert not idle.is_suspended

    mgr = _manager(HEAT_UP)
    mgr.suspend()
    clock.advance(4)
    mgr.suspend()
    clock.advance(5)
    _calc(mgr, VThermHvacMode_HEAT)
    assert not mgr.is_suspended and mgr.is_calibrating


def test_reset_cancels_a_suspended_calibration_at_once(clock):
    mgr = _manager(HEAT_UP)
    _calc(mgr, VThermHvacMode_OFF)

    mgr.reset()

    assert not mgr.is_calibrating and not mgr.is_suspended
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED
    assert mgr.save_state()["calibration_pause_start"] is None


def test_suspension_survives_save_and_load(clock):
    mgr = _manager(COOL_DOWN_FINAL)
    _calc(mgr, VThermHvacMode_OFF)
    clock.advance(4)
    saved = mgr.save_state()
    assert saved["calibration_pause_start"] == pytest.approx(START)
    assert saved["calibration_pause_mode"] == "heat"

    loaded = CalibrationManager("t")
    loaded.load_state(saved)
    assert loaded.is_suspended and loaded.state == COOL_DOWN_FINAL
    clock.advance(3)
    _calc(loaded, VThermHvacMode_HEAT)
    assert not loaded.is_suspended and loaded.state == COOL_DOWN_FINAL
    assert loaded.take_completed_result() is None

    late = CalibrationManager("t")
    late.load_state(saved)
    clock.advance(CALIBRATION_PAUSE_MAX_MIN)
    _calc(late, VThermHvacMode_HEAT)
    assert not late.is_calibrating
    assert late.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_state_without_a_suspension_loads_as_running():
    mgr = CalibrationManager("t")
    mgr.load_state({"calibration_state": HEAT_UP.value})
    assert mgr.is_calibrating and not mgr.is_suspended
    idle = CalibrationManager("t")
    idle.load_state({"calibration_state": "Idle", "calibration_pause_start": START, "calibration_pause_mode": "heat"})
    assert not idle.is_suspended and idle.save_state()["calibration_pause_mode"] is None


def _started_algo(clock):
    algo = create_smartpi()
    algo.force_calibration()
    algo.calculate(20.0, current_temp=20.0, ext_current_temp=10.0, slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.calibration_state == COOL_DOWN
    return algo


def _tick(algo, mode):
    algo.calculate(20.0, current_temp=20.0, ext_current_temp=10.0, slope=0, hvac_mode=mode)


def test_algo_suspends_on_off_and_resumes_in_the_phase(clock):
    algo = _started_algo(clock)

    _tick(algo, VThermHvacMode_OFF)
    assert algo.calibration_mgr.is_suspended
    assert algo.on_percent == 0.0

    clock.advance(5)
    _tick(algo, VThermHvacMode_HEAT)
    assert algo.calibration_state == COOL_DOWN
    assert not algo.calibration_mgr.is_suspended
    assert algo.calibration_mgr.take_completed_result() is None


def test_algo_cancels_after_a_long_stop_and_reports_it(clock):
    algo = _started_algo(clock)
    _tick(algo, VThermHvacMode_OFF)
    clock.advance(CALIBRATION_PAUSE_MAX_MIN + 1)

    _tick(algo, VThermHvacMode_HEAT)

    assert algo.calibration_state == SmartPICalibrationPhase.IDLE
    assert algo.calibration_mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_timeout_reached_during_a_suspension_ends_the_run_at_the_resume(clock):
    algo = _started_algo(clock)
    # The run started just under the timeout ago
    algo.calibration_mgr.calibration_start_time = (
        __import__("time").monotonic() - (CALIBRATION_TIMEOUT_MIN - 3) * MIN
    )
    _tick(algo, VThermHvacMode_OFF)
    clock.advance(5)

    _tick(algo, VThermHvacMode_HEAT)

    assert algo.calibration_state == SmartPICalibrationPhase.IDLE
    assert algo.calibration_mgr.calibration_result == SmartPICalibrationResult.CANCELLED
    assert algo.calibration_mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED


def test_timeout_keeps_counting_from_the_original_start(clock):
    algo = _started_algo(clock)
    algo.calibration_mgr.calibration_start_time = (
        __import__("time").monotonic() - (CALIBRATION_TIMEOUT_MIN - 30) * MIN
    )
    _tick(algo, VThermHvacMode_OFF)
    clock.advance(5)
    _tick(algo, VThermHvacMode_HEAT)
    assert algo.calibration_state == COOL_DOWN

    clock.advance(26)
    _tick(algo, VThermHvacMode_HEAT)
    assert algo.calibration_state == SmartPICalibrationPhase.IDLE


@pytest.mark.asyncio
async def test_handler_suspends_the_calibration_when_the_thermostat_stops(fake_handler_runtime):
    thermostat = fake_handler_runtime
    handler, algo = _make_handler(thermostat)
    algo.calibration_mgr.start_calibration(1.0)
    algo.calibration_mgr._calibration_state = HEAT_UP
    algo.calibration_mgr._run_mode = "heat"
    thermostat.vtherm_hvac_mode = VThermHvacMode_OFF

    await handler.on_state_changed(True)

    assert algo.calibration_mgr.is_calibrating
    assert algo.calibration_mgr.is_suspended
    assert algo.calibration_mgr.take_completed_result() is None
