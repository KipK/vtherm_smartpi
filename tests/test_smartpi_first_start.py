"""Test SmartPI specific behaviors: first start, hysteresis mode, and monotonic time."""

import pytest
import time
from unittest.mock import patch, MagicMock
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import (
    SmartPIPhase,
    HYST_UPPER_C,
    HYST_LOWER_C,
    AB_HISTORY_SIZE,
)
from custom_components.vtherm_smartpi.smartpi.integral_guard import IntegralGuardSource
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


class TestSmartPIHysteresisPhase:
    """Tests for hysteresis control during learning phase."""

    def test_starts_in_hysteresis_phase(self):
        """Test that SmartPI starts in HYSTERESIS phase with empty measurement history."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_Phase"
        )
        
        assert smartpi.phase == SmartPIPhase.HYSTERESIS
        assert len(smartpi.est.a_meas_hist) == 0
        assert len(smartpi.est.b_meas_hist) == 0

    def test_hysteresis_turns_on_when_cold(self):
        """Test that hysteresis turns ON when temp is below setpoint - HYST_LOWER_C."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_HystOn"
        )
        
        # Temperature is 0.5°C below setpoint (below threshold of -0.3°C)
        smartpi.calculate(
            target_temp=20.0,
            current_temp=19.5,  # 20 - 0.5 < 20 - 0.3 -> should turn ON
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
        assert smartpi.on_percent == 1.0, "Should be fully ON when cold"
        assert smartpi.ctl.hysteresis_state == "on"

    def test_hysteresis_turns_off_when_hot(self):
        """Test that hysteresis turns OFF when temp is above setpoint + HYST_UPPER_C."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_HystOff"
        )
        
        # First turn ON
        smartpi._on_percent = 1.0
        
        # Temperature is 0.6°C above setpoint (above threshold of +0.5°C)
        smartpi.calculate(
            target_temp=20.0,
            current_temp=20.6,  # 20.6 > 20 + 0.5 -> should turn OFF
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
        assert smartpi.on_percent == 0.0, "Should be OFF when hot"
        assert smartpi.ctl.hysteresis_state == "off"

    def test_hysteresis_maintains_state_in_band(self):
        """Test that hysteresis maintains previous state when in the band."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_HystBand"
        )
        
        # Start with ON state
        smartpi._on_percent = 1.0
        
        # Temperature is at setpoint (within band: -0.3 < 0 < +0.5)
        smartpi.calculate(
            target_temp=20.0,
            current_temp=20.0,  # Exactly at setpoint -> in band
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
        assert smartpi.on_percent == 1.0, "Should maintain ON state in band"
        assert smartpi.ctl.hysteresis_state == "band"
        
        # Now test maintaining OFF state
        smartpi._on_percent = 0.0
        smartpi.calculate(
            target_temp=20.0,
            current_temp=20.2,  # Still in band
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
        assert smartpi.on_percent == 0.0, "Should maintain OFF state in band"

    def test_no_integral_accumulation_in_hysteresis(self):
        """Test that integral does not accumulate during hysteresis phase."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_NoIntegral"
        )
        
        assert smartpi.integral == 0.0
        
        # Multiple calls in hysteresis mode
        for _ in range(5):
            smartpi.calculate(
                target_temp=20.0,
                current_temp=19.0,
                ext_current_temp=10.0,
                slope=0,
                hvac_mode=VThermHvacMode_HEAT
            )
        
        # Integral should remain 0 (no PI calculation in hysteresis)
        assert smartpi.integral == 0.0, "Integral should not accumulate in hysteresis mode"

    def test_diagnostics_show_regulation_mode(self):
        """Test that diagnostics correctly report regulation_mode."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_Diag"
        )
        
        diag = smartpi.get_diagnostics()
        
        assert diag["phase"] == SmartPIPhase.HYSTERESIS
        assert diag["regulation_mode"] == "hysteresis"
        assert "hysteresis_state" in diag

    def test_reboot_clears_transient_guard_and_trajectory_state(self):
        """A reboot must not restore active transient SmartPI recovery states."""
        smartpi1 = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RebootTransient",
        )
        smartpi1.integral_guard.arm(IntegralGuardSource.SETPOINT_CHANGE)
        smartpi1.sp_mgr.filtered_setpoint = 19.0
        smartpi1.sp_mgr.effective_setpoint = 18.9
        smartpi1.sp_mgr._pending_target_change_braking = True
        smartpi1.sp_mgr._trajectory.start(
            start_setpoint=19.0,
            target_setpoint=18.8,
            tau_ref_min=5.0,
            now_monotonic=100.0,
        )
        smartpi1.sp_mgr._trajectory_source = "setpoint"
        smartpi1.ctl.start_integral_hold("window_resume")

        saved_state = smartpi1.save_state()

        smartpi2 = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RebootTransient_Restored",
            saved_state=saved_state,
        )

        assert smartpi2.integral_guard.active is False
        assert smartpi2.sp_mgr.trajectory_active is False
        assert smartpi2.sp_mgr.trajectory_source == "none"
        assert smartpi2.ctl.integral_hold_mode == "none"


class TestSmartPIPhaseTransition:
    """Tests for transition from HYSTERESIS to STABLE phase."""

    def test_transition_requires_31_measurements(self):
        """Test that phase transitions to STABLE only after 31 A and B measurements."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_Transition"
        )
        
        # Initially in HYSTERESIS
        assert smartpi.phase == SmartPIPhase.HYSTERESIS
        
        # Add 30 measurements (not enough)
        for i in range(30):
            smartpi.est.a_meas_hist.append(0.01 + i * 0.001)
            smartpi.est.b_meas_hist.append(0.001 + i * 0.0001)
        
        assert smartpi.phase == SmartPIPhase.HYSTERESIS, "Should still be HYSTERESIS with 30 measurements"
        
        # Add the 31st measurement
        smartpi.est.a_meas_hist.append(0.05)
        smartpi.est.b_meas_hist.append(0.005)
        
        assert len(smartpi.est.a_meas_hist) == 31
        assert len(smartpi.est.b_meas_hist) == 31
        assert smartpi.phase == SmartPIPhase.STABLE, "Should transition to STABLE with 31 measurements"

    def test_diagnostics_change_after_transition(self):
        """Test that diagnostics show 'smartpi' mode after transition."""
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_DiagTransition"
        )
        
        # Fill measurements
        for i in range(AB_HISTORY_SIZE):
            smartpi.est.a_meas_hist.append(0.01)
            smartpi.est.b_meas_hist.append(0.001)
        
        diag = smartpi.get_diagnostics()
        
        assert diag["phase"] == SmartPIPhase.STABLE
        assert diag["regulation_mode"] == "smartpi"


@patch("custom_components.vtherm_smartpi.algo.time.monotonic")
class TestSmartPIMonotonicTime:
    """Tests for monotonic time handling."""

    def test_timestamp_updated_in_hysteresis(self, mock_mono):
        """Test that timestamp is updated even in hysteresis mode."""
        mock_mono.return_value = 1000.0
        
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_TimestampHyst"
        )
        
        smartpi.calculate(
            target_temp=20.0,
            current_temp=19.0,
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT
        )
        
        assert smartpi._last_calculate_time == 1000.0


class TestSmartPIRebootBehavior:
    """Tests for SmartPI behavior at HA reboot."""

    def _make_saved_state(self, integral_value: float = 42.5) -> dict:
        """Build a minimal saved_state with a non-zero integral."""
        return {
            "version": 2,
            "on_percent": 0.6,
            "last_target_temp": 20.0,
            "cycles_since_reset": 10,
            "accumulated_dt": 120.0,
            "deadtime_skip_count_a": 0,
            "deadtime_skip_count_b": 0,
            "learning_resume_ts": None,
            "learning_start_date": None,
            "est_state": {},
            "dt_est_state": {},
            "gov_state": {},
            "ctl_state": {
                "integral": integral_value,
                "hysteresis_thermal_guard": False,
            },
            "sp_mgr_state": {},
            "lw_state": {},
            "db_state": {},
            "cal_state": {},
            "gs_state": {},
            "twin_state": {},
            "guards_state": {},
            "ac_state": {},
        }

    def test_reboot_integral_is_restored(self):
        """At reboot, integral must be properly restored from the persisted value."""
        saved_state = self._make_saved_state(integral_value=42.5)
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RebootIntegral",
            saved_state=saved_state,
        )
        assert smartpi.integral == 42.5, (
            f"Expected integral=42.5 after reboot, got {smartpi.integral}"
        )

    def test_reboot_u_prev_is_zero(self):
        """At reboot, u_prev must be 0."""
        saved_state = self._make_saved_state(integral_value=10.0)
        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RebootUPrev",
            saved_state=saved_state,
        )
        assert smartpi.u_prev == 0.0, (
            f"Expected u_prev=0.0 after reboot, got {smartpi.u_prev}"
        )

    @patch("custom_components.vtherm_smartpi.algo.time.monotonic")
    def test_reboot_learning_frozen_one_cycle(self, mock_mono):
        """At reboot, learning must be frozen for exactly 1 cycle (cycle_min minutes)."""
        mock_mono.return_value = 1000.0
        cycle_min = 10
        saved_state = self._make_saved_state()

        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=cycle_min,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RebootFreeze",
            saved_state=saved_state,
        )

        # Trigger the first calculate() to activate the startup grace period logic
        smartpi.calculate(
            target_temp=20.0,
            current_temp=19.0,
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )

        expected_resume_ts = 1000.0 + (cycle_min * 60.0)
        actual_resume_ts = smartpi.learn_win.learning_resume_ts

        assert actual_resume_ts is not None, "learning_resume_ts must be set after reboot"
        assert abs(actual_resume_ts - expected_resume_ts) < 1.0, (
            f"Expected learning_resume_ts ~ {expected_resume_ts}, got {actual_resume_ts}"
        )

    @patch("custom_components.vtherm_smartpi.algo.time.monotonic")
    def test_ff_warmup_no_integral_collapse(self, mock_mono):
        """Integral must stay bounded during the FF warmup phase after reboot.

        Regression test: after reboot, u_ff ramps up by ~1/ff_warmup_cycles per cycle.
        Without the guard, the asymmetric FF bumpless drives the integral by -d_uff/Ki
        per cycle (e.g. -48 degrees C*min with salon params), collapsing to large negatives.
        The guard (cycles_since_reset >= ff_warmup_cycles) must block this.
        """
        cycle_min = 10
        ff_warmup_cycles = 6

        # Saved state with a learned model (non-zero a/b) so FF is active.
        # Salon-like params: a=0.025, b=0.001, Ki~0.00129
        saved_state = {
            "version": 2,
            "on_percent": 0.0,
            "last_target_temp": None,
            "cycles_since_reset": 0,
            "accumulated_dt": 0.0,
            "deadtime_skip_count_a": 0,
            "deadtime_skip_count_b": 0,
            "learning_resume_ts": None,
            "learning_start_date": None,
            "est_state": {
                "a": 0.025565,
                "b": 0.001036,
                "learn_ok_count": 254,
                "learn_ok_count_a": 67,
                "learn_ok_count_b": 187,
                "a_meas_hist": [0.025] * 35,
                "b_meas_hist": [0.001] * 35,
            },
            "dt_est_state": {},
            "gov_state": {},
            "ctl_state": {"integral": 0.0, "hysteresis_thermal_guard": False},
            "sp_mgr_state": {},
            "lw_state": {},
            "db_state": {},
            "cal_state": {},
            "gs_state": {"kp": 0.948701, "ki": 0.00129},
            "twin_state": {},
            "guards_state": {},
            "ac_state": {},
        }

        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=cycle_min,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_FFWarmupGuard",
            saved_state=saved_state,
        )

        # Run ff_warmup_cycles + 2 cycles at cycle_min intervals.
        # Near setpoint (salon: 19°C / 19.09°C), cold outside (5°C) -> FF active and growing.
        t_start = 1000.0
        for cycle_idx in range(ff_warmup_cycles + 2):
            mock_mono.return_value = t_start + cycle_idx * (cycle_min * 60.0)
            smartpi.calculate(
                target_temp=19.0,
                current_temp=19.09,
                ext_current_temp=5.0,
                hvac_mode=VThermHvacMode_HEAT,
            )

        # Without the guard, integral collapses to ~ -(ff_warmup_cycles * 48) = -384.
        # With the guard, only normal PI accumulation occurs: |integral| << 5.
        assert abs(smartpi.integral) < 5.0, (
            f"Integral collapsed during FF warmup: {smartpi.integral:.2f} degrees C*min "
            f"(expected |integral| < 5.0). FF bumpless guard may be missing."
        )

    @patch("custom_components.vtherm_smartpi.algo.time.monotonic")
    def test_reboot_instant_ff_with_learned_model(self, mock_mono):
        """After reboot with a fully learned model, FF must be at full strength from cycle 1.

        When learn_ok_count_a >= 10 and tau_reliable, the FF warmup ramp is
        bypassed: _cycles_since_reset is set to ff_warmup_cycles in load_state().
        Integral must remain bounded (no bumpless-induced collapse).
        """
        cycle_min = 10
        ff_warmup_cycles = 6

        saved_state = {
            "version": 2,
            "on_percent": 0.0,
            "last_target_temp": None,
            "cycles_since_reset": 0,
            "accumulated_dt": 0.0,
            "deadtime_skip_count_a": 0,
            "deadtime_skip_count_b": 0,
            "learning_resume_ts": None,
            "learning_start_date": None,
            "est_state": {
                "a": 0.025565,
                "b": 0.001036,
                "learn_ok_count": 254,
                "learn_ok_count_a": 67,
                "learn_ok_count_b": 187,
                "a_meas_hist": [0.025] * 35,
                "b_meas_hist": [0.001] * 35,
                "b_hat_hist": [0.001036] * 10,
            },
            "dt_est_state": {},
            "gov_state": {},
            "ctl_state": {"integral": 0.0, "hysteresis_thermal_guard": False},
            "traj_state": {},
            "lw_state": {},
            "db_state": {},
            "cal_state": {},
            "gs_state": {"kp": 0.948701, "ki": 0.00129},
            "twin_state": {},
            "guards_state": {},
            "ac_state": {},
        }

        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=cycle_min,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_InstantFF",
            saved_state=saved_state,
        )

        # Verify that the ramp was bypassed
        assert smartpi._cycles_since_reset == ff_warmup_cycles, (
            f"Expected cycles_since_reset={ff_warmup_cycles} (ramp bypassed), "
            f"got {smartpi._cycles_since_reset}"
        )

        # Run 3 cycles and verify integral stays bounded
        t_start = 1000.0
        for cycle_idx in range(3):
            mock_mono.return_value = t_start + cycle_idx * (cycle_min * 60.0)
            smartpi.calculate(
                target_temp=19.0,
                current_temp=19.09,
                ext_current_temp=5.0,
                hvac_mode=VThermHvacMode_HEAT,
            )

        assert abs(smartpi.integral) < 5.0, (
            f"Integral collapsed with instant FF: {smartpi.integral:.2f} "
            f"(expected |integral| < 5.0)"
        )

    def test_reboot_ramp_active_without_b_hat_hist(self):
        """When b_hat_hist is absent (tau not reliable), the FF ramp must stay active."""
        saved_state = {
            "version": 2,
            "on_percent": 0.0,
            "last_target_temp": None,
            "cycles_since_reset": 0,
            "accumulated_dt": 0.0,
            "deadtime_skip_count_a": 0,
            "deadtime_skip_count_b": 0,
            "learning_resume_ts": None,
            "learning_start_date": None,
            "est_state": {
                "a": 0.025565,
                "b": 0.001036,
                "learn_ok_count": 254,
                "learn_ok_count_a": 67,
                "learn_ok_count_b": 187,
                "a_meas_hist": [0.025] * 35,
                "b_meas_hist": [0.001] * 35,
            },
            "dt_est_state": {},
            "gov_state": {},
            "ctl_state": {"integral": 0.0, "hysteresis_thermal_guard": False},
            "traj_state": {},
            "lw_state": {},
            "db_state": {},
            "cal_state": {},
            "gs_state": {"kp": 0.948701, "ki": 0.00129},
            "twin_state": {},
            "guards_state": {},
            "ac_state": {},
        }

        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RampFallback",
            saved_state=saved_state,
        )

        assert smartpi._cycles_since_reset == 0, (
            f"Expected cycles_since_reset=0 (ramp active), got {smartpi._cycles_since_reset}"
        )

    def test_reboot_ramp_active_with_low_learn_count(self):
        """When learn_ok_count_a < 10, the FF ramp must stay active."""
        saved_state = {
            "version": 2,
            "on_percent": 0.0,
            "last_target_temp": None,
            "cycles_since_reset": 0,
            "accumulated_dt": 0.0,
            "deadtime_skip_count_a": 0,
            "deadtime_skip_count_b": 0,
            "learning_resume_ts": None,
            "learning_start_date": None,
            "est_state": {
                "a": 0.025565,
                "b": 0.001036,
                "learn_ok_count": 15,
                "learn_ok_count_a": 5,
                "learn_ok_count_b": 10,
                "a_meas_hist": [0.025] * 10,
                "b_meas_hist": [0.001] * 10,
                "b_hat_hist": [0.001036] * 10,
            },
            "dt_est_state": {},
            "gov_state": {},
            "ctl_state": {"integral": 0.0, "hysteresis_thermal_guard": False},
            "traj_state": {},
            "lw_state": {},
            "db_state": {},
            "cal_state": {},
            "gs_state": {},
            "twin_state": {},
            "guards_state": {},
            "ac_state": {},
        }

        smartpi = SmartPI(
            hass=MagicMock(),
            cycle_min=10,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TestSmartPI_RampLowLearn",
            saved_state=saved_state,
        )

        assert smartpi._cycles_since_reset == 0, (
            f"Expected cycles_since_reset=0 (ramp active), got {smartpi._cycles_since_reset}"
        )
