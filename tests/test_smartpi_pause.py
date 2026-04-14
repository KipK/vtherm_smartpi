
import pytest
from unittest.mock import MagicMock, patch
import time
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.const import LEARNING_PAUSE_RESUME_MIN
from helpers import force_smartpi_stable_mode

class MockVTherm:
    def __init__(self):
        self.name = "test_vtherm"

def test_start_pause_logic():
    """Test learning pause behavior: frozen 1 cycle at reboot, then paused on OFF resume."""
    vtherm = MockVTherm()
    pi = SmartPI(vtherm, 1, 1, 1, 1) # cycle_min=1 min
    force_smartpi_stable_mode(pi)

    # 1. Initial State (Startup)
    assert pi._last_calculate_time is None
    assert pi.learning_resume_ts is None
    assert pi._startup_grace_period is True

    # 2. First calculation (Reboot) -> SHOULD freeze learning for 1 cycle
    now = time.monotonic()
    pi.calculate(
        target_temp=20.0,
        current_temp=19.0,
        ext_current_temp=10.0,
        slope=0.0,
        hvac_mode=VThermHvacMode_HEAT
    )

    # Startup grace period consumed
    assert pi._startup_grace_period is False
    # Learning frozen for exactly cycle_min (1 min) after reboot
    assert pi.learning_resume_ts is not None
    expected_freeze = now + 1 * 60.0  # cycle_min * 60
    assert abs(pi.learning_resume_ts - expected_freeze) < 2.0

    # 3. Simulate OFF/Resume cycle
    pi.calculate(
         target_temp=20.0,
        current_temp=19.0,
        ext_current_temp=10.0,
        slope=0.0,
        hvac_mode=VThermHvacMode_OFF  # OFF
    )
    assert pi._last_calculate_time is None

    # Resume (Second "first run") -> SHOULD PAUSE for LEARNING_PAUSE_RESUME_MIN
    now2 = time.monotonic()
    pi.calculate(
        target_temp=20.0,
        current_temp=19.0,
        ext_current_temp=10.0,
        slope=0.0,
        hvac_mode=VThermHvacMode_HEAT
    )

    # Check pause timestamp set
    assert pi.learning_resume_ts is not None
    expected_resume = now2 + LEARNING_PAUSE_RESUME_MIN * 60.0
    assert abs(pi.learning_resume_ts - expected_resume) < 5.0

    # 4. Simulate cycle ongoing (should be skipped due to learning pause)
    pi.est.learn_skip_count = 0

    pi.update_learning(
        dt_min=10.0,
        current_temp=19.5,
        ext_temp=10.0,
        u_active=0.5,
        setpoint_changed=False
    )

    assert pi.est.learn_skip_count == 1
    assert "skip" in pi.est.learn_last_reason and (
        "resume" in pi.est.learn_last_reason or "governance" in pi.est.learn_last_reason
    )

    # 5. Simulate cycle completion AFTER pause window
    pi.learn_win.set_learning_resume_ts(time.monotonic() - 1.0)  # Resume time passed

    pi.update_learning(
        dt_min=10.0,
        current_temp=19.5,
        ext_temp=10.0,
        u_active=0.5,
        setpoint_changed=False
    )

    assert "resume cool-down" not in pi.est.learn_last_reason
