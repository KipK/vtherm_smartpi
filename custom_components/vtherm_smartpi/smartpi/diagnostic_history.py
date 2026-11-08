"""Build the Home Assistant diagnostic attribute envelope."""

from __future__ import annotations

from typing import Any, Mapping

DIAGNOSTIC_SCHEMA_VERSION = 2


def _horizon(errors: Any, label: str) -> Any:
    return errors.get(label) if isinstance(errors, Mapping) else None


def build_history_diagnostics(live: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable Recorder payload.

    The feed-forward, room model and dead-time series are always present, null
    when their block is missing or not computed yet.
    """
    control = live["control"]
    temperature = live["temperature"]
    setpoint = live["setpoint"]
    power = live["power"]
    model = live["model"]
    feedforward = live.get("feedforward") or {}
    room = live.get("room_model") or {}
    tar = live.get("tar_learning") or {}
    errors = room.get("forecast_error_c")
    return {
        "control": {
            "kp": control["kp"],
            "ki": control["ki"],
        },
        "temperature": {
            "indoor": temperature["indoor"],
            "integral_error": temperature["integral_error"],
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
        "feedforward": {
            "source": feedforward.get("source"),
            "wall_power_percent": feedforward.get("wall_power_percent"),
        },
        "room_model": {
            "a": room.get("a"),
            "b_eq": room.get("b_eq"),
            "forecast_error_1h_c": _horizon(errors, "1h"),
            "forecast_error_12h_c": _horizon(errors, "12h"),
        },
        "deadtime": {
            "applied_s": tar.get("applied_s"),
        },
    }


def build_diagnostic_attributes(live: dict[str, Any]) -> dict[str, Any]:
    """Return the complete diagnostic sensor attributes."""
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "live": live,
        "history": build_history_diagnostics(live),
    }
