"""Pure causal estimation of a local constant thermal-balance bias."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, expm1, isfinite

from .causal_power_trace import CausalPowerTrace, PhysicalTraceWindow
from .thermal_measurement import ThermalMeasurement
from .thermal_model import propagate_1r1c


_ACCEPTED_POWER_QUALITIES = frozenset(
    {"switch_cycle_average", "valve_segmented_linear"}
)


@dataclass(frozen=True)
class CausalThermalBalanceResult:
    """Outcome of one structurally causal endpoint reconstruction."""

    admissible: bool
    reason: str
    thermal_bias_c_per_min: float | None
    equivalent_power_bias: float | None
    predicted_end_temperature: float | None
    observed_end_temperature: float
    endpoint_residual_c: float | None
    duration_s: float
    deadtime_s: float
    mean_linear_power: float | None
    power_coverage_ratio: float
    outside_temperature_change_c: float | None
    power_quality: str
    trace_status: str
    measurement_age_s: float
    power_age_s: float | None
    epoch: int


def evaluate_causal_thermal_balance(
    *,
    start: ThermalMeasurement,
    end: ThermalMeasurement,
    power_trace: CausalPowerTrace,
    a: float,
    b: float,
    deadtime_s: float,
    hvac_mode: str,
    now_monotonic: float,
    max_measurement_age_s: float,
) -> CausalThermalBalanceResult:
    """Estimate a constant bias; callers still own statistical admission."""
    duration_s = end.observed_monotonic - start.observed_monotonic
    measurement_age_s = float(now_monotonic) - end.observed_monotonic
    base = {
        "observed_end_temperature": end.indoor_temperature,
        "duration_s": duration_s,
        "deadtime_s": float(deadtime_s),
        "measurement_age_s": measurement_age_s,
        "epoch": end.epoch,
    }
    values = (
        start.observed_monotonic,
        end.observed_monotonic,
        start.indoor_temperature,
        end.indoor_temperature,
        a,
        b,
        deadtime_s,
        now_monotonic,
        max_measurement_age_s,
    )
    if not all(isfinite(float(value)) for value in values):
        return _reject("non_finite_input", **base)
    if start.measurement_id == end.measurement_id:
        return _reject("measurement_not_distinct", **base)
    if start.epoch != end.epoch or end.epoch != power_trace.epoch:
        return _reject("epoch_mismatch", **base)
    if duration_s <= 0.0:
        return _reject("invalid_interval", **base)
    if float(max_measurement_age_s) < 0.0:
        return _reject("invalid_expiration", **base)
    if measurement_age_s < -1e-6:
        return _reject("evaluation_before_measurement", **base)
    if measurement_age_s > float(max_measurement_age_s) + 1e-6:
        return _reject("measurement_stale", **base)
    if float(b) <= 0.0 or float(deadtime_s) < 0.0:
        return _reject("invalid_model", **base)
    if hvac_mode == "heat":
        if float(a) <= 0.0:
            return _reject("invalid_model_sign", **base)
    elif hvac_mode == "cool":
        if float(a) >= 0.0:
            return _reject("invalid_model_sign", **base)
    else:
        return _reject("invalid_hvac_mode", **base)
    if start.outside_temperature is None or end.outside_temperature is None:
        return _reject("missing_outdoor_temperature", **base)
    if not all(
        isfinite(value)
        for value in (start.outside_temperature, end.outside_temperature)
    ):
        return _reject("invalid_outdoor_temperature", **base)

    delay_s = float(deadtime_s)
    causal_start = start.observed_monotonic - delay_s
    causal_end = end.observed_monotonic - delay_s
    window = power_trace.read_window(
        causal_start,
        causal_end,
        now_monotonic=float(now_monotonic),
        max_age_s=delay_s + float(max_measurement_age_s) + 1e-6,
    )
    rejection = _trace_rejection_reason(window)
    if rejection is not None:
        return _reject(
            rejection,
            window=window,
            outside_change=abs(
                end.outside_temperature - start.outside_temperature
            ),
            **base,
        )

    predicted = _propagate_window(
        start=start,
        end=end,
        window=window,
        causal_start=causal_start,
        outside_start=float(start.outside_temperature),
        outside_end=float(end.outside_temperature),
        a=float(a),
        b=float(b),
    )
    residual = end.indoor_temperature - predicted
    duration_min = duration_s / 60.0
    bias_gain = (1.0 - exp(-float(b) * duration_min)) / float(b)
    if bias_gain <= 1e-12 or not isfinite(bias_gain):
        return _reject("invalid_bias_gain", window=window, **base)
    thermal_bias = residual / bias_gain
    equivalent_power = thermal_bias / float(a)
    if not all(
        isfinite(value)
        for value in (predicted, residual, thermal_bias, equivalent_power)
    ):
        return _reject("non_finite_result", window=window, **base)

    return CausalThermalBalanceResult(
        admissible=True,
        reason="causal_balance_candidate",
        thermal_bias_c_per_min=thermal_bias,
        equivalent_power_bias=equivalent_power,
        predicted_end_temperature=predicted,
        observed_end_temperature=end.indoor_temperature,
        endpoint_residual_c=residual,
        duration_s=duration_s,
        deadtime_s=delay_s,
        mean_linear_power=window.mean_linear_power,
        power_coverage_ratio=window.power_coverage_ratio,
        outside_temperature_change_c=abs(
            end.outside_temperature - start.outside_temperature
        ),
        power_quality=_power_quality(window),
        trace_status=window.status,
        measurement_age_s=measurement_age_s,
        power_age_s=window.age_s,
        epoch=end.epoch,
    )


def _trace_rejection_reason(window: PhysicalTraceWindow) -> str | None:
    if window.status != "complete":
        return f"trace_{window.status}"
    if window.is_stale:
        return "trace_stale"
    qualities = {segment.quality for segment in window.power_segments}
    if len(qualities) != 1 or not qualities.issubset(_ACCEPTED_POWER_QUALITIES):
        return "power_quality"
    return None


def _propagate_window(
    *,
    start: ThermalMeasurement,
    end: ThermalMeasurement,
    window: PhysicalTraceWindow,
    causal_start: float,
    outside_start: float,
    outside_end: float,
    a: float,
    b: float,
) -> float:
    """Propagate exactly for segment-constant power and linear outdoor input."""
    temperature = start.indoor_temperature
    total_duration_s = end.observed_monotonic - start.observed_monotonic
    outside_slope_per_min = (
        outside_end - outside_start
    ) / (total_duration_s / 60.0)
    for segment in window.power_segments:
        segment_start_min = (
            segment.start_monotonic - causal_start
        ) / 60.0
        duration_min = (
            segment.end_monotonic - segment.start_monotonic
        ) / 60.0
        segment_outside_start = (
            outside_start + outside_slope_per_min * segment_start_min
        )
        outside = _effective_linear_outdoor(
            start=segment_outside_start,
            slope_per_min=outside_slope_per_min,
            duration_min=duration_min,
            b=b,
        )
        temperature = propagate_1r1c(
            temperature=temperature,
            external_temperature=outside,
            a=a,
            b=b,
            power=segment.linear_power,
            duration_min=duration_min,
        )
    return temperature


def _effective_linear_outdoor(
    *,
    start: float,
    slope_per_min: float,
    duration_min: float,
    b: float,
) -> float:
    """Return the exact constant outdoor equivalent for one linear interval."""
    x = b * duration_min
    if abs(x) < 1e-6:
        weighted_offset_min = (
            duration_min / 2.0
            + b * duration_min * duration_min / 12.0
        )
    else:
        one_minus_alpha = -expm1(-x)
        weighted_offset_min = duration_min / one_minus_alpha - 1.0 / b
    return start + slope_per_min * weighted_offset_min


def _power_quality(window: PhysicalTraceWindow) -> str:
    qualities = {segment.quality for segment in window.power_segments}
    if len(qualities) == 1:
        return next(iter(qualities))
    return "mixed"


def _reject(
    reason: str,
    *,
    observed_end_temperature: float,
    duration_s: float,
    deadtime_s: float,
    measurement_age_s: float,
    epoch: int,
    window: PhysicalTraceWindow | None = None,
    outside_change: float | None = None,
) -> CausalThermalBalanceResult:
    return CausalThermalBalanceResult(
        admissible=False,
        reason=reason,
        thermal_bias_c_per_min=None,
        equivalent_power_bias=None,
        predicted_end_temperature=None,
        observed_end_temperature=observed_end_temperature,
        endpoint_residual_c=None,
        duration_s=duration_s,
        deadtime_s=deadtime_s,
        mean_linear_power=(window.mean_linear_power if window else None),
        power_coverage_ratio=(window.power_coverage_ratio if window else 0.0),
        outside_temperature_change_c=outside_change,
        power_quality=(_power_quality(window) if window else "unavailable"),
        trace_status=(window.status if window else "unavailable"),
        measurement_age_s=measurement_age_s,
        power_age_s=(window.age_s if window else None),
        epoch=epoch,
    )
