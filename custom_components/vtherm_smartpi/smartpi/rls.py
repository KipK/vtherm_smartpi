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

    def reset_edge(self, edge_id: str) -> None:
        if edge_id not in self._edges:
            return
        for other in self._P[edge_id]:
            self._P[edge_id][other] = 0.0
            self._P[other][edge_id] = 0.0
        self._P[edge_id][edge_id] = self._p0

    def set_value(self, edge_id: str, theta: float) -> None:
        self.ensure_edge(edge_id)
        self._edges[edge_id].theta = float(theta)

    def seed_confidence(self, edge_id: str, *, n: int, var: float) -> None:
        """Seed an edge with a sample count and low variance (migration/tests)."""
        self.ensure_edge(edge_id)
        self._edges[edge_id].n = int(n)
        self._P[edge_id][edge_id] = float(var)

    def reliable(self, edge_id: str, *, var_max: float, min_samples: int) -> bool:
        edge = self._edges.get(edge_id)
        if edge is None:
            return False
        return self.variance(edge_id) <= var_max and edge.n >= min_samples

    def save_state(self) -> dict:
        return {
            "edges": {e: {"theta": s.theta, "n": s.n} for e, s in self._edges.items()},
            "P": {i: dict(row) for i, row in self._P.items()},
        }

    def load_state(self, state: dict) -> None:
        if not isinstance(state, dict) or not state:
            return  # best-effort: a corrupt/truncated persisted blob must not crash
        edges = state.get("edges", {})
        P = state.get("P", {})
        if not isinstance(edges, dict) or not isinstance(P, dict):
            return
        for edge_id, raw in edges.items():
            if not isinstance(raw, dict):
                continue
            self.ensure_edge(str(edge_id))
            try:
                self._edges[edge_id].theta = float(raw.get("theta", 0.0))
                self._edges[edge_id].n = int(raw.get("n", 0))
            except (TypeError, ValueError):
                continue
        for i, row in P.items():
            if i in self._P and isinstance(row, dict):
                for j, v in row.items():
                    if j in self._P[i]:
                        try:
                            self._P[i][j] = float(v)
                        except (TypeError, ValueError):
                            pass

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

    # -- update ------------------------------------------------------------

    def _huber_weight(self, e: float) -> float:
        """Return the IRLS weight for innovation e (1 inside the band, c/|e| out)."""
        ae = abs(e)
        if ae <= self._huber_c or ae == 0.0:
            return 1.0
        return self._huber_c / ae

    def update(self, regressors: dict[str, float], y: float) -> None:
        active = [e for e, x in regressors.items() if x != 0.0]
        if not active:
            return
        for edge_id in active:
            self.ensure_edge(edge_id)

        # Build the regressor over ALL edges (0 for closed). The measurement
        # update runs over the full covariance so cross-edge information is
        # carried correctly. Forgetting is then applied ONLY to the active
        # (excited) directions (directional forgetting), so a closed/structural
        # edge is held — never forgotten — and moves only through real
        # cross-covariance with an active edge. (Forgetting the whole matrix
        # would inflate closed edges every cycle; updating only the active
        # sub-block loses cross terms and can drive P non-PSD — negative
        # variance — and biases multi-edge separation.)
        idx = self.edge_ids()
        x = {i: float(regressors.get(i, 0.0)) for i in idx}
        Px = {i: sum(self._P[i][j] * x[j] for j in idx) for i in idx}
        denom = self._lam + sum(x[i] * Px[i] for i in idx)
        if denom <= 0.0:
            return
        g = {i: Px[i] / denom for i in idx}            # Kalman gain over all edges
        pred = sum(x[i] * self._edges[i].theta for i in idx)
        e = y - pred                                   # innovation
        e_eff = self._huber_weight(e) * e              # Huber-robust
        for i in idx:
            self._edges[i].theta = _clamp(
                self._edges[i].theta + g[i] * e_eff, self._theta_min, self._theta_max
            )
        for i in active:
            self._edges[i].n += 1
        # Measurement update over the full matrix: P = P - g ⊗ Px.
        for i in idx:
            for j in idx:
                self._P[i][j] = self._P[i][j] - g[i] * Px[j]
        # Directional forgetting + anti-windup cap: only the active block ages.
        for i in active:
            for j in active:
                self._P[i][j] = self._P[i][j] / self._lam
            if self._P[i][i] > self._p_max:
                self._P[i][i] = self._p_max
