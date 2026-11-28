"""Fit of the response of a room to a heating event, as a job that runs outside the regulation.

The reader of the measurement events (``smartpi/event_reading.py``) hands a plain-data snapshot of the record
(``smartpi/measure_event.py``) to ``run_fit``, which a worker runs: the regulation never waits for it. The model of the
whole chain is

    the emitter gives the room the applied power delayed by ``D`` and filtered by a first-order inertia ``tau``
    (computed from the beginning of the snapshot); the room follows ``dT/dt = a q - b (T - T_outdoor)``.

``D``, ``tau`` and ``b`` are searched, ``a`` and the temperature at the start of the window are linear (least squares
on the recorded room temperature). The emitter lag of a fit is ``L = D + tau``.

A snapshot holds ``power``, ``room`` and ``ext`` as lists of ``[seconds from the origin, value]`` change points (the
power and the outdoor temperature are held between points, the room temperature too), ``cooling`` (the room and
outdoor values are mirrored), ``start`` and ``end`` (the window, seconds from the origin); the power list ends with
the point of ``end``. numpy and scipy are imported when the first fit runs.
"""
from __future__ import annotations

import time
from typing import Any

from . import job_abort
from .sensor_step import SENSOR_STEP_FLOOR_C

READING_AGREE_REL = 0.20

# Fit bounds and grid
FIT_D_MAX_S = 5400.0
FIT_TAU_MIN_S, FIT_TAU_MAX_S = 20.0, 14400.0
FIT_B_MAX_PER_S = 0.003 / 60.0
FIT_GRID_S = 30.0
FIT_GRID_LONG_S = 60.0           # grid of a window longer than FIT_LONG_WINDOW_S
FIT_LONG_WINDOW_S = 12.0 * 3600.0
FIT_MIN_SAMPLES = 20

_NUM: dict[str, Any] = {}


def _numeric() -> dict[str, Any]:
    """numpy and scipy, imported at the first fit."""
    if not _NUM:
        import numpy as np  # pylint: disable=import-outside-toplevel
        from scipy.optimize import minimize, minimize_scalar  # pylint: disable=import-outside-toplevel
        from scipy.signal import lfilter  # pylint: disable=import-outside-toplevel

        _NUM.update(np=np, minimize=minimize, minimize_scalar=minimize_scalar, lfilter=lfilter)
    return _NUM


class Series:
    """A record on regular grids: applied power (mean over a cell), outdoor (held, at the middle of the cell), room (held).

    Times are seconds from the beginning of the record. ``on`` is the power applied from each time of ``gt`` on
    (a later duplicate of a time wins); ``st``/``sv`` the times and values of the room temperature, ``ext_t``/
    ``ext_v`` those of the outdoor temperature (already mirrored for a cooling reading).
    """

    def __init__(self, gt, on, st, sv, ext_t, ext_v) -> None:
        np = _numeric()["np"]
        self.gt = np.asarray(gt, float)
        self.on = np.asarray(on, float)
        self.st = np.asarray(st, float)
        self.sv = np.asarray(sv, float)
        self.ext_t = np.asarray(ext_t, float)
        self.ext_v = np.asarray(ext_v, float)
        self.t_end = float(self.gt[-1])
        dtg = np.diff(self.gt)
        self.cum = np.concatenate([[0.0], np.cumsum(self.on[:-1] * dtg)])
        self._grids: dict[float, tuple] = {}

    def _cumulated(self, t):
        np = _numeric()["np"]
        t = np.clip(t, self.gt[0], self.gt[-1])
        i = np.clip(np.searchsorted(self.gt, t, side="right") - 1, 0, len(self.gt) - 1)
        return self.cum[i] + self.on[i] * (t - self.gt[i])

    def grid(self, dt: float) -> tuple:
        """(grid times, cell middles, mean power per cell, outdoor per cell, room at the grid times)."""
        if dt in self._grids:
            return self._grids[dt]
        np = _numeric()["np"]
        k_max = int(self.t_end // dt)
        tg = np.arange(k_max + 1) * dt
        ubar = np.diff(self._cumulated(tg)) / dt
        cen = tg[:-1] + dt / 2
        ext = self.ext_v[np.clip(np.searchsorted(self.ext_t, cen, side="right") - 1, 0, None)]
        k = np.searchsorted(self.st, tg, side="right") - 1
        sens = np.where(k >= 0, self.sv[np.clip(k, 0, None)], np.nan)
        self._grids[dt] = (tg, cen, ubar, ext, sens)
        return self._grids[dt]


def _basis(series: Series, dt: float, k0: int, k1: int, delay: float, tau: float, b: float):
    """Responses on the grid samples k0..k1: to the power (a = 1), to the start temperature, and the outdoor offset."""
    n = _numeric()
    np, lfilter = n["np"], n["lfilter"]
    _tg, cen, ubar, ext, _sens = series.grid(dt)
    delayed = np.interp(cen[:k1] - delay, cen[:k1], ubar[:k1], left=0.0)
    alpha = np.exp(-dt / tau)
    q = np.concatenate([[0.0], lfilter([1.0 - alpha], [1.0, -alpha], delayed)])
    qm = 0.5 * (q[:-1] + q[1:])[k0:k1]
    decay = np.exp(-b * dt)
    w = dt if b < 1e-12 else (1.0 - decay) / b
    g = np.concatenate([[0.0], lfilter([w], [1.0, -decay], qm)])
    h = np.concatenate([[0.0], lfilter([w * b], [1.0, -decay], ext[k0:k1])])
    e = decay ** np.arange(k1 - k0 + 1)
    return g, e, h


def _residual(series: Series, dt: float, k0: int, k1: int, delay: float, tau: float, b: float, y, valid):
    """(sum of squared residuals, a, start temperature, samples) for D, tau, b; a >= 0 and the start by projection."""
    g, e, h = _basis(series, dt, k0, k1, delay, tau, b)
    yv = (y - h)[valid]
    g, e = g[valid], e[valid]
    sgg, sge, see = g @ g, g @ e, e @ e
    sgy, sey = g @ yv, e @ yv
    det = sgg * see - sge * sge
    if det <= 1e-18 * max(sgg * see, 1e-30):
        a, t0 = 0.0, sey / see
    else:
        a = (sgy * see - sey * sge) / det
        t0 = (sey * sgg - sgy * sge) / det
    if a < 0:
        a, t0 = 0.0, sey / see
    r = yv - a * g - t0 * e
    return float(r @ r), a, t0, int(valid.sum())


_D0 = (0.0, 300.0, 900.0, 1800.0, 3600.0)
_TAU0 = (40.0, 150.0, 500.0, 1500.0, 5000.0, 12000.0)
_B0 = (0.0, 0.05, 0.2, 0.5)       # shares of FIT_B_MAX_PER_S
_N_START = 5


def fit_window(series: Series, t0: float, t1: float) -> dict[str, Any] | None:
    """Output-error fit on the room samples of [t0, t1] (grid of 30 s, of 60 s when the window is over 12 h).

    Returns ``a_per_min`` (heating rate at full power), ``D_s``, ``tau_s``, ``lag_s`` (``D + tau``), ``b_per_min``,
    the residual and the flags of the parameters on a bound; None when the window holds too few samples. Each
    evaluation of the cost checks for an abort (``job_abort``).
    """
    n = _numeric()
    np, minimize = n["np"], n["minimize"]
    dt = FIT_GRID_LONG_S if (t1 - t0) > FIT_LONG_WINDOW_S + 1e-6 else FIT_GRID_S
    tg = series.grid(dt)[0]
    k0 = int(np.ceil(t0 / dt - 1e-9))
    k1 = min(int(np.floor(t1 / dt + 1e-9)), len(tg) - 1)
    y = series.grid(dt)[4][k0:k1 + 1]
    valid = ~np.isnan(y)
    if valid.sum() < FIT_MIN_SAMPLES or k1 - k0 < 10:
        return None
    log_tau_lo, log_tau_hi = np.log(FIT_TAU_MIN_S), np.log(FIT_TAU_MAX_S)

    def cost(x):
        job_abort.check()
        delay, log_tau, xb = x
        if delay < 0 or delay > FIT_D_MAX_S or log_tau < log_tau_lo or log_tau > log_tau_hi or xb < 0 or xb > 1:
            return 1e9
        return _residual(series, dt, k0, k1, delay, float(np.exp(log_tau)), xb * FIT_B_MAX_PER_S, y, valid)[0]

    pts = sorted(
        (cost([d, np.log(t), xb]), d, t, xb) for d in _D0 for t in _TAU0 for xb in _B0
    )
    starts, seen = [], []
    for s, d, t, xb in pts:
        if all(abs(d - d2) > 400 or abs(np.log(t / t2)) > 1.0 or abs(xb - x2) > 0.15 for d2, t2, x2 in seen):
            seen.append((d, t, xb))
            starts.append((s, d, t, xb))
        if len(starts) >= _N_START:
            break
    lo = [0, log_tau_lo, 0]
    hi = [FIT_D_MAX_S, log_tau_hi, 1]
    steps = ([400.0, 0, 0], [0, 0.7, 0], [0, 0, 0.15])
    best = None
    for _s, d, t, xb in starts:
        x0 = np.array([d, np.log(t), xb])
        simplex = np.array([x0, x0 + steps[0], x0 + steps[1], x0 + steps[2]])
        simplex = np.clip(simplex, lo, hi)
        for j in range(1, 4):                        # keep a non-degenerate simplex when x0 sits on a bound
            if np.allclose(simplex[j], simplex[0]):
                simplex[j] = np.clip(x0 - steps[j - 1], lo, hi)
        out = minimize(cost, x0, method="Nelder-Mead",
                       options={"xatol": 1.0, "fatol": 1e-10, "maxiter": 500, "initial_simplex": simplex})
        if best is None or out.fun < best[0]:
            best = (float(out.fun), out.x)
    x = best[1]
    delay = float(np.clip(x[0], 0, FIT_D_MAX_S))
    tau = float(np.exp(np.clip(x[1], log_tau_lo, log_tau_hi)))
    b = float(np.clip(x[2], 0, 1)) * FIT_B_MAX_PER_S
    ssr, a, _start, count = _residual(series, dt, k0, k1, delay, tau, b, y, valid)
    return {
        "a_per_min": float(a) * 60.0, "D_s": delay, "tau_s": tau, "lag_s": delay + tau, "b_per_min": b * 60.0,
        "rms_c": float(np.sqrt(ssr / count)), "n": int(count), "dt": dt,
        "flags": {"D_lo": bool(delay <= dt / 2), "D_hi": bool(delay >= FIT_D_MAX_S - dt / 2),
                  "tau_lo": bool(tau <= FIT_TAU_MIN_S * 1.02), "tau_hi": bool(tau >= FIT_TAU_MAX_S / 1.02),
                  "b_lo": bool(b <= 0.01 * FIT_B_MAX_PER_S), "b_hi": bool(b >= 0.99 * FIT_B_MAX_PER_S)},
    }


def readings_agree(a1: float, lag1: float, a2: float, lag2: float) -> bool:
    """Two readings agree when their ``a`` and their lags each differ by at most 20 % of the larger."""
    if min(a1, a2, lag1, lag2) <= 0.0:
        return False
    return bool(
        abs(a1 - a2) <= READING_AGREE_REL * max(a1, a2) + 1e-12
        and abs(lag1 - lag2) <= READING_AGREE_REL * max(lag1, lag2) + 1e-12
    )


def run_fit(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Fit the window of a snapshot (see the module documentation); None when it holds too little to fit.

    Runs in a worker: it reads nothing but ``snap``. The result carries the seconds the fit took.
    """
    started = time.perf_counter()
    if len(snap["power"]) < 2 or not snap["room"] or not snap["ext"]:
        return None
    sign = -1.0 if snap["cooling"] else 1.0
    gt, on = [p[0] for p in snap["power"]], [p[1] for p in snap["power"]]
    series = Series(gt, on, [p[0] for p in snap["room"]], [sign * p[1] for p in snap["room"]],
                    [p[0] for p in snap["ext"]], [sign * p[1] for p in snap["ext"]])
    result = fit_window(series, snap["start"], snap["end"])
    if result is not None:
        result["seconds"] = round(time.perf_counter() - started, 3)
    return result


# Fit of the response model (smartpi/response_model.py): profile grid of the dead time and the response time constant,
# residuals at the level crossings of the room temperature.
PROFILE_THETA_S = tuple(float(t) for t in range(0, 1201, 60)) + tuple(float(t) for t in range(1500, 5401, 300))
PROFILE_TAU_S = tuple(60.0 * 240.0 ** (k / 23.0) for k in range(24))    # 60 s .. 14400 s, log grid
PROFILE_CHI2 = 3.84            # chi-square level (one parameter, 95 %) of the confidence region
FIT_MIN_CROSSINGS = 8
FIT_MIN_RISE_STEPS = 3.0       # smallest rise of the room, in sensor steps
FIT_MIN_RISE_C = 0.10          # smallest rise of the room, in degrees: a rise of a few steps of a fine sensor fits nonsense
FIT_MAX_RMS_STEPS = 2.0        # largest distance of the fit to the room temperature (rms), in sensor steps
SPLIT_MIN_S = 300.0            # the dead time is told apart when its range is at most max(this, SPLIT_REL x S)
SPLIT_REL = 0.5

REJECT_CROSSINGS = "too few temperature changes"
REJECT_RISE = "temperature rise too small"
REJECT_NO_HEAT = "no heating seen"
REJECT_DEAD_TIME = "dead time at its limit"
REJECT_TIME_CONSTANT = "response time constant at its limit"
REJECT_RMS = "fit too far from the room temperature"


def crossings(st, sv, t0: float, t1: float) -> tuple[list[float], list[float], float]:
    """Level crossings of the room in (t0, t1]: (times, values, sensor step).

    A sensor publishing on change reports a one-step change (at most 1.5 sensor steps) when the temperature crosses the
    boundary of two levels: the value there is the midpoint of the two levels. A change of several steps comes from a
    sampling instant: the value is the new one. The sensor step is the smallest change of the series.
    """
    steps = [abs(b - a) for a, b in zip(sv, sv[1:]) if b != a]
    step = min(steps) if steps else 0.0
    times, values = [], []
    for i in range(1, len(st)):
        prev, v = sv[i - 1], sv[i]
        if v == prev or not t0 < st[i] <= t1:
            continue
        times.append(float(st[i]))
        values.append(0.5 * (v + prev) if abs(v - prev) <= 1.5 * step + 1e-9 else float(v))
    return times, values, step


def _emitter(series: Series, dt: float, k0: int, k1: int, delay: float, tau: float):
    """Power delivered to the room on the cells k0..k1-1 (a = 1): the applied power delayed by ``delay`` and filtered by
    a first-order inertia ``tau``."""
    n = _numeric()
    np, lfilter = n["np"], n["lfilter"]
    _tg, cen, ubar, _ext, _sens = series.grid(dt)
    delayed = np.interp(cen[:k1] - delay, cen[:k1], ubar[:k1], left=0.0)
    alpha = np.exp(-dt / tau)
    q = np.concatenate([[0.0], lfilter([1.0 - alpha], [1.0, -alpha], delayed)])
    return 0.5 * (q[:-1] + q[1:])[k0:k1]


class _Crossings:
    """The crossings of a window and the linear least squares of ``a`` and the start temperature at them."""

    def __init__(self, series: Series, dt: float, k0: int, k1: int, tc, yc) -> None:
        np = _numeric()["np"]
        self.ext = series.grid(dt)[3][k0:k1]
        self.dt, self.n = dt, k1 - k0
        x = (np.asarray(tc, float) - k0 * dt) / dt
        self.j = np.clip(np.floor(x).astype(int), 0, self.n - 1)
        self.w = x - self.j
        self.y = np.asarray(yc, float)

    def _at(self, v):
        return v[self.j] * (1.0 - self.w) + v[self.j + 1] * self.w

    def cost(self, qm, b: float) -> tuple[float, float, float]:
        """(sum of squared residuals, a, start temperature) for the delivered power ``qm`` and the loss ``b``."""
        n = _numeric()
        np, lfilter = n["np"], n["lfilter"]
        decay = np.exp(-b * self.dt)
        w = self.dt if b < 1e-12 else (1.0 - decay) / b
        g = self._at(np.concatenate([[0.0], lfilter([w], [1.0, -decay], qm)]))
        h = self._at(np.concatenate([[0.0], lfilter([w * b], [1.0, -decay], self.ext)]))
        e = self._at(decay ** np.arange(self.n + 1))
        yv = self.y - h
        sgg, sge, see, sgy, sey = g @ g, g @ e, e @ e, g @ yv, e @ yv
        det = sgg * see - sge * sge
        a, t0 = 0.0, sey / see
        if det > 1e-18 * max(sgg * see, 1e-30):
            a, t0 = (sgy * see - sey * sge) / det, (sey * sgg - sgy * sge) / det
            if a < 0:
                a, t0 = 0.0, sey / see
        r = yv - a * g - t0 * e
        return float(r @ r), float(a), float(t0)


def fit_response(
    series: Series, t0: float, t1: float, t_heat: float, model: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """Fit of the dead time ``theta`` and the response time constant ``tau_e`` on the level crossings of [t0, t1].

    Profile: on the grid ``PROFILE_THETA_S`` x ``PROFILE_TAU_S`` the loss ``b`` is searched in [0, ``FIT_B_MAX_PER_S``]
    (bounded scalar search), ``a`` and the start temperature are linear; the best cell is polished by Nelder-Mead
    within the bounds of the grid. The residuals of successive crossings are correlated over about S = theta + tau_e:
    the confidence region holds the cells whose cost is at most the minimum x (1 + ``PROFILE_CHI2`` / n), n = the
    window after the heating step divided by S (1 at least). ``t_heat`` is the heating step (the rise is counted
    from it). None when the window is too short to fit; a result with ``reject`` set (plain words) when it is not
    usable: a rise under ``FIT_MIN_RISE_STEPS`` sensor steps or ``FIT_MIN_RISE_C``, no heating, theta or tau_e at its upper bound, or an
    rms over ``FIT_MAX_RMS_STEPS`` sensor steps (``SENSOR_STEP_FLOOR_C`` at least). ``tau_e_lower_limit`` marks a
    tau_e on the lower bound of the grid (a fast emitter: its true value may be shorter). With ``model`` (theta,
    tau_e of the model in force), ``prediction_rms_c`` is the rms of that model on the window, ``a``, ``b`` and the
    start temperature solved as for a cell of the grid.
    """
    n = _numeric()
    np, minimize, minimize_scalar = n["np"], n["minimize"], n["minimize_scalar"]
    dt = FIT_GRID_LONG_S if (t1 - t0) > FIT_LONG_WINDOW_S + 1e-6 else FIT_GRID_S
    tg = series.grid(dt)[0]
    k0 = int(np.ceil(t0 / dt - 1e-9))
    k1 = min(int(np.floor(t1 / dt + 1e-9)), len(tg) - 1)
    if k1 - k0 < 10:
        return None
    tc, yc, step = crossings(list(series.st), list(series.sv), tg[k0], tg[k1])
    held = series.sv[max(int(np.searchsorted(series.st, t_heat, side="right")) - 1, 0)]
    rise = max([v for t, v in zip(series.st, series.sv) if t_heat < t <= t1] + [held]) - held
    base = {"crossings": len(tc), "step_c": step, "rise_c": float(rise), "dt": dt}
    if len(tc) < FIT_MIN_CROSSINGS:
        return dict(base, reject=REJECT_CROSSINGS, lag_s=max(t1 - t_heat, dt))
    data = _Crossings(series, dt, k0, k1, tc, yc)

    def profile(delay: float, tau: float) -> tuple[float, float]:
        qm = _emitter(series, dt, k0, k1, delay, tau)
        out = minimize_scalar(lambda xb: (job_abort.check(), data.cost(qm, xb * FIT_B_MAX_PER_S)[0])[1],
                              bounds=(0.0, 1.0), method="bounded", options={"xatol": 0.01})
        return float(out.fun), float(out.x)

    cells = [(d, t) + profile(d, t) for d in PROFILE_THETA_S for t in PROFILE_TAU_S]
    best = min(cells, key=lambda c: c[2])
    log_lo, log_hi = np.log(PROFILE_TAU_S[0]), np.log(PROFILE_TAU_S[-1])

    def cost(x):
        job_abort.check()
        delay, log_tau, xb = x
        if delay < 0 or delay > FIT_D_MAX_S or log_tau < log_lo or log_tau > log_hi or xb < 0 or xb > 1:
            return 1e9
        return data.cost(_emitter(series, dt, k0, k1, delay, float(np.exp(log_tau))), xb * FIT_B_MAX_PER_S)[0]

    x0 = np.array([best[0], np.log(best[1]), best[3]])
    steps = np.array([[60.0, 0, 0], [0, 0.25, 0], [0, 0, 0.1]])
    simplex = np.array([x0] + [np.clip(x0 + s, [0, log_lo, 0], [FIT_D_MAX_S, log_hi, 1]) for s in steps])
    for j in range(1, 4):                            # keep a non-degenerate simplex when x0 sits on a bound
        if np.allclose(simplex[j], simplex[0]):
            simplex[j] = np.clip(x0 - steps[j - 1], [0, log_lo, 0], [FIT_D_MAX_S, log_hi, 1])
    out = minimize(cost, x0, method="Nelder-Mead",
                   options={"xatol": 1.0, "fatol": 1e-10, "maxiter": 300, "initial_simplex": simplex})
    x = out.x if out.fun < best[2] else x0
    theta = float(np.clip(x[0], 0.0, FIT_D_MAX_S))
    tau = float(np.exp(np.clip(x[1], log_lo, log_hi)))
    b = float(np.clip(x[2], 0.0, 1.0)) * FIT_B_MAX_PER_S
    ssr, a, _start = data.cost(_emitter(series, dt, k0, k1, theta, tau), b)
    s = theta + tau
    limit = min(ssr, best[2]) * (1.0 + PROFILE_CHI2 / max((tg[k1] - t_heat) / s, 1.0))
    region = [(d, d + t) for d, t, c, _xb in cells if c <= limit] + [(theta, theta + tau)]
    theta_range = [min(r[0] for r in region), max(r[0] for r in region)]
    s_range = [min(r[1] for r in region), max(r[1] for r in region)]
    rms = float(np.sqrt(ssr / len(tc)))
    reject = (
        REJECT_RISE if rise < max(FIT_MIN_RISE_STEPS * step, FIT_MIN_RISE_C) - 1e-9
        else REJECT_NO_HEAT if a <= 0.0
        else REJECT_DEAD_TIME if theta >= FIT_D_MAX_S - dt / 2
        else REJECT_TIME_CONSTANT if tau >= PROFILE_TAU_S[-1] / 1.02
        else REJECT_RMS if rms > FIT_MAX_RMS_STEPS * max(step, SENSOR_STEP_FLOOR_C) + 1e-9
        else None
    )
    return dict(
        base, reject=reject, theta_s=theta, tau_e_s=tau, s_s=s, lag_s=s, theta_range_s=theta_range,
        s_range_s=s_range, split_ok=bool(theta_range[1] - theta_range[0] <= max(SPLIT_MIN_S, SPLIT_REL * s)),
        a_per_min=a * 60.0, b_per_min=b * 60.0, rms_c=rms, tau_e_lower_limit=bool(tau <= PROFILE_TAU_S[0] * 1.02),
        prediction_rms_c=None if model is None else float(
            np.sqrt(profile(max(float(model[0]), 0.0), max(float(model[1]), 1.0))[0] / len(tc))),
    )


def run_response_fit(snap: dict[str, Any]) -> dict[str, Any] | None:
    """``fit_response`` on the window of a snapshot (``run_fit``'s form plus ``heat``, the heating step in seconds from
    the origin, and ``model``, theta and tau_e of the model in force or None). Runs in a worker; the result carries the seconds the fit took."""
    started = time.perf_counter()
    if len(snap["power"]) < 2 or not snap["room"] or not snap["ext"]:
        return None
    sign = -1.0 if snap["cooling"] else 1.0
    series = Series([p[0] for p in snap["power"]], [p[1] for p in snap["power"]], [p[0] for p in snap["room"]],
                    [sign * p[1] for p in snap["room"]], [p[0] for p in snap["ext"]], [sign * p[1] for p in snap["ext"]])
    result = fit_response(series, snap["start"], snap["end"], snap["heat"], snap.get("model"))
    if result is not None:
        result["seconds"] = round(time.perf_counter() - started, 3)
    return result
