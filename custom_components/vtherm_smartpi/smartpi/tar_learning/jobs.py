"""Computations of the T_ar learning run in an executor (numpy, scipy).

Each job takes a snapshot of plain Python data and returns plain Python data,
so it never touches the event loop state.
"""
from __future__ import annotations

import bisect
from typing import Any

import numpy as np

from . import gates, identification, morning, room_mass
from .clock import LocalClock
from .const import ID_DT, ID_WIN_DAYS
from .signals import Anchors, ExtTL


def _clock(snap: dict):
    return LocalClock(snap.get("tz"))


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


def morning_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one morning comfort step."""
    clock = _clock(snap)
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
    inp = morning.MorningInputs(
        anch=anch, ext=ExtTL(ext_t, ext_v), ext_t=ext_t, ext_v=ext_v, on=on, off=off,
        spans=room_mass.passive_spans(on, off, snap["t_last"]), params_at=params_at, phase_at=phase_at,
        clock=clock, cycle_s=float(snap["cycle_s"]), b_pub=float(snap["b_pub"]),
        valid_until=_valid_until(snap["gaps"]), power=_power(snap),
    )
    nights = list(snap["nights"])
    day = clock.day_of(snap["t_sp"])
    evals = snap.get("evals") or []
    cls = identification.class_in_force([(e["day"], e["cls"]) for e in evals], day)
    wall = identification.params_in_force(evals, day)
    out = morning.evaluate_morning(inp, snap["t_sp"], snap["eco"], snap["sp_to"], nights, cls, wall)
    night = nights[-1]
    out["day"] = day
    out["night_summary"] = {k: night.get(k) for k in ("cls", "len_h", "tau1_h", "tau2_h", "dbic")}
    out["gate_obs"] = gates.gate_observables(clock, on, snap["t_sp"], out.get("t_s"))
    out["refined_cls"] = cls
    return out


def window_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Fit the identification windows of the requested days and gate the requested nights."""
    clock = _clock(snap)
    gaps = snap["gaps"]
    first_t, last_t = float(snap["first_t"]), float(snap["last_t"])
    out: dict[str, Any] = {"windows": {}, "nights": {}}
    power = _power(snap)
    if snap.get("window_days"):
        t_lo = min(identification.window_bounds(clock, d)[0] for d in snap["window_days"]) - 86400.0
        t_hi = max(identification.window_bounds(clock, d)[1] for d in snap["window_days"])
        g0 = max(first_t, t_lo)
        g0 = ID_DT * np.floor(g0 / ID_DT)
        d = identification.grid_data(snap["pub_t"], snap["pub_v"], snap["on"], snap["off"], snap["ext_t"],
                                     snap["ext_v"], float(g0), t_hi + 2 * ID_DT, clock,
                                     _valid_mask(gaps, first_t), power=power)
        pub_t = np.asarray(snap["pub_t"], dtype=float)
        for day in snap["window_days"]:
            t0, t1 = identification.window_bounds(clock, day)
            j = int(np.searchsorted(pub_t, t1, side="right"))
            q = identification.q_obs(snap["pub_v"][:j])
            out["windows"][str(day)] = identification.window_fit(d, t0, t1, q)
    for n in snap.get("night_days") or []:
        out["nights"][str(n)] = gates.regulated_night(
            clock, n, snap["on"], snap["off"], np.asarray(snap["pub_t"], dtype=float),
            np.asarray(snap["pub_v"], dtype=float), snap["sp0"], [tuple(x) for x in snap["sp_events"]],
            float(snap["cycle_s"]), init_on=False, valid_fn=_valid_fn(gaps, first_t, last_t), power=power)
    return out


def evaluate_job(snap: dict[str, Any]) -> dict[str, Any]:
    """Evaluation of one day from its two windows, under the regulated-night policy."""
    day = snap["day"]
    w1, w2 = snap["w1"], snap["w2"]
    e = identification.evaluate_day(w1, w2, snap["b_pub"] / 60.0)
    e["day"] = day
    nights = {int(k): v for k, v in snap["nights"].items()}
    qualified = (gates.window_regulated(nights, day - ID_WIN_DAYS, ID_WIN_DAYS)
                 and gates.window_regulated(nights, day, ID_WIN_DAYS))
    e["regulated"] = bool(qualified)
    if not qualified:
        e = dict(identification.silence(e, "not regulated"), regulated=False, day=day)
    evals = list(snap["evals"]) + [e]
    te = identification.te_in_force(evals, day + 1)
    wall = identification.params_in_force(evals, day + 1)
    cls = identification.class_in_force([(x["day"], x["cls"]) for x in evals], day + 1)
    return {"eval": e, "te_pub": te, "wall": wall, "cls": cls}

