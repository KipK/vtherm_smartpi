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


# ---------------------------------------------------------------------------
# Consensus shrinkage tests
# ---------------------------------------------------------------------------


def _consensus_edge(neighbor_k, neighbor_reliable):
    return ResolvedEdge(edge_id="B", target_kind=TARGET_ROOM, aperture_type="door",
                        open_policy="model", neighbor_temp=24.0, neighbor_power_w=None,
                        neighbor_uid="B", neighbor_k=neighbor_k,
                        neighbor_reliable=neighbor_reliable)


def test_consensus_pulls_unreliable_local_toward_reliable_neighbor():
    """Call _apply_consensus directly to isolate it from RLS dynamics."""
    est = CouplingEstimator("R")
    est._rls.ensure_edge("B")  # local edge exists but is unreliable (no data)
    edge = _consensus_edge(neighbor_k=0.10, neighbor_reliable=True)
    for _ in range(30):
        est._apply_consensus([edge])
    assert est.coeff("B") > 0.0
    assert est.coeff("B") <= 0.10 + 1e-9       # shrinks toward, never past, 0.10


def test_consensus_no_pull_when_neighbor_unreliable():
    est = CouplingEstimator("R")
    est._rls.ensure_edge("B")
    edge = _consensus_edge(neighbor_k=0.10, neighbor_reliable=False)
    for _ in range(30):
        est._apply_consensus([edge])
    assert est.coeff("B") == 0.0               # unreliable neighbour exerts no pull


def test_consensus_skips_reliable_local():
    """A well-excited (reliable) local edge keeps its room-local value."""
    est = CouplingEstimator("R")
    est._rls.seed_confidence("B", n=50, var=0.001)  # reliable locally
    est._rls.set_value("B", 0.04)
    est._kind["B"] = "room"
    edge = _consensus_edge(neighbor_k=0.10, neighbor_reliable=True)
    for _ in range(30):
        est._apply_consensus([edge])
    assert abs(est.coeff("B") - 0.04) < 1e-9   # local reliability dominates


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


def test_save_load_roundtrip_new_format():
    est = CouplingEstimator("R")
    edge = _redge("B", t_j=24.0)
    for i in range(20):
        est.update(dt_min=1.0, tin=20.0 + 0.05 * i, text=5.0, u=0.5,
                   a=0.01, b=0.008, open_edges=[edge], allow_learn=True)
    state = est.save_state()
    est2 = CouplingEstimator("R")
    est2.load_state(state)
    assert abs(est2.coeff("B") - est.coeff("B")) < 1e-9
    assert est2.edges_diag()["B"]["kind"] == "room"


def test_load_legacy_format_migrates():
    legacy = {"edges": {"B": {"k": 0.07, "reliable": True, "n_ok": 30, "hist": []}}}
    est = CouplingEstimator("R")
    est.load_state(legacy)
    assert abs(est.coeff("B") - 0.07) < 1e-9
    assert est.reliable("B") is True  # low seeded variance + n_ok >= MIN_SAMPLES


def test_covariance_reset_on_reopen_after_long_closure():
    from custom_components.vtherm_smartpi.smartpi.const import COUPLING_RESET_AFTER_CLOSED
    est = CouplingEstimator("R")
    edge = _redge("B", t_j=24.0)
    # Learn the edge to a confident (low-variance) state.
    for i in range(30):
        est.update(dt_min=1.0, tin=20.0 + 0.05 * i, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[edge], allow_learn=True)
    var_before = est._rls.variance("B")
    assert var_before < 1.0
    # Keep it closed for longer than the reset threshold (nothing open).
    for _ in range(COUPLING_RESET_AFTER_CLOSED + 1):
        est.update(dt_min=1.0, tin=20.0, text=5.0, u=0.5, a=0.01, b=0.008,
                   open_edges=[], allow_learn=True)
    # Reopen with a gradient: covariance should have been inflated (reset).
    est.update(dt_min=1.0, tin=20.0, text=5.0, u=0.5, a=0.01, b=0.008,
               open_edges=[edge], allow_learn=True)
    assert est._rls.variance("B") > var_before
