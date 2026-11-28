"""Tests for persistent drift handling in Smart-PI ABEstimator."""

from unittest.mock import MagicMock

import pytest

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


@pytest.mark.parametrize("a_between_b", [3, 4, 5])
def test_abestimator_b_drift_recenters_with_interleaved_a_measures(a_between_b):
    """b drift detection should not be blocked by a measurements between b measurements."""

    est = ABEstimator()
    est.b = 0.002
    est.b_meas_hist.extend([0.002] * 31)
    est.learn_ok_count_b = 31
    est._b_hat_hist.extend([0.002] * 10)
    est.a_meas_hist.extend([0.05] * 31)
    est.learn_ok_count_a = 31

    for _ in range(40):
        for _ in range(a_between_b):
            est.learn(
                dT_int_per_min=0.03,
                u=1.0,
                t_int=20.0,
                t_ext=10.0,
            )
        est.learn(
            dT_int_per_min=-0.04,
            u=0.0,
            t_int=20.0,
            t_ext=10.0,
        )

    assert max(est.b_meas_hist) >= 0.004
    assert est.b >= 0.003


def test_abestimator_a_drift_recenters_with_interleaved_b_measures():
    """a drift detection should not be blocked by b measurements between a measurements."""

    est = ABEstimator()
    est.b = 0.002
    est.b_meas_hist.extend([0.002] * 31)
    est.learn_ok_count_b = 31
    est._b_hat_hist.extend([0.002] * 10)
    est.a = 0.05
    est.a_meas_hist.extend([0.05] * 31)
    est.learn_ok_count_a = 31
    assert est.b_converged_for_a()

    for _ in range(40):
        for _ in range(4):
            est.learn(
                dT_int_per_min=-0.02,
                u=0.0,
                t_int=20.0,
                t_ext=10.0,
            )
        est.learn(
            dT_int_per_min=0.032,
            u=1.0,
            t_int=20.0,
            t_ext=10.0,
        )

    assert max(est.a_meas_hist) >= 0.0519
    assert est.a >= 0.051


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

    assert est2._learn_seq_a == est1._learn_seq_a
    assert est2._learn_seq_b == est1._learn_seq_b
    assert est2.b_drift.state == est1.b_drift.state
    assert est2.b_drift.recenter_cycles_left == est1.b_drift.recenter_cycles_left
    assert len(est2.b_drift.drift_buffer) == len(est1.b_drift.drift_buffer)


def test_abestimator_load_legacy_learn_seq_seeds_both_channels():
    """A legacy shared learn_seq should seed both per-channel counters."""

    est = ABEstimator()
    est.load_state({"a": 0.05, "b": 0.002, "learn_seq": 42})

    assert est._learn_seq_a == 42
    assert est._learn_seq_b == 42


def test_smartpi_diagnostics_expose_ab_drift_fields():
    """Diagnostics should expose drift fields for both parameters."""

    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPIDriftDiag",
    )

    learning = smartpi.get_published_diagnostics()["learning"]

    assert "a_drift_state" in learning
    assert "b_drift_state" in learning
    assert len(smartpi.est.a_drift.drift_buffer) == 0
    assert len(smartpi.est.b_drift.drift_buffer) == 0


def _gate(est, name, value, hist):
    """Run the outlier gate of one measure against the given accepted history."""
    channel = est.a_drift if name == "a" else est.b_drift
    return est._process_param_measure(
        name=name,
        value=value,
        history=hist,
        drift_channel=channel,
        min_collect_count=3,
        allow_drift=False,
        seq=1,
    )


def test_a_gate_keeps_a_relative_width_when_first_measures_agree():
    """Nearly equal first a measures must not shrink the gate to a sliver of the median."""

    est = ABEstimator()
    hist = [0.0785, 0.0606, 0.0604, 0.0605, 0.0606]
    # a 5 % lower measure (0.0575) is inside the 2 % floor: 3 * 1.4826 * 0.02 * 0.0605 = 0.0054
    assert _gate(est, "a", 0.0575, hist).accepted
    assert _gate(est, "a", 0.0650, hist).accepted
    # a gross outlier stays rejected
    assert not _gate(est, "a", 0.0800, hist).accepted
    assert not _gate(est, "a", 0.0450, hist).accepted


def test_a_gate_still_uses_the_mad_when_it_is_wider_than_the_floor():
    """A dispersed history keeps its own, wider gate."""

    est = ABEstimator()
    hist = [0.040, 0.050, 0.060, 0.070, 0.080]  # MAD 0.01, half-width 0.0445
    assert _gate(est, "a", 0.100, hist).accepted
    assert not _gate(est, "a", 0.150, hist).accepted


def test_b_gate_is_unchanged_at_typical_values():
    """At b of the order of 1e-3 the relative floor stays under the absolute one."""

    est = ABEstimator()
    hist = [0.0020] * 7
    # absolute floor 1e-4: half-width 3 * 1.4826 * 1e-4 = 4.45e-4
    assert _gate(est, "b", 0.00243, hist).accepted
    assert not _gate(est, "b", 0.00246, hist).accepted
    assert not _gate(est, "b", 0.00154, hist).accepted
    assert _gate(est, "b", 0.00157, hist).accepted
