"""Tests for the causally aligned FF trim observer."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    AppliedPowerSegment,
    CausalFFTrimObserver,
    ControlOwnershipSnapshot,
    FFTrimThermalSample,
)


def _sample(
    observed_monotonic: float,
    temperature: float,
    *,
    measurement_id: str | None = None,
    regime: GovernanceRegime = GovernanceRegime.DEAD_BAND,
    i_mode: str = "I:FREEZE(deadband)",
    u_pi: float = 0.1,
    hvac_mode: str = "heat",
    ff1: float = 0.5,
    outside_temperature: float | None = 10.0,
) -> FFTrimThermalSample:
    """Build one admissible mono-mode thermal measurement."""
    return FFTrimThermalSample(
        observed_monotonic=observed_monotonic,
        measurement_id=measurement_id or str(observed_monotonic),
        temperature=temperature,
        target=20.0,
        ff1=ff1,
        regime=regime,
        i_mode=i_mode,
        u_pi=u_pi,
        hvac_mode=hvac_mode,
        saturated=False,
        trajectory_active=False,
        ff3_active=False,
        setpoint_changed=False,
        outside_temperature_available=True,
        model_reliable=True,
        outside_temperature=outside_temperature,
    )


def _record_samples(
    observer: CausalFFTrimObserver,
    temperatures: tuple[float, ...],
    *,
    u_pi_values: tuple[float, ...] | None = None,
    regime: GovernanceRegime = GovernanceRegime.DEAD_BAND,
    hvac_mode: str = "heat",
    ff1: float = 0.5,
    outside_temperature: float | None = 10.0,
) -> None:
    """Record equally spaced measurements over a thirty-minute window."""
    timestamps = (120.0, 720.0, 1320.0, 1920.0)
    for index, (timestamp, temperature) in enumerate(
        zip(timestamps, temperatures)
    ):
        observer.record_thermal_sample(
            _sample(
                timestamp,
                temperature,
                regime=regime,
                i_mode=(
                    "I:RUN"
                    if regime == GovernanceRegime.NEAR_BAND
                    else "I:FREEZE(deadband)"
                ),
                u_pi=u_pi_values[index] if u_pi_values else 0.1,
                hvac_mode=hvac_mode,
                ff1=ff1,
                outside_temperature=outside_temperature,
            ),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )


def _ownership(
    *,
    ff1: float = 0.4,
    u_p: float = 0.0,
    u_i: float = 0.1,
    committed: float = 0.5,
    hvac_regime: GovernanceRegime = GovernanceRegime.DEAD_BAND,
    i_mode: str = "I:FREEZE(deadband)",
) -> ControlOwnershipSnapshot:
    """Build one internally consistent committed-command ownership snapshot."""
    visible_ff = ff1
    command = visible_ff + u_p + u_i
    return ControlOwnershipSnapshot(
        u_ff1=ff1,
        trim_stored=0.0,
        u_ff_visible=visible_ff,
        u_ff3=0.0,
        u_p=u_p,
        u_i=u_i,
        ki=0.02,
        gain_generation=1,
        u_cmd=command,
        u_limited=command,
        linear_committed_power=committed,
        regime=hvac_regime,
        i_mode=i_mode,
        quality="switch_cycle_equivalent",
    )


def _record_owned_switch_window(
    observer: CausalFFTrimObserver,
    ownership: ControlOwnershipSnapshot,
) -> None:
    """Record one complete equivalent switch-power and ownership interval."""
    observer.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=ownership.linear_committed_power,
        ownership=ownership,
    )
    observer.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=ownership.linear_committed_power,
        use_valve_trace=False,
    )


def test_causal_observer_returns_zero_for_correct_stable_ff() -> None:
    """A stable room with the correct FF must not create a trim correction."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(observer, (20.0, 20.0, 20.0, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_causal_power == pytest.approx(0.5)
    assert result.observed_hold_power == pytest.approx(0.5)
    assert result.correction == pytest.approx(0.0)


def test_causal_observer_uses_model_to_reconstruct_missing_hold_power() -> None:
    """Cooling drift under heat must produce the model-derived positive bias."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(observer, (20.0, 19.9667, 19.9333, 19.9))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_slope_h == pytest.approx(-0.2)
    assert result.observed_hold_power == pytest.approx(0.5358333, abs=1e-5)
    assert result.correction == pytest.approx(0.0358333, abs=1e-5)


def test_causal_observer_returns_zero_for_correct_stable_cool_ff() -> None:
    """The same physical observer equation is valid with a negative COOL A."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        hvac_mode="cool",
    )

    result = observer.try_complete_window(
        a=-0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.observed_hold_power == pytest.approx(0.5)
    assert result.correction == pytest.approx(0.0)


def test_causal_observer_cool_warming_drift_requests_more_power() -> None:
    """A warming room under COOL must reconstruct a positive cooling deficit."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(
        observer,
        (20.0, 20.0333, 20.0667, 20.1),
        hvac_mode="cool",
    )

    result = observer.try_complete_window(
        a=-0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_slope_h == pytest.approx(0.2)
    assert result.correction is not None
    assert result.correction > 0.0


def test_causal_observer_integrates_power_on_the_shifted_window() -> None:
    """Power before the measurements must be weighted after dead-time shift."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.start_applied_cycle(now_monotonic=0.0, linear_power=0.2)
    observer.update_applied_power(now_monotonic=900.0, linear_power=0.8)
    observer.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )
    _record_samples(observer, (20.0, 20.0, 20.0, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_causal_power == pytest.approx(0.5)
    assert result.power_coverage_ratio == pytest.approx(1.0)


def test_causal_observer_uses_realized_switch_cycle_power() -> None:
    """Switch timelines must use realized e_eff instead of requested duty."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.start_applied_cycle(now_monotonic=0.0, linear_power=0.8)
    observer.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=0.4,
        use_valve_trace=False,
    )
    _record_samples(observer, (20.0, 20.0, 20.0, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_causal_power == pytest.approx(0.4)


def test_causal_observer_deduplicates_temperature_measurements() -> None:
    """A heartbeat cannot count the same VT sensor measurement twice."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))

    observer.record_thermal_sample(
        _sample(120.0, 20.0, measurement_id="sensor-1"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )
    observer.record_thermal_sample(
        _sample(720.0, 20.1, measurement_id="sensor-1"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert observer.diagnostics["measurement_count"] == 1


def test_generic_recalculation_without_measurement_id_keeps_window() -> None:
    """A VT recalculation without sensor metadata must preserve observations."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI",
    )
    algo._fftrim_observer.record_thermal_sample(
        _sample(120.0, 20.0, measurement_id="sensor-1"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )
    diagnostics_before = dict(algo._fftrim_observer.diagnostics)

    algo._record_fftrim_thermal_measurement(
        now_monotonic=180.0,
        measurement_id=None,
        current_temp=20.0,
        target_temp=20.0,
        ext_current_temp=10.0,
        hvac_mode=VThermHvacMode_HEAT,
        setpoint_changed=False,
    )

    assert algo._fftrim_observer.diagnostics == diagnostics_before


def test_causal_observer_cancels_symmetric_cycle_limit() -> None:
    """Opposite half-cycles must not generate opposite trim corrections."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(observer, (20.0, 20.1, 19.9, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_temperature == pytest.approx(20.0)
    assert result.mean_slope_h == pytest.approx(0.0)
    assert result.correction == pytest.approx(0.0)


def test_causal_observer_waits_for_complete_power_coverage() -> None:
    """A thermal window with missing causal power must remain unapplied."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1500.0, 0.5))
    _record_samples(observer, (20.0, 20.0, 20.0, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is None
    assert observer.state == "waiting_deadtime"
    assert observer.last_reject_reason == "causal_power_not_covered"


def test_causal_observer_rejects_unstable_near_band_pi() -> None:
    """Near-band PI motion must not be mistaken for a stable FF bias."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        u_pi_values=(0.10, 0.11, 0.12, 0.13),
        regime=GovernanceRegime.NEAR_BAND,
    )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is False
    assert result.reason == "pi_unstable"


def test_causal_observer_invalidation_clears_window_and_imposes_washout() -> None:
    """A partial cycle must discard samples and delay the next observation."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_thermal_sample(
        _sample(120.0, 20.0),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    observer.invalidate(
        "partial_cycle",
        now_monotonic=300.0,
        washout_s=120.0,
    )
    observer.record_thermal_sample(
        _sample(360.0, 20.0),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert observer.state == "waiting_deadtime"
    assert observer.diagnostics["measurement_count"] == 0


def test_causal_observer_separates_integral_bias_from_physical_deficit() -> None:
    """A stable I-owned hold power must be transferable without a net step."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(observer, _ownership())
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        ff1=0.4,
    )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.transfer_eligible is True
    assert result.transfer_reason == "quasi_equilibrium"
    assert result.mean_p_power == pytest.approx(0.0)
    assert result.mean_i_power == pytest.approx(0.1)
    assert result.physical_power_deficit == pytest.approx(0.0)
    assert result.decomposed_correction == pytest.approx(0.1)


def test_causal_observer_uses_ff1_from_aligned_ownership_window() -> None:
    """Delayed FF1 ownership must replace the later thermal-sample value."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(observer, _ownership(ff1=0.4))
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        ff1=0.6,
    )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.mean_ff1 == pytest.approx(0.4)
    assert result.target_trim == pytest.approx(0.1)
    assert result.correction == pytest.approx(0.1)


def test_causal_observer_never_attributes_proportional_power_to_integral() -> None:
    """A visible P contribution must make ownership transfer ineligible."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(
        observer,
        _ownership(u_p=0.02, u_i=0.08),
    )
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        ff1=0.4,
    )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.mean_p_power == pytest.approx(0.02)
    assert result.mean_i_power == pytest.approx(0.08)
    assert result.transfer_eligible is False
    assert result.transfer_reason == "transfer_p_active"


def test_causal_observer_signed_cool_uses_same_power_ownership() -> None:
    """COOL changes the thermal sign, not the positive actuator ownership."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(observer, _ownership())
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        hvac_mode="cool",
        ff1=0.4,
        outside_temperature=30.0,
    )

    result = observer.try_complete_window(
        a=-0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.transfer_eligible is True
    assert result.mean_i_power == pytest.approx(0.1)
    assert result.physical_power_deficit == pytest.approx(0.0)
