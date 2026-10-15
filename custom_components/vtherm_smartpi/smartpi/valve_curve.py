"""Static valve actuator linearization for SmartPI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


def clamp_unit(value: float) -> float:
    """Clamp one value to [0, 1]."""
    return min(max(float(value), 0.0), 1.0)


@dataclass(slots=True, frozen=True)
class ValveCurveParams:
    """Parameters of the two-slope valve characteristic in percent units."""

    min_valve: float
    knee_demand: float
    knee_valve: float
    max_valve: float

    def __post_init__(self) -> None:
        """Reject parameters that would make the curve ambiguous."""
        if not (0.0 <= self.min_valve < self.knee_valve < self.max_valve <= 100.0):
            raise ValueError("invalid valve curve valve breakpoints")
        if not (0.0 < self.knee_demand < 100.0):
            raise ValueError("invalid valve curve demand breakpoint")


# Versatile Thermostat applies valve positions in whole percents
# (round(on_percent * 100) before the dpercent and period filters).
VALVE_POSITION_STEP = 0.01

VALVE_CURVE_DEFAULTS = ValveCurveParams(
    min_valve=7.0,
    knee_demand=80.0,
    knee_valve=15.0,
    max_valve=100.0,
)


class ValveCurveProtocol(Protocol):
    """Common interface for actuator linearization curves."""

    @property
    def params(self) -> ValveCurveParams | None:
        """Return curve parameters when the actuator needs linearization."""

    def apply(self, demand_unit: float) -> float:
        """Convert linear demand in [0, 1] to actuator command in [0, 1]."""

    def invert(self, actuator_unit: float) -> float:
        """Convert actuator feedback in [0, 1] to linear demand in [0, 1]."""


class IdentityValveCurve:
    """Identity mapping for linear actuators."""

    params = None

    def apply(self, demand_unit: float) -> float:
        """Return the demand unchanged."""
        return clamp_unit(demand_unit)

    def invert(self, actuator_unit: float) -> float:
        """Return the actuator value unchanged."""
        return clamp_unit(actuator_unit)


class TwoSlopeValveCurve:
    """Two-slope static nonlinearity for TRV-like actuators."""

    def __init__(self, params: ValveCurveParams = VALVE_CURVE_DEFAULTS) -> None:
        """Initialize the curve with validated parameters."""
        self._params = params

    @property
    def params(self) -> ValveCurveParams:
        """Return current curve parameters."""
        return self._params

    def apply(self, demand_unit: float) -> float:
        """Map linear model demand to valve position."""
        demand = clamp_unit(demand_unit) * 100.0
        p = self._params
        if demand <= 0.0:
            return 0.0
        if demand <= p.knee_demand:
            valve = p.min_valve + (demand / p.knee_demand) * (
                p.knee_valve - p.min_valve
            )
        else:
            valve = p.knee_valve + (
                (demand - p.knee_demand) / (100.0 - p.knee_demand)
            ) * (p.max_valve - p.knee_valve)
        return clamp_unit(valve / 100.0)

    def invert(self, actuator_unit: float) -> float:
        """Map valve position to equivalent linear model demand."""
        valve = clamp_unit(actuator_unit) * 100.0
        p = self._params
        if valve < p.min_valve:
            return 0.0
        if valve <= p.knee_valve:
            demand = (valve - p.min_valve) * p.knee_demand / (
                p.knee_valve - p.min_valve
            )
        else:
            demand = p.knee_demand + (valve - p.knee_valve) * (
                100.0 - p.knee_demand
            ) / (p.max_valve - p.knee_valve)
        return clamp_unit(demand / 100.0)


def applied_output_deviates(
    curve: ValveCurveProtocol,
    valve_mode: bool,
    requested: float,
    applied: float,
    epsilon: float,
) -> bool:
    """Return True when the applied linear output is not the rendering of the request.

    A switch output must match the request within epsilon. A valve position is
    the actuator request rounded to VALVE_POSITION_STEP, so the request renders
    to the applied position whenever its actuator image lies within half a step
    of it, i.e. inside the linear image of that half-step interval (the curve is
    monotonic), widened by epsilon.
    """
    if not valve_mode:
        return abs(requested - applied) > epsilon
    position = curve.apply(applied)
    half_step = 0.5 * VALVE_POSITION_STEP
    low = curve.invert(position - half_step) - epsilon
    high = curve.invert(position + half_step) + epsilon
    return not low <= requested <= high


def build_valve_curve(
    enabled: bool,
    params: ValveCurveParams | None = None,
) -> ValveCurveProtocol:
    """Build the static actuator linearization curve."""
    if enabled:
        return TwoSlopeValveCurve(params or VALVE_CURVE_DEFAULTS)
    return IdentityValveCurve()

