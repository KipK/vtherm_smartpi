"""Aggregation functions for ABEstimator published values (a_hat, b_hat)."""
from __future__ import annotations

import statistics
from typing import Any


def weighted_median(values: list[float], weights: list[float]) -> float:
    """Compute weighted median.

    Preconditions: len(values) == len(weights), all weights > 0.
    """
    pairs = sorted(zip(values, weights), key=lambda t: t[0])
    w_total = sum(w for _, w in pairs)
    w_half = 0.5 * w_total
    w_cum = 0.0
    for x, w in pairs:
        w_cum += w
        if w_cum >= w_half:
            return x
    return pairs[-1][0]


def ab_publish(
    values_deque,
    plateau_n: int,
    alpha: float,
    r: float,
    min_points_for_publish: int,
    default_value: float,
) -> tuple[float, dict[str, Any]]:
    """Compute the published a_hat or b_hat from the measurement buffer.

    Buffer ordering assumption: oldest at index 0, newest at index N-1
    (deque built with append(), oldest auto-evicted from left).
    Age j: j=0 is the most recent point, j=N-1 is the oldest.

    Returns (published_value, diag_dict).
    """
    vals = list(values_deque)
    N = len(vals)

    # Bootstrap: not enough points — freeze to default value
    if N < min_points_for_publish:
        return default_value, {
            "ab_bootstrap": True,
            "ab_points": N,
            "ab_mode_effective": "default_frozen",
        }

    # Short window: all weights are uniform, weighted median degenerates to plain median
    if N <= plateau_n:
        return statistics.median(vals), {
            "ab_bootstrap": False,
            "ab_points": N,
            "ab_mode_effective": "weighted_median",
        }

    # Weighted median: plateau of weight 1.0 for newest points, geometric tail for older ones
    weights = []
    for idx in range(N):
        j = (N - 1) - idx          # j=0 → newest (idx=N-1), j=N-1 → oldest (idx=0)
        w = 1.0 if j < plateau_n else alpha * (r ** (j - plateau_n))
        weights.append(w)

    m = weighted_median(vals, weights)
    return m, {
        "ab_bootstrap": False,
        "ab_points": N,
        "ab_mode_effective": "weighted_median",
        "ab_wmed_plateau_n": plateau_n,
        "ab_wmed_alpha": alpha,
        "ab_wmed_r": r,
    }
