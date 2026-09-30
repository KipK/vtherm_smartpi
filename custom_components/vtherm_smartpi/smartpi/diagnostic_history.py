"""Build the Home Assistant diagnostic attribute envelope."""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

DIAGNOSTIC_SCHEMA_VERSION = 2


def _history_number(value: Any, *, multiplier: float = 1.0, digits: int) -> float | None:
    """Keep absent evidence null and bound the recorded numeric precision."""
    if type(value) not in (int, float) or not isfinite(value):
        return None
    scaled = value * multiplier
    return round(scaled, digits) if isfinite(scaled) else None


def build_history_diagnostics(live: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable Recorder payload."""
    temperature = live["temperature"]
    setpoint = live["setpoint"]
    power = live["power"]
    model = live["model"]
    governor = live.get("analysis", {}).get("reference_governor", {})
    return {
        "temperature": {
            "indoor": temperature["indoor"],
        },
        "setpoint": {
            "filtered_setpoint": setpoint["filtered_setpoint"],
            "nominal_reference": _history_number(
                governor.get("nominal_reference"), digits=2
            ),
        },
        "power": {
            "applied_percent": power["applied_percent"],
            "command_percent": power["command_percent"],
            "pi_percent": power["pi_percent"],
            "ff_percent": power["ff_percent"],
            "governor_cap_percent": _history_number(
                governor.get("command_cap"), multiplier=100.0, digits=1
            ),
            "before_governor_cap_percent": _history_number(
                governor.get("command_before_cap"), multiplier=100.0, digits=1
            ),
        },
        "model": {
            "a": model["a"],
            "b": model["b"],
        },
    }


def build_diagnostic_attributes(live: dict[str, Any]) -> dict[str, Any]:
    """Return the complete diagnostic sensor attributes."""
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "live": live,
        "history": build_history_diagnostics(live),
    }
