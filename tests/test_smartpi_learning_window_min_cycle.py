"""An active learning window is not submitted before one PWM cycle."""
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager

CYCLE_MIN = 10.0


def _setup():
    est = ABEstimator()
    est.b = 3.0e-4
    est.b_meas_hist.extend([3.0e-4] * AB_A_SOFT_GATE_MIN_B)
    dt = MagicMock()
    dt.deadtime_heat_reliable = True
    dt.deadtime_heat_s = 60.0
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = []
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return LearningWindowManager("t"), est, dt, gov


def _run(minutes: int, min_active_window_min: float):
    """Fast fine-resolution heating ramp at full power, one update per minute.

    Returns the window duration (min) at its submission, or None.
    """
    win, est, dt, gov = _setup()
    reasons = []
    for k in range(minutes + 1):
        now = 1000.0 + 60.0 * k
        temp = round(19.0 + 0.03 * k, 2)  # 1.8 C/h: robust within a few minutes
        dt.tin_history.append((now, temp))
        win.update(
            dt_min=1.0,
            current_temp=temp,
            ext_temp=5.0,
            u_active=1.0,
            estimator=est,
            dt_est=dt,
            governance=gov,
            now=now,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
            hvac_mode=VThermHvacMode_HEAT,
            target_temp=21.0,
            raw_temp=temp,
            min_active_window_min=min_active_window_min,
        )
        reasons.append(est.learn_last_reason)
        if est.a_meas_hist:
            # Each update accounts for one minute, the first one included.
            return k + 1, reasons
    return None, reasons


def test_active_window_waits_for_one_cycle():
    submitted_at, reasons = _run(30, CYCLE_MIN)
    assert submitted_at is not None
    assert submitted_at >= CYCLE_MIN
    assert any("active_window_shorter_than_cycle" in r for r in reasons)


def test_without_cycle_the_active_window_is_submitted_earlier():
    early, _ = _run(30, 0.0)
    gated, _ = _run(30, CYCLE_MIN)
    assert early is not None and gated is not None
    assert early < CYCLE_MIN <= gated


def test_bootstrap_minimum_is_capped_by_the_heating_phases():
    """A bootstrap heating phase may be shorter than one cycle: half the median phase is enough."""
    win = LearningWindowManager("t")
    lag = win.response_lag
    lag._durations.extend([600.0, 600.0, 600.0])  # 10 min heating phases
    assert win._bootstrap_active_window_min(CYCLE_MIN * 2, is_hysteresis=True) == pytest.approx(5.0)
    assert win._bootstrap_active_window_min(4.0, is_hysteresis=True) == 4.0
    assert win._bootstrap_active_window_min(CYCLE_MIN * 2, is_hysteresis=False) == CYCLE_MIN * 2


def test_bootstrap_minimum_without_measured_phases_stays_the_cycle():
    win = LearningWindowManager("t")
    assert win._bootstrap_active_window_min(CYCLE_MIN, is_hysteresis=True) == CYCLE_MIN
