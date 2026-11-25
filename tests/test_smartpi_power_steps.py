"""Power steps of the a/b learning: settling waits of the response model, gates, delivered power, episodes."""
import math
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    RESPONSE_LAG_RESOLVE_EPISODES,
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.power_steps import PowerSteps
from custom_components.vtherm_smartpi.smartpi.response_model import (
    CONFIDENCE_ASSUMED,
    CONFIDENCE_MEASURED,
    CONFIDENCE_PROVISIONAL,
    delivered_fraction,
    settle_s,
)

B_PER_MIN = 3.0e-4
T_EXT = 5.0
HEAT = str(VThermHvacMode_HEAT)


def _model(theta: float, tau: float, confidence: str = CONFIDENCE_MEASURED) -> dict:
    return {"theta_s": theta, "tau_e_s": tau, "source": "pulse", "confidence": confidence}


class _Model:
    """Stand-in of ResponseModel.values for the learning window."""

    def __init__(self, values: dict | None) -> None:
        self.v = values

    def values(self, mode):
        return self.v


# -- settling waits ----------------------------------------------------------------------------------------------
def test_wait_without_a_model_is_unknown():
    assert settle_s(None) is None


@pytest.mark.parametrize("confidence", [CONFIDENCE_PROVISIONAL, CONFIDENCE_MEASURED])
def test_wait_of_a_certain_split_is_the_emitter_lag(confidence):
    """theta + tau_e (one emitter lag), after a power step and after a cut: the delivered power corrects the rest."""
    assert settle_s(_model(120.0, 600.0, confidence)) == 720.0


def test_wait_of_an_assumed_split_is_s():
    """Dead time assumed (theta = tau_e = S / 2): the wait is S, as for a measured split."""
    assert settle_s(_model(360.0, 360.0, CONFIDENCE_ASSUMED)) == 720.0


# -- delivered power ---------------------------------------------------------------------------------------------
def test_delivered_fraction_is_a_delay_then_a_first_order():
    model = _model(240.0, 360.0)
    assert delivered_fraction(0.0, 240.0, model) == 0.0
    # Over [240, 600] s: 1 - (tau/W)(1 - exp(-W/tau)) with tau = W = 360 s.
    assert delivered_fraction(240.0, 360.0, model) == pytest.approx(math.exp(-1.0), rel=1e-9)
    assert delivered_fraction(0.0, 600.0, None) == 1.0
    assert delivered_fraction(0.0, 600.0, _model(300.0, 0.0)) == pytest.approx(0.5)


def _step_response(model: dict | None) -> list[float]:
    steps = PowerSteps("t")
    now = 1000.0
    steps.observe(now=now, dt_s=10.0, u=0.0, hvac_mode=VThermHvacMode_HEAT, model=model)
    means = []
    for _ in range(60):
        now += 60.0
        steps.observe(now=now, dt_s=60.0, u=1.0, hvac_mode=VThermHvacMode_HEAT, model=model)
        means.append(steps.interval_mean_power)
    return means


def test_delivered_power_follows_the_dead_time_and_the_response_time_constant():
    means = _step_response(_model(240.0, 360.0))
    assert means[:4] == [0.0] * 4
    assert sum(means[4:10]) / 6.0 == pytest.approx(math.exp(-1.0), rel=1e-9)
    assert means[-1] == pytest.approx(1.0, abs=1e-3)


def test_delivered_power_is_the_applied_power_without_a_model():
    assert _step_response(None) == [1.0] * 60


# -- gates -------------------------------------------------------------------------------------------------------
def _runs(steps: PowerSteps, runs, model=None, now: float = 1000.0, mode=VThermHvacMode_HEAT) -> float:
    """Apply (power, seconds) runs at a 10 s step."""
    for u, seconds in runs:
        for _ in range(int(seconds / 10.0)):
            now += 10.0
            steps.observe(now=now, dt_s=10.0, u=u, hvac_mode=mode, model=model)
    return now


def test_active_gate_is_the_emitter_lag_after_the_step():
    model = _model(60.0, 300.0)
    steps = PowerSteps("t")
    assert steps.active_gate_end(model) is None and steps.passive_gate_end(model) is None
    now = _runs(steps, [(0.0, 600.0), (1.0, 60.0)], model)
    assert steps.active_gate_end(model) == pytest.approx(now - 60.0 + 60.0 + 300.0)
    assert steps.active_gate_end(None) is None
    assert steps.passive_gate_end(model) is None


def test_active_gate_is_capped_by_half_the_median_episode():
    model = _model(0.0, 3600.0)      # a one-hour emitter: its lag is longer than half a heating phase
    steps = PowerSteps("t")
    now = _runs(steps, [(0.0, 1800.0), (1.0, 2400.0)] * 3 + [(0.0, 1800.0), (1.0, 10.0)], model)
    assert steps.episode_median_s == pytest.approx(2400.0)
    assert steps.active_gate_end(model) == pytest.approx(now - 10.0 + 1200.0)


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
@pytest.mark.parametrize("confidence", [CONFIDENCE_MEASURED, CONFIDENCE_ASSUMED])
def test_passive_gate_is_the_emitter_lag_after_the_cut(mode, confidence):
    """theta + tau_e after the cut, S when the split is assumed (the same sum), in both modes."""
    model = _model(60.0, 300.0, confidence)
    steps = PowerSteps("t")
    now = _runs(steps, [(0.0, 600.0), (1.0, 3600.0), (0.0, 60.0)], model, mode=mode)
    cut = now - 60.0
    assert steps.passive_gate_end(model) == pytest.approx(cut + 360.0)
    assert steps.passive_gate_end(None) is None


def test_observation_gap_ends_the_episode_and_forgets_the_cut():
    model = _model(60.0, 300.0)
    steps = PowerSteps("t")
    now = _runs(steps, [(0.0, 600.0), (1.0, 600.0)], model)
    last = now
    now += 1800.0
    steps.observe(now=now, dt_s=10.0, u=0.0, hvac_mode=VThermHvacMode_HEAT, model=model)
    assert steps._durations[-1] == pytest.approx(last - 1600.0)
    # The cut time is unknown: the wait counts from the resume.
    assert steps.passive_gate_end(model) == pytest.approx(now - 10.0 + 60.0 + 300.0)


# -- episodes and state ------------------------------------------------------------------------------------------
def test_runs_separated_by_short_power_cuts_count_as_one_heating_phase():
    steps = PowerSteps("t")
    runs = [(0.0, 1800.0)]
    for off_s in (60.0, 120.0, 180.0, 90.0):
        runs += [(1.0, 300.0), (0.0, off_s)]
    runs += [(1.0, 300.0), (0.0, 1800.0)]
    _runs(steps, runs)
    assert steps.episodes_count == 1


def test_runs_separated_by_a_long_power_cut_count_as_two_heating_phases():
    steps = PowerSteps("t")
    _runs(steps, [(0.0, 1800.0), (1.0, 300.0), (0.0, 1200.0), (1.0, 300.0), (0.0, 1800.0)])
    assert steps.episodes_count == 2
    assert not steps.phases_resolve
    _runs(steps, [(1.0, 300.0), (0.0, 1200.0)] * (RESPONSE_LAG_RESOLVE_EPISODES - 2), now=1e5)
    assert steps.phases_resolve


def test_state_round_trip_and_mode_change():
    steps = PowerSteps("t")
    _runs(steps, [(0.0, 1800.0), (1.0, 1500.0)] * 3 + [(0.0, 600.0)])
    restored = PowerSteps("t")
    restored.load_state(steps.save_state())
    assert restored.episode_median_s == steps.episode_median_s
    assert restored.episodes_count == steps.episodes_count == 3
    _runs(restored, [(0.0, 10.0)], mode=VThermHvacMode_COOL, now=1e7)
    assert restored.episodes_count == 0 and restored._mode_stash[HEAT]["episodes"] == 3


def test_state_of_the_former_estimator_loads_its_episodes():
    steps = PowerSteps("t")
    steps.load_state({"estimates": [[300.0, 20.0]] * 3, "published": 300.0, "durations": [600.0] * 3,
                      "episodes": 5, "hvac_mode": HEAT})
    assert steps.episodes_count == 5 and steps.episode_median_s == 600.0


def test_last_episode_end_survives_save_and_load():
    steps = PowerSteps("t")
    now = _runs(steps, [(0.0, 1800.0), (1.0, 300.0), (0.0, 60.0)])
    assert steps.episodes_count == 1
    saved = steps.save_state()
    assert saved["last_episode_end_wall"] is not None
    restored = PowerSteps("t")
    restored.load_state(saved)
    assert restored._last_episode_end == pytest.approx(steps._last_episode_end, abs=1e-3)
    # An ON run right after the restart continues the phase.
    _runs(restored, [(1.0, 300.0), (0.0, 1800.0)], now=now)
    assert restored.episodes_count == 1
    restored.clear()
    assert restored._last_episode_end is None


@pytest.mark.parametrize("value", [None, "x", float("nan"), True])
def test_invalid_last_episode_end_means_no_previous_episode(value):
    steps = PowerSteps("t")
    steps.load_state({"episodes": 2, "last_episode_end_wall": value})
    assert steps._last_episode_end is None


def test_cut_survives_a_bridged_restart():
    model = _model(60.0, 300.0)
    steps = PowerSteps("t")
    now = _runs(steps, [(0.0, 600.0), (1.0, 3600.0), (0.0, 60.0)], model)
    gate = steps.passive_gate_end(model)
    restored = PowerSteps("t")
    restored.load_state(steps.save_state())
    restored.resolve_restart(True)
    restored.observe(now=now + 10.0, dt_s=10.0, u=0.0, hvac_mode=VThermHvacMode_HEAT, model=model)
    assert restored.passive_gate_end(model) == pytest.approx(gate, abs=1e-3)


# -- learning window ---------------------------------------------------------------------------------------------
def _window_setup(model: dict | None = _model(0.0, 300.0)):
    est = ABEstimator()
    est.b = B_PER_MIN
    est.b_meas_hist.extend([B_PER_MIN] * AB_A_SOFT_GATE_MIN_B)
    dt = MagicMock()
    dt.tin_history = []
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    win = LearningWindowManager("t")
    win.response_model = _Model(model)
    return win, est, dt, gov


def _update(win, est, dt, gov, now, u, temp, is_hysteresis=False):
    win.temperatures.raw = dt.tin_history
    win.update(
        dt_min=1.0, current_temp=temp, ext_temp=T_EXT, u_active=u,
        estimator=est, governance=gov, now=now, is_hysteresis=is_hysteresis,
        hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=temp,
    )


def _heat_run(model, is_hysteresis=False, ticks=40):
    win, est, dt, gov = _window_setup(model)
    now, reasons, starts, ep_start = 1000.0, [], [], None
    for k in range(ticks):
        now += 60.0
        u = 0.0 if k < 3 else 1.0
        if u > 0 and ep_start is None:
            ep_start = now - 60.0
        temp = round(19.0 + (0.025 * (k - 3) if k >= 3 else 0.0), 2)
        dt.tin_history.append((now, temp))
        _update(win, est, dt, gov, now, u, temp, is_hysteresis)
        reasons.append(est.learn_last_reason)
        if win.active and u > 0:
            starts.append(win.start_ts)
    return win, est, ep_start, starts, reasons


def test_active_window_starts_after_the_settling_wait():
    _, _, ep_start, starts, reasons = _heat_run(_model(0.0, 300.0))
    assert "skip: response transient (a)" in reasons
    assert starts and min(starts) >= ep_start + 300.0


def test_learning_stage_opens_no_window_before_the_response_model():
    win, est, _, starts, reasons = _heat_run(None, is_hysteresis=True)
    assert not starts
    assert reasons[-1] == "skip: learning stage - response model not yet published"
    assert est.a_model_ready is False and not win.a_model_ready(VThermHvacMode_HEAT)


def test_learning_stage_opens_windows_on_a_provisional_model():
    win, est, ep_start, starts, _ = _heat_run(_model(0.0, 300.0, CONFIDENCE_PROVISIONAL), is_hysteresis=True)
    assert starts and min(starts) >= ep_start + 300.0
    assert est.a_model_ready is True and win.a_model_ready(VThermHvacMode_HEAT)


def test_regulation_without_a_model_has_no_settling_gate():
    _, _, _, starts, reasons = _heat_run(None)
    assert "skip: response transient (a)" not in reasons
    assert starts


def test_first_model_does_not_recompute_an_empty_collection():
    win, est, dt, gov = _window_setup(None)
    est.replay_a_power = MagicMock(return_value=False)
    _update(win, est, dt, gov, 5000.0, 0.0, 19.0)
    win.response_model = _Model(_model(0.0, 300.0))
    _update(win, est, dt, gov, 5060.0, 0.0, 19.0)
    est.replay_a_power.assert_called_once()
    assert len(est.a_meas_hist) == 0


def test_model_after_a_load_is_taken_without_a_recompute():
    win, est, dt, gov = _window_setup()
    win.load_state({"power_steps": {"durations": [], "hvac_mode": HEAT}})
    est.replay_a_power = MagicMock(return_value=False)
    _update(win, est, dt, gov, 5000.0, 0.0, 19.0)
    est.replay_a_power.assert_not_called()


def test_a_collection_is_recomputed_with_the_delivered_power():
    """Measurements with a window geometry follow the new power model."""
    win, est, dt, gov = _window_setup(None)
    _update(win, est, dt, gov, 4940.0, 0.0, 19.0)
    b = est.b
    # Windows from 900 s to 1500 s after the step (admitted by the settling wait of one emitter lag), applied power 1, measured
    # under the applied power model (no model). A window within the transient and a measurement without geometry are
    # discarded.
    d_t_dt, delta = 0.018, 15.0
    est.a_meas_hist.extend([(d_t_dt + b * delta) / 1.0] * 2 + [0.03, 0.03])
    est._a_obs_hist.extend([(d_t_dt, delta, 1.0, (1.0, 900.0, 600.0))] * 2
                           + [(0.02, delta, 1.0, (1.0, 150.0, 600.0)), (0.02, delta, 1.0)])
    win.response_model = _Model(_model(0.0, 300.0))
    _update(win, est, dt, gov, 5000.0, 0.0, 19.0)
    eta = 1.0 - 0.5 * (math.exp(-3.0) - math.exp(-5.0))
    assert list(est.a_meas_hist) == pytest.approx([(d_t_dt + b * delta) / eta] * 2)
    assert est._a_obs_hist[0][2] == pytest.approx(eta)


def test_published_a_is_kept_when_the_model_changes():
    est = ABEstimator()
    est.a_meas_hist.extend([0.02] * 6)
    est.learn_ok_count_a = 1
    assert est.replay_a_power(lambda g: 0.5, 1.0, "x") is False
    assert list(est.a_meas_hist) == [0.02] * 6


def _gap_run(on_ticks: int):
    """ON window of on_ticks minutes, 30 min guard cut, then a falling room."""
    win, est, dt, gov = _window_setup(None)
    now, temp = 1000.0, 19.0

    def tick(u):
        nonlocal now, temp
        now += 60.0
        temp += 0.025 if u > 0 else -0.01
        dt.tin_history.append((now, round(temp, 2)))
        _update(win, est, dt, gov, now, u, round(temp, 2))

    for _ in range(on_ticks):
        tick(1.0)
    assert win.active
    # Guard cut: no learning update for 30 minutes while the room cools down.
    for _ in range(30):
        now += 60.0
        temp -= 0.01
        dt.tin_history.append((now, round(temp, 2)))
    tick(0.0)
    return win, est, now


def test_window_is_abandoned_across_an_observation_gap():
    win, est, now = _gap_run(3)
    assert not (win.active and win.u_first is not None and win.u_first > 0.2)
    assert len(est.a_meas_hist) == 0
    summary = win.outcomes_summary(now)
    assert any("observation gap" in str(entry) for entry in summary.get("a", []))


def test_window_closed_at_a_gap_uses_only_the_data_before_it():
    _, est, _ = _gap_run(20)
    assert est._a_obs_hist
    # No submitted slope mixes the rising data with the post-gap cooling.
    assert all(obs[0] == pytest.approx(0.025, rel=0.05) for obs in est._a_obs_hist)
