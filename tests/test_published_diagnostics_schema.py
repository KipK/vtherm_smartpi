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
from custom_components.vtherm_smartpi.smartpi.ff_trim import CausalFFTrimResult

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
    result = CausalFFTrimResult(
        admissible=True,
        reason="periodic_window_ready",
        correction=-0.01,
        target_trim=-0.01,
        mean_causal_power=0.04,
        mean_ff1=0.08,
        mean_temperature=20.0,
        mean_error=0.0,
        mean_slope_h=0.0,
        observed_hold_power=0.04,
        duration_s=1800.0,
        measurement_count=7,
        alignment_delay_s=120.0,
        power_coverage_ratio=1.0,
        transfer_eligible=False,
        transfer_reason="periodic_equilibrium",
        transfer_quality="valve_segmented_linear",
        observation_mode="periodic",
    )
    # Repeated proposals commit one FF trim transaction.
    for _ in range(3):
        algo.fftrim_coord.apply_observer_result(result, count_window=False)
    algo._fftrim_observer.publish_external_result(result)
    # A healthy twin adds the analysis.twin subtree.
    algo._last_twin_diag = {"status": "ok", "model_reliable": True}
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

    assert published["feedforward"]["fftrim"]["last_result"] is not None
    assert published["feedforward"]["fftrim"]["last_transaction"] is not None
    assert "twin" in published["analysis"]

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
