"""Passive window after a power cut: the settling wait of the response model, one rule for every stage, and the
residual delivered power counted in b."""
import math
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    FreezeReason,
    GovernanceDecision,
    U_OFF_MAX,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.response_model import (
    CONFIDENCE_ASSUMED,
    CONFIDENCE_MEASURED,
)

B_PER_MIN = 3.0e-4
A_PER_MIN = 0.05
T_EXT = 5.0
THETA_S, TAU_S = 120.0, 300.0
TICK_S = 60.0
ON_TICKS = 60


class _Model:
    def __init__(self, values):
        self.v = values

    def values(self, mode):
        return self.v


def _run_cut(*, model, a_known=False, is_hysteresis=False, hvac_mode=VThermHvacMode_HEAT, off_ticks=120):
    """Heat for ON_TICKS ticks, then cut; return (cut time, passive window starts, reasons, estimator)."""
    est = ABEstimator()
    est.b = B_PER_MIN
    est.b_meas_hist.extend([B_PER_MIN] * AB_A_SOFT_GATE_MIN_B)
    sign = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
    if a_known:
        est.a, est.learn_ok_count_a, est.learn_ok_count_b = sign * A_PER_MIN, 1, 1
    dt = MagicMock()
    dt.tin_history = []
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    win = LearningWindowManager("t")
    win.response_model = _Model(model)
    t_ext = 35.0 if hvac_mode == VThermHvacMode_COOL else T_EXT
    target = 24.0 if hvac_mode == VThermHvacMode_COOL else 21.0
    now, temp = 1000.0, 20.0
    cut_ts = 1000.0 + ON_TICKS * TICK_S
    starts, reasons = [], []
    for k in range(ON_TICKS + off_ticks):
        now += TICK_S
        u = 1.0 if k < ON_TICKS else 0.0
        temp += sign * (0.01 if u > 0 else -0.005)
        reading = round(temp, 2)
        dt.tin_history.append((now, reading))
        win.temperatures.raw = dt.tin_history
        win.update(
            dt_min=TICK_S / 60.0, current_temp=reading, ext_temp=t_ext, u_active=u,
            estimator=est, governance=gov, now=now, is_hysteresis=is_hysteresis,
            hvac_mode=hvac_mode, target_temp=target, raw_temp=reading,
        )
        reasons.append(est.learn_last_reason)
        # Passive windows only: an active window may still be open on the cut tick.
        if win.active and win._u_first is not None and win._u_first < U_OFF_MAX:
            starts.append(win.start_ts)
    return cut_ts, starts, reasons, est


def _model(confidence=CONFIDENCE_MEASURED):
    return {"theta_s": THETA_S, "tau_e_s": TAU_S, "source": "pulse", "confidence": confidence}


@pytest.mark.parametrize("is_hysteresis", [False, True])
def test_passive_window_waits_the_emitter_lag_in_every_stage(is_hysteresis):
    cut_ts, starts, reasons, _ = _run_cut(model=_model(), is_hysteresis=is_hysteresis)
    assert "skip: response transient (b)" in reasons
    assert min(starts) == pytest.approx(cut_ts + THETA_S + TAU_S)


def test_assumed_split_waits_s_after_the_cut():
    cut_ts, starts, _, _ = _run_cut(model=_model(CONFIDENCE_ASSUMED))
    assert min(starts) == pytest.approx(cut_ts + THETA_S + TAU_S)


def _b_observations(est, sign):
    return [(v, o) for v, o in zip(est.b_meas_hist, est._b_obs_hist) if o is not None and o[3] == sign]


@pytest.mark.parametrize("hvac_mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_b_counts_the_residual_delivered_power(hvac_mode):
    """An hour of full power, then a cut: the window opens at e^-1 of the delivered power, which keeps decaying."""
    sign = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
    _, _, _, est = _run_cut(model=_model(), a_known=True, hvac_mode=hvac_mode, off_ticks=60)
    observations = _b_observations(est, sign)
    assert observations
    for value, (d_t_dt, delta, power, *_) in observations:
        assert 0.0 < power < math.exp(-1.0)
        assert value == pytest.approx((est.a * power - d_t_dt) / delta)
        assert value > -d_t_dt / delta


def test_no_gate_outside_the_learning_stage_without_a_model():
    cut_ts, starts, reasons, _ = _run_cut(model=None)
    assert "skip: response transient (b)" not in reasons
    # Opens within a few ticks once the active window has closed.
    assert min(starts) < cut_ts + 5 * TICK_S


def test_learning_stage_without_a_model_opens_no_passive_window():
    _, starts, reasons, _ = _run_cut(model=None, is_hysteresis=True)
    assert not starts
    assert reasons[-1] == "skip: learning stage - response model not yet published"


# -- delivered power of the first windows after a cut when the model comes later ------------------------------------
PRE_TICKS = 10           # rest before the heating


def _residual(model, t0, t1):
    """Delivered power over [t0, t1] after the cut of a heating of ON_TICKS ticks from a long rest (filter of the
    model over the whole command)."""
    theta, tau = model["theta_s"], model["tau_e_s"]
    on = 1000.0 + PRE_TICKS * TICK_S
    cut = on + ON_TICKS * TICK_S
    peak = 1.0 - math.exp(-(cut - on) / tau)
    return peak * tau * (math.exp(-(t0 - cut - theta) / tau) - math.exp(-(t1 - cut - theta) / tau)) / (t1 - t0)


def _run_late_model(publish_tick, models, off_ticks=90):
    """Rest, heat, cut; the response model is published at publish_tick (learning stage), and may change later
    (``models``: tick -> model). Returns the estimator and the window."""
    est = ABEstimator()
    est.b = B_PER_MIN
    est.b_meas_hist.extend([B_PER_MIN] * AB_A_SOFT_GATE_MIN_B)
    est.a, est.learn_ok_count_a, est.learn_ok_count_b = A_PER_MIN, 1, 1
    dt = MagicMock()
    dt.tin_history = []
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    win = LearningWindowManager("t")
    win.response_model = _Model(None)
    now, temp = 1000.0, 20.0
    for k in range(PRE_TICKS + ON_TICKS + off_ticks):
        now += TICK_S
        if k == publish_tick:
            win.response_model = _Model(models[k])
        elif k in models:
            win.response_model.v = models[k]
        u = 1.0 if PRE_TICKS <= k < PRE_TICKS + ON_TICKS else 0.0
        temp += 0.01 if u > 0 else -0.005
        reading = round(temp, 2)
        dt.tin_history.append((now, reading))
        win.temperatures.raw = dt.tin_history
        win.update(
            dt_min=TICK_S / 60.0, current_temp=reading, ext_temp=T_EXT, u_active=u,
            estimator=est, governance=gov, now=now, is_hysteresis=True,
            hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=reading,
        )
    return est, win


def test_model_published_after_the_cut_counts_the_residual_power_of_the_first_window():
    """The filter followed the applied power before the model: at its publication it is rebuilt over the commands
    applied before (the heating), not restarted from the cut (0)."""
    publish = PRE_TICKS + ON_TICKS + 10
    est, _ = _run_late_model(publish, {PRE_TICKS + ON_TICKS + 10: _model()})
    observations = _b_observations(est, 1.0)
    assert observations
    value, (d_t_dt, delta, power, _, t0, t1) = observations[0]
    assert t0 == pytest.approx(1000.0 + publish * TICK_S)
    assert power == pytest.approx(_residual(_model(), t0, t1), rel=1e-6)
    assert power > 0.01
    assert value == pytest.approx((A_PER_MIN * power - d_t_dt) / delta)


def _gov():
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return gov


def test_b_measurements_follow_a_change_of_the_response_model():
    """A change of the model recomputes the delivered power of the stored b windows from the command history, once."""
    publish = PRE_TICKS + ON_TICKS + 10
    slower = {"theta_s": 300.0, "tau_e_s": 900.0, "source": "pulse", "confidence": CONFIDENCE_MEASURED}
    est, win = _run_late_model(publish, {publish: _model()}, off_ticks=40)
    first = _b_observations(est, 1.0)
    assert first
    est.replay_b_power = MagicMock(wraps=est.replay_b_power)
    win.response_model.v = slower
    now = 1000.0 + (PRE_TICKS + ON_TICKS + 40) * TICK_S
    for _ in range(3):
        now += TICK_S
        win.update(
            dt_min=TICK_S / 60.0, current_temp=19.0, ext_temp=T_EXT, u_active=0.0, estimator=est,
            governance=_gov(), now=now, is_hysteresis=True, hvac_mode=VThermHvacMode_HEAT, target_temp=21.0,
        )
    est.replay_b_power.assert_called_once()
    replayed = _b_observations(est, 1.0)[: len(first)]
    for (_, old), (value, (d_t_dt, delta, power, _, t0, t1)) in zip(first, replayed):
        assert power == pytest.approx(_residual(slower, t0, t1), rel=1e-6)
        assert power > old[2]
        assert value == pytest.approx((A_PER_MIN * power - d_t_dt) / delta)


def test_without_a_model_the_b_windows_count_the_applied_power():
    _, _, _, est = _run_cut(model=None, a_known=True, off_ticks=60)
    observations = _b_observations(est, 1.0)
    assert observations
    for value, (d_t_dt, delta, power, *_) in observations:
        assert power == 0.0
        assert value == pytest.approx(-d_t_dt / delta)
