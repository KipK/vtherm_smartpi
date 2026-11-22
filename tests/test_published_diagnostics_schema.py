"""Guard the key paths of the published SmartPI live diagnostics tree.

The expected paths live in ``fixtures/published_diagnostics_paths.json``.
After an intentional change of the published tree, regenerate it with:

    SMARTPI_UPDATE_DIAG_PATHS=1 python -m pytest tests/test_published_diagnostics_schema.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI

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
        "wall_power_percent",
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
