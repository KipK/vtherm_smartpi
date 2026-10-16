"""Build the Home Assistant diagnostic attribute envelope."""

from __future__ import annotations

from typing import Any, Mapping

DIAGNOSTIC_SCHEMA_VERSION = 2


def build_history_diagnostics(live: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable Recorder payload."""
    temperature = live["temperature"]
    setpoint = live["setpoint"]
    power = live["power"]
    model = live["model"]
    return {
        "temperature": {
            "indoor": temperature["indoor"],
        },
        "setpoint": {
            "filtered_setpoint": setpoint["filtered_setpoint"],
        },
        "power": {
            "applied_percent": power["applied_percent"],
            "command_percent": power["command_percent"],
            "pi_percent": power["pi_percent"],
            "ff_percent": power["ff_percent"],
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
