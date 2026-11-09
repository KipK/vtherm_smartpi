"""Static model of the room from a regression of daily energy balances (pure Python).

Computed once a day by the room model job over the whole local days of the
recorded history (at most EB_MAX_DAYS) ending at the last local midnight. Over
one whole day d, the room's energy balance in the units of the model reads

    a * u_d = b_eq * D_d + dTa_d / H + (wall storage)_d - (gains)_d

where u_d is the time-weighted mean applied heat-equivalent power (0..1), D_d
the mean indoor-outdoor difference, H the duration and dTa_d the change of the
indoor temperature between the ends of the valid stretches (the air storage,
removed with the heating response a). Divided by a, with the free gains split
into a constant part g (a share of full power: heat from a warmer neighbour,
steady occupants; negative toward a colder neighbour) and a varying part:

    u_d = s * D_d - g + e_d,    s = b_eq / a

Weighted least squares over the days (weight: the valid share of the day)
gives the slope s, the static loss per degree in the units of b / a, and the
constant free gain g. The static holding power is then s * (T_sp - T_ext) - g.

The points are whole days: the walls store and release heat over hours, so a
shorter bin carries a storage term of the size of the signal after a setpoint
or outdoor change; over a day it is a small share, and the regression treats
what is left as scatter.

Qualification, from the uncertainty of the estimate (Student quantiles on
the residual scatter, so a few days with a lucky small scatter are not
trusted; each standard error is also bounded below by its jackknife value,
leaving one day out, so that an estimate resting on a single day far from
the others is not trusted either):
- whole days only, each with at most 20 % of missing or invalid data, some
  heating (a day without heating says nothing about the losses) and a
  positive indoor-outdoor difference;
- at least EB_MIN_DAYS usable days: two parameters and two degrees of freedom
  for the scatter;
- a mean heating and a mean indoor-outdoor difference large enough;
- with a spread of the indoor-outdoor difference between the days (weighted
  standard deviation at least EB_MIN_SPREAD_C), the slope with its constant
  gain: the 95 % confidence half-width of s at most EB_MAX_REL_HALF_WIDTH of
  s (the slope is the reference that the fitted b_eq / a must match within
  5 %, so it must be known at least as well), s in (0, EB_MAX_SLOPE], and a
  gain that leaves a heating need in (0, 1] on every used day and is no
  larger than the mean loss. Otherwise the day is refused: a gain that is
  seen (significant and plausible) with a slope not determined, or a line
  that does not explain the days (wall storage after a change); the ratio of
  the means is not a fallback here, it would carry the same error;
- with less spread, the slope alone (g = 0, the ratio of the means), with the
  same 95 % half-width bound. Below EB_MIN_SPREAD_C, ten times the day-to-day
  variation of a regulated indoor temperature, the differences between the
  days come from the regulation itself (a day with more heating is a little
  warmer), the regression would read them as a loss, and a constant gain
  cannot be told apart from the losses: it stays in the ratio of the means,
  right at the usual operating point, not away from it;
- the qualified form must show no drift: a linear trend of the day balances
  with time, fitted with the model, must not be both significant (95 %) and
  material (more than EB_MAX_DRIFT of the mean need across the days, half
  the 5 % tolerance: the storage still to come after the days is not in the
  trend). Wall storage after a change (start of the heating, a cold spell)
  drifts the heating need in one direction for days, which the scatter
  alone does not reveal;
- otherwise the day is refused, and the value in force is kept (manager.py).
"""
from __future__ import annotations

import bisect
import math
from typing import Any

EB_BIN_S = 600.0
EB_MAX_DAYS = 7

# Qualification
EB_MIN_DAYS = 4
EB_MIN_DAY_VALID = 0.80
EB_MIN_DAY_DUTY = 0.02
EB_MIN_DUTY = 0.05
EB_MIN_DELTA_C = 6.0
EB_MAX_REL_HALF_WIDTH = 0.05
EB_MIN_SPREAD_C = 1.0
EB_MAX_DRIFT = 0.025
EB_MAX_SLOPE = 3.0

# Two-sided 95 % Student quantiles by degrees of freedom
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306}

FORM_GAIN = "gain"
FORM_SLOPE = "slope"

REASON_HISTORY = "history"
REASON_NO_A = "no a"
REASON_DAYS = "days"
REASON_DELTA = "delta"
REASON_DUTY = "duty"
REASON_GAIN = "gain"
REASON_SCATTER = "scatter"
REASON_DRIFT = "drift"
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
    """Static model of the whole days before `day` (ending at its local midnight).

    Returns {"value", "gain", "reason", "slope", "form", "days"}: value and gain are the qualified slope and
    constant gain (None when refused), reason the refusal code (None when they qualify), slope the computed
    slope whatever the verdict, form the qualified form, days the number of days used.
    """
    rec = regression(signals(snap), clock, day, snap.get("a_per_min"))
    ok = rec["reason"] is None
    return {"value": rec["slope"] if ok else None, "gain": rec["gain"] if ok else None, "reason": rec["reason"],
            "slope": rec["slope"], "form": rec["form"] if ok else None, "days": rec["n"]}


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


class _Bins:
    """Valid 10 min bins of the recorded signals over [t0, t0 + n * step)."""

    def __init__(self, sig: dict, t0: float, n: int, step: float = EB_BIN_S) -> None:
        self.t0, self.n, self.step = t0, n, step
        self.u = _zoh_bin_means(sig["heat_t"], sig["heat_v"], t0, step, n, sig["heat_init"])
        self.tin = _zoh_bin_means(sig["temp_t"], sig["temp_v"], t0, step, n, None)
        self.text = _zoh_bin_means(sig["ext_t"], sig["ext_v"], t0, step, n, None)
        self.edge_t = [_value_at(sig["temp_t"], sig["temp_v"], t0 + k * step) for k in range(n + 1)]
        first, last = sig["first_t"], sig["last_t"]
        invalid = [g for g in sorted(sig["invalid"]) if g[1] > t0 and g[0] < t0 + n * step]
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
        """Energy balance over the bins [i0, i1): valid share, mean power, mean difference, and the mean power
        with the air storage removed (None without a)."""
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
               "delta_c": sd / nv if nv else None, "u": None}
        if nv and a_per_min is not None and a_per_min > 0.0:
            # Indoor storage in full-power minutes, removed from the applied energy
            out["u"] = (su * self.step / 60.0 - d_tin / a_per_min) / (nv * self.step / 60.0)
        return out


def day_points(sig: dict, clock, day: int, a_per_min: float | None) -> list[dict]:
    """Balance of each whole local day of the record before `day` (oldest first, at most EB_MAX_DAYS)."""
    out = []
    first, last = sig["first_t"], sig["last_t"]
    if first is None or last is None:
        return out
    for k in range(1, EB_MAX_DAYS + 1):
        d0, d1 = clock.at(day - k, 0.0), clock.at(day - k + 1, 0.0)
        if d0 < first or d1 > last:
            if d0 < first:
                break
            continue
        n = int(round((d1 - d0) / EB_BIN_S))
        w = _Bins(sig, d0, n).window(0, n, a_per_min)
        out.append({"day": day - k, "valid": w["valid_share"], "duty": w["duty"], "delta_c": w["delta_c"],
                    "u": w["u"]})
    return out[::-1]


def _t975(dof: int) -> float:
    return _T975.get(dof, 1.96 + 2.4 / dof) if dof >= 1 else math.inf


def _line(w: list[float], x: list[float], y: list[float]) -> tuple[float, float] | None:
    """Weighted least-squares slope and gain of u = s D - g, None without spread."""
    sw = sum(w)
    xm = sum(a * b for a, b in zip(w, x)) / sw
    ym = sum(a * b for a, b in zip(w, y)) / sw
    sxx = sum(a * (b - xm) ** 2 for a, b in zip(w, x))
    if sxx <= 1e-12 * sw * max(1.0, xm * xm):
        return None
    s = sum(a * (b - xm) * (c - ym) for a, b, c in zip(w, x, y)) / sxx
    return s, s * xm - ym


def _ratio(w: list[float], x: list[float], y: list[float]) -> float | None:
    """Weighted least-squares slope alone of u = s0 D."""
    sx2 = sum(a * b * b for a, b in zip(w, x))
    return None if sx2 <= 0.0 else sum(a * b * c for a, b, c in zip(w, x, y)) / sx2


def _jackknife(est, w: list[float], x: list[float], y: list[float]) -> list[float] | None:
    """Jackknife standard errors of the estimates of est (leave one day out), None when a subset is singular."""
    n = len(w)
    subs = []
    for i in range(n):
        r = est(w[:i] + w[i + 1:], x[:i] + x[i + 1:], y[:i] + y[i + 1:])
        if r is None:
            return None
        subs.append(r if isinstance(r, tuple) else (r,))
    k = len(subs[0])
    means = [sum(v[j] for v in subs) / n for j in range(k)]
    return [math.sqrt((n - 1) / n * sum((v[j] - means[j]) ** 2 for v in subs)) for j in range(k)]


def fit(points: list[dict]) -> dict[str, Any]:
    """Weighted least squares of u = s * D - g (weights: valid share), and of u = s0 * D (slope alone).

    Returns the estimates, their standard errors and the relative 95 % half-widths of the slopes. Each standard
    error is the larger of the classical one (from the scatter around the line) and the jackknife one (leave one
    day out): a day far from the others sits on the line whatever its own error, so the scatter alone would not
    show it, and an estimate that rests on one day is not trusted.
    """
    w = [p["valid"] for p in points]
    x = [p["delta_c"] for p in points]
    y = [p["u"] for p in points]
    n = len(points)
    sw = sum(w)
    out: dict[str, Any] = {"n": n, "slope": None, "gain": None, "half_width": math.inf, "gain_t": None,
                           "slope_only": None, "slope_only_half_width": math.inf}
    if n < 2 or sw <= 0.0:
        return out
    xm = sum(a * b for a, b in zip(w, x)) / sw
    ym = sum(a * b for a, b in zip(w, y)) / sw
    sxx = sum(a * (b - xm) ** 2 for a, b in zip(w, x))
    out.update(mean_delta_c=xm, mean_u=ym, sd_delta_c=math.sqrt(sxx / sw))
    line = _line(w, x, y) if n >= 3 else None
    if line is not None:
        s, g = line
        var = sum(a * (c - (s * b - g)) ** 2 for a, b, c in zip(w, x, y)) / (n - 2)
        se_s = math.sqrt(var / sxx)
        se_g = math.sqrt(var * (1.0 / sw + xm * xm / sxx))
        jack = _jackknife(_line, w, x, y)
        if jack is None:
            se_s = se_g = math.inf
        else:
            se_s, se_g = max(se_s, jack[0]), max(se_g, jack[1])
        out.update(slope=s, gain=g, se_slope=se_s, se_gain=se_g, scatter=math.sqrt(var),
                   half_width=_t975(n - 2) * se_s / s if s > 0.0 else math.inf,
                   gain_t=(g / se_g if se_g > 0.0 else (0.0 if g == 0.0 else math.inf)))
    s0 = _ratio(w, x, y)
    if s0 is not None:
        sx2 = sum(a * b * b for a, b in zip(w, x))
        var0 = sum(a * (c - s0 * b) ** 2 for a, b, c in zip(w, x, y)) / (n - 1)
        se0 = math.sqrt(var0 / sx2)
        jack = _jackknife(_ratio, w, x, y)
        se0 = math.inf if jack is None else max(se0, jack[0])
        out.update(slope_only=s0, se_slope_only=se0,
                   slope_only_half_width=_t975(n - 1) * se0 / s0 if s0 > 0.0 and math.isfinite(se0) else math.inf)
    return out


def _wls(cols: list[list[float]], y: list[float], w: list[float]) -> tuple[list[float], list[float], int] | None:
    """Weighted least squares by the normal equations: (coefficients, their standard errors, degrees of freedom);
    None when singular or without a degree of freedom."""
    k, n = len(cols), len(y)
    dof = n - k
    if dof < 1:
        return None
    m = [[sum(wi * a * b for wi, a, b in zip(w, cols[i], cols[j])) for j in range(k)] for i in range(k)]
    v = [sum(wi * a * yi for wi, a, yi in zip(w, cols[i], y)) for i in range(k)]
    # Gauss-Jordan inverse of the normal matrix
    inv = [[float(i == j) for j in range(k)] for i in range(k)]
    a = [row[:] for row in m]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(a[r][c]))
        if abs(a[piv][c]) < 1e-12 * max(1.0, abs(m[c][c])):
            return None
        a[c], a[piv], inv[c], inv[piv] = a[piv], a[c], inv[piv], inv[c]
        d = a[c][c]
        a[c] = [x / d for x in a[c]]
        inv[c] = [x / d for x in inv[c]]
        for r in range(k):
            if r != c and a[r][c] != 0.0:
                f = a[r][c]
                a[r] = [x - f * z for x, z in zip(a[r], a[c])]
                inv[r] = [x - f * z for x, z in zip(inv[r], inv[c])]
    beta = [sum(inv[i][j] * v[j] for j in range(k)) for i in range(k)]
    res = [yi - sum(b * col[t] for b, col in zip(beta, cols)) for t, yi in enumerate(y)]
    var = sum(wi * r * r for wi, r in zip(w, res)) / dof
    return beta, [math.sqrt(max(var * inv[i][i], 0.0)) for i in range(k)], dof


def _drifts(points: list[dict], with_gain: bool) -> bool:
    """The day balances drift in time beyond the static model: a linear trend with the day, fitted together with
    the model, significant (95 %) and material (it moves the heating need by more than EB_MAX_DRIFT of its mean
    across the days). Storage in the walls after a change (start of the heating, a cold spell) makes the
    heating need drift in one direction for days; the regression assumes independent day errors."""
    t0 = sum(p["day"] for p in points) / len(points)
    cols = [[p["delta_c"] for p in points]]
    if with_gain:
        cols.append([-1.0] * len(points))
    cols.append([float(p["day"]) - t0 for p in points])
    out = _wls(cols, [p["u"] for p in points], [p["valid"] for p in points])
    if out is None:
        return False
    beta, se, dof = out
    span = max(p["day"] for p in points) - min(p["day"] for p in points)
    mean_u = sum(p["valid"] * p["u"] for p in points) / sum(p["valid"] for p in points)
    material = abs(beta[-1]) * span > EB_MAX_DRIFT * abs(mean_u)
    return material and se[-1] > 0.0 and abs(beta[-1] / se[-1]) > _t975(dof)


def _gain_plausible(s: float, g: float, points: list[dict], mean_delta: float) -> bool:
    """A constant gain that leaves a heating need in (0, 1] on every day used and is no larger than the mean
    loss."""
    if abs(g) > s * mean_delta:
        return False
    return all(0.0 < s * p["delta_c"] - g <= 1.0 for p in points)


def regression(sig: dict, clock, day: int, a_per_min: float | None) -> dict[str, Any]:
    """Static model (slope s, constant gain g) of the whole days before `day`, and its verdict.

    Returns the fit record with "slope", "gain", "form" (the form the slope is reported in: with its gain when
    the slope is determined, else the slope alone), "reason" (None when it qualifies) and "points".
    """
    return qualify(day_points(sig, clock, day, a_per_min), a_per_min)


def qualify(pts: list[dict], a_per_min: float | None) -> dict[str, Any]:
    """Regression of the day points (day_points) and its verdict (regression)."""
    rec: dict[str, Any] = {"points": pts, "n": 0, "slope": None, "gain": None, "form": None}
    if len(pts) < EB_MIN_DAYS:
        rec["reason"] = REASON_HISTORY
        return rec
    if a_per_min is None or a_per_min <= 0.0:
        rec["reason"] = REASON_NO_A
        return rec
    used = [p for p in pts if p["valid"] >= EB_MIN_DAY_VALID and p["u"] is not None and p["duty"] is not None
            and p["duty"] >= EB_MIN_DAY_DUTY and p["delta_c"] is not None and p["delta_c"] > 0.0]
    rec["n"] = len(used)
    if len(used) < EB_MIN_DAYS:
        rec["reason"] = REASON_DAYS
        return rec
    f = fit(used)
    rec["fit"] = f
    sw = sum(p["valid"] for p in used)
    if sum(p["valid"] * p["duty"] for p in used) / sw < EB_MIN_DUTY:
        rec["reason"] = REASON_DUTY
        return rec
    if f["mean_delta_c"] < EB_MIN_DELTA_C:
        rec["reason"] = REASON_DELTA
        return rec
    s, g, s0 = f["slope"], f["gain"], f["slope_only"]
    if f["sd_delta_c"] >= EB_MIN_SPREAD_C:
        # Enough spread: the line with its gain is the model; when it is not determined, the ratio of the means
        # is not a fallback, it would carry what the line does not explain (wall storage after a change)
        if s is not None and f["half_width"] <= EB_MAX_REL_HALF_WIDTH:
            if not (0.0 < s <= EB_MAX_SLOPE and _gain_plausible(s, g, used, f["mean_delta_c"])):
                reason = REASON_RANGE
            else:
                reason = REASON_DRIFT if _drifts(used, True) else None
            rec.update(slope=s, gain=g, form=FORM_GAIN, reason=reason)
            return rec
        seen = (s is not None and s > 0.0 and f["gain_t"] is not None and abs(f["gain_t"]) > _t975(len(used) - 2)
                and _gain_plausible(s, g, used, f["mean_delta_c"]))
        rec.update(slope=s, gain=g, form=FORM_GAIN, reason=REASON_GAIN if seen else REASON_SCATTER)
        return rec
    # Too little spread to read a slope: the day-to-day differences of D are driven by the regulation itself (a
    # day with more heating is a little warmer), not by the outdoor temperature, and a constant gain cannot be
    # told apart from the losses. The slope alone, the ratio of the means.
    if s0 is not None and f["slope_only_half_width"] <= EB_MAX_REL_HALF_WIDTH:
        if not 0.0 < s0 <= EB_MAX_SLOPE:
            reason = REASON_RANGE
        else:
            reason = REASON_DRIFT if _drifts(used, False) else None
        rec.update(slope=s0, gain=0.0, form=FORM_SLOPE, reason=reason)
        return rec
    rec.update(slope=s0, gain=0.0, form=FORM_SLOPE, reason=REASON_SCATTER)
    return rec
    s0 = f["slope_only"]
    if not gain_seen and s0 is not None and f["slope_only_half_width"] <= EB_MAX_REL_HALF_WIDTH:
        if not 0.0 < s0 <= EB_MAX_SLOPE:
            reason = REASON_RANGE
        else:
            reason = REASON_DRIFT if _drifts(used, False) else None
        rec.update(slope=s0, gain=0.0, form=FORM_SLOPE, reason=reason)
        return rec
    if gain_seen:
        rec.update(slope=s, gain=g, form=FORM_GAIN, reason=REASON_GAIN)
    else:
        rec.update(slope=s0, gain=0.0, form=FORM_SLOPE, reason=REASON_SCATTER)
    return rec
