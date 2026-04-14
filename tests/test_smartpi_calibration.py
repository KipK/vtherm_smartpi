"""Tests for Smart-PI Forced Calibration and AutoCalibTrigger."""

from datetime import datetime
import pytest
from unittest.mock import MagicMock, patch
import time

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import (
    SmartPIPhase,
    SmartPICalibrationPhase,
    AutoCalibState,
    AutoCalibWaitingReason,
    HYST_LOWER_C,
    HYST_UPPER_C,
    AUTOCALIB_SNAPSHOT_PERIOD_H,
    AUTOCALIB_COOLDOWN_H,
    AUTOCALIB_MAX_RETRIES,
)
from custom_components.vtherm_smartpi.smartpi.autocalib import AutoCalibTrigger, AutoCalibEvent
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode, VThermHvacMode_HEAT


class MockHass:
    def __init__(self):
        self.config = MagicMock()
        self.bus = MagicMock()


def create_smartpi():
    hass = MockHass()
    algo = SmartPI(
        hass=hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="test_vtherm",
        max_on_percent=1.0,
        deadband_c=0.1,
        use_setpoint_filter=True,
        debug_mode=True,
    )
    # Bypass initial learning by filling history
    algo.est.a_meas_hist = [1.0] * 35
    algo.est.b_meas_hist = [1.0] * 35
    return algo


def test_manual_trigger():
    """Manual force_calibration should start calibration on next calculate()."""
    algo = create_smartpi()
    assert algo.calibration_state == SmartPICalibrationPhase.IDLE

    # Trigger
    algo.force_calibration()
    assert algo.calibration_mgr.calibration_requested is True

    # Run calculate to start state machine
    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,  # At setpoint
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )

    assert algo.phase == SmartPIPhase.CALIBRATION
    assert algo.calibration_state == SmartPICalibrationPhase.COOL_DOWN
    assert algo.on_percent == 0.0
    assert algo.calibration_mgr.calibration_requested is False


def test_manual_trigger_blocked_during_hysteresis():
    """Manual force_calibration should be blocked during HYSTERESIS phase."""
    algo = create_smartpi()
    assert algo.calibration_state == SmartPICalibrationPhase.IDLE

    # Force phase to HYSTERESIS
    algo.est.a_meas_hist = []  # Clear history to stay in hysteresis
    algo.est.b_meas_hist = []

    # Request calibration while in hysteresis – should be silently rejected
    algo.calibration_mgr.request_calibration(phase=SmartPIPhase.HYSTERESIS)
    assert algo.calibration_mgr.calibration_requested is False


def test_calibration_cycle_flow():
    """Verify complete calibration cycle state machine transitions."""
    algo = create_smartpi()
    algo.force_calibration()

    target = 20.0

    # 1. Start -> COOL_DOWN
    algo.calculate(target, current_temp=20.0, ext_current_temp=10.0, slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.calibration_state == SmartPICalibrationPhase.COOL_DOWN
    assert algo.on_percent == 0.0

    # 2. Reach Low Threshold -> HEAT_UP
    low_thresh = target - HYST_LOWER_C - 0.1
    high_thresh = target + HYST_UPPER_C + 0.1

    algo.calculate(target, current_temp=low_thresh, ext_current_temp=10.0, slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.calibration_state == SmartPICalibrationPhase.HEAT_UP
    assert algo.on_percent == 1.0  # Updated immediately for responsiveness

    # 3. Reach High Threshold -> COOL_DOWN_FINAL
    algo.calculate(target, current_temp=high_thresh, ext_current_temp=10.0, slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.calibration_state == SmartPICalibrationPhase.COOL_DOWN_FINAL
    assert algo.on_percent == 0.0  # Updated immediately

    # 4. Reach Low Threshold again -> IDLE
    algo.calculate(target, current_temp=low_thresh, ext_current_temp=10.0, slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.calibration_state == SmartPICalibrationPhase.IDLE
    assert algo.calibration_mgr.last_calibration_time is not None


def test_calibration_heat_deadtime_survives_duplicate_calculate_before_cycle_commit():
    """Calibration must not lose the armed heat deadtime on duplicate calculate calls."""
    algo = create_smartpi()
    target = 18.0
    low_thresh = target - HYST_LOWER_C - 0.01

    # Reproduce the runtime context seen before the manual calibration:
    # a running cycle is still committed when calibration is requested.
    algo._committed_on_percent = 0.21
    algo.force_calibration()

    # First call starts the forced calibration and records the stop time when
    # entering COOL_DOWN from the previously committed active cycle.
    with patch(
        "custom_components.vtherm_smartpi.algo.time.monotonic",
        return_value=1000.0,
    ), patch(
        "custom_components.vtherm_smartpi.smartpi.calibration.time.monotonic",
        return_value=1000.0,
    ):
        algo.calculate(
            target_temp=target,
            current_temp=17.84,
            ext_current_temp=10.0,
            slope=0.0,
            hvac_mode=VThermHvacMode_HEAT,
        )

    assert algo.calibration_state == SmartPICalibrationPhase.COOL_DOWN

    # The running cycle is now effectively off, but the scheduler has not yet
    # committed the next HEAT_UP cycle when calculate() is called again.
    algo._committed_on_percent = 0.0

    # After a sufficiently long COOL_DOWN, the HEAT_UP transition must arm the
    # heat deadtime detection.
    with patch(
        "custom_components.vtherm_smartpi.algo.time.monotonic",
        return_value=1701.0,
    ), patch(
        "custom_components.vtherm_smartpi.smartpi.calibration.time.monotonic",
        return_value=1701.0,
    ):
        algo.calculate(
            target_temp=target,
            current_temp=low_thresh,
            ext_current_temp=10.0,
            slope=0.0,
            hvac_mode=VThermHvacMode_HEAT,
        )

    assert algo.calibration_state == SmartPICalibrationPhase.HEAT_UP
    assert algo.dt_est.state == "WAITING_HEAT_RESPONSE"

    # A second immediate calculate() must keep the heat deadtime armed instead
    # of synthesizing a false cool start from the stale committed power.
    with patch(
        "custom_components.vtherm_smartpi.algo.time.monotonic",
        return_value=1701.1,
    ), patch(
        "custom_components.vtherm_smartpi.smartpi.calibration.time.monotonic",
        return_value=1701.1,
    ):
        algo.calculate(
            target_temp=target,
            current_temp=low_thresh,
            ext_current_temp=10.0,
            slope=0.0,
            hvac_mode=VThermHvacMode_HEAT,
        )

    assert algo.calibration_state == SmartPICalibrationPhase.HEAT_UP
    assert algo.dt_est.state == "WAITING_HEAT_RESPONSE"
    assert algo.dt_est.cool_start_time is None


def test_attribute_exposure():
    """Verify that SmartPI published and debug diagnostics are correctly exposed."""
    algo = create_smartpi()
    now_ts = time.time()
    algo.calibration_mgr.last_calibration_time = now_ts
    algo.calibration_mgr._calibration_state = SmartPICalibrationPhase.IDLE
    algo.calibration_mgr._calibration_retry_count = 2

    attrs = algo.get_debug_diagnostics()
    assert attrs["calibration"]["state"] == SmartPICalibrationPhase.IDLE
    assert attrs["calibration"]["retry_count"] == 2
    assert attrs["calibration"]["last_time"] == datetime.fromtimestamp(now_ts).isoformat()
    assert attrs["autocalib"]["state"] == AutoCalibState.WAITING_SNAPSHOT.value
    assert attrs["autocalib"]["model_degraded"] is False
    assert "debug" in attrs
    assert attrs["debug"]["calibration_state"] == SmartPICalibrationPhase.IDLE
    assert attrs["debug"]["calibration_retry_count"] == 2
    assert attrs["debug"]["last_calibration_time"] == datetime.fromtimestamp(now_ts).isoformat()
    assert "autocalib_state" in attrs["debug"]
    assert "autocalib_model_degraded" in attrs["debug"]


# =============================================================================
# AutoCalibTrigger Tests
# =============================================================================


def test_autocalib_initial_state():
    """AutoCalibTrigger should start in WAITING_SNAPSHOT state."""
    ac = AutoCalibTrigger("test_vtherm")
    assert ac.state == AutoCalibState.WAITING_SNAPSHOT
    assert ac.waiting_reason == AutoCalibWaitingReason.DEADTIME_COOL_PENDING
    assert ac.model_degraded is False
    assert ac.retry_count == 0
    assert ac.snapshot_age_h is None


def test_autocalib_rate_limit():
    """check_hourly should be rate-limited to once per hour."""
    ac = AutoCalibTrigger("test_vtherm")
    mock_algo = MagicMock()

    now = time.time()
    # First call executes
    result1 = ac.check_hourly(now, mock_algo)
    # Second call within same hour is skipped
    result2 = ac.check_hourly(now + 1800, mock_algo)  # 30 min later
    assert result2 is None  # Rate-limited

    # Call after 1 hour executes
    result3 = ac.check_hourly(now + 3700, mock_algo)  # > 1 hour later
    # result3 may be None or an event depending on state, but it should execute


def test_autocalib_waiting_snapshot_to_monitoring():
    """Transition from WAITING_SNAPSHOT to MONITORING when conditions met."""
    ac = AutoCalibTrigger("test_vtherm")
    mock_algo = MagicMock()

    # Set up conditions for snapshot: all reliable
    mock_algo.est.tau_reliability.return_value = MagicMock(reliable=True)
    mock_algo.dt_est.deadtime_heat_reliable = True
    mock_algo.dt_est.deadtime_cool_reliable = True
    mock_algo.est.learn_ok_count_a = 10
    mock_algo.est.learn_ok_count_b = 10
    mock_algo.est.a = 0.5
    mock_algo.est.b = 0.02

    now = time.time()
    # First check: should take snapshot and transition to MONITORING
    event = ac.check_hourly(now, mock_algo)

    assert ac.state == AutoCalibState.MONITORING
    assert event is not None
    assert event.event_type == "smartpi_autocalib_snapshot_taken"
    assert ac.snapshot_age_h is not None


def test_autocalib_stagnation_trigger():
    """AutoCalibTrigger should detect stagnation and trigger calibration."""
    ac = AutoCalibTrigger("test_vtherm")

    # Set state to MONITORING with snapshot just under 120h to avoid rolling
    # Then test _evaluate_stagnation directly
    ac._state = AutoCalibState.MONITORING
    ac._snapshot_ts = time.time() - (AUTOCALIB_SNAPSHOT_PERIOD_H - 1) * 3600  # 119h
    ac._snap_ok_count_a = 10
    ac._snap_ok_count_b = 10

    mock_algo = MagicMock()
    mock_algo.phase = SmartPIPhase.STABLE
    mock_algo.calibration_mgr.is_calibrating = False
    mock_algo.calibration_mgr.last_calibration_time = None
    mock_algo.gov.regime = MagicMock()
    mock_algo.gov.regime.value = "stable"

    # Stagnating conditions: no progress, high dispersion
    mock_algo.est.learn_ok_count_a = 11  # Only 1 new observation
    mock_algo.est.learn_ok_count_b = 11
    mock_algo.est.diag_a_mad_over_med = 0.30  # Above threshold
    mock_algo.est.diag_b_mad_over_med = 0.35
    mock_algo.est.tau_reliability.return_value = MagicMock(reliable=False)
    mock_algo.dt_est.deadtime_heat_reliable = True
    mock_algo.dt_est.deadtime_cool_reliable = True
    mock_algo.est.a = 0.5
    mock_algo.est.b = 0.02

    # Test stagnation evaluation directly
    stagnating = ac._evaluate_stagnation(mock_algo, ext_temp=5.0, current_temp=20.0)
    assert "a" in stagnating or "b" in stagnating

    # Now test the full trigger path via _do_trigger
    now = time.time()
    event = ac._do_trigger(now, mock_algo, stagnating)
    assert event.should_trigger_calibration is True
    assert ac.state == AutoCalibState.TRIGGERED


def test_autocalib_guard_cooldown():
    """AutoCalibTrigger should respect cooldown period."""
    ac = AutoCalibTrigger("test_vtherm")
    ac._state = AutoCalibState.MONITORING
    ac._snapshot_ts = time.time() - (AUTOCALIB_SNAPSHOT_PERIOD_H - 1) * 3600  # 119h
    ac._last_trigger_ts = time.time() - 12 * 3600  # 12h ago (< 24h cooldown)

    mock_algo = MagicMock()
    mock_algo.phase = SmartPIPhase.STABLE
    mock_algo.calibration_mgr.is_calibrating = False
    mock_algo.gov.regime = MagicMock()
    mock_algo.gov.regime.value = "stable"

    now = time.time()
    guard_ok, guard_reason = ac._check_guards(now, mock_algo)

    # Should be blocked by cooldown
    assert guard_ok is False
    assert "cooldown" in guard_reason


def test_autocalib_post_calib_success():
    """on_calibration_complete should evaluate exit criteria."""
    ac = AutoCalibTrigger("test_vtherm")
    ac._state = AutoCalibState.TRIGGERED
    ac._post_calib_snap_a = 10
    ac._post_calib_snap_b = 10
    ac._triggered_params = ["a", "b"]

    mock_algo = MagicMock()
    # Successful calibration: new observations gained
    mock_algo.est.learn_ok_count_a = 12  # +2 new
    mock_algo.est.learn_ok_count_b = 12
    mock_algo.dt_est.deadtime_heat_reliable = True
    mock_algo.dt_est.deadtime_cool_reliable = True

    now = time.time()
    event = ac.on_calibration_complete(now, mock_algo)

    assert event is not None
    assert event.event_type == "smartpi_autocalib_success"
    assert ac.state == AutoCalibState.MONITORING
    assert ac.retry_count == 0


def test_autocalib_retry_logic():
    """AutoCalibTrigger should retry on partial exit."""
    ac = AutoCalibTrigger("test_vtherm")
    ac._state = AutoCalibState.TRIGGERED
    ac._post_calib_snap_a = 10
    ac._post_calib_snap_b = 10
    ac._triggered_params = ["a"]

    mock_algo = MagicMock()
    # Partial success: only 'a' improved
    mock_algo.est.learn_ok_count_a = 12
    mock_algo.est.learn_ok_count_b = 10  # No change
    mock_algo.dt_est.deadtime_heat_reliable = True
    mock_algo.dt_est.deadtime_cool_reliable = True

    now = time.time()
    event = ac.on_calibration_complete(now, mock_algo)

    assert event is not None
    assert event.event_type == "smartpi_autocalib_retry"
    assert ac.retry_count == 1
    assert ac.state == AutoCalibState.MONITORING


def test_autocalib_max_retries_degraded():
    """After max retries, model should be marked degraded."""
    ac = AutoCalibTrigger("test_vtherm")
    ac._state = AutoCalibState.TRIGGERED
    ac._retry_count = AUTOCALIB_MAX_RETRIES - 1
    ac._post_calib_snap_a = 10
    ac._post_calib_snap_b = 10
    ac._triggered_params = ["a"]

    mock_algo = MagicMock()
    mock_algo.est.learn_ok_count_a = 10  # No improvement
    mock_algo.est.learn_ok_count_b = 10
    mock_algo.dt_est.deadtime_heat_reliable = False
    mock_algo.dt_est.deadtime_cool_reliable = True

    now = time.time()
    event = ac.on_calibration_complete(now, mock_algo)

    assert event.event_type == "smartpi_autocalib_degraded"
    assert ac.model_degraded is True
    assert ac.retry_count == 0  # Reset after degraded


def test_autocalib_save_load_state():
    """AutoCalibTrigger state should persist correctly."""
    ac1 = AutoCalibTrigger("test_vtherm")
    ac1._state = AutoCalibState.MONITORING
    ac1._snapshot_ts = time.time() - 5000
    ac1._snap_ok_count_a = 42
    ac1._snap_ok_count_b = 38
    ac1._retry_count = 2
    ac1._model_degraded = False

    state = ac1.save_state()

    ac2 = AutoCalibTrigger("test_vtherm")
    ac2.load_state(state)

    assert ac2.state == AutoCalibState.MONITORING
    assert ac2._snap_ok_count_a == 42
    assert ac2._snap_ok_count_b == 38
    assert ac2.retry_count == 2


def test_autocalib_manual_calibration_success():
    """Manual calibration success should reset autocalib state."""
    ac = AutoCalibTrigger("test_vtherm")
    ac._state = AutoCalibState.MONITORING
    ac._retry_count = 2
    ac._model_degraded = True

    mock_algo = MagicMock()
    mock_algo.est.learn_ok_count_a = 15
    mock_algo.est.learn_ok_count_b = 15
    mock_algo.dt_est.deadtime_heat_reliable = True
    mock_algo.dt_est.deadtime_cool_reliable = True

    now = time.time()
    
    # Simulate force trigger
    ac.force_manual_trigger(now, mock_algo)
    assert ac.retry_count == 0

    # Simulate success
    mock_algo.est.learn_ok_count_a = 25
    mock_algo.est.learn_ok_count_b = 25

    event = ac.on_calibration_complete(now, mock_algo)

    # Should process and take new snapshot
    assert event is not None
    assert ac.retry_count == 0
    assert ac.model_degraded is False
