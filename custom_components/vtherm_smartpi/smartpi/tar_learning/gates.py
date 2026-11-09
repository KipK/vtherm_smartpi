"""Gates of the T_ar learning.

Both gates read the regulated part of a quiet period (quiet_periods): it
starts at the regulated start, the first heater start with the room at its low
setpoint, and ends at the setpoint rise that ends the period, or at the end of
the darkness for a period without a rise.

Regulated night (hand-over of the refined value). The period is regulated iff
its regulated part lasts at least 4 h and, over it:
  (a) the setpoint is constant;
  (b) every 30 min quarter (from the regulated start; a last piece shorter than
      15 min is merged into the previous quarter) holds a heater start;
  (c) the mean published temperature of every quarter is within 0.3 C of the
      setpoint;
  (d) the per-cycle mean heater input has |slope| <= 0.05 /h and a residual
      std around its linear trend <= 0.10;
  (e) the data of the span hold no gap.
An unmeasured night gain (occupants) is nearly collinear with the loss b in a
free fall, while during regulation it shows as a duty change: the refined value
may replace the fast value only when the six periods of its two windows are
regulated.

Confirmation of the fast value. A sleeper leaving during the fit window bends
the rise and shortens the apparent delay; a period that stays without heating
while the room should regulate at its low setpoint is the observable sign of
that risk. Without a regulated start, or with a regulated part shorter than
4 h, nothing can be observed: the morning is unconfirmed (an unknown is never
taken as a favourable zero).
  G1  : at least 1 h without heater start in [regulated start, t_ref), or the
        regulated part unknown or too short -> unconfirmed;
  GT4 : G1 and (T < 900 s, or the fitted exponential baseline b below 0.6 x the
        published b, or a heater start in the 2 h before t_ref) -> unconfirmed.
t_ref is the step (first heater start after the setpoint rise), else the rise.
"""
from __future__ import annotations

from ..quiet_periods.const import REGULATED_MIN_S
from .const import (
    GATE_IDLE_MIN_H,
    GT4_BFIT_REL_MIN,
    GT4_T_MIN_S,
    H,
    RN_ECO_TOL_C,
    RN_QUARTER_MERGE_S,
    RN_QUARTER_S,
    RN_SP_EPS,
    RN_U_RESID_MAX,
    RN_U_SLOPE_MAX_H,
    TAR_GATE_G1,
    TAR_GATE_GT4,
)


# ---------------------------------------------------------------------------
# Setpoint timeline
# ---------------------------------------------------------------------------
def sp_at(sp0: float, ev: list[tuple[float, float]], t: float) -> float:
    v = sp0
    for te, s in ev:
        if te <= t:
            v = s
        else:
            break
    return v


def sp_changes_in(sp0, ev, a: float, b: float) -> list[tuple[float, float, float]]:
    """Setpoint changes (t, old, new) inside (a, b)."""
    out, cur = [], sp_at(sp0, ev, a)
    for te, s in ev:
        if a < te < b and abs(s - cur) > RN_SP_EPS:
            out.append((te, cur, s))
            cur = s
    return out


# ---------------------------------------------------------------------------
# Regulated night
# ---------------------------------------------------------------------------
def zoh_mean(pub_t, pub_v, a: float, b: float, step: float = 60.0) -> float | None:
    import numpy as np  # pylint: disable=import-outside-toplevel

    ts = np.arange(a + 0.5 * step, b, step)
    j = np.searchsorted(pub_t, ts, side="right") - 1
    if not len(ts) or j[0] < 0:
        return None
    return float(np.mean(np.asarray(pub_v)[j]))


def quarters(a: float, b: float) -> list[tuple[float, float]]:
    q, t = [], a
    while t < b - 1e-6:
        q.append((t, min(t + RN_QUARTER_S, b)))
        t += RN_QUARTER_S
    if len(q) >= 2 and q[-1][1] - q[-1][0] < RN_QUARTER_MERGE_S:
        last = q.pop()
        q[-1] = (q[-1][0], last[1])
    return q


def regulated_span(a: float, b: float, sp: float, on, off, pub_t, pub_v, cycle_s: float, init_on: bool = False,
                   valid_fn=None, power=None, sp_timeline: tuple | None = None) -> dict:
    """Regulated-night gate of the regulated part [a, b) at the setpoint sp.

    power: valve power level timeline (times, levels), used for the holding power instead of the edges.
    sp_timeline: optional (sp0, events) setpoint timeline checked for a change inside the span.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    from .signals import duty_bins, level_bins  # pylint: disable=import-outside-toplevel

    out = {"t_start": a, "t_end": b, "sp": sp, "why": None}
    if b - a < REGULATED_MIN_S:
        out["why"] = "regulated part too short"
        return dict(out, ok=False)
    if valid_fn is not None and not valid_fn(a, b):
        out["why"] = "data gap"
        return dict(out, ok=False)
    if sp_timeline is not None and sp_changes_in(sp_timeline[0], sp_timeline[1], a, b):
        out["why"] = "setpoint change in the regulated part"
        return dict(out, ok=False)
    on = np.asarray(on, dtype=float)
    qs = quarters(a, b)
    starts = [int(np.sum((on >= x) & (on < y))) for x, y in qs]
    out["min_starts"] = min(starts) if starts else 0
    devs = []
    for x, y in qs:
        m = zoh_mean(pub_t, pub_v, x, y)
        devs.append(None if m is None else m - sp)
    out["max_dev"] = None if any(v is None for v in devs) else float(max(abs(v) for v in devs))
    n = int(round((b - a) / cycle_s))
    bins_start = np.asarray([b - (j + 1) * cycle_s for j in range(n)][::-1])
    if n < 3:
        um = None
    elif power is not None:
        um = level_bins(power[0], power[1], bins_start, cycle_s)
    else:
        um = duty_bins(on, np.asarray(off, dtype=float), bins_start, cycle_s, init_on=init_on)
    if um is not None:
        tc = (bins_start + 0.5 * cycle_s - b) / H
        s_u, c_u = np.polyfit(tc, um, 1)
        out["u_slope_h"] = float(s_u)
        out["u_resid_std"] = float(np.std(um - (s_u * tc + c_u)))
    if out["min_starts"] < 1:
        out["why"] = "a quarter without heater start"
    elif out["max_dev"] is None or out["max_dev"] > RN_ECO_TOL_C:
        out["why"] = "temperature away from the setpoint"
    elif um is None:
        out["why"] = "span too short"
    elif abs(out["u_slope_h"]) > RN_U_SLOPE_MAX_H or out["u_resid_std"] > RN_U_RESID_MAX:
        out["why"] = "holding power not stationary"
    out["ok"] = out["why"] is None
    return out


def window_regulated(nights: dict, ids) -> bool:
    """Every period of a window (its keys ids) is regulated."""
    ids = list(ids)
    return bool(ids) and all(bool((nights.get(n) or {}).get("ok")) for n in ids)


# ---------------------------------------------------------------------------
# Confirmation of the fast morning value
# ---------------------------------------------------------------------------
def max_idle_h(on, t0: float, t_end: float) -> float | None:
    """Longest span of [t0, t_end) without a heater start (hours)."""
    if t_end <= t0:
        return None
    st = sorted(float(x) for x in on if t0 <= x < t_end - 30.0)
    pts = [t0] + st + [t_end]
    return max(b - a for a, b in zip(pts, pts[1:])) / H


def gate_observables(on, t_sp: float, t_s: float | None, regulated_start: float | None) -> dict:
    """Observables of the confirmation gates of one morning.

    regulated_start: start of the regulated part of the period that the rise ends, None when the room
    never regulated at its low setpoint.
    """
    t_ref = t_s or t_sp
    reg_h = None if regulated_start is None else max(0.0, t_ref - regulated_start) / H
    idle = None
    if reg_h is not None and reg_h * H >= REGULATED_MIN_S:
        idle = max_idle_h(on, regulated_start, t_ref)
    return {"regulated_start": regulated_start, "regulated_h": reg_h, "max_idle_h": idle,
            "passive_2h": not any(t_ref - 2 * H <= x < t_ref - 30.0 for x in on)}


def _idle_reason(obs: dict) -> str | None:
    """Why the regulated part shows (or cannot exclude) a period without heating, None when it does not."""
    if obs.get("regulated_start") is None:
        return "no regulated start"
    reg_h = obs.get("regulated_h")
    if reg_h is None or reg_h * H < REGULATED_MIN_S:
        return "regulated part too short"
    idle = obs.get("max_idle_h")
    if idle is None:
        return "regulated part too short"
    return "idle in the regulated part" if idle >= GATE_IDLE_MIN_H else None


def unconfirmed(gate: str, obs: dict, T: float | None, b_fit_rel: float | None) -> str | None:
    """Reason why an accepted morning is unconfirmed under the gate, None when confirmed."""
    base = _idle_reason(obs)
    if gate == TAR_GATE_G1:
        return base
    if gate == TAR_GATE_GT4:
        if base is None:
            return None
        if T is None or T < GT4_T_MIN_S:
            return f"{base}, short delay"
        if b_fit_rel is not None and b_fit_rel < GT4_BFIT_REL_MIN:
            return f"{base}, low fitted loss"
        if not obs.get("passive_2h"):
            return f"{base}, regulated at the step"
        return None
    raise ValueError(f"unknown gate {gate}")
