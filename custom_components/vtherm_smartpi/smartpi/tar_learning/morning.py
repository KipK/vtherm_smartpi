"""Comfort-step estimator of T_ar (numpy, scipy).

Each comfort step (setpoint rise at t_sp that ends a quiet period) is a step
response of the emitter. The response windows end at the latest at the end of
the darkness (fit_end): data after sunrise are never fitted, and the gates
below judge the truncated windows (a window cut by sunrise also has to hold
the longest delay known before the step, cut_refusal). Its record keeps the
name "morning". The response fit (response.py) needs a baseline, the
trajectory the room would have followed without the step, and a room response:

- passive night (no heater start in the 2 h before the step t_s, the first
  heater start within 15 min of t_sp): the baseline follows the night class
  (night indicator, room_mass.py): two-constant night fit extrapolated with the
  outdoor temperature ('r2night'), or an exponential fitted on the 2 h before
  the step ('exp2h');
- regulated night handled by the night class: linear trend of the 2 h before
  ('lin2h', 'lin2h_2m'), with a baseline rms over PWM-cycle means;
- regulated night refused by the night class ("regulated branch"): stationary
  regulation over the 2 h before t_sp, linear baseline of the per-cycle anchor
  means, du = u - u_hold. On a room classed 'mass' (three long passive nights)
  the room response is the air-wall response with (k, tau_w) learned from the
  passive nights; on a room classed 'one' the one-constant response; a room of
  unknown mass takes the class and (k, tau_w) of the multi-day identification
  in force, and stays silent without one.

Every path ends with the same gates (baseline quality, fit quality,
excitation, sensor period). An accepted morning gives T on the window that ends
when the room reaches the new setpoint (10 min to 2 h).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .const import (
    BASE_SPAN_S,
    CLASS_REASONS,
    CYC_MIN_ANCHORS,
    CYC_MIN_BINS,
    END_RISE_S,
    R_JUMP_CYCLES,
    R_POST_S,
    R_QUARTER_S,
    R_SPAN_S,
    RULES,
    RULES_R,
)
from .response import DT, KernelCache, fit_windows, window_rise, z_all
from .room_mass import (
    classify_night,
    is_long,
    learn_room,
    loss_2mode_night,
    loss_2mode_wall,
    night_segment,
    predict_2exp,
    room_class_long,
)
from .signals import Anchors, ExtTL, level_on_grid, sensor_period, u_on_grid

WINDOWS = ("W45", "Wsp", "W2h")
# Shortest response window left before sunrise for a fit
RESPONSE_MIN_S = 600.0
CUT_REASON = "excitation: cut by sunrise"
# Decision paths: night-class baseline (night without heating or regulated night),
# regulated night with the night-class baseline, regulated branch
PATH_NIGHT = "night"
PATH_NIGHT_REGULATED = "night_regulated"
PATH_REGULATED = "regulated"
FW = "fo|Wsp"
STABLE = "STABLE"


@dataclass
class MorningInputs:
    """Data of the room around one morning (all on the same time axis)."""

    anch: Anchors
    ext: ExtTL
    ext_t: np.ndarray
    ext_v: np.ndarray
    on: np.ndarray
    off: np.ndarray
    spans: list
    params_at: Callable[[float], tuple]
    phase_at: Callable[[float], str]
    # Quiet intervals (no sun, constant setpoint, valid data) of the period the step ends
    quiet: list
    cycle_s: float
    b_pub: float
    valid_until: Callable[[float, float], float] | None = None
    cache: KernelCache = field(default_factory=KernelCache)
    # Valve power level timeline (times, levels); None for a PWM heater (edges)
    power: tuple | None = None
    # End of the darkness: no response window goes beyond it (None: no limit)
    fit_end: float | None = None
    # Shortest first moment the emitter can have from what is known before the step (largest of the
    # refined value in force, the published fast value and twice the bootstrap dead time), None if unknown
    t_floor: float | None = None

    def cut(self, ends: dict) -> dict:
        """Response window ends truncated at fit_end."""
        if self.fit_end is None:
            return ends
        return {wn: min(e, self.fit_end) for wn, e in ends.items()}

    def cut_info(self, ends: dict, cut_ends: dict) -> dict | None:
        """What the gates need when sunrise cuts the window Wsp (None when it does not)."""
        if self.fit_end is None or cut_ends["Wsp"] >= ends["Wsp"] - 1e-6:
            return None
        return {"floor": self.t_floor}

    def u_grid(self, t0: float, t1: float):
        """Heater input (0..1) on the fit grid [t0, t1)."""
        if self.power is not None:
            return level_on_grid(self.power[0], self.power[1], t0, t1)
        return u_on_grid(self.on, self.off, t0, t1)


def cut_refusal(cut: dict | None, T: float, win: float) -> str | None:
    """Why a response window cut by sunrise cannot give a value, None when it can (or is not cut).

    The window Wsp is cut when sunrise comes before the room reaches the new
    setpoint. A short window lets a slow emitter fit a short delay, and that short
    value then satisfies its own window rule (window >= exc_window_T x T). A cut
    window must therefore pass the rule for the largest delay known before the
    step (MorningInputs.t_floor) as well as for the fitted one. Without any such
    reference the window cannot be checked and gives nothing.
    """
    if cut is None:
        return None
    floor = cut.get("floor")
    if floor is None or win < RULES["exc_window_T"] * max(T, floor):
        return CUT_REASON
    return None


# ---------------------------------------------------------------------------
# Passive and regulated nights handled by the night class
# ---------------------------------------------------------------------------
def select_baseline(cls: str, has_fit: bool, room: str, passive: bool, long_night: bool) -> str | None:
    """Baseline of the morning from the night class and the room class (None: refused)."""
    if cls == "mass" and has_fit:
        return "r2night" if passive else "lin2h_2m"
    if cls == "one":
        if room == "mass":
            return None
        if room == "one" or long_night:
            return "exp2h" if passive else "lin2h"
        return None
    if cls == "edge" and has_fit and not passive:
        return "lin2h_2m"
    if room == "one":
        return "exp2h" if passive else "lin2h"
    return None


def baseline_2h(anch, ext: ExtTL, t_s: float, kind: str, b: float):
    """Linear trend or exponential (own b) on the 2 h before the step."""
    m = (anch.t >= t_s - 7200.0) & (anch.t <= t_s)
    tt, vv = anch.t[m], anch.v[m]
    if len(tt) < 4:
        return None, {"reason": "too few anchors before the step"}
    te = ext.integrals(t_s - 7200.0, t_s, 0.0)[0] / 7200.0
    if kind == "lin":
        A = np.vstack([np.ones_like(tt), tt - t_s]).T
        c, s = np.linalg.lstsq(A, vv, rcond=None)[0]
        res = vv - A @ np.array([c, s])
        return (lambda t: c + s * (t - t_s)), {"rms_base": float(np.sqrt(np.mean(res ** 2)))}
    bs = np.linspace(0.05, 3.0, 296) * b
    best = None
    for bb in bs:
        e = np.exp(-bb / 60.0 * (tt - t_s))
        k = float(np.dot(e, vv - te) / np.dot(e, e))
        res = vv - (te + k * e)
        sse = float(np.dot(res, res))
        if best is None or sse < best[0]:
            best = (sse, bb, k)
    sse, bb, k = best
    return (lambda t: te + k * np.exp(-bb / 60.0 * (t - t_s))), {"b_fit": bb, "rms_base": math.sqrt(sse / len(tt))}


def _rms_2h(anch, tb, t_s: float) -> float | None:
    m = (anch.t >= t_s - BASE_SPAN_S) & (anch.t <= t_s)
    if m.sum() < 4:
        return None
    r = anch.v[m] - tb(anch.t[m])
    return float(np.sqrt(np.mean(r * r)))


def make_baseline(name: str, inp: MorningInputs, t_s: float, b_hat: float, night_fit):
    """(tb, loss, info) or (None, None, info with the reason)."""
    if name in ("exp2h", "lin2h", "lin2h_2m"):
        kind = "exp" if name == "exp2h" else "lin"
        tb, info = baseline_2h(inp.anch, inp.ext, t_s, kind, b_hat)
        if tb is None:
            return None, None, info
        if name == "exp2h":
            loss = {"kind": "fo", "b_per_min": info["b_fit"]}
        elif name == "lin2h":
            loss = {"kind": "fo", "b_per_min": b_hat}
        else:
            loss = loss_2mode_night(night_fit["_params"], night_fit["r2"].get("b_cut_per_min"))
        return tb, loss, dict(info)
    p = night_fit["_params"]

    def tb(t, p=p):
        return predict_2exp(p, inp.ext_t, inp.ext_v, t)

    info = {"rms_base": _rms_2h(inp.anch, tb, t_s)}
    if info["rms_base"] is None:
        return None, None, {"reason": "too few anchors before the step"}
    return tb, loss_2mode_night(p, night_fit["r2"].get("b_cut_per_min")), info


def rms_cycle(anch, tb, t_s: float, cycle_s: float) -> float | None:
    """rms of the baseline residuals averaged over PWM-cycle bins ending at the step."""
    m = (anch.t >= t_s - BASE_SPAN_S) & (anch.t <= t_s)
    t = anch.t[m]
    if len(t) < 4:
        return None
    r = anch.v[m] - tb(t)
    k = np.floor((t_s - t) / cycle_s).astype(int)
    k = np.minimum(k, int(BASE_SPAN_S // cycle_s) - 1)
    means = []
    for j in np.unique(k):
        sel = k == j
        if sel.sum() >= CYC_MIN_ANCHORS:
            means.append(float(np.mean(r[sel])))
    if len(means) < CYC_MIN_BINS:
        return None
    a = np.asarray(means)
    return float(np.sqrt(np.mean(a * a)))


def _fit_step(inp: MorningInputs, du, gw, t_s: float, ends: dict, tb, loss: dict) -> dict:
    anch = inp.anch
    m = (anch.t > t_s) & (anch.t <= t_s + 7200.0)
    ta = anch.t[m]
    ya = anch.v[m] - tb(ta)
    idx_all = np.clip(((ta - t_s) / DT).astype(int), 0, len(gw) - 1)
    Z = z_all(du, loss, "fo", inp.cache)
    out = {f"fo|{wn}": v for wn, v in fit_windows(Z, idx_all, ya, ta, ends, WINDOWS).items()}
    out["rise"] = window_rise(ta, ya, ends, WINDOWS, END_RISE_S)
    return out


def morning_night_class(inp: MorningInputs, t_sp: float, sp_to: float, nights: list[dict]) -> dict:
    """Night indicator and the night-class paths of one morning; appends the night to `nights`."""
    on, anch = inp.on, inp.anch
    starts = [x for x in on if t_sp - 30.0 <= x <= t_sp + 900.0]
    t_s = float(starts[0]) if starts else None
    seg = night_segment(anch, inp.spans, inp.params_at, t_s if t_s is not None else t_sp, inp.quiet,
                        inp.valid_until)
    night, night_fit = classify_night(anch, inp.ext_t, inp.ext_v, seg)
    nights.append(night)
    room = room_class_long(nights)
    row = {"t_sp": t_sp, "night_ind": night, "room_class": room}
    if t_s is None:
        row["reason"] = "no heater start after the comfort step"
        return row
    phase = inp.phase_at(t_s)
    row.update(t_s=t_s, phase=phase)
    if phase != STABLE:
        row["reason"] = f"phase {phase}"
        return row
    if inp.fit_end is not None and inp.fit_end - t_s < RESPONSE_MIN_S:
        row["reason"] = "sunrise too close to the comfort step"
        return row
    passive = not any(t_s - 7200.0 <= x < t_s - 30.0 for x in on)
    g0 = t_s - 1800.0
    grid, u = inp.u_grid(g0, t_s + 7200.0 + DT)
    u_base = 0.0 if passive else float(np.mean(u[grid < t_s]))
    i0 = int(np.searchsorted(grid, t_s))
    du = u[i0:] - u_base
    gw = grid[i0:]
    reach = anch.first_reach(t_s, sp_to - 0.1)
    uncut = {"W45": t_s + 2700.0, "W2h": t_s + 7200.0,
             "Wsp": min(t_s + 7200.0, max(t_s + 600.0, reach)) if reach else t_s + 7200.0}
    ends = inp.cut(uncut)
    row["cut"] = inp.cut_info(uncut, ends)
    sel = select_baseline(night["cls"], night_fit is not None, room, passive, is_long(night))
    n_w = {wn: int(round((ends[wn] - t_s) / DT)) for wn in ends}
    row.update(night="passive" if passive else "regulated", sel=sel,
               win_s={wn: ends[wn] - t_s for wn in ends},
               energy_s={wn: float(np.sum(du[:n_w[wn]]) * DT) for wn in ends},
               sensor_period_s=sensor_period(anch.pub_t, anch.pub_v, t_s), q=anch.q)
    if sel is None:
        row["reason"] = "night not usable for the room class"
        return row
    tb, loss, info = make_baseline(sel, inp, t_s, inp.b_pub, night_fit)
    if tb is None:
        row["reason"] = info.get("reason", "baseline")
        return row
    row.update(rms_base=info.get("rms_base"), rms_cyc=rms_cycle(anch, tb, t_s, inp.cycle_s), loss=loss,
               fit=_fit_step(inp, du, gw, t_s, ends, tb, loss))
    return row


# ---------------------------------------------------------------------------
# Regulated-night branch
# ---------------------------------------------------------------------------
def regulated_before(on, t_sp: float) -> bool:
    return any(t_sp - R_SPAN_S <= x < t_sp - 30.0 for x in on)


def cycle_bins(t0: float, t1: float, cycle_s: float) -> list[tuple[float, float]]:
    """Consecutive bins of one cycle ending at t1 and covering [t0, t1]."""
    n = int(round((t1 - t0) / cycle_s))
    return [(t1 - (j + 1) * cycle_s, t1 - j * cycle_s) for j in range(n)][::-1]


def _bin_means_u(grid, u, bins):
    return np.asarray([float(np.mean(u[(grid >= a) & (grid < b)])) for a, b in bins])


def stationarity(anch, on, grid, u, t_sp: float, cycle_s: float, eco: float | None) -> dict:
    quarters = [sum(1 for x in on if t_sp - R_SPAN_S + j * R_QUARTER_S <= x < t_sp - R_SPAN_S + (j + 1) * R_QUARTER_S)
                for j in range(int(R_SPAN_S // R_QUARTER_S))]
    bins = cycle_bins(t_sp - R_SPAN_S, t_sp, cycle_s)
    um = _bin_means_u(grid, u, bins)
    tc = np.asarray([(0.5 * (a + b) - t_sp) / 3600.0 for a, b in bins])
    s_u, c_u = np.polyfit(tc, um, 1)
    resid = um - (s_u * tc + c_u)
    post = cycle_bins(t_sp, t_sp + R_JUMP_CYCLES * cycle_s, cycle_s)
    up = _bin_means_u(grid, u, post)
    m30 = (anch.t >= t_sp - 1800.0) & (anch.t <= t_sp)
    t30 = float(np.mean(anch.v[m30])) if m30.sum() else None
    return {"quarters": quarters, "u_hold": float(np.mean(um)), "u_slope_h": float(s_u),
            "u_resid_std": float(np.std(resid)), "jump": float(np.max(up) - np.mean(um)),
            "eco_dev": None if (t30 is None or eco is None) else t30 - eco}


def baseline_cycles(anch, t_sp: float, cycle_s: float):
    """Linear baseline on the per-cycle anchor means of the 2 h before t_sp."""
    bins = cycle_bins(t_sp - R_SPAN_S, t_sp, cycle_s)
    tm, vm = [], []
    for a, b in bins:
        sel = (anch.t >= a) & (anch.t < b) if b < t_sp else (anch.t >= a) & (anch.t <= b)
        if sel.sum() >= CYC_MIN_ANCHORS:
            tm.append(float(np.mean(anch.t[sel])) - t_sp)
            vm.append(float(np.mean(anch.v[sel])))
    if len(tm) < CYC_MIN_BINS:
        return None, None
    tm, vm = np.asarray(tm), np.asarray(vm)
    s, c = np.polyfit(tm, vm, 1)
    r_lin = vm - (c + s * tm)

    def lin(t, c=c, s=s):
        return c + s * (np.asarray(t) - t_sp)

    return lin, float(np.sqrt(np.mean(r_lin ** 2)))


def branch_r(inp: MorningInputs, t_sp: float, eco: float | None, sp_to: float) -> dict:
    """Regulated branch of one morning: statistics, setup of the response fit and the 1R1C fit."""
    anch, on, cycle_s = inp.anch, inp.on, inp.cycle_s
    g0 = t_sp - R_SPAN_S
    grid, u = inp.u_grid(g0, t_sp + R_POST_S + DT)
    st = stationarity(anch, on, grid, u, t_sp, cycle_s, eco)
    t_end = t_sp + R_POST_S
    reach = anch.first_reach(t_sp, sp_to - 0.1)
    uncut = {"W45": min(t_sp + 2700.0, t_end), "W2h": t_end,
             "Wsp": min(t_end, min(t_sp + 7200.0, max(t_sp + 600.0, reach)) if reach else t_sp + 7200.0)}
    ends = inp.cut(uncut)
    i0 = int(np.searchsorted(grid, t_sp))
    du_full = u - st["u_hold"]
    n_w = {wn: int(round((ends[wn] - t_sp) / DT)) for wn in ends}
    out = {"stat": st, "q": anch.q, "sensor_period_s": sensor_period(anch.pub_t, anch.pub_v, t_sp),
           "cut": inp.cut_info(uncut, ends),
           "win_s": {wn: ends[wn] - t_sp for wn in ends},
           "energy_s": {wn: float(np.sum(du_full[i0:i0 + n_w[wn]]) * DT) for wn in ends}}
    tb, rms_cyc = baseline_cycles(anch, t_sp, cycle_s)
    out["rms_cyc"] = rms_cyc
    if tb is None:
        return out
    m = (anch.t > t_sp) & (anch.t <= t_sp + R_POST_S)
    ta = anch.t[m]
    ya = anch.v[m] - tb(ta)
    idx_all = np.clip(((ta - g0) / DT).astype(int), 0, len(du_full) - 1)
    out["_setup"] = {"du": du_full, "ta": ta, "ya": ya, "idx": idx_all, "ends": ends}
    Z = z_all(du_full, {"kind": "fo", "b_per_min": inp.b_pub}, "fo", inp.cache)
    fit = {f"fo|{wn}": v for wn, v in fit_windows(Z, idx_all, ya, ta, ends, WINDOWS).items()}
    fit["rise"] = window_rise(ta, ya, ends, WINDOWS, END_RISE_S)
    out["fit"] = fit
    return out


def fit_wall(inp: MorningInputs, setup: dict, wall: dict) -> dict:
    """Regulated-branch fit (Wsp, W2h) with the air-wall room response."""
    if wall["k_per_s"] == 0.0:
        loss = {"kind": "fo", "b_per_min": inp.b_pub}
    else:
        loss = loss_2mode_wall(inp.b_pub / 60.0, wall["k_per_s"], wall["tau_w_s"])
    Z = z_all(setup["du"], loss, "fo", inp.cache)
    return fit_windows(Z, setup["idx"], setup["ya"], setup["ta"], setup["ends"], ("Wsp", "W2h"))


# ---------------------------------------------------------------------------
# Gates and decision path
# ---------------------------------------------------------------------------
def gate_night_class(r: dict) -> tuple[bool, str]:
    """(accepted, first failed rule) of a night-class morning."""
    if "reason" in r:
        return False, r["reason"]
    q = r.get("q") or 0.1
    thr = max(RULES["base_rms_c"], RULES["base_rms_q"] * q)
    if r.get("night") == "regulated":
        if r.get("rms_cyc") is None:
            return False, "baseline (cycle bins)"
        if r["rms_cyc"] > thr:
            return False, "baseline rms"
    else:
        if r.get("rms_base") is None:
            return False, "baseline"
        if r["rms_base"] > thr:
            return False, "baseline rms"
    x = (r.get("fit") or {}).get(FW) or {}
    T = x.get("T")
    if T is None or x.get("edge") or not x.get("a_per_min") or x["a_per_min"] <= 0:
        return False, "fit invalid"
    if x["rms"] > max(RULES["fit_rms_c"], RULES["fit_rms_q"] * q):
        return False, "fit rms"
    why = cut_refusal(r.get("cut"), T, r["win_s"]["Wsp"])
    if why is not None:
        return False, why
    if r["win_s"]["Wsp"] < RULES["exc_window_T"] * T:
        return False, "excitation: window"
    if r["energy_s"]["Wsp"] < max(RULES["exc_energy_T"] * T, RULES["exc_energy_min_s"]):
        return False, "excitation: energy"
    rise = r["fit"]["rise"].get("Wsp")
    if rise is None or rise < max(RULES["exc_rise_c"], RULES["exc_rise_q"] * q):
        return False, "excitation: rise"
    p = r.get("sensor_period_s")
    if p is None or T < RULES["sensor_T_factor"] * p:
        return False, "sensor period"
    return True, "accepted"


def path_of(r: dict) -> str:
    if r.get("regulated_tsp") and r.get("night") != "passive" and r.get("reason") in CLASS_REASONS:
        return PATH_REGULATED
    if r.get("night") == "regulated" and "reason" not in r:
        return PATH_NIGHT_REGULATED
    return PATH_NIGHT


def gate_night_regulated(r: dict) -> tuple[bool, str]:
    """Night-class gate on a regulated night with the cycle-averaged rms threshold fixed."""
    if r.get("rms_cyc") is None:
        return False, "baseline (cycle bins)"
    if r["rms_cyc"] > RULES_R["reg_rms_cyc_c"]:
        return False, "baseline rms (fixed 0.03 C)"
    return gate_night_class(dict(r, rms_cyc=0.0))


def _gate_fit(x, q, win, energy, rise, period, cut: dict | None = None) -> tuple[bool, str]:
    T = x.get("T") if x else None
    if T is None or x.get("edge") or not x.get("a_per_min") or x["a_per_min"] <= 0:
        return False, "fit invalid"
    if x["rms"] > max(RULES["fit_rms_c"], RULES["fit_rms_q"] * q):
        return False, "fit rms"
    why = cut_refusal(cut, T, win)
    if why is not None:
        return False, why
    if win < RULES["exc_window_T"] * T:
        return False, "excitation: window"
    if energy < max(RULES["exc_energy_T"] * T, RULES["exc_energy_min_s"]):
        return False, "excitation: energy"
    if rise is None or rise < max(RULES["exc_rise_c"], RULES["exc_rise_q"] * q):
        return False, "excitation: rise"
    if period is None or T < RULES["sensor_T_factor"] * period:
        return False, "sensor period"
    return True, "accepted"


def gate_r(r: dict) -> tuple[bool, str]:
    R = r.get("R")
    if R is None:
        return False, "regulated: not computed"
    if r.get("phase_tsp") != STABLE:
        return False, f"phase {r.get('phase_tsp')}"
    st = R["stat"]
    if min(st["quarters"]) < RULES_R["r_quarter_min_starts"]:
        return False, "regulated: regulation not established over 2 h"
    if st.get("eco_dev") is None or abs(st["eco_dev"]) > RULES_R["r_eco_tol_c"]:
        return False, "regulated: not at eco"
    if abs(st["u_slope_h"]) > RULES_R["r_u_slope_max_h"] or st["u_resid_std"] > RULES_R["r_u_resid_max"]:
        return False, "regulated: holding power not stationary"
    if st["jump"] < RULES_R["r_jump_min"]:
        return False, "regulated: no power jump"
    rc = R.get("rms_cyc")
    if rc is None:
        return False, "regulated: baseline (cycle bins)"
    if rc > RULES_R["reg_rms_cyc_c"]:
        return False, "regulated: baseline rms"
    q = R.get("q") or 0.1
    fit = R.get("fit") or {}
    return _gate_fit(fit.get(FW), q, R["win_s"]["Wsp"], R["energy_s"]["Wsp"], (fit.get("rise") or {}).get("Wsp"),
                     R.get("sensor_period_s"), R.get("cut"))


def decide(r: dict) -> dict:
    """Decision of a morning row: path, accepted, T and the first failed rule."""
    p = path_of(r)
    if p == PATH_REGULATED:
        ok, why = gate_r(r)
        x = (r["R"].get("fit") or {}).get(FW) or {}
    elif p == PATH_NIGHT_REGULATED:
        ok, why = gate_night_regulated(r)
        x = (r.get("fit") or {}).get(FW) or {}
    else:
        ok, why = gate_night_class(r)
        x = (r.get("fit") or {}).get(FW) or {}
    return {"path": p, "acc": bool(ok), "why": why, "T": x.get("T") if ok else None}


def _with_wall_fit(r: dict, wall_fit: dict | None) -> dict:
    R2 = dict(r["R"])
    fit = dict(R2["fit"])
    if wall_fit is None:
        fit[FW] = {"T": None, "edge": True}
    else:
        for wn in ("Wsp", "W2h"):
            fit[f"fo|{wn}"] = wall_fit[wn]
    R2["fit"] = fit
    return dict(r, R=R2)


def decide_morning(inp: MorningInputs, row: dict, refined_cls: str, refined_wall: dict | None) -> dict:
    """Final decision of a morning on the regulated branch and the night-class paths.

    row: morning_night_class row completed with regulated_tsp, phase_tsp and R (branch_r).
    The room class of the regulated branch is the night-indicator class with
    its learned (k, tau_w) ('mass'), 'one', or unknown; an unknown room takes the
    refined class in force (refined_cls, refined_wall), else stays silent.
    """
    R = row.get("R")
    learned = row.get("_learned")
    room = row.get("room_class")
    cls = "mass" if (room == "mass" and learned) else ("one" if room == "one" else "unknown")
    setup = (R or {}).get("_setup")
    use = row
    term = None
    if R is not None and R.get("fit") is not None and setup is not None and cls == "mass":
        use = _with_wall_fit(row, fit_wall(inp, setup, learned))
        term = "learned"
    dec = decide(use)
    dec["cls"] = cls
    dec["term"] = term if dec["path"] == PATH_REGULATED else None
    dec["ref"] = None
    if dec["path"] != PATH_REGULATED or cls != "unknown":
        return dec
    # Unknown mass on the regulated branch: the refined class in force, else silent.
    if refined_cls == "one":
        dec = decide(row)
        dec.update(cls="one", term=None, ref="one")
        return dec
    if refined_cls == "mass" and refined_wall is not None:
        if setup is None or R.get("fit") is None:
            return {"path": PATH_REGULATED, "acc": False, "why": "regulated: refined mass, no regulated-branch setup", "T": None,
                    "cls": "unknown", "term": None, "ref": "mass"}
        dec = decide(_with_wall_fit(row, fit_wall(inp, setup, refined_wall)))
        dec.update(cls="mass", term="refined", ref="mass")
        return dec
    return {"path": PATH_REGULATED, "acc": False, "why": "regulated: unknown mass (silent)", "T": None, "cls": "unknown",
            "term": None, "ref": None}


def evaluate_morning(inp: MorningInputs, t_sp: float, eco: float | None, sp_to: float, nights: list[dict],
                     refined_cls: str = "unknown", refined_wall: dict | None = None) -> dict:
    """Night indicator, every path and the decision of one morning (appends the night to `nights`)."""
    row = morning_night_class(inp, t_sp, sp_to, nights)
    reg = regulated_before(inp.on, t_sp)
    row["regulated_tsp"] = reg
    row["phase_tsp"] = inp.phase_at(t_sp)
    if reg:
        rr = branch_r(inp, t_sp, eco, sp_to)
        row["R"] = rr
        if rr.get("fit") is not None:
            row["_learned"] = learn_room(nights, inp.b_pub)
    dec = decide_morning(inp, row, refined_cls, refined_wall)
    loss = row.get("loss") or {}
    b_fit_rel = (loss.get("b_per_min") / inp.b_pub
                 if loss.get("kind") == "fo" and row.get("sel") == "exp2h" and loss.get("b_per_min") and inp.b_pub
                 else None)
    night = row["night_ind"]
    out = {
        "t_sp": t_sp,
        "t_s": row.get("t_s"),
        "night": row.get("night"),
        "night_cls": night.get("cls"),
        "room_class": row.get("room_class"),
        "sel": row.get("sel"),
        "b_fit_rel": b_fit_rel,
        "learned": row.get("_learned"),
        "accepted": dec["acc"],
        "T": dec["T"],
        "why": dec["why"],
        "path": dec["path"],
        "cls": dec["cls"],
        "term": dec["term"],
        "ref": dec["ref"],
    }
    return out
