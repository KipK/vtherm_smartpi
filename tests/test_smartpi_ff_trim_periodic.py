"""Tests for periodic-equilibrium FF trim observation."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.diagnostics import (
    build_published_diagnostics,
)
from custom_components.vtherm_smartpi.smartpi.ff_trim import (
    CausalFFTrimObserver,
    CausalFFTrimResult,
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
    u_cmd: float | None = None,
    u_limited: float | None = None,
    constraint_flags: tuple[str, ...] | None = None,
) -> ControlOwnershipSnapshot:
    """Build one ownership snapshot matching the committed linear power."""
    flags = constraint_flags
    if flags is None:
        flags = ("saturated_low",) if saturated_low else ()
    return ControlOwnershipSnapshot(
        u_ff1=0.08,
        trim_stored=0.0,
        u_ff_visible=0.08,
        u_ff3=0.0,
        u_p=committed - 0.08,
        u_i=0.0,
        ki=0.001,
        gain_generation=1,
        u_cmd=committed if u_cmd is None else u_cmd,
        u_limited=committed if u_limited is None else u_limited,
        linear_committed_power=committed,
        regime=(
            GovernanceRegime.SATURATED
            if saturated_low
            else GovernanceRegime.HOLD
        ),
        i_mode="I:HOLD(deadband)",
        quality="valve_segmented_linear",
        constraint_flags=flags,
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


def test_periodic_window_accepts_small_scheduler_quantization_limits() -> None:
    """Small command and PWM quantization deltas remain observable."""
    causal = CausalFFTrimObserver(cycle_min=2.0)
    periodic = PeriodicFFTrimObserver(cycle_min=2.0)
    causal.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.0,
        ownership=_ownership(0.0, saturated_low=True),
    )
    causal.update_applied_power(
        now_monotonic=900.0,
        linear_power=0.08,
        ownership=_ownership(
            0.08,
            saturated_low=False,
            u_cmd=0.12,
            u_limited=0.11,
            constraint_flags=("output_limited", "timing_output"),
        ),
    )
    causal.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )
    _record_periodic_temperatures(periodic)

    window = periodic.try_close_window(
        earliest_power_start=causal.earliest_power_start,
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert window is not None
    result = causal.evaluate_periodic_window(
        window.samples,
        a=0.1,
        b=0.005,
        deadtime_s=window.deadtime_s,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is True
    assert result.reason == "periodic_window_ready"


@pytest.mark.parametrize(
    ("u_cmd", "u_limited", "committed", "flag", "reason"),
    (
        (0.14, 0.09, 0.08, "output_limited", "periodic_constraint_output_limited"),
        (0.13, 0.13, 0.08, "timing_output", "periodic_constraint_timing_output"),
    ),
)
def test_periodic_window_rejects_hard_scheduler_limits(
    u_cmd: float,
    u_limited: float,
    committed: float,
    flag: str,
    reason: str,
) -> None:
    """Large command or PWM deltas are still unsafe for periodic trim."""
    causal = CausalFFTrimObserver(cycle_min=2.0)
    periodic = PeriodicFFTrimObserver(cycle_min=2.0)
    causal.start_applied_cycle(
        now_monotonic=0.0,
        linear_power=0.0,
        ownership=_ownership(0.0, saturated_low=True),
    )
    causal.update_applied_power(
        now_monotonic=900.0,
        linear_power=committed,
        ownership=_ownership(
            committed,
            saturated_low=False,
            u_cmd=u_cmd,
            u_limited=u_limited,
            constraint_flags=(flag,),
        ),
    )
    causal.complete_applied_cycle(
        now_monotonic=1800.0,
        realized_linear_power=None,
        use_valve_trace=True,
    )
    _record_periodic_temperatures(periodic)

    window = periodic.try_close_window(
        earliest_power_start=causal.earliest_power_start,
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    assert window is not None
    result = causal.evaluate_periodic_window(
        window.samples,
        a=0.1,
        b=0.005,
        deadtime_s=window.deadtime_s,
        current_trim=0.0,
    )

    assert result is not None
    assert result.admissible is False
    assert result.reason == reason


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


def test_admissible_result_survives_stationary_collection_and_rejection() -> None:
    """Live stationary state must not replace the last admissible result."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=2.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="FF trim diagnostics",
    )
    periodic_result = CausalFFTrimResult(
        admissible=True,
        reason="periodic_window_ready",
        correction=-0.01,
        target_trim=-0.01,
        mean_causal_power=0.04,
        mean_ff1=0.08,
        mean_temperature=20.0,
        mean_error=0.0,
        mean_slope_h=0.0,
        observed_hold_power=0.04,
        duration_s=1800.0,
        measurement_count=7,
        alignment_delay_s=120.0,
        power_coverage_ratio=1.0,
        transfer_eligible=False,
        transfer_reason="periodic_equilibrium",
        observation_mode="periodic",
    )
    algo._fftrim_observer.publish_external_result(periodic_result)
    algo._fftrim_periodic_observer.last_reject_reason = "cycle_not_closed"

    published = build_published_diagnostics(algo)["feedforward"]["fftrim"]

    assert published["state"] == "warming_up"
    assert published["stationary"]["window_duration_s"] == 0.0
    assert published["alignment_delay_s"] is None
    assert published["power_coverage_ratio"] == 0.0
    assert published["last_result"]["observation_mode"] == "periodic"
    assert published["last_result"]["alignment_delay_s"] == 120.0
    assert published["last_result"]["power_coverage_ratio"] == 1.0

    stationary_sample = replace(
        _sample(2100.0, 20.0),
        regime=GovernanceRegime.DEAD_BAND,
        i_mode="I:FREEZE(deadband)",
        u_pi=0.0,
    )
    algo._fftrim_observer.record_thermal_sample(
        stationary_sample,
        deadtime_s=120.0,
        deadtime_reliable=True,
    )

    collecting = build_published_diagnostics(algo)["feedforward"]["fftrim"]

    assert collecting["state"] == "collecting"
    assert collecting["stationary"]["state"] == "collecting"
    assert collecting["stationary"]["measurement_count"] == 1
    assert collecting["alignment_delay_s"] == 120.0
    assert collecting["power_coverage_ratio"] == 0.0
    assert collecting["last_result"]["admissible"] is True
    assert collecting["last_result"]["observation_mode"] == "periodic"
    assert collecting["last_result"]["correction"] == pytest.approx(-0.01)
    assert collecting["mean_causal_power"] == pytest.approx(0.04)
    assert collecting["correction"] == pytest.approx(-0.01)

    algo._fftrim_observer.invalidate("stationary_context_rejected")

    rejected = build_published_diagnostics(algo)["feedforward"]["fftrim"]

    assert rejected["state"] == "rejected"
    assert rejected["window_duration_s"] == 0.0
    assert rejected["stationary"]["window_duration_s"] == 0.0
    assert (
        rejected["stationary_last_reject_reason"]
        == "stationary_context_rejected"
    )
    assert rejected["periodic_last_reject_reason"] == "cycle_not_closed"
    assert rejected["last_result"]["admissible"] is True
    assert rejected["last_result"]["observation_mode"] == "periodic"
    assert rejected["last_result"]["measurement_count"] == 7
    assert rejected["mean_causal_power"] == pytest.approx(0.04)
    assert rejected["correction"] == pytest.approx(-0.01)

    algo._fftrim_observer.reset_runtime()

    reset = build_published_diagnostics(algo)["feedforward"]["fftrim"]

    assert reset["last_result"] is None
    assert reset["mean_causal_power"] is None
    assert reset["correction"] is None


def test_applied_periodic_transaction_survives_live_rejection_until_reset() -> None:
    """Applied deltas must remain inspectable after live fields are cleared."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=2.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="FF trim transaction diagnostics",
    )
    periodic_result = CausalFFTrimResult(
        admissible=True,
        reason="periodic_window_ready",
        correction=-0.01,
        target_trim=-0.01,
        mean_causal_power=0.04,
        mean_ff1=0.08,
        mean_temperature=20.0,
        mean_error=0.0,
        mean_slope_h=0.0,
        observed_hold_power=0.04,
        duration_s=1800.0,
        measurement_count=7,
        alignment_delay_s=120.0,
        power_coverage_ratio=1.0,
        transfer_eligible=False,
        transfer_reason="periodic_equilibrium",
        transfer_quality="valve_segmented_linear",
        observation_mode="periodic",
    )

    quiet_result = replace(
        periodic_result,
        correction=0.0,
        target_trim=0.0,
    )
    assert (
        algo._apply_fftrim_observer_result(
            quiet_result,
            count_window=False,
        )
        is False
    )
    assert algo._ff_trim.last_transaction is None

    for proposal_index in range(3):
        updated = algo._apply_fftrim_observer_result(
            periodic_result,
            count_window=False,
        )
        if proposal_index < 2:
            assert updated is False
            assert algo._ff_trim.last_transaction is None

    transaction = algo._ff_trim.last_transaction
    assert transaction is not None
    assert (
        datetime.fromisoformat(transaction.timestamp_utc).tzinfo
        == timezone.utc
    )
    assert transaction.observation_mode == "periodic"
    assert transaction.state == "causal_update_without_transfer"
    assert transaction.reason == "periodic_equilibrium"
    assert transaction.quality == "valve_segmented_linear"
    assert transaction.requested_trim_delta == pytest.approx(-0.0005)
    assert transaction.stored_trim_delta == pytest.approx(-0.0005)
    assert transaction.applied_trim_delta == pytest.approx(-0.0005)
    assert transaction.transferable_i_power == pytest.approx(0.0)
    assert transaction.requested_i_transfer == pytest.approx(0.0)
    assert transaction.applied_i_transfer == pytest.approx(0.0)
    assert transaction.net_command_delta == pytest.approx(-0.0005)
    assert algo._ff_trim.save_state() == {"u_ff_trim": pytest.approx(-0.0005)}
    saved_state = algo.save_state()
    assert "last_transaction" not in saved_state["ff_v2_trim"]

    rejected_result = replace(
        periodic_result,
        admissible=False,
        reason="cycle_not_closed",
        correction=None,
        target_trim=None,
    )
    algo._apply_fftrim_observer_result(rejected_result, count_window=False)

    assert algo._requested_trim_delta == pytest.approx(0.0)
    assert algo._stored_trim_delta == pytest.approx(0.0)
    assert algo._applied_trim_delta == pytest.approx(0.0)
    assert algo._transferable_i_power == pytest.approx(0.0)
    assert algo._requested_i_transfer == pytest.approx(0.0)
    assert algo._applied_i_transfer == pytest.approx(0.0)
    assert algo._net_command_delta == pytest.approx(0.0)
    assert algo._ff_trim.last_transaction == transaction

    published = build_published_diagnostics(algo)["feedforward"]["fftrim"]
    assert published["last_transaction"] == {
        "timestamp_utc": transaction.timestamp_utc,
        "observation_mode": "periodic",
        "state": "causal_update_without_transfer",
        "reason": "periodic_equilibrium",
        "quality": "valve_segmented_linear",
        "requested_trim_delta": -0.0005,
        "stored_trim_delta": -0.0005,
        "applied_trim_delta": -0.0005,
        "transferable_i_power": 0.0,
        "requested_i_transfer": 0.0,
        "applied_i_transfer": 0.0,
        "net_command_delta": -0.0005,
    }

    algo.load_state(saved_state)

    assert algo._ff_trim.last_transaction is None

    algo._ff_trim.unfreeze()
    for _ in range(3):
        algo._apply_fftrim_observer_result(
            periodic_result,
            count_window=False,
        )

    assert algo._ff_trim.last_transaction is not None

    algo.reset_cycle_state()

    assert algo._ff_trim.last_transaction is None
    assert (
        build_published_diagnostics(algo)["feedforward"]["fftrim"][
            "last_transaction"
        ]
        is None
    )
