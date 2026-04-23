"""Tests for persistent drift handling in Smart-PI ABEstimator."""

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.ab_drift import (
    DriftChannelState,
    append_drift_candidate,
    compute_recenter_virtual_center,
    detect_persistent_drift,
)
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator


def test_detect_persistent_drift_needs_enough_candidates():
    """Drift detection should stay false on a short buffer."""

    channel = DriftChannelState()
    for idx in range(5):
        append_drift_candidate(
            channel=channel,
            value=0.004,
            seq=idx + 1,
            hist_center=0.002,
        )

    ok, diag = detect_persistent_drift(
        channel=channel,
        hist_center=0.002,
        hist_mad_eff=0.0001,
        min_count=6,
        max_buffer_mad_factor=1.5,
        min_shift_factor=3.0,
        seq_gap_max=3,
    )

    assert not ok
    assert diag["reason"] == "DRIFT_NOT_ENOUGH_CANDIDATES"


def test_detect_persistent_drift_accepts_coherent_shift():
    """Drift detection should accept a coherent low-dispersion shifted cluster."""

    channel = DriftChannelState()
    for idx, value in enumerate([0.0040, 0.0041, 0.0039, 0.0040, 0.0041, 0.0040], start=1):
        append_drift_candidate(
            channel=channel,
            value=value,
            seq=idx,
            hist_center=0.002,
        )

    ok, diag = detect_persistent_drift(
        channel=channel,
        hist_center=0.002,
        hist_mad_eff=0.0001,
        min_count=6,
        max_buffer_mad_factor=1.5,
        min_shift_factor=3.0,
        seq_gap_max=3,
    )

    assert ok
    assert diag["reason"] == "PERSISTENT_DRIFT_DETECTED"
    assert diag["side"] == 1
    assert diag["shift"] > 0.001


def test_compute_recenter_virtual_center_is_bounded():
    """Recentering step should be clipped by the configured maximum step."""

    new_center = compute_recenter_virtual_center(
        current_center=0.0020,
        candidate_center=0.0040,
        hist_mad_eff=0.0001,
        alpha=0.25,
        step_max_factor=2.0,
    )

    assert new_center == 0.0022


def test_abestimator_b_persistent_drift_accepts_real_measure():
    """A stable new b regime should eventually enter the real accepted history."""

    est = ABEstimator()
    est.b = 0.002
    est.b_meas_hist.extend([0.002] * 31)
    est.learn_ok_count_b = 31
    est._b_hat_hist.extend([0.002] * 10)

    for _ in range(15):
        est.learn(
            dT_int_per_min=-0.04,
            u=0.0,
            t_int=20.0,
            t_ext=10.0,
        )

    assert max(est.b_meas_hist) >= 0.004
    assert est.learn_ok_count_b >= 32
    assert est.diag_b_last_reason in {
        "ACCEPTED_BY_RECENTERING",
        "RECENTERING_FINISHED",
        "NORMAL_ACCEPT",
    }


def test_abestimator_a_drift_is_disabled_until_b_converges():
    """A drift should remain disabled while b is not converged for a."""

    est = ABEstimator()
    est.b = 0.001
    est.learn_ok_count_b = 8
    est.b_meas_hist.extend([0.001] * 8)
    est.a_meas_hist.extend([0.020] * 31)

    for _ in range(8):
        est.learn(
            dT_int_per_min=0.03,
            u=1.0,
            t_int=20.0,
            t_ext=10.0,
        )

    assert est.a_drift.state == "NORMAL"
    assert len(est.a_drift.drift_buffer) == 0
    assert est.diag_a_last_reason == "REJECT_NOT_ELIGIBLE_FOR_DRIFT"


def test_abestimator_save_load_restores_drift_state():
    """Drift state should round-trip through estimator persistence."""

    est1 = ABEstimator()
    est1.b = 0.002
    est1.b_meas_hist.extend([0.002] * 31)
    est1.learn_ok_count_b = 31
    est1._b_hat_hist.extend([0.002] * 10)

    for _ in range(8):
        est1.learn(
            dT_int_per_min=-0.04,
            u=0.0,
            t_int=20.0,
            t_ext=10.0,
        )

    saved = est1.save_state()

    est2 = ABEstimator()
    est2.load_state(saved)

    assert est2._learn_seq == est1._learn_seq
    assert est2.b_drift.state == est1.b_drift.state
    assert est2.b_drift.recenter_cycles_left == est1.b_drift.recenter_cycles_left
    assert len(est2.b_drift.drift_buffer) == len(est1.b_drift.drift_buffer)


def test_smartpi_diagnostics_expose_ab_drift_fields():
    """Diagnostics should expose drift fields for both parameters."""

    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPIDriftDiag",
    )

    diag = smartpi.get_diagnostics()

    assert "a_drift_state" in diag
    assert "b_drift_state" in diag
    assert "a_drift_buffer_count" in diag
    assert "b_drift_buffer_count" in diag
