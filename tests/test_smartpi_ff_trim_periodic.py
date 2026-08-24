"""Tests for periodic-equilibrium FF trim observation."""

import pytest

from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    CausalFFTrimObserver,
    ControlOwnershipSnapshot,
    FFTrim,
    FFTrimThermalSample,
    FFTrimWindowProposal,
)
from custom_components.vtherm_smartpi.smartpi.ff_trim_periodic import (
    PeriodicFFTrimObserver,
)


def _sample(
    timestamp: float,
    temperature: float,
    *,
    saturation_state: str = "NO_SAT",
) -> FFTrimThermalSample:
    """Build one periodic near-equilibrium temperature sample."""
    return FFTrimThermalSample(
        observed_monotonic=timestamp,
        measurement_id=str(timestamp),
        temperature=temperature,
        target=20.0,
        ff1=0.08,
        regime=(
            GovernanceRegime.SATURATED
            if saturation_state != "NO_SAT"
            else GovernanceRegime.HOLD
        ),
        i_mode="I:HOLD(deadband)",
        u_pi=-0.04,
        hvac_mode="heat",
        saturated=saturation_state != "NO_SAT",
        trajectory_active=False,
        ff3_active=False,
        setpoint_changed=False,
        outside_temperature_available=True,
        model_reliable=True,
        outside_temperature=10.0,
        trim_frozen_reason=(
            "saturated" if saturation_state != "NO_SAT" else None
        ),
        saturation_state=saturation_state,
    )


def _ownership(
    committed: float,
    *,
    saturated_low: bool,
) -> ControlOwnershipSnapshot:
    """Build one ownership snapshot matching the committed linear power."""
    return ControlOwnershipSnapshot(
        u_ff1=0.08,
        trim_stored=0.0,
        u_ff_visible=0.08,
        u_ff3=0.0,
        u_p=committed - 0.08,
        u_i=0.0,
        ki=0.001,
        gain_generation=1,
        u_cmd=committed,
        u_limited=committed,
        linear_committed_power=committed,
        regime=(
            GovernanceRegime.SATURATED
            if saturated_low
            else GovernanceRegime.HOLD
        ),
        i_mode="I:HOLD(deadband)",
        quality="valve_segmented_linear",
        constraint_flags=("saturated_low",) if saturated_low else (),
    )


def _record_shared_power_cycle(observer: CausalFFTrimObserver) -> None:
    """Record one 0-to-8 percent valve cycle in the shared causal trace."""
    observer.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.0,
        ownership=_ownership(0.0, saturated_low=True),
    )
    observer.update_applied_power(
        now_monotonic=900.0,
        linear_power=0.08,
        ownership=_ownership(0.08, saturated_low=False),
    )
    observer.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )


def _record_periodic_temperatures(observer: PeriodicFFTrimObserver) -> None:
    """Record one quantized, phase-closed thirty-minute oscillation."""
    for timestamp, temperature in zip(
        (120.0, 420.0, 720.0, 1020.0, 1320.0, 1620.0, 1920.0),
        (20.00, 20.02, 20.04, 20.02, 20.00, 19.98, 20.00),
    ):
        observer.record_thermal_sample(
            _sample(timestamp, temperature),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )


def test_periodic_window_uses_shared_trace_and_never_transfers_integral() -> None:
    """A complete oscillation must reuse causal power without bumpless transfer."""
    causal = CausalFFTrimObserver(cycle_min=2.0)
    periodic = PeriodicFFTrimObserver(cycle_min=2.0)
    _record_shared_power_cycle(causal)
    _record_periodic_temperatures(periodic)

    window = periodic.try_close_window(
        earliest_power_start=causal.earliest_power_start,
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert window is not None
    assert periodic.target_duration_s == pytest.approx(720.0)
    assert window.duration_s == pytest.approx(1800.0)
    assert window.amplitude_c == pytest.approx(0.06)

    result = causal.evaluate_periodic_window(
        window.samples,
        a=0.1,
        b=0.005,
        deadtime_s=window.deadtime_s,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.observation_mode == "periodic"
    assert result.mean_causal_power == pytest.approx(0.04)
    assert result.transfer_eligible is False
    assert result.transfer_reason == "periodic_equilibrium"
    assert result.decomposed_correction is None
    assert causal.earliest_power_start == pytest.approx(0.0)


def test_periodic_window_rejects_opposite_phase_half_cycle() -> None:
    """Returning to the start temperature in the opposite direction is incomplete."""
    periodic = PeriodicFFTrimObserver(cycle_min=2.0)
    for timestamp, temperature in zip(
        (120.0, 420.0, 720.0, 1020.0, 1320.0),
        (20.00, 20.02, 20.04, 20.02, 20.00),
    ):
        periodic.record_thermal_sample(
            _sample(timestamp, temperature),
            deadtime_s=120.0,
            deadtime_reliable=True,
        )

    window = periodic.try_close_window(
        earliest_power_start=0.0,
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert window is None
    assert periodic.state == "waiting_phase"
    assert periodic.last_reject_reason == "cycle_not_closed"


def test_periodic_window_accepts_sat_low_but_rejects_sat_high() -> None:
    """Zero-power cycling is observable while upper saturation is unsafe."""
    periodic = PeriodicFFTrimObserver(cycle_min=2.0)
    periodic.record_thermal_sample(
        _sample(120.0, 20.0, saturation_state="SAT_LO"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert periodic.diagnostics["measurement_count"] == 1

    periodic.record_thermal_sample(
        _sample(420.0, 20.01, saturation_state="SAT_HI"),
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert periodic.diagnostics["measurement_count"] == 0
    assert periodic.last_reject_reason == "saturated_high"


def test_stationary_rejection_does_not_clear_periodic_persistence() -> None:
    """Each observation method must retain only its own evidence series."""
    trim = FFTrim()
    periodic_proposal = FFTrimWindowProposal(
        correction=-0.02,
        mean_ff1=0.08,
        physical_power_deficit=None,
        integral_bias=None,
        transfer_eligible=False,
        transfer_reason="periodic_equilibrium",
        observation_mode="periodic",
    )

    first = trim.collect_persistent(periodic_proposal)
    trim.clear_pending("stationary")
    second = trim.collect_persistent(periodic_proposal)

    assert first.pending_count == 1
    assert second.pending_count == 2
    assert second.observation_mode == "periodic"
