"""Multi-day identification of the air-wall room model on nights (numpy, scipy).

Model in SmartPI units (per second), u the heater input (0..1), Te the measured
outdoor temperature:
    dTi/dt = a u_e - b (Ti - Te) - k (Ti - Tm) - c (Te - LP(Te)),   Tm = LP(Ti)
with u_e the heater input through a first-order emitter of time constant
tau_e and LP the first-order low-pass of time constant tau_w. The wall state is
the low-pass of the MEASURED air temperature, started at the window start with
a free correction delta exp(-t/tau_w); the air is simulated from u, Te and that
wall state, restarted (free initial air) at each quiet segment. With
lam = b + k the air pole, the output is linear in (a, b, k delta, c, Ti0 per
segment), so (lam, tau_w, tau_e) is profiled on a grid and refined
(Nelder-Mead). The one-constant reference is the same fit with k = 0.

A window holds the samples of three consecutive quiet periods (no sun,
constant setpoint, valid data; quiet_periods) on a 600 s grid: a bin is fitted
only when it lies wholly inside one of them, and the air restarts at each one.
The wall state starts ID_WALL_WARMUP_S before the first period. Window class:
'mass' iff dBIC >= 10, k > 0, k/b >= 1, tau_w in [2, 300] h and not on the grid
edge; 'one' iff dBIC < 0.
An evaluation uses two windows with disjoint periods (the three periods of a
window and the three before them):
'mass' when both are 'mass' and agree (k within x1.25, tau_w within x1.5);
'one' when both are one-constant fits of a precise sensor that agree with each
other and with the published b. The emitter time constant tau_e of a decisive
evaluation is the refined T_ar when both window values agree (x1.5).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize
from scipy.signal import lfilter

from ..job_abort import check
from .const import (
    H,
    ID_DBIC_MASS,
    ID_DBIC_ONE,
    ID_DT,
    ID_FINE_S,
    ID_K_TOL,
    ID_KB_MIN,
    ID_MIN_SAMPLES,
    ID_ONE_B_TOL,
    ID_ONE_BPUB_TOL,
    ID_ONE_DBIC_MAX,
    ID_ONE_RMS_C,
    ID_PRECISE_Q,
    ID_PUB_LAST,
    ID_SIGMA_FLOOR,
    ID_TE_GRID,
    ID_TE_MAX,
    ID_TE_PUB_MAX_FRAC,
    ID_TE_PUB_MIN,
    ID_TE_TOL,
    ID_TW_HI,
    ID_TW_LO,
    ID_TW_MAX_H,
    ID_TW_MIN_H,
    ID_TW_TOL,
)
from .signals import Anchors, bin_mean, duty_bins, level_bins

LAM_C = 1.0 / (np.exp(np.linspace(math.log(0.3), math.log(600.0), 26)) * H)
TW_C = np.exp(np.linspace(math.log(ID_TW_LO / H), math.log(ID_TW_HI / H), 22)) * H
LAM_LO, LAM_HI = float(LAM_C.min()), float(LAM_C.max())


# ---------------------------------------------------------------------------
# Grid data
# ---------------------------------------------------------------------------
def grid_data(pub_t, pub_v, on, off, ext_t, ext_v, t0: float, t_end: float, valid_mask=None,
              power=None) -> dict:
    """Data on the 600 s grid [t0, t_end - step): anchors interpolated at the bin starts, heater duty of the
    bins (and of 60 s bins for the emitter), bin-mean outdoor temperature, validity.

    power: valve power level timeline (times, levels); the duty is then its bin mean instead of the
    duty of the heater edges."""
    step = ID_DT
    grid = np.arange(t0, t_end - step, step)
    anch = Anchors(pub_t, pub_v)
    y = np.interp(grid, anch.t, anch.v)
    first = anch.t[0] if len(anch.t) else t0
    fine = np.arange(t0, grid[-1] + step, ID_FINE_S)
    if power is None:
        u = duty_bins(on, off, grid, step, init_on=False)
        u_fine = duty_bins(on, off, fine, ID_FINE_S, init_on=False)
    else:
        u = level_bins(power[0], power[1], grid, step)
        u_fine = level_bins(power[0], power[1], fine, ID_FINE_S)
    te = bin_mean(ext_t, ext_v, grid, step)
    valid = grid >= first
    if valid_mask is not None:
        valid &= valid_mask(grid, step)
    return {"t": grid, "y": y, "u": u, "u_fine": u_fine, "te": te, "valid": valid, "step": step}


def u_emit(d: dict, tau_e: float) -> np.ndarray:
    """Bin means of the heater input through a first-order emitter of time constant tau_e (cached)."""
    c = d.setdefault("_ue", {})
    key = round(float(tau_e), 3)
    if key not in c:
        if tau_e < 1.0:
            c[key] = d["u"]
        else:
            uf = d["u_fine"]
            al = math.exp(-ID_FINE_S / tau_e)
            x = lfilter([1.0 - al], [1.0, -al], uf)
            # x[i] is the emitter output at the end of fine bin i; bin mean of the start/end values
            xs = np.r_[0.0, x[:-1]]
            xm = 0.5 * (xs + x)
            r = int(round(d["step"] / ID_FINE_S))
            n = len(d["u"])
            c[key] = xm[: n * r].reshape(n, r).mean(axis=1)
    return c[key]


def lp_meas(y: np.ndarray, tau_w: float, step: float) -> np.ndarray:
    """Wall state: first-order low-pass of the measured air (bin-mean input), Tm[0] = y[0]."""
    al = math.exp(-step / tau_w)
    ym = 0.5 * (y[:-1] + y[1:])
    out = np.empty_like(y)
    out[0] = y[0]
    zi = np.array([al * y[0]])
    out[1:] = lfilter([1.0 - al], [1.0, -al], ym, zi=zi)[0]
    return out


def air_filter(x: np.ndarray, lam: float, step: float) -> np.ndarray:
    """Zero-state response z[n+1] = beta z[n] + (1 - beta)/lam x[n] (z[0] = 0)."""
    be = math.exp(-lam * step)
    out = np.zeros(len(x))
    out[1:] = lfilter([(1.0 - be) / lam], [1.0, -be], x[:-1])
    return out


def segments_of(s: np.ndarray, min_len: int = 6) -> list[tuple[int, int]]:
    """Maximal runs of True of at least min_len samples, as (start, stop)."""
    segs = []
    n = len(s)
    i = 0
    while i < n:
        if s[i]:
            j = i
            while j + 1 < n and s[j + 1]:
                j += 1
            if j - i + 1 >= min_len:
                segs.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return segs


class NightProblem:
    """One window [i0, i1) of grid data; sel: residual mask; the air restarts at each selected segment."""

    def __init__(self, d: dict, i0: int, i1: int, sel: np.ndarray | None = None):
        self.d, self.i0, self.i1 = d, i0, i1
        self.y = d["y"][i0:i1].astype(float)
        self.te = d["te"][i0:i1].astype(float)
        self.step = d["step"]
        n = len(self.y)
        s = np.ones(n, dtype=bool) if sel is None else np.asarray(sel[i0:i1], dtype=bool).copy()
        if "valid" in d:
            s &= d["valid"][i0:i1]
        self.segs = segments_of(s)
        self.mask = np.zeros(n, dtype=bool)
        for a, b in self.segs:
            self.mask[a + 1:b] = True
        self.n = int(self.mask.sum())
        self.hours = self.n * self.step / H
        self._tm = {}
        self._tme = {}

    def u(self, tau_e):
        return u_emit(self.d, tau_e)[self.i0:self.i1]

    def tm(self, tau_w):
        if tau_w not in self._tm:
            self._tm[tau_w] = lp_meas(self.y, tau_w, self.step)
        return self._tm[tau_w]

    def tme(self, tau_w):
        if tau_w not in self._tme:
            self._tme[tau_w] = lp_meas(self.te, tau_w, self.step)
        return self._tme[tau_w]

    def _ucols(self, lam: float, tau_e: float):
        key = (lam, tau_e)
        if getattr(self, "_uk", None) != key:
            n = len(self.y)
            uu = self.u(tau_e)
            cu = np.zeros(n)
            ti0 = np.zeros((n, len(self.segs)))
            be = math.exp(-lam * self.step)
            for j, (a, b) in enumerate(self.segs):
                cu[a:b] = air_filter(uu[a:b], lam, self.step)
                ti0[a:b, j] = be ** np.arange(b - a)
            self._uk, self._uc = key, (cu, ti0)
        return self._uc

    def _base_columns(self, lam: float, tau_w: float | None, tau_e: float):
        n = len(self.y)
        cols_u, ti0 = self._ucols(lam, tau_e)
        cols_b, cols_m, off = np.zeros(n), np.zeros(n), np.zeros(n)
        if tau_w is not None:
            tm = self.tm(tau_w)
            ga = math.exp(-self.step / tau_w)
            geo = ga ** np.arange(n)
            tmb = np.r_[0.5 * (tm[:-1] + tm[1:]), tm[-1]]
            gb = 0.5 * (1.0 + ga) * geo
        for a, b in self.segs:
            sl = slice(a, b)
            if tau_w is None:
                off[sl] = lam * air_filter(self.te[sl], lam, self.step)
            else:
                cols_b[sl] = air_filter(self.te[sl] - tmb[sl], lam, self.step)
                off[sl] = lam * air_filter(tmb[sl], lam, self.step)
                cols_m[sl] = air_filter(gb[sl], lam, self.step)
        X = np.column_stack([cols_u, ti0]) if tau_w is None else np.column_stack([cols_u, cols_b, cols_m, ti0])
        return X, off

    def columns(self, lam: float, tau_w: float | None, tau_e: float):
        X, off = self._base_columns(lam, tau_w, tau_e)
        if tau_w is None:
            return X, off
        n = len(self.y)
        tme = self.tme(tau_w)
        teb = np.r_[0.5 * (tme[:-1] + tme[1:]), tme[-1]]
        col = np.zeros(n)
        for a, b in self.segs:
            sl = slice(a, b)
            col[sl] = air_filter(-(self.te[sl] - teb[sl]), lam, self.step)
        # inserted after (a, b, k delta): th[0..2] keep their meaning
        return np.column_stack([X[:, :3], col, X[:, 3:]]), off

    def solve(self, lam: float, tau_w: float | None, tau_e: float = 0.0):
        check()
        # bounded domain: tau_w >= 1 h excludes the degenerate observer solution (tau_w -> 0, k -> inf)
        # where the measured-air wall drags the simulated air onto the measurement
        if (not (0.0 <= tau_e <= ID_TE_MAX) or not (LAM_LO * 0.999 <= lam <= LAM_HI * 1.001)
                or (tau_w is not None and not (ID_TW_LO * 0.999 <= tau_w <= ID_TW_HI * 1.001))):
            return math.inf, None
        X, off = self.columns(lam, tau_w, tau_e)
        m = self.mask
        A, r = X[m], self.y[m] - off[m]
        th, *_ = np.linalg.lstsq(A, r, rcond=None)
        bad = th[0] <= 0 or (tau_w is not None and not (0.0 < th[1] <= lam))
        if bad:
            return math.inf, None
        res = r - A @ th
        return float(res @ res), th

    @staticmethod
    def params(lam, tau_w, tau_e, th):
        if tau_w is None:
            return {"a": float(th[0]), "b": float(lam), "k": 0.0, "tau_w": None, "lam": float(lam),
                    "tau_e": float(tau_e)}
        k = lam - th[1]
        return {"a": float(th[0]), "b": float(th[1]), "k": float(k), "tau_w": float(tau_w), "lam": float(lam),
                "tau_e": float(tau_e), "c": float(th[3])}


def _nm(fun, x0, scales):
    x0 = np.asarray(x0, dtype=float)
    simplex = [x0] + [x0 + np.eye(len(x0))[i] * scales[i] for i in range(len(x0))]
    r = minimize(fun, x0, method="Nelder-Mead",
                 options={"xatol": 1e-4, "fatol": 1e-11, "maxiter": 600, "initial_simplex": np.array(simplex)})
    return r.x, float(r.fun)


def _te(x):
    return min(ID_TE_MAX, x * x)


def classify(out: dict) -> dict:
    n = out["n"]
    ne = max(4.0, out["hours"])
    S1 = out["sse1"] / n + ID_SIGMA_FLOOR ** 2
    S2 = (out["sse2"] / n + ID_SIGMA_FLOOR ** 2) if np.isfinite(out["sse2"]) else S1
    out["rmse1"] = math.sqrt(out["sse1"] / n)
    out["rmse2"] = math.sqrt(out["sse2"] / n) if np.isfinite(out["sse2"]) else None
    dbic = ne * math.log(S1 / S2) - 3.0 * math.log(ne)
    out["dbic"] = dbic
    p2 = out.get("p2")
    fails = []
    if dbic < ID_DBIC_MASS:
        fails.append("dbic")
    if p2 is None or p2["k"] <= 0:
        fails.append("k")
    else:
        if p2["k"] / p2["b"] < ID_KB_MIN:
            fails.append("kb")
        if not (ID_TW_MIN_H * H <= p2["tau_w"] <= ID_TW_MAX_H * H) or out.get("tw_edge"):
            fails.append("tw")
    out["fails"] = fails
    out["cls"] = "mass" if not fails else ("one" if dbic < ID_DBIC_ONE else "unknown")
    return out


def fit_nights(d: dict, i0: int, i1: int, sel=None) -> dict:
    """One-constant and air-wall fits of a window, with its class."""
    pb = NightProblem(d, i0, i1, sel)
    if pb.n < ID_MIN_SAMPLES:
        return {"ok": False, "why": "too few samples", "n": pb.n, "cls": "none"}
    tes = ID_TE_GRID
    best1 = None
    for te in tes:
        for lam in LAM_C:
            v = pb.solve(lam, None, te)[0]
            if best1 is None or v < best1[0]:
                best1 = (v, lam, te)
    x, _ = _nm(lambda x: pb.solve(math.exp(x[0]), None, _te(x[1]))[0], [math.log(best1[1]), math.sqrt(best1[2])],
               [0.1, 6.0])
    lam1, te1 = math.exp(x[0]), _te(x[1])
    sse1, th1 = pb.solve(lam1, None, te1)
    if th1 is None:
        # no feasible one-constant solution (a <= 0 or the loss out of bounds everywhere)
        return {"ok": False, "why": "1R1C fit infeasible", "cls": "none"}
    out = {"ok": True, "n": pb.n, "hours": pb.hours, "n_seg": len(pb.segs),
           "p1": pb.params(lam1, None, te1, th1), "sse1": sse1}
    cand = []
    prof = np.full(len(TW_C), np.inf)
    for te in tes:
        S = np.full((len(LAM_C), len(TW_C)), np.inf)
        for jl, lam in enumerate(LAM_C):
            for jt, tw in enumerate(TW_C):
                S[jl, jt] = pb.solve(lam, tw, te)[0]
        prof = np.minimum(prof, S.min(axis=0))
        Sp = np.pad(S, 1, constant_values=np.inf)
        for jl in range(S.shape[0]):
            for jt in range(S.shape[1]):
                v = S[jl, jt]
                if np.isfinite(v) and v <= Sp[jl:jl + 3, jt:jt + 3].min():
                    cand.append((v, LAM_C[jl], TW_C[jt], te))
    cand.sort(key=lambda z: z[0])
    if not cand:
        out.update(p2=None, sse2=math.inf)
        return classify(out)
    sols = []
    for _, lam, tw, te in cand[:4]:
        x, fv = _nm(lambda x: pb.solve(math.exp(x[0]), math.exp(x[1]), _te(x[2]))[0],
                    [math.log(lam), math.log(tw), math.sqrt(te)], [0.15, 0.15, 6.0])
        sols.append((fv, math.exp(x[0]), math.exp(x[1]), _te(x[2])))
    sols.sort(key=lambda z: z[0])
    _, lam2, tw2, te2 = sols[0]
    sse2, th2 = pb.solve(lam2, tw2, te2)
    if th2 is None:
        out.update(p2=None, sse2=math.inf)
        return classify(out)
    out.update(p2=pb.params(lam2, tw2, te2, th2), sse2=sse2,
               tw_edge=bool(tw2 <= TW_C[1] or tw2 >= TW_C[-2]))
    return classify(out)


def q_obs(pub_v) -> float | None:
    """Smallest absolute change > 1e-3 C between consecutive published values (sensor step)."""
    v = np.asarray(pub_v, dtype=float)
    dv = np.abs(np.diff(v))
    dv = dv[dv > 1e-3]
    return float(dv.min()) if len(dv) else None


def interval_mask(d: dict, intervals) -> np.ndarray:
    """Bins lying wholly inside one of the intervals."""
    t, step = d["t"], d["step"]
    m = np.zeros(len(t), dtype=bool)
    for a, b in intervals:
        m |= (t >= a) & (t + step <= b + 1e-6)
    return m


def window_fit(d: dict, t0: float, t1: float, intervals, q: float | None) -> dict:
    """Fit of the window [t0, t1) on the bins inside the quiet intervals; slim result."""
    nm = interval_mask(d, intervals)
    i0 = int(np.searchsorted(d["t"], t0 - 1e-6))
    i1 = int(np.searchsorted(d["t"], t1 - 1e-6))
    f = fit_nights(d, i0, i1, nm)
    out = {"q_obs": q}
    for k in ("ok", "cls", "fails", "dbic", "rmse1", "rmse2", "n", "hours", "why"):
        out[k] = f.get(k)
    out["p1"], out["p2"] = f.get("p1"), f.get("p2")
    return out


# ---------------------------------------------------------------------------
# Evaluation of two windows (rules)
# ---------------------------------------------------------------------------
def _gm(a, b):
    return math.sqrt(a * b)


def _te_ok(x) -> bool:
    return x is not None and ID_TE_PUB_MIN <= x <= ID_TE_PUB_MAX_FRAC * ID_TE_MAX


def mass_rule(w1: dict, w2: dict) -> tuple[bool, str]:
    if w1.get("cls") != "mass" or w2.get("cls") != "mass":
        return False, "window not mass"
    p, q = w1["p2"], w2["p2"]
    if abs(math.log(p["k"] / q["k"])) > ID_K_TOL:
        return False, "k unstable"
    if abs(math.log(p["tau_w"] / q["tau_w"])) > ID_TW_TOL:
        return False, "tau_w unstable"
    return True, "mass"


def one_rule(w1: dict, w2: dict, b_pub_per_s: float) -> tuple[bool, str]:
    for w in (w1, w2):
        if not w.get("ok") or not w.get("p1"):
            return False, "no fit"
        if w.get("q_obs") is None or w["q_obs"] > ID_PRECISE_Q:
            return False, "sensor not precise"
        if w.get("dbic") is None or w["dbic"] >= ID_ONE_DBIC_MAX:
            return False, "dbic"
        if w.get("rmse1") is None or w["rmse1"] > ID_ONE_RMS_C:
            return False, "rmse1"
        if abs(math.log(w["p1"]["b"] / b_pub_per_s)) > ID_ONE_BPUB_TOL:
            return False, "b1 vs b_pub"
    if abs(math.log(w1["p1"]["b"] / w2["p1"]["b"])) > ID_ONE_B_TOL:
        return False, "b1 unstable"
    return True, "one"


def evaluate_day(w1: dict, w2: dict, b_pub_per_s: float) -> dict:
    """Refined evaluation from two windows with disjoint periods (w1 earlier, w2 later)."""
    okm, whym = mass_rule(w1, w2)
    oko, whyo = one_rule(w1, w2, b_pub_per_s)
    if okm and oko:
        cls = "unknown"
    elif okm:
        cls = "mass"
    elif oko:
        cls = "one"
    else:
        cls = "unknown"
    out = {"cls": cls, "why_mass": whym, "why_one": whyo}
    if cls == "mass":
        p, q = w1["p2"], w2["p2"]
        out.update(k=_gm(p["k"], q["k"]), tau_w=_gm(p["tau_w"], q["tau_w"]), b=_gm(p["b"], q["b"]),
                   a=_gm(p["a"], q["a"]))
        te1, te2 = p.get("tau_e"), q.get("tau_e")
    elif cls == "one":
        p, q = w1["p1"], w2["p1"]
        out.update(b=_gm(p["b"], q["b"]), a=_gm(p["a"], q["a"]))
        te1, te2 = p.get("tau_e"), q.get("tau_e")
    else:
        te1 = te2 = None
    out["te_pair"] = [te1, te2]
    te_ok = cls in ("mass", "one") and _te_ok(te1) and _te_ok(te2) and abs(math.log(te1 / te2)) <= ID_TE_TOL
    out["te_ok"] = bool(te_ok)
    out["te"] = _gm(te1, te2) if te_ok else None
    if cls in ("mass", "one") and not te_ok:
        out["why_te"] = "tau_e at a bound" if not (_te_ok(te1) and _te_ok(te2)) else "tau_e unstable"
    return out


def silence(e: dict, why: str) -> dict:
    """An evaluation kept as a diagnostic only: unknown for the pipeline, raw class and tau_e kept."""
    return {"cls": "unknown", "why_mass": why, "why_one": why, "te_pair": [None, None], "te_ok": False,
            "te": None, "day": e.get("day"), "t": e.get("t"), "silenced": True, "cls_raw": e["cls"], "te_raw": e.get("te"),
            "te_ok_raw": e.get("te_ok"), "k_raw": e.get("k"), "tau_w_raw": e.get("tau_w"),
            "a_raw": e.get("a"), "b_raw": e.get("b")}


def class_in_force(evals: list[tuple[float, str]], t_ref: float) -> str:
    """Class in force at t_ref from the (decision time, cls) evaluations decided before t_ref: the latest
    decisive class; two decisive evaluations that disagree make it 'unknown' until two consecutive ones
    agree."""
    dec = [c for t, c in sorted(evals) if t < t_ref and c in ("mass", "one")]
    if not dec:
        return "unknown"
    if len(dec) == 1:
        return dec[-1]
    state = dec[0]
    for i in range(1, len(dec)):
        if dec[i] != dec[i - 1]:
            state = "unknown"
        else:
            state = dec[i]
    return state


def params_in_force(evs: list[dict], t_ref: float) -> dict | None:
    """Median (k, tau_w) over the last ID_PUB_LAST evaluations classed 'mass' decided before t_ref."""
    ms = [e for e in sorted(evs, key=lambda x: x["t"]) if e["t"] < t_ref and e["cls"] == "mass"]
    if not ms:
        return None
    last = ms[-ID_PUB_LAST:]
    return {"k_per_s": float(np.median([e["k"] for e in last])),
            "tau_w_s": float(np.median([e["tau_w"] for e in last])), "n": len(last), "from_day": last[-1]["day"]}


def te_in_force(evs: list[dict], t_ref: float) -> dict | None:
    """Refined tau_e in force at t_ref: the latest publication decided before t_ref, a publication being
    the median of the passing values among the last ID_PUB_LAST evaluations."""
    es = sorted(evs, key=lambda x: x["t"])
    pub = None
    for i, e in enumerate(es):
        if e["t"] >= t_ref:
            break
        last = [x for x in es[max(0, i - ID_PUB_LAST + 1): i + 1] if x.get("te_ok")]
        if last:
            pub = {"value": float(np.median([x["te"] for x in last])), "n": len(last), "day": e["day"]}
    return pub
