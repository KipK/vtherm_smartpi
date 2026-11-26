"""Signal primitives of the room model identification (numpy): temperature anchors, heater power in bins
(from PWM heater edges or from a valve power level timeline), sensor step.

Units: times in s, temperatures in C.
"""
from __future__ import annotations

import numpy as np


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


def q_obs(pub_v) -> float | None:
    """Smallest absolute change > 1e-3 C between consecutive published values (sensor step)."""
    v = np.asarray(pub_v, dtype=float)
    dv = np.abs(np.diff(v))
    dv = dv[dv > 1e-3]
    return float(dv.min()) if len(dv) else None
