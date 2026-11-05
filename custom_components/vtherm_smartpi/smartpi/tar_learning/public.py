"""User-facing values of the T_ar learning diagnostics.

The learning methods keep their own short codes. The diagnostics translate them
into plain values that a user can read without knowing the methods.
"""
from __future__ import annotations

ROOM_THERMAL_MASS = "thermal_mass"
ROOM_NO_THERMAL_MASS = "no_thermal_mass"
ROOM_UNDETERMINED = "undetermined"

_ROOM_CLASSES = {"mass": ROOM_THERMAL_MASS, "one": ROOM_NO_THERMAL_MASS}

# Why a morning comfort step gave no value
_MORNING_REASONS = {
    "accepted": "accepted",
    "computation failed": "computation failed",
    "data gap around the step": "missing or excluded data around the comfort step",
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

_UNCONFIRMED_PASSIVE_NIGHT = "night with at least 1 hour without heating between 23:00 and the comfort step"


def room_class(code: str | None) -> str | None:
    """Room type: thermal mass, no thermal mass, or undetermined."""
    if code is None:
        return None
    return _ROOM_CLASSES.get(code, ROOM_UNDETERMINED)


def morning_reason(code: str | None) -> str | None:
    """Plain reason of a morning decision; an unknown code is shown as is."""
    if code is None:
        return None
    if code.startswith("phase "):
        return f"SmartPI not in steady regulation ({code[len('phase '):].lower()})"
    return _MORNING_REASONS.get(code, code)


def unconfirmed_reason(code: str | None) -> str | None:
    """Plain reason why an accepted morning is not used."""
    if code is None:
        return None
    if code.startswith("night started passive in the sleep span"):
        return _UNCONFIRMED_PASSIVE_NIGHT
    return code
