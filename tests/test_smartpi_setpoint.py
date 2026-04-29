"""Tests for SmartPISetpointManager trajectory shaping."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import (
    LANDING_SAFETY_MARGIN_C,
    TrajectoryPhase,
)
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.diagnostics import (
    _build_full_diagnostics,
    build_published_diagnostics,
)
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)

B_TEST = 0.02  # tau = 50 min
A_TEST = 0.4
EXT_TEST = 5.0
DEADTIME_COOL_TEST_S = 240.0


def _make_manager(enabled: bool = True) -> SmartPISetpointManager:
    return SmartPISetpointManager(name="test", enabled=enabled)


def _filter(
    manager: SmartPISetpointManager,
    target: float,
    current: float | None,
    now: float,
    hvac_mode=VThermHvacMode_HEAT,
    a: float = A_TEST,
    b: float = B_TEST,
    ext_temp: float = EXT_TEST,
    deadtime_cool_s: float = DEADTIME_COOL_TEST_S,
    tau_reliable: bool = True,
    deadband_c: float = 0.05,
    remaining_cycle_min: float = 0.0,
    u_ref: float = 1.0,
    next_u_ref: float = 0.0,
    cycle_min: float = 10.0,
):
    return manager.filter_setpoint(
        target_temp=target,
        current_temp=current,
        hvac_mode=hvac_mode,
        a=a,
        b=b,
        ext_current_temp=ext_temp,
        u_ref=u_ref,
        deadtime_cool_s=deadtime_cool_s,
        deadtime_cool_reliable=True,
        tau_reliable=tau_reliable,
        deadband_c=deadband_c,
        kp=1.0,
        next_cycle_u_ref=next_u_ref,
        cycle_min=cycle_min,
        remaining_cycle_min=remaining_cycle_min,
        now_monotonic=now,
    )


class TestTrajectoryActivation:
    def test_passthrough_when_disabled(self):
        manager = _make_manager(enabled=False)
        result = _filter(manager, target=21.0, current=19.0, now=0.0)
        assert result == 21.0
        assert manager.trajectory_active is False

    def test_no_update_when_current_temp_is_missing(self):
        manager = _make_manager()
        result = _filter(manager, target=21.0, current=None, now=0.0)
        assert result == 21.0
        assert manager.trajectory_active is False

    def test_does_not_arm_when_tau_is_unreliable(self):
        manager = _make_manager()
        result = _filter(
            manager,
            target=21.0,
            current=19.0,
            now=0.0,
            tau_reliable=False,
        )
        assert result == 21.0
        assert manager.trajectory_active is False

    def test_arms_on_significant_setpoint_change(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)

        result = _filter(manager, target=21.0, current=19.0, now=60.0)

        assert result == pytest.approx(21.0)
        assert manager.trajectory_active is False
        assert manager.trajectory_phase == TrajectoryPhase.IDLE

    def test_enters_late_braking_without_initial_bump(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)

        result = _filter(manager, target=21.0, current=20.7, now=120.0)

        assert result == pytest.approx(21.0)
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.TRACKING
        assert manager.trajectory_start_setpoint == pytest.approx(21.0)
        assert manager.trajectory_target_setpoint < 21.0

    def test_arms_on_significant_disturbance_without_setpoint_change(self):
        manager = _make_manager()
        _filter(manager, target=21.0, current=21.0, now=0.0)

        result = _filter(manager, target=21.0, current=20.6, now=60.0)

        assert result == pytest.approx(21.0)
        assert manager.trajectory_active is True
        assert manager.trajectory_start_setpoint == pytest.approx(21.0)
        assert manager.trajectory_target_setpoint < 21.0

    def test_remaining_cycle_time_can_advance_braking_entry(self):
        manager = _make_manager()
        _filter(manager, target=21.0, current=21.0, now=0.0, a=0.200835, b=0.005569, ext_temp=5.0, deadtime_cool_s=251.99418194520405)
        _filter(manager, target=22.0, current=21.0, now=60.0, a=0.200835, b=0.005569, ext_temp=5.0, deadtime_cool_s=251.99418194520405)

        result = _filter(
            manager,
            target=22.0,
            current=21.1,
            now=120.0,
            a=0.200835,
            b=0.005569,
            ext_temp=5.0,
            deadtime_cool_s=251.99418194520405,
            remaining_cycle_min=8.0,
        )

        assert result == pytest.approx(22.0)
        assert manager.trajectory_active is True

    def test_next_cycle_prediction_can_advance_braking_at_cycle_boundary(self):
        baseline = _make_manager()
        advanced = _make_manager()

        for manager in (baseline, advanced):
            _filter(
                manager,
                target=21.0,
                current=21.0,
                now=0.0,
                a=0.184723,
                b=0.005366,
                ext_temp=7.44,
                deadtime_cool_s=251.99418194520405,
            )
            _filter(
                manager,
                target=22.0,
                current=21.0,
                now=60.0,
                a=0.184723,
                b=0.005366,
                ext_temp=7.44,
                deadtime_cool_s=251.99418194520405,
            )

        baseline_result = _filter(
            baseline,
            target=22.0,
            current=21.1,
            now=120.0,
            a=0.184723,
            b=0.005366,
            ext_temp=7.44,
            deadtime_cool_s=251.99418194520405,
            remaining_cycle_min=0.0,
            u_ref=1.0,
            next_u_ref=0.0,
            cycle_min=10.0,
        )
        advanced_result = _filter(
            advanced,
            target=22.0,
            current=21.1,
            now=120.0,
            a=0.184723,
            b=0.005366,
            ext_temp=7.44,
            deadtime_cool_s=251.99418194520405,
            remaining_cycle_min=0.0,
            u_ref=1.0,
            next_u_ref=1.0,
            cycle_min=10.0,
        )

        assert baseline_result == pytest.approx(22.0)
        assert baseline.trajectory_active is False
        assert advanced_result == pytest.approx(22.0)
        assert advanced.trajectory_active is True

    def test_small_target_trim_keeps_pending_late_braking_armed(self):
        manager = _make_manager()
        _filter(
            manager,
            target=22.5,
            current=22.5,
            now=0.0,
            a=0.180774,
            b=0.005366,
            ext_temp=5.2,
            deadtime_cool_s=251.99418194520405,
        )
        _filter(
            manager,
            target=23.6,
            current=22.6,
            now=60.0,
            a=0.180774,
            b=0.005366,
            ext_temp=5.2,
            deadtime_cool_s=251.99418194520405,
            u_ref=1.0,
            next_u_ref=1.0,
            cycle_min=10.0,
        )

        trimmed = _filter(
            manager,
            target=23.5,
            current=22.6,
            now=120.0,
            a=0.180774,
            b=0.005366,
            ext_temp=5.2,
            deadtime_cool_s=251.99418194520405,
            u_ref=1.0,
            next_u_ref=1.0,
            cycle_min=10.0,
        )

        pending_after_trim = manager.save_state()["trajectory_pending_target_change_braking"]
        later = _filter(
            manager,
            target=23.5,
            current=22.9,
            now=180.0,
            a=0.178426,
            b=0.005366,
            ext_temp=5.19,
            deadtime_cool_s=251.99418194520405,
            u_ref=1.0,
            next_u_ref=1.0,
            cycle_min=10.0,
        )

        assert trimmed == pytest.approx(23.5)
        assert pending_after_trim is True
        assert later == pytest.approx(23.5)
        assert manager.trajectory_active is True
        assert manager.save_state()["trajectory_pending_target_change_braking"] is False

    def test_active_trajectory_resets_on_small_setpoint_trim_and_keeps_pending(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)

        trimmed = _filter(manager, target=20.95, current=20.6, now=180.0)

        assert trimmed == pytest.approx(20.95)
        assert manager.trajectory_active is False
        assert manager.save_state()["trajectory_pending_target_change_braking"] is True

    def test_multiple_setpoint_edits_keep_pending_from_final_demand_only(self):
        manager = _make_manager()

        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.5, current=19.0, now=60.0)
        _filter(manager, target=21.6, current=19.0, now=120.0)
        _filter(manager, target=21.5, current=19.0, now=180.0)

        final = _filter(manager, target=20.5, current=19.07, now=240.0)

        assert final == pytest.approx(20.5)
        assert manager.trajectory_active is False
        assert manager.save_state()["trajectory_pending_target_change_braking"] is True

    def test_braking_target_never_goes_below_current_temp_in_heat(self):
        manager = _make_manager()
        _filter(manager, target=22.0, current=22.0, now=0.0, a=0.200835, b=0.005439, ext_temp=5.0, deadtime_cool_s=251.99418194520405)
        _filter(manager, target=23.0, current=22.0, now=60.0, a=0.200835, b=0.005439, ext_temp=5.0, deadtime_cool_s=251.99418194520405)

        result = _filter(
            manager,
            target=23.0,
            current=22.59,
            now=120.0,
            a=0.200835,
            b=0.005439,
            ext_temp=5.0,
            deadtime_cool_s=251.99418194520405,
            remaining_cycle_min=8.0,
        )

        assert result == pytest.approx(23.0)
        assert manager.trajectory_active is True
        assert manager.trajectory_target_setpoint == pytest.approx(22.6925)


class TestTrajectoryProgression:
    def test_progresses_toward_target_over_time(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        first = _filter(manager, target=21.0, current=19.0, now=60.0)
        second = _filter(manager, target=21.0, current=20.6, now=120.0)
        third = _filter(manager, target=21.0, current=20.7, now=180.0)

        assert first == pytest.approx(21.0)
        assert second == pytest.approx(21.0)
        assert third < 21.0
        assert manager.trajectory_active is True

    def test_stays_active_below_threshold_until_convergence(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)

        result = _filter(manager, target=21.0, current=20.72, now=180.0)

        assert result < 21.0
        assert manager.trajectory_active is True

    def test_release_phase_smooths_return_to_raw_target(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)
        braking_sp = _filter(manager, target=21.0, current=20.7, now=180.0)

        release_sp = _filter(
            manager,
            target=21.0,
            current=20.88,
            now=240.0,
            u_ref=0.80,
        )

        assert braking_sp < 21.0
        assert braking_sp < release_sp < 21.0
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.RELEASE

    def test_release_waits_until_proportional_handoff_is_small(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.96,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.RELEASE.value,
                "trajectory_start_setpoint": 20.8,
                "trajectory_target_setpoint": 21.0,
                "trajectory_tau_ref_min": 5.0,
                "trajectory_current_setpoint": 20.96,
                "trajectory_elapsed_s": 120.0,
            }
        )

        result = _filter(manager, target=21.0, current=20.88, now=180.0, u_ref=0.80)

        assert result < 21.0
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.RELEASE

    def test_setpoint_release_does_not_handoff_before_measured_target_is_near(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.99,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.RELEASE.value,
                "trajectory_start_setpoint": 20.8,
                "trajectory_target_setpoint": 21.0,
                "trajectory_tau_ref_min": 5.0,
                "trajectory_current_setpoint": 20.99,
                "trajectory_elapsed_s": 120.0,
                "trajectory_source": "setpoint",
            }
        )

        result = manager.filter_setpoint(
            target_temp=21.0,
            current_temp=20.94,
            hvac_mode=VThermHvacMode_HEAT,
            a=A_TEST,
            b=B_TEST,
            ext_current_temp=EXT_TEST,
            u_ref=0.0,
            deadtime_cool_s=DEADTIME_COOL_TEST_S,
            deadtime_cool_reliable=True,
            tau_reliable=True,
            deadband_c=0.05,
            kp=1.0,
            next_cycle_u_ref=0.0,
            cycle_min=10.0,
            remaining_cycle_min=0.0,
            now_monotonic=180.0,
        )

        assert result < 21.0
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.RELEASE
        assert manager.trajectory_bumpless_ready is None

    def test_active_tracking_keeps_small_braking_window_margin_without_flipping_to_release(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 22.0,
                "effective_setpoint": 22.0,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.TRACKING.value,
                "trajectory_start_setpoint": 22.0,
                "trajectory_target_setpoint": 21.003,
                "trajectory_tau_ref_min": 9.588,
                "trajectory_current_setpoint": 22.0,
                "trajectory_elapsed_s": 0.0,
            }
        )

        result = _filter(
            manager,
            target=22.0,
            current=20.67,
            now=10.0,
            a=0.170377,
            b=0.005592,
            ext_temp=15.57,
            deadtime_cool_s=251.99418194520405,
            u_ref=1.0,
            next_u_ref=1.0,
            cycle_min=2.0,
            remaining_cycle_min=1.753,
        )

        assert result == pytest.approx(22.0)
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.TRACKING

    def test_setpoint_release_stays_locked_even_if_braking_reappears(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 24.31,
                "effective_setpoint": 24.31,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.RELEASE.value,
                "trajectory_start_setpoint": 24.215,
                "trajectory_target_setpoint": 24.5,
                "trajectory_tau_ref_min": 13.087,
                "trajectory_current_setpoint": 24.31,
                "trajectory_elapsed_s": 16.53,
                "trajectory_source": "setpoint",
            }
        )

        result = manager.filter_setpoint(
            target_temp=24.5,
            current_temp=24.26,
            hvac_mode=VThermHvacMode_HEAT,
            a=0.171338,
            b=0.005397,
            ext_current_temp=5.0,
            u_ref=0.6,
            deadtime_cool_s=251.99418194520405,
            deadtime_cool_reliable=True,
            tau_reliable=True,
            deadband_c=0.05,
            kp=None,
            next_cycle_u_ref=1.0,
            cycle_min=2.0,
            remaining_cycle_min=0.101,
            now_monotonic=180.0,
        )

        assert manager.trajectory_braking_needed is True
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.RELEASE
        assert manager.trajectory_target_setpoint == pytest.approx(24.5)
        assert result == pytest.approx(24.31)

    def test_release_tau_can_be_faster_than_braking_and_complete_cleanly(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.8,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.TRACKING.value,
                "trajectory_start_setpoint": 21.0,
                "trajectory_target_setpoint": 20.75,
                "trajectory_tau_ref_min": 5.0,
                "trajectory_current_setpoint": 20.8,
                "trajectory_elapsed_s": 120.0,
            }
        )

        first = _filter(manager, target=21.0, current=20.9, now=180.0, u_ref=0.0, next_u_ref=0.0)
        first_tau = manager._trajectory.tau_ref_min
        first_phase = manager.trajectory_phase
        second = _filter(manager, target=21.0, current=20.95, now=240.0, u_ref=0.0, next_u_ref=0.0)

        assert first_phase == TrajectoryPhase.RELEASE
        assert first_tau == pytest.approx(2.5)
        assert first < 21.0
        assert first < second <= 21.0
        assert manager.trajectory_phase in {TrajectoryPhase.RELEASE, TrajectoryPhase.IDLE}

    def test_model_not_ready_keeps_release_active_until_bumpless_handoff(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.8,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.TRACKING.value,
                "trajectory_start_setpoint": 21.0,
                "trajectory_target_setpoint": 20.75,
                "trajectory_tau_ref_min": 5.0,
                "trajectory_current_setpoint": 20.8,
                "trajectory_elapsed_s": 120.0,
            }
        )

        result = _filter(
            manager,
            target=21.0,
            current=20.9,
            now=180.0,
            ext_temp=5.0,
            u_ref=0.0,
            next_u_ref=0.0,
            remaining_cycle_min=0.0,
        )

        assert result < 21.0
        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.RELEASE
        assert manager.trajectory_model_ready is False

    def test_release_does_not_cross_below_current_temp_in_heating(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 20.5,
                "effective_setpoint": 20.37,
                "trajectory_active": True,
                "trajectory_phase": TrajectoryPhase.RELEASE.value,
                "trajectory_start_setpoint": 20.179,
                "trajectory_target_setpoint": 20.5,
                "trajectory_tau_ref_min": 14.501,
                "trajectory_current_setpoint": 20.37,
                "trajectory_elapsed_s": 756.853,
            }
        )

        result = _filter(
            manager,
            target=20.5,
            current=20.44,
            now=820.0,
            a=0.173127,
            b=0.005592,
            ext_temp=5.0,
            deadtime_cool_s=251.99418194520405,
            u_ref=0.133333,
            next_u_ref=0.127483,
            cycle_min=2.0,
        )

        assert result >= 20.49
        assert result <= 20.5
        assert manager.trajectory_phase in {TrajectoryPhase.RELEASE, TrajectoryPhase.IDLE}

    def test_stops_when_target_is_reached(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)

        result = _filter(manager, target=21.0, current=21.0, now=180.0)

        assert result == 21.0
        assert manager.trajectory_active is False
        assert manager.trajectory_phase == TrajectoryPhase.IDLE

    def test_demand_reduction_returns_to_passthrough(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)

        result = _filter(manager, target=20.0, current=20.6, now=180.0)

        assert result == 20.0
        assert manager.trajectory_active is False

    def test_restarts_smoothly_on_further_demand_increase(self):
        manager = _make_manager()
        _filter(manager, target=19.0, current=19.0, now=0.0)
        _filter(manager, target=21.0, current=19.0, now=60.0)
        _filter(manager, target=21.0, current=20.6, now=120.0)
        previous = _filter(manager, target=21.0, current=20.7, now=180.0)

        result = _filter(manager, target=21.5, current=20.7, now=240.0)

        assert previous < 21.0
        assert result == pytest.approx(21.5)
        assert manager.trajectory_active is False
        assert manager.filtered_setpoint == pytest.approx(21.5)

    def test_supports_signed_cooling_logic(self):
        manager = _make_manager()
        _filter(manager, target=23.0, current=23.0, now=0.0, hvac_mode=VThermHvacMode_COOL)

        result = _filter(
            manager,
            target=23.0,
            current=23.5,
            now=60.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert result == pytest.approx(23.0)
        assert manager.trajectory_active is True


class TestStatePersistence:
    def test_save_state_contains_trajectory_fields(self):
        manager = _make_manager()
        _filter(manager, target=21.0, current=21.0, now=0.0)
        _filter(manager, target=21.0, current=20.6, now=60.0)

        saved = manager.save_state()

        assert "trajectory_active" in saved
        assert "trajectory_phase" in saved
        assert "trajectory_start_setpoint" in saved
        assert "trajectory_target_setpoint" in saved
        assert "trajectory_tau_ref_min" in saved
        assert "trajectory_elapsed_s" in saved
        assert "effective_setpoint" in saved

    def test_save_and_load_roundtrip(self):
        manager = _make_manager()
        _filter(manager, target=21.0, current=21.0, now=0.0)
        _filter(manager, target=21.0, current=20.6, now=60.0)
        expected = _filter(manager, target=21.0, current=20.7, now=120.0)
        saved = manager.save_state()

        restored = _make_manager()
        restored.load_state(saved)
        result = _filter(restored, target=21.0, current=20.75, now=180.0)

        assert restored.trajectory_active is True
        assert restored.filtered_setpoint == manager.filtered_setpoint
        assert result == pytest.approx(expected)

    def test_load_legacy_servo_state_maps_to_tracking(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.4,
                "servo_active": True,
                "servo_phase": "landing",
                "servo_target_at_activation": 19.5,
                "trajectory_target_setpoint": 21.0,
                "trajectory_tau_ref_min": 50.0,
                "trajectory_elapsed_s": 300.0,
            }
        )

        assert manager.trajectory_active is True
        assert manager.trajectory_phase == TrajectoryPhase.TRACKING
        assert manager.trajectory_start_setpoint == pytest.approx(19.5)
        assert manager.trajectory_target_setpoint == pytest.approx(21.0)


class TestReset:
    def test_reset_clears_state(self):
        manager = _make_manager()
        _filter(manager, target=21.0, current=21.0, now=0.0)
        _filter(manager, target=21.0, current=20.6, now=60.0)

        manager.reset()

        assert manager.filtered_setpoint is None
        assert manager.effective_setpoint is None
        assert manager.trajectory_active is False
        assert manager.trajectory_phase == TrajectoryPhase.IDLE


# ---------------------------------------------------------------------------
# Setpoint landing tests (HEAT-only command-aware governor)
# ---------------------------------------------------------------------------


def _decision(manager, **overrides):
    """Call _compute_landing_decision with sensible defaults plus overrides."""
    kwargs = dict(
        target_temp=25.0,
        current_temp=24.75,
        ext_current_temp=15.0,
        hvac_mode=VThermHvacMode_HEAT,
        signed_error=0.25,
        a=0.075,
        b=0.0034,
        u_ref=0.35,
        u_ff_eff=0.39,
        kp=1.45,
        ki=0.0048,
        integral=-43.0,
        deadtime_cool_s=765.0,
        deadtime_cool_reliable=True,
        tau_reliable=True,
        deadband_c=0.05,
        remaining_cycle_min=1.0,
        temperature_slope_h=0.5,
    )
    kwargs.update(overrides)
    return manager._compute_landing_decision(**kwargs)


class TestSetpointLanding:
    def test_landing_inactive_when_model_unreliable(self):
        manager = _make_manager()
        manager._trajectory_source = "setpoint"
        manager._trajectory.start(
            start_setpoint=25.0,
            target_setpoint=25.0,
            tau_ref_min=10.0,
            now_monotonic=0.0,
        )

        decision = _decision(manager, tau_reliable=False)

        assert manager.landing_active is False
        assert decision.active is False
        assert decision.reason == "tau_unreliable"

    def test_landing_inactive_outside_landing_band(self):
        manager = _make_manager()
        manager._trajectory_source = "setpoint"
        manager._trajectory.start(
            start_setpoint=25.0,
            target_setpoint=25.0,
            tau_ref_min=10.0,
            now_monotonic=0.0,
        )

        decision = _decision(manager, signed_error=0.80)

        assert decision.active is False
        assert decision.reason == "outside_landing_band"

    def test_landing_cap_limits_sp_for_p_heat(self):
        manager = _make_manager()
        # Seed last user target.
        _filter(
            manager,
            target=24.0,
            current=24.0,
            now=0.0,
            a=0.075,
            b=0.0034,
            ext_temp=15.0,
            deadtime_cool_s=765.0,
            u_ref=1.0,
        )
        # Arm pending late braking via setpoint change (signed_error=0.5).
        _filter(
            manager,
            target=25.0,
            current=24.5,
            now=60.0,
            a=0.075,
            b=0.0034,
            ext_temp=15.0,
            deadtime_cool_s=765.0,
            u_ref=1.0,
        )
        # Same target, signed_error=0.40 — inside landing band; the model
        # predicts enough rise for the setpoint trajectory to arm with
        # source="setpoint", so the landing decision can be evaluated.
        sp_for_p = manager.filter_setpoint(
            target_temp=25.0,
            current_temp=24.6,
            hvac_mode=VThermHvacMode_HEAT,
            a=0.075,
            b=0.0034,
            ext_current_temp=15.0,
            u_ref=1.0,
            deadtime_cool_s=765.0,
            deadtime_cool_reliable=True,
            tau_reliable=True,
            deadband_c=0.05,
            kp=1.45,
            next_cycle_u_ref=1.0,
            cycle_min=10.0,
            remaining_cycle_min=1.0,
            now_monotonic=120.0,
            u_ff_eff=0.39,
            ki=0.0048,
            integral=-43.0,
            temperature_slope_h=0.5,
        )

        assert manager.trajectory_source == "setpoint"
        assert manager.landing_active is True
        assert manager.landing_u_cap is not None
        assert manager.landing_sp_for_p_cap is not None
        assert sp_for_p <= manager.landing_sp_for_p_cap + 1e-9
        assert manager.landing_release_allowed is False

    def test_landing_coast_when_passive_prediction_reaches_margin(self):
        # Passive cooling alone already overshoots the safety margin:
        # current temperature is well above target, so the cap collapses to 0.
        manager = _make_manager()
        manager._trajectory_source = "setpoint"
        manager._trajectory.start(
            start_setpoint=25.0,
            target_setpoint=25.0,
            tau_ref_min=10.0,
            now_monotonic=0.0,
        )

        decision = _decision(
            manager,
            target_temp=25.0,
            current_temp=24.99,
            ext_current_temp=24.99,
            signed_error=0.01,
            u_ref=1.0,
        )

        assert decision.active is True
        assert decision.coast_required is True
        assert decision.u_cap == pytest.approx(0.0)

    def test_landing_releases_residual_error_in_release_phase(self):
        manager = _make_manager()
        manager._trajectory_source = "setpoint"
        manager._trajectory.start(
            start_setpoint=24.9,
            target_setpoint=25.0,
            tau_ref_min=10.0,
            now_monotonic=0.0,
        )
        manager._trajectory.set_target(25.0, phase=TrajectoryPhase.RELEASE)

        decision = _decision(
            manager,
            target_temp=25.0,
            current_temp=24.92,
            signed_error=0.08,
            temperature_slope_h=None,
        )

        assert decision.active is False
        assert decision.reason == "residual_release"

    def test_landing_residual_release_is_sticky_until_demand_recovers(self):
        manager = _make_manager()
        manager._trajectory_source = "setpoint"
        manager._trajectory.start(
            start_setpoint=24.9,
            target_setpoint=25.0,
            tau_ref_min=10.0,
            now_monotonic=0.0,
        )
        manager._trajectory.set_target(25.0, phase=TrajectoryPhase.RELEASE)

        released = _decision(
            manager,
            target_temp=25.0,
            current_temp=24.92,
            signed_error=0.08,
            temperature_slope_h=None,
        )
        still_released = _decision(
            manager,
            target_temp=25.0,
            current_temp=24.96,
            signed_error=0.04,
            temperature_slope_h=0.5,
        )
        rearmed = _decision(
            manager,
            target_temp=25.0,
            current_temp=24.69,
            signed_error=0.31,
            temperature_slope_h=0.5,
        )

        assert released.reason == "residual_release"
        assert still_released.active is False
        assert still_released.reason == "residual_release"
        assert rearmed.active is True
        assert rearmed.reason == "cap"

    def test_landing_noop_for_cool(self):
        manager = _make_manager()
        # First seed the manager with a COOL passthrough state.
        result_seed = _filter(
            manager,
            target=22.0,
            current=23.0,
            now=0.0,
            hvac_mode=VThermHvacMode_COOL,
        )
        result = _filter(
            manager,
            target=22.0,
            current=22.2,
            now=60.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert result_seed == pytest.approx(22.0)
        assert manager.landing_active is False

        decision = _decision(
            manager,
            hvac_mode=VThermHvacMode_COOL,
            target_temp=22.0,
            current_temp=22.2,
            signed_error=0.2,
        )
        assert decision.active is False
        assert decision.reason == "cool_unsupported"
        # COOL may keep its existing trajectory shaping; landing must not cap it.
        assert 22.0 <= result <= 22.2
        assert manager.landing_u_cap is None
        assert manager.landing_sp_for_p_cap is None


class TestControllerCommandCap:
    def test_apply_command_cap_clamps_u_cmd_only(self):
        controller = SmartPIController(name="test")
        controller.u_cmd = 0.8
        controller.u_pi = 0.6

        capped = controller.apply_command_cap(0.2)

        assert capped == pytest.approx(0.2)
        assert controller.u_cmd == pytest.approx(0.2)
        assert controller.u_cmd_before_cap == pytest.approx(0.8)
        assert controller.u_cmd_cap == pytest.approx(0.2)
        # u_pi must remain untouched — it is the raw PI diagnostic.
        assert controller.u_pi == pytest.approx(0.6)


class TestLandingDiagnostics:
    @staticmethod
    def _make_algo():
        return SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPILandingDiag",
        )

    def test_full_debug_diagnostics_include_landing_keys(self):
        algo = self._make_algo()
        diag = _build_full_diagnostics(algo)

        for key in (
            "landing_active",
            "landing_reason",
            "landing_u_cap",
            "landing_sp_for_p_cap",
            "landing_predicted_temperature",
            "landing_predicted_rise",
            "landing_target_margin",
            "landing_release_allowed",
            "landing_coast_required",
            "landing_u_cmd_before_cap",
            "landing_u_cmd_after_cap",
        ):
            assert key in diag, f"missing landing key in debug diagnostics: {key}"

    def test_published_setpoint_only_exposes_essential_landing_fields(self):
        algo = self._make_algo()
        published = build_published_diagnostics(algo)

        setpoint = published["setpoint"]
        landing_keys = {k for k in setpoint if k.startswith("landing_")}
        assert landing_keys == {
            "landing_active",
            "landing_reason",
            "landing_u_cap",
            "landing_coast_required",
        }


# Reference the imported symbol so static analyzers do not flag it as unused.
_ = LANDING_SAFETY_MARGIN_C
