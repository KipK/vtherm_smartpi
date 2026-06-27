"""End-to-end room-network simulations (Scenario 1 & 2)."""

import math
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.room_coupling import (
    RoomCouplingCoordinator, EdgeConfig, TARGET_OUTSIDE, TARGET_ROOM,
)


class _FakeState:
    def __init__(self, state): self.state = state


class _FakeStates:
    def __init__(self): self._d = {}
    def set(self, e, s): self._d[e] = _FakeState(str(s))
    def get(self, e): return self._d.get(e)


class _FakeHass:
    def __init__(self): self.states = _FakeStates()


def _algo(hass, name):
    return SmartPI(hass=hass, cycle_min=10, minimal_activation_delay=0,
                   minimal_deactivation_delay=0, name=name, debug_mode=True)


def test_scenario1_outside_window_folds_and_reverts():
    hass = _FakeHass()
    hass.states.set("binary_sensor.window", "off")
    coord = RoomCouplingCoordinator(hass)
    algo = _algo(hass, "Room")
    view = coord.register_room("Room", [EdgeConfig(
        target_kind=TARGET_OUTSIDE, aperture_entity_id="binary_sensor.window",
        aperture_type="window")])
    algo.attach_coupling_view(view)
    algo.est.b = 0.008

    # Window CLOSED -> identity fold.
    b_eff, text_eff = algo._refresh_coupling_context(22.0, 7.0)
    assert b_eff == 0.008 and text_eff == 7.0

    # Window OPEN with a learned κ -> b_eff rises via √|Δ|, reference stays outside.
    algo.coupling_est._rls.ensure_edge("binary_sensor.window")
    algo.coupling_est._rls.set_value("binary_sensor.window", 0.02)
    algo.coupling_est._kind["binary_sensor.window"] = "outside"
    hass.states.set("binary_sensor.window", "on")
    for _ in range(80):
        b_eff, text_eff = algo._refresh_coupling_context(22.0, 7.0)
    assert b_eff > 0.008 + 0.02 * math.sqrt(15.0) - 5e-3
    assert abs(text_eff - 7.0) < 0.3

    # Close again -> slews back to identity.
    hass.states.set("binary_sensor.window", "off")
    for _ in range(80):
        b_eff, text_eff = algo._refresh_coupling_context(22.0, 7.0)
    assert abs(b_eff - 0.008) < 1e-6
    assert text_eff == 7.0


def test_scenario2_mixed_apertures_learn_and_mesh():
    hass = _FakeHass()
    # A connects to: room B (door), a sensed hall (door), and outside (window).
    hass.states.set("binary_sensor.door_ab", "on")
    hass.states.set("binary_sensor.door_hall", "on")
    hass.states.set("binary_sensor.window_a", "off")   # window shut for now
    hass.states.set("sensor.hall_temp", "19.0")
    coord = RoomCouplingCoordinator(hass)

    a = _algo(hass, "A"); b = _algo(hass, "B")
    a.est.a = b.est.a = 0.01
    a.est.b = b.est.b = 0.008

    va = coord.register_room("A", [
        EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B",
                   aperture_entity_id="binary_sensor.door_ab"),
        EdgeConfig(target_kind="sensor", neighbor_temp_sensor="sensor.hall_temp",
                   aperture_entity_id="binary_sensor.door_hall"),
        EdgeConfig(target_kind=TARGET_OUTSIDE, aperture_entity_id="binary_sensor.window_a",
                   aperture_type="window"),
    ])
    vb = coord.register_room("B", [
        EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="A",
                   aperture_entity_id="binary_sensor.door_ab"),
    ])
    a.attach_coupling_view(va); b.attach_coupling_view(vb)

    # B publishes a warm temperature; A reads it through the mesh.
    b._publish_coupling_snapshot(23.0, 5.0)
    a._publish_coupling_snapshot(20.0, 5.0)

    resolved = {e.edge_id: e for e in va.open_edges()}
    # Two apertures open (door_ab -> room B, door_hall -> sensed hall); window shut.
    assert set(resolved) == {"B", "binary_sensor.door_hall"}
    assert resolved["B"].neighbor_temp == 23.0
    assert resolved["binary_sensor.door_hall"].neighbor_temp == 19.0

    # Drive learning across BOTH open edges (old code would have held this).
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
    a._coupling_learn_allowed = lambda *args, **kw: True
    for i in range(40):
        a._update_coupling_learning(1.0, 20.0 + 0.04 * i, 5.0, VThermHvacMode_HEAT)
    assert a.coupling_est._rls.samples("B") > 0
    assert a.coupling_est._rls.samples("binary_sensor.door_hall") > 0

    # Now open the window too — three open edges fold together, b_eff strictly rises.
    hass.states.set("binary_sensor.window_a", "on")
    a.coupling_est._rls.ensure_edge("binary_sensor.window_a")
    a.coupling_est._rls.set_value("binary_sensor.window_a", 0.015)
    a.coupling_est._kind["binary_sensor.window_a"] = "outside"
    b_eff, _ = a._refresh_coupling_context(20.0, 5.0)
    assert b_eff > 0.008
