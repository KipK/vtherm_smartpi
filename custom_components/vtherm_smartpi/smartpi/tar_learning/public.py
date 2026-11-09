"""User-facing values of the T_ar learning diagnostics.

The learning methods keep their own short codes. The diagnostics translate them
into plain values that a user can read without knowing the methods.
"""
from __future__ import annotations

ROOM_THERMAL_MASS = "thermal_mass"
ROOM_NO_THERMAL_MASS = "no_thermal_mass"
ROOM_UNDETERMINED = "undetermined"

_ROOM_CLASSES = {"mass": ROOM_THERMAL_MASS, "one": ROOM_NO_THERMAL_MASS}

# Why a comfort step gave no value
_MORNING_REASONS = {
    "accepted": "accepted",
    "computation failed": "computation failed",
    "data gap around the step": "missing or excluded data around the comfort step",
    "home position unknown": "home position unknown: the absence of sun cannot be checked",
    "comfort step in daylight": "comfort step in daylight",
    "no quiet period before the step":
        "no quiet period before the comfort step (dark, constant setpoint and valid data for at least 1 hour)",
    "low setpoint not held in the dark before the step":
        "low setpoint not held in the dark with valid data for the 2 hours before the comfort step",
    "sunrise too close to the comfort step": "sunrise too soon after the comfort step",
    "excitation: cut by sunrise": "comfort step cut by sunrise before its response could be measured",
    "setpoint restored after a restart": "setpoint restored after a restart, not a comfort step",
    "no published b": "heat loss b not learned yet",
    "no heater start after the comfort step": "no heating start after the comfort step",
    "night not usable for the room class": "night not usable for this type of room",
    "too few anchors before the step": "too few temperature readings before the comfort step",
    "baseline": "night temperature trend not usable",
    "baseline (cycle bins)": "night temperature trend does not match the heating cycles",
    "baseline rms": "night temperature trend too irregular",
    "baseline rms (fixed 0.03 C)": "night temperature trend too irregular",
    "fit invalid": "temperature rise could not be fitted",
    "fit rms": "temperature rise fitted too loosely",
    "excitation: window": "temperature rise too short to measure",
    "excitation: energy": "too little heat delivered during the rise",
    "excitation: rise": "temperature rise too small",
    "sensor period": "temperature sensor reports too rarely",
    "regulated: not computed": "regulated night: no measurement",
    "regulated: regulation not established over 2 h": "room not regulated during the 2 hours before the comfort step",
    "regulated: not at eco": "room not at its eco setpoint before the comfort step",
    "regulated: holding power not stationary": "heating power not steady before the comfort step",
    "regulated: no power jump": "no clear heating increase at the comfort step",
    "regulated: baseline (cycle bins)": "night temperature trend does not match the heating cycles",
    "regulated: baseline rms": "night temperature trend too irregular",
    "regulated: refined mass, no regulated-branch setup":
        "room with thermal mass after a regulated night: measured by the multi-day identification only",
    "regulated: unknown mass (silent)": "type of room not known yet",
}

# Why an accepted comfort step is not used (G1 and GT4 share the first part)
_UNCONFIRMED = {
    "idle in the regulated part":
        "at least 1 hour without heating while the room was regulated at its low setpoint before the comfort step",
    "no regulated start": "the room did not reach its low setpoint before the comfort step",
    "regulated part too short": "the room was regulated at its low setpoint for less than 4 hours before the comfort step",
}

# Why no quiet period can be found
_PERIOD_REASONS = {
    "home position unknown": "home position unknown: the absence of sun cannot be checked",
}


def room_class(code: str | None) -> str | None:
    """Room type: thermal mass, no thermal mass, or undetermined."""
    if code is None:
        return None
    return _ROOM_CLASSES.get(code, ROOM_UNDETERMINED)


def morning_reason(code: str | None) -> str | None:
    """Plain reason of a comfort-step decision; an unknown code is shown as is."""
    if code is None:
        return None
    if code.startswith("phase "):
        return f"SmartPI not in steady regulation ({code[len('phase '):].lower()})"
    return _MORNING_REASONS.get(code, code)


def unconfirmed_reason(code: str | None) -> str | None:
    """Plain reason why an accepted comfort step is not used."""
    if code is None:
        return None
    return _UNCONFIRMED.get(code.split(",")[0], code)


def period_reason(code: str | None) -> str | None:
    """Plain reason why no quiet period can be found."""
    if code is None:
        return None
    return _PERIOD_REASONS.get(code, code)
