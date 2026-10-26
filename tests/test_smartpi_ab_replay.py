"""a measurements follow later b updates through their stored observations."""
import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_B,
)

T_INT, T_EXT = 20.0, 5.0
DELTA = T_INT - T_EXT


def _learn_b(est: ABEstimator, b: float, n: int, mode=VThermHvacMode_HEAT) -> None:
    t_ext = T_INT + DELTA if mode == VThermHvacMode_COOL else T_EXT
    for _ in range(n):
        est.learn(dT_int_per_min=-b * (T_INT - t_ext), u=0.0, t_int=T_INT, t_ext=t_ext, hvac_mode=mode)


def _move_b(est: ABEstimator, b_new: float) -> None:
    """Learn b_new until the published b moves (the median needs several samples)."""
    b_old = est.b
    for _ in range(20):
        _learn_b(est, b_new, 1)
        if est.b != b_old:
            return
    raise AssertionError("published b did not move")


def _learn_a(est: ABEstimator, a: float, b_true: float, n: int, u: float = 1.0) -> list:
    observations = []
    for k in range(n):
        slope = a * u - b_true * DELTA + 1e-5 * k
        est.learn(dT_int_per_min=slope, u=u, t_int=T_INT, t_ext=T_EXT)
        observations.append(slope)
    return observations


def _expected(slopes, b, u=1.0):
    return [(s + b * DELTA) / u for s in slopes]


def test_published_a_follows_a_later_b_update():
    est = ABEstimator()
    _learn_b(est, 3.0e-4, AB_A_SOFT_GATE_MIN_B)
    b_first = est.b
    slopes = _learn_a(est, 0.025, 3.0e-4, AB_MIN_SAMPLES_A + 2)
    assert est.learn_ok_count_a > 0
    assert list(est.a_meas_hist) == pytest.approx(_expected(slopes, b_first))
    a_before = est.a

    _move_b(est, 6.0e-4)
    assert est.b != b_first
    assert list(est.a_meas_hist) == pytest.approx(_expected(slopes, est.b))
    assert est.a != a_before
    assert est.a == pytest.approx(sorted(est.a_meas_hist)[len(est.a_meas_hist) // 2], rel=0.05)


def test_collecting_a_is_replayed_without_publishing_a():
    est = ABEstimator()
    _learn_b(est, 3.0e-4, AB_A_SOFT_GATE_MIN_B)
    slopes = _learn_a(est, 0.025, 3.0e-4, AB_MIN_SAMPLES_A - 2)
    a_init = est.a
    _move_b(est, 6.0e-4)
    assert list(est.a_meas_hist) == pytest.approx(_expected(slopes, est.b))
    assert est.a == a_init


def test_measurements_restored_without_observation_keep_their_value():
    est = ABEstimator()
    _learn_b(est, 3.0e-4, AB_A_SOFT_GATE_MIN_B)
    _learn_a(est, 0.025, 3.0e-4, AB_MIN_SAMPLES_A + 2)
    state = est.save_state()
    state.pop("a_obs_hist")

    legacy = ABEstimator()
    legacy.load_state(state)
    kept = list(legacy.a_meas_hist)
    _move_b(legacy, 6.0e-4)
    assert list(legacy.a_meas_hist) == pytest.approx(kept)


def test_observations_survive_persistence():
    est = ABEstimator()
    _learn_b(est, 3.0e-4, AB_A_SOFT_GATE_MIN_B)
    slopes = _learn_a(est, 0.025, 3.0e-4, AB_MIN_SAMPLES_A + 2)

    restored = ABEstimator()
    restored.load_state(est.save_state())
    _move_b(restored, 6.0e-4)
    assert list(restored.a_meas_hist) == pytest.approx(_expected(slopes, restored.b))


def test_replay_never_flips_the_hvac_sign():
    est = ABEstimator()
    _learn_b(est, 3.0e-4, AB_A_SOFT_GATE_MIN_B)
    # tiny a: with a much lower b, (slope + b * delta) / u becomes negative
    _learn_a(est, 0.0001, 3.0e-4, AB_MIN_SAMPLES_A - 2)
    kept = list(est.a_meas_hist)
    est.b = 1.0e-6
    est._replay_a_measurements(1.0)
    assert list(est.a_meas_hist) == pytest.approx(kept)


def test_bootstrap_counts_are_consistent():
    assert AB_A_SOFT_GATE_MIN_B <= AB_MIN_SAMPLES_B
