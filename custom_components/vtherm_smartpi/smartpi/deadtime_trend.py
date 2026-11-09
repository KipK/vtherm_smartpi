"""Free-running temperature trend before an active power start.

The response of the room to a power start is the deviation of its temperature
from the course it followed before the start. Without heating (HEAT) a room
relaxes towards a lower equilibrium: its free trend is flat or falling, and the
trough of the temperature after the start dates the response. A trend that
already goes in the direction of the response (a room warming with the heater
off, through occupants, sun or a neighbour) moves the temperature away from the
start level by itself, and a detection measured from the start level would
date the response too early.

``prestart_trend`` fits the trend on the raw temperature history of the OFF
period before the start. Only a trend in the direction of the response is
returned: the detector then follows the temperature minus its extrapolation.
A trend too irregular to be extrapolated, or bending towards the response,
rejects the detection.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .const import (
    DEADTIME_PRESTART_MIN_HALF_SAMPLES,
    DEADTIME_PRESTART_MIN_SAMPLES,
    DEADTIME_PRESTART_MIN_SPAN_S,
    DEADTIME_PRESTART_STEP_FLOOR_C,
    DEADTIME_PRESTART_WINDOW_S,
)


@dataclass(frozen=True)
class PrestartTrend:
    """Trend removed from the temperature while waiting for a response."""

    slope_c_per_s: float = 0.0
    rejected: bool = False


NO_TREND = PrestartTrend()


def _ols(samples: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Return (slope, residual rms) of the least-squares line, None if degenerate."""
    n = len(samples)
    if n < 2:
        return None
    mean_t = sum(t for t, _ in samples) / n
    mean_v = sum(v for _, v in samples) / n
    s_tt = sum((t - mean_t) ** 2 for t, _ in samples)
    if s_tt <= 0.0:
        return None
    slope = sum((t - mean_t) * (v - mean_v) for t, v in samples) / s_tt
    sse = sum((v - mean_v - slope * (t - mean_t)) ** 2 for t, v in samples)
    return slope, (sse / n) ** 0.5


def prestart_trend(
    history: Iterable[tuple[float, float]],
    *,
    start_ts: float,
    off_since_ts: float | None,
    direction: float,
    detection_threshold_c: float,
) -> PrestartTrend:
    """Return the free-running trend to remove from an active response.

    Args:
        history: raw (timestamp, temperature) samples, oldest first.
        start_ts: instant of the power start.
        off_since_ts: earliest instant of the free-running OFF period (the last
            power stop plus the passive delay), None when unknown.
        direction: +1 when the response rises (heat), -1 when it falls (cool).
        detection_threshold_c: move that detects a response.

    The window covers the last ``DEADTIME_PRESTART_WINDOW_S`` of the OFF
    period. Without ``DEADTIME_PRESTART_MIN_SPAN_S`` of samples no trend can be
    told apart and none is removed. A trend whose fitted change stays within
    one sensor step, or that goes against the response, is left to the trough
    (or peak) tracking: nothing is removed.

    A trend in the direction of the response is rejected when it is not a
    line: a residual rms above half the detection threshold (and above one
    sensor step), or a slope that grows towards the response between the two
    halves of the window by more than the detection threshold per half window.
    Its extrapolation would under-estimate the trend and date the response
    early. Otherwise the slope removed is the steepest one in the direction of
    the response among the whole window and its recent half: removing too much
    only delays the detection.
    """
    earliest = start_ts - DEADTIME_PRESTART_WINDOW_S
    if off_since_ts is not None:
        earliest = max(earliest, off_since_ts)
    samples = [(t, v) for t, v in history if earliest <= t <= start_ts]
    if len(samples) < DEADTIME_PRESTART_MIN_SAMPLES:
        return NO_TREND
    span = samples[-1][0] - samples[0][0]
    if span < DEADTIME_PRESTART_MIN_SPAN_S:
        return NO_TREND

    steps = [abs(b - a) for (_, a), (_, b) in zip(samples, samples[1:]) if b != a]
    if not steps:
        return NO_TREND
    step_c = max(min(steps), DEADTIME_PRESTART_STEP_FLOOR_C)

    full = _ols(samples)
    if full is None:
        return NO_TREND
    slope, rms = full

    mid = samples[0][0] + span / 2.0
    early = _half_slope([(t, v) for t, v in samples if t < mid])
    recent = _half_slope([(t, v) for t, v in samples if t >= mid])

    candidates = [(slope, span)]
    if recent is not None:
        candidates.append(recent)
    adverse = [s for s, s_span in candidates if direction * s * s_span >= step_c]
    if not adverse:
        return NO_TREND

    if rms > max(detection_threshold_c / 2.0, step_c):
        return PrestartTrend(rejected=True)
    if early is not None and recent is not None:
        bend = direction * (recent[0] - early[0]) * span / 2.0
        if bend > detection_threshold_c:
            return PrestartTrend(rejected=True)
    return PrestartTrend(slope_c_per_s=max(adverse, key=lambda s: direction * s))


def _half_slope(samples: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Return (slope, span) of a half window, None without enough samples."""
    if len(samples) < DEADTIME_PRESTART_MIN_HALF_SAMPLES:
        return None
    fit = _ols(samples)
    if fit is None:
        return None
    return fit[0], samples[-1][0] - samples[0][0]
