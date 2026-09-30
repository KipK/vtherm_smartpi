"""Tests for pure causal reconstruction of a local thermal-balance bias."""

from math import exp

import pytest

from custom_components.vtherm_smartpi.smartpi.causal_power_trace import (
    AppliedPowerSegment,
    CausalPowerTrace,
)
from custom_components.vtherm_smartpi.smartpi.causal_thermal_balance import (
    evaluate_causal_thermal_balance,
)
from custom_components.vtherm_smartpi.smartpi.thermal_measurement import (
    ThermalMeasurement,
)
from custom_components.vtherm_smartpi.smartpi.thermal_model import propagate_1r1c


def _measurement(
    observed_monotonic: float,
    temperature: float,
    *,
    measurement_id: str,
    outside: float = 10.0,
    epoch: int = 0,
) -> ThermalMeasurement:
    return ThermalMeasurement(
        observed_monotonic=observed_monotonic,
        measurement_id=measurement_id,
        indoor_temperature=temperature,
        outside_temperature=outside,
        epoch=epoch,
    )


@pytest.mark.parametrize(
    ("hvac_mode", "a", "bias"),
    (
        ("heat", 0.08, -0.003),
        ("cool", -0.08, 0.003),
    ),
)
def test_balance_recovers_signed_constant_bias(
    hvac_mode: str,
    a: float,
    bias: float,
) -> None:
    """HEAT and COOL recover mirrored bias from committed switch power."""
    b = 0.004
    power = 0.35
    duration_min = 10.0
    start_temperature = 20.0
    end_temperature = propagate_1r1c(
        temperature=start_temperature,
        external_temperature=10.0,
        a=a,
        b=b,
        power=power,
        duration_min=duration_min,
        bias=bias,
    )
    trace = CausalPowerTrace()
    trace.record_applied_power(
        AppliedPowerSegment(
            0.0,
            duration_min * 60.0,
            power,
            "switch_cycle_average",
        )
    )

    result = evaluate_causal_thermal_balance(
        start=_measurement(120.0, start_temperature, measurement_id="start"),
        end=_measurement(
            120.0 + duration_min * 60.0,
            end_temperature,
            measurement_id="end",
        ),
        power_trace=trace,
        a=a,
        b=b,
        deadtime_s=120.0,
        hvac_mode=hvac_mode,
        now_monotonic=120.0 + duration_min * 60.0,
        max_measurement_age_s=0.0,
    )

    assert result.admissible is True
    assert result.reason == "causal_balance_candidate"
    assert result.thermal_bias_c_per_min == pytest.approx(bias)
    assert result.equivalent_power_bias == pytest.approx(bias / a)
    assert result.mean_linear_power == pytest.approx(power)
    assert result.power_coverage_ratio == pytest.approx(1.0)
    assert result.power_quality == "switch_cycle_average"


def test_balance_integrates_segmented_power_and_linear_outdoor_temperature() -> None:
    """Valve segments integrate a linear outdoor ramp without false bias."""
    a = 0.08
    b = 0.004
    start_temperature = 20.0
    one_minus_alpha = 1.0 - exp(-b * 5.0)
    weighted_offset = 5.0 / one_minus_alpha - 1.0 / b
    first_outside = 10.0 + 0.4 * weighted_offset
    second_outside = 12.0 + 0.4 * weighted_offset
    middle = propagate_1r1c(
        temperature=start_temperature,
        external_temperature=first_outside,
        a=a,
        b=b,
        power=0.2,
        duration_min=5.0,
    )
    end_temperature = propagate_1r1c(
        temperature=middle,
        external_temperature=second_outside,
        a=a,
        b=b,
        power=0.8,
        duration_min=5.0,
    )
    trace = CausalPowerTrace()
    trace.record_applied_power(
        AppliedPowerSegment(0.0, 300.0, 0.2, "valve_segmented_linear")
    )
    trace.record_applied_power(
        AppliedPowerSegment(300.0, 600.0, 0.8, "valve_segmented_linear")
    )

    result = evaluate_causal_thermal_balance(
        start=_measurement(
            120.0,
            start_temperature,
            measurement_id="start",
            outside=10.0,
        ),
        end=_measurement(
            720.0,
            end_temperature,
            measurement_id="end",
            outside=14.0,
        ),
        power_trace=trace,
        a=a,
        b=b,
        deadtime_s=120.0,
        hvac_mode="heat",
        now_monotonic=720.0,
        max_measurement_age_s=0.0,
    )

    assert result.admissible is True
    assert result.thermal_bias_c_per_min == pytest.approx(0.0, abs=1e-12)
    assert result.mean_linear_power == pytest.approx(0.5)
    assert result.outside_temperature_change_c == pytest.approx(4.0)
    assert result.power_quality == "valve_segmented_linear"


@pytest.mark.parametrize(
    ("segments", "expected_reason"),
    (
        (
            (AppliedPowerSegment(0.0, 300.0, 0.4, "switch_cycle_average"),),
            "trace_pending",
        ),
        (
            (
                AppliedPowerSegment(0.0, 300.0, 0.4, "switch_cycle_average"),
                AppliedPowerSegment(304.0, 600.0, 0.4, "switch_cycle_average"),
            ),
            "trace_imputed",
        ),
        (
            (AppliedPowerSegment(0.0, 600.0, 0.4),),
            "power_quality",
        ),
    ),
)
def test_balance_rejects_unproven_physical_windows(
    segments: tuple[AppliedPowerSegment, ...],
    expected_reason: str,
) -> None:
    """Missing, imputed, or provenance-free power cannot estimate bias."""
    trace = CausalPowerTrace()
    for segment in segments:
        trace.record_applied_power(segment)

    result = evaluate_causal_thermal_balance(
        start=_measurement(120.0, 20.0, measurement_id="start"),
        end=_measurement(720.0, 20.0, measurement_id="end"),
        power_trace=trace,
        a=0.08,
        b=0.004,
        deadtime_s=120.0,
        hvac_mode="heat",
        now_monotonic=720.0,
        max_measurement_age_s=0.0,
    )

    assert result.admissible is False
    assert result.reason == expected_reason
    assert result.thermal_bias_c_per_min is None


def test_balance_rejects_mixed_structural_epochs() -> None:
    """Measurements cannot cross a physical-trace reset boundary."""
    trace = CausalPowerTrace()
    trace.record_applied_power(
        AppliedPowerSegment(0.0, 600.0, 0.4, "switch_cycle_average")
    )

    result = evaluate_causal_thermal_balance(
        start=_measurement(120.0, 20.0, measurement_id="start", epoch=0),
        end=_measurement(720.0, 20.0, measurement_id="end", epoch=1),
        power_trace=trace,
        a=0.08,
        b=0.004,
        deadtime_s=120.0,
        hvac_mode="heat",
        now_monotonic=720.0,
        max_measurement_age_s=0.0,
    )

    assert result.admissible is False
    assert result.reason == "epoch_mismatch"


def test_balance_rejects_expired_measurement_evidence() -> None:
    """Historical endpoints need an explicit as-of time and expiry policy."""
    trace = CausalPowerTrace()
    trace.record_applied_power(
        AppliedPowerSegment(0.0, 600.0, 0.4, "switch_cycle_average")
    )

    result = evaluate_causal_thermal_balance(
        start=_measurement(120.0, 20.0, measurement_id="start"),
        end=_measurement(720.0, 20.0, measurement_id="end"),
        power_trace=trace,
        a=0.08,
        b=0.004,
        deadtime_s=120.0,
        hvac_mode="heat",
        now_monotonic=781.0,
        max_measurement_age_s=60.0,
    )

    assert result.admissible is False
    assert result.reason == "measurement_stale"
    assert result.measurement_age_s == pytest.approx(61.0)
