"""The mode-dependent model is kept per HVAC mode: a change of mode sets the outgoing part aside and takes back the
one set aside for the incoming mode."""
import json
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import AB_A_SOFT_GATE_MIN_B, AB_MIN_SAMPLES_A
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.power_steps import PowerSteps

HEAT, COOL = VThermHvacMode_HEAT, VThermHvacMode_COOL
B = 3.0e-4


def _learn_b(est: ABEstimator, mode, n: int = AB_A_SOFT_GATE_MIN_B) -> None:
    t_int, t_ext = (24.0, 34.0) if mode == COOL else (20.0, 5.0)
    for _ in range(n):
        est.learn(dT_int_per_min=-B * (t_int - t_ext), u=0.0, t_int=t_int, t_ext=t_ext, hvac_mode=mode)


def _learn_a(est: ABEstimator, mode, a: float, n: int = AB_MIN_SAMPLES_A + 2) -> None:
    t_int, t_ext = (24.0, 34.0) if mode == COOL else (20.0, 5.0)
    sign = -1.0 if mode == COOL else 1.0
    for k in range(n):
        slope = sign * a - B * (t_int - t_ext) + sign * 1e-5 * k
        est.learn(dT_int_per_min=slope, u=1.0, t_int=t_int, t_ext=t_ext, hvac_mode=mode)


def _a_channel(est: ABEstimator) -> dict:
    snap = est._a_channel_snapshot()
    assert snap is not None
    return json.loads(json.dumps(snap))


def _heat_learned() -> ABEstimator:
    est = ABEstimator()
    est.ensure_hvac_mode(HEAT)
    _learn_b(est, HEAT)
    _learn_a(est, HEAT, 0.025)
    assert est.learn_ok_count_a > 0
    return est


# ---------------------------------------------------------------- a channel


def test_heat_a_channel_comes_back_after_a_cool_period():
    est = _heat_learned()
    heat = _a_channel(est)
    b, counts = est.b, est.learn_ok_count

    est.ensure_hvac_mode(COOL)
    assert est.learn_ok_count_a == 0 and est.a < 0.0 and not est.a_meas_hist
    assert est.b == b  # b is shared

    est.ensure_hvac_mode(HEAT)
    assert _a_channel(est) == heat
    assert est.b == b and est.learn_ok_count == counts and est.learn_ok_count_a == heat["learn_ok_count_a"]
    assert est._mode_stash == {}


def test_first_cool_period_starts_blank_as_before():
    est = _heat_learned()
    blank = ABEstimator()
    blank.ensure_hvac_mode(HEAT)
    blank.ensure_hvac_mode(COOL)

    assert est.ensure_hvac_mode(COOL) is True
    assert est.a == blank.a == -abs(est.A_INIT)
    assert est.learn_ok_count_a == 0
    assert not est.a_meas_hist and not est._a_hat_hist and not est._a_obs_hist
    assert not est.a_drift.drift_buffer
    assert set(est._mode_stash) == {"heat"}


def test_each_mode_keeps_its_own_a_and_b_stays_shared():
    est = _heat_learned()
    heat = _a_channel(est)
    est.ensure_hvac_mode(COOL)
    _learn_a(est, COOL, 0.015)
    cool = _a_channel(est)
    assert cool["a"] < 0.0 < heat["a"]

    est.ensure_hvac_mode(HEAT)
    assert _a_channel(est) == heat
    assert set(est._mode_stash) == {"cool"}
    est.ensure_hvac_mode(COOL)
    assert _a_channel(est) == cool


def test_a_measurements_follow_the_b_learned_in_the_other_mode():
    est = _heat_learned()
    before = list(est.a_meas_hist)
    est.ensure_hvac_mode(COOL)
    est.b = 6.0e-4  # b moved while HEAT was set aside
    est.ensure_hvac_mode(HEAT)
    expected = [(d + est.b * delta) / u for d, delta, u in (obs[:3] for obs in est._a_obs_hist)]
    assert list(est.a_meas_hist) == pytest.approx(expected)
    assert list(est.a_meas_hist) != pytest.approx(before)
    assert est.a > 0.0


def test_a_standing_starting_a_is_dropped_on_the_switch_and_comes_back():
    est = ABEstimator()
    est.ensure_hvac_mode(HEAT)
    est.give_starting_values(0.02, B, "heat", 1.0)
    assert est.start.standing("a") and est.start.standing("b")

    est.ensure_hvac_mode(COOL)
    assert not est.start.standing("a") and est.start.standing("b")
    est.ensure_hvac_mode(HEAT)
    assert est.start.standing("a") and est.a == pytest.approx(0.02)
    assert est.start.standing("b")


def test_nothing_learned_leaves_no_stash():
    est = ABEstimator()
    est.ensure_hvac_mode(HEAT)
    est.ensure_hvac_mode(COOL)
    est.ensure_hvac_mode(HEAT)
    assert est._mode_stash == {}
    assert "mode_stash" not in est.save_state()


def test_learning_reset_clears_both_modes():
    est = _heat_learned()
    est.ensure_hvac_mode(COOL)
    assert est._mode_stash
    est.reset()
    assert est._mode_stash == {} and "mode_stash" not in est.save_state()
    est.ensure_hvac_mode(HEAT)
    est.ensure_hvac_mode(COOL)
    est.ensure_hvac_mode(HEAT)
    assert est.learn_ok_count_a == 0


def test_single_mode_state_has_no_extra_key():
    est = _heat_learned()
    est.ensure_hvac_mode(HEAT)
    state = est.save_state()
    assert "mode_stash" not in state
    other = ABEstimator()
    other.load_state(json.loads(json.dumps(state)))
    assert other.save_state() == state


def test_state_saved_before_the_stash_loads_unchanged():
    est = _heat_learned()
    state = est.save_state()
    other = ABEstimator()
    other.load_state(state)
    assert other._mode_stash == {} and other.a == est.a
    other.ensure_hvac_mode(COOL)
    other.ensure_hvac_mode(HEAT)
    assert other.a == pytest.approx(est.a) and other.learn_ok_count_a == est.learn_ok_count_a


def test_stash_round_trips_through_the_saved_state():
    est = _heat_learned()
    heat = _a_channel(est)
    est.ensure_hvac_mode(COOL)
    _learn_a(est, COOL, 0.015)
    state = json.loads(json.dumps(est.save_state()))
    assert set(state["mode_stash"]) == {"heat"}

    other = ABEstimator()
    other.load_state(state)
    assert other.model_hvac_mode == "cool" and other.a < 0.0
    assert json.loads(json.dumps(other.save_state())) == state
    other.ensure_hvac_mode(HEAT)
    assert _a_channel(other) == heat


def test_malformed_or_wrong_signed_stash_is_dropped():
    est = _heat_learned()
    est.ensure_hvac_mode(COOL)
    state = est.save_state()
    state["mode_stash"]["cool"] = {"a": 0.02}  # the active mode never has a stash
    state["mode_stash"]["heat"]["a"] = -0.02  # sign contradicts the mode
    other = ABEstimator()
    other.load_state(state)
    assert set(other._mode_stash) == {"heat"}
    other.ensure_hvac_mode(HEAT)
    assert other.a > 0.0 and other.learn_ok_count_a == 0

    other = ABEstimator()
    state["mode_stash"] = {"heat": "garbage", "off": {}}
    other.load_state(state)
    assert other._mode_stash == {}


# ---------------------------------------------------------------- dead times


def _set_dead_times(dt: DeadTimeEstimator, heat: float) -> None:
    """The dead time after a start of HEAT (the heating slot)."""
    dt._history_heat.extend([heat - 10.0, heat, heat + 10.0])
    dt.deadtime_heat_s, dt.deadtime_heat_reliable = heat, True


def _dead_times(dt: DeadTimeEstimator) -> tuple:
    return (dt.deadtime_heat_s, dt.deadtime_cool_s, dt.deadtime_heat_reliable, dt.deadtime_cool_reliable,
            list(dt._history_heat), list(dt._history_cool))


def test_heat_dead_times_come_back_exactly():
    dt = DeadTimeEstimator()
    dt.ensure_hvac_mode(HEAT)
    _set_dead_times(dt, 240.0)
    heat = _dead_times(dt)

    assert dt.ensure_hvac_mode(COOL) is True
    assert _dead_times(dt) == (None, None, False, False, [], [])
    dt._history_cool.append(90.0)
    dt.deadtime_cool_s = 90.0
    assert dt.ensure_hvac_mode(HEAT) is True
    assert _dead_times(dt) == heat
    assert set(dt._mode_stash) == {"cool"} and dt._mode_stash["cool"]["deadtime_cool_s"] == 90.0


def test_dead_time_reset_clears_both_modes_and_single_mode_state_is_unchanged():
    dt = DeadTimeEstimator()
    dt.ensure_hvac_mode(HEAT)
    _set_dead_times(dt, 240.0)
    assert "mode_stash" not in dt.save_state()
    dt.ensure_hvac_mode(COOL)
    assert "mode_stash" in dt.save_state()
    dt.reset()
    assert dt._mode_stash == {} and "mode_stash" not in dt.save_state()
    dt.ensure_hvac_mode(HEAT)
    assert _dead_times(dt) == (None, None, False, False, [], [])


def test_dead_time_stash_round_trips_and_old_state_loads():
    dt = DeadTimeEstimator()
    dt.ensure_hvac_mode(HEAT)
    _set_dead_times(dt, 240.0)
    heat = _dead_times(dt)
    old = json.loads(json.dumps(dt.save_state()))
    legacy = DeadTimeEstimator()
    legacy.load_state(old)
    assert legacy._mode_stash == {} and _dead_times(legacy) == heat

    dt.ensure_hvac_mode(COOL)
    state = json.loads(json.dumps(dt.save_state()))
    assert set(state["mode_stash"]) == {"heat"}
    other = DeadTimeEstimator()
    other.load_state(state)
    assert json.loads(json.dumps(other.save_state())) == state
    other.ensure_hvac_mode(HEAT)
    assert _dead_times(other) == heat


# ---------------------------------------------------------------- emitter lag


def _observe(steps: PowerSteps, mode) -> None:
    steps.observe(now=100.0, dt_s=10.0, u=0.0, hvac_mode=mode, model=None)


def test_power_steps_are_kept_per_mode():
    steps = PowerSteps("t")
    _observe(steps, HEAT)
    steps.load_state({"hvac_mode": "heat", "durations": [900.0, 900.0, 900.0], "episodes": 4})
    assert steps.episodes_count == 4

    _observe(steps, COOL)
    assert steps.episodes_count == 0 and steps.episode_median_s is None
    state = json.loads(json.dumps(steps.save_state()))
    assert set(state["mode_stash"]) == {"heat"}

    _observe(steps, HEAT)
    assert steps.episodes_count == 4 and steps.episode_median_s == 900.0
    assert "mode_stash" not in json.loads(json.dumps(steps.save_state()))

    reloaded = PowerSteps("t")
    reloaded.load_state(state)
    _observe(reloaded, HEAT)
    assert reloaded.episodes_count == 4
    reloaded.clear()
    assert "mode_stash" not in reloaded.save_state()


# ---------------------------------------------------------------- orchestrator


def _smartpi() -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="stash")


def test_saved_state_carries_the_stash_and_only_after_a_switch():
    algo = _smartpi()
    algo.est.ensure_hvac_mode(HEAT)
    _learn_b(algo.est, HEAT)
    _learn_a(algo.est, HEAT, 0.025)
    algo.dt_est.ensure_hvac_mode(HEAT)
    _set_dead_times(algo.dt_est, 240.0)
    saved = json.loads(json.dumps(algo.save_state()))
    assert "mode_stash" not in saved["est_state"] and "mode_stash" not in saved["dt_est_state"]

    algo.calculate(target_temp=22.0, current_temp=21.0, ext_current_temp=5.0, hvac_mode=HEAT)
    algo.calculate(target_temp=22.0, current_temp=24.0, ext_current_temp=30.0, hvac_mode=COOL)
    assert algo.est.learn_ok_count_a == 0
    saved = json.loads(json.dumps(algo.save_state()))
    assert set(saved["est_state"]["mode_stash"]) == {"heat"}
    assert set(saved["dt_est_state"]["mode_stash"]) == {"heat"}

    other = _smartpi()
    other.load_state(saved)
    other.calculate(target_temp=22.0, current_temp=21.0, ext_current_temp=5.0, hvac_mode=HEAT)
    assert other.est.learn_ok_count_a > 0 and other.est.a > 0.0
    assert other.dt_est.deadtime_heat_s == 240.0 and other.dt_est.deadtime_cool_s is None

    other.reset_learning()
    assert "mode_stash" not in other.save_state()["est_state"]
    assert "mode_stash" not in other.save_state()["dt_est_state"]
