import logging
import pytest
import time
from unittest.mock import MagicMock
from datetime import datetime, timedelta

from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.smartpi.ab_estimator import ABEstimator


def test_ols_slope_accuracy():
    """Test OLS slope estimation accuracy on regular data."""
    # 6 points: 0.00, 0.05, 0.10, 0.15, 0.20, 0.25 -> 5 jumps, amp=0.25
    samples = []
    for i in range(6):
        samples.append((i * 60.0, float(i * 0.05)))

    slope_min, method, n = ABEstimator.robust_dTdt_per_min(samples)
    assert method == "ols_ttest"
    assert n == 6
    assert abs(slope_min - 0.05) < 0.02


def test_reject_single_jump_01():
    """0.1C sensor: 1 jump is not enough for OLS."""
    # 30 samples at 20.0, then 10 at 19.9 -> 1 jump
    samples = []
    for i in range(30):
        samples.append((i * 20.0, 20.0))
    for i in range(10):
        samples.append(((30 + i) * 20.0, 19.9))

    slope, reason, _ = ABEstimator.robust_dTdt_per_min(samples)
    assert slope is None
    assert reason == "too_few_jumps"


def test_reject_two_jumps_01():
    """0.1C sensor: 2 jumps is not enough for OLS (need 3)."""
    # 20.0 x15, 19.9 x15, 19.8 x10 -> 2 jumps
    samples = []
    for i in range(15):
        samples.append((i * 20.0, 20.0))
    for i in range(15):
        samples.append(((15 + i) * 20.0, 19.9))
    for i in range(10):
        samples.append(((30 + i) * 20.0, 19.8))

    slope, reason, _ = ABEstimator.robust_dTdt_per_min(samples)
    assert slope is None
    assert reason == "too_few_jumps"


def test_accept_three_jumps_01():
    """0.1C sensor: 3 jumps with clear trend should be accepted."""
    # 20.0 x10, 19.9 x10, 19.8 x10, 19.7 x10 -> 3 jumps, amp=0.3
    samples = []
    for i in range(10):
        samples.append((i * 60.0, 20.0))
    for i in range(10):
        samples.append(((10 + i) * 60.0, 19.9))
    for i in range(10):
        samples.append(((20 + i) * 60.0, 19.8))
    for i in range(10):
        samples.append(((30 + i) * 60.0, 19.7))

    slope, method, n = ABEstimator.robust_dTdt_per_min(samples)
    assert slope is not None
    assert method == "ols_ttest"
    assert slope < 0  # Cooling


def test_reject_insignificant_slope():
    """Alternating values produce no significant trend -> reject."""
    # Alternating 20.0 / 19.9 / 20.0 / 19.9 ... -> 3+ jumps but no trend
    samples = []
    for i in range(40):
        val = 20.0 if i % 2 == 0 else 19.9
        samples.append((i * 20.0, val))

    slope, reason, _ = ABEstimator.robust_dTdt_per_min(samples)
    assert slope is None
    assert reason == "slope_not_significant"


def test_accept_fine_sensor_001():
    """0.01C sensor: many jumps, small values -> accepted quickly."""
    # 20 points decreasing by 0.01 each minute -> 19 jumps, amp=0.19
    samples = []
    for i in range(20):
        samples.append((i * 60.0, 20.0 - i * 0.01))

    slope, method, n = ABEstimator.robust_dTdt_per_min(samples)
    assert slope is not None
    assert method == "ols_ttest"
    assert n == 20
    assert abs(slope - (-0.01)) < 0.005
