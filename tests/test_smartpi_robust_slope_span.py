"""Span guard of the robust learning slope."""
import random
import statistics

from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator


def _published_history(seed: int, minutes: int, slope_c_per_min: float, step: float = 0.1, noise: float = 0.05):
    """Learning history of a noisy sensor published on value change.

    The sensor is sampled every 10 s, publishes only when its quantised value
    changes, and the history takes a sample at each publication and at least
    every 60 s, as in SmartPI.
    """
    rng = random.Random(seed)
    history, published, t = [], None, 0.0
    while t <= minutes * 60:
        value = round(round((20.03 + slope_c_per_min * t / 60 + rng.uniform(-noise, noise)) / step) * step, 2)
        changed = published is None or value != published
        if changed:
            published = value
        if changed or t % 60 == 0:
            if not history or history[-1][1] != published or t - history[-1][0] >= 60:
                history.append((t, published))
        t += 10.0
    return history


def _first_accepted(seed: int, slope_c_per_min: float, step: float, noise: float):
    """Evaluate the slope each minute, as the learning window does, until accepted."""
    full = _published_history(seed, 240, slope_c_per_min, step, noise)
    for minute in range(10, 241):
        window = [p for p in full if p[0] <= minute * 60]
        estimate, method, _ = ABEstimator.robust_dTdt_per_min(window, trim_start_frac=0.10)
        if estimate is not None:
            return estimate, method, minute
    return None, None, None


def test_slow_decay_seen_by_a_flickering_sensor_is_not_submitted_early():
    slope = -0.0015  # 0.09 C/h, a slow passive decay
    ratios, minutes = [], []
    for seed in range(20):
        estimate, method, minute = _first_accepted(seed, slope, 0.1, 0.05)
        assert estimate is not None
        assert method == "ols_ttest"
        ratios.append(estimate / slope)
        minutes.append(minute)
    # 1.5 steps of 0.1 C at 0.09 C/h take about 100 min of fitted change
    assert min(minutes) >= 90
    assert 0.9 <= statistics.median(ratios) <= 1.1
    assert max(abs(r - 1.0) for r in ratios) <= 0.3


def test_short_window_is_rejected_by_the_span_guard():
    estimate, method, _ = ABEstimator.robust_dTdt_per_min(
        _published_history(0, 45, -0.0015), trim_start_frac=0.10
    )
    assert estimate is None
    assert method in ("span_below_resolution", "slope_not_significant", "too_few_jumps")


def test_fine_sensor_keeps_a_fast_acceptance():
    estimate, method, minute = _first_accepted(0, -0.005, 0.01, 0.02)
    assert method == "ols_ttest"
    assert minute <= 30
    assert abs(estimate / -0.005 - 1.0) <= 0.2
