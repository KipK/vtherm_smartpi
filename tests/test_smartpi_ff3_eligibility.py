"""Tests for FF3 disturbance-only eligibility."""

import pytest

from custom_components.vtherm_smartpi.smartpi.const import (
    FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR,
)
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


def test_ff3_prediction_quality_degraded_keeps_reduced_authority():
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": False,
            "warming_up": False,
            "T_steady_valid": True,
            "bias_warning": False,
            "external_gain_detected": False,
            "external_loss_detected": True,
            "perturbation_dTdt": -0.015,
        },
        measured_slope_h=-0.1,
        trajectory_active=False,
        trajectory_source="none",
    )

    assert context.disturbance_active is True
    assert context.prediction_usable is True
    assert context.prediction_quality == "degraded"
    assert context.authority_factor == pytest.approx(FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR)


def test_ff3_warming_up_blocks_even_with_disturbance():
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": False,
            "warming_up": True,
            "T_steady_valid": True,
            "bias_warning": False,
            "external_gain_detected": False,
            "external_loss_detected": True,
            "perturbation_dTdt": -0.015,
        },
        measured_slope_h=-0.1,
        trajectory_active=False,
        trajectory_source="none",
    )

    assert context.disturbance_active is False
    assert context.prediction_usable is False
    assert context.reason == "twin_warming_up"
    assert context.authority_factor == 0.0


def test_ff3_invalid_steady_state_blocks_prediction():
    context = build_ff3_disturbance_context(
        twin_diag={
            "status": "ok",
            "model_reliable": False,
            "warming_up": False,
            "T_steady_valid": False,
            "bias_warning": False,
            "external_gain_detected": False,
            "external_loss_detected": True,
            "perturbation_dTdt": -0.015,
        },
        measured_slope_h=-0.1,
        trajectory_active=False,
        trajectory_source="none",
    )

    assert context.reason == "twin_steady_invalid"
    assert context.prediction_usable is False
