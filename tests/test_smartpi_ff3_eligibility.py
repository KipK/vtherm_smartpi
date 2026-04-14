"""Tests for FF3 disturbance-only eligibility."""

from custom_components.vtherm_smartpi.smartpi.ff3_eligibility import (
    build_ff3_disturbance_context,
)


def test_ff3_context_rejects_setpoint_trajectory():
    """A setpoint trajectory must block FF3 even if twin residuals are strong."""
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": True,
            "bias_warning": True,
            "external_gain_detected": True,
            "external_loss_detected": False,
            "perturbation_dTdt": 0.01,
            "warming_up": False,
        },
        measured_slope_h=0.2,
        trajectory_active=True,
        trajectory_source="setpoint",
    )

    assert context.disturbance_active is False
    assert context.reason == "trajectory_setpoint_active"
    assert context.disturbance_kind == "gain"
    assert context.residual_persistent is True
    assert context.dynamic_coherent is True


def test_ff3_context_requires_dynamic_coherence():
    """Residual persistence alone must not activate FF3 without matching dynamics."""
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": True,
            "bias_warning": True,
            "external_gain_detected": False,
            "external_loss_detected": False,
            "perturbation_dTdt": None,
            "warming_up": False,
        },
        measured_slope_h=None,
        trajectory_active=False,
        trajectory_source="none",
    )

    assert context.disturbance_active is False
    assert context.reason == "disturbance_unclassified"
    assert context.disturbance_kind == "none"
    assert context.residual_persistent is True
    assert context.dynamic_coherent is False


def test_ff3_context_accepts_external_loss_with_coherent_cooling():
    """A persistent external loss with coherent negative dynamics may enable FF3."""
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": True,
            "bias_warning": False,
            "external_gain_detected": False,
            "external_loss_detected": True,
            "perturbation_dTdt": -0.015,
            "warming_up": False,
        },
        measured_slope_h=-0.1,
        trajectory_active=False,
        trajectory_source="disturbance",
    )

    assert context.disturbance_active is True
    assert context.reason == "none"
    assert context.disturbance_kind == "loss"
    assert context.residual_persistent is True
    assert context.dynamic_coherent is True
