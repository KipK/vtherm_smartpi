"""Algo-level integration tests for room coupling.

Exercises the coupling wiring inside SmartPI without driving a full control
cycle: effective-parameter folding, the diagnostics block, snapshot publishing,
and persistence (including the no-coupling regression to identity).
"""

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.room_coupling import ResolvedEdge
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


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
                edge_id="N",
                target_kind="room",
                aperture_type="door",
                open_policy="model",
                neighbor_temp=self._neighbor_temp,
                neighbor_power_w=self._power,
                neighbor_uid="N",
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
    assert "rls" in state["coupling_state"]
    assert "N" in state["coupling_state"]["rls"]["edges"]


def test_persistence_round_trip():
    algo = make_smartpi()
    _load_k(algo, k=0.013)
    state = algo.save_state()
    restored = make_smartpi()
    restored.load_state(state)
    assert abs(restored.coupling_est.coeff("N") - 0.013) < 1e-9


def test_no_edges_save_state_regression():
    """An instance with no coupling persists an empty edge map (identity)."""
    algo = make_smartpi()
    state = algo.save_state()
    assert state["coupling_state"]["rls"]["edges"] == {}
    assert state["coupling_state"]["kind"] == {}


def test_learning_accepts_multiple_open_edges():
    algo = make_smartpi()
    e1 = ResolvedEdge(edge_id="B", target_kind="room", aperture_type="door",
                      open_policy="model", neighbor_temp=24.0, neighbor_power_w=None,
                      neighbor_uid="B")
    e2 = ResolvedEdge(edge_id="C", target_kind="room", aperture_type="door",
                      open_policy="model", neighbor_temp=18.0, neighbor_power_w=None,
                      neighbor_uid="C")

    class _View:
        uid = "A"
        def publish(self, snap): pass
        def any_open(self): return True
        def open_edges(self): return [e1, e2]
        def component_power_w(self): return 0.0

    algo.attach_coupling_view(_View())
    # Make the base model "reliable enough" for learning to proceed.
    algo.est.a = 0.01
    algo.est.b = 0.008
    # Prime + drive a few cycles (allow_learn is gated internally on base reliability;
    # force the gate open by monkeypatching the precondition).
    algo._coupling_learn_allowed = lambda *a, **k: True  # see Step 3
    for i in range(20):
        algo._update_coupling_learning(1.0, 20.0 + 0.05 * i, 5.0, VThermHvacMode_HEAT)
    assert algo.coupling_est._rls.samples("B") > 0
    assert algo.coupling_est._rls.samples("C") > 0


def test_snapshot_includes_room_edge_k_map():
    algo = make_smartpi()
    captured = {}

    class _View:
        uid = "A"
        def publish(self, snap): captured.update(snap)
        def any_open(self): return False
        def open_edges(self): return []
        def component_power_w(self): return 0.0

    algo.attach_coupling_view(_View())
    algo.coupling_est._rls.ensure_edge("B")
    algo.coupling_est._rls.set_value("B", 0.07)
    algo.coupling_est._kind["B"] = "room"
    algo._publish_coupling_snapshot(20.0, 5.0)
    assert "coupling_k_by_neighbor" in captured
    assert abs(captured["coupling_k_by_neighbor"]["B"]["k"] - 0.07) < 1e-9
