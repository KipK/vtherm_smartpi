"""Step-response fit of the emitter delay T_ar (numpy).

Model: around a baseline T_b, the deviation y = T - T_b obeys
y = a (g_room * h_T * du), with du the applied power minus the baseline power,
h_T the emitter shape (first order of time constant T: "fo"; delay 0.3 T then
first order 0.7 T: "d03fo"; pure delay: "delay") and g_room the impulse
response of the room seen from the air:
  'fo'    : exp(-b t)                                (one time constant)
  '2mode' : c1 exp(-lam1 t) + c2 exp(-lam2 t)        (air and walls)
The fit is linear in a: for each T of the grid a = <z, y>/<z, z>, and T
minimises the residual.
"""
from __future__ import annotations

import math

import numpy as np

from .const import FIT_DT

DT = FIT_DT
T_GRID = np.concatenate([np.arange(0.0, 600.0, 10.0), np.arange(600.0, 7201.0, 30.0)])


def kernels(shape: str, n: int) -> np.ndarray:
    """Impulse responses of h_T (rows: T_GRID) on the DT grid, each summing to 1."""
    t = np.arange(n) * DT
    K = np.zeros((len(T_GRID), n))
    for i, T in enumerate(T_GRID):
        if T <= 0:
            K[i, 0] = 1.0
            continue
        if shape == "delay":
            j = int(round(T / DT))
            if j < n:
                K[i, j] = 1.0
            continue
        L, tau = (0.0, T) if shape == "fo" else (0.3 * T, 0.7 * T)
        k = np.exp(-np.maximum(t - L, 0.0) / tau) - np.exp(-np.maximum(t + DT - L, 0.0) / tau)
        k[t + DT <= L] = 0.0
        s = k.sum()
        K[i] = k / s if s > 0 else k
    return K


def room_ir(loss: dict, n: int) -> np.ndarray:
    """Room impulse response seen from the air (per unit a), sampled on the DT grid, times DT."""
    k = np.arange(n) * DT
    if loss["kind"] == "fo":
        return np.exp(-loss["b_per_min"] / 60.0 * k) * DT
    l1, l2, k3 = loss["lam1"], loss["lam2"], loss["k3"]
    c1 = (k3 - l1) / (l2 - l1)
    c2 = (k3 - l2) / (l1 - l2)
    return (c1 * np.exp(-l1 * k) + c2 * np.exp(-l2 * k)) * DT


class KernelCache:
    """FFT of the emitter kernels, kept for the duration of one computation."""

    def __init__(self) -> None:
        self._items: dict = {}

    def get(self, shape: str, n: int):
        key = (shape, n)
        if key not in self._items:
            nfft = 1 << int(math.ceil(math.log2(3 * n)))
            self._items[key] = (nfft, np.fft.rfft(kernels(shape, n), nfft, axis=1))
        return self._items[key]


def z_all(du: np.ndarray, loss: dict, shape: str, cache: KernelCache) -> np.ndarray:
    """Model columns Z[T] = h_T * g_room * du for every T of the grid."""
    n = len(du)
    nfft, FK = cache.get(shape, n)
    F = np.fft.rfft(du, nfft) * np.fft.rfft(room_ir(loss, n), nfft)
    return np.fft.irfft(FK * F[None, :], nfft, axis=1)[:, :n]


def fit_T(Z: np.ndarray, idx: np.ndarray, y: np.ndarray) -> dict:
    """Best T of the grid (profile over a, a > 0) for the anchors at grid indices idx."""
    Zs = Z[:, idx]
    zz = np.einsum("ij,ij->i", Zs, Zs)
    ok = zz > 1e-12
    a = np.where(ok, (Zs @ y) / np.where(ok, zz, 1.0), 0.0)
    r = Zs * a[:, None] - y[None, :]
    sse = np.where(ok & (a > 0), np.einsum("ij,ij->i", r, r), np.inf)
    k = int(np.argmin(sse))
    if not np.isfinite(sse[k]):
        return {"T": None, "a_per_min": None, "rms": None, "edge": True}
    return {"T": float(T_GRID[k]), "a_per_min": float(a[k]) * 60.0, "rms": float(math.sqrt(sse[k] / len(y))),
            "edge": bool(k == 0 or k == len(T_GRID) - 1)}


def fit_windows(Z, idx_all, y, ta, ends: dict, windows) -> dict:
    """fit_T on each window (anchors up to the window end)."""
    res = {}
    for wn in windows:
        sel = ta <= ends[wn]
        if sel.sum() < 5:
            res[wn] = {"T": None, "reason": "few anchors"}
            continue
        res[wn] = fit_T(Z, idx_all[sel], y[sel])
    return res


def window_rise(ta, ya, ends: dict, windows, end_rise_s: float) -> dict:
    """Mean deviation of the anchors of the last end_rise_s of each window."""
    rise = {}
    for wn in windows:
        sel = (ta <= ends[wn]) & (ta >= ends[wn] - end_rise_s)
        if sel.sum() == 0:
            sel = np.nonzero(ta <= ends[wn])[0][-1:]
        rise[wn] = float(np.mean(ya[sel])) if np.size(ya[sel]) else None
    return rise
