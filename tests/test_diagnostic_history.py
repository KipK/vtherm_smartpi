"""Tests for the SmartPI Recorder diagnostic payload."""

from custom_components.vtherm_smartpi.smartpi.diagnostic_history import (
    DIAGNOSTIC_SCHEMA_VERSION,
    build_diagnostic_attributes,
    build_history_diagnostics,
)


def _live_diagnostics() -> dict:
    return {
        "control": {"phase": "Stable"},
        "temperature": {"indoor": 20.5, "outdoor": 8.0},
        "setpoint": {"filtered_setpoint": 21.0, "boost_active": False},
        "power": {
            "applied_percent": 30.0,
            "command_percent": 35.0,
            "pi_percent": 20.0,
            "ff_percent": 15.0,
            "limited_percent": 32.0,
        },
        "model": {"a": 0.05, "b": 0.001, "confidence": "ab_ok"},
        "analysis": {"control": {"error_filtered": 0.5}},
    }


def test_history_contains_only_equinox_time_series() -> None:
    """Recorder history must contain only the stable chart contract."""
    assert build_history_diagnostics(_live_diagnostics()) == {
        "temperature": {"indoor": 20.5},
        "setpoint": {"filtered_setpoint": 21.0},
        "power": {
            "applied_percent": 30.0,
            "command_percent": 35.0,
            "pi_percent": 20.0,
            "ff_percent": 15.0,
        },
        "model": {"a": 0.05, "b": 0.001},
    }


def test_attribute_envelope_keeps_live_and_history_separate() -> None:
    """The v2 envelope must expose live data without copying it into history."""
    live = _live_diagnostics()

    attributes = build_diagnostic_attributes(live)

    assert attributes["schema_version"] == DIAGNOSTIC_SCHEMA_VERSION
    assert attributes["live"] is live
    assert attributes["history"] == build_history_diagnostics(live)
    assert "analysis" not in attributes["history"]
