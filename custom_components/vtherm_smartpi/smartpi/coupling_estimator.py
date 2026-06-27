"""Inter-room coupling estimator for SmartPI.

Estimates the per-edge coupling coefficient for each declared neighbour from
the base-model residual, using a joint multi-edge Recursive Least Squares
(RLS) filter:

    m = (T_i - T_i_prev) / dt_min                 measured slope (°C/min)
    p = a_i·u - b_i·(T_i - Text)                   base 1R1C prediction
    r = m - p = Σ_j θ_j·x_j + noise

where x_j is the per-edge regressor (Task 9) and θ_j is the per-edge
coefficient estimated by the joint RLS. Multiple open edges are handled
simultaneously (no longer held to single-aperture case).
"""

from __future__ import annotations

import logging
from math import isfinite, sqrt

from .rls import MultiEdgeRLS
from .const import (
    COUPLING_DT_MIN_C,
    COUPLING_K_MAX,
    COUPLING_KAPPA_MAX,
    COUPLING_MIN_SAMPLES,
    COUPLING_RESIDUAL_MAX_C_MIN,
    COUPLING_RLS_HUBER_C,
    COUPLING_RLS_LAMBDA,
    COUPLING_RLS_P0,
    COUPLING_RLS_P_MAX,
    COUPLING_RLS_VAR_RELIABLE,
    clamp,
)
from .room_coupling import TARGET_OUTSIDE, TARGET_ROOM

_LOGGER = logging.getLogger(__name__)


def edge_regressor(target_kind: str, t_i: float, t_j: float) -> float:
    """Per-cycle regressor x_j for one open edge (linear, or √|Δ|-law outside)."""
    delta = t_i - t_j
    if target_kind == TARGET_OUTSIDE:
        return -(delta) * sqrt(abs(delta)) if delta != 0.0 else 0.0
    return -delta


def edge_k_instant(target_kind: str, coeff: float, t_i: float, t_j: float) -> float:
    """Instantaneous conductance k handed to the fold (κ·√|Δ| for outside)."""
    if target_kind == TARGET_OUTSIDE:
        return coeff * sqrt(abs(t_i - t_j))
    return coeff


class CouplingEstimator:
    """Learns per-edge coupling for one room via a joint multi-edge RLS."""

    def __init__(self, name: str) -> None:
        self._name = name
        # κ for outside edges can exceed K_MAX before ×√|Δ|; use the larger cap
        # as the RLS theta ceiling and re-clamp the instantaneous k at use.
        self._rls = MultiEdgeRLS(
            p0=COUPLING_RLS_P0,
            lam=COUPLING_RLS_LAMBDA,
            p_max=COUPLING_RLS_P_MAX,
            huber_c=COUPLING_RLS_HUBER_C,
            theta_min=0.0,
            theta_max=max(COUPLING_K_MAX, COUPLING_KAPPA_MAX),
        )
        self._kind: dict[str, str] = {}      # edge_id -> target_kind
        self._last_tin: float | None = None

    # -- learning ----------------------------------------------------------

    def update(
        self,
        *,
        dt_min: float,
        tin: float | None,
        text: float | None,
        u: float,
        a: float,
        b: float,
        open_edges,
        allow_learn: bool,
    ) -> None:
        if dt_min <= 0.0 or tin is None or not isfinite(tin):
            return
        prev_tin = self._last_tin
        self._last_tin = float(tin)
        if not allow_learn or text is None or prev_tin is None:
            return
        if not (isfinite(text) and isfinite(a) and isfinite(b)):
            return

        # Base-model residual (no coupling): r = m - p.
        m = (tin - prev_tin) / dt_min
        p = a * u - b * (tin - text)
        r = clamp(m - p, -COUPLING_RESIDUAL_MAX_C_MIN, COUPLING_RESIDUAL_MAX_C_MIN)

        regressors: dict[str, float] = {}
        for edge in open_edges:
            t_j = text if edge.target_kind == TARGET_OUTSIDE else edge.neighbor_temp
            if t_j is None or not isfinite(t_j):
                continue
            if abs(tin - t_j) < COUPLING_DT_MIN_C:
                continue
            self._kind[edge.edge_id] = edge.target_kind
            regressors[edge.edge_id] = edge_regressor(edge.target_kind, tin, t_j)

        if regressors:
            self._rls.update(regressors, r)
        # Consensus runs even for open-but-unexcited edges — its job is to
        # rescue an edge that has no local excitation from the neighbour's data.
        self._apply_consensus(open_edges)

    def _apply_consensus(self, open_edges) -> None:
        from .const import COUPLING_CONSENSUS_GAIN

        for edge in open_edges:
            if edge.target_kind != TARGET_ROOM:
                continue
            if edge.neighbor_k is None or not edge.neighbor_reliable:
                continue
            if self.reliable(edge.edge_id):
                continue  # well-excited locally -> keep room-local value
            current = self._rls.value(edge.edge_id)
            target = float(edge.neighbor_k)
            nudged = current + COUPLING_CONSENSUS_GAIN * (target - current)
            self._rls.set_value(edge.edge_id, clamp(nudged, 0.0, COUPLING_K_MAX))

    # -- accessors ---------------------------------------------------------

    def coeff(self, edge_id: str) -> float:
        return self._rls.value(edge_id)

    def reliable(self, edge_id: str) -> bool:
        return self._rls.reliable(
            edge_id, var_max=COUPLING_RLS_VAR_RELIABLE, min_samples=COUPLING_MIN_SAMPLES
        )

    def k(self, edge_id: str, t_i: float, t_j: float, target_kind: str) -> float:
        return clamp(
            edge_k_instant(target_kind, self._rls.value(edge_id), t_i, t_j),
            0.0,
            COUPLING_K_MAX,
        )

    def edges_diag(self) -> dict:
        return {
            edge_id: {
                "coeff": round(self._rls.value(edge_id), 5),
                "var": round(self._rls.variance(edge_id), 4),
                "reliable": self.reliable(edge_id),
                "n": self._rls.samples(edge_id),
                "kind": self._kind.get(edge_id, "room"),
            }
            for edge_id in self._rls.edge_ids()
        }

    def prune(self, valid_edge_ids: set) -> None:
        self._rls.drop_missing(set(valid_edge_ids))
        for edge_id in list(self._kind):
            if edge_id not in valid_edge_ids:
                del self._kind[edge_id]

    # -- persistence -------------------------------------------------------

    def save_state(self) -> dict:
        """Serialise coupling state via the RLS filter."""
        return {"rls": self._rls.save_state(), "kind": dict(self._kind)}

    def load_state(self, state: dict) -> None:
        """Restore coupling state (best-effort, NaN-safe)."""
        if not state:
            return
        rls_state = state.get("rls")
        if rls_state:
            self._rls.load_state(rls_state)
        kind = state.get("kind")
        if isinstance(kind, dict):
            self._kind.update({str(k): str(v) for k, v in kind.items()})
