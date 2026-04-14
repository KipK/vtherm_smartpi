"""Tests for SmartPI setpoint boost behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)

from helpers import force_smartpi_stable_mode


def _make_boost_smartpi(name: str) -> SmartPI:
    """Return a SmartPI instance prepared for boost tests."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name=name,
        debug_mode=True,
    )
    force_smartpi_stable_mode(smartpi)
    return smartpi


def test_setpoint_boost_activates_on_setpoint_increase() -> None:
    """A significant HEAT setpoint increase should activate boost."""
    smartpi = _make_boost_smartpi("TestSmartPI_Boost")

    smartpi.calculate(
        target_temp=18.0,
        current_temp=18.0,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.setpoint_boost_active is False
    assert smartpi.sp_mgr.prev_setpoint_for_boost == 18.0

    smartpi._last_calculate_time = None
    smartpi.calculate(
        target_temp=19.0,
        current_temp=18.0,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.setpoint_boost_active is True
    assert smartpi.sp_mgr.prev_setpoint_for_boost == 19.0
    assert smartpi.get_debug_diagnostics()["debug"]["setpoint_boost_active"] is True


def test_setpoint_boost_deactivates_when_error_small() -> None:
    """Boost should clear once the remaining error becomes small."""
    smartpi = _make_boost_smartpi("TestSmartPI_BoostDeact")

    smartpi.sp_mgr.boost_active = True
    smartpi.sp_mgr.prev_setpoint_for_boost = 20.0
    smartpi.u_prev = 0.5

    smartpi.calculate(
        target_temp=20.0,
        current_temp=19.85,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.setpoint_boost_active is False


def test_setpoint_boost_activates_on_decrease_in_heat_mode() -> None:
    """A significant HEAT setpoint decrease should also activate boost logic."""
    smartpi = _make_boost_smartpi("TestSmartPI_BoostDecrease")

    smartpi.sp_mgr.prev_setpoint_for_boost = 22.0
    smartpi.sp_mgr.boost_active = False
    smartpi.u_prev = 0.5

    smartpi.calculate(
        target_temp=20.0,
        current_temp=21.0,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.setpoint_boost_active is True
    assert smartpi.sp_mgr.prev_setpoint_for_boost == 20.0


def test_setpoint_boost_persisted() -> None:
    """Boost state should survive a save/load round-trip."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI_BoostPersist1",
    )
    smartpi1.sp_mgr.boost_active = True
    smartpi1.sp_mgr.prev_setpoint_for_boost = 21.0

    saved = smartpi1.save_state()

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI_BoostPersist2",
        saved_state=saved,
    )

    assert smartpi2.setpoint_boost_active is True
    assert smartpi2.sp_mgr.prev_setpoint_for_boost == 21.0


def test_setpoint_boost_cleared_on_reset() -> None:
    """Reset should clear boost runtime state."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI_BoostReset",
    )
    smartpi.sp_mgr.boost_active = True
    smartpi.sp_mgr.prev_setpoint_for_boost = 21.0

    smartpi.reset_learning()

    assert smartpi.setpoint_boost_active is False
    assert smartpi.sp_mgr.prev_setpoint_for_boost is None


def test_setpoint_boost_cool_mode() -> None:
    """A significant COOL setpoint decrease should activate boost."""
    smartpi = _make_boost_smartpi("TestSmartPI_BoostCool")

    smartpi.sp_mgr.prev_setpoint_for_boost = 25.0
    smartpi.sp_mgr.boost_active = False
    smartpi.u_prev = 0.3

    smartpi.calculate(
        target_temp=22.0,
        current_temp=26.0,
        ext_current_temp=30.0,
        slope=0,
        hvac_mode=VThermHvacMode_COOL,
    )

    assert smartpi.setpoint_boost_active is True
    assert smartpi.sp_mgr.prev_setpoint_for_boost == 22.0
