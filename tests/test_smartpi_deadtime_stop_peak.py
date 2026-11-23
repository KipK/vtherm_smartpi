"""Dead time after a stop read on the peak of the stop (direct start): early confirmation of the peak."""

import pytest

from custom_components.vtherm_smartpi.smartpi.const import HEATING_BALANCE_PEAK_BIN_S, HEATING_BALANCE_PEAK_CONFIRM_S
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator

BIN = HEATING_BALANCE_PEAK_BIN_S
TICK = 60.0


def _heat(est: DeadTimeEstimator, quantum: float = 0.01) -> float:
    """Full-power heating for 30 min, temperature rising by one sensor step a tick; returns the cut time."""
    t, temp = 0.0, 19.0
    for _ in range(30):
        t += TICK
        temp += quantum
        est.update(t, round(temp / quantum) * quantum, 1.0)
    return t + TICK


def _profile(rise_c: float, rise_s: float, fall_c_per_h: float, t0: float = 20.0):
    """Temperature after the cut (s since the cut): linear rise to the peak, then linear fall."""
    return lambda s: t0 + rise_c * min(s, rise_s) / rise_s - fall_c_per_h / 3600.0 * max(0.0, s - rise_s)


def _run(temp_at, duration_s: float, quantum: float | None = None, restart_s: float | None = None):
    """Heat, cut, then follow ``temp_at`` every minute; heating again at ``restart_s`` after the cut. Returns the
    estimator, the cut time and the time of the first sample of the dead time after a stop (None without one)."""
    est = DeadTimeEstimator(passive_by_peak=True)
    cut = _heat(est, quantum or 0.01)
    measured_at = None
    s = 0.0
    while s <= duration_s:
        temp = temp_at(s)
        if quantum:
            temp = round(temp / quantum) * quantum
        heating = restart_s is not None and s >= restart_s
        est.update(cut + s, temp, 1.0 if heating else 0.0)
        if measured_at is None and est.passive_measures:
            measured_at = cut + s
        if heating:
            break
        s += TICK
    return est, cut, measured_at


def test_fast_falling_room_confirms_after_one_closed_bin():
    est, cut, measured_at = _run(_profile(0.12, 600.0, 3.0), 3 * 3600.0)
    # Best bin 1; bin 2 is closed by the first sample of bin 3 and lies 0.06 C under it.
    assert measured_at == pytest.approx(cut + 3 * BIN)
    assert measured_at < cut + BIN + HEATING_BALANCE_PEAK_CONFIRM_S
    assert est.passive_measures[0]["value_s"] == pytest.approx(1.5 * BIN)
    assert est.passive_measures[0]["origin"] == "peak" and est.passive_measures[0]["fed"] is True
    assert est.deadtime_cool_s == pytest.approx(1.5 * BIN)
    assert est.stop_peak_confirmed is True


def test_room_losing_about_one_and_a_half_degree_an_hour_confirms_twenty_minutes_after_the_cut():
    """About 1.4 C/h: confirmed at the close of bin 3, not 1800 s after the best bin (close of bin 7)."""
    _, cut, measured_at = _run(_profile(0.12, 600.0, 1.4), 3 * 3600.0)
    assert measured_at == pytest.approx(cut + 4 * BIN)


def test_a_wall_mass_dip_under_the_threshold_does_not_confirm_the_first_maximum():
    def temp_at(s):
        # Up 0.2 C in 10 min, a 0.04 C dip, a second rise to +0.25 C at 25 min, then a slow fall.
        if s <= 600.0:
            return 20.0 + 0.2 * s / 600.0
        if s <= 900.0:
            return 20.2 - 0.04 * (s - 600.0) / 300.0
        if s <= 1500.0:
            return 20.16 + 0.09 * (s - 900.0) / 600.0
        return 20.25 - 0.3 / 3600.0 * (s - 1500.0)

    est, cut, measured_at = _run(temp_at, 4 * 3600.0)
    assert measured_at is not None
    assert est.passive_measures[0]["value_s"] == pytest.approx(5.5 * BIN)   # the second, true maximum
    assert len(est.passive_measures) == 1


def test_a_tenth_of_a_degree_sensor_needs_two_steps_under_the_best_bin():
    # Rounded to 0.1 C: 20.1 from 3 min, 20.0 from 16 min, 19.9 from 26 min after the cut.
    def temp_at(s):
        return 20.0 if s < 180.0 else 20.1 if s < 960.0 else 20.0 if s < 1560.0 else 19.9

    est, cut, measured_at = _run(temp_at, 3 * 3600.0, quantum=0.1)
    # Bin 4 is one step (0.1 C) under the best bin 1: enough for a 0.01 C sensor, not for this one. Bin 6 is two
    # steps under it: confirmed at its close (first sample of bin 7), before the 1800 s rule (close of bin 7).
    assert measured_at == pytest.approx(cut + 7 * BIN)
    assert est.passive_measures[0]["value_s"] == pytest.approx(1.5 * BIN)


def test_heating_again_during_a_bin_far_enough_under_the_best_confirms_the_peak():
    est, cut, measured_at = _run(_profile(0.12, 600.0, 3.0), 3 * 3600.0, restart_s=840.0)
    assert measured_at == pytest.approx(cut + 840.0)
    assert est.passive_measures[0]["value_s"] == pytest.approx(1.5 * BIN)
    assert est.deadtime_cool_s == pytest.approx(1.5 * BIN)
    # The heating runs again: nothing waits for the peak of that stop any more.
    assert est.stop_peak_confirmed is False and est.stop_response_pending is False


def test_heating_again_during_a_bin_too_close_to_the_best_gives_no_measure():
    est, cut, measured_at = _run(_profile(0.12, 600.0, 1.4), 3 * 3600.0, restart_s=840.0)
    assert measured_at is None
    assert est.deadtime_cool_s is None
    assert est.stop_response_pending is False


def test_the_bootstrap_start_reads_no_peak():
    est = DeadTimeEstimator()
    cut = _heat(est)
    temp_at = _profile(0.12, 600.0, 3.0)
    for k in range(30):
        est.update(cut + k * TICK, temp_at(k * TICK), 0.0)
    assert all(m["origin"] == "detection" for m in est.passive_measures)
