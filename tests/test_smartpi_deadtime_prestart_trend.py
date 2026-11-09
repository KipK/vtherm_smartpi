"""Active dead time measured against the free-running trend before the start."""

from __future__ import annotations

import math
import random

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi import deadtime_estimator
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.deadtime_trend import (
    NO_TREND,
    prestart_trend,
)
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter

START_TS = 100_000.0
TICK_S = 60.0
SENSOR_STEP_C = 0.01


def _room_temp(
    dt_s: float,
    *,
    pre_c_per_h: float,
    deadtime_s: float,
    rate_c_per_h: float,
    lag_s: float,
    prestart=None,
) -> float:
    """Free-running trend plus the delayed response to a full power start."""
    if dt_s < 0.0 and prestart is not None:
        return 20.0 + prestart(dt_s)
    temp = 20.0 + pre_c_per_h / 3600.0 * dt_s
    elapsed = dt_s - deadtime_s
    if elapsed > 0.0:
        rate = rate_c_per_h / 3600.0
        if lag_s > 0.0:
            temp += rate * (elapsed - lag_s * (1.0 - math.exp(-elapsed / lag_s)))
        else:
            temp += rate * elapsed
    return temp


def _drive(
    est: DeadTimeEstimator,
    *,
    pre_c_per_h: float = 0.0,
    deadtime_s: float = 300.0,
    rate_c_per_h: float = 3.0,
    lag_s: float = 0.0,
    noise_c: float = 0.0,
    off_min: float = 60.0,
    hvac_mode=VThermHvacMode_HEAT,
    prestart=None,
    seed: int = 1,
) -> None:
    """Feed an OFF period then a full power start, as SmartPI does, until the wait ends."""
    rnd = random.Random(seed)
    sign = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
    filt = AdaptiveTintFilter("test")
    now = START_TS - off_min * 60.0
    while now <= START_TS + 7200.0:
        dt_s = now - START_TS
        temp = 20.0 + sign * (
            _room_temp(
                dt_s,
                pre_c_per_h=pre_c_per_h,
                deadtime_s=deadtime_s,
                rate_c_per_h=rate_c_per_h,
                lag_s=lag_s,
                prestart=prestart,
            )
            - 20.0
        )
        raw = round((temp + rnd.gauss(0.0, noise_c)) / SENSOR_STEP_C) * SENSOR_STEP_C
        t_int_lp, _ = filt.update(raw, now)
        est.update(
            now,
            t_int_lp,
            1.0 if now >= START_TS else 0.0,
            hvac_mode=hvac_mode,
            tin_raw=raw,
        )
        waiting = est.state in {"WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE"}
        if now >= START_TS and not waiting:
            return
        now += TICK_S


@pytest.fixture
def without_trend(monkeypatch):
    """Return a factory of estimators that measure from the start level only."""

    def factory() -> DeadTimeEstimator:
        monkeypatch.setattr(
            deadtime_estimator, "prestart_trend", lambda *args, **kwargs: NO_TREND
        )
        return DeadTimeEstimator()

    return factory


def _measure(**kwargs) -> tuple[str | None, float | None, float]:
    est = DeadTimeEstimator()
    _drive(est, **kwargs)
    return est.last_outcome["heat"], est.deadtime_heat_s, est._trend_slope


@pytest.mark.parametrize("noise_c", [0.0, 0.006])
@pytest.mark.parametrize("lag_s", [0.0, 300.0])
def test_flat_prestart_detection_is_unchanged(without_trend, noise_c, lag_s) -> None:
    """A flat room before the start gives the trough-based dead time."""
    outcome, deadtime, slope = _measure(noise_c=noise_c, lag_s=lag_s)
    reference = without_trend()
    _drive(reference, noise_c=noise_c, lag_s=lag_s)

    assert slope == 0.0
    assert outcome == "detected"
    assert deadtime == reference.deadtime_heat_s


@pytest.mark.parametrize("lag_s", [0.0, 300.0])
def test_rising_prestart_is_measured_against_its_trend(without_trend, lag_s) -> None:
    """A room already warming before the start: the response is the deviation from that trend."""
    flat_outcome, flat_deadtime, _ = _measure(lag_s=lag_s)
    outcome, deadtime, slope = _measure(pre_c_per_h=0.2, lag_s=lag_s)
    reference = without_trend()
    _drive(reference, pre_c_per_h=0.2, lag_s=lag_s)

    assert slope * 3600.0 == pytest.approx(0.2, abs=0.03)
    assert outcome == "detected"
    # Close to the same room without the trend (one or two samples apart),
    # where the start level alone dates the response far too early.
    assert abs(deadtime - flat_deadtime) <= 2 * TICK_S
    assert reference.deadtime_heat_s <= flat_deadtime - 3 * TICK_S


def test_irregular_prestart_is_rejected_and_keeps_the_value() -> None:
    """A warming trend too noisy to be extrapolated rejects the measurement."""
    est = DeadTimeEstimator()
    est._add_sample_heat(250.0)

    _drive(est, pre_c_per_h=0.2, noise_c=0.04)

    assert est.last_outcome["heat"] == "rejected"
    assert est.state == "HEATING"
    assert list(est._history_heat) == [250.0]
    assert est.deadtime_heat_s == 250.0


def test_prestart_bending_towards_the_response_is_rejected() -> None:
    """Flat, then warming in the last minutes: a line would under-estimate the trend."""

    def bend(dt_s: float) -> float:
        return max(0.0, dt_s + 900.0) * 0.4 / 3600.0 - 0.1

    est = DeadTimeEstimator()
    est._add_sample_heat(250.0)
    _drive(est, prestart=bend)

    assert est.last_outcome["heat"] == "rejected"
    assert est.deadtime_heat_s == 250.0


def test_steep_rising_prestart_is_rejected_at_detection() -> None:
    """A trend that carries the detection threshold by itself is not separable."""
    est = DeadTimeEstimator()
    est._add_sample_heat(250.0)

    _drive(est, pre_c_per_h=0.8)

    assert est.last_outcome["heat"] == "rejected"
    assert est._trend_slope > 0.0
    assert est.deadtime_heat_s == 250.0


@pytest.mark.parametrize("pre_c_per_h", [-0.3, -1.0])
def test_cooling_prestart_keeps_the_trough_detection(without_trend, pre_c_per_h) -> None:
    """A room cooling before the start is left to the trough tracking, as before."""
    outcome, deadtime, slope = _measure(pre_c_per_h=pre_c_per_h, lag_s=300.0)
    reference = without_trend()
    _drive(reference, pre_c_per_h=pre_c_per_h, lag_s=300.0)

    assert slope == 0.0
    assert outcome == "detected"
    assert deadtime == reference.deadtime_heat_s
    # The trough comes after the onset of the response.
    assert deadtime >= 300.0


def test_short_off_period_keeps_the_trough_detection(without_trend) -> None:
    """Without 15 minutes of free-running samples no trend is removed."""
    outcome, deadtime, slope = _measure(pre_c_per_h=0.2, off_min=12.0)
    reference = without_trend()
    _drive(reference, pre_c_per_h=0.2, off_min=12.0)

    assert slope == 0.0
    assert outcome == reference.last_outcome["heat"]
    assert deadtime == reference.deadtime_heat_s


def test_active_cool_response_is_measured_against_a_falling_trend() -> None:
    """In COOL a room already cooling before the start is the mirrored case."""
    flat = DeadTimeEstimator()
    _drive(flat, hvac_mode=VThermHvacMode_COOL)
    est = DeadTimeEstimator()
    _drive(est, pre_c_per_h=0.2, hvac_mode=VThermHvacMode_COOL)

    assert est._trend_slope * 3600.0 == pytest.approx(-0.2, abs=0.03)
    assert est.last_outcome["cool"] == "detected"
    assert abs(est.deadtime_cool_s - flat.deadtime_cool_s) <= 2 * TICK_S


def test_passive_response_is_not_detrended() -> None:
    """A power cut keeps the peak tracking: the trend is that of the heating."""
    est = DeadTimeEstimator()
    now = START_TS
    for _ in range(30):
        est.update(now, 20.0 + (now - START_TS) / 3600.0, 1.0, tin_raw=20.0)
        now += TICK_S
    est.update(now, 20.5, 0.0)

    assert est.state == "WAITING_COOL_RESPONSE"
    assert est._trend_slope == 0.0


def test_trend_window_starts_after_the_passive_delay() -> None:
    """Right after a stop the emitter still acts: those samples are left out."""
    est = DeadTimeEstimator()
    est.last_stop_time = 1000.0
    assert est._free_running_since(VThermHvacMode_HEAT) == 1000.0
    est._add_sample_cool(600.0)
    assert est._free_running_since(VThermHvacMode_HEAT) == 1600.0
    est.last_stop_time = None
    assert est._free_running_since(VThermHvacMode_HEAT) is None


def _history(values: list[float], start: float = START_TS) -> list[tuple[float, float]]:
    return [(start - (len(values) - 1 - i) * TICK_S, v) for i, v in enumerate(values)]


def test_prestart_trend_flat_and_short_windows() -> None:
    """A constant window or less than 15 minutes of samples give no trend."""
    kwargs = {"start_ts": START_TS, "off_since_ts": None, "direction": 1.0, "detection_threshold_c": 0.05}
    assert prestart_trend(_history([20.0] * 31), **kwargs) == NO_TREND
    rising = [20.0 + 0.01 * i for i in range(10)]
    assert prestart_trend(_history(rising), **kwargs) == NO_TREND
    # The OFF period bounds the window.
    long_rising = [20.0 + 0.005 * i for i in range(31)]
    bounded = dict(kwargs, off_since_ts=START_TS - 600.0)
    assert prestart_trend(_history(long_rising), **bounded) == NO_TREND
    assert prestart_trend(_history(long_rising), **kwargs).slope_c_per_s > 0.0


def test_prestart_trend_within_one_sensor_step_is_flat() -> None:
    """A fitted change below one sensor step over the window is not a trend."""
    kwargs = {"start_ts": START_TS, "off_since_ts": None, "direction": 1.0, "detection_threshold_c": 0.05}
    dither = [20.0 + (0.01 if i in (24, 27, 29) else 0.0) for i in range(31)]
    assert prestart_trend(_history(dither), **kwargs) == NO_TREND
