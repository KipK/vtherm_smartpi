"""Daily identification of the room model, run in an executor (numpy, scipy).

The job takes a snapshot of plain Python data and returns plain Python data,
so it never touches the event loop state.

Energy balance: the job first computes the energy-balance ratio of the whole
days before the day (energy_balance.py), whatever the outcome of the fits. The
ratio in force for the decision is that ratio when it qualifies, else the one
in force before the job (snap["eb"], None before a first qualified ratio); a
candidate must agree with it (gates.py).

Emitter refinement: the full model is fitted with the emitter first moment in
force scaled by each multiple of
RM_EMITTER_GRID, split into tau_e and L like the manager does; the lowest cost
sets the emitter of the day's fits. An optimum at either end of the grid means
the true emitter lies outside it, and the day is refused.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..tar_learning.clock import LocalClock
from ..tar_learning.identification import q_obs
from . import energy_balance, gates, identification
from .const import (
    RM_EMITTER_GRID,
    RM_MIN_DELTA_C,
    RM_MIN_EVENTS,
    RM_MIN_FIT_H,
    RM_MIN_HEAT_S,
    STRUCT_FULL,
    STRUCT_REDUCED,
)
from .emitter import split_first_moment
from .observer import is_precise
from .periods import fit_periods, window_bounds


# Below this first moment (s) every multiple gives no emitter lag: nothing to refine.
_EMITTER_MIN_S = 2.0


def _te_pub(snap: dict, tau_e: float, L: float) -> float | None:
    """Refined first-order part of the emitter in force (a delay is only there under a refined value)."""
    if "te_pub" in snap:
        return None if snap["te_pub"] is None else float(snap["te_pub"])
    return tau_e if L > 0.0 else None


def refine_emitter(snap: dict, periods: list, data: identification.WindowData, tau_e: float,
                   L: float) -> dict:
    """Emitter chosen by the full fit cost over multiples of the first moment in force.

    Returns {"tau_e", "L", "mult", "costs", "edge", "data"}: data is the window on the chosen emitter.
    """
    fm = tau_e + L
    out = {"tau_e": tau_e, "L": L, "mult": 1.0, "costs": None, "edge": False, "data": data}
    if fm < _EMITTER_MIN_S:
        return out
    te_pub = _te_pub(snap, tau_e, L)
    best = None
    costs = []
    for m in RM_EMITTER_GRID:
        te, lag = split_first_moment(fm * m, te_pub)
        same = abs(te - tau_e) < 1e-6 and abs(lag - L) < 1e-6
        d = data if same else identification.WindowData(snap, periods, te, lag)
        f = identification.fit(identification.Problem(d, True)) if d.segments else None
        cost = None if f is None else f["cost"]
        costs.append(None if cost is None else round(cost, 6))
        if cost is not None and (best is None or cost < best[0]):
            best = (cost, m, te, lag, d)
    out["costs"] = costs
    if best is None:
        return out
    _, m, te, lag, d = best
    out.update(tau_e=te, L=lag, mult=m, data=d, edge=m in (RM_EMITTER_GRID[0], RM_EMITTER_GRID[-1]))
    return out


def identify_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Fit the window of snap["day"], check it against the earlier window and decide."""
    clock = LocalClock(snap.get("tz"))
    day = int(snap["day"])
    t0, t1 = window_bounds(clock, day)
    rec: dict[str, Any] = {"day": day, "eb": energy_balance.daily(snap, clock, day)}
    first_t, last_t = snap.get("first_t"), snap.get("last_t")
    if first_t is None or last_t is None or first_t > t0 + 0.5 * (t1 - t0) or last_t < t1:
        rec["why"] = gates.WHY_HISTORY
        return _done(rec, snap)
    tau_e, L = snap.get("tau_e"), snap.get("L")
    if tau_e is None or L is None:
        rec["why"] = gates.WHY_EMITTER
        return _done(rec, snap)
    excluded = [int(n) for n in snap.get("excluded_nights") or []]
    rec["excluded_nights"] = [n for n in excluded if day - 3 <= n < day]
    periods = fit_periods(clock, day, float(first_t), float(last_t), [tuple(g) for g in snap["gaps"]], excluded)
    pub_t = np.asarray(snap["pub_t"], dtype=float)
    j = int(np.searchsorted(pub_t, t1, side="right"))
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
    rec["nights"] = sorted({s["night"] for s in data.segments})
    if data.hours < RM_MIN_FIT_H or data.n_events < RM_MIN_EVENTS:
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
    em = refine_emitter(snap, periods, data, float(tau_e), float(L))
    rec["emitter"] = {"tau_e": round(em["tau_e"], 1), "L": round(em["L"], 1), "mult": em["mult"],
                      "costs": em["costs"]}
    if em["edge"]:
        rec["why"] = gates.WHY_EMITTER_EDGE
        return _done(rec, snap)
    tau_e, L, data = em["tau_e"], em["L"], em["data"]
    sensor = "precise" if precise else "coarse"
    red = identification.fit(identification.Problem(data, False))
    rec["reduced"] = gates.fit_record(red, STRUCT_REDUCED, sensor, float(tau_e), float(L), q)
    prev = snap.get("prev")
    if precise:
        warm = ((prev or {}).get("full") or {}).get("params")
        full = identification.fit(identification.Problem(data, True), warm=warm)
        rec["full"] = gates.fit_record(full, STRUCT_FULL, sensor, float(tau_e), float(L), q)
    held: dict[str, Any] = {}
    for struct in ("full", "reduced"):
        p = ((prev or {}).get(struct) or {})
        if p.get("ok"):
            held[struct] = gates.slim_check(identification.held_out(data, p["params"]))
    rec["held"] = held
    return _done(rec, snap)


def eb_in_force(rec: dict, snap: dict) -> float | None:
    """Energy-balance ratio in force for the decision: the job's own when it qualifies, else the earlier one."""
    fresh = (rec.get("eb") or {}).get("value")
    if fresh is not None:
        return float(fresh)
    prev = snap.get("eb")
    return None if prev is None else float(prev)


def _done(rec: dict, snap: dict) -> dict:
    a_per_min = snap.get("a_per_min")
    a_online = float(a_per_min) / 60.0 if a_per_min else None
    rec["decision"] = gates.decide(rec, snap.get("prev"), rec.get("held") or {}, bool(rec.get("precise")),
                                   a_online=a_online, eb=eb_in_force(rec, snap))
    return rec
