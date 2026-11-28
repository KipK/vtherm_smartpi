"""Guard the key paths of the published SmartPI live diagnostics tree.

The expected paths live in ``fixtures/published_diagnostics_paths.json``.
After an intentional change of the published tree, regenerate it with:

    SMARTPI_UPDATE_DIAG_PATHS=1 python -m pytest tests/test_published_diagnostics_schema.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI

from .helpers import force_smartpi_stable_mode

FIXTURE = Path(__file__).parent / "fixtures" / "published_diagnostics_paths.json"


def _paths(node: Any, prefix: str = "") -> set[str]:
    """Return every dict key path of a nested mapping, leaves included."""
    if not isinstance(node, dict):
        return set()
    paths: set[str] = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.add(path)
        paths |= _paths(value, path)
    return paths


def _representative_smartpi() -> SmartPI:
    """Return a SmartPI exposing every optional published subtree."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=2.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Published diagnostics schema",
        room_model=True,
    )
    # Valve mode adds the valve subtree.
    algo.valve_config_diag = {
        "vt_dpercent": 0.0,
        "vt_period_min": 0.0,
        "opening_remap_active": False,
        "issues": [],
    }
    return algo


def test_published_diagnostics_key_paths_match_fixture() -> None:
    published = _representative_smartpi().get_published_diagnostics()
    actual = sorted(_paths(published))

    # The feed-forward trim, the short-horizon feed-forward and the thermal twin are not published.
    assert set(published["feedforward"]) == {
        "source",
        "deadband_power_source",
        "deadband_p_mode",
    }
    assert set(published["analysis"]["feedforward"]) == {
        "reason",
        "u_ff1",
        "u_ff_final",
        "u_ff_effective",
    }
    assert "twin" not in published["analysis"]
    # The emitter lag learning is not published; the room model shows its quiet periods and data quality.
    assert "tar_learning" not in published
    assert set(published["room_model"]["quiet_periods"]) == {"reason", "count", "last"}
    # The room model is observation only, and the holding power always comes from the learned model.
    assert published["room_model"]["used_by_regulation"] is False
    assert published["feedforward"]["source"] == "learned model"

    if os.environ.get("SMARTPI_UPDATE_DIAG_PATHS") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")

    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    assert not missing and not unexpected, (
        f"Published diagnostics paths changed. Missing: {missing}. "
        f"Unexpected: {unexpected}."
    )


def test_published_integral_time_follows_applied_gains() -> None:
    """ti_min is Kp / Ki in minutes, None when Ki is not positive."""
    algo = _representative_smartpi()

    algo.Kp = 0.5
    algo.Ki = 0.004
    assert algo.get_published_diagnostics()["control"]["ti_min"] == 125.0

    algo.Ki = 0.0
    assert algo.get_published_diagnostics()["control"]["ti_min"] is None


def test_published_start_summary_has_plain_values() -> None:
    """learning.start: the start mode, the pulses done and the pulse in progress in words, the kept values."""
    algo = _representative_smartpi()
    start = algo.get_published_diagnostics()["learning"]["start"]
    assert set(start) == {
        "mode", "pulses_done", "pulse", "values_kept", "values_kept_at", "kept_a", "kept_b", "kept_lag_s",
    }
    assert start["mode"] == "bootstrap" and start["pulses_done"] == 0 and start["pulse"] == "none"
    assert start["values_kept"] is False and start["values_kept_at"] is None
    for state, plain in (("idle", "none"), ("rest", "rest"), ("heat", "full power"), ("stop", "stop")):
        algo._start.pulse._state = state
        assert algo.get_published_diagnostics()["learning"]["start"]["pulse"] == plain


def test_published_calibration_summary_has_plain_values() -> None:
    """calibration: its state in words, the pulses, the result, the last request, the dead time after a start, the agreeing reading,
    the values in force it was compared with and the quantities it corrected, in words."""
    algo = _representative_smartpi()
    cal = algo.get_published_diagnostics()["calibration"]
    assert set(cal) == {
        "state", "pulses_done", "result", "last_request", "last_time", "deadtime_start_s",
        "reading_a", "reading_b", "reading_lag_s", "in_force_a", "in_force_b", "in_force_lag_s", "corrected",
        "automatic", "interruptions", "automatic_status",
    }
    assert cal["state"] == "idle" and cal["pulses_done"] == 0 and cal["result"] is None
    assert cal["automatic"] is False and cal["interruptions"] == [] and cal["automatic_status"] == "not needed"
    assert cal["last_request"] is None and cal["corrected"] == [] and cal["in_force_a"] is None
    algo._start.calibration.in_force = {"a": 0.05, "b": 0.0003, "lag_s": 600.0}
    algo._start.calibration.corrected = ["a", "b"]
    cal = algo.get_published_diagnostics()["calibration"]
    assert cal["corrected"] == ["heating rate", "loss coefficient"]
    assert (cal["in_force_a"], cal["in_force_b"], cal["in_force_lag_s"]) == (0.05, 0.0003, 600)
    force_smartpi_stable_mode(algo)
    algo._start.calibration.begin(automatic=True)
    algo._start.calibration.note_interruption("setpoint change", 1_700_000_000.0)
    cal = algo.get_published_diagnostics()["calibration"]
    assert cal["state"] == "waiting" and cal["automatic"] is True
    assert cal["interruptions"] == [{"reason": "setpoint change", "at": datetime.fromtimestamp(1_700_000_000.0).isoformat()}]
    algo._start.calibration.begin()
    algo._start.pulse._cur = {"calibration": True}
    for state, plain in (("rest", "rest"), ("heat", "full power"), ("stop", "stop")):
        algo._start.pulse._state = state
        published = algo.get_published_diagnostics()
        assert published["calibration"]["state"] == plain and published["learning"]["start"]["pulse"] == "none"
        assert published["control"]["phase"] == "Calibration"


def test_published_learning_labels_name_the_pulses_with_the_direct_start_only() -> None:
    """While the first model is not published: the bootstrap start keeps its labels, the direct start publishes plain
    ones that name neither the bootstrap nor the hysteresis; after the stage both read the same."""
    from custom_components.vtherm_smartpi.sensor import _get_diagnostic_state

    def seen(algo: SmartPI) -> tuple:
        live = algo.get_published_diagnostics()
        return (
            _get_diagnostic_state(algo), live["control"]["phase"], live["learning"]["stage"], live["model"]["confidence"],
        )

    boot = _representative_smartpi()
    assert seen(boot) == ("bootstrap_hysteresis", "Hysteresis", "bootstrap", "ab_bootstrap")
    direct = SmartPI(
        hass=MagicMock(), cycle_min=2.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
        name="Published diagnostics direct", learning_start="direct",
    )
    state, phase, stage, confidence = seen(direct)
    assert (state, phase, stage, confidence) == ("learning_pulses", "Learning", "measurement pulses", "ab_learning")
    published = direct.get_published_diagnostics()
    assert published["control"]["mode"] == "smartpi"
    for value in (state, phase, stage, confidence):
        assert "bootstrap" not in value.lower() and "hysteresis" not in value.lower()
    direct._start.stage_waiting = lambda _estimator: False     # the first model is published
    after = seen(direct)
    assert after[:2] == ("stable", "Stable") and after[2] not in ("measurement pulses", "bootstrap")
    assert after[3] != "ab_learning"


def test_published_values_during_a_guard_cut_follow_the_room_in_plain_words() -> None:
    """During a guard cut the PI is not computed: the error of the cut is published, no anticipation, and the regulation
    mode says that the heating is cut above the setpoint; the values of the regulation come back after the cut."""
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
    from custom_components.vtherm_smartpi.smartpi.guards import GUARD_CUT_I_MODE, GUARD_CUT_STATE

    algo = _representative_smartpi()
    force_smartpi_stable_mode(algo)
    algo._last_error, algo._last_error_p, algo._anticipation.value = 0.4, 0.9, -0.5   # frozen at the cut start
    algo._last_current_temp = 20.6
    algo.guards._guard_cut_active = True
    algo.ctl.last_i_mode = GUARD_CUT_I_MODE
    algo.guards.note_cut(20.0, 20.6, VThermHvacMode_HEAT)
    live = algo.get_published_diagnostics()
    assert live["control"]["mode"] == GUARD_CUT_STATE == "heating cut above the setpoint"
    assert live["temperature"]["error"] == -0.6 and live["temperature"]["anticipated"] == 20.6
    assert live["analysis"]["control"]["error_p"] == -0.6 and live["analysis"]["control"]["anticipation_c"] == 0.0
    assert (algo.error, algo.error_p, algo.anticipation_c) == (0.4, 0.9, -0.5)   # the regulation state is untouched
    algo.ctl.last_i_mode = "I:FREEZE(hvac_off)"     # a calculation outside the cut (off) while the cut is held
    live = algo.get_published_diagnostics()
    assert live["control"]["mode"] == "smartpi" and live["temperature"]["error"] == 0.4
    assert live["analysis"]["control"]["anticipation_c"] == -0.5


def test_published_learning_block_values_are_plain_words() -> None:
    """learning.block_reason: null, "paused", "disturbance" or "heating unavailable"; the integral hold sources of
    the heating unavailable entity are "heating_unavailable" and "heating_unavailable_resume"."""
    from custom_components.vtherm_smartpi.smartpi.learning_block import (
        BLOCK_DISTURBANCE,
        BLOCK_HEATING_UNAVAILABLE,
        BLOCK_PAUSED,
        INTEGRAL_HOLD_SOURCE_HEATING_UNAVAILABLE,
    )
    from custom_components.vtherm_smartpi.smartpi.resume_hold import ResumeHoldSource

    algo = _representative_smartpi()
    assert algo.get_published_diagnostics()["learning"]["block_reason"] is None
    algo.set_learning_enabled(False)
    assert algo.get_published_diagnostics()["learning"]["block_reason"] == BLOCK_PAUSED == "paused"
    assert (BLOCK_DISTURBANCE, BLOCK_HEATING_UNAVAILABLE) == ("disturbance", "heating unavailable")
    assert INTEGRAL_HOLD_SOURCE_HEATING_UNAVAILABLE == "heating_unavailable"
    assert ResumeHoldSource.HEATING_UNAVAILABLE_RESUME.value == "heating_unavailable_resume"
