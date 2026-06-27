"""Tests for the joint multi-edge recursive least squares engine."""

import math

from custom_components.vtherm_smartpi.smartpi.rls import MultiEdgeRLS


def _rls(**kw):
    defaults = dict(p0=10.0, lam=0.995, p_max=50.0, huber_c=0.2,
                    theta_min=0.0, theta_max=0.5)
    defaults.update(kw)
    return MultiEdgeRLS(**defaults)


def test_ensure_and_accessors():
    rls = _rls()
    assert rls.value("A") == 0.0
    assert math.isinf(rls.variance("A"))
    rls.ensure_edge("A")
    assert rls.value("A") == 0.0
    assert rls.variance("A") == 10.0
    assert rls.edge_ids() == ["A"]


def test_drop_missing():
    rls = _rls()
    rls.ensure_edge("A")
    rls.ensure_edge("B")
    rls.drop_missing({"A"})
    assert rls.edge_ids() == ["A"]
    assert math.isinf(rls.variance("B"))


def test_single_edge_recovers_true_coefficient():
    """One always-open edge with varying x converges to the true theta."""
    rls = _rls()
    true_k = 0.08
    # y = true_k * x, with x = -(Ti - Tj) varying each cycle.
    for x in [-3.0, -2.0, -4.0, -2.5, -3.5] * 8:
        rls.update({"A": x}, true_k * x)
    assert abs(rls.value("A") - true_k) < 5e-3
    assert rls.variance("A") < 0.05  # excited -> identifiable


def test_two_edges_separable_when_patterns_vary():
    """Two edges with time-varying open patterns are jointly identified."""
    rls = _rls()
    ka, kb = 0.06, 0.10
    # Alternate which edges are open so the regressors are not collinear.
    patterns = [
        {"A": -3.0},
        {"B": -2.0},
        {"A": -2.5, "B": -3.0},
        {"A": -4.0},
        {"B": -1.5},
        {"A": -1.0, "B": -2.5},
    ]
    for _ in range(15):
        for p in patterns:
            y = sum({"A": ka, "B": kb}[e] * x for e, x in p.items())
            rls.update(p, y)
    assert abs(rls.value("A") - ka) < 8e-3
    assert abs(rls.value("B") - kb) < 8e-3


def test_collinear_always_together_stays_unidentified():
    """Two edges ALWAYS open together with equal x: only the sum is learnable;
    individual variances stay high (unidentifiable)."""
    rls = _rls()
    ka, kb = 0.05, 0.09
    for x in [-3.0, -2.0, -4.0, -2.5] * 20:
        # Both edges share the same regressor every cycle -> collinear.
        y = ka * x + kb * x
        rls.update({"A": x, "B": x}, y)
    # Sum is pinned...
    assert abs((rls.value("A") + rls.value("B")) - (ka + kb)) < 0.02
    # ...but neither individual edge is confidently identified.
    assert rls.variance("A") > 0.05
    assert rls.variance("B") > 0.05


def test_non_negativity_projection():
    """A negative true coefficient is clamped at theta_min (=0)."""
    rls = _rls()
    for x in [-3.0, -2.0, -4.0] * 10:
        rls.update({"A": x}, -0.05 * x)  # true theta = -0.05
    assert rls.value("A") == 0.0


def test_closed_edge_is_held():
    """An edge absent from regressors keeps its theta and variance."""
    rls = _rls()
    for x in [-3.0, -2.0, -4.0] * 8:
        rls.update({"A": x}, 0.08 * x)
    held_theta = rls.value("A")
    held_var = rls.variance("A")
    rls.ensure_edge("B")
    for x in [-2.0, -3.0] * 8:
        rls.update({"B": x}, 0.05 * x)  # A is closed here
    assert rls.value("A") == held_theta
    assert rls.variance("A") == held_var


def test_reset_edge_inflates_variance_keeps_theta():
    rls = _rls()
    for x in [-3.0, -2.0, -4.0] * 8:
        rls.update({"A": x}, 0.08 * x)
    theta = rls.value("A")
    assert rls.variance("A") < 10.0
    rls.reset_edge("A")
    assert rls.variance("A") == 10.0
    assert rls.value("A") == theta


def test_reliable_helper():
    rls = _rls()
    rls.ensure_edge("A")
    assert rls.reliable("A", var_max=0.05, min_samples=8) is False
    for x in [-3.0, -2.0, -4.0, -2.5] * 6:
        rls.update({"A": x}, 0.08 * x)
    assert rls.reliable("A", var_max=0.05, min_samples=8) is True


def test_load_state_ignores_malformed_input():
    rls = _rls()
    for bad in (None, {}, [1, 2, 3], "corrupt", 42):
        rls.load_state(bad)        # must not raise
    assert rls.edge_ids() == []    # nothing loaded from garbage


def test_save_load_roundtrip():
    rls = _rls()
    for x in [-3.0, -2.0, -4.0] * 8:
        rls.update({"A": x, "B": x * 0.5}, 0.08 * x)  # A and B co-active -> cross-term
    state = rls.save_state()
    restored = _rls()
    restored.load_state(state)
    assert abs(restored.value("A") - rls.value("A")) < 1e-12
    assert abs(restored.variance("A") - rls.variance("A")) < 1e-12
    assert restored.samples("A") == rls.samples("A")
    assert set(restored.edge_ids()) == {"A", "B"}
    # Off-diagonal cross-covariance is faithfully restored.
    assert abs(restored._P["A"]["B"] - rls._P["A"]["B"]) < 1e-12
