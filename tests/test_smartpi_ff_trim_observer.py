"""Tests for the causally aligned FF trim observer."""

import pytest

from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    AppliedPowerSegment,
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
) -> FFTrimThermalSample:
    """Build one admissible heating measurement."""
    return FFTrimThermalSample(
        observed_monotonic=observed_monotonic,
        measurement_id=measurement_id or str(observed_monotonic),
        temperature=temperature,
        target=20.0,
        ff1=0.5,
        regime=regime,
        i_mode=i_mode,
        u_pi=u_pi,
        hvac_mode="heat",
        saturated=False,
        trajectory_active=False,
        ff3_active=False,
        setpoint_changed=False,
        outside_temperature_available=True,
        model_reliable=True,
    )


def _record_samples(
    observer: CausalFFTrimObserver,
    temperatures: tuple[float, ...],
    *,
    u_pi_values: tuple[float, ...] | None = None,
    regime: GovernanceRegime = GovernanceRegime.DEAD_BAND,
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
            ),
            deadtime_s=120.0,
            deadtime_reliable=True,
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
