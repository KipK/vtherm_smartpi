"""The passive response after a stop is timed only once the active period has settled
(documentation/en/technical_doc.md, 3.6): the active period lasted at least the active dead time
and, with a published response lag, its settling time (ResponseLagEstimator.active_settle_s), the
same gate as an active learning window. Without a reliable active dead time no passive response
is timed. The stop is recorded either way."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import custom_components.vtherm_smartpi.algo as algo_module
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.response_lag import ResponseLagEstimator

START = 1000.0
LAG_S = 780.0


def _run_then_stop(
    est: DeadTimeEstimator,
    *,
    run_s: float,
    deadtime_s: float | None = None,
    settle_s: float | None = None,
    hvac_mode=VThermHvacMode_HEAT,
) -> float:
    """The power runs from START for ``run_s`` and stops; the room does not move meanwhile. The
    active dead time (heat in HEAT, cool in COOL) is ``deadtime_s``, reliable, or unknown; the
    response lag's settling time ``settle_s``. Returns the stop time."""
    channel = "cool" if hvac_mode == VThermHvacMode_COOL else "heat"
    est.model_hvac_mode = str(hvac_mode)  # the model's mode: a change of mode resets it
    if deadtime_s is not None:
        setattr(est, f"deadtime_{channel}_s", deadtime_s)
        setattr(est, f"deadtime_{channel}_reliable", True)
    est.active_settle_s = settle_s
    est.update(0.0, 20.0, 0.0, hvac_mode=hvac_mode)
    t = START
    while t < START + run_s:
        est.update(t, 20.0, 1.0, hvac_mode=hvac_mode)
        t = min(t + 60.0, START + run_s)
    est.update(START + run_s, 20.0, 0.0, hvac_mode=hvac_mode)
    return START + run_s


def _armed(est: DeadTimeEstimator, hvac_mode=VThermHvacMode_HEAT) -> bool:
    passive = "WAITING_HEAT_RESPONSE" if hvac_mode == VThermHvacMode_COOL else "WAITING_COOL_RESPONSE"
    return est.state == passive


@pytest.mark.parametrize(
    ("deadtime_s", "run_s", "armed"),
    [
        (780.0, 5 * 60.0, False),  # 5 minutes of heat in a room of 780 s
        (780.0, 15 * 60.0, True),
        (None, 30 * 60.0, False),  # no reliable heating dead time: no passive response at all
        (120.0, 5 * 60.0, True),  # a fast heater
        (2 * 3600.0, 3600.0, False),  # a floor heating, one hour
        (2 * 3600.0, 2.5 * 3600.0, True),
    ],
)
def test_the_passive_response_after_a_heating_that_lasted_its_dead_time(deadtime_s, run_s, armed) -> None:
    """Without a published response lag the heating must last the heating dead time; the stop is
    recorded either way."""
    est = DeadTimeEstimator()
    stop = _run_then_stop(est, run_s=run_s, deadtime_s=deadtime_s)

    assert _armed(est) is armed
    assert est.last_stop_time == stop
    assert est.last_start_time == START
    if not armed:
        assert est.state == "COOLING" and est.cool_start_time is None


def test_a_passive_response_armed_after_a_settled_heating_is_measured_as_before() -> None:
    """Once armed, the detection is the author's: the room still rises, then drops; the dead
    time runs to the last instant near the peak."""
    est = DeadTimeEstimator()
    stop = _run_then_stop(est, run_s=15 * 60.0, deadtime_s=LAG_S)
    assert (est.state, est.cool_start_time, est.cool_peak_temp) == ("WAITING_COOL_RESPONSE", stop, 20.0)

    est.update(stop + 60.0, 20.1, 0.0)
    est.update(stop + 360.0, 20.1, 0.0)
    est.update(stop + 420.0, 20.04, 0.0)

    assert est.last_outcome["cool"] == "detected"
    assert est.deadtime_cool_s == pytest.approx(360.0)


def test_a_short_heating_drops_the_heating_detection_it_armed() -> None:
    """A stop before the heating settled ends the heating detection still waiting, as a stop
    always does, and arms nothing."""
    est = DeadTimeEstimator()
    est.deadtime_heat_s, est.deadtime_heat_reliable = LAG_S, True
    est.update(0.0, 20.0, 0.0)
    est.update(START, 20.0, 1.0)  # no stop known: the heating detection arms
    assert est.state == "WAITING_HEAT_RESPONSE"

    est.update(START + 300.0, 20.0, 0.0)

    assert (est.state, est.last_outcome["heat"], est.last_outcome["cool"]) == ("COOLING", "aborted", None)


@pytest.mark.parametrize(("run_s", "armed"), [(5 * 60.0, False), (15 * 60.0, True)])
def test_in_cooling_the_passive_response_follows_a_cooling_that_lasted_its_dead_time(run_s, armed) -> None:
    """In HVAC COOL the active response is cooling: the rise after a stop is timed only once the
    cooling lasted its dead time."""
    est = DeadTimeEstimator()
    _run_then_stop(est, run_s=run_s, deadtime_s=LAG_S, hvac_mode=VThermHvacMode_COOL)

    assert _armed(est, VThermHvacMode_COOL) is armed
    if not armed:
        assert est.heat_start_time is None and est.last_outcome["heat"] is None


def _lag(median_heating_s: float) -> ResponseLagEstimator:
    """A response lag of 780 s published, with three heating periods of ``median_heating_s``."""
    lag = ResponseLagEstimator("test")
    lag.load_state({"estimates": [[LAG_S, 10.0]] * 3, "durations": [median_heating_s] * 3, "published": LAG_S, "episodes": 3})
    return lag


@pytest.mark.parametrize(
    ("median_heating_s", "threshold_s"),
    [
        (2 * 3600.0, 3 * LAG_S),  # three lags: the response has settled within 5 %
        (40 * 60.0, 20 * 60.0),  # the author's cap, half the median heating period
        (20 * 60.0, LAG_S),  # the cap would fall below the dead time: the dead time
    ],
)
def test_with_a_published_lag_the_heating_lasts_the_settling_time(median_heating_s, threshold_s) -> None:
    """With a published response lag the heating must last max(heating dead time, the author's
    settling time of an active window): min(3 lags, half the median heating period)."""
    settle_s = _lag(median_heating_s).active_settle_s
    assert settle_s == pytest.approx(min(3 * LAG_S, median_heating_s / 2))

    short, long_ = DeadTimeEstimator(), DeadTimeEstimator()
    _run_then_stop(short, run_s=threshold_s - 60.0, deadtime_s=LAG_S, settle_s=settle_s)
    _run_then_stop(long_, run_s=threshold_s, deadtime_s=LAG_S, settle_s=settle_s)

    assert not _armed(short) and _armed(long_)


def test_smartpi_gives_the_estimator_the_author_s_settling_time(monkeypatch) -> None:
    """Each calculation passes the response lag's settling time to the dead-time estimator."""
    monkeypatch.setattr(algo_module.time, "monotonic", lambda: 5000.0)
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="Settle")
    smartpi.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)
    assert smartpi.dt_est.active_settle_s is None

    smartpi.learn_win.response_lag.load_state(_lag(2 * 3600.0).save_state())
    smartpi.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=5.0, slope=0.0, hvac_mode=VThermHvacMode_HEAT)

    assert smartpi.dt_est.active_settle_s == pytest.approx(3 * LAG_S)


@pytest.mark.parametrize(("restart_after_s", "armed_heat"), [(300.0, False), (700.0, True)])
def test_a_start_ends_a_passive_detection_still_waiting(restart_after_s, armed_heat) -> None:
    """The power starts again while the passive response is still waited for: the detection ends
    "aborted", whether the start is refused (less than 600 s without heat) or measured."""
    est = DeadTimeEstimator()
    stop = _run_then_stop(est, run_s=15 * 60.0, deadtime_s=LAG_S)
    assert est.state == "WAITING_COOL_RESPONSE"

    est.update(stop + restart_after_s, 20.0, 1.0)

    assert est.last_outcome["cool"] == "aborted"
    assert (est.state == "WAITING_HEAT_RESPONSE") is armed_heat
