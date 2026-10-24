"""
Integral balance pools for a/b learning windows without a robust slope.

The 1R1C model integrated between two instants gives

    T(t2) - T(t1) = a * U - b * D,  U = int(u dt),  D = int(T_int - T_ext) dt

so the information of a window does not require a significant OLS slope: it
requires the temperature change between two raw sensor steps and the applied
power between them. Coarse or slowly published sensors rarely give a robust
slope within one heating or cooling phase; the segments of several such
windows are pooled until their quantisation-bounded uncertainty is small
enough, then submitted to the estimator as one measurement.

Both segment ends are raw steps in the response direction. A sensor
publishing on change reports such a step when the temperature crosses the
same quantisation boundary offset, so the change between the two ends is
exact; a periodically sampled sensor reports a point sample within half a
step. The uncertainty model uses half a step per end in both cases, which
only holds without sensor noise at the scale of a step: the window must be
monotone (no raw step against the response direction) and the segment must
last at least AB_BALANCE_MIN_SEGMENT_MIN, noise-limited windows being left to
the OLS slope. Collection stops at the first tick whose power
departs from the window mean, so that the actuator lag after a power change
is not credited to the previous power.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Deque

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_B_MIN_PASSIVE_GRADIENT_C,
    AB_B_OUTDOOR_TARGET_MARGIN_C,
    AB_BALANCE_DEFAULT_RESOLUTION_C,
    AB_BALANCE_MAX_REL_UNC,
    AB_BALANCE_MIN_RESOLUTION_C,
    AB_BALANCE_MIN_SEGMENT_MIN,
    AB_BALANCE_POOL_MAX_AGE_S,
    AB_BALANCE_POOL_MAX_SEGMENTS,
    U_CV_MAX,
    U_CV_MIN_MEAN,
    U_OFF_MAX,
    U_ON_MIN,
)

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode
    from .ab_estimator import ABEstimator

_LOGGER = logging.getLogger(__name__)

_STEP_EPS_C = 1e-3


@dataclass(frozen=True)
class BalanceAnchor:
    """Cumulative window integrals at one raw temperature step."""

    ts: float
    temp: float
    direction: int
    t_min: float
    u_int: float
    d_int: float
    text_int: float


@dataclass(frozen=True)
class BalanceSegment:
    """Integral balance between two raw steps of one closed window."""

    end_ts: float
    param: str
    dt_min: float
    d_temp: float
    u_int: float
    d_int: float
    text_int: float


def passive_gradient_ok(
    hvac_mode: "VThermHvacMode",
    t_int: float,
    t_ext: float,
    target_temp: float | None,
) -> bool:
    """Return whether a passive segment has a usable indoor/outdoor gradient."""
    if hvac_mode == VThermHvacMode_COOL:
        if t_ext - t_int < AB_B_MIN_PASSIVE_GRADIENT_C:
            return False
        return target_temp is None or t_ext >= target_temp + AB_B_OUTDOOR_TARGET_MARGIN_C
    if t_int - t_ext < AB_B_MIN_PASSIVE_GRADIENT_C:
        return False
    return target_temp is None or t_ext <= target_temp - AB_B_OUTDOOR_TARGET_MARGIN_C


def pooled_relative_uncertainty(
    n_segments: int, numerator: float, resolution_c: float
) -> float:
    """Return the quantisation-bounded relative uncertainty of a pool.

    Each segment carries two ends uniformly distributed within half a sensor
    step, i.e. a variance of resolution**2 / 6, independent between segments.
    """
    if n_segments <= 0 or abs(numerator) < 1e-12:
        return math.inf
    return resolution_c * math.sqrt(n_segments / 6.0) / abs(numerator)


class ABWindowBalance:
    """Collect window segments and pool them per parameter."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._anchors: list[BalanceAnchor] = []
        self._open = False
        self._last_temp: float | None = None
        self._t_min = 0.0
        self._u_int = 0.0
        self._d_int = 0.0
        self._text_int = 0.0
        self._n_ticks = 0
        self._frozen = False
        self._resolution_c: float | None = None
        self._pools: dict[str, Deque[BalanceSegment]] = {"a": deque(), "b": deque()}
        self._pool_mode: "VThermHvacMode | None" = None

    # ------------------------------------------------------------------
    # Window collection
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Return whether a window segment is being collected."""
        return self._open

    @property
    def resolution_c(self) -> float:
        """Return the sensor step used for the uncertainty model."""
        step = (
            self._resolution_c
            if self._resolution_c is not None
            else AB_BALANCE_DEFAULT_RESOLUTION_C
        )
        return max(step, AB_BALANCE_MIN_RESOLUTION_C)

    def pool_size(self, param: str) -> int:
        """Return the number of pooled segments for ``param``."""
        return len(self._pools[param])

    def observe(
        self, now: float, dt_s: float, u: float, temp: float, t_ext: float
    ) -> None:
        """Integrate one accepted window tick and record raw steps."""
        dt_min = dt_s / 60.0
        if not self._open:
            self._open = True
            self._frozen = False
            self._anchors = []
            self._t_min = self._u_int = self._d_int = self._text_int = 0.0
            self._n_ticks = 0
            self._last_temp = temp
            return
        if self._frozen:
            return
        if self._n_ticks > 0:
            u_mean = self._u_int / self._t_min if self._t_min > 0.0 else u
            if abs(u - u_mean) > max(U_CV_MAX * u_mean, U_CV_MIN_MEAN):
                self._frozen = True
                return
        self._n_ticks += 1

        held = self._last_temp if self._last_temp is not None else temp
        self._t_min += dt_min
        self._u_int += u * dt_min
        self._d_int += (held - t_ext) * dt_min
        self._text_int += t_ext * dt_min

        step = temp - held
        if abs(step) >= _STEP_EPS_C:
            if self._resolution_c is None or abs(step) < self._resolution_c:
                self._resolution_c = abs(step)
            self._anchors.append(
                BalanceAnchor(
                    ts=now,
                    temp=temp,
                    direction=1 if step > 0 else -1,
                    t_min=self._t_min,
                    u_int=self._u_int,
                    d_int=self._d_int,
                    text_int=self._text_int,
                )
            )
        self._last_temp = temp

    def discard(self) -> None:
        """Drop the window being collected (contaminated or already used)."""
        self._open = False
        self._anchors = []
        self._last_temp = None

    def clear(self) -> None:
        """Drop the window and every pooled segment."""
        self.discard()
        for pool in self._pools.values():
            pool.clear()
        self._pool_mode = None

    # ------------------------------------------------------------------
    # Window closing and pooling
    # ------------------------------------------------------------------

    def close(
        self,
        *,
        now: float,
        hvac_mode: "VThermHvacMode | None",
        target_temp: float | None,
        estimator: "ABEstimator",
    ) -> tuple[str, bool] | None:
        """Pool the closed window segment and submit a pool when precise.

        Returns ``(param, submitted)`` when a segment was pooled, else None.
        """
        anchors = self._anchors
        self.discard()
        if hvac_mode not in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            return None
        if self._pool_mode != hvac_mode:
            for pool in self._pools.values():
                pool.clear()
            self._pool_mode = hvac_mode

        segment = self._build_segment(anchors, hvac_mode, target_temp)
        if segment is None:
            return None
        pool = self._pools[segment.param]
        pool.append(segment)
        while pool and (
            len(pool) > AB_BALANCE_POOL_MAX_SEGMENTS
            or now - pool[0].end_ts > AB_BALANCE_POOL_MAX_AGE_S
        ):
            pool.popleft()
        submitted = self._submit_if_precise(segment.param, hvac_mode, estimator)
        return segment.param, submitted

    def _build_segment(
        self,
        anchors: list[BalanceAnchor],
        hvac_mode: "VThermHvacMode",
        target_temp: float | None,
    ) -> BalanceSegment | None:
        """Return the segment between the first and last raw steps of a window."""
        if len(anchors) < 2:
            return None
        first, last = anchors[0], anchors[-1]
        dt_min = last.t_min - first.t_min
        if dt_min < AB_BALANCE_MIN_SEGMENT_MIN:
            return None
        u_mean = (last.u_int - first.u_int) / dt_min
        if u_mean > U_ON_MIN:
            param = "a"
        elif u_mean < U_OFF_MAX:
            param = "b"
        else:
            return None

        active_dir = -1 if hvac_mode == VThermHvacMode_COOL else 1
        expected = active_dir if param == "a" else -active_dir
        if any(x.direction != expected for x in anchors):
            return None

        segment = BalanceSegment(
            end_ts=last.ts,
            param=param,
            dt_min=dt_min,
            d_temp=last.temp - first.temp,
            u_int=last.u_int - first.u_int,
            d_int=last.d_int - first.d_int,
            text_int=last.text_int - first.text_int,
        )
        if param == "b":
            t_ext = segment.text_int / dt_min
            t_int = t_ext + segment.d_int / dt_min
            if not passive_gradient_ok(hvac_mode, t_int, t_ext, target_temp):
                return None
        return segment

    def _submit_if_precise(
        self,
        param: str,
        hvac_mode: "VThermHvacMode",
        estimator: "ABEstimator",
    ) -> bool:
        """Submit the pooled balance once its uncertainty is low enough."""
        pool = self._pools[param]
        if param == "a" and len(estimator.b_meas_hist) < AB_A_SOFT_GATE_MIN_B:
            return False

        dt_min = sum(s.dt_min for s in pool)
        d_temp = sum(s.d_temp for s in pool)
        u_int = sum(s.u_int for s in pool)
        d_int = sum(s.d_int for s in pool)
        text_int = sum(s.text_int for s in pool)
        if dt_min <= 0.0:
            return False
        # a * U = dT + b * D for the active parameter, b * D = -dT for the
        # passive one.
        numerator = d_temp + estimator.b * d_int if param == "a" else d_temp
        rel_unc = pooled_relative_uncertainty(len(pool), numerator, self.resolution_c)
        if rel_unc > AB_BALANCE_MAX_REL_UNC:
            return False

        n_segments = len(pool)
        pool.clear()
        t_ext = text_int / dt_min
        estimator.diag_dTdt_method = "balance_pool"
        estimator.learn(
            dT_int_per_min=d_temp / dt_min,
            u=u_int / dt_min if param == "a" else 0.0,
            t_int=t_ext + d_int / dt_min,
            t_ext=t_ext,
            hvac_mode=hvac_mode,
        )
        _LOGGER.debug(
            "%s - balance pool %s submitted (%d segments, %.0f min, rel unc %.2f)",
            self._name,
            param,
            n_segments,
            dt_min,
            rel_unc,
        )
        return True
