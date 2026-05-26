"""Tests for ON-phase A learning slope eligibility."""
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.smartpi.const import (
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager


def _make_estimator():
    est = MagicMock()
    est.learn_skip_count = 0
    est.learn_last_reason = ""
    est.learn_ok_count = 0
    est.learn_ok_count_b = 0
    est.diag_dTdt_method = "init"
    return est


def _make_dt_est():
    dt = MagicMock()
    dt.deadtime_heat_reliable = False
    dt.deadtime_heat_s = None
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = [
        (0.0, 20.00),
        (60.0, 20.02),
        (120.0, 20.05),
        (180.0, 20.08),
        (240.0, 20.12),
        (300.0, 20.16),
    ]
    return dt


def _make_governance():
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return gov


def _start_on_window(win, est, dt_est, gov):
    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        return_value=(None, "insufficient_samples", 0),
    ):
        win.update(
            dt_min=1.0,
            current_temp=20.0,
            ext_temp=10.0,
            u_active=0.5,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=60.0,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )


def test_a_learning_skips_when_final_on_slope_is_not_robust_in_near_band():
    """Near-band ADAPT_ON must not publish A with a non-robust final ON slope."""
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()
    _start_on_window(win, est, dt_est, gov)

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        side_effect=[
            (0.10, "ols_ttest", 6),
            (None, "slope_not_significant", 6),
        ],
    ):
        win.update(
            dt_min=1.0,
            current_temp=20.2,
            ext_temp=10.0,
            u_active=0.5,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=120.0,
            in_deadband=False,
            in_near_band=True,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )

    est.learn.assert_not_called()
    assert est.learn_skip_count == 1
    assert est.learn_last_reason == "skip: ON slope not robust (slope_not_significant)"
    assert not win.active


def test_a_learning_calibration_keeps_simple_slope_fallback():
    """Calibration can use the simple slope fallback when the final ON slope is not robust."""
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()
    _start_on_window(win, est, dt_est, gov)

    with patch(
        "custom_components.vtherm_smartpi.smartpi.ab_estimator.ABEstimator.robust_dTdt_per_min",
        side_effect=[
            (0.10, "ols_ttest", 6),
            (None, "slope_not_significant", 6),
        ],
    ):
        win.update(
            dt_min=1.0,
            current_temp=20.2,
            ext_temp=10.0,
            u_active=0.5,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=120.0,
            in_deadband=False,
            in_near_band=True,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
            is_calibrating=True,
        )

    est.learn.assert_called_once()
    assert est.diag_dTdt_method == "fallback_simple"
