"""Tests for SmartPI deadband output shaping."""

import pytest

from custom_components.vtherm_smartpi.smartpi.deadband_output import (
    deadband_proportional_error,
)
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


def test_deadband_blocks_p_when_allow_p_disabled():
    """Frozen deadband with P disabled must zero the proportional path."""
    error_p_db, mode = deadband_proportional_error(
        error_p=0.07,
        deadband_c=0.10,
        freeze_deadband=True,
        deadband_allow_p=False,
    )

    assert error_p_db == 0.0
    assert mode == "deadband_frozen"


def test_deadband_allow_p_quiet_zone_blocks_small_sensor_steps():
    """Allowed P is quiet inside the inner deadband zone."""
    error_p_db, mode = deadband_proportional_error(
        error_p=0.07,
        deadband_c=0.10,
        freeze_deadband=True,
        deadband_allow_p=True,
    )

    assert error_p_db == 0.0
    assert mode == "deadband_quiet"


def test_deadband_allow_p_resumes_smoothly_at_positive_edge():
    """Allowed P subtracts the quiet zone near the positive deadband edge."""
    error_p_db, mode = deadband_proportional_error(
        error_p=0.10,
        deadband_c=0.10,
        freeze_deadband=True,
        deadband_allow_p=True,
    )

    assert error_p_db == pytest.approx(0.025)
    assert mode == "deadband_edge"


def test_deadband_allow_p_resumes_smoothly_at_negative_edge():
    """Allowed P subtracts the quiet zone symmetrically."""
    error_p_db, mode = deadband_proportional_error(
        error_p=-0.10,
        deadband_c=0.10,
        freeze_deadband=True,
        deadband_allow_p=True,
    )

    assert error_p_db == pytest.approx(-0.025)
    assert mode == "deadband_edge"


def test_outside_deadband_preserves_raw_proportional_behavior():
    """Normal PI behavior outside the frozen deadband is unchanged."""
    error_p_db, mode = deadband_proportional_error(
        error_p=0.07,
        deadband_c=0.10,
        freeze_deadband=False,
        deadband_allow_p=True,
    )

    assert error_p_db == 0.07
    assert mode == "raw"


def test_controller_requires_persistent_deadband_edge_before_p_term():
    """The controller waits before applying damped P at the deadband edge."""
    controller = SmartPIController("test")

    kwargs = {
        "error": -0.10,
        "error_p": -0.10,
        "kp": 1.0,
        "ki": 0.01,
        "u_ff": 0.40,
        "dt_min": 1.0,
        "cycle_min": 10.0,
        "in_deadband": True,
        "in_near_band": True,
        "integrator_hold": False,
        "u_db_nominal": 0.40,
        "hvac_mode": VThermHvacMode_HEAT,
        "current_temp": 20.10,
        "target_temp": 20.0,
        "hysteresis_thermal_guard": False,
        "is_tau_reliable": True,
        "learn_ok_count_a": 10,
        "deadband_c": 0.10,
        "core_deadband": True,
        "deadband_allow_p": True,
    }

    first = controller.compute_pwm(**kwargs)
    first_mode = controller.deadband_p_mode
    second = controller.compute_pwm(**kwargs)

    assert first == pytest.approx(0.40)
    assert first_mode == "deadband_edge_pending"
    assert controller.deadband_p_mode == "deadband_edge"
    assert second == pytest.approx(0.375)
