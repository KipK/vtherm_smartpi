import pytest
from unittest.mock import MagicMock
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
import time

class TestSmartPiHysteresisDeadtimeWindow:
    """Test suite for Smart-PI Deadtime window logic in Hysteresis mode."""

    def test_heat_episode_start_update_in_hysteresis(self):
        """Verify that _t_heat_episode_start is updated in HYSTERESIS phase when heating starts."""
        # Setup SmartPI
        hass = MagicMock()
        pi = SmartPI(hass, 10.0, 0, 0, "test_pi")
        
        # Mock the estimator to appear reliable so we can also check in_deadtime_window if we want
        pi.dt_est = MagicMock()
        pi.dt_est.deadtime_heat_reliable = True
        pi.dt_est.deadtime_heat_s = 600.0
        pi.dt_est.deadtime_cool_reliable = True # Ensure we don't switch phase due to this
        pi.dt_est.deadtime_cool_s = 600.0  # Required for update_near_band_auto
        
        # Force HYSTERESIS phase by clearing history
        pi.est = MagicMock()
        pi.est.a_meas_hist = []  # Empty history
        pi.est.b_meas_hist = []
        pi.est.learn_ok_count_a = 0  # Required for update_near_band_auto
        pi.est.a = 0.0  # Required for update_near_band_auto
        
        # Ensure Phase is Hysteresis
        # We need to rely on the property logic. 
        # SmartPI.phase checks self.est.learn_ok_count or similar?
        # Let's check prop_algo_smartpi.py for phase property definition if needed.
        # But assuming empty history makes it Hysteresis as per default.
        
        # 1. Start heating
        # Target 20, Current 19 -> ON
        pi.calculate(20.0, 19.0, 10.0, 0.0, VThermHvacMode_HEAT)
        
        # Verify we are in Hysteresis
        assert pi.phase == SmartPIPhase.HYSTERESIS
        
        # Verify output is ON
        assert pi.on_percent > 0.0
        
        # FAILURE EXPECTED HERE:
        # _t_heat_episode_start should be set
        assert pi._t_heat_episode_start is not None, "_t_heat_episode_start should be set when heating starts"

    def test_cool_episode_start_update_in_hysteresis(self):
        """Verify that _t_cool_episode_start (to be added) is updated in HYSTERESIS phase when cooling starts."""
        from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL

        # Setup SmartPI
        hass = MagicMock()
        pi = SmartPI(hass, 10.0, 0, 0, "test_pi")
        
        # Mock the estimator
        pi.dt_est = MagicMock()
        pi.dt_est.deadtime_cool_reliable = True
        pi.dt_est.deadtime_cool_s = 600.0
        pi.dt_est.deadtime_heat_reliable = True 
        
        # Force HYSTERESIS phase
        pi.est = MagicMock()
        pi.est.a_meas_hist = [] 
        pi.est.b_meas_hist = []
        
        # 1. Start Cooling
        # Target 20, Current 21 -> ON (Cooling)
        pi.calculate(20.0, 21.0, 10.0, 0.0, VThermHvacMode_COOL)
        
        assert pi.phase == SmartPIPhase.HYSTERESIS
        assert pi.on_percent > 0.0
        
        # Expectation: We need a way to track cooling start
        # Currently _t_cool_episode_start likely doesn't exist, so this test serves as a requirement verification
        # accessing it will raise AttributeError if not impl, or None if logic missing
        
        has_cool_start = hasattr(pi, '_t_cool_episode_start')
        # If it doesn't exist yet, we fail this check effectively saying "Feature missing"
        assert has_cool_start, "SmartPI should have _t_cool_episode_start"
        
        assert pi._t_cool_episode_start is not None, "_t_cool_episode_start should be set when cooling starts"


HYST_ON = 0.3
HYST_OFF = 0.5


@pytest.mark.parametrize(
    ("current_temp", "expected_out", "expected_state"),
    [
        (19.7, 1.0, "on"),     # SP - hyst_on
        (19.8, None, "band"),
        (20.4, None, "band"),
        (20.5, 0.0, "off"),    # SP + hyst_off
    ],
)
def test_calculate_hysteresis_heat_thresholds(current_temp, expected_out, expected_state):
    """HEAT: ON at SP - hyst_on, OFF at SP + hyst_off."""
    from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController

    ctl = SmartPIController("test_hyst_heat")
    out = ctl.calculate_hysteresis(20.0, current_temp, VThermHvacMode_HEAT, HYST_OFF, HYST_ON)

    assert out == expected_out
    assert ctl.hysteresis_state == expected_state


@pytest.mark.parametrize(
    ("current_temp", "expected_out", "expected_state"),
    [
        (20.3, 1.0, "on"),     # SP + hyst_on
        (20.2, None, "band"),
        (19.6, None, "band"),
        (19.5, 0.0, "off"),    # SP - hyst_off
    ],
)
def test_calculate_hysteresis_cool_mirrors_heat(current_temp, expected_out, expected_state):
    """COOL mirrors HEAT: ON at SP + hyst_on, OFF at SP - hyst_off."""
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL
    from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController

    ctl = SmartPIController("test_hyst_cool")
    out = ctl.calculate_hysteresis(20.0, current_temp, VThermHvacMode_COOL, HYST_OFF, HYST_ON)

    assert out == expected_out
    assert ctl.hysteresis_state == expected_state


def test_smartpi_cool_hysteresis_uses_mirrored_thresholds():
    """SmartPI wiring: COOL starts at SP + hysteresis_on and stops at SP - hysteresis_off."""
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL

    pi = SmartPI(MagicMock(), 10.0, 0, 0, "test_pi_cool_hyst", hysteresis_on=HYST_ON, hysteresis_off=HYST_OFF)

    pi.calculate(20.0, 20.3, 30.0, 0.0, VThermHvacMode_COOL)
    assert pi.phase == SmartPIPhase.HYSTERESIS
    assert pi.ctl.hysteresis_state == "on"
    assert pi.on_percent == 1.0

    pi.calculate(20.0, 19.6, 30.0, 0.0, VThermHvacMode_COOL)
    assert pi.ctl.hysteresis_state == "band"
    assert pi.on_percent == 1.0

    pi.calculate(20.0, 19.5, 30.0, 0.0, VThermHvacMode_COOL)
    assert pi.ctl.hysteresis_state == "off"
    assert pi.on_percent == 0.0
