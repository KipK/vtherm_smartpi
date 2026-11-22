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
        from scipy.optimize import minimize  # pylint: disable=import-outside-toplevel
        from scipy.signal import lfilter  # pylint: disable=import-outside-toplevel

        _NUM.update(np=np, minimize=minimize, lfilter=lfilter)
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
