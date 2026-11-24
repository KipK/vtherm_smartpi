"""Build the Home Assistant diagnostic attribute envelope."""

from __future__ import annotations

from typing import Any, Mapping

DIAGNOSTIC_SCHEMA_VERSION = 2


def _horizon(errors: Any, label: str) -> Any:
    return errors.get(label) if isinstance(errors, Mapping) else None


def _effective_dead_time(model: Mapping[str, Any]) -> int | None:
    """Dead time plus half the response time constant of the response model (whole seconds), None without it."""
    dead, tau = model.get("dead_time_s"), model.get("response_time_constant_s")
    return None if dead is None or tau is None else round(dead + tau / 2)


def build_history_diagnostics(live: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable Recorder payload.

    The feed-forward, room model, dead-time and effective dead time series are
    always present, null when their block is missing or not computed yet.
    """
    control = live["control"]
    temperature = live["temperature"]
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
            "anticipated": temperature["anticipated"],
            "integral_error": temperature["integral_error"],
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
            "effective_dead_time_s": _effective_dead_time(model),
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
