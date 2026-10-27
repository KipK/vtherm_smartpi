"""The first a publication may come early when the collected measurements agree."""
import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_A_CONSISTENT,
)
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff_gain

T_INT, T_EXT = 20.0, 5.0
DELTA = T_INT - T_EXT
A_TRUE, B_TRUE = 0.025, 3.0e-4


def _estimator() -> ABEstimator:
    est = ABEstimator()
    for _ in range(AB_A_SOFT_GATE_MIN_B):
        est.learn(dT_int_per_min=-B_TRUE * DELTA, u=0.0, t_int=T_INT, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT)
    return est


def _learn_a(est: ABEstimator, factors: list[float]) -> None:
    for f in factors:
        est.learn(dT_int_per_min=A_TRUE * f - est.b * DELTA, u=1.0, t_int=T_INT, t_ext=T_EXT)


def test_agreeing_measurements_publish_a_before_the_full_count():
    est = _estimator()
    _learn_a(est, [1.0, 1.01, 0.99, 1.0])
    assert AB_MIN_SAMPLES_A_CONSISTENT < AB_MIN_SAMPLES_A
    assert len(est.a_meas_hist) == AB_MIN_SAMPLES_A_CONSISTENT
    assert est.learn_ok_count_a == 1
    assert est.a == pytest.approx(A_TRUE, rel=0.02)


def test_scattered_measurements_wait_for_the_full_count():
    est = _estimator()
    _learn_a(est, [1.0, 1.3, 0.75, 1.2])
    assert len(est.a_meas_hist) == AB_MIN_SAMPLES_A_CONSISTENT
    assert est.learn_ok_count_a == 0


def test_fewer_agreeing_measurements_do_not_publish():
    est = _estimator()
    _learn_a(est, [1.0] * (AB_MIN_SAMPLES_A_CONSISTENT - 1))
    assert est.learn_ok_count_a == 0


def test_waiting_for_the_response_lag_blocks_the_early_publication():
    est = _estimator()
    est.a_model_ready = False
    _learn_a(est, [1.0, 1.01, 0.99, 1.0])
    assert est.learn_ok_count_a == 0

    est.a_model_ready = True
    assert est.publish_collected_a(1.0)
    assert est.learn_ok_count_a == 1
    assert est.a == pytest.approx(A_TRUE, rel=0.02)


def test_feedforward_uses_an_early_published_a():
    est = _estimator()
    _learn_a(est, [1.0, 1.01, 0.99, 1.0])
    assert len(est.a_meas_hist) < AB_MIN_SAMPLES_A
    assert compute_ff_gain(est, VThermHvacMode_HEAT, True) == pytest.approx(est.b / est.a)
