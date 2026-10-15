"""Tests for the model-gain generation used by the periodic FF-trim window."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import (
    GovernanceDecision,
    GovernanceRegime,
)
from custom_components.vtherm_smartpi.smartpi.gains import GainScheduler


def _model(a: float = 0.02) -> tuple[SimpleNamespace, SimpleNamespace]:
    estimator = SimpleNamespace(a=a)
    dt_est = SimpleNamespace(
        deadtime_heat_s=300.0,
        deadtime_heat_reliable=True,
        deadtime_cool_s=300.0,
        deadtime_cool_reliable=True,
    )
    return estimator, dt_est


def _calculate(
    scheduler: GainScheduler,
    *,
    a: float = 0.02,
    in_near_band: bool = True,
    ratio: float = 1.0,
    decision: GovernanceDecision = GovernanceDecision.ADAPT_ON,
):
    estimator, dt_est = _model(a)
    return scheduler.calculate(
        tau_reliable=True,
        tau_min=200.0,
        estimator=estimator,
        dt_est=dt_est,
        in_near_band=in_near_band,
        governance_decision=decision,
        near_band_ratio=ratio,
        cycle_min=10.0,
    )


def test_near_band_taper_keeps_the_gain_generation() -> None:
    """A taper-only Kp/Ki change belongs to the same gain generation."""
    scheduler = GainScheduler("taper")
    first = _calculate(scheduler, ratio=1.0)
    generation = scheduler.generation

    tapered = _calculate(scheduler, ratio=0.3)
    untapered = _calculate(scheduler, in_near_band=False)

    assert generation == 1
    assert tapered.kp < first.kp
    assert tapered.ki < first.ki
    assert untapered.kp == pytest.approx(first.kp)
    assert scheduler.generation == generation


def test_model_change_starts_a_gain_generation() -> None:
    """A different A/B model changes the untapered gains and the generation."""
    scheduler = GainScheduler("model")
    _calculate(scheduler, a=0.02)

    _calculate(scheduler, a=0.03, ratio=0.5)

    assert scheduler.generation == 2


def test_frozen_model_change_counts_when_the_gains_follow_it() -> None:
    """A model change hidden by a gain freeze starts a generation on release."""
    scheduler = GainScheduler("freeze")
    _calculate(scheduler, a=0.02)

    frozen = _calculate(scheduler, a=0.03, decision=GovernanceDecision.HARD_FREEZE)

    assert frozen.kp_source == "frozen"
    assert scheduler.generation == 1

    _calculate(scheduler, a=0.03)

    assert scheduler.generation == 2


def test_safe_gains_after_reset_start_from_generation_zero() -> None:
    """Reset returns to the safe gains and forgets the active generation."""
    scheduler = GainScheduler("reset")
    _calculate(scheduler)

    scheduler.reset()

    assert scheduler.generation == 0
    _calculate(scheduler)
    assert scheduler.generation == 1


def test_ownership_snapshot_uses_the_scheduler_generation() -> None:
    """The causal trace sees one generation across near-band taper changes."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="gain-generation",
        use_ff3=False,
    )
    algo.est.a = 0.02
    algo.est.b = 0.001
    algo.est.a_meas_hist.extend([0.02] * 8)
    algo.est.b_meas_hist.extend([0.001] * 8)
    algo.est.learn_ok_count = 40
    algo.est.learn_ok_count_a = 20
    algo.est.learn_ok_count_b = 20
    algo.dt_est.deadtime_heat_s = 300.0
    algo.dt_est.deadtime_heat_reliable = True
    algo._tau_reliable = True
    algo._cycles_since_reset = algo.ff_warmup_cycles
    algo.gov.regime = GovernanceRegime.NEAR_BAND
    algo.deadband_mgr.in_near_band = True

    generations = []
    gains = []
    for error in (0.30, 0.20, 0.10):
        algo._apply_gains_and_ff(
            GovernanceDecision.ADAPT_ON,
            target_temp_ff=20.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            error=error,
            current_temp=20.0 - error,
            e_p=error,
        )
        snapshot = algo._build_command_ownership_snapshot(VThermHvacMode_HEAT)
        generations.append(snapshot.gain_generation)
        gains.append((algo.Kp, algo.Ki))

    assert len(set(gains)) == 3
    assert generations == [algo.gain_scheduler.generation] * 3
