"""Tests for SmartPI OFF-mode behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_OFF


def test_integral_reset_on_off_mode() -> None:
    """OFF mode should force 0% output and reset dt tracking."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI_ResetOFF",
    )

    smartpi.integral = 5.0
    smartpi.u_prev = 0.5

    smartpi.calculate(
        target_temp=20.0,
        current_temp=18.0,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_OFF,
    )

    assert getattr(smartpi.ctl, "integral", smartpi.integral) == 5.0
    assert smartpi.on_percent == 0.0
    assert smartpi._last_calculate_time is None
