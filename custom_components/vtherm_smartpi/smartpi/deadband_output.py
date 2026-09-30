"""Deadband-safe output shaping for SmartPI."""
from __future__ import annotations

from dataclasses import dataclass

from .const import DEADBAND_EDGE_PERSISTENCE, DEADBAND_HYSTERESIS


@dataclass(frozen=True, slots=True)
class ProportionalState:
    """Inputs needed to project one P evaluation without advancing the controller."""

    deadband_c: float
    freeze_deadband: bool
    deadband_allow_p: bool
    edge_count: int
    edge_sign: float | None

    @property
    def threshold(self) -> float:
        return proportional_threshold(self.deadband_c, self.deadband_allow_p)

    def project(self, error_p: float) -> tuple[float, str, int, float | None]:
        """Return the P error, mode and next persistence state for one sample."""
        shaped, mode = deadband_proportional_error(
            error_p=error_p,
            deadband_c=self.deadband_c,
            freeze_deadband=self.freeze_deadband,
            deadband_allow_p=self.deadband_allow_p,
        )
        if mode not in {"deadband_edge", "deadzone_edge"}:
            return shaped, mode, 0, None
        sign = 1.0 if shaped >= 0.0 else -1.0
        count = self.edge_count + 1 if self.edge_sign == sign else 1
        if mode == "deadband_edge" and count < DEADBAND_EDGE_PERSISTENCE:
            return 0.0, "deadband_edge_pending", count, sign
        return shaped, mode, count, sign


def proportional_threshold(deadband_c: float, deadband_allow_p: bool) -> float:
    """Return the shared deadzone edge for active or damped P."""
    db_size = max(float(deadband_c), 0.0)
    hysteresis = max(float(DEADBAND_HYSTERESIS), 0.0)
    return max(db_size - hysteresis, 0.0) if deadband_allow_p else db_size


def deadband_proportional_error(
    *,
    error_p: float,
    deadband_c: float,
    freeze_deadband: bool,
    deadband_allow_p: bool,
) -> tuple[float, str]:
    """Return the proportional error to use for PI output calculation."""
    if freeze_deadband and not deadband_allow_p:
        return 0.0, "deadband_frozen"

    threshold = proportional_threshold(deadband_c, deadband_allow_p)
    abs_error = abs(error_p)
    if abs_error <= threshold:
        mode = "deadband_quiet" if freeze_deadband else "off"
        return 0.0, mode

    sign = 1.0 if error_p >= 0.0 else -1.0
    mode = "deadband_edge" if freeze_deadband else "deadzone_edge"
    return sign * (abs_error - threshold), mode
