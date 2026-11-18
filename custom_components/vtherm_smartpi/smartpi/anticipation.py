"""Anticipation of the measured temperature for the regulation, in heating and cooling.

The proportional and integral errors of the regulation read an anticipated
temperature: the measured one plus the distance the room is about to travel
during the delay between a command and its effect. The distance is the slope
of the raw temperature history times that delay (the horizon), limited, and
faded out when the room approaches the setpoint from far enough. The rule is
the same in both modes: the correction is a temperature added to the measured
one, whatever the direction of the demand.
"""
from __future__ import annotations

from typing import Callable, Iterable, NamedTuple, Tuple

from ..hvac_mode import VThermHvacMode_COOL
from .const import (
    ANTICIPATION_MAX_C,
    ANTICIPATION_MIN_WINDOW_S,
    ANTICIPATION_WINDOW_FRAC,
    clamp,
)

SOURCE_HEATING_BALANCE = "heating balance"
SOURCE_DEAD_TIMES = "dead times"
SOURCE_NONE = "none"


class AnticipationHorizon(NamedTuple):
    """Horizon (s) of the anticipation and where it comes from; 0 means no anticipation."""

    seconds: float
    source: str


def select_horizon(kept_lag_s: float | None, dead_time_view: Callable[[], object], hvac_mode) -> AnticipationHorizon:
    """Return the horizon of the anticipation for ``hvac_mode``.

    In heating it is the median of the last lags measured by the heating balance
    when that is positive. Otherwise, and always in cooling (the balance is
    measured in heating only), it is the longer of the active dead time of the
    mode and half its passive dead time, each counted only when reliable and
    positive. It is 0 when none counts. ``dead_time_view`` returns the dead-time
    source of the gain rule (``active_deadtime`` and ``passive_deadtime`` by mode)
    and is called only when the lag does not decide.
    """
    if hvac_mode != VThermHvacMode_COOL and kept_lag_s is not None and kept_lag_s > 0.0:
        return AnticipationHorizon(float(kept_lag_s), SOURCE_HEATING_BALANCE)
    view = dead_time_view()
    active, active_reliable = view.active_deadtime(hvac_mode)
    passive, passive_reliable = view.passive_deadtime(hvac_mode)
    active = float(active) if active_reliable and active is not None and active > 0.0 else 0.0
    passive = 0.5 * float(passive) if passive_reliable and passive is not None and passive > 0.0 else 0.0
    seconds = max(active, passive)
    return AnticipationHorizon(seconds, SOURCE_DEAD_TIMES if seconds > 0.0 else SOURCE_NONE)


def anticipated_error(error: float, correction: float, hvac_mode) -> float:
    """Return ``error`` recomputed on the anticipated temperature (measured + ``correction``).

    ``error`` is signed by the mode: target - measured in heating, measured - target
    in cooling. Adding ``correction`` to the measured temperature lowers the heating
    error and raises the cooling one by the same amount.
    """
    return error + correction if hvac_mode == VThermHvacMode_COOL else error - correction


def correction_c(
    horizon_s: float,
    now: float,
    current_temp: float,
    target_temp: float | None,
    tin_history: Iterable[Tuple[float, float]],
    active: bool,
) -> float:
    """Return the correction (°C) added to the measured temperature for the P and I errors.

    The correction is the least-squares slope of the raw temperature history over
    the last max(ANTICIPATION_MIN_WINDOW_S, horizon * ANTICIPATION_WINDOW_FRAC)
    times the horizon, limited to +/- ANTICIPATION_MAX_C. It is 0 when the horizon
    is 0, when ``active`` is false (the caller passes the stable phase),
    with fewer than 3 samples, or when the samples span less than half of the window.

    The correction c is then multiplied by w = clamp(2 - e / c, 0, 1), e being the
    raw error to the target (target - measured temperature): w is 0 when the room
    moves toward the setpoint and is further from it than twice c, 1 within c of it,
    beyond it, or when the room moves away from it.
    """
    if horizon_s <= 0.0 or not active:
        return 0.0
    window = max(ANTICIPATION_MIN_WINDOW_S, horizon_s * ANTICIPATION_WINDOW_FRAC)
    points = [(t, v) for t, v in tin_history if now - window <= t <= now]
    if not points or points[-1][0] < now:
        points.append((now, float(current_temp)))  # the current tick is not in the history yet
    if len(points) < 3 or points[-1][0] - points[0][0] < 0.5 * window:
        return 0.0
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in points)
    if var_t <= 0.0:
        return 0.0
    slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / var_t
    correction = clamp(slope * horizon_s, -ANTICIPATION_MAX_C, ANTICIPATION_MAX_C)
    if target_temp is not None:
        if correction == 0.0:
            return 0.0
        error = float(target_temp) - float(current_temp)
        correction *= clamp(2.0 - error / correction, 0.0, 1.0)
    return correction
