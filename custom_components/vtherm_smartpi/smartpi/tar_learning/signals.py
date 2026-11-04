"""Signal primitives of the T_ar learning (numpy): temperature anchors, outdoor
timeline, heater power on a grid (from PWM heater edges or from a valve power
level timeline).

Units: times in s, temperatures in C, the loss b per minute at the interfaces.
"""
from __future__ import annotations

import math

import numpy as np

from .const import FIT_DT, RM_BIN_S, SENSOR_SPAN_S


class Anchors:
    """Anchors (time, value) of a temperature sensor publishing on change.

    Two reconstructions, chosen from the publications themselves:
    - crossing anchors (default): a one-step change is dated at its publication
      and valued at the midpoint of the two levels, a multi-step change keeps
      its value; the first publication is not an anchor;
    - dithered sensor (a noise of the order of the step makes the published
      value flicker: at least DITHER_REVERSALS of the changes reverse the
      previous one): the held published signal is sampled on a regular grid and
      shifted back by half the sampling period of the sensor (5th percentile of
      the intervals between changes).
    """

    DITHER_REVERSALS = 0.25
    GRID_MIN_S = 10.0

    def __init__(self, pub_t, pub_v, mode: str = "auto") -> None:
        pub_t = np.asarray(pub_t, dtype=float)
        pub_v = np.asarray(pub_v, dtype=float)
        ok = np.isfinite(pub_v)
        pub_t, pub_v = pub_t[ok], pub_v[ok]
        self.pub_t, self.pub_v = pub_t, pub_v
        diffs = np.diff(pub_v)
        ch = diffs[np.abs(diffs) > 1e-9]
        self.q = float(np.min(np.abs(ch))) if len(ch) else 0.01
        rev = float(np.mean(np.sign(ch[1:]) != np.sign(ch[:-1]))) if len(ch) > 2 else 0.0
        self.reversal_frac = rev
        if mode == "auto":
            mode = "dither" if rev >= self.DITHER_REVERSALS else "crossing"
        self.mode = mode
        if mode == "crossing":
            single = 1.5 * self.q
            t, v = [], []
            prev = None
            for ti, vi in zip(pub_t, pub_v):
                # The first publication is a held value of unknown age: not an anchor.
                if prev is not None and vi != prev:
                    t.append(ti)
                    v.append(0.5 * (vi + prev) if abs(vi - prev) <= single else vi)
                prev = vi
            self.t = np.asarray(t)
            self.v = np.asarray(v)
            self.sample_s = None
        else:
            # Sampling period: 5th percentile of the intervals between value changes.
            t_ch = pub_t[1:][np.abs(diffs) > 1e-9]
            gaps = np.diff(t_ch)
            gaps = gaps[gaps > 0]
            self.sample_s = float(np.percentile(gaps, 5)) if len(gaps) else self.GRID_MIN_S
            step = max(self.GRID_MIN_S, self.sample_s)
            shift = 0.5 * self.sample_s
            grid = np.arange(pub_t[0] + step, pub_t[-1], step)
            idx = np.searchsorted(pub_t, grid + shift, side="right") - 1
            self.t = grid
            self.v = pub_v[np.clip(idx, 0, len(pub_v) - 1)]

    def first_reach(self, t_from: float, level: float) -> float | None:
        """First publication time after t_from with a value >= level."""
        i0 = int(np.searchsorted(self.pub_t, t_from, side="right"))
        hit = np.nonzero(self.pub_v[i0:] >= level)[0]
        return float(self.pub_t[i0 + hit[0]]) if len(hit) else None


class ExtTL:
    """Outdoor temperature timeline (zero-order hold)."""

    def __init__(self, t, v) -> None:
        t = np.asarray(t, dtype=float)
        v = np.asarray(v, dtype=float)
        ok = np.isfinite(v)
        self.t, self.v = t[ok], v[ok]

    def integrals(self, w0: float, w1: float, origin: float) -> tuple[float, float]:
        """(int T_ext, int (t - origin) T_ext) over [w0, w1], zero-order hold."""
        t, v = self.t, self.v
        i0 = max(0, int(np.searchsorted(t, w0, side="right")) - 1)
        i1 = int(np.searchsorted(t, w1, side="left"))
        tt = t[i0:i1 + 1]
        vv = v[i0:i1 + 1]
        starts = np.clip(tt, w0, w1)
        ends = np.clip(np.append(tt[1:], np.inf), w0, w1)
        if w0 < t[0]:
            starts = np.append(w0, starts)
            ends = np.append(min(t[0], w1), ends)
            vv = np.append(v[0], vv)
        a, b = starts - origin, ends - origin
        return float(np.sum(vv * (b - a))), float(np.sum(vv * (b * b - a * a) / 2.0))


def u_on_grid(on_edges, off_edges, t0: float, t1: float, step: float = FIT_DT):
    """Heater input (0..1) averaged over each step of the grid [t0, t1)."""
    ev = sorted([(float(x), 1.0) for x in on_edges] + [(float(x), 0.0) for x in off_edges])
    grid = np.arange(t0, t1, step)
    u = np.zeros(len(grid))
    state = 0.0
    # State at t0: last edge before t0.
    prev = [v for t, v in ev if t <= t0]
    if prev:
        state = prev[-1]
    elif ev and ev[0][1] == 0.0:
        state = 1.0
    cur_t = t0
    for t, v in [e for e in ev if t0 < e[0] < t1] + [(t1, None)]:
        if state > 0:
            a = (cur_t - t0) / step
            b = (t - t0) / step
            ia, ib = int(math.floor(a)), int(math.floor(b))
            if ia == ib:
                if ia < len(u):
                    u[ia] += b - a
            else:
                if ia < len(u):
                    u[ia] += ia + 1 - a
                u[ia + 1:min(ib, len(u))] += 1.0
                if ib < len(u):
                    u[ib] += b - ib
        cur_t = t
        if v is not None:
            state = v
    return grid, np.clip(u, 0.0, 1.0)


def duty_bins(on, off, grid_start: np.ndarray, step: float, init_on: bool | None = None) -> np.ndarray:
    """Mean of the heater input state (1 between an on edge and the next off edge) over each bin."""
    on = np.asarray(on, dtype=float)
    off = np.asarray(off, dtype=float)
    ev_t = np.r_[on, off]
    ev_v = np.r_[np.ones(len(on)), np.zeros(len(off))]
    o = np.argsort(ev_t, kind="stable")
    ev_t, ev_v = ev_t[o], ev_v[o]
    if init_on is None:
        init_on = bool(len(off) and (len(on) == 0 or off[0] < on[0]))
    t0 = min(float(grid_start[0]), float(ev_t[0]) if len(ev_t) else float(grid_start[0])) - 1.0
    tt = np.r_[t0, ev_t]
    st = np.r_[1.0 if init_on else 0.0, ev_v]
    cum = np.r_[0.0, np.cumsum(st[:-1] * np.diff(tt))]

    def cumulative(s):
        k = np.clip(np.searchsorted(tt, s, side="right") - 1, 0, len(st) - 1)
        return cum[k] + st[k] * (s - tt[k])

    return (cumulative(grid_start + step) - cumulative(grid_start)) / step


def level_bins(pw_t, pw_v, grid_start: np.ndarray, step: float) -> np.ndarray:
    """Mean over each bin of a power level timeline (zero-order hold, 0 before its first sample)."""
    t = np.asarray(pw_t, dtype=float)
    v = np.asarray(pw_v, dtype=float)
    grid_start = np.asarray(grid_start, dtype=float)
    if len(t) == 0 or len(grid_start) == 0:
        return np.zeros(len(grid_start))
    tt = np.r_[min(float(t[0]), float(grid_start[0])) - 1.0, t]
    vv = np.r_[0.0, v]
    cum = np.r_[0.0, np.cumsum(vv[:-1] * np.diff(tt))]

    def cumulative(s):
        k = np.clip(np.searchsorted(tt, s, side="right") - 1, 0, len(vv) - 1)
        return cum[k] + vv[k] * (s - tt[k])

    return (cumulative(grid_start + step) - cumulative(grid_start)) / step


def level_on_grid(pw_t, pw_v, t0: float, t1: float, step: float = FIT_DT):
    """Valve power level (0..1) averaged over each step of the grid [t0, t1)."""
    grid = np.arange(t0, t1, step)
    return grid, np.clip(level_bins(pw_t, pw_v, grid, step), 0.0, 1.0)


def bin_mean(t, v, grid_start: np.ndarray, step: float) -> np.ndarray:
    """Mean over each bin of the zero-order hold of (t, v)."""
    t = np.asarray(t, dtype=float)
    v = np.asarray(v, dtype=float)
    fine = (grid_start[:, None] + (np.arange(10) + 0.5)[None, :] * step / 10.0).ravel()
    i = np.clip(np.searchsorted(t, fine, side="right") - 1, 0, len(v) - 1)
    return v[i].reshape(len(grid_start), 10).mean(axis=1)


def binned(anch: Anchors, s0: float, s1: float):
    """Anchors in [s0, s1] averaged in RM_BIN_S bins (mean time, mean value)."""
    m = (anch.t >= s0) & (anch.t <= s1)
    t, v = anch.t[m], anch.v[m]
    if len(t) == 0:
        return t, v
    b = ((t - s0) // RM_BIN_S).astype(int)
    _, inv = np.unique(b, return_inverse=True)
    cnt = np.bincount(inv)
    return np.bincount(inv, t) / cnt, np.bincount(inv, v) / cnt


def sensor_period(pub_t, pub_v, t_s: float) -> float | None:
    """5th percentile of the intervals between value changes over the 24 h before t_s."""
    pt, pv = np.asarray(pub_t, dtype=float), np.asarray(pub_v, dtype=float)
    m = (pt >= t_s - SENSOR_SPAN_S) & (pt <= t_s)
    t, v = pt[m], pv[m]
    if len(t) < 3:
        return None
    ch = np.nonzero(np.abs(np.diff(v)) > 1e-9)[0]
    tc = t[1:][ch]
    gaps = np.diff(tc)
    gaps = gaps[gaps > 0]
    return float(np.percentile(gaps, 5)) if len(gaps) else None
