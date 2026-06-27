"""Tests for the per-edge coupling estimator."""

import math

from custom_components.vtherm_smartpi.smartpi.coupling_estimator import (
    CouplingEstimator,
    edge_k_instant,
    edge_regressor,
)
from custom_components.vtherm_smartpi.smartpi.room_coupling import (
    TARGET_OUTSIDE,
    TARGET_ROOM,
    ResolvedEdge,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redge(edge_id="B", t_j=24.0, kind=TARGET_ROOM, **kw):
    return ResolvedEdge(
        edge_id=edge_id, target_kind=kind, aperture_type="door",
        open_policy="model", neighbor_temp=t_j, neighbor_power_w=None,
        neighbor_uid=edge_id if kind == TARGET_ROOM else None, **kw
    )


def _drive(est, edges, tins, *, a=0.01, b=0.008, u=0.5, text=5.0):
    """Feed a tin trajectory through the estimator (it differences tin internally)."""
    for tin in tins:
        est.update(dt_min=1.0, tin=tin, text=text, u=u, a=a, b=b,
                   open_edges=edges, allow_learn=True)


# ---------------------------------------------------------------------------
# New RLS-based tests
# ---------------------------------------------------------------------------


def test_single_edge_learns_positive_k():
    est = CouplingEstimator("R")
    edge = _redge("B", t_j=24.0)
    # A rising tin (neighbour warmer than us) yields a positive residual the
    # estimator attributes to the open edge -> coeff becomes positive.
    tins = [20.0 + 0.05 * i for i in range(40)]
    _drive(est, [edge], tins)
    assert est.coeff("B") > 0.0
    assert est._rls.samples("B") > 0


def test_two_open_edges_no_longer_blocked():
    """Two simultaneously-open edges are accepted (old code held this)."""
    est = CouplingEstimator("R")
    e1 = _redge("B", t_j=24.0)
    e2 = _redge("C", t_j=18.0)
    prev = 20.0
    est.update(dt_min=1.0, tin=prev, text=5.0, u=0.5, a=0.01, b=0.008,
               open_edges=[e1, e2], allow_learn=True)
    for i in range(30):
        tin = 20.0 + 0.03 * i
        est.update(dt_min=1.0, tin=tin, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[e1, e2], allow_learn=True)
    # Both edges have received samples (not held).
    assert est._rls.samples("B") > 0
    assert est._rls.samples("C") > 0


def test_allow_learn_false_holds():
    est = CouplingEstimator("R")
    edge = _redge("B")
    est.update(dt_min=1.0, tin=20.0, text=5.0, u=0.5, a=0.01, b=0.008,
               open_edges=[edge], allow_learn=False)
    est.update(dt_min=1.0, tin=20.5, text=5.0, u=0.5, a=0.01, b=0.008,
               open_edges=[edge], allow_learn=False)
    assert est._rls.samples("B") == 0


# ---------------------------------------------------------------------------
# Standalone function tests (kept — no API change)
# ---------------------------------------------------------------------------


def test_regressor_linear_for_room():
    assert edge_regressor(TARGET_ROOM, 22.0, 19.0) == -3.0


def test_regressor_sqrt_law_for_outside():
    # Δ = 22 - 7 = 15 ; x = -sign(Δ)|Δ|^1.5
    assert abs(edge_regressor(TARGET_OUTSIDE, 22.0, 7.0) - (-(15.0 ** 1.5))) < 1e-9
    # Sign follows Δ.
    assert edge_regressor(TARGET_OUTSIDE, 5.0, 10.0) > 0.0


def test_k_instant_room_vs_outside():
    assert edge_k_instant(TARGET_ROOM, 0.08, 22.0, 19.0) == 0.08
    assert abs(edge_k_instant(TARGET_OUTSIDE, 0.02, 22.0, 7.0)
               - 0.02 * math.sqrt(15.0)) < 1e-9


def test_holds_when_gradient_too_small():
    """An open edge with |tin - T_j| below COUPLING_DT_MIN_C yields no sample."""
    est = CouplingEstimator("R")
    edge = _redge("B", t_j=20.2)  # tin ~20.0 -> |Delta| ~0.2 < 0.5 threshold
    est.update(dt_min=1.0, tin=20.0, text=5.0, u=0.5, a=0.01, b=0.008,
               open_edges=[edge], allow_learn=True)
    for _ in range(10):
        est.update(dt_min=1.0, tin=20.05, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[edge], allow_learn=True)
    assert est._rls.samples("B") == 0


def test_prune_drops_unknown_edges():
    est = CouplingEstimator("R")
    e1 = _redge("B", t_j=24.0)
    e2 = _redge("C", t_j=18.0)
    for i in range(10):
        est.update(dt_min=1.0, tin=20.0 + 0.05 * i, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[e1, e2], allow_learn=True)
    assert "B" in est._rls.edge_ids() and "C" in est._rls.edge_ids()
    est.prune({"B"})
    assert est._rls.edge_ids() == ["B"]


def test_coeff_held_when_no_edge_open():
    """A learned edge keeps its coefficient across cycles with nothing open."""
    est = CouplingEstimator("R")
    edge = _redge("B", t_j=24.0)
    for i in range(20):
        est.update(dt_min=1.0, tin=20.0 + 0.05 * i, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[edge], allow_learn=True)
    learned = est.coeff("B")
    assert learned != 0.0
    for i in range(10):  # nothing open -> learning held
        est.update(dt_min=1.0, tin=21.0 + 0.05 * i, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[], allow_learn=True)
    assert est.coeff("B") == learned
