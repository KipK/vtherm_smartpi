"""Tests for SmartPI persistence and reset behavior."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import (
    PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN,
)
from custom_components.vtherm_smartpi.smartpi.diagnostics import build_published_diagnostics


def test_save_and_load_state() -> None:
    """SmartPI state should restore the learned model and PI state."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI1",
    )

    smartpi1.est.a = 0.015
    smartpi1.est.b = 0.003
    smartpi1.est.learn_ok_count = 10
    smartpi1.integral = 5.0

    saved = smartpi1.save_state()

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI2",
        saved_state=saved,
    )

    assert smartpi2.est.a == 0.015
    assert smartpi2.est.b == 0.003
    assert smartpi2.est.learn_ok_count == 10
    assert smartpi2.integral == 5.0
    assert smartpi2.u_prev == 0.0


def test_last_active_wall_timestamp_is_persisted() -> None:
    """Persistence must retain the last valid active regulation wall time."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI1",
    )
    smartpi1._last_active_wall_ts = 1_700_000_000.0

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI2",
        saved_state=smartpi1.save_state(),
    )

    assert smartpi2._last_active_wall_ts == 1_700_000_000.0


def test_prolonged_inactivity_expires_only_controller_memory() -> None:
    """A pause beyond the configured limit must purge PI state but preserve the model."""
    now_wall = 1_800_000_000.0
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI",
    )
    smartpi.est.a = 0.015
    smartpi.est.b = 0.003
    smartpi.integral = 5.0
    smartpi._last_active_wall_ts = now_wall - (
        (PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN + 1.0) * 60.0
    )

    with patch("custom_components.vtherm_smartpi.algo.time.time", return_value=now_wall):
        expired = smartpi._refresh_active_timestamp_and_expire_controller_memory()

    assert expired is True
    assert smartpi.integral == 0.0
    assert smartpi.est.a == 0.015
    assert smartpi.est.b == 0.003
    assert smartpi._last_active_wall_ts == now_wall


def test_missing_active_timestamp_does_not_expire_legacy_state() -> None:
    """A state saved without an active timestamp must remain backward compatible."""
    now_wall = 1_800_000_000.0
    source = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI",
    )
    source.integral = 5.0
    saved = source.save_state()
    saved.pop("last_active_wall_ts")

    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPILegacy",
        saved_state=saved,
    )
    assert smartpi.integral == 5.0

    with patch("custom_components.vtherm_smartpi.algo.time.time", return_value=now_wall):
        expired = smartpi._refresh_active_timestamp_and_expire_controller_memory()

    assert expired is False
    assert smartpi.integral == 5.0
    assert smartpi._last_active_wall_ts == now_wall


def test_debug_learning_counters_are_runtime_scoped_after_load() -> None:
    """Published learning counters should restart from zero after restore."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI1",
    )
    smartpi1.est.learn_ok_count = 10
    smartpi1.est.learn_skip_count = 4

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI2",
        saved_state=smartpi1.save_state(),
        debug_mode=True,
    )

    debug = smartpi2.get_debug_diagnostics()["debug"]
    assert debug["learn_ok_count"] == 0
    assert debug["learn_skip_count"] == 0

    smartpi2.est.learn_ok_count += 2
    smartpi2.est.learn_skip_count += 1

    debug = smartpi2.get_debug_diagnostics()["debug"]
    assert debug["learn_ok_count"] == 2
    assert debug["learn_skip_count"] == 1


def test_restored_learned_model_publishes_confidence_before_active_cycle() -> None:
    """Restored learned models should publish their A/B confidence immediately."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI1",
    )
    smartpi1.est.a = 0.05899
    smartpi1.est.b = 0.000459
    smartpi1.est.learn_ok_count = 1929
    smartpi1.est.learn_ok_count_a = 517
    smartpi1.est.learn_ok_count_b = 1412
    smartpi1.est.a_meas_hist.extend([0.05899] * 31)
    smartpi1.est.b_meas_hist.extend([0.000459] * 31)
    smartpi1.est._b_hat_hist.extend([0.000459] * 20)

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI2",
        saved_state=smartpi1.save_state(),
    )

    published = build_published_diagnostics(smartpi2)

    assert published["control"]["phase"] == "Stable"
    assert published["ab_learning"]["stage"] == "monitoring"
    assert published["model"]["confidence"] == "ab_ok"


def test_save_and_load_state_restores_autocalib_and_learning_start() -> None:
    """SmartPI persistence should restore AutoCalib and learning-start state."""
    smartpi1 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPIPersist",
    )

    learning_start = datetime(2024, 1, 2, 3, 4, 5)
    snapshot_ts = time.time() - (5 * 3600)

    smartpi1.learn_win._learning_start_date = learning_start
    smartpi1._learning_start_date = learning_start
    smartpi1.autocalib._snapshot_ts = snapshot_ts
    smartpi1.autocalib._retry_count = 2
    smartpi1.autocalib._model_degraded = True
    smartpi1.autocalib._triggered_params = ["a", "deadtime_heat"]

    saved = smartpi1.save_state()

    smartpi2 = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPIRestore",
        saved_state=saved,
    )

    assert smartpi2.learning_start_dt == learning_start.isoformat()
    assert smartpi2.autocalib._snapshot_ts == snapshot_ts
    assert smartpi2.autocalib.retry_count == 2
    assert smartpi2.autocalib.model_degraded is True
    assert smartpi2.autocalib.triggered_params == ["a", "deadtime_heat"]
    assert smartpi2.autocalib.snapshot_age_h is not None


def test_reset_learning() -> None:
    """Reset should clear the learned model and the PI accumulator."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI",
    )

    smartpi.est.a = 0.02
    smartpi.est.b = 0.005
    smartpi.est.learn_ok_count = 25
    smartpi.integral = 10.0

    smartpi.reset_learning()

    assert smartpi.est.a == smartpi.est.A_INIT
    assert smartpi.est.b == smartpi.est.B_INIT
    assert smartpi.integral == 0.0
    assert smartpi.est.learn_ok_count == 0
