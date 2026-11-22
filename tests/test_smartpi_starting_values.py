"""Starting values of the room model: given once, released at the learning's own values, latched ramp credit."""

import json
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff_gain, compute_ff_warmup_scale
from custom_components.vtherm_smartpi.smartpi.learning_start import LearningStart
from custom_components.vtherm_smartpi.smartpi.starting_values import KEPT_RAMP_MEASURES, StartingValues

A_START, B_START = 0.08, 0.0004


def _own_b_sample(est: ABEstimator, k: int) -> None:
    """One passive measurement of b: about 2e-4 1/min, a little different each time."""
    est.learn(-0.002 * (1.0 + 0.01 * (k % 3)), 0.0, 20.0, 10.0, hvac_mode=VThermHvacMode_HEAT)


def _make_direct() -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="SV",
                   learning_start="direct")


def _kept(a=A_START, b=B_START, mode="heat") -> dict:
    return {"mode": mode, "a_per_min": a, "lag_s": 300.0, "b_per_min": b, "pulses": [1, 2]}


def test_values_are_given_once_per_blank_start():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    assert est.start.standing("a") and est.start.standing("b")
    assert est.a == A_START and est.b == B_START and est.usable_a and est.usable_b
    est.give_starting_values(0.2, 0.001, "heat", 1.0)       # a second give changes nothing
    assert est.a == A_START and est.b == B_START and est.start.credit == 2 * KEPT_RAMP_MEASURES


def test_a_given_in_cool_takes_the_sign_and_b_out_of_bounds_is_not_given():
    est = ABEstimator()
    est.give_starting_values(A_START, 1.0, "cool", -1.0)
    assert est.a == -A_START and est.start.standing("a") and not est.start.standing("b")
    assert est.start.credit == KEPT_RAMP_MEASURES and est.b == est.B_INIT


def test_a_is_released_at_the_first_own_publication_and_takes_the_own_value():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    est.a_meas_hist.extend([0.05] * 5)
    assert est.publish_collected_a(1.0) is True
    assert est.learn_ok_count_a == 1 and not est.start.standing("a") and est.start.standing("b")
    assert est.a == pytest.approx(0.05) and est.usable_a


def test_b_stays_standing_until_the_own_time_constant_is_reliable_then_takes_the_own_value():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    for k in range(4):
        _own_b_sample(est, k)
    assert est.start.standing("b") and est.b == B_START           # own b is not written while the value stands
    assert est.tau_reliability().tau_min == pytest.approx(1.0 / B_START)
    for k in range(4, 14):
        _own_b_sample(est, k)
        if not est.start.standing("b"):
            break
    assert not est.start.standing("b") and est.start.own_b and est.usable_b
    assert est.learn_ok_count_b >= 5 and est.b == pytest.approx(2e-4, rel=0.1)


def test_own_b_reliable_before_the_give_releases_at_once():
    est = ABEstimator()
    for k in range(12):
        _own_b_sample(est, k)
    own = est.b
    assert est.start.own_b
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    assert not est.start.standing("b") and est.b == own and est.start.credit == 2 * KEPT_RAMP_MEASURES


def test_credit_is_latched_at_the_give_and_the_ramp_never_decreases_across_releases():
    est = ABEstimator()
    assert est.ramp_count == 0
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    assert est.ramp_count == 2 * KEPT_RAMP_MEASURES
    ramp = [est.ramp_count]
    est.a_meas_hist.extend([0.05] * 5)
    est.publish_collected_a(1.0)                                   # release of a: the credit stays
    ramp.append(est.ramp_count)
    for k in range(14):
        _own_b_sample(est, k)                                      # release of b somewhere in here
        ramp.append(est.ramp_count)
    assert est.start.own_b and ramp == sorted(ramp) and ramp[0] == 2 * KEPT_RAMP_MEASURES
    assert est.ramp_count == max(2 * KEPT_RAMP_MEASURES, est.learn_ok_count)
    scales = [compute_ff_warmup_scale(learn_ok_count=n, ff_warmup_ok_count=30, cycles_since_reset=100, ff_warmup_cycles=10)
              for n in ramp]
    assert scales == sorted(scales)


def test_the_ramp_reads_the_larger_of_the_credit_and_the_accepted_measures():
    sv = StartingValues()
    sv.give(A_START, B_START, "heat")
    assert sv.ramp_count(3) == 10 and sv.ramp_count(10) == 10 and sv.ramp_count(25) == 25


def test_the_feed_forward_model_is_ready_with_standing_values():
    est = ABEstimator()
    assert compute_ff_gain(est, VThermHvacMode_HEAT, True) == 0.0
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    assert compute_ff_gain(est, VThermHvacMode_HEAT, True) == pytest.approx(B_START / A_START)


def test_a_learning_reset_drops_the_standing_values_and_the_credit():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    est.reset()
    assert not est.usable_a and not est.usable_b and est.ramp_count == 0 and est.start.handed is False
    est.give_starting_values(A_START, B_START, "heat", 1.0)       # a blank start can be given again
    assert est.start.standing("a") and est.start.standing("b")


def test_the_reset_of_the_mode_channel_drops_a_but_keeps_b_and_the_credit():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    est.ensure_hvac_mode(VThermHvacMode_HEAT)
    est.ensure_hvac_mode(VThermHvacMode_COOL)
    assert not est.start.standing("a") and est.start.standing("b") and est.ramp_count == 2 * KEPT_RAMP_MEASURES


def test_starting_values_survive_a_save_and_a_load():
    est = ABEstimator()
    est.give_starting_values(A_START, B_START, "heat", 1.0)
    state = json.loads(json.dumps(est.save_state()))
    assert state["starting_values"] == {"a": A_START, "b": B_START, "mode": "heat", "handed": True, "credit": 10}
    other = ABEstimator()
    other.load_state(state)
    assert other.start.standing("a") and other.start.standing("b") and other.ramp_count == 10 and other.start.handed


def test_a_malformed_saved_block_is_a_blank_start():
    sv = StartingValues()
    for bad in (None, 3, {"handed": True, "credit": "x"}, {"handed": True, "credit": 5, "a": "x"}, {"handed": False}):
        sv.load(bad)
        assert sv.handed is False and sv.credit == 0 and not sv.standing("a")


def test_a_state_of_the_earlier_tree_hands_over_the_kept_values_at_its_load():
    """The earlier tree saved no starting values: the kept values and the estimator counters tell what stood."""
    algo = _make_direct()
    algo._start.reading.load({"kept": _kept(), "at_wall": 1.0e9})
    state = json.loads(json.dumps(algo.est.save_state()))
    assert "starting_values" in state
    del state["starting_values"]
    other = _make_direct()
    other.est.load_state(state)
    other._start.reading.load({"kept": _kept(), "at_wall": 1.0e9})
    other._start.hand_over(other.est)
    assert other.est.start.standing("a") and other.est.start.standing("b") and other.est.ramp_count == 10
    assert other.est.a == A_START and other.est.b == B_START
    # a state whose own a had been published: a is released at the hand-over, the credit still counts both
    state["learn_ok_count_a"] = 1
    third = _make_direct()
    third.est.load_state(state)
    third._start.reading.load({"kept": _kept(), "at_wall": 1.0e9})
    third._start.hand_over(third.est)
    assert not third.est.start.standing("a") and third.est.start.standing("b") and third.est.ramp_count == 10


def test_the_hand_over_gives_the_kept_values_once_and_not_again_after_a_release():
    algo = _make_direct()
    start: LearningStart = algo._start
    start.hand_over(algo.est)
    assert algo.est.start.handed is False                          # nothing kept: nothing given
    start.reading._kept = dict(_kept(), at_s=0.0)
    start.hand_over(algo.est)
    assert algo.est.start.standing("a") and algo.est.a == A_START
    algo.est.a_meas_hist.extend([0.05] * 5)
    algo.est.publish_collected_a(1.0)
    start.hand_over(algo.est)                                      # a is not given again
    assert not algo.est.start.standing("a") and algo.est.a == pytest.approx(0.05)


def _make_bootstrap() -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="B")


def _end_the_stage(algo: SmartPI) -> None:
    """The bootstrap's own collections that end the learning stage: five measures of a and of b."""
    algo.est.a_meas_hist.extend([0.05] * 5)
    algo.est.b_meas_hist.extend([0.001] * 5)
    algo.est.learn_ok_count_a = 1
    assert algo.phase != SmartPIPhase.HYSTERESIS


def test_the_bootstrap_start_gives_nothing_without_kept_values():
    algo = _make_bootstrap()
    assert algo._start.reading.kept is None
    algo._start.hand_over(algo.est)
    assert algo.est.start.handed is False and algo.est.ramp_count == algo.est.learn_ok_count == 0


def test_the_bootstrap_start_gives_the_kept_values_once_the_stage_has_ended():
    algo = _make_bootstrap()
    algo._start.reading._kept = dict(_kept(), at_s=0.0)
    algo._start.hand_over(algo.est)
    assert algo.phase == SmartPIPhase.HYSTERESIS and algo.est.start.handed is False      # the stage lasts: nothing given
    _end_the_stage(algo)
    algo._start.hand_over(algo.est)
    start = algo.est.start
    assert start.handed and start.credit == 2 * KEPT_RAMP_MEASURES
    assert not start.standing("a")                              # the learning has published its own a: released at once
    assert start.standing("b") and algo.est.b == B_START and algo.est.usable_b
    assert algo.est.ramp_count == 10 and algo.est.tau_reliability().reliable
    algo._start.hand_over(algo.est)
    assert algo.est.ramp_count == 10                            # given once


def test_bootstrap_starting_values_follow_the_release_rules_of_the_owner():
    algo = _make_bootstrap()
    algo._start.reading._kept = dict(_kept(), at_s=0.0)
    _end_the_stage(algo)
    algo._start.hand_over(algo.est)
    est = algo.est
    for k in range(14):
        _own_b_sample(est, k)
        if not est.start.standing("b"):
            break
    assert not est.start.standing("b") and est.start.own_b       # released when the own time constant is reliable
    assert est.ramp_count == max(10, est.learn_ok_count)


def test_the_lag_of_the_bootstrap_readings_is_published_not_used():
    bootstrap, direct = _make_bootstrap(), _make_direct()
    for algo in (bootstrap, direct):
        algo._start.reading._kept = dict(_kept(), lag_s=420.0, at_s=0.0)
    before = bootstrap.anticipation_horizon(VThermHvacMode_HEAT)
    assert bootstrap._start.kept_lag_s(VThermHvacMode_HEAT) is None
    assert bootstrap._start.reading.kept_lag_s(VThermHvacMode_HEAT) == 420.0
    bootstrap._start.advance(1000.0, 20.0, 19.0, VThermHvacMode_HEAT, 0.0)
    assert bootstrap.learn_win.response_lag.lag_s is None
    assert bootstrap.anticipation_horizon(VThermHvacMode_HEAT) == before
    assert before.seconds != 420.0
    direct._start.advance(1000.0, 20.0, 19.0, VThermHvacMode_HEAT, 0.0)
    assert direct._start.kept_lag_s(VThermHvacMode_HEAT) == 420.0
    assert direct.anticipation_horizon(VThermHvacMode_HEAT).seconds == 420.0


def test_the_bootstrap_start_runs_the_reader_and_no_pulse():
    algo = _make_bootstrap()
    algo._start.advance(1000.0, 20.0, 19.0, VThermHvacMode_HEAT, 0.0)
    assert algo._start.record.enabled is True and algo._start.pulse.active is False
    assert algo.command_is_hysteresis is True and algo._start.pulse.save(1000.0) is None
