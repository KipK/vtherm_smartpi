"""Tests for smartpi/ab_aggregator.py — weighted-median aggregation."""
from __future__ import annotations

import statistics
from collections import deque

from custom_components.versatile_thermostat.smartpi.ab_aggregator import (
    ab_publish,
    weighted_median,
)


# ---------------------------------------------------------------------------
# T1 — Short-window degeneracy: N <= plateau_n → plain median result
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """T1: when N <= plateau_n, ab_publish must produce the same result as statistics.median()."""

    def test_plain_median_matches_stdlib(self):
        values = [0.005, 0.008, 0.003, 0.007, 0.006, 0.004, 0.009, 0.002, 0.010, 0.001, 0.005]
        d = deque(values, maxlen=31)
        result, diag = ab_publish(
            d,
            plateau_n=11,
            alpha=1.0,
            r=0.85,
            min_points_for_publish=1,
            default_value=0.0005,
        )
        assert result == statistics.median(values)
        assert diag["ab_mode_effective"] == "weighted_median"
        assert diag["ab_bootstrap"] is False

    def test_plain_median_odd_buffer(self):
        values = list(range(1, 12))  # 11 values, equals plateau_n
        d = deque([float(v) for v in values], maxlen=31)
        result, _ = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert result == statistics.median([float(v) for v in values])


# ---------------------------------------------------------------------------
# T2 — Bootstrap freeze: N < min_points_for_publish → return default_value
# ---------------------------------------------------------------------------

class TestBootstrapFreeze:
    """T2: buffer smaller than min_points_for_publish must return default_value."""

    def test_bootstrap_returns_default(self):
        d = deque([0.001, 0.002, 0.003], maxlen=31)
        default = 0.0010
        result, diag = ab_publish(
            d,
            plateau_n=11,
            alpha=1.0,
            r=0.85,
            min_points_for_publish=11,
            default_value=default,
        )
        assert result == default
        assert diag["ab_bootstrap"] is True
        assert diag["ab_mode_effective"] == "default_frozen"

    def test_bootstrap_independent_of_buffer_content(self):
        """Different buffer contents must all produce the same default_value."""
        default = 0.0010
        for content in [[0.001] * 5, [99.0] * 7, [0.0] * 3]:
            d = deque(content, maxlen=31)
            result, diag = ab_publish(
                d, plateau_n=11, alpha=1.0, r=0.85,
                min_points_for_publish=11, default_value=default,
            )
            assert result == default
            assert diag["ab_bootstrap"] is True

    def test_bootstrap_boundary_exact_min(self):
        """Exactly min_points_for_publish points must NOT trigger bootstrap."""
        values = [0.005] * 11
        d = deque(values, maxlen=31)
        result, diag = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=11, default_value=9999.0,
        )
        assert diag["ab_bootstrap"] is False
        assert result != 9999.0


# ---------------------------------------------------------------------------
# T3 — Short-window degeneracy: N <= plateau_n → plain median result
# ---------------------------------------------------------------------------

class TestShortWindowDegeneracy:
    """T3: when N <= plateau_n, weighted mode must degenerate to plain median."""

    def test_n_equals_plateau(self):
        values = [0.001, 0.009, 0.005, 0.003, 0.007, 0.002, 0.008, 0.004, 0.006, 0.010, 0.005]
        assert len(values) == 11  # equals plateau_n=11
        d = deque(values, maxlen=31)
        result, diag = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert result == statistics.median(values)
        assert diag["ab_mode_effective"] == "weighted_median"

    def test_n_less_than_plateau(self):
        values = [0.005] * 7
        d = deque(values, maxlen=31)
        result, diag = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert result == statistics.median(values)
        assert diag["ab_mode_effective"] == "weighted_median"


# ---------------------------------------------------------------------------
# T4 — Correct age ordering: newest points should have highest weights
# ---------------------------------------------------------------------------

class TestAgeOrdering:
    """T4: verify that age mapping is not inverted (newest → plateau, oldest → tail)."""

    def _make_deque(self, oldest_values, newest_values):
        """Build a deque: oldest_values first, then newest_values (append order)."""
        d = deque(maxlen=31)
        for v in oldest_values:
            d.append(v)
        for v in newest_values:
            d.append(v)
        return d

    def test_recent_cluster_dominates(self):
        """Newest 11 points = 0.010 (plateau); older 10 points = 0.001 (tail).
        With weighted_median the result should lean toward the recent cluster.
        """
        d = self._make_deque(oldest_values=[0.001] * 10, newest_values=[0.010] * 11)
        result, diag = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert diag["ab_mode_effective"] == "weighted_median"
        # Weighted median must be pulled toward 0.010 (the plateau cluster)
        assert result >= 0.005

    def test_inverted_order_gives_different_result(self):
        """Swapping oldest/newest should yield a different result when the two
        clusters are distinct — proving the age mapping is not symmetric/inverted.
        """
        oldest = [0.001] * 10
        newest = [0.010] * 11

        d_correct = self._make_deque(oldest_values=oldest, newest_values=newest)
        d_inverted = self._make_deque(oldest_values=newest, newest_values=oldest)

        result_correct, _ = ab_publish(
            d_correct, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        result_inverted, _ = ab_publish(
            d_inverted, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert result_correct != result_inverted


# ---------------------------------------------------------------------------
# T5 — Outlier robustness: a single extreme outlier must not dominate
# ---------------------------------------------------------------------------

class TestOutlierRobustness:
    """T5: one extreme outlier in the recent window should not shift the weighted median."""

    def test_single_extreme_outlier_does_not_dominate(self):
        """20 normal values around 0.005; one extreme outlier (0.100) as the newest.
        The single outlier has plateau weight 1.0 but all other plateau points
        also have weight 1.0 — so it cannot accumulate >50% cumulative weight
        by itself if plateau_n > 1.
        """
        normal_value = 0.005
        outlier = 0.100

        d = deque(maxlen=31)
        # Add 20 normal values
        for _ in range(20):
            d.append(normal_value)
        # Add outlier as most recent point
        d.append(outlier)

        result, diag = ab_publish(
            d, plateau_n=11, alpha=1.0, r=0.85,
            min_points_for_publish=1, default_value=0.0,
        )
        assert diag["ab_mode_effective"] == "weighted_median"
        # The outlier is 20x the normal value; the median must stay near the cluster
        assert result < outlier * 0.5, f"Outlier dominated: result={result}"
        assert abs(result - normal_value) < 0.001, f"Too far from cluster: result={result}"

    def test_weighted_median_primitive_basic(self):
        """Direct test of weighted_median() primitive."""
        # Equal weights → same as plain median
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        weights = [1.0, 1.0, 1.0, 1.0, 1.0]
        assert weighted_median(values, weights) == statistics.median(values)

    def test_weighted_median_heavy_tail(self):
        """Heavy weight on one side should shift result toward that side."""
        values = [1.0, 2.0, 10.0]
        weights = [1.0, 1.0, 100.0]  # 10.0 has overwhelming weight
        result = weighted_median(values, weights)
        assert result == 10.0
