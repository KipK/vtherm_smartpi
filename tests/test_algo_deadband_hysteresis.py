"""Tests for SmartPI deadband hysteresis behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import DEADBAND_HYSTERESIS

from helpers import force_smartpi_stable_mode


def _make_deadband_smartpi() -> SmartPI:
    """Return a SmartPI instance prepared for deadband tests."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPIDeadband",
        deadband_c=0.10,
        near_band_deg=0.0,
    )
    force_smartpi_stable_mode(smartpi)
    smartpi.est.learn_ok_count = 20
    smartpi.est.learn_ok_count_a = 20
    smartpi.est.learn_ok_count_b = 20
    smartpi.est.b = 0.002
    smartpi._cycles_since_reset = 10
    for _ in range(10):
        smartpi.est._b_hat_hist.append(smartpi.est.b)
    return smartpi


def test_reset_learning_clears_in_deadband() -> None:
    """Reset should clear the persisted deadband runtime state."""
    smartpi = _make_deadband_smartpi()
    smartpi.in_deadband = True

    smartpi.reset_learning()

    assert smartpi.in_deadband is False


def test_deadband_hysteresis_entry_and_exit() -> None:
    """Deadband should use different thresholds for entry and exit."""
    smartpi = _make_deadband_smartpi()
    exit_threshold = 0.10 + DEADBAND_HYSTERESIS

    smartpi.in_deadband = False
    smartpi.u_prev = 0.4
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.7,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_COOL,
    )
    assert smartpi.in_deadband is False

    smartpi._last_calculate_time = None
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.95,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is True

    smartpi._last_calculate_time = None
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.91,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is True

    smartpi._last_calculate_time = None
    smartpi.calculate(
        target_temp=20.0,
        current_temp=20.0 - exit_threshold - 0.005,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is False


def test_deadband_hysteresis_prevents_chattering() -> None:
    """Oscillations inside the hysteresis band should not toggle deadband state."""
    smartpi = _make_deadband_smartpi()

    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.95,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is True

    for current_temp in [19.91, 19.885, 19.9, 19.89, 19.905]:
        smartpi._last_calculate_time = None
        smartpi.calculate(
            target_temp=20.0,
            current_temp=current_temp,
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )
        assert smartpi.in_deadband is True


def test_deadband_hysteresis_zone_from_outside() -> None:
    """Crossing only the hysteresis band from outside must stay outside deadband."""
    smartpi = _make_deadband_smartpi()

    smartpi.in_deadband = False
    smartpi.u_prev = 0.4
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.7,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is False

    smartpi._last_calculate_time = None
    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.88,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )
    assert smartpi.in_deadband is False
