"""Joint multi-edge recursive least squares for room-coupling identification.

Solves r = Σ_j θ_j·x_j (linear in the open edges' coefficients) with per-edge
forgetting. The covariance P is stored sparsely as a dict-of-dicts keyed by
edge-id. Only the edges marked active in a given ``update`` are aged and
updated; closed edges are HELD (their rows/cols untouched), which prevents
covariance windup on unexcited directions. A high covariance diagonal means
low information on that edge (unidentifiable, e.g. always co-open collinear
edges) and is surfaced via ``variance``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class _Edge:
    theta: float = 0.0
    n: int = 0


class MultiEdgeRLS:
    """Sparse joint RLS over a dynamic set of edges."""

    def __init__(
        self,
        *,
        p0: float,
        lam: float,
        p_max: float,
        huber_c: float,
        theta_min: float,
        theta_max: float,
    ) -> None:
        self._p0 = float(p0)
        self._lam = float(lam)
        self._p_max = float(p_max)
        self._huber_c = float(huber_c)
        self._theta_min = float(theta_min)
        self._theta_max = float(theta_max)
        self._edges: dict[str, _Edge] = {}
        # Sparse covariance: _P[i][j]. Diagonal seeded to p0 on ensure_edge.
        self._P: dict[str, dict[str, float]] = {}

    # -- lifecycle ---------------------------------------------------------

    def ensure_edge(self, edge_id: str) -> None:
        if edge_id in self._edges:
            return
        self._edges[edge_id] = _Edge()
        self._P[edge_id] = {edge_id: self._p0}
        for other in self._edges:
            if other == edge_id:
                continue
            self._P[edge_id][other] = 0.0
            self._P[other][edge_id] = 0.0

    def drop_missing(self, valid_ids: set[str]) -> None:
        for edge_id in list(self._edges):
            if edge_id not in valid_ids:
                del self._edges[edge_id]
                self._P.pop(edge_id, None)
                for row in self._P.values():
                    row.pop(edge_id, None)

    # -- accessors ---------------------------------------------------------

    def value(self, edge_id: str) -> float:
        edge = self._edges.get(edge_id)
        return edge.theta if edge is not None else 0.0

    def variance(self, edge_id: str) -> float:
        row = self._P.get(edge_id)
        return row[edge_id] if row is not None else inf

    def samples(self, edge_id: str) -> int:
        edge = self._edges.get(edge_id)
        return edge.n if edge is not None else 0

    def edge_ids(self) -> list[str]:
        return list(self._edges)
