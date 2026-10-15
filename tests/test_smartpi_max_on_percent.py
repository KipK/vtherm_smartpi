"""max_on_percent is an actuator limit applied by Versatile Thermostat."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.valve_curve import TwoSlopeValveCurve


def _algo(max_on: float | None, valve_curve: bool) -> SmartPI:
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="max_on",
        max_on_percent=max_on,
        enable_valve_linearization=valve_curve,
    )


@pytest.mark.parametrize("max_on", [None, 0.3, 0.6, 1.0])
def test_identity_curve_caps_linear_demand_at_max_on(max_on) -> None:
    """Without linearization the cap is max_on_percent itself (switch and linear valve)."""
    algo = _algo(max_on, valve_curve=False)
    assert algo._linear_max_on() == max_on
    algo._apply_soft_constraints(0.95, 1.0, True)
    expected = 0.95 if max_on is None else min(0.95, max_on)
    assert algo.linear_on_percent == expected
    assert algo.on_percent == expected


def test_valve_curve_caps_actuator_command_at_max_on() -> None:
    """With the two-slope curve the actuator command reaches max_on_percent."""
    algo = _algo(0.6, valve_curve=True)
    linear_cap = TwoSlopeValveCurve().invert(0.6)
    assert algo._linear_max_on() == linear_cap
    assert linear_cap == pytest.approx(0.9058823529, abs=1e-9)

    algo._apply_soft_constraints(0.95, 1.0, True)
    assert algo.linear_on_percent == linear_cap
    assert algo.on_percent == pytest.approx(0.6, abs=1e-9)
    assert algo.on_percent == pytest.approx(algo.valve_curve.apply(linear_cap), abs=1e-9)

    # Below the cap the demand is unchanged.
    algo._apply_soft_constraints(0.5, 1.0, True)
    assert algo.linear_on_percent == 0.5


def test_valve_curve_cap_drives_calculate_output() -> None:
    """A full demand in valve mode ends at max_on_percent, not at apply(max_on_percent)."""
    algo = _algo(0.6, valve_curve=True)
    with (
        patch("custom_components.vtherm_smartpi.algo.time.monotonic") as monotonic,
        patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
    ):
        for minute in range(3):
            monotonic.return_value = 1000.0 + 60.0 * minute
            algo.calculate(
                target_temp=22.0,
                current_temp=12.0,
                ext_current_temp=0.0,
                slope=0.0,
                hvac_mode=VThermHvacMode_HEAT,
            )
    assert algo.on_percent <= 0.6 + 1e-9
    assert algo.on_percent == pytest.approx(0.6, abs=1e-9)
    assert algo.on_percent > algo.valve_curve.apply(0.6)


def test_calibration_receives_linear_cap() -> None:
    """Forced calibration clamps its linear output with the linear cap."""
    algo = _algo(0.6, valve_curve=True)
    algo.calibration_mgr.calculate = MagicMock(return_value=MagicMock(on_percent=None))
    algo._calculate_forced_calibration(20.0, 19.0, VThermHvacMode_HEAT)
    kwargs = algo.calibration_mgr.calculate.call_args.kwargs
    assert kwargs["max_on_percent"] == algo._linear_max_on()
