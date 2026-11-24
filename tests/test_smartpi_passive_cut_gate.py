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
        win.update(
            dt_min=TICK_S / 60.0, current_temp=reading, ext_temp=t_ext, u_active=u,
            estimator=est, dt_est=dt, governance=gov, now=now, is_hysteresis=is_hysteresis,
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
    for value, (d_t_dt, delta, power, _) in observations:
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
