"""Test the ABEstimator estimator for SmartPI.

Tests are designed for the Theil-Sen robust regression algorithm which requires
varying input values (delta for b, u for a) to compute slopes.
"""

from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.smartpi.ab_estimator import ABEstimator
from custom_components.versatile_thermostat.smartpi.const import (
    AB_MIN_SAMPLES_B,
    AB_B_CONVERGENCE_MIN_SAMPLES,
)
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT
from unittest.mock import MagicMock
from .commons import force_smartpi_stable_mode
from .commons import force_smartpi_stable_mode


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
    """Test that Kp/Ki adapt based on thermal time constant."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI"
    )
    force_smartpi_stable_mode(smartpi)

    # Set a low b (long time constant)
    smartpi.est.b = 0.001  # tau = 1000 min
    # We need to ensure we have enough samples for reliability
    smartpi.est.learn_ok_count = 20
    smartpi.est.learn_ok_count_b = 20
    # Mock reliability check to isolate Kp logic testing
    # Create a proper mock object with attributes set
    tau_info_slow = MagicMock()
    tau_info_slow.reliable = True
    tau_info_slow.tau_min = 1000.0
    smartpi.est.tau_reliability = MagicMock(return_value=tau_info_slow)
    # Note: We still populated history above (now irrelevant but harmless)

    smartpi.calculate(
        target_temp=20,
        current_temp=19,
        ext_current_temp=10,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )

    kp_slow = smartpi.Kp

    # Set a high b (short time constant)
    smartpi.est.b = 0.01  # tau = 100 min
    # Create a proper mock object with attributes set
    tau_info_fast = MagicMock()
    tau_info_fast.reliable = True
    tau_info_fast.tau_min = 100.0
    smartpi.est.tau_reliability = MagicMock(return_value=tau_info_fast)
    smartpi.est._b_hat_hist.clear()
    for _ in range(10):
        smartpi.est._b_hat_hist.append(0.01)

    # Reset the rate-limiting timestamp to allow immediate recalculation
    smartpi._last_calculate_time = None
    # Clear cycle regimes to simulate a clean cycle (avoid REGIME_TRANSITION freeze)
    smartpi._cycle_regimes.clear()
    # Prevent false resume detection from _last_calculate_time reset
    smartpi._startup_grace_period = True
    # Reset output_initialized to avoid SATURATED regime detection
    # (which would freeze gains and prevent Kp adaptation)
    smartpi._output_initialized = False
    smartpi._on_percent = 0.5  # Set to non-saturated value

    smartpi.calculate(
        target_temp=20,
        current_temp=19,
        ext_current_temp=10,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT
    )

    kp_fast = smartpi.Kp

    # Fast system should have lower Kp (it responds quickly)
    # Based on formula: Kp = 0.35 + 0.9 * sqrt(tau/200)
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
