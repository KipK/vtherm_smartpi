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


def build_valve_curve(
    enabled: bool,
    params: ValveCurveParams | None = None,
) -> ValveCurveProtocol:
    """Build the static actuator linearization curve."""
    if enabled:
        return TwoSlopeValveCurve(params or VALVE_CURVE_DEFAULTS)
    return IdentityValveCurve()


def apply_valve_activation_floor(
    curve: ValveCurveProtocol,
    linear_demand: float,
    cycle_min: float,
    minimal_activation_delay: int,
) -> tuple[float, bool]:
    """Raise a valve demand when scheduler timing would otherwise close it."""
    demand = clamp_unit(linear_demand)
    if demand <= 0.0:
        return demand, False

    cycle_sec = max(float(cycle_min) * 60.0, 0.0)
    min_on_sec = max(float(minimal_activation_delay), 0.0)
    if min_on_sec <= 0.0 or cycle_sec <= 0.0 or min_on_sec >= cycle_sec:
        return demand, False

    actuator_demand = curve.apply(demand)
    min_actuator_demand = min_on_sec / cycle_sec
    if actuator_demand + 1e-9 >= min_actuator_demand:
        return demand, False

    return max(demand, curve.invert(min_actuator_demand)), True
