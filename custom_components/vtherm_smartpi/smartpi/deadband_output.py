"""Deadband-safe output shaping for SmartPI."""
from __future__ import annotations

from .const import DEADBAND_HYSTERESIS, ENABLE_PROPORTIONAL_DEADZONE


def deadband_proportional_error(
    *,
    error_p: float,
    deadband_c: float,
    freeze_deadband: bool,
    deadband_allow_p: bool,
) -> tuple[float, str]:
    """Return the proportional error to use for PI output calculation."""
    db_size = max(deadband_c, 0.0)

    if freeze_deadband:
        if not deadband_allow_p:
            return 0.0, "deadband_frozen"

        quiet = max(db_size - max(DEADBAND_HYSTERESIS, 0.0), 0.0)
        abs_error = abs(error_p)
        if abs_error <= quiet:
            return 0.0, "deadband_quiet"

        sign = 1.0 if error_p >= 0.0 else -1.0
        return sign * (abs_error - quiet), "deadband_edge"

    if not ENABLE_PROPORTIONAL_DEADZONE:
        return error_p, "raw"

    abs_error = abs(error_p)
    if abs_error <= db_size:
        return 0.0, "off"

    sign = 1.0 if error_p >= 0.0 else -1.0
    return sign * (abs_error - db_size), "deadzone_edge"
