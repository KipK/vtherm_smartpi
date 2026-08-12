"""Deadband-safe output shaping for SmartPI."""
from __future__ import annotations

from .const import DEADBAND_HYSTERESIS


def deadband_proportional_error(
    *,
    error_p: float,
    deadband_c: float,
    freeze_deadband: bool,
    deadband_allow_p: bool,
) -> tuple[float, str]:
    """Return the proportional error to use for PI output calculation."""
    db_size = max(float(deadband_c), 0.0)
    hysteresis = max(float(DEADBAND_HYSTERESIS), 0.0)
    quiet = max(db_size - hysteresis, 0.0)

    if freeze_deadband and not deadband_allow_p:
        return 0.0, "deadband_frozen"

    threshold = quiet if deadband_allow_p else db_size
    abs_error = abs(error_p)
    if abs_error <= threshold:
        mode = "deadband_quiet" if freeze_deadband else "off"
        return 0.0, mode

    sign = 1.0 if error_p >= 0.0 else -1.0
    mode = "deadband_edge" if freeze_deadband else "deadzone_edge"
    return sign * (abs_error - threshold), mode
