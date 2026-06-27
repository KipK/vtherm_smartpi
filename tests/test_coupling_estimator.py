"""Tests for the per-edge coupling estimator."""

from custom_components.vtherm_smartpi.smartpi.coupling_estimator import (
    CouplingEstimator,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    COUPLING_K_MAX,
    COUPLING_MIN_SAMPLES,
)
from custom_components.vtherm_smartpi.smartpi.room_coupling import ResolvedEdge


A, B, U, TEXT = 0.01, 0.008, 0.5, 5.0


def _edge(neighbor_uid="N", t_j=24.0, power=100.0):
    return ResolvedEdge(
        neighbor_uid=neighbor_uid,
        door_open=True,
        neighbor_available=True,
        neighbor_temp=t_j,
        neighbor_power_w=power,
    )


def _drive_single(est, k_true, ti=21.0, t_j=24.0, n=40, dt=1.0):
    """Feed synthetic samples where the residual is exactly -k_true*(Ti-Tj)."""
    edge = _edge(t_j=t_j)
    for _ in range(n):
        desired_m = A * U - B * (ti - TEXT) - k_true * (ti - t_j)
        est._last_tin = ti - desired_m * dt
        est.update(
            dt_min=dt, tin=ti, text=TEXT, u=U, a=A, b=B,
            open_edges=[edge], allow_learn=True,
        )


def test_converges_to_true_k():
    est = CouplingEstimator("t")
    _drive_single(est, 0.02)
    assert abs(est.k("N") - 0.02) < 0.003
    assert est.reliable("N") is True


def test_not_reliable_before_min_samples():
    est = CouplingEstimator("t")
    _drive_single(est, 0.02, n=COUPLING_MIN_SAMPLES - 1)
    assert est.reliable("N") is False


def test_k_clamped_to_max():
    est = CouplingEstimator("t")
    _drive_single(est, 10.0)  # absurdly large true coupling
    assert est.k("N") <= COUPLING_K_MAX + 1e-9


def test_persists_when_door_closed():
    """k is structural: it must NOT decay while the door is closed."""
    est = CouplingEstimator("t")
    _drive_single(est, 0.02)
    learned = est.k("N")
    for _ in range(20):
        est._last_tin = 21.0
        est.update(
            dt_min=1.0, tin=21.0, text=TEXT, u=U, a=A, b=B,
            open_edges=[], allow_learn=True,
        )
    assert abs(est.k("N") - learned) < 1e-9


def test_holds_when_gradient_too_small():
    """No sample is taken when |Ti - Tj| is below the gradient floor."""
    est = CouplingEstimator("t")
    _drive_single(est, 0.02, ti=24.0, t_j=24.1)  # 0.1 °C < DT_MIN
    assert est.k("N") == 0.0
    assert est.reliable("N") is False


def test_no_learn_when_disallowed():
    est = CouplingEstimator("t")
    edge = _edge()
    for _ in range(20):
        est._last_tin = 20.0
        est.update(
            dt_min=1.0, tin=21.0, text=TEXT, u=U, a=A, b=B,
            open_edges=[edge], allow_learn=False,
        )
    assert est.k("N") == 0.0


def test_two_open_doors_hold_learning():
    """Two doors open at once is unidentifiable -> no edge is updated."""
    est = CouplingEstimator("t")
    ti, tj1, tj2 = 21.0, 24.0, 17.0
    e1, e2 = _edge("N1", tj1), _edge("N2", tj2)
    for _ in range(40):
        desired_m = A * U - B * (ti - TEXT) - 0.02 * (ti - tj1) - 0.04 * (ti - tj2)
        est._last_tin = ti - desired_m
        est.update(
            dt_min=1.0, tin=ti, text=TEXT, u=U, a=A, b=B,
            open_edges=[e1, e2], allow_learn=True,
        )
    assert est.k("N1") == 0.0
    assert est.k("N2") == 0.0


def test_single_door_learns_when_other_closed():
    """Each edge is learned opportunistically while it is the sole open door."""
    est = CouplingEstimator("t")
    _drive_single(est, 0.025, t_j=24.0)  # only neighbour N open
    assert abs(est.k("N") - 0.025) < 0.004
    assert est.reliable("N") is True


def test_save_load_round_trip():
    est = CouplingEstimator("t")
    _drive_single(est, 0.02)
    state = est.save_state()
    restored = CouplingEstimator("t2")
    restored.load_state(state)
    assert abs(restored.k("N") - est.k("N")) < 1e-9
    assert restored.reliable("N") == est.reliable("N")


def test_prune_drops_unknown_neighbors():
    est = CouplingEstimator("t")
    _drive_single(est, 0.02)
    est.prune(set())  # no neighbours configured anymore
    assert est.k("N") == 0.0
