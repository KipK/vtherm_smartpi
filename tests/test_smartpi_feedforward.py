"""Tests for the Feed-Forward orchestrator (smartpi/feedforward.py)."""

from unittest.mock import MagicMock

import pytest
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.feedforward import (
    FFResult,
    LearnedStepTransfer,
    compute_ff,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    GovernanceDecision,
    GovernanceRegime,
)
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
)


def _call(
    *,
    k_ff: float = 0.20,
    ext_temp: float | None = 5.0,
    target_temp_ff: float = 21.0,
    warmup_scale: float = 1.0,
) -> FFResult:
    """Helper: call compute_ff with sensible defaults for behavioural tests."""
    return compute_ff(
        k_ff=k_ff,
        target_temp_ff=target_temp_ff,
        ext_temp=ext_temp,
        warmup_scale=warmup_scale,
    )


def test_signed_cool_feedforward_matches_heat_magnitude():
    """Signed b/a produces positive holding power when outdoor air is warmer."""
    heat = _call(
        k_ff=0.05,
        target_temp_ff=20.0,
        ext_temp=10.0,
    )
    cool = _call(
        k_ff=-0.05,
        target_temp_ff=20.0,
        ext_temp=30.0,
    )

    assert heat.u_ff1 == pytest.approx(0.5)
    assert cool.u_ff1 == pytest.approx(heat.u_ff1)


def test_algo_enables_signed_structural_feedforward_in_cool():
    """The runtime orchestrator does not force COOL FF1 to zero."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="cool-ff",
    )
    algo.est.a = -0.05
    algo.est.b = 0.0025
    algo.est.model_hvac_mode = "cool"
    algo.est.a_meas_hist.extend([-0.05] * 8)
    algo.est.b_meas_hist.extend([0.0025] * 8)
    algo.est._b_hat_hist.extend([0.0025] * 5)
    algo.est.learn_ok_count = 40
    algo.est.learn_ok_count_a = 20
    algo.est.learn_ok_count_b = 20
    algo.dt_est.deadtime_cool_s = 120.0
    algo.dt_est.deadtime_cool_reliable = True
    algo._tau_reliable = True
    algo._cycles_since_reset = algo.ff_warmup_cycles
    algo.gov.regime = GovernanceRegime.EXCITED_STABLE

    runtime_ff = algo._apply_gains_and_ff(
        GovernanceDecision.ADAPT_ON,
        target_temp_ff=20.0,
        ext_current_temp=30.0,
        hvac_mode=VThermHvacMode_COOL,
        error=0.2,
        e_p=0.2,
    )

    assert algo._last_ff_result is not None
    assert algo._last_ff_result.u_ff1 == pytest.approx(0.5)
    assert runtime_ff == pytest.approx(0.5)


# ================================================================
# Passthrough Tests
# ================================================================


class TestPassthrough:
    """FF1 reflects the structural branch k_ff * (target - ext) scaled by the warm-up."""

    def test_positive_error_passes_through(self):
        result = _call(k_ff=0.2, target_temp_ff=21.0, ext_temp=5.0)
        # u_ff1 = clamp(0.2 * (21 - 5), 0, 1) * 1.0 = clamp(3.2, 0, 1) = 1.0
        assert result.u_ff1 == pytest.approx(1.0)
        assert result.ff_reason == "ff_none"

    def test_small_positive_error_passes_through(self):
        result = _call(k_ff=0.05, target_temp_ff=21.0, ext_temp=20.0)
        # u_ff1 = clamp(0.05 * 1.0, 0, 1) = 0.05
        assert result.u_ff1 == pytest.approx(0.05)
        assert result.ff_reason == "ff_none"

    def test_no_ext_temp_gives_zero(self):
        result = _call(ext_temp=None)
        assert result.u_ff1 == 0.0
        assert result.ff_reason == "ff_no_ext_temp"

    def test_zero_warmup_scale_gives_zero(self):
        result = _call(warmup_scale=0.0)
        assert result.u_ff1 == 0.0

    def test_half_warmup_scale_halves_ff1(self):
        result = _call(k_ff=0.05, target_temp_ff=21.0, ext_temp=15.0, warmup_scale=0.5)
        assert result.u_ff1 == pytest.approx(0.15)
        assert result.warmup_scale == pytest.approx(0.5)


# ================================================================
# FFResult structure and wall part
# ================================================================


class TestFFResult:
    """FFResult carries the FF1 power, its reason and the warm-up factor."""

    def test_result_fields(self):
        result = _call(k_ff=0.1, target_temp_ff=21.0, ext_temp=15.0)
        assert isinstance(result, FFResult)
        assert result.u_ff1 == pytest.approx(0.6)
        assert result.ff_reason == "ff_none"
        assert result.warmup_scale == pytest.approx(1.0)


class TestLearnedStepTransfer:
    """A calibration step of the learned holding power is handed to the integral once."""

    def test_the_step_of_a_corrected_learned_gain_is_transferred_once(self):
        tr = LearnedStepTransfer()
        kw = dict(target_temp=20.0, ext_temp=5.0)
        old = compute_ff(k_ff=0.006, target_temp_ff=20.0, ext_temp=5.0, warmup_scale=0.5)
        new = compute_ff(k_ff=0.012, target_temp_ff=20.0, ext_temp=5.0, warmup_scale=0.5)
        assert tr.update(learned=old, **kw) == 0.0
        tr.note(0.006)
        assert tr.update(learned=new, **kw) == pytest.approx(new.u_ff1 - old.u_ff1)
        assert tr.last_delta == pytest.approx(0.045)
        assert tr.update(learned=new, **kw) == 0.0 and tr.last_delta == 0.0

    def test_a_reset_forgets_a_pending_step(self):
        tr = LearnedStepTransfer()
        new = compute_ff(k_ff=0.012, target_temp_ff=20.0, ext_temp=5.0, warmup_scale=0.5)
        tr.note(0.006)
        tr.reset()
        assert tr.update(learned=new, target_temp=20.0, ext_temp=5.0) == 0.0


# ================================================================
# Invariant: 0 <= u_ff1 <= 1
# ================================================================


class TestInvariants:
    """u_ff1 must always be in [0, 1]."""

    @pytest.mark.parametrize("k_ff", [0.0, 0.05, 0.20, 0.50, 2.0])
    def test_u_ff1_in_range(self, k_ff):
        result = _call(k_ff=k_ff)
        assert 0.0 <= result.u_ff1 <= 1.0
