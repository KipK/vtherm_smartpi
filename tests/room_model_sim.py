"""Synthetic room following the room model exactly (room model tests).

The plant is the air-wall model with a first-order emitter and a pure delay,
heated by PWM cycles of a simple proportional rule around a day/night
setpoint, under a sinusoidal outdoor temperature. An open window (optional)
stops the heater and adds an air loss to the outside. The sensor publishes on each
change of its quantised value (and a heartbeat). The output is the recorded
history in the form of the T_ar learning history.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
from scipy.linalg import expm

H = 3600.0
DAY = 86400.0
T0 = dt.datetime(2026, 1, 5, 0, 0, tzinfo=dt.timezone.utc).timestamp()  # local midnight (UTC)

# Mass room (per second): a, alpha, beta, c; emitter tau_e and delay L (s)
MASS = {"a": 0.0012, "alpha": 1.0 / (2.5 * H), "beta": 1.0 / (80.0 * H), "c": 0.65 / (2.5 * H),
        "tau_e": 600.0, "L": 120.0}
NO_MASS = {"a": 0.0012, "alpha": 1.0 / (30.0 * H), "beta": 1.0 / H, "c": 0.0, "tau_e": 600.0, "L": 120.0}


def setpoint(t: float) -> float:
    h = ((t - T0) / H) % 24.0
    return 20.0 if 6.0 <= h < 22.0 else 17.0


def outdoor(t: float) -> float:
    return 4.0 + 3.0 * math.sin(2.0 * math.pi * ((t - T0) / DAY - 0.375))


def simulate(p: dict, days: float, *, q: float = 0.01, noise: float = 0.0, seed: int = 1, gain=None,
             cycle_s: float = 600.0, ta0: float = 19.0, t0: float = T0, window=None) -> dict:
    """Closed loop on 10 s steps; returns the history lists and the true trajectories (60 s).

    window: (start, end, extra air loss rate per second): heater off and extra loss over [start, end).
    """
    rng = np.random.default_rng(seed)
    full = p["c"] > 0.0
    alpha, beta, c, a = p["alpha"], p["beta"], p["c"], p["a"]
    gamma = c * beta if full else 0.0
    k = 0.5 * (c + alpha) if full else 0.0
    A = np.array([[-alpha, 1.0], [gamma, -beta]]) if full else np.array([[-alpha, 0.0], [0.0, -beta]])
    B = np.array([[a, alpha, 0.0, 1.0], [0.0, -gamma, -k, 0.0]])
    h = 10.0
    Mx = np.zeros((6, 6))
    Mx[:2, :2], Mx[:2, 2:] = A * h, B * h
    E = expm(Mx)
    phi, gam = E[:2, :2], E[:2, 2:]
    phi_w, gam_w = phi, gam
    if window is not None:
        Mw = Mx.copy()
        Mw[0, 0] -= window[2] * h
        Mw[0, 3] += window[2] * h
        Ew = expm(Mw)
        phi_w, gam_w = Ew[:2, :2], Ew[:2, 2:]
    n = int(days * DAY / h)
    lag = int(round(p["L"] / h))
    e_tau = math.exp(-h / p["tau_e"]) if p["tau_e"] > 1.0 else 0.0
    te0 = outdoor(t0)
    x = np.array([ta0, c * (ta0 - te0) if full else 0.0])
    ue = 0.0
    ubuf = [0.0] * (lag + 1)
    on, off, pub_t, pub_v, ext_t, ext_v, sp_ev = [], [], [], [], [], [], []
    last_pub, last_pub_t = None, -1e9
    duty = 0.0
    heater = False
    cyc_start = t0
    tru_t, tru_a, tru_w = [], [], []
    cycles = []
    for i in range(n):
        t = t0 + i * h
        sp = setpoint(t)
        in_win = window is not None and window[0] <= t < window[1]
        if not sp_ev or sp_ev[-1][1] != sp:
            sp_ev.append((t, sp))
        if i % int(cycle_s / h) == 0:
            meas = last_pub if last_pub is not None else x[0]
            te_now = outdoor(t)
            ff = (alpha - c) / a * (sp - te_now)
            duty = 0.0 if in_win else min(1.0, max(0.0, ff + 0.8 * (sp - meas)))
            cyc_start = t
            cycles.append((t, duty))
        t_off = cyc_start + duty * cycle_s
        u_now = 0.0 if in_win else min(1.0, max(0.0, (t_off - t) / h))
        if u_now > 0 and not heater:
            on.append(t)
            heater = True
        if heater and (in_win or t_off < t + h - 1e-9):
            off.append(t if in_win else max(t, t_off))
            heater = False
        ubuf.append(u_now)
        u_del = ubuf.pop(0)
        ue_mean = u_del + (ue - u_del) * (p["tau_e"] / h) * (1.0 - e_tau) if e_tau else u_del
        ue = u_del + (ue - u_del) * e_tau
        te_a, te_b = outdoor(t), outdoor(t + h)
        dist = gain(t) if gain is not None else 0.0
        v = np.array([ue_mean, 0.5 * (te_a + te_b), (te_b - te_a) / h, dist])
        x = (phi_w @ x + gam_w @ v) if in_win else (phi @ x + gam @ v)
        t1 = t + h
        val = round((x[0] + rng.uniform(-noise, noise)) / q) * q
        val = round(val, 3)
        if last_pub is None or (val != last_pub and t1 - last_pub_t >= 30.0) or t1 - last_pub_t >= 600.0:
            if last_pub is None or val != last_pub:
                pub_t.append(t1)
                pub_v.append(val)
            last_pub, last_pub_t = val, t1
        te_q = round(te_b * 10.0) / 10.0
        if not ext_v or ext_v[-1] != te_q:
            ext_t.append(t1)
            ext_v.append(te_q)
        if i % 6 == 5:
            tru_t.append(t1)
            tru_a.append(x[0])
            tru_w.append(x[1])
    return {"on": on, "off": off, "pub_t": pub_t, "pub_v": pub_v, "ext_t": ext_t, "ext_v": ext_v, "sp": sp_ev,
            "first_t": t0, "last_t": t0 + n * h, "cycles": cycles, "truth": (np.array(tru_t), np.array(tru_a), np.array(tru_w))}


def snapshot(sim: dict, day: int, *, prev=None, tau_e=None, L=None, a_per_min=None, excluded=()) -> dict:
    """Snapshot of the identification job of a day (date ordinal)."""
    return {"tz": "UTC", "day": day, "pub_t": sim["pub_t"], "pub_v": sim["pub_v"], "ext_t": sim["ext_t"],
            "ext_v": sim["ext_v"], "on": sim["on"], "off": sim["off"], "pw": None, "gaps": [],
            "first_t": sim["first_t"], "last_t": sim["last_t"], "sp": [list(x) for x in sim["sp"]],
            "tau_e": tau_e, "L": L, "a_per_min": a_per_min, "excluded_nights": list(excluded), "prev": prev}


def day_of(t: float) -> int:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).date().toordinal()


def replay(sim: dict, feed, t_from: float, t_to: float, step: float = 60.0) -> None:
    """Call feed(t, kind, payload) for the applied cycles and the 60 s ticks of [t_from, t_to), in time order.

    kind "cycle": payload (power); kind "tick": payload (published temperature, its publication time, outdoor).
    """
    pub_t, pub_v = sim["pub_t"], sim["pub_v"]
    ext_t, ext_v = sim["ext_t"], sim["ext_v"]
    cyc = [c for c in sim["cycles"] if t_from <= c[0] < t_to]
    ticks = np.arange(t_from, t_to, step)
    ev = [(c[0], 0, c[1]) for c in cyc] + [(float(t), 1, None) for t in ticks]
    ev.sort(key=lambda e: (e[0], e[1]))
    ip = ie = 0
    for t, kind, pl in ev:
        if kind == 0:
            feed(t, "cycle", pl)
            continue
        while ip < len(pub_t) and pub_t[ip] <= t:
            ip += 1
        while ie < len(ext_t) and ext_t[ie] <= t:
            ie += 1
        if ip == 0 or ie == 0:
            continue
        feed(t, "tick", (pub_v[ip - 1], pub_t[ip - 1], ext_v[ie - 1]))
