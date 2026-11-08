"""Tests for the SmartPI Recorder diagnostic payload."""

from copy import deepcopy

from custom_components.vtherm_smartpi.smartpi.diagnostic_history import (
    DIAGNOSTIC_SCHEMA_VERSION,
    build_diagnostic_attributes,
    build_history_diagnostics,
)


def _live_diagnostics() -> dict:
    return {
        "control": {"phase": "Stable", "kp": 0.4, "ki": 0.002, "ti_min": 200.0},
        "temperature": {"indoor": 20.5, "outdoor": 8.0, "integral_error": 75.0},
        "setpoint": {"filtered_setpoint": 21.0, "boost_active": False},
        "power": {
            "applied_percent": 30.0,
            "command_percent": 35.0,
            "pi_percent": 20.0,
            "ff_percent": 15.0,
            "limited_percent": 32.0,
        },
        "model": {"a": 0.05, "b": 0.001, "confidence": "ab_ok"},
        "analysis": {
            "control": {"error_filtered": 0.5},
        },
    }


def test_history_contains_only_equinox_time_series() -> None:
    """Recorder history must contain only the stable chart contract."""
    assert build_history_diagnostics(_live_diagnostics()) == {
        "control": {"kp": 0.4, "ki": 0.002},
        "temperature": {"indoor": 20.5, "integral_error": 75.0},
        "setpoint": {"filtered_setpoint": 21.0},
        "power": {
            "applied_percent": 30.0,
            "command_percent": 35.0,
            "pi_percent": 20.0,
            "ff_percent": 15.0,
        },
        "model": {"a": 0.05, "b": 0.001},
        "feedforward": {"source": None, "wall_power_percent": None},
        "room_model": {
            "a": None,
            "b_eq": None,
            "forecast_error_1h_c": None,
            "forecast_error_12h_c": None,
        },
        "deadtime": {"applied_s": None},
    }


def test_history_carries_the_feedforward_room_model_and_deadtime_series() -> None:
    """The room model, its feed-forward and the dead time in force are charted."""
    live = _live_diagnostics()
    live["feedforward"] = {"source": "room model (air and walls)", "wall_power_percent": 4.2, "ff3_status": "x"}
    live["room_model"] = {
        "status": "identified",
        "a": 0.031,
        "b_eq": 0.0012,
        "forecast_error_c": {"1h": 0.02, "2h": 0.03, "6h": 0.05, "12h": 0.08},
        "forecast_c": {"1h": 20.1},
    }
    live["tar_learning"] = {"applied_s": 310.0, "source": "refined"}

    history = build_history_diagnostics(live)

    assert history["feedforward"] == {"source": "room model (air and walls)", "wall_power_percent": 4.2}
    assert history["room_model"] == {
        "a": 0.031,
        "b_eq": 0.0012,
        "forecast_error_1h_c": 0.02,
        "forecast_error_12h_c": 0.08,
    }
    assert history["deadtime"] == {"applied_s": 310.0}


def test_history_series_are_null_before_any_forecast_check() -> None:
    live = _live_diagnostics()
    live["room_model"] = {"a": None, "b_eq": None, "forecast_error_c": {"1h": None, "12h": None}}
    history = build_history_diagnostics(live)
    assert history["room_model"]["forecast_error_1h_c"] is None
    assert history["deadtime"] == {"applied_s": None}


def test_attribute_envelope_keeps_live_and_history_separate() -> None:
    """The v2 envelope must expose live data without copying it into history."""
    live = _live_diagnostics()

    attributes = build_diagnostic_attributes(live)

    assert attributes["schema_version"] == DIAGNOSTIC_SCHEMA_VERSION
    assert attributes["live"] is live
    assert attributes["history"] == build_history_diagnostics(live)
    assert "analysis" not in attributes["history"]


def test_history_does_not_depend_on_the_analysis_subtree() -> None:
    live = _live_diagnostics()
    before = deepcopy(live)
    full = build_history_diagnostics(live)
    assert live == before
    del live["analysis"]
    assert build_history_diagnostics(live) == full
