"""Tests for SmartPI static valve curve linearization."""

from __future__ import annotations

import pytest

from custom_components.vtherm_smartpi.smartpi.valve_curve import (
    IdentityValveCurve,
    TwoSlopeValveCurve,
    ValveCurveParams,
)


def test_identity_valve_curve_keeps_unit_values() -> None:
    """Identity curve must leave linear actuators unchanged."""
    curve = IdentityValveCurve()

    assert curve.apply(0.42) == pytest.approx(0.42)
    assert curve.invert(0.42) == pytest.approx(0.42)
    assert curve.apply(-1.0) == pytest.approx(0.0)
    assert curve.invert(2.0) == pytest.approx(1.0)


def test_two_slope_valve_curve_maps_and_inverts_breakpoints() -> None:
    """Two-slope curve must preserve the configured breakpoints."""
    params = ValveCurveParams(
        min_valve=7.0,
        knee_demand=80.0,
        knee_valve=15.0,
        max_valve=100.0,
    )
    curve = TwoSlopeValveCurve(params)

    assert curve.apply(0.0) == pytest.approx(0.0)
    assert curve.apply(0.8) == pytest.approx(0.15)
    assert curve.apply(1.0) == pytest.approx(1.0)
    assert curve.invert(0.15) == pytest.approx(0.8)
    assert curve.invert(1.0) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "params",
    [
        {"min_valve": 15.0, "knee_demand": 80.0, "knee_valve": 15.0, "max_valve": 100.0},
        {"min_valve": 7.0, "knee_demand": 0.0, "knee_valve": 15.0, "max_valve": 100.0},
        {"min_valve": 7.0, "knee_demand": 80.0, "knee_valve": 101.0, "max_valve": 100.0},
    ],
)
def test_valve_curve_params_reject_ambiguous_curves(params: dict[str, float]) -> None:
    """Invalid breakpoint ordering must be rejected."""
    with pytest.raises(ValueError):
        ValveCurveParams(**params)
