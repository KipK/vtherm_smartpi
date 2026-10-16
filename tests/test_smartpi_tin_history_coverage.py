"""The dead time estimator temperature histories cover a full learning window."""

from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import DT_MAX_MIN
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator


def _feed_fast_sensor(est: DeadTimeEstimator, start: float, minutes: float, period_s: float) -> float:
    """Feed a sensor that reports a new value every period_s seconds (slow decay + dither)."""
    now = start
    i = 0
    while now - start < minutes * 60.0:
        raw = 21.0 - 0.005 * (now - start) / 60.0 + (0.01 if i % 2 else 0.0)
        est.update(now, raw, 0.0, tin_raw=round(raw, 2))
        now += period_s
        i += 1
    return now - period_s


def test_fast_sensor_history_covers_a_full_learning_window() -> None:
    """A sensor reporting every 15 s keeps the samples of the last DT_MAX_MIN minutes."""
    est = DeadTimeEstimator()
    start = 1000.0
    last = _feed_fast_sensor(est, start, minutes=DT_MAX_MIN + 30, period_s=15.0)

    window_start = last - DT_MAX_MIN * 60.0
    for history in (est.tin_history, est._tin_lp_history):
        assert history[0][0] <= window_start


def test_window_slope_samples_start_at_window_start() -> None:
    """The samples selected by a 200 min learning window begin at its start."""
    est = DeadTimeEstimator()
    start = 1000.0
    last = _feed_fast_sensor(est, start, minutes=210, period_s=15.0)

    window_start = last - 200 * 60.0
    samples = [p for p in est.tin_history if p[0] >= window_start]
    assert samples[0][0] - window_start < 15.0
    slope, method, _ = ABEstimator.robust_dTdt_per_min(samples)
    assert method == "ols_ttest"
    assert abs(slope - (-0.005)) < 0.0005


def test_samples_older_than_retention_are_dropped() -> None:
    """Samples older than the retention duration leave both histories."""
    from custom_components.vtherm_smartpi.smartpi.const import (
        TIN_HISTORY_RETENTION_MIN,
    )

    assert TIN_HISTORY_RETENTION_MIN >= DT_MAX_MIN
    est = DeadTimeEstimator()
    start = 1000.0
    now = start
    for i in range(TIN_HISTORY_RETENTION_MIN + 120):
        est.update(now, 20.0 + 0.1 * (i % 2), 0.0)
        now += 60.0
    last = now - 60.0

    for history in (est.tin_history, est._tin_lp_history):
        assert last - history[0][0] <= TIN_HISTORY_RETENTION_MIN * 60.0
        assert last - history[0][0] >= TIN_HISTORY_RETENTION_MIN * 60.0 - 60.0
