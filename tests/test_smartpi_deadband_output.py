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


@pytest.mark.parametrize(
    ("error_p", "deadband_allow_p", "expected_error", "expected_mode"),
    [
        (0.07, True, 0.0, "off"),
        (0.10, True, 0.025, "deadzone_edge"),
        (0.12, True, 0.045, "deadzone_edge"),
        (-0.07, True, 0.0, "off"),
        (-0.10, True, -0.025, "deadzone_edge"),
        (-0.12, True, -0.045, "deadzone_edge"),
        (0.10, False, 0.0, "off"),
        (0.12, False, 0.02, "deadzone_edge"),
        (-0.10, False, 0.0, "off"),
        (-0.12, False, -0.02, "deadzone_edge"),
    ],
)
def test_outside_deadband_subtracts_continuous_deadzone(
    error_p, deadband_allow_p, expected_error, expected_mode
):
    """The proportional path subtracts its active threshold outside the core."""
    error_p_db, mode = deadband_proportional_error(
        error_p=error_p,
        deadband_c=0.10,
        freeze_deadband=False,
        deadband_allow_p=deadband_allow_p,
    )

    assert error_p_db == pytest.approx(expected_error)
    assert mode == expected_mode


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_deadband_allow_p_is_continuous_at_core_boundary(sign):
    """Allowed P has the same limit immediately inside and outside the core."""
    deadband_c = 0.10
    epsilon = 1e-6

    inside, inside_mode = deadband_proportional_error(
        error_p=sign * (deadband_c - epsilon),
        deadband_c=deadband_c,
        freeze_deadband=True,
        deadband_allow_p=True,
    )
    boundary, boundary_mode = deadband_proportional_error(
        error_p=sign * deadband_c,
        deadband_c=deadband_c,
        freeze_deadband=False,
        deadband_allow_p=True,
    )
    outside, outside_mode = deadband_proportional_error(
        error_p=sign * (deadband_c + epsilon),
        deadband_c=deadband_c,
        freeze_deadband=False,
        deadband_allow_p=True,
    )

    assert inside == pytest.approx(sign * (0.025 - epsilon))
    assert boundary == pytest.approx(sign * 0.025)
    assert outside == pytest.approx(sign * (0.025 + epsilon))
    assert inside_mode == "deadband_edge"
    assert boundary_mode == "deadzone_edge"
    assert outside_mode == "deadzone_edge"


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_deadband_without_p_is_continuous_at_core_boundary(sign):
    """Disabled core P resumes continuously from zero outside the core."""
    deadband_c = 0.10
    epsilon = 1e-6

    inside, inside_mode = deadband_proportional_error(
        error_p=sign * (deadband_c - epsilon),
        deadband_c=deadband_c,
        freeze_deadband=True,
        deadband_allow_p=False,
    )
    boundary, boundary_mode = deadband_proportional_error(
        error_p=sign * deadband_c,
        deadband_c=deadband_c,
        freeze_deadband=False,
        deadband_allow_p=False,
    )
    outside, outside_mode = deadband_proportional_error(
        error_p=sign * (deadband_c + epsilon),
        deadband_c=deadband_c,
        freeze_deadband=False,
        deadband_allow_p=False,
    )

    assert inside == 0.0
    assert boundary == 0.0
    assert outside == pytest.approx(sign * epsilon)
    assert inside_mode == "deadband_frozen"
    assert boundary_mode == "off"
    assert outside_mode == "deadzone_edge"


@pytest.mark.parametrize("deadband_allow_p", [True, False])
@pytest.mark.parametrize("error_p", [0.12, -0.12])
def test_zero_deadband_preserves_raw_proportional_error(
    deadband_allow_p, error_p
):
    """A zero-width deadband leaves the proportional path unchanged."""
    error_p_db, mode = deadband_proportional_error(
        error_p=error_p,
        deadband_c=0.0,
        freeze_deadband=False,
        deadband_allow_p=deadband_allow_p,
    )

    assert error_p_db == pytest.approx(error_p)
    assert mode == "deadzone_edge"


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


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_controller_carries_edge_persistence_across_core_boundary(sign):
    """An outside edge sample prevents an artificial P drop on core entry."""
    controller = SmartPIController("test")
    deadband_c = 0.10
    epsilon = 1e-6

    common = {
        "kp": 1.0,
        "ki": 0.01,
        "u_ff": 0.40,
        "dt_min": 1.0,
        "cycle_min": 10.0,
        "in_near_band": True,
        "integrator_hold": True,
        "u_db_nominal": 0.40,
        "hvac_mode": VThermHvacMode_HEAT,
        "target_temp": 20.0,
        "is_tau_reliable": True,
        "learn_ok_count_a": 10,
        "deadband_c": deadband_c,
        "deadband_allow_p": True,
    }

    outside_error = sign * (deadband_c + epsilon)
    outside = controller.compute_pwm(
        error=outside_error,
        error_p=outside_error,
        in_deadband=False,
        core_deadband=False,
        current_temp=20.0 - outside_error,
        **common,
    )
    assert controller.deadband_p_mode == "deadzone_edge"

    inside_error = sign * (deadband_c - epsilon)
    inside = controller.compute_pwm(
        error=inside_error,
        error_p=inside_error,
        in_deadband=True,
        core_deadband=True,
        current_temp=20.0 - inside_error,
        **common,
    )

    assert controller.deadband_p_mode == "deadband_edge"
    assert controller.last_error_p_db == pytest.approx(sign * (0.025 - epsilon))
    assert inside == pytest.approx(outside - sign * 2.0 * epsilon)


def test_controller_resets_edge_persistence_on_sign_change():
    """An opposite-sign core edge must establish its own persistence."""
    controller = SmartPIController("test")
    common = {
        "kp": 1.0,
        "ki": 0.01,
        "u_ff": 0.40,
        "dt_min": 1.0,
        "cycle_min": 10.0,
        "in_near_band": True,
        "integrator_hold": True,
        "u_db_nominal": 0.40,
        "hvac_mode": VThermHvacMode_HEAT,
        "target_temp": 20.0,
        "is_tau_reliable": True,
        "learn_ok_count_a": 10,
        "deadband_c": 0.10,
        "deadband_allow_p": True,
    }

    controller.compute_pwm(
        error=0.11,
        error_p=0.11,
        in_deadband=False,
        core_deadband=False,
        current_temp=19.89,
        **common,
    )
    first_negative = controller.compute_pwm(
        error=-0.09,
        error_p=-0.09,
        in_deadband=True,
        core_deadband=True,
        current_temp=20.09,
        **common,
    )

    assert first_negative == pytest.approx(0.40)
    assert controller.last_error_p_db == 0.0
    assert controller.deadband_p_mode == "deadband_edge_pending"

    second_negative = controller.compute_pwm(
        error=-0.09,
        error_p=-0.09,
        in_deadband=True,
        core_deadband=True,
        current_temp=20.09,
        **common,
    )

    assert second_negative == pytest.approx(0.385)
    assert controller.last_error_p_db == pytest.approx(-0.015)
    assert controller.deadband_p_mode == "deadband_edge"
