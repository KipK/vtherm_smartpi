"""Static gain b_eq / a from a multi-day energy balance (pure Python).

Computed once a day by the room model job over the EB_WINDOW_DAYS whole local
days ending at the last local midnight. A qualified ratio is the static gain
of the room model feed-forward and the reference its fits must agree with; a
refused day keeps the previous qualified ratio (manager.py). Over a window of
whole days, the room's energy balance in the units of the model reads

    a * mean(u) = b_eq * mean(Ta - Text) + dTa / H + (wall storage) - mean(gains)

where u is the applied heat-equivalent power (0..1), H the window duration and
dTa the change of the indoor temperature between the window ends. Its ratio,
with the indoor storage dTa / H removed, estimates b_eq / a. The wall storage
and the unmeasured gains (sun, occupants) bias it; the refusal rules reject the
windows where they show:
- too much missing or excluded data;
- the heating response a not known (needed for the indoor storage);
- an indoor-outdoor difference too small;
- too little heating (a day without heating demand says nothing about the
  losses);
- a heating need that differs between the days of the window;
- a ratio that moves too much between windows shifted by a few hours (a
  balance not settled at the window ends);
- a value out of range.
The means are time weighted over 10 min bins of the recorded signals, the
invalid intervals excluded.
"""
from __future__ import annotations

import bisect
from typing import Any

EB_BIN_S = 600.0
EB_WINDOW_DAYS = 3
EB_SHIFT_STEP_H = 2.0
EB_SHIFTS = 12  # windows ending 0, 2, ..., 22 h before the reference end

# Refusal thresholds
EB_MIN_DELTA_C = 6.0
EB_MIN_DUTY = 0.05
EB_MIN_DAY_DUTY = 0.02
EB_MAX_EXCLUDED = 0.20
EB_MAX_SPREAD = 0.03
EB_MAX_DAY_SPREAD = 0.05
EB_MAX_RATIO = 3.0

REASON_HISTORY = "history"
REASON_EXCLUDED = "excluded"
REASON_NO_A = "no a"
REASON_DELTA = "delta"
REASON_DUTY = "duty"
REASON_DAY_DUTY = "day duty"
REASON_DAY_SPREAD = "day spread"
REASON_SPREAD = "spread"
REASON_RANGE = "range"


def signals(snap: dict) -> dict:
    """Recorded signals of a job snapshot in the form of the energy balance."""
    if snap.get("pw") is not None:
        heat_t, heat_v, init = list(snap["pw"][0]), list(snap["pw"][1]), 0.0
    else:
        heat_t, heat_v, init = heater_timeline(snap["on"], snap["off"])
    return {"heat_t": heat_t, "heat_v": heat_v, "heat_init": init, "temp_t": snap["pub_t"], "temp_v": snap["pub_v"],
            "ext_t": snap["ext_t"], "ext_v": snap["ext_v"], "first_t": snap["first_t"], "last_t": snap["last_t"],
            "invalid": [tuple(g) for g in snap["gaps"]]}


def daily(snap: dict, clock, day: int) -> dict[str, Any]:
    """Energy balance of the EB_WINDOW_DAYS whole local days before `day` (ending at its local midnight).

    Returns {"value", "reason", "ratio"}: value is the qualified ratio (None when refused), reason the refusal
    code (None when it qualifies), ratio the computed ratio whatever the verdict.
    """
    rec = static_gain(signals(snap), clock.at(day, 0.0), snap.get("a_per_min"))
    reason = rec.get("reason")
    ratio = rec.get("value")
    return {"value": ratio if reason is None else None, "reason": reason, "ratio": ratio}


def heater_timeline(on: list, off: list) -> tuple[list, list, float]:
    """Applied power timeline (times, levels, level before the first time) of PWM heater edges."""
    ev = sorted([(float(x), 1.0) for x in on] + [(float(x), 0.0) for x in off])
    init = 1.0 if ev and ev[0][1] == 0.0 else 0.0
    ts: list[float] = []
    vs: list[float] = []
    state = init
    i = 0
    while i < len(ev):
        j = i
        while j < len(ev) and ev[j][0] == ev[i][0]:
            j += 1
        kinds = {v for _, v in ev[i:j]}
        # An on and an off edge at the same instant are a zero-length pulse: the state is unchanged.
        new = state if len(kinds) == 2 else kinds.pop()
        if new != state:
            ts.append(ev[i][0])
            vs.append(new)
            state = new
        i = j
    return ts, vs, init


def _zoh_bin_means(ts: list, vs: list, t0: float, step: float, n: int, initial: float | None) -> list:
    """Mean of the zero-order hold of (ts, vs) over n bins from t0; None where part of a bin has no value."""
    out: list = [None] * n
    j = bisect.bisect_right(ts, t0)
    cur = vs[j - 1] if j > 0 else initial
    for k in range(n):
        b0 = t0 + k * step
        b1 = b0 + step
        t_cur = b0
        acc = 0.0
        covered = 0.0
        while j < len(ts) and ts[j] < b1:
            if cur is not None:
                acc += cur * (ts[j] - t_cur)
                covered += ts[j] - t_cur
            t_cur = max(t_cur, ts[j])
            cur = vs[j]
            j += 1
        if cur is not None:
            acc += cur * (b1 - t_cur)
            covered += b1 - t_cur
        if covered >= step - 1e-6:
            out[k] = acc / step
    return out


def _value_at(ts: list, vs: list, t: float) -> float | None:
    i = bisect.bisect_right(ts, t)
    return vs[i - 1] if i > 0 else None


def _median(v: list) -> float:
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


class _Bins:
    """Valid 10 min bins of the recorded signals over [t0, t0 + n * step)."""

    def __init__(self, sig: dict, t0: float, n: int, step: float = EB_BIN_S) -> None:
        self.t0, self.n, self.step = t0, n, step
        self.u = _zoh_bin_means(sig["heat_t"], sig["heat_v"], t0, step, n, sig["heat_init"])
        self.tin = _zoh_bin_means(sig["temp_t"], sig["temp_v"], t0, step, n, None)
        self.text = _zoh_bin_means(sig["ext_t"], sig["ext_v"], t0, step, n, None)
        self.edge_t = [_value_at(sig["temp_t"], sig["temp_v"], t0 + k * step) for k in range(n + 1)]
        first, last = sig["first_t"], sig["last_t"]
        invalid = sorted(sig["invalid"])
        self.valid = []
        for k in range(n):
            b0 = t0 + k * step
            b1 = b0 + step
            ok = (b0 >= first and b1 <= last and self.u[k] is not None and self.tin[k] is not None
                  and self.text[k] is not None and self.edge_t[k] is not None and self.edge_t[k + 1] is not None)
            if ok:
                ok = not any(g0 < b1 and g1 > b0 for g0, g1 in invalid)
            self.valid.append(ok)

    def window(self, i0: int, i1: int, a_per_min: float | None) -> dict:
        """Energy balance over the bins [i0, i1)."""
        su = sd = 0.0
        nv = 0
        d_tin = 0.0
        seg = None
        for i in range(i0, i1 + 1):
            ok = i < i1 and self.valid[i]
            if ok:
                if seg is None:
                    seg = i
                su += self.u[i]
                sd += self.tin[i] - self.text[i]
                nv += 1
            elif seg is not None:
                d_tin += self.edge_t[i] - self.edge_t[seg]
                seg = None
        n = i1 - i0
        out = {"valid_share": nv / n if n else 0.0, "duty": su / nv if nv else None,
               "delta_c": sd / nv if nv else None, "raw": None, "ratio": None}
        if nv and sd > 0.0:
            out["raw"] = su / sd
            if a_per_min is not None and a_per_min > 0.0:
                # Indoor storage in power x time units (minutes), removed from the applied energy
                storage = d_tin / a_per_min
                out["ratio"] = (su * self.step / 60.0 - storage) / (sd * self.step / 60.0)
        return out


def static_gain(sig: dict, end: float, a_per_min: float | None) -> dict[str, Any]:
    """Energy balance b_eq / a of the EB_WINDOW_DAYS days ending at `end`.

    sig: recorded signals (heat_t, heat_v, heat_init: applied power timeline;
    temp_t, temp_v; ext_t, ext_v; first_t, last_t; invalid intervals).
    Returns {"value", "reason"}: reason is None when the value qualifies.
    """
    per_day = int(round(86400.0 / EB_BIN_S))
    nb = EB_WINDOW_DAYS * per_day
    shift = int(round(EB_SHIFT_STEP_H * 3600.0 / EB_BIN_S))
    span = nb + shift * (EB_SHIFTS - 1)
    t0 = end - span * EB_BIN_S
    rec: dict[str, Any] = {"end": end, "value": None}
    if sig["first_t"] is None or sig["first_t"] > t0 or sig["last_t"] is None or sig["last_t"] < end:
        rec["reason"] = REASON_HISTORY
        return rec
    bins = _Bins(sig, t0, span)
    main = bins.window(span - nb, span, a_per_min)
    rec.update(main)
    days = []
    for d in range(EB_WINDOW_DAYS):
        w = bins.window(span - nb + d * per_day, span - nb + (d + 1) * per_day, a_per_min)
        days.append({"duty": w["duty"], "raw": w["raw"]})
    rec["days"] = days
    raws = sorted(d["raw"] for d in days if d["raw"] is not None)
    rec["day_spread"] = ((raws[-1] - raws[0]) / _median(raws)
                         if len(raws) == len(days) and _median(raws) > 0.0 else None)
    fam = []
    for j in range(EB_SHIFTS):
        w = bins.window(span - nb - j * shift, span - j * shift, a_per_min)
        fam.append(w["ratio"] if w["valid_share"] >= 1.0 - EB_MAX_EXCLUDED else None)
    rec["family"] = fam
    vals = sorted(x for x in fam if x is not None)
    rec["median"] = _median(vals) if vals else None
    trimmed = vals[1:-1]
    rec["spread"] = ((trimmed[-1] - trimmed[0]) / abs(rec["median"])
                     if len(trimmed) >= 2 and rec["median"] else None)
    rec["value"] = main["ratio"]
    rec["reason"] = _refusal(rec, a_per_min)
    return rec


def _refusal(rec: dict, a_per_min: float | None) -> str | None:
    """Why the computed ratio must not be used, None when it qualifies."""
    if rec["valid_share"] < 1.0 - EB_MAX_EXCLUDED or None in rec["family"]:
        return REASON_EXCLUDED
    if a_per_min is None or a_per_min <= 0.0:
        return REASON_NO_A
    if rec["delta_c"] is None or rec["delta_c"] < EB_MIN_DELTA_C:
        return REASON_DELTA
    if rec["duty"] is None or rec["duty"] < EB_MIN_DUTY:
        return REASON_DUTY
    if any(d["duty"] is None or d["duty"] < EB_MIN_DAY_DUTY for d in rec["days"]):
        return REASON_DAY_DUTY
    if rec["day_spread"] is None or rec["day_spread"] > EB_MAX_DAY_SPREAD:
        return REASON_DAY_SPREAD
    if rec["spread"] is None or rec["spread"] > EB_MAX_SPREAD:
        return REASON_SPREAD
    if rec["value"] is None or not 0.0 < rec["value"] <= EB_MAX_RATIO:
        return REASON_RANGE
    return None
