"""Identification of the room model on the fit periods of a window (numpy, scipy).

Output-error fit on a 60 s grid: the model is simulated from each period start
with the applied command (through the emitter, tau_e and L fixed), the measured
outdoor temperature and its derivative; residuals are taken at the sensor
events (the temperature anchors, each weighted by the time until the next one,
capped). For fixed nonlinear parameters the output is linear in the heating
response a and in the initial air and wall state of each period: those are
solved by least squares inside the search over the nonlinear parameters
(variable projection). The wall state at each period start is estimated, never
assumed at equilibrium. The disturbance is not modelled: disturbed periods are
excluded upstream.

Full model, nonlinear parameters (alpha, beta, g = c / alpha):
    x = (Ta - ., w), dTa/dt = a u_e - alpha (Ta - Text) + w,
    dw/dt = g alpha beta (Ta - Text) - beta w - k dText/dt, k = (c + alpha) / 2.
Reduced model, nonlinear parameter b_eq: dTa/dt = a u_e - b_eq (Ta - Text).

The heat input is a (u_e + g), with g the constant free gain of the static
model in force (energy_balance.py, a share of full power: heat from a warmer
neighbour, steady occupants), taken as known: without it such a gain would be
read as a lower static loss, and the fit's b_eq / a could not match the
energy-balance slope. The gain is not fitted per window: over the quiet periods the
indoor-outdoor difference varies too little to separate a constant input from
the static loss, and a fitted one trades against b_eq freely.

The fit is ordinary least squares on the measurements alone: its static
gain b_eq / a is then an independent check of the energy-balance slope
(gates.py).

Residual checks are computed on a 10 min grid inside the periods: bias, root
mean square, runs test, autocorrelation at 10 min and 1 h, and correlation with
the emitter output, the setpoint and the outdoor temperature. The runs test
counts the sign changes of the hourly means of the residuals: the model serves
hours ahead (wall term, forecasts from 1 h), and ripple shorter than an hour
(sensor response, emitter and cycle shape) makes long runs of 10 min samples
without any error at the scale of the model. The effective number of
independent residuals (the count over the integrated autocorrelation time) is
reported for the emitter search (jobs.py).

Precision (precision()): the least-squares covariance of all the parameters of
a fit at its optimum, from the Jacobian of the weighted model outputs, scaled
by the integrated autocorrelation time of the residuals; it gives the
standard deviations of the identifiable coordinates that the publication
rules compare (gates.py).
"""
from __future__ import annotations

import copy
import math

import numpy as np
from scipy.linalg import expm
from scipy.optimize import minimize, minimize_scalar
from scipy.signal import lfilter

from ..job_abort import check
from ..tar_learning.signals import Anchors, duty_bins, level_bins
from .const import (
    H,
    RM_ALPHA_MAX,
    RM_BEQ_RANGE,
    RM_BETA_RANGE,
    RM_CHECK_STEP_S,
    RM_EVENT_WEIGHT_CAP_S,
    RM_FIT_STEP_S,
    RM_G_GRID,
    RM_RES_RUNS_BLOCK_S,
    RM_RES_RUNS_MIN_BLOCKS,
    RM_SEARCH_STARTS,
    RM_TAU_AIR_GRID_H,
    RM_TAU_WALL_GRID_H,
)

STEP = RM_FIT_STEP_S
_ALPHA_MIN = RM_BEQ_RANGE[0]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def emitter_bins(u: np.ndarray, step: float, tau_e: float, L: float, u_before: float = 0.0) -> np.ndarray:
    """Bin means of the emitter output for bin-mean commands u (delay L, then first order tau_e)."""
    u = np.asarray(u, dtype=float)
    n = len(u)
    if n == 0:
        return u
    if L > 0.0:
        grid = np.arange(n + 1) * step
        cum = np.r_[0.0, np.cumsum(u) * step]
        tt = grid - L
        cc = np.interp(tt, grid, cum)
        cc = np.where(tt < 0.0, tt * u_before, cc)
        v = np.diff(cc) / step
    else:
        v = u.copy()
    if tau_e < 1.0:
        return v
    e = math.exp(-step / tau_e)
    x_end = lfilter([1.0 - e], [1.0, -e], v, zi=np.array([e * u_before]))[0]
    x_start = np.r_[u_before, x_end[:-1]]
    return v + (x_start - v) * (tau_e / step) * (1.0 - e)


class WindowData:
    """Signals of a window on the 60 s grid and the observations of each fit period."""

    def __init__(self, snap: dict, periods: list[tuple[float, float, int]], tau_e: float, L: float) -> None:
        self.periods = periods
        t_lo = min(p[0] for p in periods)
        t_hi = max(p[1] for p in periods)
        warm = max(2.0 * H, L + 5.0 * tau_e)
        g0 = STEP * math.floor((t_lo - warm) / STEP)
        n = int(math.ceil((t_hi - g0) / STEP)) + 1
        self.g0 = g0
        self.t = g0 + STEP * np.arange(n)
        starts = self.t[:-1]
        pw = snap.get("pw")
        if pw is None:
            u = duty_bins(snap["on"], snap["off"], starts, STEP, init_on=False)
        else:
            u = level_bins(pw[0], pw[1], starts, STEP)
        self.u = np.clip(u, 0.0, 1.0)
        self.ue = emitter_bins(self.u, STEP, tau_e, L)
        ext_t = np.asarray(snap["ext_t"], dtype=float)
        ext_v = np.asarray(snap["ext_v"], dtype=float)
        j = np.clip(np.searchsorted(ext_t, self.t, side="right") - 1, 0, max(len(ext_v) - 1, 0))
        self.te = ext_v[j] if len(ext_v) else np.zeros(n)
        self.te_bin = 0.5 * (self.te[:-1] + self.te[1:])
        self.dte_bin = np.diff(self.te) / STEP
        sp = snap.get("sp") or []
        if sp:
            sp_t = np.asarray([x[0] for x in sp], dtype=float)
            sp_v = np.asarray([x[1] for x in sp], dtype=float)
            k = np.clip(np.searchsorted(sp_t, self.t, side="right") - 1, 0, len(sp_v) - 1)
            self.sp = sp_v[k]
        else:
            self.sp = np.zeros(n)
        anch = Anchors(snap["pub_t"], snap["pub_v"])
        self.anch_t, self.anch_v = anch.t, anch.v
        self.segments = []
        cap = RM_EVENT_WEIGHT_CAP_S / STEP
        for a, b, night in periods:
            i0 = int(math.ceil((a - g0) / STEP - 1e-9))
            i1 = int(math.floor((b - g0) / STEP + 1e-9))
            if i1 - i0 < 10:
                continue
            m = (self.anch_t > self.t[i0]) & (self.anch_t <= self.t[i1])
            k = np.ceil((self.anch_t[m] - g0) / STEP - 1e-9).astype(int) - i0
            y = self.anch_v[m]
            if len(k):
                # the last anchor of a bin wins
                last = np.r_[k[1:] != k[:-1], True]
                k, y = k[last], y[last]
            y_start = self._anchor_at(self.t[i0])
            if y_start is None:
                continue
            k = np.r_[0, k]
            y = np.r_[y_start, y]
            nxt = np.r_[k[1:], i1 - i0 + 1]
            w = np.minimum(nxt - k, cap).astype(float)
            n_check = (i1 - i0) * STEP / RM_CHECK_STEP_S + 1
            w *= n_check / w.sum()
            kc = np.arange(0, i1 - i0 + 1, int(round(RM_CHECK_STEP_S / STEP)))
            yc = np.interp(self.t[i0 + kc], self.anch_t, self.anch_v)
            self.segments.append({"i0": i0, "N": i1 - i0, "k": k, "y": y, "w": w, "kc": kc, "yc": yc,
                                  "te0": float(self.te[i0]), "night": night})
        self.n_events = int(sum(len(s["k"]) for s in self.segments))
        self.hours = float(sum(s["N"] for s in self.segments) * STEP / H)

    def with_emitter(self, tau_e: float, L: float) -> WindowData:
        """The same window with another emitter (the emitter output recomputed, the rest shared)."""
        out = copy.copy(self)
        out.ue = emitter_bins(self.u, STEP, tau_e, L)
        return out

    def _anchor_at(self, t: float) -> float | None:
        if len(self.anch_t) == 0 or t < self.anch_t[0] - 2.0 * H:
            return None
        return float(np.interp(t, self.anch_t, self.anch_v))

    def heat_s(self) -> float:
        """Full-power equivalent heating delivered over the fit periods (s)."""
        return float(sum(np.sum(self.ue[s["i0"]:s["i0"] + s["N"]]) * STEP for s in self.segments))

    def mean_delta(self) -> float | None:
        vals = [s["yc"] - self.te[s["i0"] + s["kc"]] for s in self.segments]
        return float(np.mean(np.concatenate(vals))) if vals else None


# ---------------------------------------------------------------------------
# Problem
# ---------------------------------------------------------------------------
def _disc2(alpha: float, beta: float, gamma: float, h: float):
    """Zero-order-hold discretisation of the air-wall pair with unit coefficients on a and k."""
    M = np.zeros((5, 5))
    M[:2, :2] = [[-alpha, 1.0], [gamma, -beta]]
    M[:2, 2:] = [[1.0, alpha, 0.0], [0.0, -gamma, -1.0]]
    E = expm(M * h)
    return E[:2, :2], E[:2, 2:]


def _ss_columns(phi, gam, inputs, imp):
    """Outputs Ta of the pair driven by each input (padded by one zero) from a zero state, and the responses
    to the initial Ta and w (imp: unit impulse of the output length)."""
    tr = phi[0, 0] + phi[1, 1]
    det = phi[0, 0] * phi[1, 1] - phi[0, 1] * phi[1, 0]
    den = [1.0, -tr, det]
    cols = []
    for j, v in enumerate(inputs):
        num = [0.0, gam[0, j], -phi[1, 1] * gam[0, j] + phi[0, 1] * gam[1, j]]
        cols.append(lfilter(num, den, v))
    for j in range(2):
        s0 = 1.0 if j == 0 else 0.0
        s1 = phi[0, j]
        cols.append(lfilter([s0, s1 - tr * s0], den, imp))
    return cols


class Problem:
    """Weighted least squares of one window for the full or the reduced model."""

    def __init__(self, data: WindowData, full: bool, gain: float = 0.0) -> None:
        """gain: constant free gain (share of full power) of the heat input a (u_e + gain), taken as known."""
        self.data, self.full, self.gain = data, full, gain
        self.inputs = []
        self.imp = []
        for s in data.segments:
            i0, N = s["i0"], s["N"]
            self.inputs.append(tuple(np.append(x[i0:i0 + N], 0.0) for x in (data.ue, data.te_bin, data.dte_bin,
                                                                            np.ones(len(data.ue)))))
            imp = np.zeros(N + 1)
            imp[0] = 1.0
            self.imp.append(imp)
        self._steps = [np.arange(s["N"] + 1) for s in data.segments]

    # nonlinear parameters
    def params(self, x) -> dict:
        if self.full:
            alpha = math.exp(x[0])
            beta = math.exp(x[1])
            g = 1.0 / (1.0 + math.exp(-x[2]))
            return {"alpha": alpha, "beta": beta, "g": g, "c": g * alpha, "gamma": g * alpha * beta,
                    "b_eq": alpha * (1.0 - g)}
        b = math.exp(x[0])
        return {"alpha": b, "beta": 0.0, "g": 0.0, "c": 0.0, "gamma": 0.0, "b_eq": b}

    def in_domain(self, q: dict) -> bool:
        if not _ALPHA_MIN <= q["alpha"] <= RM_ALPHA_MAX:
            return False
        if self.full:
            return RM_BETA_RANGE[0] <= q["beta"] <= RM_BETA_RANGE[1] and 1e-3 < q["g"] < 0.999
        return True

    def columns(self, q: dict) -> list[list[np.ndarray]]:
        """Per segment, full-length response columns: Yu, Yte (with its coefficients), Ydte, Yg (a unit constant
        input entering like u_e), Y0, Yw."""
        out = []
        if self.full:
            phi, gam = _disc2(q["alpha"], q["beta"], q["gamma"], STEP)
            gam = np.c_[gam, gam[:, 0]]
            for inp, imp in zip(self.inputs, self.imp):
                out.append(_ss_columns(phi, gam, inp, imp))
        else:
            b = q["alpha"]
            e = math.exp(-b * STEP)
            gu = (1.0 - e) / b
            for (ue, te, _dte, one), steps in zip(self.inputs, self._steps):
                yu = lfilter([0.0, gu], [1.0, -e], ue)
                yt = lfilter([0.0, gu * b], [1.0, -e], te)
                yo = lfilter([0.0, gu], [1.0, -e], one)
                zero = np.zeros(len(steps))
                out.append([yu, yt, zero, yo, e ** steps, zero])
        return out

    def solve(self, x, a_fixed: float | None = None, detail: bool = False):
        check()
        q = self.params(x)
        if not self.in_domain(q):
            return math.inf, None
        cols = self.columns(q)
        k_te = 0.5 * (q["c"] + q["alpha"]) if self.full else 0.0
        per = 2 if self.full else 1
        nseg = len(cols)
        glob = 0 if a_fixed is not None else 1
        ncol = glob + per * nseg
        rows, rhs, wts = [], [], []
        for si, (yu, yt, yd, yo, y0, yw) in enumerate(cols):
            s = self.data.segments[si]
            k = s["k"]
            M = np.zeros((len(k), ncol))
            off = yt[k] + k_te * yd[k]
            # heat input a (u_e + g) with the known gain g
            yuk = yu[k] + self.gain * yo[k]
            if a_fixed is None:
                M[:, 0] = yuk
            else:
                off = off + a_fixed * yuk
            base = glob + per * si
            M[:, base] = y0[k]
            if self.full:
                M[:, base + 1] = yw[k]
            rows.append(M)
            rhs.append(s["y"] - off)
            wts.append(s["w"])
        A = np.vstack(rows)
        r = np.concatenate(rhs)
        sw = np.sqrt(np.concatenate(wts))
        AA, rr = A * sw[:, None], r * sw
        th, *_ = np.linalg.lstsq(AA, rr, rcond=None)
        a_val = a_fixed if a_fixed is not None else float(th[0])
        if a_val <= 0.0 or q["b_eq"] <= 0.0:
            return math.inf, None
        res = (r - A @ th) * sw
        cost = float(res @ res)
        if not detail:
            return cost, th
        out = dict(q, a=float(a_val), k_te=k_te, cost=cost, x=[float(v) for v in x], gain=self.gain)
        init = []
        for si in range(nseg):
            base = glob + per * si
            init.append((float(th[base]), float(th[base + 1]) if self.full else 0.0))
        out["init"] = init
        out["check"] = self._check_residuals(cols, a_val, k_te, init)
        return cost, out

    def _check_residuals(self, cols, a, k_te, init) -> dict:
        res, U, S, T = [], [], [], []
        d = self.data
        for (yu, yt, yd, yo, y0, yw), s, (ta0, w0) in zip(cols, d.segments, init):
            kc = s["kc"]
            model = a * (yu[kc] + self.gain * yo[kc]) + yt[kc] + k_te * yd[kc] + ta0 * y0[kc] + w0 * yw[kc]
            res.append(s["yc"] - model)
            idx = s["i0"] + kc
            U.append(d.ue[np.clip(idx - 1, 0, len(d.ue) - 1)])
            S.append(d.sp[idx])
            T.append(d.te[idx])
        return residual_checks(res, U, S, T, lag_1h=int(round(H / RM_CHECK_STEP_S)))


def _nm(fun, x0, scales, maxiter=400):
    x0 = np.asarray(x0, dtype=float)
    simplex = [x0] + [x0 + np.eye(len(x0))[i] * scales[i] for i in range(len(x0))]
    r = minimize(fun, x0, method="Nelder-Mead",
                 options={"xatol": 1e-3, "fatol": 1e-10, "maxiter": maxiter, "initial_simplex": np.array(simplex)})
    return r.x, float(r.fun)


def _logit(g: float) -> float:
    return math.log(g / (1.0 - g))


def fit(pb: Problem, warm: dict | None = None) -> dict | None:
    """Least-squares fit (grid then Nelder-Mead); warm: parameters of an earlier fit used as an extra start."""
    if not pb.data.segments:
        return None
    if not pb.full:
        lo, hi = math.log(RM_BEQ_RANGE[0]), math.log(RM_ALPHA_MAX)
        grid = np.linspace(lo, hi, 16)
        vals = [pb.solve([x])[0] for x in grid]
        j = int(np.argmin(vals))
        if not np.isfinite(vals[j]):
            return None
        a, b = grid[max(0, j - 1)], grid[min(len(grid) - 1, j + 1)]
        r = minimize_scalar(lambda x: pb.solve([x])[0], bounds=(a, b), method="bounded",
                            options={"xatol": 1e-4})
        return pb.solve([float(r.x)], detail=True)[1]
    cands = []
    for ta in RM_TAU_AIR_GRID_H:
        for tw in RM_TAU_WALL_GRID_H:
            for g in RM_G_GRID:
                x = np.array([math.log(1.0 / (ta * H)), math.log(1.0 / (tw * H)), _logit(g)])
                v, _ = pb.solve(x)
                if np.isfinite(v):
                    cands.append((v, x))
    cands.sort(key=lambda z: z[0])
    starts = []
    for _, x in cands:
        if all(np.max(np.abs(x - s)) > 0.5 for s in starts):
            starts.append(x)
        if len(starts) >= RM_SEARCH_STARTS:
            break
    if warm and warm.get("alpha") and warm.get("beta") and warm.get("c") and warm["c"] < warm["alpha"]:
        starts.append(np.array([math.log(warm["alpha"]), math.log(warm["beta"]), _logit(warm["c"] / warm["alpha"])]))
    if not starts:
        return None
    best = None
    sc = np.array([0.3, 0.3, 0.5])
    for x in starts:
        z, fv = _nm(lambda y: pb.solve(y)[0], x, sc)
        z, fv = _nm(lambda y: pb.solve(y)[0], z, sc * 0.3, maxiter=200)
        if best is None or fv < best[0]:
            best = (fv, z)
    if not np.isfinite(best[0]):
        return None
    out = pb.solve(best[1], detail=True)[1]
    if out is not None:
        out["beta_at_bound"] = bool(out["beta"] <= RM_BETA_RANGE[0] * 1.05 or out["beta"] >= RM_BETA_RANGE[1] / 1.05)
    return out


def held_out(data: WindowData, params: dict, gain: float = 0.0) -> dict | None:
    """Residual checks of earlier parameters on this window: parameters frozen, initial states re-estimated."""
    full = params.get("struct") == "full"
    pb = Problem(data, full, gain)
    if full:
        x = [math.log(params["alpha"]), math.log(params["beta"]), _logit(params["c"] / params["alpha"])]
    else:
        x = [math.log(params["alpha"])]
    _, out = pb.solve(x, a_fixed=params["a"], detail=True)
    return None if out is None else out["check"]


# ---------------------------------------------------------------------------
# Precision of the identified parameters
# ---------------------------------------------------------------------------
_DX = 1e-3       # step of the numerical derivatives in the nonlinear parameters (log or logit units)
_DLOG_EM = 0.02  # step of the numerical derivative in the log of the emitter first moment


def _predictions(pb: Problem, x, a: float, init: list) -> np.ndarray | None:
    """Weighted model outputs at the anchors for the nonlinear parameters x, a and the initial states."""
    q = pb.params(x)
    cols = pb.columns(q)
    k_te = 0.5 * (q["c"] + q["alpha"]) if pb.full else 0.0
    out = []
    for (yu, yt, yd, yo, y0, yw), s, (ta0, w0) in zip(cols, pb.data.segments, init):
        k = s["k"]
        m = a * (yu[k] + pb.gain * yo[k]) + yt[k] + k_te * yd[k] + ta0 * y0[k] + w0 * yw[k]
        out.append(m * np.sqrt(s["w"]))
    return np.concatenate(out) if out else None


def precision(pb: Problem, fit: dict, emitter: tuple[Problem, Problem, float] | None = None) -> dict | None:
    """Standard deviations of the identifiable coordinates of a fit, from its Jacobian at the optimum.

    Least squares: the covariance of all the parameters (nonlinear ones, a, the initial air and wall state of
    every period, and the emitter first moment when the window estimated it) is s2 (J' J)^-1, with J the
    Jacobian of the weighted model outputs, s2 the residual variance per unit weight and the result scaled by
    the integrated autocorrelation time of the residuals (count over effective count): correlated residuals
    carry less information than their number. The linear parameters are included, so their uncertainty is in
    the result (it equals the profile over them).

    emitter: (problem at first moment x exp(-h), problem at first moment x exp(+h), h) when the emitter was
    estimated by the window; its uncertainty then enters every coordinate.

    Returns {"sd": {"a", "b_eq_a"[, "alpha", "c"]}, "tau"} with the standard deviations of the logarithms
    (relative), None when a coordinate is not determined (singular information).
    """
    d = pb.data
    x0 = np.asarray(fit["x"], dtype=float)
    a = float(fit["a"])
    init = fit["init"]
    nseg = len(d.segments)
    if nseg == 0 or a <= 0.0:
        return None
    base = _predictions(pb, x0, a, init)
    if base is None:
        return None
    cols = []
    nx = len(x0)
    for j in range(nx):
        e = np.zeros(nx)
        e[j] = _DX
        hi, lo = _predictions(pb, x0 + e, a, init), _predictions(pb, x0 - e, a, init)
        if hi is None or lo is None:
            return None
        cols.append((hi - lo) / (2.0 * _DX))
    # a enters linearly: derivative in ln a
    q = pb.params(x0)
    pcols = pb.columns(q)
    rows_a, n = [], 0
    for (yu, _yt, _yd, yo, _y0, _yw), s in zip(pcols, d.segments):
        k = s["k"]
        rows_a.append(a * (yu[k] + pb.gain * yo[k]) * np.sqrt(s["w"]))
    cols.append(np.concatenate(rows_a))
    for si, ((_yu, _yt, _yd, _yo, y0, yw), s) in enumerate(zip(pcols, d.segments)):
        k = s["k"]
        for col in ((y0,) + ((yw,) if pb.full else ())):
            v = np.zeros(len(base))
            v[n:n + len(k)] = col[k] * np.sqrt(s["w"])
            cols.append(v)
        n += len(k)
    if emitter is not None:
        lo_pb, hi_pb, h = emitter
        hi, lo = _predictions(hi_pb, x0, a, init), _predictions(lo_pb, x0, a, init)
        if hi is None or lo is None:
            return None
        cols.append((hi - lo) / (2.0 * h))
    J = np.column_stack(cols)
    p = J.shape[1]
    w_sum = float(sum(np.sum(s["w"]) for s in d.segments))
    dof = w_sum - p
    if dof <= 0.0:
        return None
    check = fit.get("check") or {}
    n_res, n_eff = check.get("n"), check.get("n_eff")
    tau = max(1.0, float(n_res) / float(n_eff)) if n_res and n_eff else 1.0
    s2 = float(fit["cost"]) / dof
    F = J.T @ J
    try:
        C = np.linalg.inv(F) * s2 * tau
    except np.linalg.LinAlgError:
        return None
    ia = nx

    def sd(grad: dict[int, float]) -> float | None:
        g = np.zeros(p)
        for i, v in grad.items():
            g[i] = v
        var = float(g @ C @ g)
        return math.sqrt(var) if math.isfinite(var) and var >= 0.0 else None

    out = {"a": sd({ia: 1.0})}
    if pb.full:
        sig = q["g"]
        # ln alpha = x0; ln c = x0 + ln g; ln b_eq = x0 + ln(1 - g), g = 1 / (1 + exp(-x2))
        out["b_eq_a"] = sd({0: 1.0, 2: -sig, ia: -1.0})
        out["alpha"] = sd({0: 1.0})
        out["c"] = sd({0: 1.0, 2: 1.0 - sig})
    else:
        out["b_eq_a"] = sd({0: 1.0, ia: -1.0})
    if any(v is None for v in out.values()):
        return None
    return {"sd": out, "tau": tau}


# ---------------------------------------------------------------------------
# Residual checks
# ---------------------------------------------------------------------------
def residual_checks(res_list, u_list, sp_list, te_list, lag_1h: int) -> dict:
    """Bias, root mean square, runs test, autocorrelation (1 step and 1 h), correlation with the regressors."""
    r = np.concatenate(res_list) if res_list else np.zeros(0)
    if len(r) < 3:
        return {"n": int(len(r))}
    out = {"n": int(len(r)), "mean": float(np.mean(r)), "median": float(np.median(r)),
           "rms": float(np.sqrt(np.mean(r ** 2)))}
    out["runs_z_10min"] = runs_z(r)
    block = max(1, int(round(RM_RES_RUNS_BLOCK_S / RM_CHECK_STEP_S)))
    means = [x[: (len(x) // block) * block].reshape(-1, block).mean(axis=1) for x in res_list if len(x) >= block]
    means = np.concatenate(means) if means else np.zeros(0)
    out["runs_blocks"] = int(len(means))
    out["runs_z"] = runs_z(means) if len(means) >= RM_RES_RUNS_MIN_BLOCKS else None
    out["n_eff"] = effective_count(res_list)

    def ac(lag):
        num = den = 0.0
        for x in res_list:
            x = x - x.mean()
            if len(x) > lag:
                num += float(np.sum(x[lag:] * x[:-lag]))
            den += float(np.sum(x * x))
        return num / den if den > 0 else None

    out["ac1"] = ac(1)
    out["ac1h"] = ac(lag_1h)
    for nm, lst in (("u", u_list), ("sp", sp_list), ("te", te_list)):
        x = np.concatenate(lst)
        out[f"corr_{nm}"] = (float(np.corrcoef(r, x)[0, 1])
                             if np.std(x) > 1e-9 and np.std(r) > 1e-12 else None)
    return out


def runs_z(r: np.ndarray) -> float | None:
    """Wald-Wolfowitz runs test of the signs of a sequence (z score, negative for long runs)."""
    s = np.sign(np.asarray(r, dtype=float))
    s = s[s != 0]
    n1, n2 = int(np.sum(s > 0)), int(np.sum(s < 0))
    runs = 1 + int(np.sum(s[1:] != s[:-1])) if len(s) else 0
    mu = 2.0 * n1 * n2 / max(1, n1 + n2) + 1.0
    var = (mu - 1.0) * (mu - 2.0) / max(1, n1 + n2 - 1)
    return float((runs - mu) / math.sqrt(var)) if var > 0 else None


def effective_count(res_list) -> float:
    """Effective number of independent residuals: their count over the integrated autocorrelation time
    1 + 2 sum(rho_k), the sum taken over the lags before the pooled autocorrelation first drops to zero."""
    xs = [np.asarray(x, dtype=float) - np.mean(x) for x in res_list if len(x) > 1]
    n = sum(len(x) for x in xs)
    den = sum(float(np.sum(x * x)) for x in xs)
    if n == 0 or den <= 0.0:
        return float(n)
    tau = 1.0
    for lag in range(1, max(len(x) for x in xs)):
        num = sum(float(np.sum(x[lag:] * x[:-lag])) for x in xs if len(x) > lag)
        rho = num / den
        if rho <= 0.0:
            break
        tau += 2.0 * rho
    return n / tau
