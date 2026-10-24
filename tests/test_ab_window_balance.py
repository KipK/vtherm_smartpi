"""Tests for the integral balance pools of learning windows without a robust slope."""
import math
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.ab_window_balance import (
    ABWindowBalance,
    passive_gradient_ok,
    pooled_relative_uncertainty,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_BALANCE_MAX_REL_UNC,
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager

TICK_S = 60.0
T_EXT = 5.0
TARGET = 20.0


def _quantise(value: float, step: float) -> float:
    return round(round(value / step) * step, 3)


def _feed(bal, t0, minutes, u, temp_at, step=0.2, t_ext=T_EXT):
    """Feed one tick per minute of a sensor publishing on change."""
    now = t0
    for _ in range(int(minutes)):
        bal.observe(now, TICK_S, u, _quantise(temp_at(now), step), t_ext)
        now += TICK_S
    return now


def _passive(b, t_start):
    """Exact 1R1C passive response (time in seconds)."""
    return lambda t: T_EXT + (t_start - T_EXT) * math.exp(-b * t / 60.0)


def _active(a, b, t_start, u=1.0, t_ext=T_EXT):
    """Exact 1R1C active response (time in seconds)."""
    t_eq = t_ext + a * u / b
    return lambda t: t_eq + (t_start - t_eq) * math.exp(-b * t / 60.0)


def _estimator(b=None, b_samples=0):
    est = ABEstimator()
    if b is not None:
        est.b = b
    est.b_meas_hist.extend([est.b] * b_samples)
    return est


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_pooled_relative_uncertainty_scales_with_segments():
    one = pooled_relative_uncertainty(1, 0.4, 0.2)
    assert one == pytest.approx(0.2 / math.sqrt(6.0) / 0.4)
    assert pooled_relative_uncertainty(4, 1.6, 0.2) == pytest.approx(one / 2.0)
    assert pooled_relative_uncertainty(0, 1.0, 0.2) == math.inf
    assert pooled_relative_uncertainty(2, 0.0, 0.2) == math.inf


def test_passive_gradient_gate_is_mirrored_in_cool():
    assert passive_gradient_ok(VThermHvacMode_HEAT, 20.0, 5.0, 20.0)
    assert not passive_gradient_ok(VThermHvacMode_HEAT, 20.0, 19.0, 20.0)
    assert not passive_gradient_ok(VThermHvacMode_HEAT, 21.0, 18.5, 20.0)
    assert passive_gradient_ok(VThermHvacMode_COOL, 24.0, 32.0, 24.0)
    assert not passive_gradient_ok(VThermHvacMode_COOL, 24.0, 25.0, 24.0)


# ---------------------------------------------------------------------------
# Passive (b) pooling
# ---------------------------------------------------------------------------

def test_coarse_passive_windows_are_pooled_into_an_exact_b():
    b_true = 2.0e-4
    bal = ABWindowBalance("t")
    est = _estimator()
    now = 0.0
    submitted = 0
    for k in range(6):
        temp_at = _passive(b_true, 20.03 - 0.07 * k)
        start = now
        now = _feed(bal, now, 180, 0.0, lambda t, s=start, f=temp_at: f(t - s))
        n_before = len(est.b_meas_hist)
        bal.close(now=now, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
        submitted += len(est.b_meas_hist) - n_before
    assert submitted >= 1
    assert est.diag_dTdt_method == "balance_pool"
    for value in est.b_meas_hist:
        assert value == pytest.approx(b_true, rel=0.05)


def test_single_coarse_segment_is_not_precise_enough():
    bal = ABWindowBalance("t")
    est = _estimator()
    _feed(bal, 0.0, 120, 0.0, _passive(2.0e-4, 20.03))
    bal.close(now=120 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert len(est.b_meas_hist) == 0
    assert bal.pool_size("b") == 1


def test_passive_segment_rejected_without_gradient():
    bal = ABWindowBalance("t")
    est = _estimator()
    _feed(bal, 0.0, 180, 0.0, _passive(2.0e-3, 20.0), t_ext=18.5)
    bal.close(now=180 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert bal.pool_size("b") == 0


# ---------------------------------------------------------------------------
# Active (a) pooling
# ---------------------------------------------------------------------------

def test_active_segment_gives_a_once_b_is_available():
    a_true, b_true = 0.025, 3.0e-4
    bal = ABWindowBalance("t")
    est = _estimator(b=b_true, b_samples=AB_A_SOFT_GATE_MIN_B)
    _feed(bal, 0.0, 40, 1.0, _active(a_true, b_true, 19.71))
    bal.close(now=40 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert len(est.a_meas_hist) == 1
    assert est.a_meas_hist[0] == pytest.approx(a_true, rel=0.05)


def test_active_pool_waits_for_b_samples():
    a_true, b_true = 0.025, 3.0e-4
    bal = ABWindowBalance("t")
    est = _estimator(b=b_true, b_samples=AB_A_SOFT_GATE_MIN_B - 1)
    _feed(bal, 0.0, 40, 1.0, _active(a_true, b_true, 19.71))
    bal.close(now=40 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert len(est.a_meas_hist) == 0
    assert bal.pool_size("a") == 1

    est.b_meas_hist.append(b_true)
    now = _feed(bal, 40 * TICK_S, 40, 1.0, _active(a_true, b_true, 19.71))
    bal.close(now=now, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert len(est.a_meas_hist) == 1
    assert est.a_meas_hist[0] == pytest.approx(a_true, rel=0.05)
    assert bal.pool_size("a") == 0


def test_cool_mode_active_segment_is_falling():
    a_true, b_true = -0.025, 3.0e-4
    bal = ABWindowBalance("t")
    est = _estimator(b=b_true, b_samples=AB_A_SOFT_GATE_MIN_B)
    est.ensure_hvac_mode(VThermHvacMode_COOL)
    temp_at = _active(a_true, b_true, 24.29, t_ext=30.0)
    now = 0.0
    for _ in range(40):
        bal.observe(now, TICK_S, 1.0, _quantise(temp_at(now), 0.2), 30.0)
        now += TICK_S
    bal.close(now=now, hvac_mode=VThermHvacMode_COOL, target_temp=24.0, estimator=est)
    assert len(est.a_meas_hist) == 1
    assert est.a_meas_hist[0] == pytest.approx(a_true, rel=0.05)


# ---------------------------------------------------------------------------
# Rejection rules
# ---------------------------------------------------------------------------

def test_reversed_step_rejects_the_window():
    bal = ABWindowBalance("t")
    est = _estimator(b=3.0e-4, b_samples=AB_A_SOFT_GATE_MIN_B)
    values = [19.6] * 5 + [19.8] * 10 + [19.6] * 2 + [19.8] * 10 + [20.0] * 10
    for i, v in enumerate(values):
        bal.observe(i * TICK_S, TICK_S, 1.0, v, T_EXT)
    bal.close(now=len(values) * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert bal.pool_size("a") == 0
    assert len(est.a_meas_hist) == 0


def test_short_segment_is_rejected():
    bal = ABWindowBalance("t")
    est = _estimator(b=3.0e-4, b_samples=AB_A_SOFT_GATE_MIN_B)
    values = [19.6] * 3 + [19.8] * 5 + [20.0] * 3
    for i, v in enumerate(values):
        bal.observe(i * TICK_S, TICK_S, 1.0, v, T_EXT)
    bal.close(now=len(values) * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert bal.pool_size("a") == 0


def test_steps_after_a_power_change_are_ignored():
    a_true, b_true = 0.025, 3.0e-4
    temp_at = _active(a_true, b_true, 19.71)
    reference = ABWindowBalance("ref")
    _feed(reference, 0.0, 40, 1.0, temp_at)

    bal = ABWindowBalance("t")
    now = _feed(bal, 0.0, 40, 1.0, temp_at)
    # Power drops but the actuator lag keeps the room rising: not credited.
    for v in (20.4, 20.6):
        bal.observe(now, TICK_S, 0.0, v, T_EXT)
        now += TICK_S
    assert bal._anchors == reference._anchors


def test_mode_change_clears_pools():
    bal = ABWindowBalance("t")
    est = _estimator()
    _feed(bal, 0.0, 120, 0.0, _passive(2.0e-4, 20.03))
    bal.close(now=120 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert bal.pool_size("b") == 1
    _feed(bal, 0.0, 10, 0.0, lambda t: 24.0, t_ext=30.0)
    bal.close(now=200 * TICK_S, hvac_mode=VThermHvacMode_COOL, target_temp=24.0, estimator=est)
    assert bal.pool_size("b") == 0


def test_old_segments_expire():
    bal = ABWindowBalance("t")
    est = _estimator()
    _feed(bal, 0.0, 120, 0.0, _passive(2.0e-4, 20.03))
    bal.close(now=120 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    late = 2 * 86400.0
    _feed(bal, late, 120, 0.0, lambda t: _passive(2.0e-4, 20.03)(t - late))
    bal.close(now=late + 120 * TICK_S, hvac_mode=VThermHvacMode_HEAT, target_temp=TARGET, estimator=est)
    assert bal.pool_size("b") == 1


def test_emission_threshold_constant_is_stricter_than_ols():
    # OLS accepts a slope with t >= 2.5, i.e. a relative standard error up to 0.40.
    assert AB_BALANCE_MAX_REL_UNC < 1.0 / 2.5


# ---------------------------------------------------------------------------
# LearningWindowManager integration
# ---------------------------------------------------------------------------

def _dt_est():
    dt = MagicMock()
    dt.deadtime_heat_reliable = True
    dt.deadtime_heat_s = 60.0
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = []
    return dt


def _governance():
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return gov


def _window_update(win, est, dt, gov, now, temp, u, t_heat_start=None):
    dt.tin_history.append((now, temp))
    win.update(
        dt_min=1.0,
        current_temp=temp,
        ext_temp=T_EXT,
        u_active=u,
        estimator=est,
        dt_est=dt,
        governance=gov,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=t_heat_start,
        t_cool_episode_start=None,
        hvac_mode=VThermHvacMode_HEAT,
        target_temp=TARGET,
        raw_temp=temp,
    )


def _run_passive_window(win, est, dt, gov, now, minutes=150, b_true=2.0e-4, t_start=20.03):
    temp_at = _passive(b_true, t_start)
    start = now
    for _ in range(minutes):
        _window_update(win, est, dt, gov, now, _quantise(temp_at(now - start), 0.2), 0.0)
        now += TICK_S
    return now


def test_window_closed_by_a_gate_is_pooled():
    win, est, dt, gov = LearningWindowManager("t"), _estimator(), _dt_est(), _governance()
    now = _run_passive_window(win, est, dt, gov, 1000.0)
    assert win.active
    # Heating restarts: the dead time gate closes the passive window.
    _window_update(win, est, dt, gov, now, dt.tin_history[-1][1], 1.0, t_heat_start=now)
    assert not win.active
    assert win._balance.pool_size("b") == 1
    outcomes = dict((r, n) for r, n in win.outcomes_summary(now)["b"])
    assert outcomes == {"skip: deadtime window": 1, "pool: segment added": 1}


def test_external_reset_discards_the_window():
    win, est, dt, gov = LearningWindowManager("t"), _estimator(), _dt_est(), _governance()
    now = _run_passive_window(win, est, dt, gov, 1000.0)
    win.reset()
    _window_update(win, est, dt, gov, now, dt.tin_history[-1][1], 1.0, t_heat_start=now)
    assert win._balance.pool_size("b") == 0


def test_reset_all_clears_pools():
    win, est, dt, gov = LearningWindowManager("t"), _estimator(), _dt_est(), _governance()
    now = _run_passive_window(win, est, dt, gov, 1000.0)
    _window_update(win, est, dt, gov, now, dt.tin_history[-1][1], 1.0, t_heat_start=now)
    assert win._balance.pool_size("b") == 1
    win.reset_all()
    assert win._balance.pool_size("b") == 0


def test_window_submitted_by_ols_is_not_pooled():
    win, est, dt, gov = LearningWindowManager("t"), _estimator(), _dt_est(), _governance()
    # Fine resolution: the OLS slope becomes robust and submits the window.
    temp_at = _passive(1.0e-3, 20.0)
    now = 1000.0
    for _ in range(240):
        _window_update(win, est, dt, gov, now, _quantise(temp_at(now - 1000.0), 0.01), 0.0)
        now += TICK_S
        if est.b_meas_hist:
            break
    assert len(est.b_meas_hist) == 1
    assert est.diag_dTdt_method == "ols_ttest"
    assert win._balance.pool_size("b") == 0
