"""Gates of the T_ar learning.

Regulated night (hand-over of the refined value). The sleep span of a night is
[23:00, t_end) local, t_end the comfort step of the next morning (first setpoint
rise in [03:00, 08:00)), else 08:00. The night is regulated iff, over that span:
  (a) the setpoint is constant;
  (b) every 30 min quarter (from 23:00; a last piece shorter than 15 min is
      merged into the previous quarter) holds a heater start;
  (c) the mean published temperature of every quarter is within 0.3 C of the
      setpoint;
  (d) the per-cycle mean heater input has |slope| <= 0.05 /h and a residual
      std around its linear trend <= 0.10;
  (e) the data of the span hold no gap.
An unmeasured night gain (occupants) is nearly collinear with the loss b in a
free fall, while during regulation it shows as a duty change: the refined value
may replace the fast value only when the six nights of its two windows are
regulated.

Confirmation of the fast morning value. A sleeper leaving during the fit window
bends the rise and shortens the apparent delay; a night that starts passive in
the sleep span is the observable sign of that risk:
  G1  : at least 1 h without heater start in [23:00, t_ref) -> unconfirmed;
  GT4 : G1 and (T < 900 s, or the fitted exponential baseline b below 0.6 x the
        published b, or a heater start in the 2 h before t_ref) -> unconfirmed.
t_ref is the step (first heater start after the setpoint rise), else the rise.
"""
from __future__ import annotations

from .const import (
    GATE_IDLE_MIN_H,
    GATE_SLEEP_START_H,
    GT4_BFIT_REL_MIN,
    GT4_T_MIN_S,
    H,
    RN_ECO_TOL_C,
    RN_NIGHTS_PER_WINDOW,
    RN_QUARTER_MERGE_S,
    RN_QUARTER_S,
    RN_SP_EPS,
    RN_SP_RISE_FROM_H,
    RN_SPAN_END_MAX_H,
    RN_SPAN_START_H,
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


def regulated_night(clock, night_day: int, on, off, pub_t, pub_v, sp0: float, ev, cycle_s: float,
                    init_on: bool = False, valid_fn=None) -> dict:
    """Regulated-night gate of the night that starts on the evening of `night_day`."""
    import numpy as np  # pylint: disable=import-outside-toplevel

    from .signals import duty_bins  # pylint: disable=import-outside-toplevel

    a = clock.at(night_day, RN_SPAN_START_H)
    lo = clock.at(night_day + 1, RN_SP_RISE_FROM_H)
    hi = clock.at(night_day + 1, RN_SPAN_END_MAX_H)
    sp_a = sp_at(sp0, ev, a)
    rises = [te for te, old, new in sp_changes_in(sp0, ev, lo - 1e-6, hi) if new > old + RN_SP_EPS and te >= lo]
    b = rises[0] if rises else hi
    out = {"t_start": a, "t_end": b, "sp": sp_a, "why": None}
    if valid_fn is not None and not valid_fn(a, b):
        out["why"] = "data gap"
        return dict(out, ok=False)
    ch = sp_changes_in(sp0, ev, a, b)
    if ch:
        out["why"] = "setpoint change in the sleep span"
        return dict(out, ok=False)
    on = np.asarray(on, dtype=float)
    qs = quarters(a, b)
    starts = [int(np.sum((on >= x) & (on < y))) for x, y in qs]
    out["min_starts"] = min(starts) if starts else 0
    devs = []
    for x, y in qs:
        m = zoh_mean(pub_t, pub_v, x, y)
        devs.append(None if m is None else m - sp_a)
    out["max_dev"] = None if any(v is None for v in devs) else float(max(abs(v) for v in devs))
    n = int(round((b - a) / cycle_s))
    bins_start = np.asarray([b - (j + 1) * cycle_s for j in range(n)][::-1])
    um = duty_bins(on, np.asarray(off, dtype=float), bins_start, cycle_s, init_on=init_on) if n >= 3 else None
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


def window_regulated(nights: dict, day: int, win_days: int) -> bool:
    """Window ending at 08:00 of `day`: its nights (evenings day - win_days .. day - 1) are all regulated."""
    ns = range(day - win_days, day)
    ok = [bool((nights.get(n) or {}).get("ok")) for n in ns]
    return sum(ok) >= RN_NIGHTS_PER_WINDOW


# ---------------------------------------------------------------------------
# Confirmation of the fast morning value
# ---------------------------------------------------------------------------
def max_idle_h(on, t23: float, t_end: float) -> float | None:
    """Longest span of [t23, t_end) without a heater start (hours)."""
    if t_end <= t23:
        return None
    st = sorted(float(x) for x in on if t23 <= x < t_end - 30.0)
    pts = [t23] + st + [t_end]
    return max(b - a for a, b in zip(pts, pts[1:])) / H


def gate_observables(clock, on, t_sp: float, t_s: float | None) -> dict:
    """Observables of the confirmation gates of one morning."""
    t_ref = t_s or t_sp
    t23 = clock.at(clock.day_of(t_sp) - 1, GATE_SLEEP_START_H)
    return {"max_idle_sleep_h": max_idle_h(on, t23, t_ref),
            "passive_2h": not any(t_ref - 2 * H <= x < t_ref - 30.0 for x in on)}


def unconfirmed(gate: str, obs: dict, T: float | None, b_fit_rel: float | None) -> str | None:
    """Reason why an accepted morning is unconfirmed under the gate, None when confirmed."""
    idle = (obs.get("max_idle_sleep_h") or 0.0) >= GATE_IDLE_MIN_H
    if gate == TAR_GATE_G1:
        return "night started passive in the sleep span" if idle else None
    if gate == TAR_GATE_GT4:
        if not idle:
            return None
        if (T or 0.0) < GT4_T_MIN_S:
            return "night started passive in the sleep span, short delay"
        if b_fit_rel is not None and b_fit_rel < GT4_BFIT_REL_MIN:
            return "night started passive in the sleep span, low fitted loss"
        if not obs.get("passive_2h"):
            return "night started passive in the sleep span, regulated at the step"
        return None
    raise ValueError(f"unknown gate {gate}")
