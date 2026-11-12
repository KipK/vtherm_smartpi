"""Tests for the causally aligned FF trim observer."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.causal_power_trace import (
    AppliedPowerSegment,
    ControlOwnershipSnapshot,
)
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    CausalFFTrimObserver,
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
    i_hold_source: str | None = None,
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
        i_hold_source=i_hold_source,
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
    ki: float = 0.02,
    committed: float = 0.5,
    hvac_regime: GovernanceRegime = GovernanceRegime.DEAD_BAND,
    i_mode: str = "I:FREEZE(deadband)",
    i_hold_source: str | None = None,
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
        ki=ki,
        gain_generation=1,
        u_cmd=command,
        u_limited=command,
        linear_committed_power=committed,
        regime=hvac_regime,
        i_mode=i_mode,
        quality="switch_cycle_equivalent",
        i_hold_source=i_hold_source,
    )


def _record_owned_switch_window(
    observer: CausalFFTrimObserver,
    ownership: ControlOwnershipSnapshot,
) -> None:
    """Record one complete equivalent switch-power and ownership interval."""
    observer.physical_trace.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=ownership.linear_committed_power,
        ownership=ownership,
    )
    observer.physical_trace.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=ownership.linear_committed_power,
        use_valve_trace=False,
    )


def test_causal_observer_returns_zero_for_correct_stable_ff() -> None:
    """A stable room with the correct FF must not create a trim correction."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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
    assert observer.last_admissible_result is result

    observer.invalidate("later_stationary_rejection")

    assert observer.last_admissible_result is result


def test_causal_observer_uses_model_to_reconstruct_missing_hold_power() -> None:
    """Cooling drift under heat must produce the model-derived positive bias."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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
    observer.physical_trace.start_applied_cycle(now_monotonic=0.0, linear_power=0.2)
    observer.physical_trace.update_applied_power(now_monotonic=300.0, linear_power=0.8)
    observer.physical_trace.complete_applied_cycle(
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
    assert result.mean_causal_power == pytest.approx(0.7)
    assert result.power_coverage_ratio == pytest.approx(1.0)


def test_causal_observer_time_weights_irregular_temperature_samples() -> None:
    """Temperature density must not turn the window mean into a sample mean."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
    for timestamp, temperature in (
        (120.0, 20.0),
        (240.0, 20.2),
        (1920.0, 20.2),
    ):
        observer.record_thermal_sample(
            _sample(timestamp, temperature),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_temperature == pytest.approx(20.1933333333)
    assert result.mean_temperature != pytest.approx((20.0 + 20.2 + 20.2) / 3.0)


def test_causal_observer_uses_realized_switch_cycle_power() -> None:
    """Switch timelines must use realized e_eff instead of requested duty."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.start_applied_cycle(now_monotonic=0.0, linear_power=0.8)
    observer.physical_trace.complete_applied_cycle(
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

    algo.fftrim_coord.record_thermal_measurement(
        now_monotonic=180.0,
        measurement_id=None,
        current_temp=20.0,
        target_temp=20.0,
        ext_current_temp=10.0,
        hvac_mode=VThermHvacMode_HEAT,
        setpoint_changed=False,
    )

    assert algo._fftrim_observer.diagnostics == diagnostics_before


def test_repeated_measurement_identity_does_not_reapply_trim_gates() -> None:
    """A heartbeat with the same sensor identity cannot reject a live window."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestSmartPI",
    )
    acquired = algo._thermal_measurements.observe(
        now_monotonic=120.0,
        measurement_id="sensor-1",
        indoor_temperature=20.0,
        outside_temperature=10.0,
    )
    assert acquired is not None
    algo._fftrim_observer.record_thermal_sample(
        _sample(120.0, 20.0, measurement_id="sensor-1"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )
    diagnostics_before = dict(algo._fftrim_observer.diagnostics)

    algo.fftrim_coord.record_thermal_measurement(
        now_monotonic=180.0,
        measurement_id="sensor-1",
        current_temp=20.1,
        target_temp=21.0,
        ext_current_temp=None,
        hvac_mode=VThermHvacMode_HEAT,
        setpoint_changed=True,
    )

    assert algo._fftrim_observer.diagnostics == diagnostics_before


def test_causal_observer_cancels_symmetric_cycle_limit() -> None:
    """Opposite half-cycles must not generate opposite trim corrections."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1500.0, 0.5))
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
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 1800.0, 0.5))
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


def test_causal_observer_time_weights_asynchronous_ownership() -> None:
    """Ownership components must follow their unequal committed durations."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.2,
        ownership=_ownership(
            ff1=0.1,
            u_p=0.02,
            u_i=0.08,
            ki=0.01,
            committed=0.2,
        ),
    )
    observer.physical_trace.update_applied_power(
        now_monotonic=300.0,
        linear_power=0.8,
        ownership=_ownership(
            ff1=0.4,
            u_p=0.1,
            u_i=0.3,
            ki=0.03,
            committed=0.8,
        ),
    )
    observer.physical_trace.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )
    _record_samples(
        observer,
        (20.0, 20.0, 20.0, 20.0),
        ff1=0.9,
    )

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.mean_causal_power == pytest.approx(0.7)
    assert result.mean_ff1 == pytest.approx(0.35)
    assert result.mean_p_power == pytest.approx(0.0866666667)
    assert result.mean_i_power == pytest.approx(0.2633333333)
    assert result.mean_delivery_residual == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("temperatures", "expected_slope_h"),
    (
        ((20.1, 20.0, 20.0, 20.0), -0.2),
        ((20.0, 20.1, 20.0, 20.0), 0.0),
    ),
)
def test_causal_observer_distinguishes_endpoint_from_interior_noise(
    temperatures: tuple[float, ...],
    expected_slope_h: float,
) -> None:
    """Only endpoint noise changes net slope; both patterns block transfer."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(observer, _ownership())
    _record_samples(
        observer,
        temperatures,
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
    assert result.admissible is True
    assert result.mean_slope_h == pytest.approx(expected_slope_h)
    assert result.transfer_eligible is False
    assert result.transfer_reason == "transfer_temperature_noise"


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


@pytest.mark.parametrize("unobserved", [False, True])
def test_causal_observer_rejects_a_window_with_unobserved_power(unobserved) -> None:
    """Power carried over a missing valve reading cannot feed the stationary trim."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    trace = observer.physical_trace
    trace.start_applied_cycle(now_monotonic=0.0, linear_power=0.5)
    if unobserved:
        trace.mark_unobserved_power(600.0, "missing_valve_position")
    trace.complete_applied_cycle(
        now_monotonic=1800.0, realized_linear_power=None, use_valve_trace=True
    )
    _record_samples(observer, (20.0, 20.1, 19.9, 20.0))

    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is not unobserved
    if unobserved:
        assert result.reason == "causal_power_not_covered"


def test_causal_observer_keeps_accepting_legacy_gap_fill() -> None:
    """A short scheduler gap filled with the previous power stays usable."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.physical_trace.record_applied_power(AppliedPowerSegment(0.0, 900.0, 0.5))
    observer.physical_trace.record_applied_power(AppliedPowerSegment(903.0, 1800.0, 0.5))
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


def _shell_hold_window(source: str, temperature: float = 20.0):
    """Complete one owned window whose integral is held with the given source."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(
        observer,
        _ownership(i_mode="I:HOLD", i_hold_source=source),
    )
    for timestamp in (120.0, 720.0, 1320.0, 1920.0):
        observer.record_thermal_sample(
            _sample(
                timestamp,
                temperature,
                i_mode="I:HOLD",
                ff1=0.4,
                i_hold_source=source,
            ),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )
    result = observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )
    return observer, result


def test_causal_observer_accepts_deadband_shell_hold_and_transfers() -> None:
    """The hysteresis-shell hold does not move the integral, as the freeze."""
    _, result = _shell_hold_window("deadband_hysteresis_shell")

    assert result is not None
    assert result.admissible is True
    assert result.transfer_eligible is True
    assert result.mean_i_power == pytest.approx(0.1)
    assert result.physical_power_deficit == pytest.approx(0.0)


@pytest.mark.parametrize(
    "source", ("deadtime", "governance_sensor_invalid", "vt_safety")
)
def test_causal_observer_rejects_other_holds(source: str) -> None:
    observer = CausalFFTrimObserver(cycle_min=5.0)
    observer.record_thermal_sample(
        _sample(
            120.0,
            20.0,
            i_mode="I:HOLD",
            ff1=0.4,
            i_hold_source=source,
        ),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert observer.last_reject_reason == "pi_mode_I:HOLD"


def _hold_window(
    source: str | None,
    temperatures: tuple[float, ...],
    *,
    i_mode: str = "I:HOLD",
):
    """Complete one owned window with the given integral mode over the given readings."""
    observer = CausalFFTrimObserver(cycle_min=5.0)
    _record_owned_switch_window(
        observer,
        _ownership(i_mode=i_mode, i_hold_source=source),
    )
    for timestamp, temperature in zip((120.0, 720.0, 1320.0, 1920.0), temperatures):
        observer.record_thermal_sample(
            _sample(
                timestamp,
                temperature,
                i_mode=i_mode,
                ff1=0.4,
                i_hold_source=source,
            ),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )
    return observer.try_complete_window(
        a=0.1,
        b=0.005,
        deadtime_s=120.0,
        deadtime_reliable=True,
        current_trim=0.0,
    )


@pytest.mark.parametrize("temperature", (20.06, 19.94))
def test_shell_hold_offset_transfers_integral_share_only(temperature: float) -> None:
    """Off the setpoint in the shell, the integral moves and the net command does not."""
    result = _hold_window("deadband_hysteresis_shell", (temperature,) * 4)

    assert result is not None
    assert result.admissible is True
    assert result.transfer_eligible is True
    assert result.transfer_reason == "quasi_equilibrium"
    # The offset deficit is measured but left to the loop
    assert abs(result.physical_power_deficit) > 1e-3
    assert result.transfer_power_deficit == 0.0
    assert result.decomposed_correction == pytest.approx(result.mean_i_power)


def test_deadband_freeze_offset_keeps_its_error_tolerance() -> None:
    """The deadband freeze is unchanged: an offset window is not transferable."""
    result = _hold_window(None, (20.06,) * 4, i_mode="I:FREEZE(deadband)")

    assert result is not None
    assert result.admissible is True
    assert result.transfer_eligible is False
    assert result.transfer_reason == "transfer_mean_error"


def test_deadband_freeze_transfers_the_measured_deficit() -> None:
    result = _hold_window(None, (20.0,) * 4, i_mode="I:FREEZE(deadband)")

    assert result is not None
    assert result.transfer_eligible is True
    assert result.transfer_power_deficit == pytest.approx(result.physical_power_deficit)


def test_shell_hold_keeps_the_per_sample_error_bound() -> None:
    result = _hold_window("deadband_hysteresis_shell", (20.12,) * 4)

    assert result is not None
    assert result.admissible is True
    assert result.transfer_eligible is False
    assert result.transfer_reason == "transfer_sample_error"


def test_shell_hold_keeps_the_stationarity_requirement() -> None:
    result = _hold_window(
        "deadband_hysteresis_shell", (20.05, 20.055, 20.06, 20.065)
    )

    assert result is not None
    assert result.transfer_eligible is False
    assert result.transfer_reason == "transfer_slope"
