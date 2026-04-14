"""Test the ABEstimator estimator for SmartPI.

Tests are designed for the Theil-Sen robust regression algorithm which requires
varying input values (delta for b, u for a) to compute slopes.
"""

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_MIN_SAMPLES_B,
    AB_B_CONVERGENCE_MIN_SAMPLES,
    GovernanceDecision,
)
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from unittest.mock import MagicMock
from helpers import force_smartpi_stable_mode


def test_median_convergence_b():
    """Test that ABEstimator converges to correct b (cooling phase).

    Median+MAD strategy uses median of b measurements.
    Model: dT = -b * delta, so b = -dT / delta
    """
    est = ABEstimator()

    # True b = 0.002
    # Initialize est.b closer to target to avoid >50% variation rejection (0.001 -> 0.002 is 100%)
    est.b = 0.0015
    
    # Generate varying delta values with corresponding dT
    # dT = -b * delta = -0.002 * delta
    true_b = 0.002

    for i in range(30):
        # Vary delta between 5 and 15 (different outdoor temps)
        delta = 5.0 + (i % 11)  # cycles 5, 6, 7, ..., 15, 5, 6, ...
        dT = -true_b * delta  # e.g., -0.01 to -0.03
        t_ext = 20.0 - delta  # e.g., 15 to 5
        est.learn(dT_int_per_min=dT, u=0.0, t_int=20.0, t_ext=t_ext)

    # Should converge close to true_b
    assert 0.0015 < est.b < 0.0030, f"b should be ~0.002, got {est.b}"
    assert est.learn_ok_count_b > 0, f"Should have learned b, reason: {est.learn_last_reason}"


def test_median_convergence_a():
    """Test that ABEstimator converges to correct a (heating phase).

    Median+MAD strategy uses median of a measurements.
    Model: dT = a*u - b*delta, rearranged: a = (dT + b*delta) / u
    """
    est = ABEstimator()

    # First learn b so it's stable
    true_b = 0.002
    for i in range(AB_MIN_SAMPLES_B + AB_B_CONVERGENCE_MIN_SAMPLES + 2):
        delta = 8.0 + (i % 5)  # 8 to 12
        dT = -true_b * delta
        t_ext = 20.0 - delta
        est.learn(dT_int_per_min=dT, u=0.0, t_int=20.0, t_ext=t_ext)

    # Now learn a with varying u values
    true_a = 0.015  # heating effectiveness
    # Initialize est.a closer to target to avoid >50% variation rejection
    est.a = 0.01
    
    delta = 10.0  # fixed delta for ON phase
    t_ext = 10.0

    for i in range(20):
        # Vary u between 0.3 and 1.0
        u = 0.3 + 0.1 * (i % 8)  # 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0
        # dT = a*u - b*delta
        dT = true_a * u - est.b * delta
        est.learn(dT_int_per_min=dT, u=u, t_int=20.0, t_ext=t_ext)

    # Should converge close to true_a
    assert 0.010 < est.a < 0.025, f"a should be ~0.015, got {est.a}"
    assert est.learn_ok_count_a > 0, f"Should have learned a, reason: {est.learn_last_reason}"


def test_smartpi_gain_adaptation():
    """Test that heuristic gains adapt monotonically with the time constant."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI"
    )
    force_smartpi_stable_mode(smartpi)

    estimator = MagicMock()
    estimator.a = 0.01
    dt_est = MagicMock()
    dt_est.deadtime_heat_reliable = False
    dt_est.deadtime_heat_s = 0.0

    slow_result = smartpi.gain_scheduler.calculate(
        tau_reliable=True,
        tau_min=1000.0,
        estimator=estimator,
        dt_est=dt_est,
        in_near_band=False,
        kp_near_factor=smartpi.kp_near_factor,
        ki_near_factor=smartpi.ki_near_factor,
        governance_decision=GovernanceDecision.ADAPT_ON,
    )

    fast_result = smartpi.gain_scheduler.calculate(
        tau_reliable=True,
        tau_min=100.0,
        estimator=estimator,
        dt_est=dt_est,
        in_near_band=False,
        kp_near_factor=smartpi.kp_near_factor,
        ki_near_factor=smartpi.ki_near_factor,
        governance_decision=GovernanceDecision.ADAPT_ON,
    )
    kp_slow = slow_result.kp
    kp_fast = fast_result.kp

    # The current scheduler increases the heuristic Kp with tau:
    # Kp = 0.35 + 0.9 * sqrt(tau/200).
    # A slower system (larger tau) must therefore receive the higher Kp.
    assert kp_fast < kp_slow, f"Kp_fast ({kp_fast}) should be < Kp_slow ({kp_slow})"


def test_smartpi_outlier_rejection():
    """Test that outlier rejection rejects solar gain outliers.

    The ABEstimator uses Median+MAD based gating to detect and reject outlier
    measurements that deviate significantly from the learned model. This protects
    against transient disturbances like solar gain corrupting the model.
    """
    est = ABEstimator()

    # 1. Bootstrap phase with varying delta values
    # Train 'b' to a stable value of ~0.002
    est.b = 0.0015 # Initialize closer to target
    true_b = 0.002
    for i in range(25):
        delta = 8.0 + (i % 5)  # 8, 9, 10, 11, 12, ...
        dT = -true_b * delta
        t_ext = 20.0 - delta
        est.learn(dT_int_per_min=dT, u=0.0, t_int=20.0, t_ext=t_ext)

    assert est.learn_ok_count >= 10, (
        f"Should have enough samples post-bootstrap, got {est.learn_ok_count}, "
        f"reason: {est.learn_last_reason}"
    )
    stable_b = est.b
    assert 0.0015 < stable_b < 0.0030, f"b should be ~0.002, got {stable_b}"

    # 2. Solar Event - sudden positive residual (outlier)
    # Normal prediction: dT = -b * delta = -0.002 * 10 = -0.02 (cooling)
    # With sun hitting: observed dT becomes +0.02 (heating instead of cooling!)
    # Residual r = observed - predicted = +0.02 - (-0.02) = +0.04
    # This is a huge deviation and should be REJECTED.
    # It leads to b_meas < 0 which is rejected by physics check, 
    # or if slightly positive but far from median, rejected by MAD.

    prev_skip_count = est.learn_skip_count
    b_before_solar = est.b

    # Solar gain: temperature rises instead of falling
    est.learn(dT_int_per_min=+0.03, u=0.0, t_int=20.0, t_ext=10.0)

    assert est.learn_skip_count == prev_skip_count + 1, (
        f"Solar outlier should be skipped, reason: {est.learn_last_reason}"
    )
    # Reason can be "skip: b_meas <= 0" or "skip: b_meas outlier"
    assert "skip" in est.learn_last_reason.lower() and ("b_meas" in est.learn_last_reason or "slope" in est.learn_last_reason), (
        f"Should be rejected, got: {est.learn_last_reason}"
    )
    assert est.b == b_before_solar, "b should not change on solar outlier"

    # 3. Normal sample after solar event - should be accepted
    # Back to normal cooling pattern with varying delta
    prev_b = est.b
    for i in range(5):
        delta = 9.0 + i  # 9, 10, 11, 12, 13
        dT = -true_b * delta
        t_ext = 20.0 - delta
        est.learn(dT_int_per_min=dT, u=0.0, t_int=20.0, t_ext=t_ext)

    # Model should still be close to true_b (not corrupted by solar outlier)
    assert 0.0015 < est.b < 0.0030, f"b should remain stable after solar event, got {est.b}"
