"""Forecast errors of the room model (pure Python).

Every hour the observer state is copied as a forecast origin. Each origin is
then propagated open loop (no correction, disturbance held) with the command
actually applied and the measured outdoor temperature. When an origin reaches
a horizon its predicted air temperature is compared with the latest measured
value; the error of keeping the temperature unchanged since the origin is kept
as a reference. The last errors of each horizon give a rolling root mean
square and mean.
"""
from __future__ import annotations

import math
from typing import Any

from .const import RM_HORIZONS_S, RM_SCORE_EVERY_S, RM_SCORE_KEEP


class ForecastScore:
    """Pending forecast origins and the recent errors per horizon."""

    def __init__(self) -> None:
        self.origins: list[dict[str, Any]] = []
        self.errors: list[list[float]] = [[] for _ in RM_HORIZONS_S]
        self.unchanged: list[list[float]] = [[] for _ in RM_HORIZONS_S]
        self.last_origin_t: float | None = None

    def drop_pending(self) -> None:
        """Forget the forecasts in progress (data interruption, new parameters, restart)."""
        self.origins = []

    def maybe_start(self, t: float, x: list[float], y_meas: float | None) -> None:
        if y_meas is None:
            return
        if self.last_origin_t is not None and t - self.last_origin_t < RM_SCORE_EVERY_S - 1e-6:
            return
        self.last_origin_t = t
        self.origins.append({"t0": t, "x": list(x), "y0": float(y_meas), "next": 0})

    def step(self, phi, gam, v: list[float], d_max: float) -> None:
        """Propagate every origin by one step with the applied inputs."""
        for o in self.origins:
            x = o["x"]
            nx = [
                phi[i][0] * x[0] + phi[i][1] * x[1] + phi[i][2] * x[2]
                + gam[i][0] * v[0] + gam[i][1] * v[1] + gam[i][2] * v[2]
                for i in range(3)
            ]
            nx[2] = min(d_max, max(-d_max, nx[2]))
            o["x"] = nx

    def score(self, t: float, y_meas: float | None) -> None:
        """Record the errors of the origins that reached a horizon at t."""
        if y_meas is None:
            return
        keep = []
        for o in self.origins:
            while o["next"] < len(RM_HORIZONS_S) and t - o["t0"] >= RM_HORIZONS_S[o["next"]] - 1e-6:
                k = o["next"]
                self._push(self.errors[k], o["x"][0] - y_meas)
                self._push(self.unchanged[k], o["y0"] - y_meas)
                o["next"] += 1
            if o["next"] < len(RM_HORIZONS_S):
                keep.append(o)
        self.origins = keep

    @staticmethod
    def _push(lst: list[float], e: float) -> None:
        if math.isfinite(e):
            lst.append(round(e, 4))
            del lst[:-RM_SCORE_KEEP]

    @staticmethod
    def _rms(lst: list[float]) -> float | None:
        return math.sqrt(sum(e * e for e in lst) / len(lst)) if lst else None

    def summary(self) -> dict[str, list]:
        return {
            "rmse": [self._rms(e) for e in self.errors],
            "bias": [sum(e) / len(e) if e else None for e in self.errors],
            "count": [len(e) for e in self.errors],
            "unchanged_rmse": [self._rms(e) for e in self.unchanged],
        }

    def reset(self) -> None:
        self.__init__()

    def to_dict(self) -> dict[str, Any]:
        return {"errors": self.errors, "unchanged": self.unchanged, "last_origin_t": self.last_origin_t}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ForecastScore":
        s = cls()
        if not isinstance(data, dict):
            return s
        try:
            err = [[float(x) for x in lst][-RM_SCORE_KEEP:] for lst in data.get("errors") or []]
            unc = [[float(x) for x in lst][-RM_SCORE_KEEP:] for lst in data.get("unchanged") or []]
            if len(err) == len(RM_HORIZONS_S) and len(unc) == len(RM_HORIZONS_S):
                s.errors, s.unchanged = err, unc
            lo = data.get("last_origin_t")
            s.last_origin_t = None if lo is None else float(lo)
        except (TypeError, ValueError):
            return cls()
        return s
