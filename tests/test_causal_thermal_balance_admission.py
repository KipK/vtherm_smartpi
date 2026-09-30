"""Contract tests for bounded causal thermal-balance admission."""

from dataclasses import replace

import pytest

from custom_components.vtherm_smartpi.smartpi.causal_power_trace import (
    AppliedPowerSegment,
    CausalPowerTrace,
)
from custom_components.vtherm_smartpi.smartpi.causal_thermal_balance_admission import (
    CausalThermalBalanceAdmission,
    ThermalBalanceContext,
)
from custom_components.vtherm_smartpi.smartpi.thermal_measurement import (
    ThermalMeasurement,
)
from custom_components.vtherm_smartpi.smartpi.thermal_model import propagate_1r1c


def _context(mode: str = "heat") -> ThermalBalanceContext:
    return ThermalBalanceContext(
        hvac_mode=mode,
        a=0.08 if mode == "heat" else -0.08,
        b=0.004,
        deadtime_s=120.0,
        deadtime_reliable=True,
        cycle_s=300.0,
        sensor_resolution_c=0.1,
        sensor_noise_bound_c=0.0,
        model_revision=1,
        setpoint_revision=1,
        actuator_revision=1,
    )


def _measurement(time: float, temperature: float, epoch: int = 0) -> ThermalMeasurement:
    return ThermalMeasurement(time, str(time), temperature, 10.0, epoch)


def _temperatures(
    context: ThermalBalanceContext,
    biases: tuple[float, ...],
) -> list[float]:
    values = [20.0]
    for bias in biases:
        values.append(
            propagate_1r1c(
                temperature=values[-1],
                external_temperature=10.0,
                a=context.a,
                b=context.b,
                power=0.4,
                duration_min=30.0,
                bias=bias,
            )
        )
    return values


def _trace(end: float = 5400.0) -> CausalPowerTrace:
    trace = CausalPowerTrace()
    if end > 0:
        trace.record_applied_power(
            AppliedPowerSegment(0.0, end, 0.4, "switch_cycle_average")
        )
    return trace


def _start(
    admission: CausalThermalBalanceAdmission,
    context: ThermalBalanceContext,
    trace: CausalPowerTrace,
    temperature: float = 20.0,
) -> None:
    assert admission.observe(
        measurement=None, context=context, power_trace=trace, now_monotonic=0.0
    ).reason == "structural_reset"
    assert admission.observe(
        measurement=_measurement(120.0, temperature),
        context=context, power_trace=trace, now_monotonic=120.0,
    ).reason == "anchor_recorded"


@pytest.mark.parametrize("mode,bias", [("heat", 0.008), ("cool", -0.008)])
def test_constant_bias_requires_three_disjoint_windows(mode: str, bias: float) -> None:
    context = _context(mode)
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (bias,) * 3)
    _start(admission, context, trace)
    short = admission.observe(
        measurement=_measurement(1000.0, values[0]),
        context=context, power_trace=trace, now_monotonic=1000.0,
    )
    assert short.reason == "window_too_short"
    assert short.evidence_count == 0
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context, power_trace=trace, now_monotonic=end,
        )
        assert result.evidence_count == index
        assert result.min_window_s == 1800.0
        if index < 3:
            assert result.reason == "insufficient_windows"
            assert result.thermal_bias_c_per_min is None
    assert result.reason == "admitted"
    assert result.thermal_bias_c_per_min == pytest.approx(bias)
    assert result.equivalent_power_bias == pytest.approx(bias / context.a)
    assert result.valid_until_monotonic == pytest.approx(9120.0)


def test_sub_resolution_effect_and_inconsistent_windows_are_rejected() -> None:
    context = _context()
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    _start(admission, context, trace)
    small = _temperatures(context, (0.001,))[1]
    result = admission.observe(
        measurement=_measurement(1920.0, small),
        context=context, power_trace=trace, now_monotonic=1920.0,
    )
    assert result.reason == "effect_below_resolution"
    assert result.evidence_count == 0

    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (0.008, 0.018, 0.008))
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context, power_trace=trace, now_monotonic=end,
        )
    assert result.reason == "inconsistent_dispersion"
    assert result.thermal_bias_c_per_min is None
    assert result.evidence_count == 1

    sign_changed = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    opposite = _temperatures(context, (0.008, -0.008))
    _start(sign_changed, context, trace)
    sign_changed.observe(
        measurement=_measurement(1920.0, opposite[1]),
        context=context, power_trace=trace, now_monotonic=1920.0,
    )
    result = sign_changed.observe(
        measurement=_measurement(3720.0, opposite[2]),
        context=context, power_trace=trace, now_monotonic=3720.0,
    )
    assert result.reason == "inconsistent_sign"
    assert result.evidence_count == 1


def test_pending_endpoint_is_retried_after_power_commit() -> None:
    context = _context()
    trace = _trace(1700.0)
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    _start(admission, context, trace)
    end_temperature = _temperatures(context, (0.008,))[1]
    pending = admission.observe(
        measurement=_measurement(1920.0, end_temperature),
        context=context, power_trace=trace, now_monotonic=1920.0,
    )
    assert pending.reason == "trace_pending"
    assert pending.pending
    trace.record_applied_power(
        AppliedPowerSegment(1700.0, 1800.0, 0.4, "switch_cycle_average")
    )
    result = admission.observe(
        measurement=None, context=context, power_trace=trace,
        now_monotonic=1930.0,
    )
    assert result.reason == "insufficient_windows"
    assert result.evidence_count == 1
    assert not result.pending


def test_contradictory_window_revokes_an_admitted_bias() -> None:
    context = _context()
    trace = _trace(7200.0)
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (0.008, 0.008, 0.008, -0.008))
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
    assert result.reason == "admitted"

    end = 120.0 + 1800.0 * 4
    result = admission.observe(
        measurement=_measurement(end, values[4]),
        context=context,
        power_trace=trace,
        now_monotonic=end,
    )
    assert result.reason == "inconsistent_sign"
    assert result.thermal_bias_c_per_min is None
    assert result.valid_until_monotonic is None
    assert result.evidence_count == 1


def test_pending_admission_does_not_extend_expired_measurement_evidence() -> None:
    context = _context()
    trace = _trace(5300.0)
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=1.0
    )
    values = _temperatures(context, (0.008,) * 3)
    _start(admission, context, trace)
    for index in range(1, 3):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
        assert result.reason == "insufficient_windows"

    end = 5520.0
    pending = admission.observe(
        measurement=_measurement(end, values[3]),
        context=context,
        power_trace=trace,
        now_monotonic=end,
    )
    assert pending.reason == "trace_pending"
    trace.record_applied_power(
        AppliedPowerSegment(5300.0, 5400.0, 0.4, "switch_cycle_average")
    )
    result = admission.observe(
        measurement=None,
        context=context,
        power_trace=trace,
        now_monotonic=end + 10.0,
    )
    assert result.reason == "admission_expired"
    assert result.thermal_bias_c_per_min is None
    assert result.valid_until_monotonic is None
    assert result.evidence_count == 0


def test_expiration_requires_three_fresh_windows_before_readmission() -> None:
    context = _context()
    trace = _trace(12600.0)
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=60.0
    )
    values = _temperatures(context, (0.008,) * 7)
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
    assert result.reason == "admitted"

    expired = admission.observe(
        measurement=None,
        context=context,
        power_trace=trace,
        now_monotonic=5581.0,
    )
    assert expired.reason == "admission_expired"
    assert expired.evidence_count == 0
    end = 7320.0
    anchor = admission.observe(
        measurement=_measurement(end, values[4]),
        context=context,
        power_trace=trace,
        now_monotonic=end,
    )
    assert anchor.reason == "anchor_recorded"
    for index in range(5, 8):
        end = 120.0 + 1800.0 * index
        fresh = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
        assert fresh.evidence_count == index - 4
    assert fresh.reason == "admitted"
    assert fresh.thermal_bias_c_per_min == pytest.approx(0.008)


def test_revisions_washout_setpoint_retention_and_expiry() -> None:
    context = _context()
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (0.008,) * 3)
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context, power_trace=trace, now_monotonic=end,
        )
    assert result.reason == "admitted"

    setpoint = replace(context, setpoint_revision=2)
    result = admission.observe(
        measurement=None, context=setpoint, power_trace=trace, now_monotonic=5600.0
    )
    assert result.reason == "setpoint_changed"
    assert result.evidence_count == 0
    assert result.thermal_bias_c_per_min == pytest.approx(0.008)
    assert admission.observe(
        measurement=None,
        context=setpoint,
        power_trace=trace,
        now_monotonic=5601.0,
    ).reason == "deadtime_washout"
    result = admission.observe(
        measurement=None, context=setpoint, power_trace=trace, now_monotonic=9120.1
    )
    assert result.reason == "admission_expired"
    assert result.thermal_bias_c_per_min is None
    assert result.valid_until_monotonic is None

    for changed in (
        replace(setpoint, model_revision=2),
        replace(setpoint, hvac_mode="cool", a=-0.08),
        replace(setpoint, actuator_revision=2),
    ):
        result = admission.observe(
            measurement=None, context=changed, power_trace=trace,
            now_monotonic=10000.0,
        )
        assert result.reason == "structural_reset"
        assert result.evidence_count == 0
        assert admission.observe(
            measurement=None, context=changed, power_trace=trace,
            now_monotonic=10001.0,
        ).reason == "deadtime_washout"
        setpoint = changed

    trace.reset()
    assert admission.observe(
        measurement=None, context=setpoint, power_trace=trace,
        now_monotonic=11000.0,
    ).reason == "structural_reset"
    result = admission.reset(
        context=setpoint, power_trace=trace, now_monotonic=12000.0
    )
    assert result.reason == "external_reset"
    assert result.thermal_bias_c_per_min is None
    assert admission.observe(
        measurement=None, context=setpoint, power_trace=trace,
        now_monotonic=12001.0,
    ).reason == "deadtime_washout"


def test_invalid_sampling_context_revokes_an_admitted_bias() -> None:
    context = _context()
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (0.008,) * 3)
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
    assert result.reason == "admitted"

    invalid = replace(context, sensor_resolution_c=0.0)
    result = admission.observe(
        measurement=None,
        context=invalid,
        power_trace=trace,
        now_monotonic=5600.0,
    )
    assert result.reason == "invalid_context"
    assert result.thermal_bias_c_per_min is None
    assert result.valid_until_monotonic is None


def test_bounded_sensor_noise_is_included_in_evidence_uncertainty() -> None:
    context = replace(
        _context(),
        sensor_resolution_c=0.01,
        sensor_noise_bound_c=0.02,
    )
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    true_values = _temperatures(context, (0.008,) * 3)
    noisy_values = [
        value + noise
        for value, noise in zip(true_values, (0.02, -0.02, 0.02, -0.02))
    ]
    _start(admission, context, trace, temperature=noisy_values[0])
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, noisy_values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
    assert result.reason == "admitted"
    assert result.thermal_bias_c_per_min == pytest.approx(0.008, abs=0.002)


def test_admitted_bias_lies_inside_the_common_uncertainty_interval() -> None:
    context = replace(
        _context(),
        sensor_resolution_c=0.01,
        sensor_noise_bound_c=0.02,
    )
    trace = _trace()
    admission = CausalThermalBalanceAdmission(
        max_measurement_age_s=60.0, admitted_ttl_s=3600.0
    )
    values = _temperatures(context, (0.0067, 0.0093, 0.0067))
    _start(admission, context, trace)
    for index in range(1, 4):
        end = 120.0 + 1800.0 * index
        result = admission.observe(
            measurement=_measurement(end, values[index]),
            context=context,
            power_trace=trace,
            now_monotonic=end,
        )
    assert result.reason == "admitted"
    assert result.thermal_bias_c_per_min is not None
    assert 0.0076 <= result.thermal_bias_c_per_min <= 0.0084
