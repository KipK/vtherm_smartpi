"""Computations of the T_ar learning run in an executor (numpy, scipy).

Each job takes a snapshot of plain Python data and returns plain Python data,
so it never touches the event loop state. The quiet periods are chosen by the
manager (quiet_periods) and passed in the snapshot as intervals.
"""
from __future__ import annotations

import bisect
from typing import Any

import numpy as np

from . import gates, identification, morning, room_mass
from .const import ID_DT, ID_WALL_WARMUP_S, TAR_GAIN_DEADTIME_FACTOR

# First moment of a first-order emitter / its apparent dead time (inverse of the gain rule factor)
TAR_BOOTSTRAP_MOMENT_FACTOR = 1.0 / TAR_GAIN_DEADTIME_FACTOR
from .signals import Anchors, ExtTL


def _latest(series: list, t: float, default=None):
    """Latest item (time first) at or before t."""
    times = [x[0] for x in series]
    i = bisect.bisect_right(times, t) - 1
    return series[i] if i >= 0 else default


def _valid_until(gaps):
    def cut(a: float, b: float) -> float:
        for g0, g1 in gaps:
            if g0 < b and g1 > a:
                b = min(b, max(a, g0))
        return b

    return cut


def _valid_mask(gaps, first_t):
    def mask(grid, step):
        ok = grid >= first_t
        for g0, g1 in gaps:
            ok &= ~((grid < g1) & (grid + step > g0))
        return ok

    return mask


def _valid_fn(gaps, first_t, last_t):
    def ok(a: float, b: float) -> bool:
        if a < first_t or b > last_t:
            return False
        return not any(g0 < b and g1 > a for g0, g1 in gaps)

    return ok


def _power(snap: dict):
    """Valve power level timeline of the snapshot (arrays), None for a PWM heater."""
    pw = snap.get("pw")
    if pw is None:
        return None
    return np.asarray(pw[0], dtype=float), np.asarray(pw[1], dtype=float)


def delay_floor(te_pub: dict | None, fast_s: float | None, bootstrap_s: float | None) -> float | None:
    """Shortest first moment of the emitter known before a step: the largest of the refined value in force,
    the published fast value and twice the bootstrap dead time. The bootstrap measures an apparent dead
    time; the first moment of a first-order emitter is about twice that (the factor the gain rule undoes,
    TAR_GAIN_DEADTIME_FACTOR). None without any of them."""
    vals = [te_pub["value"] if te_pub else None, fast_s,
            None if bootstrap_s is None else TAR_BOOTSTRAP_MOMENT_FACTOR * bootstrap_s]
    vals = [float(v) for v in vals if v is not None and v > 0.0]
    return max(vals) if vals else None


def morning_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one comfort step: the rise that ends the quiet period snap['night_id']."""
    params = snap["params"]
    phases = snap["phases"]

    def params_at(t):
        p = _latest(params, t)
        return (None, None) if p is None else (p[2], p[3])

    def phase_at(t):
        p = _latest(phases, t)
        return None if p is None else p[1]

    on = np.asarray(snap["on"], dtype=float)
    off = np.asarray(snap["off"], dtype=float)
    anch = Anchors(snap["pub_t"], snap["pub_v"])
    ext_t = np.asarray(snap["ext_t"], dtype=float)
    ext_v = np.asarray(snap["ext_v"], dtype=float)
    t_sp = float(snap["t_sp"])
    evals = snap.get("evals") or []
    inp = morning.MorningInputs(
        anch=anch, ext=ExtTL(ext_t, ext_v), ext_t=ext_t, ext_v=ext_v, on=on, off=off,
        spans=room_mass.passive_spans(on, off, snap["t_last"]), params_at=params_at, phase_at=phase_at,
        quiet=[tuple(x) for x in snap["quiet"]], cycle_s=float(snap["cycle_s"]), b_pub=float(snap["b_pub"]),
        valid_until=_valid_until(snap["gaps"]), power=_power(snap), fit_end=snap.get("fit_end"),
        t_floor=delay_floor(identification.te_in_force(evals, t_sp), snap.get("fast_s"), snap.get("bootstrap_s")),
    )
    nid = snap["night_id"]
    # One entry per night: a night evaluated again replaces its entry before the room class is computed
    nights = [n for n in snap["nights"] if n.get("night_id") != nid]
    cls = identification.class_in_force([(e["t"], e["cls"]) for e in evals], t_sp)
    wall = identification.params_in_force(evals, t_sp)
    out = morning.evaluate_morning(inp, t_sp, snap["eco"], snap["sp_to"], nights, cls, wall)
    night = nights[-1]
    out["night_id"] = nid
    out["night_summary"] = dict({k: night.get(k) for k in ("cls", "len_h", "tau1_h", "tau2_h", "dbic")},
                                night_id=nid)
    out["gate_obs"] = gates.gate_observables(on, t_sp, out.get("t_s"), snap.get("regulated_start"))
    out["refined_cls"] = cls
    return out


def window_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Regulated-night gate of one quiet period and the identification window that it ends."""
    gaps = snap["gaps"]
    first_t, last_t = float(snap["first_t"]), float(snap["last_t"])
    out: dict[str, Any] = {"window": None, "night": None}
    power = _power(snap)
    g = snap.get("gate")
    if g is not None:
        if g.get("why") is not None:
            out["night"] = {"ok": False, "why": g["why"], "t_start": g.get("a"), "t_end": g.get("b")}
        else:
            out["night"] = gates.regulated_span(
                float(g["a"]), float(g["b"]), float(g["sp"]), snap["on"], snap["off"],
                np.asarray(snap["pub_t"], dtype=float), np.asarray(snap["pub_v"], dtype=float),
                float(snap["cycle_s"]), init_on=False, valid_fn=_valid_fn(gaps, first_t, last_t), power=power)
    w = snap.get("window")
    if w is not None:
        ivs = [tuple(x) for x in w["intervals"]]
        if ivs[0][0] < first_t:
            out["window"] = {"ok": False, "why": "window outside history"}
            return out
        t0 = max(first_t, ivs[0][0] - ID_WALL_WARMUP_S)
        t1 = ivs[-1][1]
        g0 = ID_DT * np.floor(t0 / ID_DT)
        d = identification.grid_data(snap["pub_t"], snap["pub_v"], snap["on"], snap["off"], snap["ext_t"],
                                     snap["ext_v"], float(g0), t1 + 2 * ID_DT, _valid_mask(gaps, first_t),
                                     power=power)
        pub_t = np.asarray(snap["pub_t"], dtype=float)
        j = int(np.searchsorted(pub_t, t1, side="right"))
        q = identification.q_obs(snap["pub_v"][:j])
        out["window"] = identification.window_fit(d, float(g0), t1, ivs, q)
    return out


def evaluate_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Evaluation of one window against the window of the three periods before it."""
    w1, w2 = snap["w1"], snap["w2"]
    e = identification.evaluate_day(w1, w2, snap["b_pub"] / 60.0)
    e["day"], e["t"], e["window"] = snap["day"], snap["t"], snap["window_id"]
    qualified = gates.window_regulated(snap["nights"], list(w1["nights"]) + list(w2["nights"]))
    e["regulated"] = bool(qualified)
    if not qualified:
        e = dict(identification.silence(e, "not regulated"), regulated=False, window=snap["window_id"])
    evals = list(snap["evals"]) + [e]
    after = float(snap["t"]) + 1.0
    te = identification.te_in_force(evals, after)
    wall = identification.params_in_force(evals, after)
    cls = identification.class_in_force([(x["t"], x["cls"]) for x in evals], after)
    return {"eval": e, "te_pub": te, "wall": wall, "cls": cls}
