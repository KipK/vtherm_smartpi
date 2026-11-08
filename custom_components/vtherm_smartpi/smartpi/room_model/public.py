"""User-facing values of the room model diagnostics.

The model keeps its own short codes. The diagnostics translate them into plain
values that a user can read without knowing the methods.
"""
from __future__ import annotations

from . import gates, observer
from .const import STRUCT_FULL

STATUS_LEARNING = "learning"
STATUS_IDENTIFIED = "identified"
STATUS_SIMPLE = "simple model"
NOT_IDENTIFIED = "not identified: {}"

REASON_VALVE = "valve"
REASON_DEPENDENCIES = "dependencies"

_REASONS = {
    gates.WHY_HISTORY: "not enough recorded nights yet",
    gates.WHY_EMITTER: "heating response delay not known yet",
    gates.WHY_DATA: "too few valid night measurements",
    gates.WHY_DISTURBED: "heat gains or losses detected during the nights",
    gates.WHY_HEAT: "too little heating during the nights",
    gates.WHY_DELTA: "indoor-outdoor temperature difference too small",
    gates.WHY_FIT: "the model could not be fitted",
    gates.WHY_RANGE: "values out of the plausible range",
    gates.WHY_RESIDUALS: "the model does not follow the measured temperature closely enough",
    gates.WHY_NO_PREVIOUS: "waiting for a second series of nights to confirm",
    gates.WHY_DISAGREE: "two successive series of nights disagree",
    gates.WHY_HELDOUT: "the model does not predict the following nights well enough",
    gates.WHY_FAILED: "computation failed",
    gates.WHY_EMITTER_EDGE: "the heating response delay does not match the measured temperature",
    gates.WHY_A_ONLINE: "the heating response differs too much from the one measured by the regulation",
    REASON_VALVE: "valve heaters are not supported yet",
    REASON_DEPENDENCIES: "numerical libraries unavailable",
}

# Learning outcomes that are only a matter of time
_WAITING = (gates.WHY_HISTORY, gates.WHY_NO_PREVIOUS)

_WALLS = {
    gates.WALL_COARSE: "not identified: temperature sensor too coarse",
    gates.WALL_SMALL: "not identified: no thermal mass detected",
    gates.WALL_NOT_CONFIRMED: "not identified: not confirmed yet",
    gates.WALL_NOT_BETTER: "not identified: no better than the simple model",
}

_TRACKING = {
    observer.HEALTH_OK: "ok",
    observer.HEALTH_NO_MODEL: "waiting for an identified model",
    observer.HEALTH_WAITING: "waiting for measurements",
    observer.HEALTH_PAUSED: "paused (SmartPI not heating or not in control)",
    observer.HEALTH_SETTLING: "settling after a restart",
    observer.HEALTH_WINDOW: "recovering after an open window",
    observer.HEALTH_BIAS: "measurements drift away from the model",
    observer.HEALTH_SPREAD: "measurements inconsistent with the model",
    observer.HEALTH_UNCERTAIN: "temperature estimate uncertain",
    observer.HEALTH_D_BOUND: "large unexplained heat gains or losses",
    observer.HEALTH_WALL: "wall state implausible",
}


def reason(code: str | None) -> str | None:
    """Plain reason of a refusal; an unknown code is shown as is."""
    if code is None:
        return None
    return _REASONS.get(code, code)


def status(struct: str | None, last_why: str | None, attempted: bool) -> str:
    """Model status: learning, identified, simple model, or not identified with its reason."""
    if struct is not None:
        return STATUS_IDENTIFIED if struct == STRUCT_FULL else STATUS_SIMPLE
    if last_why in (REASON_VALVE, REASON_DEPENDENCIES):
        return NOT_IDENTIFIED.format(reason(last_why))
    if not attempted or last_why is None or last_why in _WAITING:
        return STATUS_LEARNING
    return NOT_IDENTIFIED.format(reason(last_why))


def walls(struct: str | None, code: str | None) -> str | None:
    """Wall part of the model: identified, or why it is not."""
    if struct == STRUCT_FULL:
        return "identified"
    if code is None:
        return None
    return _WALLS.get(code, code)


def tracking(code: str) -> str:
    return _TRACKING.get(code, code)
