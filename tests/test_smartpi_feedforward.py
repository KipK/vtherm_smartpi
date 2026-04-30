"""Tests for the Feed-Forward orchestrator (smartpi/feedforward.py)."""

import pytest
from custom_components.vtherm_smartpi.smartpi.feedforward import (
    compute_ff,
    FFResult,
)
from custom_components.vtherm_smartpi.smartpi.ff3 import (
    _compute_near_band_ff3_scale,
    compute_ff3,
)
from custom_components.vtherm_smartpi.smartpi.ff3_predictor import (
    compute_ff3_action_sensitivity,
    compute_ff3_horizon,
    predict_ff3_open_loop,
)
from custom_components.vtherm_smartpi.smartpi.ff3_eligibility import (
    build_ff3_disturbance_context,
)
from custom_components.vtherm_smartpi.smartpi.ff_trim import FFTrim
from custom_components.vtherm_smartpi.smartpi.const import (
    GovernanceRegime,
    FF3_DELTA_U,
    FF3_MAX_HORIZON_CYCLES,
    FF3_MAX_AUTHORITY,
    FF3_NEARBAND_GAIN,
    FF_TRIM_LAMBDA,
)
from custom_components.vtherm_smartpi.smartpi.thermal_twin_1r1c import ThermalTwin1R1C
from custom_components.vtherm_smartpi.const import (
    CONF_SMART_PI_USE_FF3,
    DEFAULT_OPTIONS,
    DEFAULT_SMART_PI_USE_FF3,
)
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


def _call(
    *,
    error: float,
    near_band_above_deg: float = 0.3,
    near_band_below_deg: float = 0.4,
    k_ff: float = 0.20,
    ext_temp: float | None = 5.0,
    target_temp_ff: float = 21.0,
    warmup_scale: float = 1.0,
    regime: GovernanceRegime = GovernanceRegime.EXCITED_STABLE,
    ab_fallback: float | None = None,
    trim_value: float = 0.0,
) -> FFResult:
    """Helper: call compute_ff with sensible defaults for behavioural tests."""
    trim = FFTrim()
    trim.u_ff_trim = trim_value
    return compute_ff(
        k_ff=k_ff,
        target_temp_ff=target_temp_ff,
        ext_temp=ext_temp,
        warmup_scale=warmup_scale,
        trim=trim,
        regime=regime,
        error=error,
        near_band_below_deg=near_band_below_deg,
        near_band_above_deg=near_band_above_deg,
        ab_fallback=ab_fallback,
    )


def test_smartpi_ff3_default_is_enabled():
    assert DEFAULT_SMART_PI_USE_FF3 is True
    assert DEFAULT_OPTIONS[CONF_SMART_PI_USE_FF3] is True


def test_smartpi_ff3_missing_entry_uses_default_options():
    entry = {}
    use_ff3 = entry.get(
        CONF_SMART_PI_USE_FF3,
        DEFAULT_OPTIONS[CONF_SMART_PI_USE_FF3],
    )
    assert use_ff3 is True


def test_smartpi_ff3_explicit_false_stays_disabled():
    entry = {CONF_SMART_PI_USE_FF3: False}
    use_ff3 = entry.get(
        CONF_SMART_PI_USE_FF3,
        DEFAULT_OPTIONS[CONF_SMART_PI_USE_FF3],
    )
    assert use_ff3 is False


def test_ff3_horizon_covers_heat_deadtime():
    horizon = compute_ff3_horizon(cycle_min=10.0, deadtime_heat_s=1800.0)
    assert horizon.deadtime_cycles == 3
    assert horizon.horizon_cycles == 5
    assert horizon.horizon_capped is False


def test_ff3_horizon_caps_long_deadtime():
    horizon = compute_ff3_horizon(cycle_min=10.0, deadtime_heat_s=7200.0)
    assert horizon.deadtime_cycles == 12
    assert horizon.horizon_cycles == FF3_MAX_HORIZON_CYCLES
    assert horizon.horizon_capped is True


def test_ff3_open_loop_candidate_effect_appears_after_deadtime():
    twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
    twin.reset(tin_init=20.7, text_init=5.0, u_init=0.6)

    base_prediction = predict_ff3_open_loop(
        twin=twin,
        current_temp=20.7,
        ext_temp=5.0,
        a=0.08,
        b=0.004,
        u_first_cycle=0.6,
        u_base=0.6,
        cycle_min=10.0,
        deadtime_heat_s=1800.0,
        horizon_cycles=5,
    )
    candidate_prediction = predict_ff3_open_loop(
        twin=twin,
        current_temp=20.7,
        ext_temp=5.0,
        a=0.08,
        b=0.004,
        u_first_cycle=0.8,
        u_base=0.6,
        cycle_min=10.0,
        deadtime_heat_s=1800.0,
        horizon_cycles=5,
    )

    assert base_prediction.temperatures[:3] == pytest.approx(
        candidate_prediction.temperatures[:3],
        abs=1e-9,
    )
    assert (
        base_prediction.temperatures[3]
        != pytest.approx(candidate_prediction.temperatures[3])
        or base_prediction.temperatures[4]
        != pytest.approx(candidate_prediction.temperatures[4])
    )
    assert compute_ff3_action_sensitivity([base_prediction, candidate_prediction]) > 0.0


# ================================================================
# Hard Gate Tests (legacy behaviour preserved)
# ================================================================


class TestHardGate:
    """Hard gate (ff_cut_above_setpoint) has been removed. FF passes through regardless of sign."""

    def test_above_setpoint_cuts_ff(self):
        """Hard gate removed: FF is NOT cut when error < -near_band_above_deg."""
        result = _call(error=-0.5, near_band_above_deg=0.3)
        assert result.u_ff_eff > 0.0
        assert result.ff_reason != "ff_cut_above_setpoint"

    def test_at_setpoint_exact_no_cut(self):
        """error == 0 should NOT trigger hard gate."""
        result = _call(error=0.0, near_band_above_deg=0.3)
        assert result.u_ff_eff > 0.0
        assert result.ff_reason != "ff_cut_above_setpoint"

    def test_in_near_band_above_no_cut(self):
        """error in (-near_band_above_deg, 0) should NOT trigger hard gate."""
        result = _call(error=-0.1, near_band_above_deg=0.3)
        assert result.u_ff_eff > 0.0
        assert result.ff_reason != "ff_cut_above_setpoint"

    def test_hard_gate_with_zero_raw(self):
        """With k_ff=0 there is no FF regardless of error; reason is not ff_cut_above_setpoint."""
        result = _call(error=-1.0, near_band_above_deg=0.3, k_ff=0.0)
        assert result.u_ff_eff == 0.0
        assert result.ff_reason != "ff_cut_above_setpoint"

    def test_hard_gate_exact_boundary(self):
        """error == -near_band_above_deg: no gate, FF passes through."""
        # error == -0.3, near_band_above_deg == 0.3 -> NOT cut
        result = _call(error=-0.3, near_band_above_deg=0.3)
        assert result.ff_reason != "ff_cut_above_setpoint"


# ================================================================
# Passthrough Tests
# ================================================================


class TestPassthrough:
    """When no gate fires, u_ff_eff reflects u_ff_ab + trim."""

    def test_positive_error_passes_through(self):
        result = _call(error=1.0, k_ff=0.2, target_temp_ff=21.0, ext_temp=5.0)
        # u_ff_ab = clamp(0.2 * (21 - 5), 0, 1) * 1.0 = clamp(3.2, 0, 1) = 1.0
        assert result.u_ff_eff == pytest.approx(1.0)
        assert result.ff_reason == "ff_none"

    def test_small_positive_error_passes_through(self):
        result = _call(error=0.05, k_ff=0.05, target_temp_ff=21.0, ext_temp=20.0)
        # u_ff_ab = clamp(0.05 * 1.0, 0, 1) = 0.05
        assert result.u_ff_eff == pytest.approx(0.05)
        assert result.ff_reason == "ff_none"

    def test_no_ext_temp_gives_zero(self):
        result = _call(error=1.0, ext_temp=None)
        assert result.u_ff_eff == 0.0
        assert result.ff_reason == "ff_no_ext_temp"

    def test_zero_warmup_scale_gives_zero(self):
        result = _call(error=1.0, warmup_scale=0.0)
        assert result.u_ff_eff == 0.0


# ================================================================
# FFResult structure
# ================================================================


class TestFFResult:
    """Verify all FFResult fields are populated correctly."""

    def test_result_fields_present(self):
        result = _call(error=0.5, k_ff=0.1, target_temp_ff=21.0, ext_temp=15.0)
        assert isinstance(result, FFResult)
        assert hasattr(result, "ff_raw")
        assert hasattr(result, "u_ff1")
        assert hasattr(result, "u_ff2")
        assert hasattr(result, "u_ff_final")
        assert hasattr(result, "u_ff3")
        assert hasattr(result, "u_db_nominal")
        assert hasattr(result, "u_ff_ab")
        assert hasattr(result, "u_ff_trim")
        assert hasattr(result, "u_ff_base")
        assert hasattr(result, "u_ff_eff")
        assert hasattr(result, "ff_reason")

    def test_no_trim_base_equals_ab(self):
        """With default (zero) trim, u_ff_base == u_ff_ab."""
        result = _call(error=1.0, k_ff=0.1, target_temp_ff=21.0, ext_temp=15.0)
        assert result.u_ff_base == pytest.approx(result.u_ff_ab)

    def test_ab_fallback_overrides_ab(self):
        """When ab_fallback is provided, u_ff_ab == fallback value."""
        result = _call(error=1.0, ab_fallback=0.30)
        assert result.u_ff_ab == pytest.approx(0.30)
        assert result.u_ff1 == pytest.approx(0.30)
        assert result.ff_reason == "ff_ab_fallback"

    def test_ff_final_matches_sum_of_ff1_and_ff2(self):
        """The explicit FF final command must be the saturated sum of FF1 and FF2."""
        result = _call(
            error=1.0,
            k_ff=0.05,
            target_temp_ff=21.0,
            ext_temp=15.0,
            trim_value=0.10,
        )
        assert result.u_ff1 == pytest.approx(0.30)
        assert result.u_ff2 == pytest.approx(0.10)
        assert result.u_ff_final == pytest.approx(0.40)
        assert result.u_db_nominal == pytest.approx(result.u_ff_final)
        assert result.u_ff_eff == pytest.approx(result.u_ff_final)

    def test_ff3_only_changes_effective_ff(self):
        """FF3 must not rewrite the nominal FF1 + FF2 result."""
        trim = FFTrim()
        trim.u_ff_trim = 0.10
        result = compute_ff(
            k_ff=0.05,
            target_temp_ff=21.0,
            ext_temp=15.0,
            warmup_scale=1.0,
            trim=trim,
            regime=GovernanceRegime.EXCITED_STABLE,
            error=1.0,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            ab_fallback=None,
            u_ff3=0.05,
        )
        assert result.u_ff1 == pytest.approx(0.30)
        assert result.u_ff2 == pytest.approx(0.10)
        assert result.u_ff_final == pytest.approx(0.40)
        assert result.u_ff3 == pytest.approx(0.05)
        assert result.u_db_nominal == pytest.approx(0.40)
        assert result.u_ff_eff == pytest.approx(0.45)

    def test_trim_update_applies_incremental_correction(self):
        """FFTrim.update must apply delta_power as an increment, not an absolute target.

        authority = FF_TRIM_RHO * u_ff_ab = 0.15 * 1.0 = 0.15
        Starting trim must be within [-0.15, 0.15] to avoid clamping.
        expected = u_ff_trim + FF_TRIM_LAMBDA * delta_power.
        """
        trim = FFTrim()
        trim.u_ff_trim = 0.10  # within authority (0.15)

        trim.update(delta_power=0.03, u_ff_ab=1.0)

        expected = 0.10 + FF_TRIM_LAMBDA * 0.03
        assert trim.u_ff_trim == pytest.approx(expected)


class TestFF3:
    """Behavioural tests for the FF3 conservative selector."""

    @staticmethod
    def _make_initialized_twin() -> ThermalTwin1R1C:
        twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
        twin.reset(tin_init=19.0, text_init=5.0, u_init=0.4)
        result = twin.step(
            tin_meas=19.0,
            text_meas=5.0,
            a=0.02,
            b=0.002,
            u_now=0.4,
            deadtime_s=0.0,
            sp=21.0,
            dt_s=60.0,
            mode="heat",
        )
        assert result["status"] == "ok"
        assert twin.T_hat is not None
        return twin

    def test_ff3_disabled_on_recent_setpoint_change(self):
        """FF3 must remain off immediately after a setpoint change."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=False,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=True,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
        )
        assert result.enabled is False
        assert result.u_ff3_applied == pytest.approx(0.0)
        assert result.reason_disabled == "recent_setpoint_change"
        assert result.horizon_cycles == 2

    def test_ff3_disabled_outside_near_band(self):
        """FF3 must remain off outside the near-band."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=False,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
        )
        assert result.enabled is False
        assert result.u_ff3_applied == pytest.approx(0.0)
        assert result.reason_disabled == "not_near_band"
        assert result.candidate_scores == []
        assert result.horizon_cycles == 2

    def test_ff3_disabled_without_disturbance_context(self):
        """FF3 must remain off when no disturbance-recovery context is active."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
            disturbance_context_active=False,
            disturbance_context_reason="trajectory_setpoint_active",
        )
        assert result.enabled is False
        assert result.u_ff3_applied == pytest.approx(0.0)
        assert result.reason_disabled == "trajectory_setpoint_active"
        assert result.candidate_scores == []
        assert result.horizon_cycles == 2

    def test_ff3_disabled_when_twin_is_warming_up(self):
        """FF3 must expose the warm-up reason instead of a generic reliability failure."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=False,
            twin_initialized=True,
            twin_disabled_reason="twin_warming_up",
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
        )
        assert result.enabled is False
        assert result.reason_disabled == "twin_warming_up"

    def test_ff3_disturbance_context_reports_twin_unavailable(self):
        """FF3 disturbance diagnostics must distinguish an unavailable twin."""
        context = build_ff3_disturbance_context(
            twin_diag={"status": "zero_dt"},
            measured_slope_h=None,
            trajectory_active=False,
            trajectory_source="none",
        )
        assert context.disturbance_active is False
        assert context.reason == "twin_unavailable"

    def test_ff3_uses_nine_candidates_in_near_band(self):
        """FF3 must evaluate the full candidate set when near-band is active."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
        )
        candidate_u = [item["u"] for item in result.candidate_scores]
        assert len(candidate_u) == 9
        assert candidate_u == pytest.approx([0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6])
        assert result.horizon_cycles == 2

    def test_ff3_uses_two_cycle_horizon_when_deadtime_exceeds_cycle(self):
        """FF3 must keep the configured multi-cycle horizon in near-band."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=900.0,
            deadtime_cool_s=0.0,
        )
        assert result.horizon_cycles == 4
        assert len(result.candidate_scores) == 9

    def test_ff3_near_band_applies_gain(self):
        """Near-band FF3 output must be scaled by the near-band gain."""
        twin = self._make_initialized_twin()
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.EXCITED_STABLE,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.4,
            current_temp=19.0,
            setpoint=30.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.02,
            b=0.002,
            deadtime_heat_s=900.0,
            deadtime_cool_s=0.0,
        )
        assert abs(result.u_ff3_raw) <= FF3_MAX_AUTHORITY + 1e-9
        assert result.u_ff3_applied == pytest.approx(result.u_ff3_raw * FF3_NEARBAND_GAIN)
        assert abs(result.u_ff3_applied) <= (FF3_MAX_AUTHORITY * FF3_NEARBAND_GAIN) + 1e-9

    def test_ff3_deadtime_aware_horizon_can_select_candidate(self):
        twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
        twin.reset(tin_init=20.7, text_init=5.0, u_init=0.6)
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.NEAR_BAND,
            in_deadband=False,
            in_near_band=True,
            disturbance_context_active=True,
            current_temp=20.7,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.08,
            b=0.004,
            u_base=0.6,
            deadtime_heat_s=1800.0,
            deadtime_cool_s=0.0,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
        )

        assert result.horizon_cycles == 5
        assert result.deadtime_cycles == 3
        assert result.action_sensitivity > 0.0
        assert len(result.candidate_scores) > 0
        assert result.reason_disabled in (
            "none",
            "score_not_better",
            "authority_tapered_to_zero",
        )

    def test_ff3_reports_no_candidate_effect_when_horizon_cannot_see_action(self):
        twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
        twin.reset(tin_init=20.7, text_init=5.0, u_init=0.6)
        base_prediction = predict_ff3_open_loop(
            twin=twin,
            current_temp=20.7,
            ext_temp=5.0,
            a=0.08,
            b=0.004,
            u_first_cycle=0.6,
            u_base=0.6,
            cycle_min=10.0,
            deadtime_heat_s=1800.0,
            horizon_cycles=3,
        )
        candidate_prediction = predict_ff3_open_loop(
            twin=twin,
            current_temp=20.7,
            ext_temp=5.0,
            a=0.08,
            b=0.004,
            u_first_cycle=0.8,
            u_base=0.6,
            cycle_min=10.0,
            deadtime_heat_s=1800.0,
            horizon_cycles=3,
        )

        assert compute_ff3_action_sensitivity([base_prediction, candidate_prediction]) == 0.0

    def test_ff3_selects_closest_candidate_on_equal_cost(self):
        twin = self._make_initialized_twin()
        u_base = 0.4
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.NEAR_BAND,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=u_base,
            current_temp=19.0,
            setpoint=19.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.0,
            b=0.002,
            deadtime_heat_s=0.0,
            deadtime_cool_s=0.0,
        )

        assert result.selected_candidate == pytest.approx(u_base)
        assert result.enabled is False

    def test_ff3_authority_factor_limits_output(self):
        twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
        twin.reset(tin_init=20.7, text_init=5.0, u_init=0.6)
        result = compute_ff3(
            enabled=True,
            twin=twin,
            twin_reliable=True,
            twin_initialized=True,
            tau_reliable=True,
            ext_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            regime=GovernanceRegime.NEAR_BAND,
            in_deadband=False,
            in_near_band=True,
            is_calibrating=False,
            power_shedding=False,
            setpoint_changed=False,
            startup_first_run=False,
            last_sat="NO_SAT",
            u_base=0.6,
            current_temp=20.7,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
            cycle_min=10.0,
            a=0.08,
            b=0.004,
            deadtime_heat_s=1800.0,
            deadtime_cool_s=0.0,
            authority_factor=0.5,
        )

        assert abs(result.u_ff3_raw) <= FF3_MAX_AUTHORITY * 0.5 + 1e-9
        assert abs(result.u_ff3_applied) <= FF3_MAX_AUTHORITY * 0.5 + 1e-9

    def test_ff3_authority_tapers_near_deadband(self):
        """Near the deadband boundary, FF3 authority must taper continuously."""
        scale = _compute_near_band_ff3_scale(
            current_temp=20.94,
            setpoint=21.0,
            deadband_c=0.05,
            near_band_below_deg=0.4,
            near_band_above_deg=0.3,
        )

        assert scale == pytest.approx((0.06 - 0.05) / (0.4 - 0.05))


# ================================================================
# Invariant: 0 <= u_ff_eff <= 1
# ================================================================


class TestInvariants:
    """u_ff_eff must always be in [0, 1]."""

    @pytest.mark.parametrize("error", [-2.0, -0.5, -0.3, 0.0, 0.05, 0.5, 2.0])
    @pytest.mark.parametrize("k_ff", [0.0, 0.05, 0.20, 0.50, 2.0])
    def test_u_ff_eff_in_range(self, error, k_ff):
        result = _call(error=error, k_ff=k_ff)
        assert 0.0 <= result.u_ff_eff <= 1.0

    @pytest.mark.parametrize("error", [-2.0, -0.5, -0.3, 0.0, 0.05, 0.5, 2.0])
    def test_ff_passes_through_for_all_errors(self, error):
        """FF passes through for all error values."""
        if error < -0.3:
            result = _call(error=error, near_band_above_deg=0.3)
            assert result.u_ff_eff > 0.0
