"""Versatile Thermostat calls SmartPI.calculate positionally from recalculate()."""
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
)


def _make_algo() -> SmartPI:
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="vt_contract",
    )


@pytest.mark.parametrize("hvac_mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_recalculate_positional_order_is_accepted(hvac_mode):
    """ThermostatProp.recalculate passes (target, current, ext, slope, hvac_mode)."""
    algo = _make_algo()

    algo.calculate(
        20.0,
        19.0,
        5.0,
        0.4,
        hvac_mode,
        power_shedding=False,
        off_reason=None,
    )

    assert algo._last_temperature_slope_h == pytest.approx(0.4)
    assert str(algo._last_hvac_mode) == str(hvac_mode)


def test_recalculate_positional_off_is_seen_as_off():
    """An OFF thermostat called through recalculate reaches the OFF path."""
    algo = _make_algo()

    algo.calculate(20.0, 19.0, 5.0, None, VThermHvacMode_OFF, power_shedding=False, off_reason=None)

    assert algo.on_percent == 0.0
    assert algo._last_calculate_time is None
