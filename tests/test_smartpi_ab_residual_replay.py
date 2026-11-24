"""a/b estimator: residual delivered power in b, b measurements following a, a measurements kept until b is learned."""
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_HISTORY_SIZE,
    AB_MIN_SAMPLES_B,
    AB_VAL_TOLERANCE,
)

T_INT, T_EXT = 20.0, 5.0
DELTA = T_INT - T_EXT
B_SLOPE = -0.006          # passive slope: b = 0.0004 /min without residual power
POWER = 0.2               # residual delivered power over the passive windows
A = 0.03


def _learn_b(est, slope=B_SLOPE, power=POWER, mode=VThermHvacMode_HEAT, t_int=T_INT, t_ext=T_EXT):
    est.learn(dT_int_per_min=slope, u=0.0, residual_power=power, t_int=t_int, t_ext=t_ext, hvac_mode=mode)


def _learn_a(est, slope=0.02):
    est.learn(dT_int_per_min=slope, u=1.0, t_int=T_INT, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT)


def _publish_a(est, value=A):
    """a published by the learning (b has its samples)."""
    est.a_meas_hist.extend([value] * 5)
    assert est.publish_collected_a(1.0) is True


def _give_a(est, mode="heat", sign=1.0):
    """a usable through a starting value (b may not have its samples)."""
    est.give_starting_values(A, None, mode, sign)
    assert est.usable_a


# -- residual power in b -------------------------------------------------------------------------------------------
def test_b_without_residual_power_is_unchanged():
    est = ABEstimator()
    _give_a(est)
    _learn_b(est, power=0.0)
    assert est.b_meas_hist[-1] == pytest.approx(-B_SLOPE / DELTA)
    assert est._b_obs_hist[-1] == (B_SLOPE, DELTA, 0.0, 1.0)


def test_b_counts_the_residual_heat_with_the_usable_a():
    est = ABEstimator()
    _give_a(est)
    _learn_b(est)
    assert est.b_meas_hist[-1] == pytest.approx((A * POWER - B_SLOPE) / DELTA)


def test_b_in_cooling_counts_the_residual_cooling():
    est = ABEstimator()
    est.ensure_hvac_mode(VThermHvacMode_COOL)
    _give_a(est, "cool", -1.0)
    assert est.a == -A
    # Room below the outdoor air, warming up while the cooler still cools.
    _learn_b(est, slope=0.006, mode=VThermHvacMode_COOL, t_int=24.0, t_ext=34.0)
    assert est.b_meas_hist[-1] == pytest.approx((-A * POWER - 0.006) / -10.0)
    assert est._b_obs_hist[-1][3] == -1.0


def test_b_before_a_is_known_does_not_count_the_residual_power_and_keeps_it():
    est = ABEstimator()
    assert not est.usable_a
    _learn_b(est)
    assert est.b_meas_hist[-1] == pytest.approx(-B_SLOPE / DELTA)
    assert est._b_obs_hist[-1] == (B_SLOPE, DELTA, POWER, 1.0)


# -- a -> b replay -------------------------------------------------------------------------------------------------
def _b_learned_before_a() -> ABEstimator:
    est = ABEstimator()
    for _ in range(AB_MIN_SAMPLES_B):
        _learn_b(est)
    assert est.learn_ok_count_b == 1 and est.b == pytest.approx(-B_SLOPE / DELTA)
    return est


def test_first_a_recomputes_the_b_measurements_once_without_replaying_a():
    est = _b_learned_before_a()
    est._replay_a_measurements = MagicMock(wraps=est._replay_a_measurements)
    est._replay_b_measurements = MagicMock(wraps=est._replay_b_measurements)
    _publish_a(est)
    expected = (A * POWER - B_SLOPE) / DELTA
    assert list(est.b_meas_hist) == pytest.approx([expected] * AB_MIN_SAMPLES_B)
    assert est.b == pytest.approx(expected)
    est._replay_b_measurements.assert_called_once()
    est._replay_a_measurements.assert_not_called()
    assert list(est.a_meas_hist) == [A] * 5


def test_a_starting_value_recomputes_the_b_measurements():
    est = _b_learned_before_a()
    est.give_starting_values(A, None, "heat", 1.0)
    assert est.b_meas_hist[-1] == pytest.approx((A * POWER - B_SLOPE) / DELTA)


def test_b_measurements_without_residual_power_or_of_the_other_mode_keep_their_value():
    est = ABEstimator()
    _learn_b(est, power=0.0)
    _learn_b(est, slope=0.006, mode=VThermHvacMode_COOL, t_int=24.0, t_ext=34.0)
    est.ensure_hvac_mode(VThermHvacMode_HEAT)
    est.b_meas_hist.appendleft(0.0005)          # restored without its observation
    before = list(est.b_meas_hist)
    _give_a(est)
    assert list(est.b_meas_hist) == before


def test_a_change_under_the_tolerance_does_not_replay_b():
    est = _b_learned_before_a()
    _publish_a(est)
    est._replay_b_measurements = MagicMock()
    est._follow_a(est.a - 0.5 * AB_VAL_TOLERANCE, 1.0)
    est._replay_b_measurements.assert_not_called()
    est._follow_a(est.a - 0.01, 1.0)
    est._replay_b_measurements.assert_called_once()


# -- b -> a replay -------------------------------------------------------------------------------------------------
def test_b_change_recomputes_a_once_without_replaying_b():
    est = ABEstimator()
    for slope in (-0.005, -0.006, -0.007, -0.008, -0.009):
        _learn_b(est, slope=slope, power=0.0)
    _publish_a(est)
    est._a_obs_hist.extend([(0.02, DELTA, 1.0)] * 5)
    est._replay_a_measurements = MagicMock(wraps=est._replay_a_measurements)
    est._replay_b_measurements = MagicMock(wraps=est._replay_b_measurements)
    b_before = est.b
    _learn_b(est, slope=-0.0085, power=0.0)
    assert est.b != b_before
    est._replay_a_measurements.assert_called_once()
    est._replay_b_measurements.assert_not_called()
    assert list(est.a_meas_hist) == pytest.approx([0.02 + est.b * DELTA] * 5)


# -- a kept until b is learned -------------------------------------------------------------------------------------
def test_a_windows_before_b_are_kept_and_recomputed_when_b_is_learned():
    est = ABEstimator()
    for _ in range(6):
        _learn_a(est)
        assert est.learn_last_reason.startswith("skip: a kept until b is learned")
    assert list(est.a_meas_hist) == pytest.approx([0.02 + est.B_INIT * DELTA] * 6)
    assert est.learn_ok_count_a == 0
    assert est.publish_collected_a(1.0) is False          # b has not its samples
    for _ in range(AB_A_SOFT_GATE_MIN_B):
        _learn_b(est, power=0.0)
    b = -B_SLOPE / DELTA
    assert est.b == pytest.approx(b)
    assert list(est.a_meas_hist) == pytest.approx([0.02 + b * DELTA] * 6)
    assert est.publish_collected_a(1.0) is True
    assert est.a == pytest.approx(0.02 + b * DELTA)


def test_kept_a_windows_respect_the_history_cap():
    est = ABEstimator()
    for _ in range(AB_HISTORY_SIZE + 9):
        _learn_a(est)
    assert len(est.a_meas_hist) == len(est._a_obs_hist) == AB_HISTORY_SIZE


def test_kept_a_window_of_the_wrong_sign_is_refused():
    est = ABEstimator()
    _learn_a(est, slope=-0.05)
    assert est.learn_last_reason == "skip: a_meas wrong hvac sign"
    assert not est.a_meas_hist


# -- persistence ---------------------------------------------------------------------------------------------------
def test_b_observations_survive_save_and_load():
    est = ABEstimator()
    est.b_meas_hist.append(0.0005)             # without observation
    _learn_b(est)
    restored = ABEstimator()
    restored.load_state(est.save_state())
    assert list(restored._b_obs_hist) == [None, (B_SLOPE, DELTA, POWER, 1.0)]
    _give_a(restored)
    assert restored.b_meas_hist[0] == 0.0005
    assert restored.b_meas_hist[1] == pytest.approx((A * POWER - B_SLOPE) / DELTA)


def test_state_without_b_observations_keeps_the_b_measurements():
    est = ABEstimator()
    _learn_b(est)
    state = est.save_state()
    del state["b_obs_hist"]
    restored = ABEstimator()
    restored.load_state(state)
    assert list(restored._b_obs_hist) == [None]
    _give_a(restored)
    assert restored.b_meas_hist[0] == pytest.approx(-B_SLOPE / DELTA)
