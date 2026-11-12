"""Room-mass indicator on passive nights: one time constant against two (numpy, scipy).

Models, with the measured outdoor temperature Te as input (zero-order hold on a
60 s grid, k = grid index from the segment start s0, Te0 = Te(s0), dTe = Te - Te0):
  1R1C:  Ti = Te0 + F_tau(dTe) + A a^k                              a = exp(-60/tau)
  2-exp: Ti = Te0 + g F_tau1(dTe) + (1 - g) F_tau2(dTe) + A1 a1^k + A2 a2^k,  tau1 < tau2
F_tau is the zero-state response of a unit-gain first order. With a constant Te
the 2-exp model is Ti - Te = A1 exp(-t/tau1) + A2 exp(-t/tau2), the free response
of an air node coupled to a wall node. Constraints of the mass hypothesis:
tau2 >= 1.5 tau1, A1 >= 0, A2 >= 0, g in [0, 1]. Both models are linear in their
amplitudes for given time constants: exact profile on a log grid of the time
constants, then Nelder-Mead.

Model comparison: BIC with one effective point per 10 min and a variance floor,
dBIC = n_eff ln(S_1R1C / S_2exp) - 3 ln n_eff; positive values favour two time
constants. A night is classed 'mass', 'edge' (slow mode at the grid edge),
'one' or 'none'; the room class needs at least three long qualified nights.
"""
from __future__ import annotations

import itertools
import math
import statistics

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.signal import lfilter

from ..job_abort import check
from .const import (
    LONG_NIGHT_H,
    MIN_FIT_S,
    NIGHT_LOOKBACK_S,
    PEAK_SMOOTH_S,
    RM_BIN_S,
    RM_GRID_S,
    RM_N_EFF_STEP_S,
    RM_RATIO_MIN,
    RM_RULE,
    RM_SIGMA_FLOOR_C,
    RM_TAU_COUNT,
    RM_TAU_MAX_S,
    RM_TAU_MIN_S,
    TAU2_EDGE_H,
    WASHOUT_FALLBACK_S,
)
from .signals import binned

GRID_S = RM_GRID_S
TAUS = np.exp(np.linspace(math.log(RM_TAU_MIN_S), math.log(RM_TAU_MAX_S), RM_TAU_COUNT))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def te_on_grid(ext_t, ext_v, s0: float, s_end: float):
    """Outdoor temperature held (zero-order) on the 60 s grid [s0, s_end]."""
    g = np.arange(s0, s_end + 1e-9, GRID_S)
    ext_t = np.asarray(ext_t, dtype=float)
    ext_v = np.asarray(ext_v, dtype=float)
    idx = np.clip(np.searchsorted(ext_t, g, side="right") - 1, 0, len(ext_v) - 1)
    return g, ext_v[idx]


def filt(x: np.ndarray, tau: float):
    """Zero-state response of a unit-gain first order to x (ZOH) on the grid, out[0] = 0; and a."""
    a = math.exp(-GRID_S / tau)
    return lfilter([0.0, 1.0 - a], [1.0, -a], x), a


def _bounded_lstsq(B: np.ndarray, r: np.ndarray, bounds):
    """min |r - B c|^2 with c_j in bounds[j] = (lo, hi) (None = unbounded); exact by enumeration."""
    c, *_ = np.linalg.lstsq(B, r, rcond=None)
    if all((lo is None or c[j] >= lo - 1e-12) and (hi is None or c[j] <= hi + 1e-12)
           for j, (lo, hi) in enumerate(bounds)):
        return float(np.sum((r - B @ c) ** 2)), c
    states = []
    for lo, hi in bounds:
        s = ["free"]
        if lo is not None:
            s.append(lo)
        if hi is not None:
            s.append(hi)
        states.append(s)
    best = None
    for combo in itertools.product(*states):
        fixed = np.array([0.0 if s == "free" else float(s) for s in combo])
        free = [j for j, s in enumerate(combo) if s == "free"]
        rr = r - B @ fixed
        cc = fixed.copy()
        if free:
            sol, *_ = np.linalg.lstsq(B[:, free], rr, rcond=None)
            cc[free] = sol
        if not all((lo is None or cc[j] >= lo - 1e-9) and (hi is None or cc[j] <= hi + 1e-9)
                   for j, (lo, hi) in enumerate(bounds)):
            continue
        sse = float(np.sum((r - B @ cc) ** 2))
        if best is None or sse < best[0]:
            best = (sse, cc)
    return best


class Segment:
    """Observations and outdoor input of one passive segment."""

    def __init__(self, anch, ext_t, ext_v, s0: float, s1: float):
        self.s0, self.s1 = float(s0), float(s1)
        self.ta, self.ya = binned(anch, s0, s1)
        self.g, self.te = te_on_grid(ext_t, ext_v, s0, s1)
        self.te0 = float(self.te[0])
        self.dte = self.te - self.te0
        self.k = np.clip(np.round((self.ta - s0) / GRID_S).astype(int), 0, len(self.g) - 1)
        self.kk = self.k.astype(float)
        self.q = anch.q
        self._cache = {}

    def cols(self, tau: float):
        c = self._cache.get(tau)
        if c is None:
            y, a = filt(self.dte, tau)
            c = (y[self.k], a ** self.kk)
            self._cache[tau] = c
        return c

    def sse1(self, tau: float):
        check()
        F, A = self.cols(tau)
        r = self.ya - self.te0 - F
        X = float(np.dot(A, r) / np.dot(A, A))
        return float(np.sum((r - X * A) ** 2)), X

    def sse2(self, t1: float, t2: float):
        check()
        F1, A1 = self.cols(t1)
        F2, A2 = self.cols(t2)
        B = np.vstack([F1 - F2, A1, A2]).T
        r = self.ya - self.te0 - F2
        return _bounded_lstsq(B, r, [(0.0, 1.0), (0.0, None), (0.0, None)])


def fit_1r1c(seg: Segment):
    best = None
    for tau in TAUS:
        sse, X = seg.sse1(tau)
        if best is None or sse < best[0]:
            best = (sse, tau, X)
    lt = math.log(best[1])
    rr = minimize_scalar(lambda x: seg.sse1(math.exp(x))[0],
                         bounds=(max(lt - 0.3, math.log(RM_TAU_MIN_S)), min(lt + 0.3, math.log(RM_TAU_MAX_S))),
                         method="bounded", options={"xatol": 1e-4})
    if rr.fun < best[0]:
        sse, X = seg.sse1(math.exp(rr.x))
        best = (sse, math.exp(rr.x), X)
    return best


def _grid_2exp(seg: Segment):
    """Best (tau1, tau2) grid pair: constrained least squares for every pair at once."""
    F = np.vstack([seg.cols(t)[0] for t in TAUS])
    A = np.vstack([seg.cols(t)[1] for t in TAUS])
    y = seg.ya - seg.te0
    FF, FA, AA = F @ F.T, F @ A.T, A @ A.T
    yF, yA, yy = F @ y, A @ y, float(y @ y)
    ii, jj = np.nonzero(TAUS[None, :] >= RM_RATIO_MIN * TAUS[:, None])
    G = np.empty((len(ii), 3, 3))
    G[:, 0, 0] = FF[ii, ii] - 2 * FF[ii, jj] + FF[jj, jj]
    G[:, 0, 1] = G[:, 1, 0] = FA[ii, ii] - FA[jj, ii]
    G[:, 0, 2] = G[:, 2, 0] = FA[ii, jj] - FA[jj, jj]
    G[:, 1, 1] = AA[ii, ii]
    G[:, 1, 2] = G[:, 2, 1] = AA[ii, jj]
    G[:, 2, 2] = AA[jj, jj]
    b = np.empty((len(ii), 3))
    b[:, 0] = (yF[ii] - yF[jj]) - (FF[ii, jj] - FF[jj, jj])
    b[:, 1] = yA[ii] - FA[jj, ii]
    b[:, 2] = yA[jj] - FA[jj, jj]
    rr = yy - 2 * yF[jj] + FF[jj, jj]
    ridge = 1e-10 * (np.trace(G, axis1=1, axis2=2) / 3.0 + 1e-12)
    bounds = [(0.0, 1.0), (0.0, None), (0.0, None)]
    best_sse = np.full(len(ii), np.inf)
    best_c = np.zeros((len(ii), 3))
    states = [["free", 0.0, 1.0], ["free", 0.0], ["free", 0.0]]
    for combo in itertools.product(*states):
        free = [k for k, st in enumerate(combo) if st == "free"]
        fixed = np.array([0.0 if st == "free" else float(st) for st in combo])
        c = np.tile(fixed, (len(ii), 1))
        if free:
            Gf = G[:, free][:, :, free] + ridge[:, None, None] * np.eye(len(free))[None]
            rhs = b[:, free] - np.einsum("nij,j->ni", G[:, free, :], fixed)
            c[:, free] = np.linalg.solve(Gf, rhs[..., None])[..., 0]
        ok = np.ones(len(ii), dtype=bool)
        for k, (lo, hi) in enumerate(bounds):
            if lo is not None:
                ok &= c[:, k] >= lo - 1e-9
            if hi is not None:
                ok &= c[:, k] <= hi + 1e-9
        sse = rr - 2 * np.einsum("ni,ni->n", c, b) + np.einsum("ni,nij,nj->n", c, G, c)
        upd = ok & (sse < best_sse)
        best_sse[upd] = sse[upd]
        best_c[upd] = c[upd]
    k = int(np.argmin(best_sse))
    if not np.isfinite(best_sse[k]):
        return None
    t1, t2 = TAUS[ii[k]], TAUS[jj[k]]
    res = seg.sse2(t1, t2)
    return None if res is None else (res[0], t1, t2, res[1])


def fit_2exp(seg: Segment):
    best = _grid_2exp(seg)
    if best is None:
        return None
    lmin, lmax = math.log(RM_TAU_MIN_S), math.log(RM_TAU_MAX_S)

    def obj(v):
        l1, l2 = v
        if not (lmin <= l1 and l2 <= lmax and l2 - l1 >= math.log(RM_RATIO_MIN)):
            return 1e9
        res = seg.sse2(math.exp(l1), math.exp(l2))
        return 1e9 if res is None else res[0]

    rr = minimize(obj, [math.log(best[1]), math.log(best[2])], method="Nelder-Mead",
                  options={"xatol": 1e-3, "fatol": 1e-12, "maxiter": 300})
    if rr.fun < best[0]:
        t1, t2 = math.exp(rr.x[0]), math.exp(rr.x[1])
        res = seg.sse2(t1, t2)
        best = (res[0], t1, t2, res[1])
    return best


def fit_segment(anch, ext_t, ext_v, s0: float, s1: float, cut: float | None = None) -> dict | None:
    """Both fits of one passive segment, the dBIC and the cut-time static loss."""
    seg = Segment(anch, ext_t, ext_v, s0, s1)
    n = len(seg.ya)
    if n < 8:
        return None
    sse1, tau, X = fit_1r1c(seg)
    b2 = fit_2exp(seg)
    if b2 is None:
        return None
    sse2, t1, t2, (gg, A1, A2) = b2
    n_eff = max(4.0, min(float(n), (s1 - s0) / RM_N_EFF_STEP_S))
    S1 = sse1 / n + RM_SIGMA_FLOOR_C ** 2
    S2 = sse2 / n + RM_SIGMA_FLOOR_C ** 2
    dbic = n_eff * math.log(S1 / S2) - 3.0 * math.log(n_eff)
    L = s1 - s0
    share_amp = A1 / (A1 + A2) if A1 + A2 > 0 else None
    out = {"s0": s0, "s1": s1, "len_h": L / 3600.0, "n": n, "q": seg.q,
           "r1": {"tau_h": tau / 3600.0},
           "r2": {"tau1_h": t1 / 3600.0, "tau2_h": t2 / 3600.0, "g": float(gg), "A1": float(A1), "A2": float(A2),
                  "share_amp": share_amp},
           "dbic": dbic, "tau_ratio": t2 / t1}
    if cut is not None and A1 + A2 > 0:
        back = s0 - cut
        A1c, A2c = A1 * math.exp(back / t1), A2 * math.exp(back / t2)
        lam1, lam2 = 1.0 / t1, 1.0 / t2
        k2 = (lam1 * A1c + lam2 * A2c) / (A1c + A2c)
        out["r2"]["b_cut_per_min"] = 60.0 * k2
    out["_params"] = {"tau": tau, "X": X, "t1": t1, "t2": t2, "g": float(gg), "A1": float(A1), "A2": float(A2),
                      "te0": seg.te0, "s0": s0}
    return out


def predict_2exp(params: dict, ext_t, ext_v, t_query) -> np.ndarray:
    """Two-constant night fit evaluated at t_query >= s0 with the measured Te."""
    s0 = params["s0"]
    t_query = np.asarray(t_query, dtype=float)
    g, te = te_on_grid(ext_t, ext_v, s0, float(np.max(t_query)) + GRID_S)
    dte = te - params["te0"]
    kf = (t_query - s0) / GRID_S
    k = np.clip(np.floor(kf).astype(int), 0, len(g) - 1)
    F1, _ = filt(dte, params["t1"])
    F2, _ = filt(dte, params["t2"])
    gg = params["g"]
    return (params["te0"] + gg * F1[k] + (1 - gg) * F2[k] + params["A1"] * np.exp(-(t_query - s0) / params["t1"])
            + params["A2"] * np.exp(-(t_query - s0) / params["t2"]))


def segment_flag(r: dict, rule: dict = RM_RULE) -> tuple[str, list[str]]:
    """'mass', 'one' or 'too_short' for one fitted segment, with the failed conditions."""
    if r["len_h"] < rule["min_len_h"]:
        return "too_short", ["len"]
    fails = []
    r2 = r["r2"]
    if r["dbic"] < rule["dbic_min"]:
        fails.append("dbic")
    if r["tau_ratio"] < rule["tau_ratio_min"]:
        fails.append("ratio")
    if r2["A1"] < max(rule["fast_amp_min_c"], rule["fast_amp_min_q"] * (r.get("q") or 0.0)):
        fails.append("amp")
    if (r2["share_amp"] or 0.0) < rule["share_amp_min"]:
        fails.append("share")
    if r2["tau1_h"] > rule["tau1_max_len_frac"] * r["len_h"]:
        fails.append("tau1_long")
    if r2["tau1_h"] < rule["tau1_min_h"]:
        fails.append("tau1_short")
    return ("mass" if not fails else "one"), fails


# ---------------------------------------------------------------------------
# Passive spans and the washed-out quiet part
# ---------------------------------------------------------------------------
def passive_spans(on_edges, off_edges, t_last: float) -> list[tuple[float, float]]:
    """(heater cut, next heater start) spans of at least MIN_FIT_S; t_last ends an open span."""
    on, off = np.asarray(on_edges, dtype=float), np.asarray(off_edges, dtype=float)
    out = []
    for c in off:
        nxt = on[on > c]
        e = float(nxt[0]) if len(nxt) else float(t_last)
        if e - c >= MIN_FIT_S:
            out.append((float(c), e))
    return out


def washout(dtc: float | None, lag: float | None) -> float:
    """Delay after the heater cut before the night is fitted (emitter heat washed out)."""
    if dtc and lag:
        return max(1.5 * dtc, 3.0 * lag)
    if dtc:
        return 1.5 * dtc
    if lag:
        return 3.0 * lag
    return WASHOUT_FALLBACK_S


def post_cut_peak(anch, cut: float, end: float) -> float:
    """Time of the post-cut maximum of the smoothed published temperature."""
    tb, vb = binned(anch, cut, min(end, cut + 12 * 3600.0))
    if len(tb) < 3:
        return cut
    w = max(1, int(PEAK_SMOOTH_S / RM_BIN_S))
    k = np.ones(w) / w
    sm = np.convolve(vb, k, mode="same") if len(vb) > w else vb
    return float(tb[int(np.argmax(sm))]) if len(vb) > w else cut


def quiet_part(s0: float, e: float, quiet) -> tuple[float, float] | None:
    """Longest part of [s0, e) inside the quiet intervals, on a 600 s grid from s0.

    A grid point inside a quiet interval opens or extends a part; the part ends
    at the first point outside, and never after the end of its quiet interval.
    """
    grid = np.arange(s0, e, 600.0)
    if len(grid) == 0:
        return None
    ivs = sorted((float(a), float(b)) for a, b in quiet)

    def inside(x: float) -> float | None:
        for a, b in ivs:
            if a <= x < b:
                return b
        return None

    best, cur, cur_end = None, None, None
    for x in grid:
        b = inside(float(x))
        if b is not None and cur is not None and b != cur_end:
            # the next quiet interval starts at once: close the current part at its end
            if best is None or cur_end - cur > best[1] - best[0]:
                best = (cur, cur_end)
            cur = None
        if b is not None and cur is None:
            cur, cur_end = float(x), b
        if b is None and cur is not None:
            end = min(float(x), cur_end)
            if best is None or end - cur > best[1] - best[0]:
                best = (cur, end)
            cur = None
    if cur is not None:
        end = min(e, cur_end)
        if best is None or end - cur > best[1] - best[0]:
            best = (cur, end)
    return best


def night_segment(anch, spans, params_at, t_end: float, quiet, valid_until=None):
    """Longest washed-out quiet part (>= 1 h) of the passive spans ending by t_end.

    quiet: the quiet intervals (no sun, constant setpoint, valid data) the
    segment must lie in. params_at(t) returns (reliable cool dead time or
    None, published lag or None) valid at t. valid_until(a, b) optionally
    shortens [a, b) at the first data gap.
    """
    best = None
    for c, e in spans:
        if c >= t_end or e <= t_end - NIGHT_LOOKBACK_S:
            continue
        e2 = min(e, t_end)
        if valid_until is not None:
            e2 = valid_until(c, e2)
        dtc, lag = params_at(c)
        base = washout(dtc, lag)
        peak = post_cut_peak(anch, c, e2)
        s0 = max(c + base, peak)
        if e2 - s0 < MIN_FIT_S:
            continue
        nb = quiet_part(s0, e2, quiet)
        if not nb or nb[1] - nb[0] < MIN_FIT_S:
            continue
        if best is None or nb[1] - nb[0] > best["s1"] - best["s0"]:
            best = {"cut": c, "s0": float(nb[0]), "s1": float(nb[1])}
    return best


def classify_night(anch, ext_t, ext_v, seg):
    """Night class and the fit of the night segment (None when not fitted)."""
    if seg is None:
        return {"cls": "none", "why": "no washed-out quiet part >= 1 h"}, None
    r = fit_segment(anch, ext_t, ext_v, seg["s0"], seg["s1"], cut=seg["cut"])
    if r is None:
        return {"cls": "none", "why": "too few anchors", **seg}, None
    flag, fails = segment_flag(r)
    r2 = r["r2"]
    if flag == "mass":
        cls = "edge" if r2["tau2_h"] >= TAU2_EDGE_H else "mass"
    elif flag == "one":
        cls = "one"
    else:
        cls = "none"
    info = {"cls": cls, "fails": fails, **seg, "len_h": r["len_h"], "dbic": r["dbic"],
            "tau1_h": r2["tau1_h"], "tau2_h": r2["tau2_h"], "A1": r2["A1"], "A2": r2["A2"],
            "share_amp": r2["share_amp"], "b_cut_per_min": r2.get("b_cut_per_min")}
    return info, r


def room_class_long(nights: list[dict]) -> str:
    """Room class from at least 3 qualified long nights (>= 4 h), strict majority."""
    q = [n["cls"] for n in nights if n["cls"] in ("mass", "one") and (n.get("len_h") or 0) >= LONG_NIGHT_H]
    if len(q) < 3:
        return "inconclusive"
    nm = sum(1 for c in q if c == "mass")
    return "mass" if nm / len(q) > 0.5 else "one" if nm / len(q) < 0.5 else "inconclusive"


def is_long(night: dict) -> bool:
    return (night.get("len_h") or 0.0) >= LONG_NIGHT_H


# ---------------------------------------------------------------------------
# Air-wall parameters learned from the passive nights
# ---------------------------------------------------------------------------
def night_params(tau1_h: float, tau2_h: float, b_ref_per_min: float) -> dict | None:
    """(k, tau_w) of the air-wall model from a two-constant night fit and a static b:
    k3 = 1/tau_w = lam1 lam2 / b (clipped to [lam2, lam1]), k = lam1 + lam2 - b - k3."""
    if not tau1_h or not tau2_h or not b_ref_per_min or b_ref_per_min <= 0:
        return None
    l1, l2 = 1.0 / (tau1_h * 3600.0), 1.0 / (tau2_h * 3600.0)
    b = b_ref_per_min / 60.0
    k3 = min(max(l1 * l2 / b, l2), l1)
    k = l1 + l2 - b - k3
    if k <= 0:
        return None
    return {"k_per_s": k, "tau_w_s": 1.0 / k3}


def learn_room(nights: list[dict], b_pub_per_min: float) -> dict | None:
    """Median (k, tau_w) over the long nights classed 'mass' (None without any)."""
    ks, tws = [], []
    for n in nights:
        if n.get("cls") != "mass" or (n.get("len_h") or 0.0) < LONG_NIGHT_H:
            continue
        p = night_params(n.get("tau1_h"), n.get("tau2_h"), b_pub_per_min)
        if p:
            ks.append(p["k_per_s"])
            tws.append(p["tau_w_s"])
    if not ks:
        return None
    return {"k_per_s": statistics.median(ks), "tau_w_s": statistics.median(tws), "n": len(ks)}


def loss_2mode_night(params: dict, b_cut_per_min: float | None) -> dict:
    """Room response of a two-constant night: k3 = lam1 lam2 / b_cut, clipped to [lam2, lam1]."""
    l1, l2 = 1.0 / params["t1"], 1.0 / params["t2"]
    if not b_cut_per_min or params["A1"] <= 0:
        return {"kind": "fo", "b_per_min": 60.0 / params["t2"]}
    k2 = b_cut_per_min / 60.0
    k3 = min(max(l1 * l2 / k2, l2), l1)
    return {"kind": "2mode", "lam1": l1, "lam2": l2, "k3": k3}


def loss_2mode_wall(b_per_s: float, k_per_s: float, tau_w_s: float) -> dict:
    """Room response of the air-wall model seen from the air:
    G(s) = (s + 1/tau_w) / ((s + b + k)(s + 1/tau_w) - k/tau_w)."""
    s = b_per_s + k_per_s + 1.0 / tau_w_s
    p = b_per_s / tau_w_s
    d = math.sqrt(max(s * s - 4.0 * p, 0.0))
    return {"kind": "2mode", "lam1": 0.5 * (s + d), "lam2": 0.5 * (s - d), "k3": 1.0 / tau_w_s}
