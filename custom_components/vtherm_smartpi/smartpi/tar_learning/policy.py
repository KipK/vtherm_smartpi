"""Policy of the T_ar learning: which value the regulation uses (pure Python).

- Fast value: the morning-step publication over the accepted and confirmed
  mornings (at least 3, median of the last 5, MAD / median <= 25 %).
- Refined value: the emitter time constant tau_e of the multi-day
  identification, published only from evaluations whose six nights are
  regulated; any other evaluation is a diagnostic.
- In force: the refined value once published, else the fast value.
- Applied: the value in force when it lies within [TAR_APPLY_BOUND_LOW,
  TAR_APPLY_BOUND_HIGH] x the bootstrap dead time; otherwise the bootstrap
  dead time stays in use and the value is flagged out of bound.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from ...hvac_mode import VThermHvacMode_COOL
from .const import (
    PUB_LAST,
    PUB_MAD_REL,
    PUB_MIN_ACCEPTED,
    SOURCE_BOOTSTRAP,
    SOURCE_FAST,
    SOURCE_REFINED,
    TAR_APPLY_BOUND_HIGH,
    TAR_APPLY_BOUND_LOW,
)


def fast_publication(values: list[float]) -> float | None:
    """Value published after the accepted mornings `values` (oldest first), None if not published."""
    if len(values) < PUB_MIN_ACCEPTED:
        return None
    last = values[-PUB_LAST:]
    m = statistics.median(last)
    mad = statistics.median([abs(x - m) for x in last])
    if m > 0 and mad / m <= PUB_MAD_REL:
        return m
    return None


@dataclass(frozen=True)
class AppliedValue:
    """Value of the HEAT active dead time used by the regulation."""

    value_s: float | None
    source: str
    in_force_s: float | None
    in_force_source: str | None
    out_of_bound: bool


def applied_value(
    bootstrap_s: float | None,
    bootstrap_reliable: bool,
    fast_s: float | None,
    refined_s: float | None,
) -> AppliedValue:
    """Value used by the regulation from the bootstrap, fast and refined values."""
    if refined_s is not None:
        in_force, src = refined_s, SOURCE_REFINED
    elif fast_s is not None:
        in_force, src = fast_s, SOURCE_FAST
    else:
        in_force, src = None, None
    if in_force is None or not bootstrap_reliable or bootstrap_s is None or bootstrap_s <= 0.0:
        return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, in_force, src, False)
    lo, hi = TAR_APPLY_BOUND_LOW * bootstrap_s, TAR_APPLY_BOUND_HIGH * bootstrap_s
    if not lo <= in_force <= hi:
        return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, in_force, src, True)
    return AppliedValue(float(in_force), src, in_force, src, False)


class RegulationDeadTime:
    """Dead-time view of the regulation: the HEAT active dead time is the learned one.

    Every other attribute is the dead-time estimator's own, so the bootstrap and
    calibration values stay stored and untouched.
    """

    def __init__(self, dt_est, heat_s: float) -> None:
        self._dt_est = dt_est
        self._heat_s = float(heat_s)

    def __getattr__(self, name):
        return getattr(self._dt_est, name)

    @property
    def deadtime_heat_s(self) -> float:
        return self._heat_s

    def active_deadtime(self, hvac_mode) -> tuple[float | None, bool]:
        if hvac_mode == VThermHvacMode_COOL:
            return self._dt_est.active_deadtime(hvac_mode)
        return self._heat_s, self._dt_est.deadtime_heat_reliable

    def passive_deadtime(self, hvac_mode) -> tuple[float | None, bool]:
        return self._dt_est.passive_deadtime(hvac_mode)
