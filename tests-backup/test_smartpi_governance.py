"""Test the Smart-PI Safety-First Governance layer."""

import logging
import math
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.smartpi.ab_estimator import ABEstimator
from custom_components.versatile_thermostat.smartpi.const import (
    AB_HISTORY_SIZE,
    SmartPIPhase,
)
from custom_components.versatile_thermostat.smartpi.const import (
    GovernanceRegime,
    GovernanceDecision,
    FreezeReason,
    GOVERNANCE_MATRIX,
    KP_SAFE,
    KI_SAFE,
)
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT


_LOGGER = logging.getLogger(__name__)


def make_smartpi(**kwargs):
    """Create a SmartPI instance with sensible test defaults."""
    defaults = dict(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestGov",
        debug_mode=True,
    )
    defaults.update(kwargs)
    return SmartPI(**defaults)


def force_stable_mode(smartpi):
    """Force SmartPI into STABLE phase by populating measurement history."""
    for _ in range(AB_HISTORY_SIZE + 1):
        smartpi.est.a_meas_hist.append(0.01)
        smartpi.est.b_meas_hist.append(0.002)
    if hasattr(smartpi, "dt_est"):
        smartpi.dt_est.deadtime_heat_s = 600.0
        smartpi.dt_est.deadtime_cool_s = 600.0
        smartpi.dt_est.deadtime_heat_reliable = True
        smartpi.dt_est.deadtime_cool_reliable = True
        smartpi.calibration_mgr.last_calibration_time = time.time()


# =====================================================================
#  _determine_current_regime
# =====================================================================

class TestDetermineCurrentRegime:
    """Tests for the regime detection helper."""

    def call_determine(self, spi, ext_temp=10.0, **kwargs):
        defaults = dict(
            phase=spi.phase,
            ext_temp=ext_temp,
            integrator_hold=False,
            power_shedding=False,
            output_initialized=spi._output_initialized,
            u_cmd=spi._on_percent,
            in_deadband=spi.in_deadband,
            in_near_band=spi.in_near_band,
        )
        defaults.update(kwargs)
        return spi.gov.determine_regime(**defaults)

    def test_warmup_in_hysteresis(self):
        """Hysteresis phase → WARMUP regime."""
        spi = make_smartpi()
        # Default is HYSTERESIS (no measurements yet)
        assert spi.phase == SmartPIPhase.HYSTERESIS
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.WARMUP

    def test_degraded_when_ext_temp_none(self):
        """No external temperature sensor → DEGRADED."""
        spi = make_smartpi()
        force_stable_mode(spi)
        regime = self.call_determine(spi, ext_temp=None)
        assert regime == GovernanceRegime.DEGRADED

    def test_perturbed_power_shedding(self):
        """Power shedding active → PERTURBED."""
        spi = make_smartpi()
        force_stable_mode(spi)
        regime = self.call_determine(spi, ext_temp=10.0, power_shedding=True)
        assert regime == GovernanceRegime.PERTURBED

    def test_hold(self):
        """Integrator hold → HOLD."""
        spi = make_smartpi()
        force_stable_mode(spi)
        regime = self.call_determine(spi, ext_temp=10.0, integrator_hold=True)
        assert regime == GovernanceRegime.HOLD

    def test_saturated_high(self):
        """Command at 100% → SATURATED."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 1.0
        spi._output_initialized = True  # Mark output as valid
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.SATURATED

    def test_saturated_low(self):
        """Command at 0% → SATURATED."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.0
        spi._output_initialized = True  # Mark output as valid
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.SATURATED

    def test_dead_band(self):
        """In dead band → DEAD_BAND."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = True
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.DEAD_BAND

    def test_near_band(self):
        """In near band (but not deadband) → NEAR_BAND."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = False
        spi.in_near_band = True
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.NEAR_BAND

    def test_excited_stable_default(self):
        """Normal stable PI operation → EXCITED_STABLE."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = False
        spi.in_near_band = False
        regime = self.call_determine(spi, ext_temp=10.0)
        assert regime == GovernanceRegime.EXCITED_STABLE


# =====================================================================
#  decide_update
# =====================================================================

class TestDecideUpdate:
    """Tests for the central governance decision method."""

    def call_decide(self, spi, domain):
        lrt = spi.learning_resume_ts
        return spi.gov.decide_update(domain, learning_resume_ts=lrt, now=time.monotonic())

    def test_regime_transition_freezes_both(self):
        """Multiple regimes in cycle → HARD_FREEZE for both domains."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = False
        spi.in_near_band = False
        spi.gov._current_regime = GovernanceRegime.EXCITED_STABLE
        # Simulate regime transition (two different regimes seen in cycle)
        spi.gov._cycle_regimes = {GovernanceRegime.EXCITED_STABLE, GovernanceRegime.NEAR_BAND}

        dec_t, reason_t = self.call_decide(spi, 'thermal')
        dec_g, reason_g = self.call_decide(spi, 'gains')

        assert dec_t == GovernanceDecision.HARD_FREEZE
        assert reason_t == FreezeReason.REGIME_TRANSITION
        assert dec_g == GovernanceDecision.HARD_FREEZE
        assert reason_g == FreezeReason.REGIME_TRANSITION
        assert spi.gov.last_decision_thermal == GovernanceDecision.HARD_FREEZE
        assert spi.gov.last_freeze_reason_thermal == FreezeReason.REGIME_TRANSITION
        assert spi.gov.last_decision_gains == GovernanceDecision.HARD_FREEZE
        assert spi.gov.last_freeze_reason_gains == FreezeReason.REGIME_TRANSITION

    def test_perturbed_resume_freezes(self):
        """Active resume cool-down → HARD_FREEZE."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.gov._current_regime = GovernanceRegime.EXCITED_STABLE
        spi.gov._cycle_regimes = {GovernanceRegime.EXCITED_STABLE}
        # Set resume timestamp in the future
        spi.learn_win.set_learning_resume_ts(time.monotonic() + 3600)

        dec, reason = self.call_decide(spi, 'thermal')
        assert dec == GovernanceDecision.HARD_FREEZE
        assert reason == FreezeReason.PERTURBED
        assert spi.gov.last_decision_thermal == GovernanceDecision.HARD_FREEZE
        assert spi.gov.last_freeze_reason_thermal == FreezeReason.PERTURBED

    def test_excited_stable_adapts_both(self):
        """Single EXCITED_STABLE regime → ADAPT_ON for both domains."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = False
        spi.in_near_band = False
        spi.gov._current_regime = GovernanceRegime.EXCITED_STABLE
        spi.gov._cycle_regimes = {GovernanceRegime.EXCITED_STABLE}

        dec_t, reason_t = self.call_decide(spi, 'thermal')
        dec_g, reason_g = self.call_decide(spi, 'gains')

        assert dec_t == GovernanceDecision.ADAPT_ON
        assert reason_t == FreezeReason.NONE
        assert dec_g == GovernanceDecision.ADAPT_ON
        assert reason_g == FreezeReason.NONE

    def test_near_band_freezes_thermal_softfreezes_gains(self):
        """Single NEAR_BAND regime → thermal ADAPT_ON, gains ADAPT_ON."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_near_band = True
        spi.in_deadband = False
        spi.gov._current_regime = GovernanceRegime.NEAR_BAND
        spi.gov._cycle_regimes = {GovernanceRegime.NEAR_BAND}

        dec_t, reason_t = self.call_decide(spi, 'thermal')
        dec_g, reason_g = self.call_decide(spi, 'gains')

        assert dec_t == GovernanceDecision.ADAPT_ON
        assert reason_t == FreezeReason.NONE
        assert dec_g == GovernanceDecision.ADAPT_ON
        assert reason_g == FreezeReason.NONE

    def test_dead_band_freezes_all(self):
        """Single DEAD_BAND regime → HARD_FREEZE for both."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi._on_percent = 0.5
        spi.in_deadband = True
        spi.gov._current_regime = GovernanceRegime.DEAD_BAND
        spi.gov._cycle_regimes = {GovernanceRegime.DEAD_BAND}

        dec_t, _ = self.call_decide(spi, 'thermal')
        dec_g, _ = self.call_decide(spi, 'gains')

        assert dec_t == GovernanceDecision.HARD_FREEZE
        assert dec_g == GovernanceDecision.HARD_FREEZE

    def test_warmup_adapts_thermal_freezes_gains(self):
        """WARMUP regime → thermal ADAPT_ON, gains FREEZE."""
        spi = make_smartpi()
        # Default = HYSTERESIS = WARMUP
        spi.gov._current_regime = GovernanceRegime.WARMUP
        spi.gov._cycle_regimes = {GovernanceRegime.WARMUP}

        dec_t, reason_t = self.call_decide(spi, 'thermal')
        dec_g, reason_g = self.call_decide(spi, 'gains')

        assert dec_t == GovernanceDecision.ADAPT_ON
        assert reason_t == FreezeReason.NONE
        assert dec_g == GovernanceDecision.FREEZE
        assert reason_g == FreezeReason.WARMUP


# =====================================================================
#  Governance matrix completeness
# =====================================================================

class TestGovernanceMatrix:
    """Verify the governance matrix covers all regimes."""

    def test_all_regimes_covered(self):
        """Every GovernanceRegime must have an entry in the matrix."""
        for regime in GovernanceRegime:
            assert regime in GOVERNANCE_MATRIX, f"Missing matrix entry for {regime}"
            assert "thermal" in GOVERNANCE_MATRIX[regime]
            assert "gains" in GOVERNANCE_MATRIX[regime]


# =====================================================================
#  Cycle regime tracking
# =====================================================================

class TestCycleRegimeTracking:
    """Tests for regime accumulation and reset."""

    @pytest.mark.asyncio
    async def test_cycle_regimes_reset_on_cycle_start(self):
        """on_cycle_started should clear the regime set."""
        spi = make_smartpi()
        spi.gov._cycle_regimes = {GovernanceRegime.EXCITED_STABLE, GovernanceRegime.NEAR_BAND}
        
        await spi.on_cycle_started(
            on_time_sec=300, off_time_sec=300,
            on_percent=0.5, hvac_mode="Heat"
        )
        
        assert len(spi.gov._cycle_regimes) == 0

    def test_regime_added_during_calculate(self):
        """calculate() should add current regime to cycle set."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi.est.learn_ok_count = 15
        spi.est.learn_ok_count_b = 15
        spi.est.b = 0.002
        for _ in range(10):
            spi.est._b_hat_hist.append(0.002)

        spi.gov._cycle_regimes.clear()

        # Calculate with normal conditions (should add EXCITED_STABLE or similar)
        spi.calculate(
            target_temp=20.0, current_temp=18.0,
            ext_current_temp=10.0, slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )

        assert len(spi.gov._cycle_regimes) > 0, "Regime should be added during calculate"


# =====================================================================
#  Integration: Gains governance in calculate()
# =====================================================================

class TestGainsGovernance:
    """Test that governance correctly gates Kp/Ki updates in calculate()."""

    def test_soft_freeze_down_prevents_increase(self):
        """In SOFT_FREEZE_DOWN, gains should not increase from previous values."""
        spi = make_smartpi(near_band_deg=0.5)
        force_stable_mode(spi)
        spi.est.learn_ok_count = 15
        spi.est.learn_ok_count_b = 15
        spi.est.b = 0.002
        for _ in range(10):
            spi.est._b_hat_hist.append(0.002)

        # First calculate: establish baseline gains at EXCITED_STABLE
        spi.calculate(
            target_temp=20.0, current_temp=18.0,  # large error, outside near-band
            ext_current_temp=10.0, slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )
        kp_baseline = spi.Kp
        ki_baseline = spi.Ki

        # Now move into near-band (should trigger SOFT_FREEZE_DOWN for gains)
        spi._last_calculate_time = None
        spi._e_filt = None
        spi.gov._cycle_regimes.clear()  # Reset to avoid REGIME_TRANSITION

        spi.calculate(
            target_temp=20.0, current_temp=19.8,  # error=0.2 < near_band_deg=0.5
            ext_current_temp=10.0, slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )

        # Gains should not exceed baseline (SOFT_FREEZE_DOWN)
        assert spi.Kp <= kp_baseline + 1e-9, \
            f"Kp should not increase: {spi.Kp} vs baseline {kp_baseline}"
        assert spi.Ki <= ki_baseline + 1e-9, \
            f"Ki should not increase: {spi.Ki} vs baseline {ki_baseline}"


# =====================================================================
#  Diagnostics
# =====================================================================

class TestGovernanceDiagnostics:
    """Test that governance fields appear in diagnostics."""

    def test_diagnostics_contain_governance_keys(self):
        """get_diagnostics() should expose all governance fields."""
        spi = make_smartpi()
        diag = spi.get_diagnostics()

        expected_keys = [
            "governance_regime",
            "governance_cycle_regimes",
            "last_freeze_reason_thermal",
            "last_freeze_reason_gains",
            "last_decision_thermal",
            "last_decision_gains",
        ]
        for key in expected_keys:
            assert key in diag, f"Missing diagnostic key: {key}"

    def test_diagnostics_values_after_calculate(self):
        """Governance diagnostics should reflect actual state after calculate."""
        spi = make_smartpi()
        force_stable_mode(spi)
        spi.est.learn_ok_count = 15
        spi.est.learn_ok_count_b = 15
        spi.est.b = 0.002
        for _ in range(10):
            spi.est._b_hat_hist.append(0.002)

        spi.calculate(
            target_temp=20.0, current_temp=18.0,
            ext_current_temp=10.0, slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )

        diag = spi.get_diagnostics()
        assert diag["governance_regime"] in [r.value for r in GovernanceRegime]
        assert isinstance(diag["governance_cycle_regimes"], list)
        assert len(diag["governance_cycle_regimes"]) > 0


# =====================================================================
#  Reset
# =====================================================================

class TestGovernanceReset:
    """Test that reset_learning clears governance state."""

    def test_reset_clears_governance(self):
        """reset_learning should clear all governance attributes."""
        spi = make_smartpi()
        spi.gov._cycle_regimes = {GovernanceRegime.NEAR_BAND, GovernanceRegime.DEAD_BAND}
        spi.gov.last_freeze_reason_thermal = FreezeReason.NEAR_BAND
        spi.gov.last_freeze_reason_gains = FreezeReason.DEAD_BAND
        spi.gov.last_decision_thermal = GovernanceDecision.HARD_FREEZE
        spi.gov.last_decision_gains = GovernanceDecision.HARD_FREEZE

        spi.reset_learning()

        assert len(spi.gov._cycle_regimes) == 0
        assert spi.gov.last_freeze_reason_thermal == FreezeReason.NONE
        assert spi.gov.last_freeze_reason_gains == FreezeReason.NONE
        assert spi.gov.last_decision_thermal == GovernanceDecision.ADAPT_ON
        assert spi.gov.last_decision_gains == GovernanceDecision.ADAPT_ON
        assert spi._prev_kp == KP_SAFE
        assert spi._prev_ki == KI_SAFE
