"""Tests for the RoomCouplingCoordinator and per-room views."""

from custom_components.vtherm_smartpi.smartpi.room_coupling import (
    EdgeConfig,
    ResolvedEdge,
    RoomCouplingCoordinator,
    TARGET_ROOM,
    TARGET_OUTSIDE,
    TARGET_SENSOR,
    build_edge_configs,
)
from custom_components.vtherm_smartpi.const import (
    CONF_CONN_NEIGHBOR_VTHERM,
    CONF_CONN_DOOR_SENSOR,
    CONF_CONN_TARGET_KIND,
    CONF_CONN_APERTURE_SENSOR,
    CONF_CONN_APERTURE_TYPE,
    CONF_CONN_OPEN_POLICY,
    CONF_CONN_NEIGHBOR_TEMP_SENSOR,
)


class _FakeState:
    def __init__(self, state):
        self.state = state


class _FakeStates:
    def __init__(self):
        self._d = {}

    def set(self, entity_id, state):
        self._d[entity_id] = _FakeState(state)

    def get(self, entity_id):
        return self._d.get(entity_id)


class _FakeHass:
    def __init__(self):
        self.states = _FakeStates()


def _snap(t_int, power, available=True):
    return {
        "t_int": t_int,
        "text": 5.0,
        "on_percent": 0.5,
        "power_w": power,
        "available": available,
    }


def test_edge_dedup_one_sided_declaration():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    coord.register_room("B", [])  # neighbour declares nothing
    assert len(coord._edges) == 1


def test_door_gate_and_power_aggregation():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    va = coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    vb = coord.register_room("B", [])
    va.publish(_snap(21.0, 100.0))
    vb.publish(_snap(23.0, 150.0))

    hass.states.set("binary_sensor.door", "off")
    assert va.any_open() is False
    assert coord.component_power_w("A") == 100.0  # isolated -> self only

    hass.states.set("binary_sensor.door", "on")
    assert va.any_open() is True
    open_edges = va.open_edges()
    assert len(open_edges) == 1
    assert open_edges[0].neighbor_temp == 23.0
    assert coord.component_power_w("A") == 250.0  # whole component


def test_unknown_door_state_fails_closed():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    va = coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    coord.register_room("B", []).publish(_snap(23.0, 150.0))
    va.publish(_snap(21.0, 100.0))
    # No state set for the door -> treated as closed.
    assert va.any_open() is False


def test_unavailable_neighbor_is_isolated():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    va = coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    vb = coord.register_room("B", [])
    va.publish(_snap(21.0, 100.0))
    vb.publish(_snap(None, 150.0, available=False))
    hass.states.set("binary_sensor.door", "on")
    assert va.any_open() is False
    assert coord.component_power_w("A") == 100.0


def test_late_neighbor_resolution():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    va = coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    hass.states.set("binary_sensor.door", "on")
    va.publish(_snap(21.0, 100.0))
    # B not registered yet -> edge open but no available neighbour.
    assert va.any_open() is False
    vb = coord.register_room("B", [])
    vb.publish(_snap(23.0, 150.0))
    assert va.any_open() is True


def test_unregister_drops_edges():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    coord.register_room("B", [])
    coord.unregister_room("A")
    assert coord._edges == {}
    assert "A" not in coord._nodes


def test_reregister_replaces_own_edges_keeps_neighbor_declared():
    hass = _FakeHass()
    coord = RoomCouplingCoordinator(hass)
    coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B", aperture_entity_id="binary_sensor.door")])
    coord.register_room("B", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="A", aperture_entity_id="binary_sensor.door")])
    # A drops its declaration; B still declares the edge -> it survives.
    coord.register_room("A", [])
    assert len(coord._edges) == 1


def test_edge_id_room_vs_aperture():
    room = EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B",
                      aperture_entity_id="binary_sensor.door_ab")
    out = EdgeConfig(target_kind=TARGET_OUTSIDE,
                     aperture_entity_id="binary_sensor.window_1")
    assert room.edge_id == "B"
    assert out.edge_id == "binary_sensor.window_1"


def test_build_edge_configs_legacy_shape():
    """Legacy {neighbor_vtherm, door_sensor} -> room/door/model."""
    raw = [{CONF_CONN_NEIGHBOR_VTHERM: "B",
            CONF_CONN_DOOR_SENSOR: "binary_sensor.door_ab"}]
    edges, ids = build_edge_configs(raw)
    assert len(edges) == 1
    e = edges[0]
    assert e.target_kind == TARGET_ROOM
    assert e.neighbor_uid == "B"
    assert e.aperture_entity_id == "binary_sensor.door_ab"
    assert e.aperture_type == "door"
    assert e.open_policy == "model"
    assert ids == {"B"}


def test_build_edge_configs_new_shapes():
    raw = [
        {CONF_CONN_TARGET_KIND: TARGET_OUTSIDE,
         CONF_CONN_APERTURE_SENSOR: "binary_sensor.window_1",
         CONF_CONN_APERTURE_TYPE: "window",
         CONF_CONN_OPEN_POLICY: "trip_off"},
        {CONF_CONN_TARGET_KIND: TARGET_SENSOR,
         CONF_CONN_NEIGHBOR_TEMP_SENSOR: "sensor.hall_temp",
         CONF_CONN_APERTURE_SENSOR: "binary_sensor.door_hall"},
    ]
    edges, ids = build_edge_configs(raw)
    assert edges[0].target_kind == TARGET_OUTSIDE
    assert edges[0].open_policy == "trip_off"
    assert edges[1].target_kind == TARGET_SENSOR
    assert edges[1].neighbor_temp_sensor == "sensor.hall_temp"
    assert ids == {"binary_sensor.window_1", "binary_sensor.door_hall"}


def test_outside_and_sensor_edges_resolve():
    hass = _FakeHass()
    hass.states.set("binary_sensor.window_1", "on")     # open
    hass.states.set("binary_sensor.door_hall", "on")    # open
    hass.states.set("sensor.hall_temp", "19.5")
    coord = RoomCouplingCoordinator(hass)
    edges = [
        EdgeConfig(target_kind=TARGET_OUTSIDE,
                   aperture_entity_id="binary_sensor.window_1",
                   aperture_type="window"),
        EdgeConfig(target_kind=TARGET_SENSOR,
                   aperture_entity_id="binary_sensor.door_hall",
                   neighbor_temp_sensor="sensor.hall_temp"),
    ]
    coord.register_room("A", edges)
    resolved = {e.edge_id: e for e in coord.open_edges("A")}
    assert resolved["binary_sensor.window_1"].target_kind == TARGET_OUTSIDE
    assert resolved["binary_sensor.window_1"].neighbor_temp is None
    assert resolved["binary_sensor.door_hall"].neighbor_temp == 19.5
    assert coord.any_open("A") is True


def test_controlled_edge_exposes_neighbor_k_for_consensus():
    hass = _FakeHass()
    hass.states.set("binary_sensor.door_ab", "on")
    coord = RoomCouplingCoordinator(hass)
    coord.register_room("A", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="B",
                                         aperture_entity_id="binary_sensor.door_ab")])
    coord.register_room("B", [EdgeConfig(target_kind=TARGET_ROOM, neighbor_uid="A",
                                         aperture_entity_id="binary_sensor.door_ab")])
    coord.publish("B", {"t_int": 21.0, "available": True, "power_w": 50.0,
                        "coupling_k_by_neighbor": {"A": {"k": 0.07, "reliable": True}}})
    coord.publish("A", {"t_int": 20.0, "available": True})
    edge = coord.open_edges("A")[0]
    assert edge.neighbor_uid == "B"
    assert edge.neighbor_temp == 21.0
    assert edge.neighbor_k == 0.07
    assert edge.neighbor_reliable is True


def test_closed_aperture_not_returned():
    hass = _FakeHass()
    hass.states.set("binary_sensor.window_1", "off")
    coord = RoomCouplingCoordinator(hass)
    coord.register_room("A", [EdgeConfig(target_kind=TARGET_OUTSIDE,
                                         aperture_entity_id="binary_sensor.window_1")])
    assert coord.open_edges("A") == []
    assert coord.any_open("A") is False
