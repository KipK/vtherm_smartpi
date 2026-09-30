"""Pure constant-power propagation for the signed 1R1C thermal model."""

from math import expm1


def propagate_1r1c(
    *,
    temperature: float,
    external_temperature: float,
    a: float,
    b: float,
    power: float,
    duration_min: float,
    bias: float = 0.0,
) -> float:
    """Propagate temperature over minutes with constant power and bias.

    Callers supply finite inputs, b > 0, and duration_min >= 0. The signed
    coefficient a is positive for heating and negative for cooling. Bias has
    the same temperature-per-minute units as a * power.
    """
    if duration_min == 0.0:
        return temperature
    decay_fraction = -expm1(-b * duration_min)
    equilibrium_offset = (a * power + bias) / b
    return temperature + (
        external_temperature - temperature + equilibrium_offset
    ) * decay_fraction
