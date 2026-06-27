"""Room coupling coordinator for SmartPI.

Models connected rooms as a coupled RC network. When a door connecting room i
and room j is open, the two rooms exchange heat at a learned rate ``k_ij``. The
coordinator is a hass-level singleton that:

  - tracks the live topology (edges + door sensors) across all SmartPI rooms,
  - holds the latest per-room snapshot (temperature, power, command, door view),
  - exposes each room a :class:`RoomView` facade so the controller can read its
    neighbours without ever touching the registry or other algos directly,
  - aggregates the total power across the open-door connected component.

Rooms tick on independent timers and HA runs single-threaded async, so a room
reads the neighbour's *most recently published* snapshot — at most one SmartPI
recalc interval stale. This one-cycle lag is harmless: ``k`` is small and slow.

Edges are unordered (``A-B`` == ``B-A``) and may be declared from either side
(one-sided declaration is enough — the coordinator makes them bidirectional).

The effective-parameter fold (``compute_effective_params``) collapses the whole
coupled loss back into a single 1R1C loss with ``(b_eff, Text_eff)`` so the rest
of the control law stays unchanged. See the module docstring math in the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.core import HomeAssistant


COORDINATOR_DATA_KEY = "room_coupling_coordinator"

TARGET_ROOM = "room"
TARGET_SENSOR = "sensor"
TARGET_OUTSIDE = "outside"


# ---------------------------------------------------------------------------
# Lightweight data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeConfig:
    """A connection declared by a room: a typed neighbour + the aperture sensor."""

    target_kind: str
    aperture_entity_id: str
    neighbor_uid: str | None = None
    neighbor_temp_sensor: str | None = None
    aperture_type: str = "door"
    open_policy: str = "model"

    @property
    def edge_id(self) -> str:
        if self.target_kind == TARGET_ROOM and self.neighbor_uid:
            return self.neighbor_uid
        return self.aperture_entity_id


@dataclass
class _Edge:
    """Internal bidirectional edge between two room uids.

    ``door_by`` maps the declaring room uid -> its door entity id, so removing a
    room's declaration cleanly drops only its side; the edge survives as long as
    at least one side still declares it.
    """

    key: frozenset
    door_by: dict[str, str] = field(default_factory=dict)

    def door_entity(self) -> str | None:
        """Return the door entity id for this edge (deterministic pick)."""
        if not self.door_by:
            return None
        # Prefer a stable, deterministic choice when both sides declare a door.
        for uid in sorted(self.door_by):
            if self.door_by[uid]:
                return self.door_by[uid]
        return None

    def other(self, uid: str) -> str:
        """Return the uid on the opposite side of this edge from *uid*."""
        for member in self.key:
            if member != uid:
                return member
        return uid  # self-loop guard (should never happen)


@dataclass
class _RoomNode:
    """Registered room: its snapshot plus the edges it participates in."""

    uid: str
    snapshot: dict | None = None


@dataclass(frozen=True)
class ResolvedEdge:
    """A room's open-edge view of one neighbour for this cycle."""

    edge_id: str
    target_kind: str
    aperture_type: str
    open_policy: str
    neighbor_temp: float | None
    neighbor_power_w: float | None
    neighbor_uid: str | None = None
    neighbor_k: float | None = None
    neighbor_reliable: bool = False


def build_edge_configs(connections):
    """Parse raw connection dicts (new or legacy shape) into EdgeConfigs.

    Returns (edges, edge_ids). Legacy entries — ``{neighbor_vtherm_entity,
    connection_door_sensor}`` — map to a room/door/model edge.
    """
    from ..const import (
        CONF_CONN_NEIGHBOR_VTHERM,
        CONF_CONN_DOOR_SENSOR,
        CONF_CONN_TARGET_KIND,
        CONF_CONN_NEIGHBOR_TEMP_SENSOR,
        CONF_CONN_APERTURE_SENSOR,
        CONF_CONN_APERTURE_TYPE,
        CONF_CONN_OPEN_POLICY,
    )

    edges: list[EdgeConfig] = []
    ids: set[str] = set()
    for conn in connections or []:
        target_kind = conn.get(CONF_CONN_TARGET_KIND)
        aperture = conn.get(CONF_CONN_APERTURE_SENSOR) or conn.get(
            CONF_CONN_DOOR_SENSOR
        )
        if target_kind is None:
            # Legacy shape: a room neighbour + door sensor.
            neighbor_uid = conn.get(CONF_CONN_NEIGHBOR_VTHERM)
            if not (neighbor_uid and aperture):
                continue
            edge = EdgeConfig(
                target_kind=TARGET_ROOM,
                aperture_entity_id=aperture,
                neighbor_uid=neighbor_uid,
            )
        else:
            if not aperture:
                continue
            edge = EdgeConfig(
                target_kind=target_kind,
                aperture_entity_id=aperture,
                neighbor_uid=conn.get(CONF_CONN_NEIGHBOR_VTHERM),
                neighbor_temp_sensor=conn.get(CONF_CONN_NEIGHBOR_TEMP_SENSOR),
                aperture_type=conn.get(CONF_CONN_APERTURE_TYPE, "door"),
                open_policy=conn.get(CONF_CONN_OPEN_POLICY, "model"),
            )
        edges.append(edge)
        ids.add(edge.edge_id)
    return edges, ids


# ---------------------------------------------------------------------------
# Pure effective-parameter fold
# ---------------------------------------------------------------------------


def compute_effective_params(
    b_base: float,
    text: float | None,
    sum_k: float,
    sum_kt: float,
) -> tuple[float, float | None]:
    """Fold coupling into an equivalent 1R1C ``(b_eff, Text_eff)``.

    ``sum_k  = Σ k_ij·open_ij`` and ``sum_kt = Σ k_ij·T_j·open_ij`` (already
    door-gated and, in the controller, EMA-slewed). With no open edge both are
    zero and the result is identity ``(b_base, text)`` — byte-identical to the
    uncoupled behaviour.
    """
    b_eff = b_base + max(sum_k, 0.0)
    if text is None or b_eff <= 0.0 or sum_k <= 0.0:
        return b_eff, text
    text_eff = (b_base * text + sum_kt) / b_eff
    return b_eff, text_eff


# ---------------------------------------------------------------------------
# Per-room facade
# ---------------------------------------------------------------------------


class RoomView:
    """Per-room handle the SmartPI algo uses to talk to the coordinator."""

    def __init__(self, coordinator: "RoomCouplingCoordinator", uid: str) -> None:
        self._coord = coordinator
        self._uid = uid

    @property
    def uid(self) -> str:
        """Return the room's target VTherm unique id."""
        return self._uid

    def publish(self, snapshot: dict) -> None:
        """Publish this room's latest snapshot for neighbours to read."""
        self._coord.publish(self._uid, snapshot)

    def any_open(self) -> bool:
        """Return True if any connected door is open with an available neighbour."""
        return self._coord.any_open(self._uid)

    def open_edges(self) -> list[ResolvedEdge]:
        """Return the resolved open edges for this room this cycle."""
        return self._coord.open_edges(self._uid)

    def component_power_w(self) -> float:
        """Return total power across this room's open-door connected component."""
        return self._coord.component_power_w(self._uid)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class RoomCouplingCoordinator:
    """Hass-level singleton tracking topology and per-room snapshots."""

    def __init__(self, hass: "HomeAssistant") -> None:
        self._hass = hass
        self._nodes: dict[str, _RoomNode] = {}
        self._edges: dict[frozenset, _Edge] = {}

    # -- lifecycle ---------------------------------------------------------

    def register_room(self, uid: str, edges: list[EdgeConfig]) -> RoomView:
        """Register (or re-register) a room and its declared edges.

        Re-registration replaces only *this* room's edge declarations; edges
        also declared by the neighbour survive. Returns a :class:`RoomView`.
        """
        node = self._nodes.get(uid)
        if node is None:
            node = _RoomNode(uid=uid)
            self._nodes[uid] = node

        # Drop this room's previous declarations from all edges.
        for edge in list(self._edges.values()):
            if uid in edge.door_by:
                del edge.door_by[uid]
        # Remove edges nobody declares anymore.
        self._edges = {
            key: edge for key, edge in self._edges.items() if edge.door_by
        }

        # Apply the new declarations.
        for cfg in edges:
            if not cfg.neighbor_uid or cfg.neighbor_uid == uid:
                continue
            key = frozenset({uid, cfg.neighbor_uid})
            edge = self._edges.get(key)
            if edge is None:
                edge = _Edge(key=key)
                self._edges[key] = edge
            edge.door_by[uid] = cfg.aperture_entity_id

        return RoomView(self, uid)

    def unregister_room(self, uid: str) -> None:
        """Remove a room and drop its edge declarations."""
        self._nodes.pop(uid, None)
        for edge in list(self._edges.values()):
            edge.door_by.pop(uid, None)
        self._edges = {
            key: edge for key, edge in self._edges.items() if edge.door_by
        }

    # -- snapshots ---------------------------------------------------------

    def publish(self, uid: str, snapshot: dict) -> None:
        """Store a room's latest snapshot (atomic slot swap)."""
        node = self._nodes.get(uid)
        if node is not None:
            node.snapshot = snapshot

    def _neighbor_available(self, neighbor_uid: str) -> tuple[bool, dict | None]:
        node = self._nodes.get(neighbor_uid)
        if node is None or node.snapshot is None:
            return False, None
        snap = node.snapshot
        available = bool(snap.get("available")) and snap.get("t_int") is not None
        return available, snap

    def _is_door_open(self, door_entity_id: str | None) -> bool:
        """Read the door sensor: open iff state == 'on' (fail-safe to closed)."""
        if not door_entity_id:
            return False
        state = self._hass.states.get(door_entity_id)
        if state is None:
            return False
        return str(state.state).lower() == "on"

    # -- queries -----------------------------------------------------------

    def _edges_for(self, uid: str) -> list[_Edge]:
        return [edge for edge in self._edges.values() if uid in edge.key]

    def open_edges(self, uid: str) -> list[ResolvedEdge]:
        """Return resolved open edges (door open + neighbour available)."""
        resolved: list[ResolvedEdge] = []
        for edge in self._edges_for(uid):
            neighbor_uid = edge.other(uid)
            door_open = self._is_door_open(edge.door_entity())
            if not door_open:
                continue
            available, snap = self._neighbor_available(neighbor_uid)
            if not available or snap is None:
                continue
            resolved.append(
                ResolvedEdge(
                    edge_id=neighbor_uid,
                    target_kind=TARGET_ROOM,
                    aperture_type="door",
                    open_policy="model",
                    neighbor_temp=snap.get("t_int"),
                    neighbor_power_w=snap.get("power_w"),
                    neighbor_uid=neighbor_uid,
                )
            )
        return resolved

    def any_open(self, uid: str) -> bool:
        """Return True if this room has at least one open, available edge."""
        for edge in self._edges_for(uid):
            if not self._is_door_open(edge.door_entity()):
                continue
            available, _ = self._neighbor_available(edge.other(uid))
            if available:
                return True
        return False

    def component_power_w(self, uid: str) -> float:
        """Sum power (watts) over the open-door connected component of *uid*.

        BFS over edges whose door is open and whose endpoints are both
        registered + available. Rooms with no power sensor contribute 0.
        """
        if uid not in self._nodes:
            return 0.0
        seen: set[str] = set()
        stack = [uid]
        total = 0.0
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self._nodes.get(current)
            if node is not None and node.snapshot is not None:
                power = node.snapshot.get("power_w")
                if isinstance(power, (int, float)):
                    total += float(power)
            for edge in self._edges_for(current):
                if not self._is_door_open(edge.door_entity()):
                    continue
                neighbor_uid = edge.other(current)
                if neighbor_uid in seen:
                    continue
                available, _ = self._neighbor_available(neighbor_uid)
                if available:
                    stack.append(neighbor_uid)
        return total


# ---------------------------------------------------------------------------
# Accessor
# ---------------------------------------------------------------------------


def get_coordinator(
    hass: "HomeAssistant",
    domain_data: dict,
) -> RoomCouplingCoordinator:
    """Return the shared coordinator, creating it on first use."""
    coordinator = domain_data.get(COORDINATOR_DATA_KEY)
    if coordinator is None:
        coordinator = RoomCouplingCoordinator(hass)
        domain_data[COORDINATOR_DATA_KEY] = coordinator
    return coordinator
