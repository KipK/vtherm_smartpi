"""Tests for pausing and resuming SmartPI thermal learning."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.diagnostics import (
    build_published_diagnostics,
)
from custom_components.vtherm_smartpi.smartpi.controller import PIOutputSnapshot
from custom_components.vtherm_smartpi.smartpi.deadband_output import ProportionalState
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from custom_components.vtherm_smartpi.smartpi.reference_governor_runtime import (
    ReferenceGovernorPhase,
)
from helpers import force_smartpi_stable_mode


def _snapshot() -> PIOutputSnapshot:
    return PIOutputSnapshot(1.0, 0.0, 0.0, ProportionalState(0.05, False, False, 0, None))


def _make_smartpi() -> SmartPI:
    """Return a SmartPI instance suitable for learning tests."""
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Learning enablement test",
    )


def _filter_setpoint_step(
    manager: SmartPISetpointManager,
    *,
    target: float,
    current: float,
    now: float,
    allow_disturbance_trigger: bool = True,
) -> None:
    """Advance a setpoint manager with a model-ready HEAT observation."""
    manager.filter_setpoint(
        target_temp=target,
        current_temp=current,
        hvac_mode=VThermHvacMode_HEAT,
        a=0.4,
        b=0.02,
        ext_current_temp=10.0,
        u_ref=1.0,
        deadtime_cool_s=240.0,
        deadtime_cool_reliable=True,
        tau_reliable=True,
        deadband_c=0.05,
        kp=1.0,
        next_cycle_u_ref=0.9,
        cycle_min=10.0,
        now_monotonic=now,
        allow_disturbance_trigger=allow_disturbance_trigger,
        temperature_slope_h=0.01,
    )


def test_setpoint_landing_property_tracks_authoritative_episode_only() -> None:
    """The landing gate starts after trajectory activation and excludes disturbances."""
    manager = SmartPISetpointManager("setpoint landing")

    assert manager.setpoint_landing_active is False
    _filter_setpoint_step(manager, target=21.0, current=21.0, now=0.0)
    _filter_setpoint_step(manager, target=22.0, current=21.0, now=60.0)
    # Pending braking is still the early ascent phase, not landing.
    assert manager.setpoint_landing_active is False

    _filter_setpoint_step(manager, target=22.0, current=21.7, now=120.0)
    assert manager.setpoint_landing_active is True

    governed = manager.resolve_reference_governor(
        requested_u=0.9,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=_snapshot(),
    )
    assert governed is not None
    assert governed.phase is ReferenceGovernorPhase.GOVERNED
    assert manager.setpoint_landing_active is True

    _filter_setpoint_step(manager, target=22.0, current=21.7, now=180.0)
    handoff = manager.resolve_reference_governor(
        requested_u=0.0,
        now_monotonic=180.0,
        cycle_min=10.0,
        pi_snapshot=_snapshot(),
    )
    assert handoff is not None
    assert handoff.phase is ReferenceGovernorPhase.HANDOFF
    assert manager.setpoint_landing_active is True

    for now in (480.0, 780.0):
        _filter_setpoint_step(manager, target=22.0, current=21.7, now=now)
        handoff = manager.resolve_reference_governor(
            requested_u=0.0,
            now_monotonic=now,
            cycle_min=10.0,
            pi_snapshot=_snapshot(),
        )

    assert handoff is not None
    assert handoff.handoff_ready is True
    assert manager.setpoint_landing_active is False

    disturbance = SmartPISetpointManager("disturbance")
    _filter_setpoint_step(disturbance, target=21.0, current=21.0, now=0.0)
    _filter_setpoint_step(disturbance, target=21.0, current=20.0, now=60.0)
    assert disturbance.trajectory_source == "disturbance"
    assert disturbance.setpoint_landing_active is False


def test_unconstrained_setpoint_episode_stays_blocked_until_handoff() -> None:
    """An armed episode without a prior cap still requires a real handoff."""
    manager = SmartPISetpointManager("unconstrained setpoint transient")
    _filter_setpoint_step(manager, target=21.0, current=21.0, now=0.0)
    _filter_setpoint_step(manager, target=22.0, current=21.0, now=60.0)
    _filter_setpoint_step(manager, target=22.0, current=21.7, now=120.0)

    unconstrained = manager.resolve_reference_governor(
        requested_u=0.0,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=_snapshot(),
    )
    assert unconstrained is not None
    assert unconstrained.phase is ReferenceGovernorPhase.IDLE
    assert manager.setpoint_landing_active is True

    for now in (180.0, 480.0, 780.0):
        _filter_setpoint_step(manager, target=22.0, current=22.01, now=now)
        handoff = manager.resolve_reference_governor(
            requested_u=0.0,
            now_monotonic=now,
            cycle_min=10.0,
            pi_snapshot=_snapshot(),
        )

    assert handoff is not None
    assert handoff.handoff_ready is True
    assert manager.setpoint_landing_active is False


def test_update_learning_forwards_causal_setpoint_gate_reasons() -> None:
    """Heartbeat learning receives transition and landing reasons separately."""
    smartpi = _make_smartpi()
    smartpi.learn_win.update = MagicMock(return_value=(0, 0))

    smartpi.update_learning(10.0, 20.0, 10.0, 0.5, setpoint_changed=True)

    kwargs = smartpi.learn_win.update.call_args.kwargs
    assert kwargs["a_learning_allowed"] is False
    assert kwargs["a_learning_block_reason"] == "skip: setpoint transition"

    smartpi.sp_mgr._reference_governor_authority_episode = True
    smartpi.sp_mgr._trajectory_source = "setpoint"
    smartpi.update_learning(10.0, 20.0, 10.0, 0.5)

    kwargs = smartpi.learn_win.update.call_args.kwargs
    assert kwargs["a_learning_allowed"] is False
    assert kwargs["a_learning_block_reason"] == "skip: setpoint landing"

    smartpi.sp_mgr.set_passthrough(20.0)
    smartpi.update_learning(10.0, 20.0, 10.0, 0.5)

    kwargs = smartpi.learn_win.update.call_args.kwargs
    assert kwargs["a_learning_allowed"] is True
    assert kwargs["a_learning_block_reason"] is None

    smartpi.sp_mgr._trajectory_source = "disturbance"
    smartpi.update_learning(10.0, 20.0, 10.0, 0.5)

    kwargs = smartpi.learn_win.update.call_args.kwargs
    assert kwargs["a_learning_allowed"] is True
    assert kwargs["a_learning_block_reason"] is None


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

    assert diagnostics["learning"]["enabled"] is False


def test_published_diagnostics_use_only_canonical_governor_fields() -> None:
    """Published setpoint diagnostics must not retain landing aliases."""
    diagnostics = build_published_diagnostics(_make_smartpi())

    assert {
        "governor_active",
        "governor_phase",
        "governor_reason",
        "governor_command_cap",
        "governor_coast_required",
    } <= diagnostics["setpoint"].keys()
    assert "reference_governor" in diagnostics["analysis"]
    assert "landing" not in diagnostics["analysis"]
    assert not any(key.startswith("landing_") for key in diagnostics["setpoint"])
