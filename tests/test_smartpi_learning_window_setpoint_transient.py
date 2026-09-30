"""Tests for the local ON/A learning eligibility gate."""
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import (
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import (
    LearningWindowManager,
)


def _make_estimator():
    estimator = MagicMock()
    estimator.learn_skip_count = 0
    estimator.learn_last_reason = ""
    return estimator


def _make_dt_estimator():
    dt_estimator = MagicMock()
    dt_estimator.deadtime_heat_reliable = False
    dt_estimator.deadtime_heat_s = None
    dt_estimator.deadtime_cool_reliable = False
    dt_estimator.deadtime_cool_s = None
    dt_estimator.tin_history = [
        (0.0, 20.0),
        (60.0, 20.1),
        (120.0, 20.2),
    ]
    return dt_estimator


def _make_governance():
    governance = MagicMock()
    governance.decide_update.return_value = (
        GovernanceDecision.ADAPT_ON,
        FreezeReason.NONE,
    )
    return governance


def _update(
    window,
    estimator,
    dt_estimator,
    *,
    u_active,
    current_temp=20.2,
    ext_temp=10.0,
    now=120.0,
    **kwargs,
):
    return window.update(
        dt_min=1.0,
        current_temp=current_temp,
        ext_temp=ext_temp,
        u_active=u_active,
        setpoint_changed=False,
        estimator=estimator,
        dt_est=dt_estimator,
        governance=_make_governance(),
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=None,
        t_cool_episode_start=None,
        hvac_mode=VThermHvacMode_HEAT,
        **kwargs,
    )


def test_blocked_on_resets_active_window_and_prevents_a_learning():
    """A blocked ON tick resets an active window and cannot submit A."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(None, "insufficient_samples", 0),
    ):
        _update(
            window,
            estimator,
            dt_estimator,
            u_active=0.8,
            now=60.0,
        )

    assert window.active

    _update(
        window,
        estimator,
        dt_estimator,
        u_active=0.8,
        a_learning_allowed=False,
        now=120.0,
    )

    assert not window.active
    assert estimator.learn_skip_count == 1
    assert estimator.learn_last_reason == "skip: setpoint transient"
    estimator.learn.assert_not_called()


def test_blocked_a_window_cannot_submit_on_transition_to_off():
    """An A window is rejected even when the blocking tick is already OFF."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(None, "insufficient_samples", 0),
    ):
        _update(
            window,
            estimator,
            dt_estimator,
            u_active=0.8,
            now=60.0,
        )

    assert window.active

    _update(
        window,
        estimator,
        dt_estimator,
        u_active=0.0,
        a_learning_allowed=False,
        now=120.0,
    )

    assert not window.active
    assert estimator.learn_last_reason == "skip: setpoint transient"
    estimator.learn.assert_not_called()


def test_blocked_on_does_not_start_a_window():
    """A blocked ON tick cannot open a new learning window."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    _update(
        window,
        estimator,
        dt_estimator,
        u_active=0.8,
        a_learning_allowed=False,
    )

    assert not window.active
    assert estimator.learn_skip_count == 1
    assert estimator.learn_last_reason == "skip: setpoint transient"
    estimator.learn.assert_not_called()


def test_blocked_off_still_submits_b_learning():
    """Blocking A must leave the normal OFF/B learning path untouched."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(-0.1, "test_slope", 3),
    ):
        _update(
            window,
            estimator,
            dt_estimator,
            u_active=0.0,
            current_temp=19.0,
            a_learning_allowed=False,
        )

    estimator.learn.assert_called_once()
    assert estimator.learn.call_args.kwargs["u"] == 0.0


def test_allowed_on_submits_a_learning_with_existing_defaults():
    """An eligible ON tick keeps the existing A learning behavior."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(0.1, "test_slope", 3),
    ):
        _update(window, estimator, dt_estimator, u_active=0.8)

    estimator.learn.assert_called_once()
    assert estimator.learn.call_args.kwargs["u"] == 0.8


def test_calibration_bypasses_blocked_on_gate():
    """Calibration can still submit ON/A learning when the gate is closed."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(0.1, "test_slope", 3),
    ):
        _update(
            window,
            estimator,
            dt_estimator,
            u_active=0.8,
            a_learning_allowed=False,
            is_calibrating=True,
        )

    estimator.learn.assert_called_once()


def test_blocked_on_uses_supplied_diagnostic_reason():
    """A caller-provided reason is preserved in the learning diagnostics."""
    window = LearningWindowManager("test")
    estimator = _make_estimator()
    dt_estimator = _make_dt_estimator()

    _update(
        window,
        estimator,
        dt_estimator,
        u_active=0.8,
        a_learning_allowed=False,
        a_learning_block_reason="skip: trajectory active",
    )

    assert estimator.learn_last_reason == "skip: trajectory active"
