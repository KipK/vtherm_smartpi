"""Tests for SmartPISetpointManager: boost state and persistence."""

from custom_components.vtherm_smartpi.smartpi.const import (
    SETPOINT_BOOST_ERROR_MIN,
    SETPOINT_BOOST_THRESHOLD,
)
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager


def _make_manager() -> SmartPISetpointManager:
    return SmartPISetpointManager(name="test")


class TestBoost:
    def test_first_target_is_tracked_without_boost(self):
        manager = _make_manager()

        manager.update_boost_state(21.0, 0.5)

        assert manager.prev_setpoint_for_boost == 21.0
        assert manager.boost_active is False

    def test_boost_on_significant_change_and_off_when_error_small(self):
        manager = _make_manager()
        manager.update_boost_state(20.0, 0.0)

        assert manager.update_boost_state(20.0 + SETPOINT_BOOST_THRESHOLD, 1.0) is True
        assert manager.update_boost_state(20.0 + SETPOINT_BOOST_THRESHOLD, SETPOINT_BOOST_ERROR_MIN / 2) is False

    def test_small_change_only_tracks_the_target(self):
        manager = _make_manager()
        manager.update_boost_state(20.0, 0.0)

        assert manager.update_boost_state(20.1, 1.0) is False
        assert manager.prev_setpoint_for_boost == 20.1


class TestStatePersistence:
    def test_save_and_load_roundtrip(self):
        manager = _make_manager()
        manager.update_boost_state(20.0, 0.0)
        manager.update_boost_state(21.0, 1.0)
        saved = manager.save_state()

        restored = _make_manager()
        restored.load_state(saved)

        assert saved == {
            "setpoint_boost_active": True,
            "prev_setpoint_for_boost": 21.0,
        }
        assert restored.boost_active is True
        assert restored.prev_setpoint_for_boost == 21.0

    def test_load_old_state_ignores_removed_keys(self):
        manager = _make_manager()
        manager.load_state(
            {
                "filtered_setpoint": 21.0,
                "effective_setpoint": 20.4,
                "last_user_target_temp": 21.0,
                "setpoint_boost_active": True,
                "prev_setpoint_for_boost": 21.0,
                "trajectory_pending_target_change_braking": True,
                "trajectory_source": "setpoint",
                "trajectory_active": True,
                "trajectory_phase": "tracking",
                "trajectory_start_setpoint": 21.0,
                "trajectory_target_setpoint": 21.0,
                "trajectory_tau_ref_min": 50.0,
                "trajectory_elapsed_s": 300.0,
                "servo_active": True,
            }
        )

        assert manager.boost_active is True
        assert manager.prev_setpoint_for_boost == 21.0
        assert set(manager.save_state()) == {"setpoint_boost_active", "prev_setpoint_for_boost"}

    def test_load_empty_state_keeps_defaults(self):
        manager = _make_manager()
        manager.load_state({})

        assert manager.boost_active is False
        assert manager.prev_setpoint_for_boost is None


class TestReset:
    def test_reset_clears_state(self):
        manager = _make_manager()
        manager.update_boost_state(20.0, 0.0)
        manager.update_boost_state(21.0, 1.0)

        manager.reset()

        assert manager.boost_active is False
        assert manager.prev_setpoint_for_boost is None
