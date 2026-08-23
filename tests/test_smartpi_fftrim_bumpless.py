"""Tests for causal FF-trim and integral ownership transfer."""

import pytest
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import (
    FF_TRIM_LAMBDA,
    GovernanceRegime,
)
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.feedforward import FFResult
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    FFTrim,
    FFTrimPersistentResult,
    FFTrimWindowProposal,
    prepare_bumpless_transfer,
)


@pytest.mark.parametrize("integral_bias", (0.12, -0.08))
def test_stable_integral_bias_changes_owner_without_command_step(
    integral_bias: float,
) -> None:
    """Positive and negative integral bias must transfer symmetrically."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=0.5,
        current_i_power=integral_bias,
        observed_i_bias=integral_bias,
        physical_power_deficit=0.0,
        current_ki=0.02,
        current_raw_command=0.5 + integral_bias,
    )

    assert plan.applicable is True
    assert plan.visible_ff_delta == pytest.approx(
        FF_TRIM_LAMBDA * integral_bias
    )
    assert plan.applied_i_transfer == pytest.approx(plan.visible_ff_delta)
    assert plan.net_command_delta == pytest.approx(0.0)


def test_opposing_persisted_trim_and_integral_compact_without_command_step() -> None:
    """Opposing stored trim and integral bias must both move toward zero."""
    current_trim = -0.014593
    current_i_power = 0.007642571
    plan = prepare_bumpless_transfer(
        current_trim=current_trim,
        current_ff1=0.096757,
        current_i_power=current_i_power,
        observed_i_bias=current_i_power,
        physical_power_deficit=0.0,
        current_ki=0.001,
        current_raw_command=0.089806571,
    )

    assert plan.applicable is True
    assert abs(plan.new_trim) < abs(current_trim)
    assert plan.applied_i_transfer > 0.0
    assert abs(current_i_power - plan.applied_i_transfer) < abs(current_i_power)
    assert plan.visible_ff_delta == pytest.approx(plan.applied_i_transfer)
    assert plan.net_command_delta == pytest.approx(0.0)


def test_small_same_direction_trim_and_integral_remain_quiet() -> None:
    """Sub-precision ownership transfer must stay quiet without cancellation."""
    current_trim = 0.014593
    current_i_power = 0.007642571
    plan = prepare_bumpless_transfer(
        current_trim=current_trim,
        current_ff1=0.096757,
        current_i_power=current_i_power,
        observed_i_bias=current_i_power,
        physical_power_deficit=0.0,
        current_ki=0.001,
        current_raw_command=0.118992571,
    )

    assert plan.applicable is False
    assert plan.reason == "quiet_trim_delta"


def test_physical_deficit_remains_a_real_command_change() -> None:
    """Only the I-owned share is cancelled by the integral mutation."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=0.4,
        current_i_power=0.10,
        observed_i_bias=0.10,
        physical_power_deficit=0.06,
        current_ki=0.02,
        current_raw_command=0.50,
    )

    assert plan.applicable is True
    assert plan.visible_ff_delta == pytest.approx(0.008)
    assert plan.applied_i_transfer == pytest.approx(0.005)
    assert plan.net_command_delta == pytest.approx(0.003)


def test_integral_bias_and_surplus_can_cancel_in_trim_but_not_command() -> None:
    """A zero trim delta must not hide a real causal command correction."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=0.5,
        current_i_power=0.1,
        observed_i_bias=0.1,
        physical_power_deficit=-0.1,
        current_ki=0.05,
        current_raw_command=0.6,
    )

    assert plan.applicable is True
    assert plan.visible_ff_delta == pytest.approx(0.0)
    assert plan.applied_i_transfer == pytest.approx(FF_TRIM_LAMBDA * 0.1)
    assert plan.net_command_delta == pytest.approx(-FF_TRIM_LAMBDA * 0.1)


def test_zero_integral_deficit_still_changes_power() -> None:
    """A physical deficit must not be neutralized when I owns no power."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=0.4,
        current_i_power=0.0,
        observed_i_bias=0.0,
        physical_power_deficit=0.10,
        current_ki=0.02,
        current_raw_command=0.40,
    )

    assert plan.applicable is True
    assert plan.applied_i_transfer == pytest.approx(0.0)
    assert plan.net_command_delta == pytest.approx(0.005)


@pytest.mark.parametrize("ki", (0.0, 1e-6, float("nan")))
def test_invalid_ki_is_rejected_before_integral_division(ki: float) -> None:
    """The transaction must never divide by an invalid effective Ki."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=0.4,
        current_i_power=0.1,
        observed_i_bias=0.1,
        physical_power_deficit=0.0,
        current_ki=ki,
        current_raw_command=0.5,
    )

    assert plan.applicable is False


def test_ff_clamp_rejects_invisible_trim_storage() -> None:
    """An already clamped FF branch must not accumulate latent trim."""
    plan = prepare_bumpless_transfer(
        current_trim=0.0,
        current_ff1=1.0,
        current_i_power=0.1,
        observed_i_bias=0.1,
        physical_power_deficit=0.0,
        current_ki=0.02,
        current_raw_command=1.0,
    )

    assert plan.applicable is False
    assert plan.reason == "ff_branch_clamped"


def test_authority_reduces_transfer_and_deficit_by_one_common_factor() -> None:
    """Authority headroom must preserve the decomposed invariant under clamp."""
    plan = prepare_bumpless_transfer(
        current_trim=0.029,
        current_ff1=0.2,
        current_i_power=0.1,
        observed_i_bias=0.1,
        physical_power_deficit=0.1,
        current_ki=0.02,
        current_raw_command=0.329,
    )

    assert plan.applicable is True
    assert 0.0 < plan.alpha < 1.0
    assert plan.net_command_delta == pytest.approx(
        plan.alpha * FF_TRIM_LAMBDA * 0.1
    )


def test_controller_integral_transfer_moves_power_toward_zero() -> None:
    """The controller applies the signed power transfer exactly once."""
    controller = SmartPIController("bumpless")
    controller.integral = 5.0
    controller.u_p = 0.0
    controller.u_i = 0.1
    controller.u_pi = 0.1
    controller.last_i_mode = "I:FREEZE(deadband)"

    result = controller.apply_integral_power_transfer(
        ki=0.02,
        requested_power=0.005,
    )

    assert result.applied is True
    assert result.old_i_power == pytest.approx(0.1)
    assert result.new_i_power == pytest.approx(0.095)
    assert controller.integral == pytest.approx(4.75)
    assert controller.u_pi == pytest.approx(0.095)


def test_one_causal_buffer_aggregates_deficit_and_integral_together() -> None:
    """The persistence gate must return paired D/J medians from the same windows."""
    trim = FFTrim()
    proposals = (
        FFTrimWindowProposal(0.13, 0.4, 0.03, 0.10, True, "quasi_equilibrium"),
        FFTrimWindowProposal(0.15, 0.4, 0.05, 0.10, True, "quasi_equilibrium"),
        FFTrimWindowProposal(0.14, 0.4, 0.04, 0.10, True, "quasi_equilibrium"),
    )

    result = None
    for proposal in proposals:
        result = trim.collect_persistent(proposal)

    assert result is not None
    assert result.ready is True
    assert result.transfer_eligible is True
    assert result.median_physical_power_deficit == pytest.approx(0.04)
    assert result.median_integral_bias == pytest.approx(0.10)
    assert result.median_correction == pytest.approx(0.14)


def test_causal_buffer_keeps_opposing_deficit_and_integral_pair() -> None:
    """A zero J+D median still carries a persistent causal command signal."""
    trim = FFTrim()
    proposal = FFTrimWindowProposal(
        0.0,
        0.5,
        -0.1,
        0.1,
        True,
        "quasi_equilibrium",
    )

    trim.collect_persistent(proposal)
    trim.collect_persistent(proposal)
    result = trim.collect_persistent(proposal)

    assert result.ready is True
    assert result.transfer_eligible is True
    assert result.median_correction == pytest.approx(0.0)
    assert result.median_physical_power_deficit == pytest.approx(-0.1)
    assert result.median_integral_bias == pytest.approx(0.1)


def test_smartpi_applies_trim_and_integral_as_one_post_aw_transaction() -> None:
    """The orchestrator must preserve I ownership and retain the physical delta."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="bumpless-integration",
    )
    algo.Kp = 0.5
    algo.Ki = 0.02
    algo.gov.regime = GovernanceRegime.DEAD_BAND
    algo.ctl.integral = 5.0
    algo.ctl.u_ff = 0.4
    algo.ctl.u_p = 0.0
    algo.ctl.u_i = 0.1
    algo.ctl.u_pi = 0.1
    algo.ctl.u_cmd = 0.5
    algo.ctl.last_i_mode = "I:FREEZE(deadband)"
    algo.ctl.last_sat = "NO_SAT"
    algo._last_u_cmd = 0.5
    algo._last_u_limited = 0.5
    algo._last_u_applied = 0.5
    algo._last_ff_result = FFResult(
        ff_raw=0.4,
        u_ff1=0.4,
        u_ff2=0.0,
        u_ff_final=0.4,
        u_ff3=0.0,
        u_db_nominal=0.4,
        u_ff_ab=0.4,
        u_ff_trim=0.0,
        u_ff_base=0.4,
        u_ff_eff=0.4,
        ff_reason="ff_none",
    )
    persistent = FFTrimPersistentResult(
        ready=True,
        reason="persistent_ready",
        pending_count=3,
        median_correction=0.16,
        median_ff1=0.4,
        median_physical_power_deficit=0.06,
        median_integral_bias=0.10,
        transfer_eligible=True,
        transfer_reason="quasi_equilibrium",
    )

    updated = algo._apply_bumpless_persistent_result(
        persistent=persistent,
        quality="switch_cycle_equivalent",
    )

    assert updated is True
    assert algo._ff_trim.u_ff_trim == pytest.approx(0.008)
    assert algo.Ki * algo.integral == pytest.approx(0.095)
    assert algo.ctl.u_cmd == pytest.approx(0.503)
    assert algo._applied_trim_delta == pytest.approx(0.008)
    assert algo._applied_i_transfer == pytest.approx(0.005)
    assert algo._net_command_delta == pytest.approx(0.003)
    assert algo._bumpless_transfer_state == "applied"
    assert algo._transfer_pending_engagement is True
