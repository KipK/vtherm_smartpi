"""Policy of the T_ar learning: which value the regulation uses (pure Python).

- Fast value: the morning-step publication over the accepted and confirmed
  mornings (at least 3, median of the last 5, MAD / median <= 25 %).
- Lower bound: the median of the last 5 confirmed comfort steps cut by sunrise
  too early for an estimate; each gives TAR_CUT_BOUND_FACTOR x its fitted value.
  A bound never enters the fast value. It raises a shorter learned value (or
  stands for a missing one) and is never confirmed by a second method.
- Refined value: the emitter time constant tau_e of the multi-day
  identification, published only from evaluations whose six nights are
  regulated; any other evaluation is a diagnostic.
- In force: the refined value once published, else the fast value.
- Applied (gain units, const.py): the learned effective dead time
  L_T = TAR_GAIN_DEADTIME_FACTOR x the learned value (the value in force, raised
  to the lower bound) against the reference L_b. Below L_b the gains keep L_b;
  up to TAR_APPLY_LONG_FACTOR x L_b they use L_T; above, they use L_T only when
  the fast value and the window value (the refined value, else the emitter time
  constant of the latest evaluation whose two windows agree) agree within
  TAR_AGREE_TOL, else TAR_APPLY_LONG_FACTOR x L_b. The other consumers of the
  dead time use the learned value when the gains use L_T, else the bootstrap
  dead time.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from ...hvac_mode import VThermHvacMode_COOL
from .const import (
    LIMIT_LONG,
    LIMIT_SHORT,
    PUB_LAST,
    PUB_MAD_REL,
    PUB_MIN_ACCEPTED,
    SOURCE_BOOTSTRAP,
    SOURCE_FAST,
    SOURCE_LOWER_BOUND,
    SOURCE_REFINED,
    TAR_AGREE_TOL,
    TAR_APPLY_LONG_FACTOR,
    TAR_GAIN_DEADTIME_FACTOR,
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


def bound_publication(values: list[float]) -> float | None:
    """Lower bound in force after the confirmed cut comfort steps `values` (oldest first), None without any."""
    last = [v for v in values[-PUB_LAST:] if v > 0.0]
    return float(statistics.median(last)) if last else None


def methods_agree(fast_s: float | None, window_s: float | None) -> bool:
    """The morning-step value and the window value agree within TAR_AGREE_TOL (both needed)."""
    if fast_s is None or window_s is None or fast_s <= 0.0 or window_s <= 0.0:
        return False
    return abs(math.log(fast_s / window_s)) <= TAR_AGREE_TOL


@dataclass(frozen=True)
class AppliedValue:
    """Values of the HEAT active dead time used by the regulation.

    value_s: dead time of the regulation (the learned value when the gains use it, else the bootstrap);
    gains_s: effective dead time of the gain rule; reference_s: the reference L_b of the rule;
    out_of_bound: a learned value exists and is not used as it is; limit: why (LIMIT_SHORT, LIMIT_LONG).
    """

    value_s: float | None
    source: str
    in_force_s: float | None
    in_force_source: str | None
    out_of_bound: bool
    gains_s: float | None = None
    reference_s: float | None = None
    limit: str | None = None


def _positive(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0.0


def applied_value(
    bootstrap_s: float | None,
    bootstrap_reliable: bool,
    fast_s: float | None,
    refined_s: float | None,
    reference_s: float | None = None,
    window_s: float | None = None,
    bound_s: float | None = None,
) -> AppliedValue:
    """Value used by the regulation from the bootstrap, fast, refined and lower-bound values.

    reference_s: the reference L_b in gain units (the bootstrap dead time when None);
    window_s: the window value that may confirm the fast value above TAR_APPLY_LONG_FACTOR x L_b.
    """
    if refined_s is not None:
        in_force, src = refined_s, SOURCE_REFINED
    elif fast_s is not None:
        in_force, src = fast_s, SOURCE_FAST
    else:
        in_force, src = None, None
    learned, learned_src = in_force, src
    if _positive(bound_s) and (learned is None or bound_s > learned):
        learned, learned_src = float(bound_s), SOURCE_LOWER_BOUND
    ref = reference_s if reference_s is not None else bootstrap_s
    if not (bootstrap_reliable and _positive(bootstrap_s) and _positive(ref)) or not _positive(learned):
        return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, in_force, src, False, bootstrap_s,
                            ref if _positive(ref) else None, None)
    lt = TAR_GAIN_DEADTIME_FACTOR * learned
    if lt < ref:
        return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, in_force, src, True, float(ref), float(ref), LIMIT_SHORT)
    if lt <= TAR_APPLY_LONG_FACTOR * ref or (learned_src != SOURCE_LOWER_BOUND and methods_agree(fast_s, window_s)):
        return AppliedValue(float(learned), learned_src, in_force, src, False, lt, float(ref), None)
    return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, in_force, src, True, TAR_APPLY_LONG_FACTOR * ref, float(ref),
                        LIMIT_LONG)


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
