import pytest
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator

class TestDeadTimeEstimatorNew:
    def setup_method(self):
        self.est = DeadTimeEstimator()

    def test_nominal_heat_detection(self):
        now = 1000.0
        # Condition initial: OFF
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # Start Heat (Power 0 -> 1)
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        assert self.est.heat_start_time == now
        assert self.est.heat_start_temp == 20.0
        
        # Wait, temp constant
        now += 300
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # Temp rises 0.05
        now += 60
        self.est.update(now, 20.06, 21.0, 1.0)
        # 20.06 - 20.0 = 0.06 >= 0.05
        assert self.est.state == "HEATING"
        # start at 1060. Inflection at 1360. Diff = 300.
        assert self.est.deadtime_heat_s == 300.0
        assert self.est.deadtime_heat_reliable is True

    def test_nominal_cool_detection(self):
        now = 1000.0
        # Initial: Heating
        self.est.update(now, 20.0, 21.0, 0.0) # OFF first
        now += 1000
        self.est.update(now, 20.0, 21.0, 1.0) # ON
        # Wait enough for min power/state
        
        # Force state to HEATING (detected or not)
        self.est.state = "HEATING" 
        self.est.last_power = 1.0 # Simulate prev power
        
        # Stop Heat (Power 1 -> 0)
        now += 60
        stop_time = now
        # Prev power 1.0 > 0.8
        self.est.update(now, 22.0, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.cool_start_time == now
        assert self.est.cool_peak_temp == 22.0
        
        # Inertia: Temp rises a bit
        now += 60
        self.est.update(now, 22.1, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.cool_peak_temp == 22.1
        
        # Temp Drops 0.05 from peak (22.1 - 0.05 = 22.05)
        now += 300
        self.est.update(now, 22.04, 21.0, 0.0)
        assert self.est.state == "COOLING"
        # start at 2060. Peak/inflection at 2120. Diff = 60.
        assert self.est.deadtime_cool_s == 60.0
        assert self.est.deadtime_cool_reliable is True

    def test_cooling_persistence(self):
        """Test that COOLING state persists even with subsequent updates at 0 power"""
        now = 1000.0
        self.est.state = "COOLING"
        self.est.last_power = 0.0
        
        # Update with 0 power
        now += 60
        self.est.update(now, 20.0, 21.0, 0.0)
        assert self.est.state == "COOLING"
        
        # Update again
        now += 60
        self.est.update(now, 19.9, 21.0, 0.0)
        assert self.est.state == "COOLING"

    def test_power_threshold_check(self):
        now = 1000.0
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # Weak Start (Power 0 -> 0.5)
        now += 60
        self.est.update(now, 20.0, 21.0, 0.5)
        assert self.est.state == "HEATING" # Active but ignored
        # Should not be WAITING_HEAT_RESPONSE
        assert self.est.heat_start_time is None

    def test_min_off_time_check(self):
        now = 1000.0
        # OFF
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # ON
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        # Should accept (first run, last_stop_time is None)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # OFF (shortly after)
        now += 60
        self.est.update(now, 20.0, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.last_stop_time == now
        stop_time = now
        
        # ON again (too soon, 1 min later)
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        # Should Ignore Heat Start because (now - stop_time) < 600
        assert self.est.state == "HEATING" # Ignored
        assert self.est.heat_start_time < stop_time 

    def test_timeout_heat(self):
        now = 0.0
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # ON
        now += 60
        start_time = now
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # Forward > 4h (14400s) - timeout_seconds is 14400.0
        now += 14401
        self.est.update(now, 20.04, 21.0, 1.0) # Temp didn't rise enough (0.04 < 0.05)
        
        assert self.est.state == "HEATING"
        assert self.est.deadtime_heat_s is None
