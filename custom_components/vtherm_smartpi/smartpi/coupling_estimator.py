"""Inter-room coupling estimator for SmartPI.

Estimates the open-door coupling coefficient ``k_ij`` (min^-1) between a room and
each declared neighbour, from the *base-model residual*:

    m = (T_i - T_i_prev) / dt_min                 measured slope (°C/min)
    p = a_i·u - b_i·(T_i - Text)                   base 1R1C prediction (no coupling)
    r = m - p = -Σ_j k_ij·(T_i - T_j)·open + noise

For a single open edge with a sufficient gradient:  k_ij = -r / (T_i - T_j).
With several doors open at once a single residual cannot separate the edges
(one equation, many unknowns), so learning is HELD whenever more than one door
is open — each edge is learned opportunistically while it is the sole open door.

``k_ij`` is a *structural* property of the doorway. It is learned only while the
door is open AND the base model is reliable ("seed from base model"), and is
otherwise HELD (never decayed) so it is remembered for the next time the door
opens. The door-state gate (``open_ij``) — not a decay — turns the contribution
on and off. Robustness mirrors :mod:`ab_estimator` (median/MAD reliability gate)
and the values are clamped to ``[COUPLING_K_MIN, COUPLING_K_MAX]``.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, Deque

from .ab_drift import robust_mad, robust_median
from .const import (
    COUPLING_EMA_ALPHA,
    COUPLING_HIST_MAX,
    COUPLING_K_MAX,
    COUPLING_K_MIN,
    COUPLING_MAD_RATIO_MAX,
    COUPLING_MIN_SAMPLES,
    COUPLING_RESIDUAL_MAX_C_MIN,
    COUPLING_DT_MIN_C,
    clamp,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .room_coupling import ResolvedEdge

_LOGGER = logging.getLogger(__name__)


@dataclass
class _EdgeState:
    """Per-neighbour coupling state."""

    k: float = 0.0
    reliable: bool = False
    n_ok: int = 0
    hist: Deque[float] = field(default_factory=lambda: deque(maxlen=COUPLING_HIST_MAX))


class CouplingEstimator:
    """Learns per-edge coupling ``k_ij`` for one room."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._edges: dict[str, _EdgeState] = {}
        self._last_tin: float | None = None

    # -- accessors ---------------------------------------------------------

    def k(self, neighbor_uid: str) -> float:
        """Return the live coupling estimate for an edge (0 if unknown)."""
        edge = self._edges.get(neighbor_uid)
        return edge.k if edge is not None else 0.0

    def reliable(self, neighbor_uid: str) -> bool:
        """Return whether an edge's coupling estimate is reliable."""
        edge = self._edges.get(neighbor_uid)
        return bool(edge.reliable) if edge is not None else False

    def edges_diag(self) -> dict[str, dict]:
        """Return a per-edge diagnostic snapshot."""
        return {
            uid: {"k": round(e.k, 5), "reliable": e.reliable, "n": e.n_ok}
            for uid, e in self._edges.items()
        }

    def prune(self, valid_neighbor_uids: set[str]) -> None:
        """Drop persisted edges no longer present in the configuration."""
        for uid in list(self._edges):
            if uid not in valid_neighbor_uids:
                del self._edges[uid]

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
        open_edges: "list[ResolvedEdge]",
        allow_learn: bool,
    ) -> None:
        """Update coupling estimates for one control cycle.

        ``open_edges`` are the door-open, neighbour-available edges resolved by
        the coordinator. ``allow_learn`` must be True only when the base model is
        reliable and the regime is not perturbed/degraded.

        Learning is restricted to the single-open-door case: with two or more
        doors open the residual is not separable, so all edges are held.
        """
        # Only advance the slope reference on real (dt>0) cycles so the measured
        # slope always pairs with the matching dt.
        if dt_min <= 0.0 or tin is None or not isfinite(tin):
            return
        prev_tin = self._last_tin
        self._last_tin = float(tin)

        if not allow_learn or text is None or prev_tin is None:
            return
        if not (isfinite(text) and isfinite(a) and isfinite(b)):
            return

        # Identifiability: only learn when exactly one door is open.
        if len(open_edges) != 1:
            return
        edge = open_edges[0]
        if edge.neighbor_temp is None or not isfinite(edge.neighbor_temp):
            return
        delta_t = tin - edge.neighbor_temp
        if abs(delta_t) < COUPLING_DT_MIN_C:
            return

        # Measured slope vs base-model prediction (no coupling, no d_hat).
        m = (tin - prev_tin) / dt_min
        p = a * u - b * (tin - text)
        r = clamp(m - p, -COUPLING_RESIDUAL_MAX_C_MIN, COUPLING_RESIDUAL_MAX_C_MIN)

        k_sample = clamp(-r / delta_t, COUPLING_K_MIN, COUPLING_K_MAX)
        self._accept_sample(edge.neighbor_uid, k_sample)

    def _accept_sample(self, neighbor_uid: str, k_sample: float) -> None:
        edge = self._edges.get(neighbor_uid)
        if edge is None:
            edge = _EdgeState()
            self._edges[neighbor_uid] = edge

        edge.hist.append(k_sample)
        # EMA toward the new sample for a smooth live value.
        edge.k = clamp(
            (1.0 - COUPLING_EMA_ALPHA) * edge.k + COUPLING_EMA_ALPHA * k_sample,
            COUPLING_K_MIN,
            COUPLING_K_MAX,
        )
        edge.n_ok += 1

        # Reliability via median/MAD (mirrors ABEstimator gating).
        if edge.n_ok >= COUPLING_MIN_SAMPLES and len(edge.hist) >= COUPLING_MIN_SAMPLES:
            values = list(edge.hist)
            median = robust_median(values)
            mad = robust_mad(values, median)
            if median is not None and median > 1e-9 and mad is not None:
                edge.reliable = (mad / median) <= COUPLING_MAD_RATIO_MAX
            else:
                # Near-zero coupling with tight spread is a reliable "no coupling".
                edge.reliable = mad is not None and mad <= 1e-3
        else:
            edge.reliable = False

    # -- persistence -------------------------------------------------------

    def save_state(self) -> dict:
        """Serialise per-edge coupling state keyed by neighbour uid."""
        return {
            "edges": {
                uid: {
                    "k": e.k,
                    "reliable": e.reliable,
                    "n_ok": e.n_ok,
                    "hist": list(e.hist),
                }
                for uid, e in self._edges.items()
            }
        }

    def load_state(self, state: dict) -> None:
        """Restore per-edge coupling state (best-effort, NaN-safe)."""
        if not state:
            return
        edges = state.get("edges")
        if not isinstance(edges, dict):
            return
        for uid, raw in edges.items():
            if not isinstance(raw, dict):
                continue
            edge = _EdgeState()
            try:
                k = float(raw.get("k", 0.0))
                edge.k = clamp(k, COUPLING_K_MIN, COUPLING_K_MAX) if isfinite(k) else 0.0
                edge.reliable = bool(raw.get("reliable", False))
                edge.n_ok = int(raw.get("n_ok", 0))
                hist = raw.get("hist", [])
                if isinstance(hist, list):
                    edge.hist = deque(
                        (float(v) for v in hist if isinstance(v, (int, float)) and isfinite(float(v))),
                        maxlen=COUPLING_HIST_MAX,
                    )
            except (TypeError, ValueError):
                continue
            self._edges[str(uid)] = edge
