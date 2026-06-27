"""Tests for the COUPLED governance regime (open connected door)."""

from custom_components.vtherm_smartpi.smartpi.governance import SmartPIGovernance
from custom_components.vtherm_smartpi.smartpi.const import (
    GovernanceRegime,
    GovernanceDecision,
    FreezeReason,
    SmartPIPhase,
)


def _regime(gov, *, any_door_open, power_shedding=False, ext_temp=5.0, phase=SmartPIPhase.STABLE):
    return gov.determine_regime(
        phase,
        ext_temp,
        False,             # integrator_hold
        power_shedding,
        True,              # output_initialized
        0.5,               # u_cmd
        False,             # in_deadband
        False,             # in_near_band
        any_door_open=any_door_open,
    )


def test_open_door_yields_coupled_regime():
    gov = SmartPIGovernance("t")
    assert _regime(gov, any_door_open=True) == GovernanceRegime.COUPLED


def test_closed_door_unchanged():
    gov = SmartPIGovernance("t")
    assert _regime(gov, any_door_open=False) == GovernanceRegime.EXCITED_STABLE


def test_coupled_freezes_thermal_and_gains():
    gov = SmartPIGovernance("t")
    gov.update_regime(_regime(gov, any_door_open=True))
    thermal_dec, thermal_reason = gov.decide_update("thermal")
    gains_dec, gains_reason = gov.decide_update("gains")
    assert thermal_dec == GovernanceDecision.HARD_FREEZE
    assert thermal_reason == FreezeReason.COUPLED
    assert gains_dec == GovernanceDecision.FREEZE
    assert gains_reason == FreezeReason.COUPLED


def test_perturbation_takes_priority_over_coupling():
    gov = SmartPIGovernance("t")
    regime = _regime(gov, any_door_open=True, power_shedding=True)
    assert regime == GovernanceRegime.PERTURBED


def test_degraded_takes_priority_over_coupling():
    gov = SmartPIGovernance("t")
    regime = _regime(gov, any_door_open=True, ext_temp=None)
    assert regime == GovernanceRegime.DEGRADED


def test_warmup_takes_priority_over_coupling():
    gov = SmartPIGovernance("t")
    regime = _regime(gov, any_door_open=True, phase=SmartPIPhase.HYSTERESIS)
    assert regime == GovernanceRegime.WARMUP
