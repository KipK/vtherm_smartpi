"""Tests for SmartPI deadband output shaping."""

import pytest

from custom_components.vtherm_smartpi.smartpi.deadband_output import (
    deadband_proportional_error,
)


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
