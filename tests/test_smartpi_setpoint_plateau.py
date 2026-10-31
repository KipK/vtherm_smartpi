"""Tests for the plateau end of disturbance trajectories."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.integral_guard import IntegralGuardSource
from custom_components.vtherm_smartpi.smartpi.recovery_plateau import (
    RecoveryPlateauDetector,
    plateau_block_min,
    plateau_tolerance_c,
)
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from helpers import force_smartpi_stable_mode

TARGET = 21.0


def _filter(manager: SmartPISetpointManager, current: float, now: float, target: float = TARGET):
    return manager.filter_setpoint(
        target_temp=target,
        current_temp=current,
        hvac_mode=VThermHvacMode_HEAT,
        a=0.4,
        b=0.02,
        ext_current_temp=5.0,
        u_ref=1.0,
        stop_deadtime_s=240.0,
        stop_deadtime_reliable=True,
        tau_reliable=True,
        deadband_c=0.05,
        kp=1.0,
        cycle_min=10.0,
        now_monotonic=now,
    )


def _disturbance_manager() -> SmartPISetpointManager:
    manager = SmartPISetpointManager(name="test")
    _filter(manager, current=TARGET, now=0.0)
    _filter(manager, current=20.6, now=60.0)
    assert manager.trajectory_active is True
    assert manager.trajectory_source == "disturbance"
    return manager


def _hold(manager: SmartPISetpointManager, current: float, start_min: int, minutes: int) -> int:
    """Feed one update per minute; return the minute at which the trajectory ended (or -1)."""
    for minute in range(start_min, start_min + minutes):
        _filter(manager, current=current, now=60.0 * minute)
        if not manager.trajectory_active:
            return minute
    return -1


def test_plateau_parameters_follow_the_cycle():
    assert plateau_block_min(10.0) == 30.0
    assert plateau_tolerance_c(30.0) == pytest.approx(0.06)


def test_plateau_detector_ignores_a_transient_disturbance():
    detector = RecoveryPlateauDetector()
    errors = [0.3 + 0.02 * m for m in range(40)] + [1.1 - 0.012 * m for m in range(80)]
    assert not any(
        detector.update(error=e, dt_min=1.0, block_min=30.0, floor_c=0.1) for e in errors
    )


def test_disturbance_trajectory_stuck_on_a_plateau_ends():
    manager = _disturbance_manager()
    ended_at = _hold(manager, current=20.6, start_min=2, minutes=200)
    # Five 30-minute blocks after the first update of the trajectory: two
    # stationary blocks and no net progress over four blocks.
    assert 150 <= ended_at <= 152
    assert manager.effective_setpoint == pytest.approx(TARGET)
    assert manager.consume_disturbance_plateau_end() is True
    assert manager.consume_disturbance_plateau_end() is False


def test_slow_quantized_tail_is_not_a_plateau():
    """Recovery tail read on 0.1 C steps: three flat blocks, but the error fell over four blocks."""
    manager = _disturbance_manager()
    errors = [0.35] * 30 + [0.27] * 30 + [0.2] * 90 + [0.12] * 60
    for minute, error in enumerate(errors, start=2):
        _filter(manager, current=round(TARGET - error, 2), now=60.0 * minute)
    assert manager.consume_disturbance_plateau_end() is False


def test_recovering_disturbance_trajectory_is_kept():
    manager = _disturbance_manager()
    for minute in range(2, 120):
        current = 20.6 + 0.3 * minute / 60.0 * 0.5  # 0.15 C/h toward the target
        _filter(manager, current=min(current, 20.9), now=60.0 * minute)
    assert manager.consume_disturbance_plateau_end() is False


def test_no_new_disturbance_trajectory_while_the_error_stays_in_the_trigger_zone():
    manager = _disturbance_manager()
    _hold(manager, current=20.6, start_min=2, minutes=200)
    assert manager.disturbance_retrigger_blocked is True
    # The error dips just under the trigger threshold, then rises again.
    _filter(manager, current=20.75, now=60.0 * 210)
    _filter(manager, current=20.6, now=60.0 * 211)
    assert manager.trajectory_active is False
    assert manager.disturbance_retrigger_blocked is True
    # Leaving the trigger zone (error under 0.3 - 0.1) lifts the block.
    _filter(manager, current=20.85, now=60.0 * 220)
    assert manager.disturbance_retrigger_blocked is False
    _filter(manager, current=20.6, now=60.0 * 221)
    assert manager.trajectory_active is True
    assert manager.trajectory_source == "disturbance"


def test_target_change_lifts_the_retrigger_block():
    manager = _disturbance_manager()
    _hold(manager, current=20.6, start_min=2, minutes=200)
    _filter(manager, current=20.6, now=60.0 * 125, target=21.5)
    assert manager.disturbance_retrigger_blocked is False


def test_reset_lifts_the_retrigger_block():
    manager = _disturbance_manager()
    _hold(manager, current=20.6, start_min=2, minutes=200)
    manager.clear_runtime_transients()
    assert manager.disturbance_retrigger_blocked is False
    assert manager.consume_disturbance_plateau_end() is False


def test_algo_releases_the_disturbance_guard_when_its_trajectory_ends_on_a_plateau():
    pi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0,
                 minimal_deactivation_delay=0, name="TestTrajectoryPlateau", deadband_c=0.05)
    force_smartpi_stable_mode(pi)
    pi.calculate(TARGET, 20.6, 5.0, 0.0, VThermHvacMode_HEAT)
    pi.integral_guard.arm(IntegralGuardSource.DISTURBANCE_RECOVERY)
    with patch.object(pi.sp_mgr, "consume_disturbance_plateau_end", return_value=True):
        pi.calculate(TARGET, 20.6, 5.0, 0.0, VThermHvacMode_HEAT)
    assert pi.integral_guard.active is False
    assert pi.integral_guard._last_release_reason == "disturbance_plateau"
