import pytest
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from unittest.mock import MagicMock
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT

class TestSmartPiHysteresisLearning:
    """Test suite for Smart-PI Deadtime learning in Hysteresis mode."""

    def setup_method(self):
        self.est = DeadTimeEstimator()

    def test_hysteresis_trigger(self):
        """Test that power rising triggers learning."""
        now = 0.0
        # History of OFF
        for i in range(15):
             self.est.update(now=now, tin=19.0, sp=20.0, u_applied=0.0, is_hysteresis=True)
             now += 60.0
             
        # Power ON
        self.est.update(now=now, tin=19.0, sp=20.0, u_applied=1.0, is_hysteresis=True)
        
        # New assertion: State check
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        assert self.est.heat_start_time == now

    def test_hysteresis_learning_short_pulse(self):
        """Test that a 10 min pulse is enough to learn if temp rises."""
        now = 0.0
        # OFF
        for i in range(15):
             self.est.update(now=now, tin=19.0, sp=20.0, u_applied=0.0, is_hysteresis=True)
             now += 60.0
             
        # ON (Start)
        self.est.update(now=now, tin=19.0, sp=21.0, u_applied=1.0, is_hysteresis=True)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # Pulse ON for 10 mins, temp rises
        for i in range(1, 11):
            now += 60.0
            # Temp stays flat for 3 mins, then rises 0.02 per min. 
            # At i=6 (now=360), delta=0.06 -> Trigger!
            if i <= 3:
                tin = 19.0
            else:
                tin = 19.0 + (i - 3) * 0.02
                
            self.est.update(now=now, tin=tin, sp=21.0, u_applied=1.0, is_hysteresis=True)
            
        # At i=6 (6 mins), trigger is hit.
        # Inflection point was at i=3 (3 mins = 180s).
        # Check current state
        assert self.est.state == "HEATING"
        assert self.est.deadtime_heat_s == 180.0
        assert self.est.deadtime_heat_reliable is True


class TestSmartPiIntegration:
    """Integration tests for SmartPI -> DeadTimeEstimator."""
    
    def test_smartpi_passes_is_hysteresis(self):
        """Verify SmartPI passes is_hysteresis=True when in HYSTERESIS phase."""
        # Setup SmartPI
        hass = MagicMock()
        # Correct args: hass, cycle_min, min_on, min_off, name
        pi = SmartPI(hass, 10.0, 0, 0, "test_pi")
        
        # Mock the estimator to inspect calls
        pi.dt_est = MagicMock()
        pi.dt_est.deadtime_heat_reliable = False # Ensure Hysteresis phase
        pi.dt_est.deadtime_cool_reliable = True
        pi.dt_est.deadtime_cool_s = 120.0
        # Override phase property? SmartPI.phase checks history size.
        # History is empty by default -> HYSTERESIS.
        assert pi.phase == SmartPIPhase.HYSTERESIS
        
        # Call calculate
        pi.calculate(20.0, 19.0, 10.0, 0.0, VThermHvacMode_HEAT)
        
        # Verify update was called with is_hysteresis=True
        pi.dt_est.update.assert_called()
        call_kwargs = pi.dt_est.update.call_args.kwargs
        assert call_kwargs.get('is_hysteresis') is True, "SmartPI should pass is_hysteresis=True in Hysteresis phase"

    def test_smartpi_passes_is_hysteresis_false_in_stable(self):
        """Verify SmartPI passes is_hysteresis=False when in STABLE phase."""
        hass = MagicMock()
        pi = SmartPI(hass, 10.0, 0, 0, "test_pi")
        import time
        pi.calibration_mgr.last_calibration_time = time.time()
        
        pi.dt_est = MagicMock()
        pi.dt_est.deadtime_heat_s = 120.0 # Set valid float
        pi.dt_est.deadtime_heat_reliable = True # Set valid bool
        pi.dt_est.deadtime_cool_reliable = True
        pi.dt_est.deadtime_cool_s = 120.0

        # Force STABLE phase by populating history
        # We need AB_HISTORY_SIZE (31) measurements
        pi.est.a_meas_hist = [0.1] * 35
        pi.est.b_meas_hist = [0.1] * 35
        
        assert pi.phase == SmartPIPhase.STABLE
        
        # Call calculate
        pi.calculate(20.0, 19.0, 10.0, 0.0, VThermHvacMode_HEAT)
        
        # Verify update was called
        pi.dt_est.update.assert_called()
        call_kwargs = pi.dt_est.update.call_args.kwargs
        assert call_kwargs.get('is_hysteresis', False) is False, "SmartPI should NOT pass is_hysteresis=True in Stable phase"
