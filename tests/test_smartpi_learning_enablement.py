"""Tests for pausing and resuming SmartPI thermal learning."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.diagnostics import (
    build_published_diagnostics,
)
from helpers import force_smartpi_stable_mode


def _make_smartpi() -> SmartPI:
    """Return a SmartPI instance suitable for learning tests."""
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Learning enablement test",
    )


def test_disabled_learning_blocks_ab_updates() -> None:
    """Pausing learning must block the A/B learning entry point."""
    smartpi = _make_smartpi()
    smartpi.learn_win.update = MagicMock(return_value=(0, 0))

    smartpi.set_learning_enabled(False)
    smartpi.update_learning(10.0, 20.0, 10.0, 0.5)

    smartpi.learn_win.update.assert_not_called()

    smartpi.set_learning_enabled(True)
    smartpi.update_learning(10.0, 20.0, 10.0, 0.5)

    smartpi.learn_win.update.assert_called_once()


def test_disabled_learning_blocks_stable_deadtime_updates() -> None:
    """Stable regulation must not feed dead-time learning while paused."""
    smartpi = _make_smartpi()
    force_smartpi_stable_mode(smartpi)
    smartpi.set_learning_enabled(False)
    smartpi.dt_est.update = MagicMock()

    smartpi.calculate(20.0, 19.0, 10.0, 0.0, VThermHvacMode_HEAT)

    smartpi.dt_est.update.assert_not_called()


def test_disabled_learning_blocks_hysteresis_deadtime_updates() -> None:
    """Hysteresis regulation must not feed dead-time learning while paused."""
    smartpi = _make_smartpi()
    smartpi.set_learning_enabled(False)
    smartpi.dt_est.update = MagicMock()

    smartpi.calculate(20.0, 19.0, 10.0, 0.0, VThermHvacMode_HEAT)

    assert smartpi.on_percent > 0.0
    smartpi.dt_est.update.assert_not_called()


def test_learning_change_starts_with_clean_observations() -> None:
    """Each flag transition must discard transients but preserve learned data."""
    smartpi = _make_smartpi()
    smartpi.est.a = 0.015
    smartpi.est.b = 0.003
    smartpi.est.a_meas_hist.extend([0.015, 0.016])
    smartpi.dt_est.deadtime_heat_s = 180.0
    smartpi.dt_est.deadtime_heat_reliable = True
    smartpi.dt_est._history_heat.extend([120.0, 240.0])
    smartpi.learn_win._active = True
    smartpi.dt_est.state = "WAITING_HEAT_RESPONSE"
    smartpi.dt_est.heat_start_time = 100.0
    smartpi.dt_est._tin_history.append((100.0, 19.0))
    smartpi._t_heat_episode_start = 100.0

    smartpi.set_learning_enabled(False)

    assert smartpi.est.a == 0.015
    assert smartpi.est.b == 0.003
    assert list(smartpi.est.a_meas_hist) == [0.015, 0.016]
    assert smartpi.dt_est.deadtime_heat_s == 180.0
    assert list(smartpi.dt_est._history_heat) == [120.0, 240.0]
    assert smartpi.learn_win_active is False
    assert smartpi.dt_est.state == "OFF"
    assert smartpi.dt_est.heat_start_time is None
    assert not smartpi.dt_est.tin_history
    assert smartpi._t_heat_episode_start is None

    smartpi.learn_win._active = True
    smartpi.dt_est.state = "WAITING_COOL_RESPONSE"
    smartpi.dt_est.cool_start_time = 200.0
    smartpi.dt_est._tin_history.append((200.0, 20.0))
    smartpi._committed_on_percent = 0.8

    smartpi.set_learning_enabled(True)

    assert smartpi.learning_enabled is True
    assert smartpi.learn_win_active is False
    assert smartpi.dt_est.state == "OFF"
    assert smartpi.dt_est.cool_start_time is None
    assert not smartpi.dt_est.tin_history
    assert smartpi.dt_est.last_power == 0.8
    assert smartpi.dt_est.deadtime_heat_s == 180.0

    smartpi.dt_est.update(
        now=300.0,
        tin=20.0,
        sp=21.0,
        u_applied=0.8,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.dt_est.heat_start_time is None


def test_learning_flag_persistence_is_backward_compatible() -> None:
    """The flag must persist while legacy states default to enabled."""
    source = _make_smartpi()
    source.set_learning_enabled(False)
    saved = source.save_state()

    restored = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Restored learning state",
        saved_state=saved,
    )
    assert restored.learning_enabled is False

    saved.pop("learning_enabled")
    legacy = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Legacy learning state",
        saved_state=saved,
    )
    assert legacy.learning_enabled is True


def test_published_diagnostics_expose_learning_enabled() -> None:
    """Published A/B diagnostics must expose the learning flag."""
    smartpi = _make_smartpi()
    smartpi.set_learning_enabled(False)

    diagnostics = build_published_diagnostics(smartpi)

    assert diagnostics["ab_learning"]["enabled"] is False
