"""Response lag estimator: intercept inversion, episodes, gates and filter."""
import math
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_SETTLE_K_ACTIVE,
    AB_SETTLE_K_PASSIVE,
    FreezeReason,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.response_lag import (
    ResponseLagEstimator,
    invert_single_lag_intercept,
    single_lag_intercept,
)

A_PER_S = 0.025 / 60.0  # full-power heating rate (C/s)
B_PER_MIN = 3.0e-4
T_EXT = 5.0


class _Plant:
    """Room integrator behind a first-order heater lag, 0.01 C sensor."""

    def __init__(self, lag_s: float) -> None:
        self.lag_s = lag_s
        self.temp = 19.0
        self.heat = 0.0

    def step(self, dt_s: float, u: float) -> None:
        for _ in range(int(dt_s)):
            self.heat += (u - self.heat) / self.lag_s
            self.temp += A_PER_S * self.heat - B_PER_MIN / 60.0 * (self.temp - T_EXT)

    @property
    def reading(self) -> float:
        return round(self.temp, 2)


def _drive(
    est: ResponseLagEstimator,
    lag_s: float,
    on_min: float,
    episodes: int,
    off_min: float = 60.0,
    b: float | None = None,
    quantum: float = 0.01,
):
    """Alternate off_min OFF and on_min ON phases; return the episode starts."""
    plant = _Plant(lag_s)
    history: list[tuple[float, float]] = []
    now, starts = 1000.0, []
    phases = []
    for _ in range(episodes):
        phases += [(0.0, off_min), (1.0, on_min)]
    phases.append((0.0, off_min))
    for u, minutes in phases:
        if u > 0:
            starts.append(now)
        for _ in range(int(minutes * 6)):
            plant.step(10.0, u)
            now += 10.0
            history.append((now, round(round(plant.temp / quantum) * quantum, 2)))
            est.observe(
                now=now, dt_s=10.0, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=b, tin_history=history,
            )
    return starts, now


def test_intercept_inversion_round_trip():
    for lag in (60.0, 300.0, 1200.0):
        for t1, t2 in ((450.0, 900.0), (900.0, 1800.0), (1200.0, 2400.0), (1800.0, 3600.0)):
            intercept = single_lag_intercept(t1, t2, lag)
            assert 0.0 <= intercept < lag
            inverted = invert_single_lag_intercept(intercept, t1, t2)
            if inverted is not None:
                assert inverted == pytest.approx(lag, rel=1e-6)
    # Increasing in the lag, so the inversion is unique.
    values = [single_lag_intercept(1200.0, 2400.0, lag) for lag in range(10, 20000, 10)]
    assert all(b > a for a, b in zip(values, values[1:]))


def test_unresolvable_intercept_is_rejected():
    assert invert_single_lag_intercept(600.0, 600.0, 1200.0) is None
    assert invert_single_lag_intercept(-10.0, 600.0, 1200.0) == 0.0


@pytest.mark.parametrize("lag_s, on_min, off_min", [(300.0, 25.0, 60.0), (1200.0, 40.0, 110.0)])
def test_lag_is_recovered_on_constant_power_episodes(lag_s, on_min, off_min):
    """The passive slope is measured on the OFF tail (b not published yet)."""
    est = ResponseLagEstimator("t")
    _drive(est, lag_s, on_min, episodes=3, off_min=off_min)
    assert est.estimates_count == 3
    assert est.lag_s == pytest.approx(lag_s, rel=0.15)
    assert est.episode_median_s == pytest.approx(on_min * 60.0, abs=20.0)


def test_published_b_is_the_fallback_baseline():
    """A 0.1 C sensor cannot measure the slow passive slope: b is used."""
    est = ResponseLagEstimator("t")
    _drive(est, 300.0, 35.0, episodes=3, quantum=0.1)
    assert est.estimates_count == 0
    assert est.last_reason == "passive slope unknown"
    est = ResponseLagEstimator("t")
    _drive(est, 300.0, 35.0, episodes=3, b=B_PER_MIN, quantum=0.1)
    assert est.estimates_count == 3
    assert est.lag_s == pytest.approx(300.0, rel=0.25)


def test_inconsistent_estimates_are_not_published():
    est = ResponseLagEstimator("t")
    est.load_state({"estimates": [[100.0, 300.0], [400.0, 300.0], [900.0, 300.0]], "durations": []})
    assert est.lag_s is None
    est.load_state({"estimates": [[280.0, 20.0], [300.0, 20.0], [330.0, 20.0]], "durations": []})
    assert est.lag_s == 300.0
    est.load_state({"estimates": [[280.0, 20.0], [300.0, 20.0]], "durations": []})
    assert est.lag_s is None


def test_published_lag_survives_an_inconsistent_set():
    est = ResponseLagEstimator("t")
    est.load_state({"estimates": [[280.0, 20.0], [300.0, 20.0], [330.0, 20.0]], "durations": []})
    assert est.lag_s == 300.0
    est._estimates.extend([(100.0, 50.0), (700.0, 50.0), (900.0, 50.0)])
    est._refresh_published()
    assert est.lag_s == 300.0
    restored = ResponseLagEstimator("t")
    restored.load_state(est.save_state())
    assert restored.lag_s == 300.0


def test_unsettled_baseline_is_not_used():
    est = ResponseLagEstimator("t")
    _drive(est, 1200.0, 40.0, episodes=3, off_min=60.0)
    assert est.estimates_count == 1
    assert est.last_reason == "passive baseline not settled"


def test_no_estimate_with_varying_power_or_without_response():
    est = ResponseLagEstimator("t")
    history = [(1000.0 + 10.0 * k, 19.0 + 0.01 * (k % 2)) for k in range(400)]
    now = 1000.0
    for k in range(400):
        now += 10.0
        u = 0.0 if k < 200 or k >= 330 else (0.5 if k < 260 else 0.9)
        est.observe(now=now, dt_s=10.0, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                    b=B_PER_MIN, tin_history=history)
    assert est.estimates_count == 0
    assert est.last_reason == "power not constant"

    est = ResponseLagEstimator("t")
    now = 1000.0
    for k in range(400):
        now += 10.0
        u = 1.0 if 200 <= k < 330 else 0.0
        est.observe(now=now, dt_s=10.0, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                    b=None, tin_history=history)
    assert est.estimates_count == 0


def test_gates_follow_the_published_lag():
    est = ResponseLagEstimator("t")
    assert est.active_gate_end() is None and est.passive_gate_end() is None
    _, now = _drive(est, 300.0, 25.0, episodes=3)
    lag = est.lag_s
    off_start = now - 3600.0
    assert est.active_gate_end() is None
    assert est.passive_gate_end() == pytest.approx(off_start + AB_SETTLE_K_PASSIVE * lag)

    est.observe(now=now + 10.0, dt_s=10.0, u=1.0, t_ext=T_EXT,
                hvac_mode=VThermHvacMode_HEAT, b=B_PER_MIN, tin_history=[])
    assert est.passive_gate_end() is None
    settle = min(AB_SETTLE_K_ACTIVE * lag, 0.5 * est.episode_median_s)
    assert est.active_gate_end() == pytest.approx(now + settle)


def test_active_gate_is_capped_by_half_the_median_episode():
    est = ResponseLagEstimator("t")
    _, now = _drive(est, 1200.0, 40.0, episodes=3, off_min=110.0)
    est.observe(now=now + 10.0, dt_s=10.0, u=1.0, t_ext=T_EXT,
                hvac_mode=VThermHvacMode_HEAT, b=B_PER_MIN, tin_history=[])
    assert AB_SETTLE_K_ACTIVE * est.lag_s > 0.5 * est.episode_median_s
    assert est.active_gate_end() == pytest.approx(now + 0.5 * est.episode_median_s)


def _step_response(deadtime_s: float) -> list[float]:
    est = ResponseLagEstimator("t")
    est.load_state({"estimates": [[600.0, 30.0]] * 3, "durations": [], "hvac_mode": str(VThermHvacMode_HEAT)})
    now = 1000.0
    est.observe(now=now, dt_s=10.0, u=0.0, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=B_PER_MIN, tin_history=[], deadtime_s=deadtime_s)
    means = []
    for _ in range(60):
        now += 60.0
        est.observe(now=now, dt_s=60.0, u=1.0, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                    b=B_PER_MIN, tin_history=[], deadtime_s=deadtime_s)
        means.append(est.interval_mean_power)
    return means


def test_delivered_power_splits_the_lag_into_dead_time_and_first_order_lag():
    # Dead time 240 s: pure delay 240 s, then a 360 s first-order lag.
    means = _step_response(240.0)
    assert means[:4] == [0.0] * 4
    # Mean over minutes 4..10: 1 - (tau/W)(1 - exp(-W/tau)) with tau = W = 6 min
    assert sum(means[4:10]) / 6.0 == pytest.approx(math.exp(-1.0), rel=1e-9)
    assert means[-1] == pytest.approx(1.0, abs=1e-3)


def test_dead_time_longer_than_the_lag_is_a_pure_delay():
    means = _step_response(900.0)
    assert means[:10] == [0.0] * 10
    assert means[10:] == [1.0] * 50


def test_delivered_power_is_the_applied_power_without_dead_time():
    assert _step_response(None) == [1.0] * 60


def test_state_round_trip_and_mode_change():
    est = ResponseLagEstimator("t")
    _drive(est, 300.0, 25.0, episodes=3)
    saved = est.save_state()
    restored = ResponseLagEstimator("t")
    restored.load_state(saved)
    assert restored.lag_s == est.lag_s
    assert restored.episode_median_s == est.episode_median_s
    restored.load_state({"estimates": [["x", 1.0], [-5.0, 1.0], [120.0, 10.0], 300.0], "durations": None})
    assert restored.estimates_count == 1

    est.observe(now=1e7, dt_s=10.0, u=0.0, t_ext=T_EXT, hvac_mode="cool",
                b=B_PER_MIN, tin_history=[])
    assert est.estimates_count == 0


def _window_setup():
    est = ABEstimator()
    est.b = B_PER_MIN
    est.b_meas_hist.extend([B_PER_MIN] * AB_A_SOFT_GATE_MIN_B)
    dt = MagicMock()
    dt.deadtime_heat_reliable = True
    dt.deadtime_heat_s = 60.0
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = []
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    win = LearningWindowManager("t")
    win.load_state({"response_lag": {"estimates": [[300.0, 20.0]] * 3, "durations": [],
                                     "hvac_mode": str(VThermHvacMode_HEAT)}})
    return win, est, dt, gov


def test_active_window_starts_after_the_settling_gate():
    win, est, dt, gov = _window_setup()
    now = 1000.0
    reasons, ep_start = [], None
    for k in range(40):
        now += 60.0
        u = 0.0 if k < 3 else 1.0
        if u > 0 and ep_start is None:
            ep_start = now - 60.0
        temp = 19.0 + (0.025 * (k - 3) if k >= 3 else 0.0)
        dt.tin_history.append((now, round(temp, 2)))
        win.update(
            dt_min=1.0, current_temp=round(temp, 2), ext_temp=T_EXT, u_active=u,
            estimator=est, dt_est=dt, governance=gov, now=now,
            in_deadband=False, in_near_band=False,
            t_heat_episode_start=ep_start, t_cool_episode_start=None,
            hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=round(temp, 2),
        )
        reasons.append(est.learn_last_reason)
        if win.active and u > 0:
            assert win.start_ts >= ep_start + AB_SETTLE_K_ACTIVE * 300.0
    assert "skip: response transient (a)" in reasons


def test_observation_gap_closes_the_episode_at_the_last_observation():
    """A guard cut stops the observation: the episode ends at the last tick."""
    est = ResponseLagEstimator("t")
    plant = _Plant(300.0)
    history: list[tuple[float, float]] = []
    now = 1000.0
    for k in range(360 + 150):
        u = 0.0 if k < 360 else 1.0
        plant.step(10.0, u)
        now += 10.0
        history.append((now, plant.reading))
        est.observe(now=now, dt_s=10.0, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                    b=None, tin_history=history)
    last = now
    now += 1800.0
    est.observe(now=now, dt_s=10.0, u=0.0, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=None, tin_history=history)
    assert est.last_reason == "estimated"
    assert est.estimates_count == 1
    assert est._durations[-1] == pytest.approx(last - (1000.0 + 3600.0))
    est.load_state({"estimates": [[300.0, 20.0]] * 3, "durations": [], "hvac_mode": str(VThermHvacMode_HEAT)})
    est.observe(now=now + 10.0, dt_s=10.0, u=0.0, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=None, tin_history=history)
    now += 4000.0
    est.observe(now=now, dt_s=10.0, u=0.0, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=None, tin_history=history)
    assert est.passive_gate_end() == pytest.approx(now - 10.0 + AB_SETTLE_K_PASSIVE * 300.0)


def test_level_crossings_remove_the_staircase_bias_of_a_coarse_sensor():
    """0.2 C sensor publishing on change: the lag is recovered within 15 %."""
    est = ResponseLagEstimator("t")
    _drive(est, 300.0, 60.0, episodes=4, off_min=180.0, b=B_PER_MIN, quantum=0.2)
    assert est.estimates_count >= 3
    assert est.lag_s == pytest.approx(300.0, rel=0.15)


def test_a_collection_restarts_when_the_lag_is_published():
    _, est, dt, gov = _window_setup()
    win = LearningWindowManager("t")
    est.a_meas_hist.extend([0.02, 0.021])
    est._a_obs_hist.extend([(0.01, 15.0, 1.0)] * 2)
    win.response_lag.load_state({"estimates": [[300.0, 20.0]] * 3, "durations": [],
                                 "hvac_mode": str(VThermHvacMode_HEAT)})
    win.update(
        dt_min=1.0, current_temp=19.0, ext_temp=T_EXT, u_active=0.0,
        estimator=est, dt_est=dt, governance=gov, now=5000.0,
        in_deadband=False, in_near_band=False,
        t_heat_episode_start=None, t_cool_episode_start=None,
        hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=19.0,
    )
    assert len(est.a_meas_hist) == 0 and len(est._a_obs_hist) == 0


def test_published_a_is_kept_when_the_lag_is_published():
    est = ABEstimator()
    est.a_meas_hist.extend([0.02] * 6)
    est.learn_ok_count_a = 1
    assert est.replay_a_power(lambda g: 0.5, 1.0, "x") is False
    assert list(est.a_meas_hist) == [0.02] * 6


def test_a_collection_is_recomputed_with_the_delivered_power():
    """Measurements with a window geometry follow the new power model."""
    _, est, dt, gov = _window_setup()
    win = LearningWindowManager("t")
    b = est.b
    # Windows from 900 s to 1500 s after the step (admitted by the settling
    # gate of 3 lags), applied power 1, measured under the applied power
    # model (lag unknown). A window within the transient and a measurement
    # without geometry are discarded.
    d_t_dt, delta = 0.018, 15.0
    est.a_meas_hist.extend([(d_t_dt + b * delta) / 1.0] * 2 + [0.03, 0.03])
    est._a_obs_hist.extend([(d_t_dt, delta, 1.0, (1.0, 900.0, 600.0))] * 2
                           + [(0.02, delta, 1.0, (1.0, 150.0, 600.0)), (0.02, delta, 1.0)])
    win.response_lag.load_state({"estimates": [[300.0, 20.0]] * 3, "durations": [],
                                 "hvac_mode": str(VThermHvacMode_HEAT)})
    dt.deadtime_heat_s = 0.0
    win.update(
        dt_min=1.0, current_temp=19.0, ext_temp=T_EXT, u_active=0.0,
        estimator=est, dt_est=dt, governance=gov, now=5000.0,
        in_deadband=False, in_near_band=False,
        t_heat_episode_start=None, t_cool_episode_start=None,
        hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=19.0,
    )
    # Pure lag of 300 s: delivered fraction over [900, 1500] s.
    eta = 1.0 - 0.5 * (math.exp(-3.0) - math.exp(-5.0))
    assert len(est.a_meas_hist) == 2
    assert list(est.a_meas_hist) == pytest.approx([(d_t_dt + b * delta) / eta] * 2)
    assert est._a_obs_hist[0][2] == pytest.approx(eta)


def _gap_run(on_ticks: int):
    """ON window of on_ticks minutes, 30 min guard cut, then a falling room."""
    _, est, dt, gov = _window_setup()
    win = LearningWindowManager("t")
    now, temp = 1000.0, 19.0

    def tick(u):
        nonlocal now, temp
        now += 60.0
        temp += 0.025 if u > 0 else -0.01
        dt.tin_history.append((now, round(temp, 2)))
        win.update(
            dt_min=1.0, current_temp=round(temp, 2), ext_temp=T_EXT, u_active=u,
            estimator=est, dt_est=dt, governance=gov, now=now,
            in_deadband=False, in_near_band=False,
            t_heat_episode_start=1000.0, t_cool_episode_start=None,
            hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=round(temp, 2),
        )

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
    win, est, _ = _gap_run(20)
    assert est._a_obs_hist
    # No submitted slope mixes the rising data with the post-gap cooling.
    assert all(obs[0] == pytest.approx(0.025, rel=0.05) for obs in est._a_obs_hist)
