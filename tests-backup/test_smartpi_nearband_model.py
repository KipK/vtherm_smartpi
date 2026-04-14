# tests/test_smartpi_nearband_model.py
import pytest
from unittest.mock import MagicMock, patch
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT

class TestSmartPINearBandModel:

    def setup_method(self):
        # Create a basic SmartPI instance
        hass_mock = MagicMock()
        self.pi = SmartPI(
            hass_mock,
            10, # cycle_min
            0, # min_activation
            0, # min_deactivation
            "test_pi", # name
            max_on_percent=1.0,
            deadband_c=0.0,
            near_band_deg=0.5,
        )
        # Mock internal dependencies
        self.pi.dt_est = MagicMock()
        self.pi.est = MagicMock()
        
        # Default reliable state for tests
        self.pi.dt_est.deadtime_heat_reliable = True
        self.pi.dt_est.deadtime_heat_s = 300.0 # 5 min = 300s
        self.pi.dt_est.deadtime_cool_reliable = True 
        self.pi.dt_est.deadtime_cool_s = 600.0 # 10 min = 600s
        
        self.pi.est.learn_ok_count_a = 20
        self.pi.est.learn_ok_count_b = 20
        self.pi.est.a = 0.02 # deg/min/% -> per minute at 100%
        self.pi.est.b = 0.001 # deg/min/deltaT -> loss per minute per degree difference

    def testupdate_near_band_auto_model_based(self):
        """
        Test that nearband is correctly calculated from model parameters a and b.
        """
        # Context: delta T = 10 degrees (Tin=20, Text=10)
        current_temp = 20.0
        ext_temp = 10.0
        
        # Expected Slopes:
        # s_cool = b * deltaT = 0.001 * 10 = 0.01 deg/min
        # s_heat_net = a - s_cool = 0.02 - 0.01 = 0.01 deg/min
        # alpha = s_cool / s_heat_net = 0.01 / 0.01 = 1.0 (clamped [0.3, 1.0])
        
        # Expected Horizons (Seconds):
        # cycle = 600s
        # H_below = L_heat (300) + cycle/2 (300) = 600s = 10 min
        # H_above = L_cool (600) + cycle/2 (300) = 900s = 15 min
        
        # Expected Raw Calculation:
        # DB base = deadband_c = 0.0
        # s_heat_s = 0.01 / 60.0 = 0.0001666... deg/sec
        # NB_below_raw = 0.0 + 1.0 * (0.01/60) * 900 = 0.15 deg
        # NB_above_raw = 0.0 + 1.0 * (0.01/60) * 600 = 0.10 deg
        
        # Constraints:
        # NB_below >= DB + 0.1 = 0.1. Result 0.15.
        # NB_above >= DB + 0.1 = 0.1. Result 0.10.
        
        # Perform update
        # We need to mock _t_ext_current somewhere if used, or pass it? 
        # update_near_band_auto signature is (hvac_mode, current_temp, ext_temp) potentially?
        # WAIT: The current method in code is `update_near_band_auto(self, hvac_mode)`.
        # It reads temps from self vars ?? NO, original code used self attributes if stored, 
        # but actually the ORIGINAL code didn't use temps because it used slope history!
        # ===> I MUST MODIFY signature of update_near_band_auto to accept temps OR store them.
        # Let's assume I will modify it to accept current_temp and ext_temp.
        
        # For this test to run against *future* code, I will call it with arguments 
        # assuming I changed the signature, OR I will set internal vars if I choose that path.
        # Plan says: "Step 2: Model-based Slope: ... delta_T = current_temp - ext_temp"
        # So I need access to these temps.
        # I will modify the method to accept these arguments.
        
        self.pi.update_near_band_auto(VThermHvacMode_HEAT, current_temp, ext_temp)

        assert self.pi.deadband_mgr.near_band_source == "auto_model_aware"
        assert self.pi.deadband_mgr.near_band_below_deg == pytest.approx(0.15, abs=0.01)
        assert self.pi.deadband_mgr.near_band_above_deg == pytest.approx(0.10, abs=0.01)

    def testupdate_near_band_auto_fallback_unreliable_deadtime(self):
        """Test fallback when DeadTime is unreliable."""
        self.pi.dt_est.deadtime_heat_reliable = False
        
        # Call with mocked temps
        self.pi.update_near_band_auto(VThermHvacMode_HEAT, 20.0, 10.0)
        
        assert self.pi.deadband_mgr.near_band_source == "fallback_deadtime"
        assert self.pi.deadband_mgr.near_band_below_deg == 0.5 # Default
    
    def testupdate_near_band_auto_fallback_unreliable_model(self):
        """Test fallback when Model (a/b) is unreliable."""
        self.pi.est.learn_ok_count_a = 0 # Unreliable
        
        # Call with mocked temps
        self.pi.update_near_band_auto(VThermHvacMode_HEAT, 20.0, 10.0)
        
        assert self.pi.deadband_mgr.near_band_source == "fallback_model"
        # Checks defaults
        assert self.pi.deadband_mgr.near_band_below_deg == 0.5

    def test_horizon_use_cool_deadtime(self):
        """Test that L_cool is used for H_above when reliable."""
        # Setup specific values to distinguish
        # L_heat = 300 (5min), L_cool = 1200 (20min)
        self.pi.dt_est.deadtime_heat_s = 300.0
        self.pi.dt_est.deadtime_cool_s = 1200.0
        self.pi.dt_est.deadtime_cool_reliable = True
        
        # Slopes s_heat=0.01, s_cool=0.01 (alpha=1)
        # H_below = 300 + 300 = 600s (10min) -> Contribution 0.1 deg
        # H_above = 1200 + 300 = 1500s (25min) -> Contribution 0.25 deg
        
        # NB_below = 0.1
        # NB_above = 0.25 -> Clamped to NB_below? No, wait.
        # Plan Constraint: nb_above = clamp(nb_above_raw, ..., nb_below)
        # So nb_above CANNOT exceed nb_below.
        # If H_above is huge, it just caps at nb_below. 
        
        # To test L_cool usage, we need a case where nb_above < nb_below naturally?
        # nb_below = s_heat * H_below
        # nb_above = alpha * s_heat * H_above
        # If alpha is small (e.g. 0.3), nb_above might be smaller than nb_below even if H_above > H_below.
        
        # Let's tweak alpha.
        # Need s_cool small. s_cool = b * deltaT.
        # b=0.001. deltaT = 2.0 -> s_cool = 0.002.
        # a=0.02. s_heat_net = 0.02 - 0.002 = 0.018.
        # alpha = 0.002 / 0.018 = 0.11 -> Clamped to 0.3.
        
        # Recalculate:
        # s_heat_s = 0.018 / 60 = 0.0003
        # NB_below_raw = 0.0003 * 600 = 0.18
        # NB_above_raw = 0.3 * 0.0003 * 1500 (if L_cool used) = 0.135
        # If L_cool NOT used (L_heat=300 used):
        # NB_above_raw_bad = 0.3 * 0.0003 * 600 = 0.054
        
        # 0.135 vs 0.054 is distinguishable.
        
        self.pi.update_near_band_auto(VThermHvacMode_HEAT, 20.0, 18.0) # deltaT=2

        assert self.pi.deadband_mgr.near_band_below_deg == pytest.approx(0.45, abs=0.01)
        assert self.pi.deadband_mgr.near_band_above_deg == pytest.approx(0.10, abs=0.01)
