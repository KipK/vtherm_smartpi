"""Tests for the SmartPI Recorder diagnostic payload."""

from copy import deepcopy

import pytest

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
        "analysis": {
            "control": {"error_filtered": 0.5},
            "reference_governor": {
                "nominal_reference": 20.987654,
                "command_cap": 0.287468,
                "command_before_cap": 0.311479,
            },
        },
    }


def test_history_contains_only_equinox_time_series() -> None:
    """Recorder history must contain only the stable chart contract."""
    assert build_history_diagnostics(_live_diagnostics()) == {
        "temperature": {"indoor": 20.5},
        "setpoint": {"filtered_setpoint": 21.0, "nominal_reference": 20.99},
        "power": {
            "applied_percent": 30.0,
            "command_percent": 35.0,
            "pi_percent": 20.0,
            "ff_percent": 15.0,
            "governor_cap_percent": 28.7,
            "before_governor_cap_percent": 31.1,
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


@pytest.mark.parametrize("missing", ["analysis", "reference_governor"])
def test_older_live_payload_keeps_optional_history_values_null(missing) -> None:
    live = _live_diagnostics()
    if missing == "analysis":
        del live["analysis"]
    else:
        del live["analysis"]["reference_governor"]
    history = build_history_diagnostics(live)
    assert history["setpoint"]["nominal_reference"] is None
    assert history["power"]["governor_cap_percent"] is None
    assert history["power"]["before_governor_cap_percent"] is None
    assert history["power"]["command_percent"] == 35.0


def test_retired_episode_drops_cap_and_reference_without_stale_values() -> None:
    live = _live_diagnostics()
    before = deepcopy(live)
    active = build_history_diagnostics(live)
    assert live == before
    live["analysis"]["reference_governor"].update(
        nominal_reference=None, command_cap=None, command_before_cap=0.32
    )
    retired = build_history_diagnostics(live)
    assert active["power"]["governor_cap_percent"] == 28.7
    assert retired["power"]["governor_cap_percent"] is None
    assert retired["setpoint"]["nominal_reference"] is None
    assert retired["power"]["before_governor_cap_percent"] == 32.0


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_real_command_boundaries_are_recorded_as_percent(value) -> None:
    live = _live_diagnostics()
    live["analysis"]["reference_governor"].update(
        command_cap=value, command_before_cap=value
    )
    history = build_history_diagnostics(live)
    assert history["power"]["governor_cap_percent"] == value * 100.0
    assert history["power"]["before_governor_cap_percent"] == value * 100.0


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "0.3"])
def test_invalid_optional_numeric_evidence_is_not_recorded(value) -> None:
    live = _live_diagnostics()
    live["analysis"]["reference_governor"] = dict(
        nominal_reference=value, command_cap=value, command_before_cap=value
    )
    history = build_history_diagnostics(live)
    assert history["setpoint"]["nominal_reference"] is None
    assert history["power"]["governor_cap_percent"] is None
    assert history["power"]["before_governor_cap_percent"] is None
