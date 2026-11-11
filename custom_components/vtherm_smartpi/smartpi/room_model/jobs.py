"""Identification of the room model and daily energy balance, run in an executor (numpy, scipy).

The jobs take a snapshot of plain Python data and return plain Python data,
so they never touch the event loop state.

Energy balance (balance_job): the static model of the whole local days before
the day of the snapshot (energy_balance.py), computed once a day whatever the
quiet periods. Identification (identify_job): the window of three closed quiet
periods of the snapshot (periods.py); a candidate's b_eq / a must agree with
the slope in force (snap["eb"], None before a first qualified value; gates.py).

Emitter search: the full model is fitted with the emitter first moment on the
fixed grid RM_EMITTER_GRID_S, split into tau_e and L like the manager does;
the lowest cost, refined by a bounded Brent search in log time between its
grid neighbours, sets the emitter of the
window's fits. The grid does not depend on the dead time in force. Every fit
takes the constant free gain in force of the energy balance as a known input
(identification.py). The cost profile is judged against the residual variance
per effective independent residual (const.py):
- significant, optimum at an end of the grid: the emitter lies outside it,
  the window is refused;
- not significant: the window does not determine the emitter; the emitter in
  force is used when the T_ar learning has published a refined value, else
  the grid optimum. The static loss of such a fit is then judged by its
  agreement with the energy balance.

Information: each fit carries the precision of its identifiable coordinates
(identification.precision), the emitter included when the window estimated
it; a fit that does not determine them well enough is refused (gates.py).
There is no minimum duration of the fit periods: the precision says whether
they carry enough information.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar

from ..tar_learning.clock import LocalClock
from ..tar_learning.identification import q_obs
from . import energy_balance, gates, identification
from .const import (
    RM_EMITTER_CHI2,
    RM_EMITTER_GRID_S,
    RM_EMITTER_REFINE_MAX,
    RM_EMITTER_XATOL,
    RM_MIN_DELTA_C,
    RM_MIN_EVENTS,
    RM_MIN_HEAT_S,
    STRUCT_FULL,
    STRUCT_REDUCED,
)
from .emitter import split_first_moment
from .observer import is_precise
from .periods import fit_periods

# Step of the emitter first moment (log) for the precision of a window that estimated its emitter
_EM_DLOG = 0.02


def _te_pub(snap: dict, tau_e: float, L: float) -> float | None:
    """Refined first-order part of the emitter in force (a delay is only there under a refined value)."""
    if "te_pub" in snap:
        return None if snap["te_pub"] is None else float(snap["te_pub"])
    return tau_e if L > 0.0 else None


def _full_fit(snap: dict, periods: list, first_moment: float, te_pub: float | None, gain: float) -> tuple:
    te, lag = split_first_moment(first_moment, te_pub)
    d = identification.WindowData(snap, periods, te, lag)
    f = identification.fit(identification.Problem(d, True, gain)) if d.segments else None
    return te, lag, d, f


def refine_emitter(snap: dict, periods: list, data: identification.WindowData, tau_e: float,
                   L: float, gain: float = 0.0) -> dict:
    """Emitter chosen by the full fit cost over the fixed grid of first moments.

    Returns {"tau_e", "L", "first_moment", "costs", "significance", "n_eff", "edge", "flat", "source", "data"}:
    data is the window on the chosen emitter; edge is True when the day must be refused.
    """
    out = {"tau_e": tau_e, "L": L, "first_moment": tau_e + L, "costs": None, "significance": None,
           "n_eff": None, "edge": False, "flat": None, "source": "in force", "data": data}
    te_pub = _te_pub(snap, tau_e, L)
    fits = [_full_fit(snap, periods, fm, te_pub, gain) for fm in RM_EMITTER_GRID_S]
    costs = [None if f[3] is None else float(f[3]["cost"]) for f in fits]
    out["costs"] = [None if c is None else round(c, 6) for c in costs]
    ok = [i for i, c in enumerate(costs) if c is not None and math.isfinite(c)]
    if not ok:
        return out
    i = min(ok, key=lambda k: costs[k])
    j_min = costs[i]
    n_eff = float(((fits[i][3].get("check") or {}).get("n_eff")) or 0.0)
    sig = max((costs[k] - j_min) / j_min * n_eff for k in ok) if j_min > 0.0 else 0.0
    marked = sig > RM_EMITTER_CHI2
    edge = i in (0, len(RM_EMITTER_GRID_S) - 1)
    out.update(significance=round(sig, 2), n_eff=round(n_eff, 1), flat=not marked)
    if edge and marked:
        out.update(edge=True, first_moment=RM_EMITTER_GRID_S[i])
        return out
    if not marked and te_pub is not None:
        return out
    fm, (te, lag, d, f) = RM_EMITTER_GRID_S[i], fits[i]
    if not edge and costs[i - 1] is not None and costs[i + 1] is not None:
        # Brent search in log time between the neighbours of the grid minimum
        tried: dict[float, tuple] = {}

        def cost(x: float) -> float:
            tried[x] = _full_fit(snap, periods, math.exp(x), te_pub, gain)
            return math.inf if tried[x][3] is None else float(tried[x][3]["cost"])

        minimize_scalar(cost, bounds=(math.log(RM_EMITTER_GRID_S[i - 1]), math.log(RM_EMITTER_GRID_S[i + 1])),
                        method="bounded", options={"xatol": RM_EMITTER_XATOL, "maxiter": RM_EMITTER_REFINE_MAX})
        best = min(tried.items(), key=lambda kv: math.inf if kv[1][3] is None else kv[1][3]["cost"], default=None)
        if best is not None and best[1][3] is not None and best[1][3]["cost"] < costs[i]:
            fm, (te, lag, d, f) = math.exp(best[0]), best[1]
    out.update(tau_e=te, L=lag, first_moment=fm, data=d, source="grid")
    return out


def balance_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Energy balance of the whole local days before snap["day"]."""
    day = int(snap["day"])
    return {"day": day, "eb": energy_balance.daily(snap, LocalClock(snap.get("tz")), day)}


def _emitter_problems(data: identification.WindowData, em: dict, te_pub: float | None,
                      gain: float, full: bool) -> tuple | None:
    """Problems at the emitter first moment x exp(-+_EM_DLOG) when the window estimated it, else None."""
    if em.get("source") != "grid":
        return None
    fm = float(em["first_moment"])
    lo = data.with_emitter(*split_first_moment(fm * math.exp(-_EM_DLOG), te_pub))
    hi = data.with_emitter(*split_first_moment(fm * math.exp(_EM_DLOG), te_pub))
    return identification.Problem(lo, full, gain), identification.Problem(hi, full, gain), _EM_DLOG


def _fit(data: identification.WindowData, full: bool, gain: float, em: dict, te_pub: float | None,
         warm: dict | None = None) -> dict | None:
    """Fit of one structure with the precision of its identifiable coordinates."""
    pb = identification.Problem(data, full, gain)
    f = identification.fit(pb, warm=warm) if full else identification.fit(pb)
    if f is not None:
        f["info"] = identification.precision(pb, f, _emitter_problems(data, em, te_pub, gain, full))
    return f


def identify_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Fit the window of snap["window"], check it against the earlier window and decide."""
    w = snap["window"]
    t_end = float(w["t_end"])
    rec: dict[str, Any] = {"id": w["id"], "day": LocalClock(snap.get("tz")).day_of(t_end), "t_end": t_end,
                           "nights": list(w["nights"]), "prev_id": w.get("prev")}
    first_t, last_t = snap.get("first_t"), snap.get("last_t")
    t0 = min(float(x[0]) for x in w["intervals"])
    if first_t is None or last_t is None or first_t > t0 or last_t < t_end:
        rec["why"] = gates.WHY_HISTORY
        return _done(rec, snap)
    tau_e, L = snap.get("tau_e"), snap.get("L")
    if tau_e is None or L is None:
        rec["why"] = gates.WHY_EMITTER
        return _done(rec, snap)
    excluded = [str(n) for n in snap.get("excluded_nights") or []]
    rec["excluded_nights"] = [n for n in w["nights"] if n in excluded]
    periods = fit_periods(w, float(first_t), float(last_t), [tuple(g) for g in snap["gaps"]], excluded)
    pub_t = np.asarray(snap["pub_t"], dtype=float)
    j = int(np.searchsorted(pub_t, t_end, side="right"))
    q = q_obs(snap["pub_v"][:j])
    precise = is_precise(q)
    rec["q"] = q
    rec["precise"] = precise
    if not periods:
        rec["why"] = gates.WHY_DISTURBED if rec["excluded_nights"] else gates.WHY_DATA
        return _done(rec, snap)
    data = identification.WindowData(snap, periods, float(tau_e), float(L))
    rec["hours"] = round(data.hours, 2)
    rec["events"] = data.n_events
    rec["fitted"] = [n for n in w["nights"] if any(s["night"] == n for s in data.segments)]
    if data.n_events < RM_MIN_EVENTS:
        rec["why"] = gates.WHY_DISTURBED if rec["excluded_nights"] else gates.WHY_DATA
        return _done(rec, snap)
    rec["heat_s"] = round(data.heat_s(), 1)
    if rec["heat_s"] < RM_MIN_HEAT_S:
        rec["why"] = gates.WHY_HEAT
        return _done(rec, snap)
    delta = data.mean_delta()
    rec["delta_c"] = None if delta is None else round(delta, 2)
    if delta is None or delta < RM_MIN_DELTA_C:
        rec["why"] = gates.WHY_DELTA
        return _done(rec, snap)
    gain = eb_gain_in_force(snap)
    rec["gain"] = gain
    em = refine_emitter(snap, periods, data, float(tau_e), float(L), gain)
    rec["emitter"] = {"tau_e": round(em["tau_e"], 1), "L": round(em["L"], 1),
                      "first_moment": round(em["first_moment"], 1), "costs": em["costs"],
                      "significance": em["significance"], "n_eff": em["n_eff"], "flat": em["flat"],
                      "source": em["source"]}
    if em["edge"]:
        rec["why"] = gates.WHY_EMITTER_EDGE
        return _done(rec, snap)
    te_pub = _te_pub(snap, float(tau_e), float(L))
    tau_e, L, data = em["tau_e"], em["L"], em["data"]
    sensor = "precise" if precise else "coarse"
    red = _fit(data, False, gain, em, te_pub)
    rec["reduced"] = gates.fit_record(red, STRUCT_REDUCED, sensor, float(tau_e), float(L), q)
    prev = snap.get("prev")
    if precise:
        warm = ((prev or {}).get("full") or {}).get("params")
        full = _fit(data, True, gain, em, te_pub, warm=warm)
        rec["full"] = gates.fit_record(full, STRUCT_FULL, sensor, float(tau_e), float(L), q)
    held: dict[str, Any] = {}
    for struct in ("full", "reduced"):
        p = ((prev or {}).get(struct) or {})
        if p.get("ok"):
            held[struct] = gates.slim_check(identification.held_out(data, p["params"], gain))
    rec["held"] = held
    return _done(rec, snap)


def eb_in_force(snap: dict) -> float | None:
    """Energy-balance slope in force for the decision (None before a first qualified value)."""
    eb = snap.get("eb")
    return None if eb is None else float(eb)


def eb_gain_in_force(snap: dict) -> float:
    """Constant free gain of the static model in force for the fits, 0 without one."""
    g = snap.get("eb_gain") if snap.get("eb") is not None else None
    try:
        g = 0.0 if g is None else float(g)
    except (TypeError, ValueError):
        return 0.0
    return g if math.isfinite(g) else 0.0


def _done(rec: dict, snap: dict) -> dict:
    a_per_min = snap.get("a_per_min")
    a_online = float(a_per_min) / 60.0 if a_per_min else None
    rec["eb"] = eb_in_force(snap)
    rec["decision"] = gates.decide(rec, snap.get("prev"), rec.get("held") or {}, bool(rec.get("precise")),
                                   a_online=a_online, eb=rec["eb"])
    return rec
