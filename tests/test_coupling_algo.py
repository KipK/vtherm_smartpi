"""Algo-level integration tests for room coupling.

Exercises the coupling wiring inside SmartPI without driving a full control
cycle: effective-parameter folding, the diagnostics block, snapshot publishing,
and persistence (including the no-coupling regression to identity).
"""

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.room_coupling import ResolvedEdge


def make_smartpi(**kwargs):
    defaults = dict(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestCoupling",
        debug_mode=True,
    )
    defaults.update(kwargs)
    return SmartPI(**defaults)


class _FakeView:
    """Minimal RoomView stand-in for one open neighbour 'N'."""

    def __init__(self, *, open_=True, neighbor_temp=24.0, power=250.0):
        self._open = open_
        self._neighbor_temp = neighbor_temp
        self._power = power
        self.published = []

    def any_open(self):
        return self._open

    def open_edges(self):
        if not self._open:
            return []
        return [
            ResolvedEdge(
                neighbor_uid="N",
                door_open=True,
                neighbor_available=True,
                neighbor_temp=self._neighbor_temp,
                neighbor_power_w=self._power,
            )
        ]

    def component_power_w(self):
        return self._power

    def publish(self, snapshot):
        self.published.append(snapshot)


def _load_k(algo, neighbor="N", k=0.01):
    algo.coupling_est.load_state(
        {"edges": {neighbor: {"k": k, "reliable": True, "n_ok": 10, "hist": [k] * 10}}}
    )


def test_fresh_instance_has_coupling_components():
    algo = make_smartpi()
    assert algo.coupling_est is not None
    assert algo._coupling_view is None
    assert algo._last_coupling_diag == {}


def test_refresh_context_identity_without_view():
    algo = make_smartpi()
    b_eff, text_eff = algo._refresh_coupling_context(21.0, 5.0)
    assert b_eff == algo.est.b
    assert text_eff == 5.0
    assert algo._coupling_any_open() is False


def test_refresh_context_folds_open_edge():
    algo = make_smartpi()
    _load_k(algo, k=0.01)
    algo.attach_coupling_view(_FakeView(open_=True, neighbor_temp=24.0))
    base_b = algo.est.b
    # Slew converges over several cycles toward (b + k).
    for _ in range(60):
        b_eff, text_eff = algo._refresh_coupling_context(21.0, 5.0)
    assert b_eff > base_b
    assert b_eff <= base_b + 0.01 + 1e-9
    # Text_eff pulled from outdoor (5) toward the warmer neighbour (24).
    assert 5.0 < text_eff < 24.0
    diag = algo._last_coupling_diag
    assert diag["any_door_open"] is True
    assert diag["open_neighbors"] == ["N"]
    assert diag["component_power_w"] == 250.0


def test_closed_door_returns_to_identity():
    algo = make_smartpi()
    _load_k(algo, k=0.01)
    view = _FakeView(open_=True)
    algo.attach_coupling_view(view)
    for _ in range(60):
        algo._refresh_coupling_context(21.0, 5.0)
    # Door closes: the slewed load decays and snaps back to the base model.
    view._open = False
    for _ in range(200):
        b_eff, text_eff = algo._refresh_coupling_context(21.0, 5.0)
    assert b_eff == algo.est.b
    assert text_eff == 5.0


def test_set_measured_power_and_snapshot_publish():
    algo = make_smartpi()
    view = _FakeView()
    algo.attach_coupling_view(view)
    algo.set_measured_power(1234.0)
    algo._publish_coupling_snapshot(21.0, 5.0)
    assert view.published[-1]["power_w"] == 1234.0
    assert view.published[-1]["t_int"] == 21.0
    assert view.published[-1]["available"] is True

    algo.set_measured_power(None)
    algo._publish_coupling_snapshot(None, 5.0)
    assert view.published[-1]["power_w"] is None
    assert view.published[-1]["available"] is False


def test_save_state_includes_coupling():
    algo = make_smartpi()
    _load_k(algo, k=0.013)
    state = algo.save_state()
    assert "coupling_state" in state
    assert "N" in state["coupling_state"]["edges"]


def test_persistence_round_trip():
    algo = make_smartpi()
    _load_k(algo, k=0.013)
    state = algo.save_state()
    restored = make_smartpi()
    restored.load_state(state)
    assert abs(restored.coupling_est.k("N") - 0.013) < 1e-9


def test_no_edges_save_state_regression():
    """An instance with no coupling persists an empty edge map (identity)."""
    algo = make_smartpi()
    state = algo.save_state()
    assert state["coupling_state"] == {"edges": {}}
