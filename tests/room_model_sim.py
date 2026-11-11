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
# Home position of the synthetic rooms: in January the sun is down from about 15:00 to 09:00 UTC, so a
# setback night 22:00-06:00 and the 2 h response after it are quiet periods.
LAT, LON = 60.0, 0.0

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


def quiet_periods(sim: dict, gaps=()) -> list[dict]:
    """Closed quiet periods of a simulated record (the shared catalogue, at the test position)."""
    from custom_components.vtherm_smartpi.smartpi.quiet_periods import build_episodes, summary, valid_spans
    from custom_components.vtherm_smartpi.smartpi.quiet_periods.solar import validate_position

    valid = valid_spans(sim["first_t"], sim["last_t"], list(gaps))
    eps = build_episodes(validate_position(LAT, LON), sim["sp"], valid, sim["first_t"], sim["last_t"], sim["on"],
                         (sim["pub_t"], sim["pub_v"]), [])
    return [summary(e) for e in eps if e["settled"] and e["complete"]]


def window(sim: dict, day: int, gaps=()) -> dict | None:
    """Identification window of the last three quiet periods closed by 08:00 of `day` (date ordinal)."""
    from custom_components.vtherm_smartpi.smartpi.room_model.periods import windows

    t = dt.datetime.combine(dt.date.fromordinal(day), dt.time(8, 0), tzinfo=dt.timezone.utc).timestamp()
    ws = [w for w in windows(quiet_periods(sim, gaps)) if w["t_end"] <= t]
    return ws[-1] if ws else None


def snapshot(sim: dict, day: int, *, prev=None, tau_e=None, L=None, a_per_min=None, excluded=()) -> dict:
    """Snapshot of the identification job of the window closed by 08:00 of a day (date ordinal)."""
    return {"tz": "UTC", "day": day, "pub_t": sim["pub_t"], "pub_v": sim["pub_v"], "ext_t": sim["ext_t"],
            "ext_v": sim["ext_v"], "on": sim["on"], "off": sim["off"], "pw": None, "gaps": [],
            "first_t": sim["first_t"], "last_t": sim["last_t"], "sp": [list(x) for x in sim["sp"]],
            "tau_e": tau_e, "L": L, "a_per_min": a_per_min, "excluded_nights": list(excluded), "prev": prev,
            "window": window(sim, day)}


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


def prefill_history(history, p: dict, days: float, t_end: float, **kw) -> None:
    """Recorded T_ar history of `days` days of the plant under the simulator's own rule, ending at t_end."""
    data = simulate(p, days, t0=t_end - days * DAY, **kw)
    history.temp_t, history.temp_v = list(data["pub_t"]), list(data["pub_v"])
    history.ext_t, history.ext_v = list(data["ext_t"]), list(data["ext_v"])
    history.on, history.off = list(data["on"]), list(data["off"])
    history.setpoints = [tuple(x) for x in data["sp"]]
    history.first_t, history.last_t = data["first_t"], data["last_t"]


def apply_energy_balance(algo, t: float) -> dict:
    """Energy balance of the whole days before t on the recorded T_ar history, applied to the room model as the
    balance job does; returns the computation."""
    from custom_components.vtherm_smartpi.smartpi.room_model import energy_balance
    from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock

    snap = algo.tar_learning.signals_snapshot(t)
    snap["a_per_min"] = algo.est.a
    day = day_of(t)
    rec = energy_balance.daily(snap, LocalClock("UTC"), day)
    algo.room_model.apply_energy_balance(day, rec)
    return rec


def run_smartpi(p: dict, days: float, *, q: float = 0.01, cycle_min: float = 10.0, ta0: float = 19.0,
                t0: float = T0, b_online_ratio: float = 1.0, on_tick=None, restart_at: float | None = None,
                history_days: float = 0.0) -> dict:
    """Closed loop of SmartPI (room model on, learning paused) on the air-wall plant.

    The online model is seeded with the true a and b = b_online_ratio x b_eq, with reliable
    dead times. on_tick(algo, t) runs before each calculation (60 s ticks), for example to
    publish the room model. The sensor publishes the quantised air temperature at each tick.
    history_days: days of T_ar history recorded before t0 (prefill_history), so that the energy
    balance of the days before t0 can qualify (apply_energy_balance).
    restart_at: at the first cycle start from this time, the SmartPI and room model states are
    saved and restored into a new instance, as a restart of Home Assistant does (no downtime).
    Returns per tick: time, setpoint, measured air, command, holding power, integral power, and
    the restart information.
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    from custom_components.vtherm_smartpi.algo import SmartPI
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT

    full = p["c"] > 0.0
    alpha, beta, c, a = p["alpha"], p["beta"], p["c"], p["a"]
    b_eq = alpha - c if full else alpha
    gamma = c * beta if full else 0.0
    k = 0.5 * (c + alpha) if full else 0.0
    A = np.array([[-alpha, 1.0], [gamma, -beta]]) if full else np.array([[-alpha, 0.0], [0.0, -beta]])
    B = np.array([[a, alpha, 0.0], [0.0, -gamma, -k]])
    h = 10.0
    Mx = np.zeros((5, 5))
    Mx[:2, :2], Mx[:2, 2:] = A * h, B * h
    E = expm(Mx)
    phi, gam = E[:2, :2], E[:2, 2:]
    lag = int(round(p["L"] / h))
    e_tau = math.exp(-h / p["tau_e"]) if p["tau_e"] > 1.0 else 0.0
    x = np.array([ta0, c * (ta0 - outdoor(t0)) if full else 0.0])
    ue = 0.0
    ubuf = [0.0] * (lag + 1)
    clock = {"t": t0}
    out = {"t": [], "sp": [], "ta": [], "u": [], "u_ff": [], "i_power": [], "source": []}
    loop = asyncio.new_event_loop()
    cycle_s = cycle_min * 60.0
    with patch("time.time", lambda: clock["t"]), patch("time.monotonic", lambda: clock["t"] - 1.7e9):
        def new_algo():
            return SmartPI(hass=MagicMock(), cycle_min=cycle_min, minimal_activation_delay=0,
                           minimal_deactivation_delay=0, name="RoomSim", use_ff3=True, tar_learning=True,
                           room_model=True)

        algo = new_algo()
        if history_days > 0.0:
            prefill_history(algo.tar_learning.history, p, history_days, t0, q=q)
        est = algo.est
        a_min, b_min = a * 60.0, b_eq * b_online_ratio * 60.0
        for _ in range(40):
            est.a_meas_hist.append(a_min)
            est.b_meas_hist.append(b_min)
        est.a, est.b = a_min, b_min
        est.learn_ok_count_a = est.learn_ok_count_b = 40
        est.learn_ok_count = 80
        for i in range(10):
            est._b_hat_hist.append(b_min * (1.0 + 0.01 * (i - 5)))
        algo.dt_est.deadtime_heat_s = p["tau_e"] + p["L"]
        algo.dt_est.deadtime_heat_reliable = True
        algo.dt_est.deadtime_cool_s = 900.0
        algo.dt_est.deadtime_cool_reliable = True
        algo._output_initialized = True
        algo.calibration_mgr.last_calibration_time = t0
        algo.set_learning_enabled(False)
        last_pub, pub_time = None, None
        for _ in range(int(days * DAY / cycle_s)):
            on_percent = 0.0
            if restart_at is not None and clock["t"] >= restart_at and "restart" not in out:
                saved, room_saved = algo.save_state(), algo.room_model.save_state()
                out["restart"] = {"i": len(out["t"]), "state": saved, "integral": algo.integral}
                algo = new_algo()
                algo.load_state(saved)
                algo.room_model.load_state(room_saved)
            for kk in range(int(cycle_s // 60)):
                t = clock["t"]
                sp = setpoint(t)
                te = round(outdoor(t) * 10.0) / 10.0
                meas = round(round(x[0] / q) * q, 3)
                if meas != last_pub:
                    last_pub, pub_time = meas, t
                if on_tick is not None:
                    on_tick(algo, t)
                algo.calculate(target_temp=sp, current_temp=meas, ext_current_temp=te, slope=None,
                               hvac_mode=VThermHvacMode_HEAT, cycle_boundary=(kk == 0))
                algo.observe_room_model(meas, te, VThermHvacMode_HEAT, measure_id=pub_time)
                if kk == 0:
                    on_percent = algo.on_percent
                    algo._commit_applied_command(on_percent, VThermHvacMode_HEAT, None, cycle_start=True)
                out["t"].append(t)
                out["sp"].append(sp)
                out["ta"].append(float(x[0]))
                out["u"].append(algo.ctl.u_cmd)
                out["u_ff"].append(algo.ctl.u_ff)
                out["i_power"].append(algo.Ki * algo.integral)
                out["source"].append(algo._ff_holding.source.kind)
                for s in range(6):
                    tt = clock["t"]
                    u_now = 1.0 if (kk * 60 + s * 10) < on_percent * cycle_s else 0.0
                    ubuf.append(u_now)
                    u_del = ubuf.pop(0)
                    ue_mean = u_del + (ue - u_del) * (p["tau_e"] / h) * (1.0 - e_tau) if e_tau else u_del
                    ue = u_del + (ue - u_del) * e_tau
                    te_a, te_b = outdoor(tt), outdoor(tt + h)
                    x = phi @ x + gam @ np.array([ue_mean, 0.5 * (te_a + te_b), (te_b - te_a) / h])
                    clock["t"] = tt + h
            loop.run_until_complete(algo.on_cycle_completed(e_eff=on_percent, elapsed_ratio=1.0))
    loop.close()
    out["algo"] = algo
    return out
