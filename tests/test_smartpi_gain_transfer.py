"""Tests for the bumpless gain transfer."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceDecision


def test_any_ki_change_rescales_the_integral() -> None:
    """Ki * I is kept on every Ki change, however small (bumpless transfer)."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="gain-rescale",
    )
    algo.ctl.integral = 1.0
    old_ki = algo.gain_scheduler.ki
    new_ki = old_ki + 5e-7  # below the former 1e-6 threshold

    def calculate(**_kwargs):
        algo.gain_scheduler._ki = new_ki

    algo.gain_scheduler.calculate = calculate
    algo._apply_gains_and_ff(
        GovernanceDecision.ADAPT_ON,
        target_temp_ff=20.0,
        ext_current_temp=5.0,
        hvac_mode=VThermHvacMode_HEAT,
        error=0.1,
        e_p=0.1,
    )

    assert algo.gain_scheduler.ki == new_ki
    assert new_ki * algo.ctl.integral == pytest.approx(old_ki * 1.0, rel=1e-12)
