"""Publication rules of the room model identification (pure Python).

A window fit passes its checks when its values are plausible and its
residuals (on the fitted periods) show no bias and no structure. A day
publishes a candidate only when the window of the day and the window ending
ID_WIN_DAYS days earlier (disjoint) both pass, agree, and the earlier
parameters predict the later window (held-out residual checks, parameters
frozen, initial states re-estimated).

The full model (air and walls) is chosen when the sensor is precise, the wall
carries a real share of the air loss in both windows and the full model beats
the reduced one on the held-out window; otherwise the reduced model is used if
it passes. A structure whose heating response a lies beyond a factor
RM_A_ONLINE_RATIO_MAX of the online estimate is refused: it lumps the walls
into the air.

Candidates are combined in the identifiable coordinates (a, b_eq / a, c /
alpha, beta): the candidate is the geometric mean of the two windows in those
coordinates, and the published value is their median over the last
RM_PUB_LAST candidates of the same structure.
"""
from __future__ import annotations

import math
import statistics

from .const import (
    RM_A_ONLINE_RATIO_MAX,
    RM_AGREE_TOL,
    RM_FULL_HELDOUT_GAIN,
    RM_PUB_LAST,
    RM_RES_AC_1H_MAX,
    RM_RES_CORR_MAX,
    RM_RES_HELDOUT_MEAN_MAX,
    RM_RES_HELDOUT_RMS_MAX,
    RM_RES_MEAN_MAX,
    RM_RES_RMS_MAX,
    RM_RES_RUNS_Z_MIN,
    RM_RES_STRUCT_FLOOR_C,
    RM_RES_STRUCT_FLOOR_Q,
    RM_WALL_SHARE_MIN,
    STRUCT_FULL,
    STRUCT_REDUCED,
)
from .model import RoomParams

# Refusal and selection codes (translated in public.py)
WHY_HISTORY = "history"
WHY_EMITTER = "emitter"
WHY_DATA = "data"
WHY_DISTURBED = "disturbed"
WHY_HEAT = "heat"
WHY_DELTA = "delta"
WHY_FIT = "fit"
WHY_RANGE = "range"
WHY_RESIDUALS = "residuals"
WHY_NO_PREVIOUS = "no previous"
WHY_DISAGREE = "disagree"
WHY_HELDOUT = "heldout"
WHY_FAILED = "failed"
WHY_EMITTER_EDGE = "emitter edge"
WHY_A_ONLINE = "a online"

WALL_COARSE = "coarse sensor"
WALL_SMALL = "no wall share"
WALL_NOT_CONFIRMED = "not confirmed"
WALL_NOT_BETTER = "not better"


def residuals_ok(check: dict | None, sensor: str, held_out: bool = False, q: float | None = None) -> bool:
    """Residual checks: no bias, small spread, no runs or autocorrelation, no link with the regressors.

    The structure tests apply only when the residuals exceed the sensor resolution.
    """
    if not check or check.get("n", 0) < 10:
        return False
    mean_max = (RM_RES_HELDOUT_MEAN_MAX if held_out else RM_RES_MEAN_MAX)[sensor]
    rms_max = (RM_RES_HELDOUT_RMS_MAX if held_out else RM_RES_RMS_MAX)[sensor]
    if abs(check["mean"]) > mean_max or check["rms"] > rms_max:
        return False
    if check["rms"] <= max(RM_RES_STRUCT_FLOOR_C, RM_RES_STRUCT_FLOOR_Q * (q or 0.0)):
        return True
    if check.get("runs_z") is not None and check["runs_z"] < RM_RES_RUNS_Z_MIN:
        return False
    if check.get("ac1h") is not None and check["ac1h"] > RM_RES_AC_1H_MAX:
        return False
    for k in ("corr_u", "corr_sp", "corr_te"):
        if check.get(k) is not None and abs(check[k]) > RM_RES_CORR_MAX:
            return False
    return True


def fit_record(fit: dict | None, struct: str, sensor: str, tau_e: float, L: float, q: float | None) -> dict:
    """Slim record of one window fit: parameters, residual summary and verdict."""
    if fit is None:
        return {"ok": False, "why": WHY_FIT}
    params = RoomParams(struct, fit["a"], fit["alpha"], fit["beta"] if struct == STRUCT_FULL else 1.0 / 3600.0,
                        fit["c"], tau_e, L)
    rec = {"params": params.to_dict(), "b_eq": params.b_eq, "check": slim_check(fit.get("check"))}
    if not params.plausible() or fit.get("beta_at_bound"):
        rec.update(ok=False, why=WHY_RANGE)
    elif not residuals_ok(fit.get("check"), sensor, q=q):
        rec.update(ok=False, why=WHY_RESIDUALS)
    else:
        rec.update(ok=True, why=None)
    return rec


def slim_check(check: dict | None) -> dict | None:
    if not check:
        return None
    return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in check.items()}


def agree(p: dict, q: dict, keys: tuple[str, ...]) -> bool:
    for k in keys:
        x, y = _value(p, k), _value(q, k)
        if x is None or y is None or x <= 0.0 or y <= 0.0 or abs(math.log(x / y)) > RM_AGREE_TOL[k]:
            return False
    return True


def _value(p: dict, k: str) -> float | None:
    if k == "b_eq":
        return p["alpha"] - p["c"] if p["struct"] == STRUCT_FULL else p["alpha"]
    return p.get(k)


def a_consistent(a: float | None, a_online: float | None) -> bool:
    """The heating response lies within RM_A_ONLINE_RATIO_MAX of the online estimate (no estimate: no check)."""
    if a_online is None or a_online <= 0.0:
        return True
    if a is None or a <= 0.0:
        return False
    return abs(math.log(a / a_online)) <= math.log(RM_A_ONLINE_RATIO_MAX)


def _coords(p: dict) -> tuple[float, float, float, float] | None:
    """Identifiable coordinates of a parameter set: (a, b_eq / a, c / alpha, beta)."""
    full = p.get("struct") == STRUCT_FULL
    b_eq = _value(p, "b_eq")
    if p["a"] <= 0.0 or p["alpha"] <= 0.0 or b_eq <= 0.0 or p["beta"] <= 0.0 or (full and p["c"] <= 0.0):
        return None
    return p["a"], b_eq / p["a"], (p["c"] / p["alpha"] if full else 0.0), p["beta"]


def _from_coords(base: dict, a: float, r: float, g: float, beta: float) -> dict:
    out = dict(base, a=a, beta=beta)
    b_eq = r * a
    if base.get("struct") == STRUCT_FULL:
        out["alpha"] = b_eq / (1.0 - g)
        out["c"] = g * out["alpha"]
    else:
        out["alpha"] = b_eq
    return out


def _gm(values: list[float]) -> float:
    return math.exp(sum(math.log(v) for v in values) / len(values))


def combine(p: dict, q: dict) -> dict:
    """Geometric mean of two parameter sets of the same structure, in the identifiable coordinates."""
    cp, cq = _coords(p), _coords(q)
    if cp is None or cq is None:
        return dict(q)
    full = q.get("struct") == STRUCT_FULL
    m = [(_gm([x, y]) if (i != 2 or full) else 0.0) for i, (x, y) in enumerate(zip(cp, cq))]
    if m[2] >= 1.0:
        return dict(q)
    return _from_coords(q, *m)


def decide(cur: dict, prev: dict | None, held: dict, precise: bool, a_online: float | None = None) -> dict:
    """Decision of a day: {"struct", "candidate", "wall"} or {"why", "wall"}.

    a_online: heating response of the online estimate (per second), None when unknown.
    """
    if cur.get("why"):
        return {"why": cur["why"], "wall": None}
    sensor = "precise" if precise else "coarse"
    q = cur.get("q")
    red, full = cur.get("reduced") or {}, cur.get("full") or {}
    p_red = (prev or {}).get("reduced") or {}
    p_full = (prev or {}).get("full") or {}
    # Why the full model is not publishable: (wall code, refusal code when it is the better model)
    if not precise:
        wall, full_why = WALL_COARSE, None
    elif not full.get("ok"):
        wall = WALL_NOT_CONFIRMED if full.get("why") == WHY_RESIDUALS else WALL_SMALL
        full_why = None
    elif min(r["params"]["c"] / r["params"]["alpha"] for r in ([full, p_full] if p_full.get("ok") else [full])) \
            < RM_WALL_SHARE_MIN:
        wall, full_why = WALL_SMALL, None
    elif not a_consistent(full["params"]["a"], a_online):
        wall, full_why = WALL_NOT_CONFIRMED, WHY_A_ONLINE
    elif not p_full.get("ok"):
        wall, full_why = WALL_NOT_CONFIRMED, WHY_NO_PREVIOUS
    elif not agree(full["params"], p_full["params"], ("a", "b_eq", "alpha", "c")):
        wall, full_why = WALL_NOT_CONFIRMED, WHY_DISAGREE
    elif not residuals_ok(held.get("full"), sensor, held_out=True, q=q):
        wall, full_why = WALL_NOT_CONFIRMED, WHY_HELDOUT
    elif (held.get("reduced") and held["reduced"].get("rms") is not None
          and held["full"]["rms"] > RM_FULL_HELDOUT_GAIN * held["reduced"]["rms"]):
        wall, full_why = WALL_NOT_BETTER, None
    elif not a_consistent((cand := combine(p_full["params"], full["params"]))["a"], a_online):
        wall, full_why = WALL_NOT_CONFIRMED, WHY_A_ONLINE
    else:
        return {"struct": STRUCT_FULL, "candidate": cand, "wall": None}
    if not red.get("ok"):
        why = full_why or red.get("why") or WHY_FIT
    elif not a_consistent(red["params"]["a"], a_online):
        why = WHY_A_ONLINE
    elif prev is None or prev.get("why") or not p_red.get("ok"):
        why = WHY_NO_PREVIOUS
    elif not agree(red["params"], p_red["params"], ("a", "b_eq")):
        why = WHY_DISAGREE
    elif not residuals_ok(held.get("reduced"), sensor, held_out=True, q=q):
        why = WHY_HELDOUT
    elif not a_consistent((cand := combine(p_red["params"], red["params"]))["a"], a_online):
        why = WHY_A_ONLINE
    else:
        return {"struct": STRUCT_REDUCED, "candidate": cand, "wall": wall}
    return {"why": why, "wall": wall}


def published(candidates: list[dict]) -> dict | None:
    """Median of the last candidates of the latest structure in the identifiable coordinates (a change of
    structure starts over)."""
    if not candidates:
        return None
    struct = candidates[-1]["struct"]
    same = []
    for c in reversed(candidates):
        if c["struct"] != struct:
            break
        same.append(c)
    last = same[:RM_PUB_LAST]
    coords = [_coords(c) for c in last]
    if any(c is None for c in coords):
        return dict(last[0])
    med = [statistics.median(c[i] for c in coords) for i in range(4)]
    if med[2] >= 1.0:
        return dict(last[0])
    return _from_coords(last[0], *med)
