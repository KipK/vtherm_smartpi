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
    ANTICIPATION_MAX_WINDOW_S,
    ANTICIPATION_MIN_WINDOW_S,
    clamp,
)

SOURCE_RESPONSE_MODEL = "response model"
SOURCE_PULSES = "measurement pulses"
SOURCE_DEAD_TIMES = "dead times"
SOURCE_NONE = "none"


class AnticipationHorizon(NamedTuple):
    """Horizon (s) of the anticipation and where it comes from; 0 means no anticipation."""

    seconds: float
    source: str


def select_horizon(
    model: dict | None,
    dead_time_view: Callable[[], object],
    hvac_mode,
    pulse_lag_s: float | None = None,
) -> AnticipationHorizon:
    """Return the horizon of the anticipation for ``hvac_mode``.

    ``model`` is the response model of the mode (``ResponseModel.values``: ``theta_s``, ``tau_e_s``), None without
    one. With a model the horizon is its emitter lag theta + tau_e, the mean arrival time of the commanded heat; when
    the split of the model is assumed that sum is the emitter lag S itself (the cautious end of theta in [0, S]).
    Without a model: ``pulse_lag_s`` (the lag kept from the readings of the measurement pulses in ``hvac_mode``, None
    when no values are kept in that mode) when positive, else the longer of the active dead time of the mode and half
    its passive dead time, each counted only when reliable and positive; 0 when none counts. ``dead_time_view``
    returns the dead-time estimator (``active_deadtime`` and ``passive_deadtime`` by mode) and is called only when
    nothing before decides.
    """
    if model is not None:
        lag = float(model["theta_s"]) + float(model["tau_e_s"])
        if lag > 0.0:
            return AnticipationHorizon(lag, SOURCE_RESPONSE_MODEL)
    if pulse_lag_s is not None and pulse_lag_s > 0.0:
        return AnticipationHorizon(float(pulse_lag_s), SOURCE_PULSES)
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

    The correction is the least-squares slope of the raw temperature history times
    the horizon, limited to +/- ANTICIPATION_MAX_C. The slope is read over a window
    equal to the horizon, bounded to [ANTICIPATION_MIN_WINDOW_S,
    ANTICIPATION_MAX_WINDOW_S] (a window shorter than the horizon would turn a quick
    sensor step into a large correction). It is 0 when the horizon
    is 0, when ``active`` is false (the caller passes the stable phase),
    with fewer than 3 samples, or when the samples span less than half of the window.

    The correction c is then multiplied by w = clamp(2 - e / c, 0, 1), e being the
    raw error to the target (target - measured temperature): w is 0 when the room
    moves toward the setpoint and is further from it than twice c, 1 within c of it,
    beyond it, or when the room moves away from it.
    """
    if horizon_s <= 0.0 or not active:
        return 0.0
    window = min(max(ANTICIPATION_MIN_WINDOW_S, horizon_s), ANTICIPATION_MAX_WINDOW_S)
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


class DeadbandGate:
    """Ends the correction once the room has settled inside the regulator's deadband.

    Outside the deadband the correction passes unchanged. Inside it, the correction
    is kept until it is 0 or of the sign opposite to the previous calculation's, so
    that a room arriving at its setpoint keeps its braking and the command does not
    step at the entry; from then on it is 0 until the room leaves the deadband: a
    room settled at its setpoint is not approaching it, and a small quick move of
    the sensor would otherwise be extrapolated over the whole horizon.
    ``in_deadband`` is the state the previous calculation left. The state is not
    persisted.
    """

    def __init__(self) -> None:
        self.settled = False
        self.previous = 0.0
        self.value = 0.0

    def reset(self) -> None:
        """Forget the settled mark and the previous correction."""
        self.settled = False
        self.previous = 0.0
        self.value = 0.0

    def apply(self, correction: float, in_deadband: bool) -> float:
        """Return the correction to use for this calculation."""
        previous, self.previous = self.previous, correction
        if not in_deadband:
            self.settled = False
        elif not self.settled and (correction == 0.0 or correction * previous < 0.0):
            self.settled = True
        self.value = 0.0 if self.settled else correction
        return self.value
