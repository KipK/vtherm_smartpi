"""Tests for heat-mode B learning guards."""
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import (
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager


def _make_estimator():
    est = MagicMock()
    est.learn_skip_count = 0
    est.learn_last_reason = ""
    est.learn_ok_count_b = 0
    return est


def _make_dt_est():
    dt = MagicMock()
    dt.deadtime_heat_reliable = False
    dt.deadtime_heat_s = None
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = [(0.0, 20.0), (60.0, 19.9), (120.0, 19.8)]
    return dt


def _make_governance():
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return gov


def _update_once(
    win,
    est,
    *,
    current_temp,
    ext_temp,
    target_temp,
):
    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(-0.1, "test_slope", 3),
    ):
        win.update(
            dt_min=1.0,
            current_temp=current_temp,
            ext_temp=ext_temp,
            u_active=0.0,
            setpoint_changed=False,
            estimator=est,
            dt_est=_make_dt_est(),
            governance=_make_governance(),
            learning_resume_ts=None,
            now=120.0,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
            hvac_mode=VThermHvacMode_HEAT,
            target_temp=target_temp,
        )


def test_b_learning_skipped_when_outdoor_is_too_warm_in_heat():
    """B learning must not run when heat loss gradient is not present."""
    win = LearningWindowManager("test")
    est = _make_estimator()

    _update_once(
        win,
        est,
        current_temp=20.0,
        ext_temp=21.0,
        target_temp=19.0,
    )

    assert est.learn_ok_count_b == 0
    assert est.learn_last_reason == (
        "skip: b heat mode - insufficient heat-loss gradient"
    )
    est.learn.assert_not_called()


def test_b_learning_skipped_when_outdoor_is_close_to_target_in_heat():
    """B learning must not run when outdoor temperature is too close to target."""
    win = LearningWindowManager("test")
    est = _make_estimator()

    _update_once(
        win,
        est,
        current_temp=21.0,
        ext_temp=18.8,
        target_temp=20.0,
    )

    assert est.learn_ok_count_b == 0
    assert est.learn_last_reason == "skip: b heat mode - outdoor too close to target"
    est.learn.assert_not_called()


def test_b_learning_allowed_when_real_heat_loss_gradient_exists():
    """B learning remains eligible when the heat-loss context is valid."""
    win = LearningWindowManager("test")
    est = _make_estimator()

    _update_once(
        win,
        est,
        current_temp=21.0,
        ext_temp=8.0,
        target_temp=20.0,
    )

    est.learn.assert_called_once_with(
        dT_int_per_min=-0.1,
        u=0.0,
        t_int=21.0,
        t_ext=8.0,
    )
    assert est.learn_last_reason not in {
        "skip: b heat mode - insufficient heat-loss gradient",
        "skip: b heat mode - outdoor too close to target",
    }
