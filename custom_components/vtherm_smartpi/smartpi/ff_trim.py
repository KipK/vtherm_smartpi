"""
Feed-Forward Slow Trim for Smart-PI.

The trim corrects a slow, persistent bias in u_ff_ab without replacing the FF principal.
It is applied additively AFTER the taper, so regime-based modulation does not attenuate
the empirical correction.

Signal chain:
  u_ff_eff = clamp(alpha * u_ff_ab + u_ff_trim, 0, 1)

Authority:
  |u_ff_trim| <= rho_trim * max(u_ff_ab, FF_TRIM_EPSILON)

The trim is frozen under several conditions (see freeze()).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import median
from typing import Callable, Deque, Sequence

from .const import (
    GovernanceRegime,
    clamp,
    FF_TRIM_RHO,
    FF_TRIM_LAMBDA,
    FF_TRIM_EPSILON,
    FF_TRIM_PERSISTENCE,
    FF_TRIM_BUFFER_SIZE,
    FF_TRIM_DELTA_EPSILON,
    FF_TRIM_PI_STABILITY_EPSILON,
    FF_TRIM_MIN_WINDOW_MIN,
    FF_TRIM_MIN_WINDOW_CYCLES,
    FF_TRIM_DEADTIME_WINDOW_FACTOR,
    FF_TRIM_MIN_DISTINCT_MEASUREMENTS,
    FF_TRIM_MIN_POWER_COVERAGE,
    FF_TRIM_PERIODIC_MIN_POWER_RANGE,
    FF_TRIM_MAX_ERROR_C,
    FF_TRIM_MAX_SLOPE_H,
    FF_TRIM_TRANSFER_MAX_ERROR_C,
    FF_TRIM_TRANSFER_MAX_SLOPE_H,
    FF_TRIM_TRANSFER_MAX_P_POWER,
    FF_TRIM_TRANSFER_MAX_POWER_RANGE,
    FF_TRIM_TRANSFER_MAX_DELIVERY_RESIDUAL,
    FF_TRIM_TRANSFER_COMMAND_EPSILON,
    KI_MIN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FFTrimUpdateResult:
    """Result of one persistent trim update attempt."""

    updated: bool
    reason: str
    applied_delta: float
    pending_count: int
    median_correction: float = 0.0
    requested_trim_delta: float = 0.0
    stored_trim_delta: float = 0.0
    visible_ff_delta: float = 0.0


@dataclass(frozen=True)
class FFTrimPIEligibility:
    """PI-state eligibility for trim learning."""

    admissible: bool
    reason: str


@dataclass(frozen=True)
class AppliedPowerSegment:
    """Linear power applied over one monotonic interval."""

    start_monotonic: float
    end_monotonic: float
    linear_power: float


@dataclass(frozen=True)
class ControlOwnershipSnapshot:
    """Control terms that own one physically committed command."""

    u_ff1: float
    trim_stored: float
    u_ff_visible: float
    u_ff3: float
    u_p: float
    u_i: float
    ki: float
    gain_generation: int
    u_cmd: float
    u_limited: float
    linear_committed_power: float
    regime: GovernanceRegime | str | None
    i_mode: str | None
    quality: str = "causal_full"
    constraint_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlOwnershipSegment:
    """One ownership snapshot over a monotonic physical interval."""

    start_monotonic: float
    end_monotonic: float
    ownership: ControlOwnershipSnapshot


@dataclass(frozen=True)
class FFTrimThermalSample:
    """One distinct temperature measurement and its control context."""

    observed_monotonic: float
    measurement_id: str
    temperature: float
    target: float
    ff1: float
    regime: GovernanceRegime | str | None
    i_mode: str | None
    u_pi: float | None
    hvac_mode: str
    saturated: bool
    trajectory_active: bool
    ff3_active: bool
    setpoint_changed: bool
    outside_temperature_available: bool
    model_reliable: bool
    outside_temperature: float | None = None
    trim_frozen_reason: str | None = None
    saturation_state: str = "NO_SAT"


@dataclass(frozen=True)
class CausalFFTrimResult:
    """Outcome of one independent causal thermal window."""

    admissible: bool
    reason: str
    correction: float | None
    target_trim: float | None
    mean_causal_power: float | None
    mean_ff1: float | None
    mean_temperature: float | None
    mean_error: float | None
    mean_slope_h: float | None
    observed_hold_power: float | None
    duration_s: float
    measurement_count: int
    alignment_delay_s: float | None
    power_coverage_ratio: float
    mean_p_power: float | None = None
    mean_i_power: float | None = None
    mean_visible_ff_power: float | None = None
    mean_ki: float | None = None
    mean_delivery_residual: float | None = None
    physical_power_deficit: float | None = None
    decomposed_correction: float | None = None
    transfer_eligible: bool = False
    transfer_reason: str = "ownership_unavailable"
    transfer_quality: str = "unavailable"
    observation_mode: str = "stationary"


@dataclass(frozen=True)
class FFTrimWindowProposal:
    """One independent causal window submitted to the persistence gate."""

    correction: float
    mean_ff1: float
    physical_power_deficit: float | None
    integral_bias: float | None
    transfer_eligible: bool
    transfer_reason: str
    observation_mode: str = "stationary"


@dataclass(frozen=True)
class FFTrimPersistentResult:
    """Robust aggregate of same-context causal window proposals."""

    ready: bool
    reason: str
    pending_count: int
    median_correction: float = 0.0
    median_ff1: float = 0.0
    median_physical_power_deficit: float | None = None
    median_integral_bias: float | None = None
    transfer_eligible: bool = False
    transfer_reason: str = "not_ready"
    observation_mode: str = "stationary"


@dataclass(frozen=True)
class FFTrimBumplessPlan:
    """Fully bounded ownership-transfer transaction prepared without mutation."""

    applicable: bool
    state: str
    reason: str
    alpha: float = 0.0
    old_trim: float = 0.0
    new_trim: float = 0.0
    requested_trim_delta: float = 0.0
    stored_trim_delta: float = 0.0
    visible_ff_delta: float = 0.0
    transferable_i_power: float = 0.0
    requested_i_transfer: float = 0.0
    applied_i_transfer: float = 0.0
    physical_power_deficit: float = 0.0
    net_command_delta: float = 0.0


@dataclass(frozen=True)
class _OwnershipStats:
    """Time-weighted ownership statistics over one causal interval."""

    coverage: float
    mean_ff1: float
    mean_trim: float
    mean_visible_ff: float
    mean_ff3: float
    mean_p: float
    mean_i: float
    mean_ki: float
    mean_model_power: float
    mean_committed_power: float
    max_delivery_residual: float
    ff1_range: float
    trim_range: float
    p_range: float
    i_range: float
    i_drift: float
    committed_range: float
    ki_min: float
    i_sign_persistent: bool
    regimes: frozenset[GovernanceRegime | str | None]
    i_modes: frozenset[str | None]
    constraint_flags: frozenset[str]
    gain_generations: frozenset[int]
    quality: str


class CausalFFTrimObserver:
    """Estimate slow FF bias from causally aligned thermal windows."""

    _POWER_HISTORY_MAX_S = 24.0 * 60.0 * 60.0
    _MAX_CONTINUITY_GAP_S = 5.0

    def __init__(self, cycle_min: float) -> None:
        self._cycle_min = max(float(cycle_min), 0.0)
        self._power_segments: Deque[AppliedPowerSegment] = deque()
        self._ownership_segments: Deque[ControlOwnershipSegment] = deque()
        self._active_cycle_segments: list[AppliedPowerSegment] = []
        self._active_ownership_segments: list[ControlOwnershipSegment] = []
        self._active_power_start: float | None = None
        self._active_linear_power: float | None = None
        self._active_ownership_start: float | None = None
        self._active_ownership: ControlOwnershipSnapshot | None = None
        self._thermal_samples: list[FFTrimThermalSample] = []
        self._last_measurement_id: str | None = None
        self._window_deadtime_s: float | None = None
        self._washout_until_monotonic: float = 0.0
        self.state: str = "warming_up"
        self.last_reject_reason: str = "none"
        self.last_update_reason: str = "none"
        self.target_duration_s: float = FF_TRIM_MIN_WINDOW_MIN * 60.0
        self.last_result: CausalFFTrimResult = self._empty_result(
            "warming_up",
            measurement_count=0,
        )
        self._last_admissible_result: CausalFFTrimResult | None = None

    def record_applied_power(self, segment: AppliedPowerSegment) -> None:
        """Append one non-overlapping segment in linear model space."""
        start = float(segment.start_monotonic)
        end = float(segment.end_monotonic)
        power = clamp(float(segment.linear_power), 0.0, 1.0)
        if not all(isfinite(value) for value in (start, end, power)) or end <= start:
            return

        if self._power_segments:
            previous = self._power_segments[-1]
            gap_s = start - previous.end_monotonic
            if 0.0 < gap_s <= self._MAX_CONTINUITY_GAP_S:
                previous = AppliedPowerSegment(
                    previous.start_monotonic,
                    start,
                    previous.linear_power,
                )
                self._power_segments[-1] = previous
            start = max(start, previous.end_monotonic)
            if end <= start:
                return
            if (
                abs(start - previous.end_monotonic) <= 1e-6
                and abs(power - previous.linear_power) <= 1e-9
            ):
                self._power_segments[-1] = AppliedPowerSegment(
                    previous.start_monotonic,
                    end,
                    power,
                )
                self._prune_power_history(end)
                return

        self._power_segments.append(AppliedPowerSegment(start, end, power))
        self._prune_power_history(end)

    def start_applied_cycle(
        self,
        *,
        now_monotonic: float,
        linear_power: float,
        ownership: ControlOwnershipSnapshot | None = None,
    ) -> None:
        """Start the transient trace for one physical scheduler cycle."""
        self._active_cycle_segments.clear()
        self._active_ownership_segments.clear()
        self._active_power_start = float(now_monotonic)
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)
        self._active_ownership_start = float(now_monotonic)
        self._active_ownership = ownership

    def update_applied_power(
        self,
        *,
        now_monotonic: float,
        linear_power: float,
        ownership: ControlOwnershipSnapshot | None = None,
    ) -> None:
        """Record a physical valve-power change inside the active cycle."""
        now = float(now_monotonic)
        if (
            self._active_power_start is not None
            and self._active_linear_power is not None
            and now > self._active_power_start
        ):
            self._active_cycle_segments.append(
                AppliedPowerSegment(
                    self._active_power_start,
                    now,
                    self._active_linear_power,
                )
            )
        if (
            self._active_ownership_start is not None
            and self._active_ownership is not None
            and now > self._active_ownership_start
        ):
            self._active_ownership_segments.append(
                ControlOwnershipSegment(
                    self._active_ownership_start,
                    now,
                    self._active_ownership,
                )
            )
        self._active_power_start = now
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)
        self._active_ownership_start = now
        self._active_ownership = ownership

    def complete_applied_cycle(
        self,
        *,
        now_monotonic: float,
        realized_linear_power: float | None,
        use_valve_trace: bool,
    ) -> None:
        """Commit either the valve trace or the realized cycle duty."""
        cycle_end = float(now_monotonic)
        cycle_start = self._active_power_start
        cycle_power = self._active_linear_power
        if cycle_start is None or cycle_end <= cycle_start:
            self._clear_active_cycle()
            return

        if use_valve_trace:
            if cycle_power is not None:
                self._active_cycle_segments.append(
                    AppliedPowerSegment(cycle_start, cycle_end, cycle_power)
                )
            for segment in self._active_cycle_segments:
                self.record_applied_power(segment)
        elif realized_linear_power is not None:
            first_start = (
                self._active_cycle_segments[0].start_monotonic
                if self._active_cycle_segments
                else cycle_start
            )
            self.record_applied_power(
                AppliedPowerSegment(
                    first_start,
                    cycle_end,
                    clamp(float(realized_linear_power), 0.0, 1.0),
                )
            )

        if (
            self._active_ownership_start is not None
            and self._active_ownership is not None
            and cycle_end > self._active_ownership_start
        ):
            self._active_ownership_segments.append(
                ControlOwnershipSegment(
                    self._active_ownership_start,
                    cycle_end,
                    self._active_ownership,
                )
            )
        for segment in self._active_ownership_segments:
            self._record_ownership_segment(segment)

        self._clear_active_cycle()

    def record_thermal_sample(
        self,
        sample: FFTrimThermalSample,
        *,
        deadtime_s: float | None,
        deadtime_reliable: bool,
    ) -> CausalFFTrimResult | None:
        """Store one fresh measurement or reject its full thermal context."""
        rejection = self._sample_rejection_reason(sample)
        if rejection is not None:
            self._last_measurement_id = sample.measurement_id
            return self.invalidate(
                rejection,
                now_monotonic=sample.observed_monotonic,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )

        if sample.measurement_id == self._last_measurement_id:
            return None
        self._last_measurement_id = sample.measurement_id

        if (
            not deadtime_reliable
            or deadtime_s is None
            or not isfinite(float(deadtime_s))
            or float(deadtime_s) <= 0.0
        ):
            return self.invalidate(
                "deadtime_unreliable",
                now_monotonic=sample.observed_monotonic,
            )

        delay_s = float(deadtime_s)
        if (
            self._window_deadtime_s is not None
            and abs(delay_s - self._window_deadtime_s)
            > max(1.0, 0.05 * self._window_deadtime_s)
        ):
            self._last_measurement_id = sample.measurement_id
            return self.invalidate(
                "deadtime_changed",
                now_monotonic=sample.observed_monotonic,
                washout_s=delay_s,
            )

        if sample.observed_monotonic < self._washout_until_monotonic:
            self.state = "waiting_deadtime"
            self.last_reject_reason = "washout"
            return None

        if not self._thermal_samples:
            self._window_deadtime_s = delay_s
        self._thermal_samples.append(sample)
        self.state = "collecting"
        self.last_reject_reason = "none"
        return None

    def try_complete_window(
        self,
        *,
        a: float,
        b: float,
        deadtime_s: float | None,
        deadtime_reliable: bool,
        current_trim: float,
    ) -> CausalFFTrimResult | None:
        """Return a correction when one full causal window is observable."""
        if (
            not deadtime_reliable
            or deadtime_s is None
            or not isfinite(float(deadtime_s))
            or float(deadtime_s) <= 0.0
        ):
            self.state = "waiting_deadtime"
            self.last_reject_reason = "deadtime_unreliable"
            return None

        delay_s = float(deadtime_s)
        if self._window_deadtime_s is not None:
            delay_s = self._window_deadtime_s
        self.target_duration_s = max(
            FF_TRIM_MIN_WINDOW_MIN * 60.0,
            FF_TRIM_MIN_WINDOW_CYCLES * self._cycle_min * 60.0,
            FF_TRIM_DEADTIME_WINDOW_FACTOR * delay_s,
        )

        if not self._power_segments:
            self.state = "warming_up"
            self.last_reject_reason = "causal_power_not_covered"
            return None

        earliest_observable = self._power_segments[0].start_monotonic + delay_s
        while (
            self._thermal_samples
            and self._thermal_samples[0].observed_monotonic < earliest_observable
        ):
            self._thermal_samples.pop(0)

        if len(self._thermal_samples) < FF_TRIM_MIN_DISTINCT_MEASUREMENTS:
            self.state = "collecting"
            self.last_reject_reason = "not_enough_measurements"
            return None

        samples = tuple(self._thermal_samples)
        duration_s = samples[-1].observed_monotonic - samples[0].observed_monotonic
        if duration_s < self.target_duration_s:
            self.state = "collecting"
            self.last_reject_reason = "window_too_short"
            return None

        result = self._evaluate_completed_window(
            samples=samples,
            a=float(a),
            b=float(b),
            delay_s=delay_s,
            current_trim=float(current_trim),
            observation_mode="stationary",
        )
        if result is None:
            self.state = "waiting_deadtime"
            self.last_reject_reason = "causal_power_not_covered"
            return None

        self.last_result = result
        if result.admissible:
            self._last_admissible_result = result
        self.state = "ready" if result.admissible else "rejected"
        self.last_reject_reason = "none" if result.admissible else result.reason
        self.last_update_reason = result.reason if result.admissible else "skipped"
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        return result

    def evaluate_periodic_window(
        self,
        samples: Sequence[FFTrimThermalSample],
        *,
        a: float,
        b: float,
        deadtime_s: float,
        current_trim: float,
    ) -> CausalFFTrimResult | None:
        """Evaluate a phase-closed window against the shared causal traces."""
        return self._evaluate_completed_window(
            samples=tuple(samples),
            a=float(a),
            b=float(b),
            delay_s=float(deadtime_s),
            current_trim=float(current_trim),
            observation_mode="periodic",
        )

    def publish_external_result(self, result: CausalFFTrimResult) -> None:
        """Publish a result evaluated from another logical sample window."""
        self.last_update_reason = result.reason if result.admissible else "skipped"
        if result.admissible:
            self._last_admissible_result = result

    @property
    def last_admissible_result(self) -> CausalFFTrimResult | None:
        """Return the latest admissible completed result across all modes."""
        return self._last_admissible_result

    @property
    def earliest_power_start(self) -> float | None:
        """Return the oldest physical-power timestamp still retained."""
        if not self._power_segments:
            return None
        return self._power_segments[0].start_monotonic

    def _evaluate_completed_window(
        self,
        *,
        samples: Sequence[FFTrimThermalSample],
        a: float,
        b: float,
        delay_s: float,
        current_trim: float,
        observation_mode: str,
    ) -> CausalFFTrimResult | None:
        """Evaluate one complete window using the shared causal traces."""
        duration_s = samples[-1].observed_monotonic - samples[0].observed_monotonic
        model_rejection = self._model_rejection_reason(samples[-1].hvac_mode, a, b)
        if model_rejection is not None:
            return self._reject_completed_window(
                model_rejection,
                samples,
                delay_s,
                observation_mode=observation_mode,
            )

        target_span = max(sample.target for sample in samples) - min(
            sample.target for sample in samples
        )
        if target_span > 1e-6:
            return self._reject_completed_window(
                "setpoint_changed",
                samples,
                delay_s,
                observation_mode=observation_mode,
            )

        modes = {sample.hvac_mode for sample in samples}
        if len(modes) != 1:
            return self._reject_completed_window(
                "hvac_mode_changed",
                samples,
                delay_s,
                observation_mode=observation_mode,
            )

        u_pi_values = [sample.u_pi for sample in samples if sample.u_pi is not None]
        if observation_mode == "stationary":
            if len(u_pi_values) != len(samples):
                return self._reject_completed_window(
                    "pi_missing",
                    samples,
                    delay_s,
                    observation_mode=observation_mode,
                )
            has_near_band_sample = any(
                sample.regime == GovernanceRegime.NEAR_BAND for sample in samples
            )
            if (
                has_near_band_sample
                and max(u_pi_values) - min(u_pi_values)
                > FF_TRIM_PI_STABILITY_EPSILON
            ):
                return self._reject_completed_window(
                    "pi_unstable",
                    samples,
                    delay_s,
                    observation_mode=observation_mode,
                )

        causal_start = samples[0].observed_monotonic - delay_s
        causal_end = samples[-1].observed_monotonic - delay_s
        mean_power, coverage = self._mean_causal_power(causal_start, causal_end)
        if mean_power is None or coverage < FF_TRIM_MIN_POWER_COVERAGE:
            if not self._power_segments or self._power_segments[-1].end_monotonic < causal_end:
                return None
            return self._reject_completed_window(
                "causal_power_not_covered",
                samples,
                delay_s,
                coverage=coverage,
                observation_mode=observation_mode,
            )

        ownership_stats = self._ownership_stats(causal_start, causal_end)
        if observation_mode == "periodic":
            periodic_rejection = self._periodic_ownership_rejection_reason(
                samples=samples,
                stats=ownership_stats,
                a=a,
                b=b,
                current_trim=current_trim,
            )
            if periodic_rejection is not None:
                return self._reject_completed_window(
                    periodic_rejection,
                    samples,
                    delay_s,
                    coverage=coverage,
                    observation_mode=observation_mode,
                )

        mean_temperature = self._time_weighted_mean(
            samples,
            lambda sample: sample.temperature,
        )
        mean_target = self._time_weighted_mean(samples, lambda sample: sample.target)
        sampled_mean_ff1 = self._time_weighted_mean(
            samples,
            lambda sample: sample.ff1,
        )
        mean_ff1 = (
            ownership_stats.mean_ff1
            if ownership_stats is not None
            else sampled_mean_ff1
        )
        slope_per_min = (
            samples[-1].temperature - samples[0].temperature
        ) / (duration_s / 60.0)
        mean_slope_h = slope_per_min * 60.0
        mean_error = mean_target - mean_temperature

        if abs(mean_error) > FF_TRIM_MAX_ERROR_C:
            return self._reject_completed_window(
                f"error_{mean_error:.3f}",
                samples,
                delay_s,
                coverage=coverage,
                observation_mode=observation_mode,
            )
        if abs(mean_slope_h) > FF_TRIM_MAX_SLOPE_H:
            return self._reject_completed_window(
                f"slope_{mean_slope_h:.3f}",
                samples,
                delay_s,
                coverage=coverage,
                observation_mode=observation_mode,
            )

        observed_hold_power = (
            mean_power
            - slope_per_min / a
            + (b / a) * (mean_target - mean_temperature)
        )
        target_trim = observed_hold_power - mean_ff1
        correction = target_trim - current_trim
        physical_power_deficit = observed_hold_power - mean_power
        decomposed_correction = None
        transfer_eligible = False
        transfer_reason = "ownership_not_covered"
        transfer_quality = "unavailable"
        mean_p_power = None
        mean_i_power = None
        mean_visible_ff_power = None
        mean_ki = None
        mean_delivery_residual = None
        if ownership_stats is not None:
            mean_p_power = ownership_stats.mean_p
            mean_i_power = ownership_stats.mean_i
            mean_visible_ff_power = ownership_stats.mean_visible_ff
            mean_ki = ownership_stats.mean_ki
            mean_delivery_residual = mean_power - ownership_stats.mean_model_power
            transfer_quality = ownership_stats.quality
            if observation_mode == "stationary":
                decomposed_correction = physical_power_deficit + mean_i_power
                transfer_reason = self._transfer_rejection_reason(
                    samples=samples,
                    stats=ownership_stats,
                    a=a,
                    b=b,
                    mean_error=mean_error,
                    mean_slope_h=mean_slope_h,
                    mean_delivery_residual=mean_delivery_residual,
                ) or "quasi_equilibrium"
                transfer_eligible = transfer_reason == "quasi_equilibrium"
            else:
                transfer_reason = "periodic_equilibrium"

        if not all(
            isfinite(value)
            for value in (
                observed_hold_power,
                target_trim,
                correction,
                physical_power_deficit,
            )
        ):
            return self._reject_completed_window(
                "non_finite_result",
                samples,
                delay_s,
                coverage=coverage,
                observation_mode=observation_mode,
            )

        return CausalFFTrimResult(
            admissible=True,
            reason=(
                "periodic_window_ready"
                if observation_mode == "periodic"
                else "causal_window_ready"
            ),
            correction=correction,
            target_trim=target_trim,
            mean_causal_power=mean_power,
            mean_ff1=mean_ff1,
            mean_temperature=mean_temperature,
            mean_error=mean_error,
            mean_slope_h=mean_slope_h,
            observed_hold_power=observed_hold_power,
            duration_s=duration_s,
            measurement_count=len(samples),
            alignment_delay_s=delay_s,
            power_coverage_ratio=coverage,
            mean_p_power=mean_p_power,
            mean_i_power=mean_i_power,
            mean_visible_ff_power=mean_visible_ff_power,
            mean_ki=mean_ki,
            mean_delivery_residual=mean_delivery_residual,
            physical_power_deficit=physical_power_deficit,
            decomposed_correction=decomposed_correction,
            transfer_eligible=transfer_eligible,
            transfer_reason=transfer_reason,
            transfer_quality=transfer_quality,
            observation_mode=observation_mode,
        )

    @staticmethod
    def _periodic_ownership_rejection_reason(
        *,
        samples: Sequence[FFTrimThermalSample],
        stats: _OwnershipStats | None,
        a: float,
        b: float,
        current_trim: float,
    ) -> str | None:
        """Return why a phase-closed cycle is unsafe for periodic trim."""
        if stats is None:
            return "periodic_ownership_not_covered"
        allowed_regimes = frozenset(
            {
                GovernanceRegime.NEAR_BAND,
                GovernanceRegime.DEAD_BAND,
                GovernanceRegime.HOLD,
                GovernanceRegime.SATURATED,
            }
        )
        if not stats.regimes or not stats.regimes.issubset(allowed_regimes):
            return "periodic_regime_changed"
        disallowed_flags = stats.constraint_flags - {"saturated_low"}
        if disallowed_flags:
            return f"periodic_constraint_{sorted(disallowed_flags)[0]}"
        if len(stats.gain_generations) != 1:
            return "periodic_gains_changed"
        if (
            stats.ff1_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE
            or stats.trim_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE
            or abs(stats.mean_trim - current_trim)
            > FF_TRIM_TRANSFER_MAX_POWER_RANGE
        ):
            return "periodic_ff_changed"
        if stats.committed_range < FF_TRIM_PERIODIC_MIN_POWER_RANGE:
            return "periodic_power_range"
        outside_values = [
            sample.outside_temperature
            for sample in samples
            if sample.outside_temperature is not None
        ]
        if len(outside_values) != len(samples):
            return "periodic_outdoor_temperature_missing"
        projected_outdoor_range = abs(b / a) * (
            max(outside_values) - min(outside_values)
        )
        if projected_outdoor_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE:
            return "periodic_outdoor_temperature_unstable"
        return None

    def invalidate(
        self,
        reason: str,
        *,
        now_monotonic: float | None = None,
        washout_s: float | None = None,
    ) -> CausalFFTrimResult:
        """Discard the current window and optionally impose a causal washout."""
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        if now_monotonic is not None and washout_s is not None:
            self._washout_until_monotonic = max(
                self._washout_until_monotonic,
                float(now_monotonic) + max(float(washout_s), 0.0),
            )
        result = self._empty_result(reason, measurement_count=0)
        self.last_result = result
        self.state = "rejected"
        self.last_reject_reason = reason
        self.last_update_reason = "skipped"
        return result

    def reset_after_trim_update(
        self,
        *,
        now_monotonic: float,
        washout_s: float,
    ) -> None:
        """Start a fresh stationary window after the reference trim changes."""
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        self._washout_until_monotonic = max(
            self._washout_until_monotonic,
            float(now_monotonic) + max(float(washout_s), 0.0),
        )
        self.state = "waiting_deadtime"

    def reset_runtime(self) -> None:
        """Clear transient observation state without changing persisted trim."""
        self._power_segments.clear()
        self._ownership_segments.clear()
        self._clear_active_cycle()
        self._thermal_samples.clear()
        self._last_measurement_id = None
        self._window_deadtime_s = None
        self._washout_until_monotonic = 0.0
        self.state = "warming_up"
        self.last_reject_reason = "none"
        self.last_update_reason = "none"
        self.last_result = self._empty_result(
            "warming_up",
            measurement_count=0,
        )
        self._last_admissible_result = None

    @property
    def diagnostics(self) -> dict[str, float | int | str | None]:
        """Return the observer state without exposing mutable internals."""
        current_result = self.last_result
        result = self._last_admissible_result or current_result
        has_samples = bool(self._thermal_samples)
        duration_s = 0.0
        if len(self._thermal_samples) >= 2:
            duration_s = (
                self._thermal_samples[-1].observed_monotonic
                - self._thermal_samples[0].observed_monotonic
            )
        return {
            "state": self.state,
            "window_duration_s": (
                duration_s if has_samples else current_result.duration_s
            ),
            "window_target_duration_s": self.target_duration_s,
            "measurement_count": (
                len(self._thermal_samples)
                if has_samples
                else current_result.measurement_count
            ),
            "current_alignment_delay_s": (
                self._window_deadtime_s if has_samples else None
            ),
            "alignment_delay_s": (
                self._window_deadtime_s
                if has_samples
                else current_result.alignment_delay_s
            ),
            "power_coverage_ratio": (
                0.0 if has_samples else current_result.power_coverage_ratio
            ),
            "mean_causal_power": result.mean_causal_power,
            "mean_ff1": result.mean_ff1,
            "mean_temperature": result.mean_temperature,
            "mean_error": result.mean_error,
            "mean_slope_h": result.mean_slope_h,
            "observed_hold_power": result.observed_hold_power,
            "target_trim": result.target_trim,
            "correction": result.correction,
            "mean_p_power": result.mean_p_power,
            "mean_i_power": result.mean_i_power,
            "mean_visible_ff_power": result.mean_visible_ff_power,
            "mean_ki": result.mean_ki,
            "mean_delivery_residual": result.mean_delivery_residual,
            "physical_power_deficit": result.physical_power_deficit,
            "decomposed_correction": result.decomposed_correction,
            "transfer_eligible": result.transfer_eligible,
            "transfer_reason": result.transfer_reason,
            "transfer_quality": result.transfer_quality,
            "last_reject_reason": self.last_reject_reason,
            "last_update_reason": self.last_update_reason,
        }

    def _sample_rejection_reason(
        self,
        sample: FFTrimThermalSample,
    ) -> str | None:
        values = (
            sample.observed_monotonic,
            sample.temperature,
            sample.target,
            sample.ff1,
        )
        if not all(isfinite(float(value)) for value in values):
            return "invalid_measurement"
        if not sample.outside_temperature_available:
            return "missing_outdoor_temperature"
        if not sample.model_reliable:
            return "model_confidence"
        if sample.trim_frozen_reason not in (None, "none"):
            return f"frozen_{sample.trim_frozen_reason}"
        if sample.setpoint_changed:
            return "setpoint_changed"
        if sample.trajectory_active:
            return "trajectory_active"
        if sample.ff3_active:
            return "ff3_active"
        if sample.saturated:
            return "saturated"

        pi_eligibility = evaluate_pi_eligibility_for_trim(
            sample.regime,
            sample.i_mode,
            sample.u_pi,
            sample.u_pi,
        )
        if not pi_eligibility.admissible:
            return pi_eligibility.reason

        signed_error = sample.target - sample.temperature
        if abs(signed_error) > FF_TRIM_MAX_ERROR_C:
            return f"error_{signed_error:.3f}"
        return None

    @staticmethod
    def _model_rejection_reason(
        hvac_mode: str,
        a: float,
        b: float,
    ) -> str | None:
        if not isfinite(float(a)) or not isfinite(float(b)):
            return "invalid_model"
        if float(b) < 0.0 or abs(float(a)) <= 1e-9:
            return "invalid_model_sign"
        if hvac_mode == "heat" and float(a) <= 0.0:
            return "invalid_model_sign"
        if hvac_mode == "cool" and float(a) >= 0.0:
            return "invalid_model_sign"
        if hvac_mode not in {"heat", "cool"}:
            return "invalid_hvac_mode"
        return None

    def _mean_causal_power(
        self,
        start: float,
        end: float,
    ) -> tuple[float | None, float]:
        duration = end - start
        if duration <= 0.0:
            return None, 0.0
        weighted_power = 0.0
        covered = 0.0
        for segment in self._power_segments:
            overlap_start = max(start, segment.start_monotonic)
            overlap_end = min(end, segment.end_monotonic)
            if overlap_end <= overlap_start:
                continue
            overlap = overlap_end - overlap_start
            weighted_power += overlap * segment.linear_power
            covered += overlap
        coverage = clamp(covered / duration, 0.0, 1.0)
        if covered <= 0.0:
            return None, coverage
        return weighted_power / covered, coverage

    @staticmethod
    def _time_weighted_mean(
        samples: Sequence[FFTrimThermalSample],
        value_getter: Callable[[FFTrimThermalSample], float],
    ) -> float:
        duration = samples[-1].observed_monotonic - samples[0].observed_monotonic
        if duration <= 0.0:
            return float(value_getter(samples[-1]))
        integral = 0.0
        for previous, current in zip(samples, samples[1:]):
            dt_s = current.observed_monotonic - previous.observed_monotonic
            integral += (
                float(value_getter(previous)) + float(value_getter(current))
            ) * 0.5 * dt_s
        return integral / duration

    def _ownership_stats(
        self,
        start: float,
        end: float,
    ) -> _OwnershipStats | None:
        """Aggregate control ownership over the same causal power interval."""
        duration = end - start
        if duration <= 0.0:
            return None

        weighted = {
            "ff1": 0.0,
            "trim": 0.0,
            "visible_ff": 0.0,
            "ff3": 0.0,
            "p": 0.0,
            "i": 0.0,
            "ki": 0.0,
            "model": 0.0,
            "committed": 0.0,
        }
        covered = 0.0
        ff1_values: list[float] = []
        trim_values: list[float] = []
        p_values: list[float] = []
        i_values: list[float] = []
        ki_values: list[float] = []
        residual_values: list[float] = []
        committed_values: list[float] = []
        regimes: set[GovernanceRegime | str | None] = set()
        i_modes: set[str | None] = set()
        flags: set[str] = set()
        generations: set[int] = set()
        qualities: set[str] = set()

        for segment in self._ownership_segments:
            overlap_start = max(start, segment.start_monotonic)
            overlap_end = min(end, segment.end_monotonic)
            if overlap_end <= overlap_start:
                continue
            overlap = overlap_end - overlap_start
            item = segment.ownership
            values = (
                item.u_ff1,
                item.trim_stored,
                item.u_ff_visible,
                item.u_ff3,
                item.u_p,
                item.u_i,
                item.ki,
                item.u_cmd,
                item.u_limited,
                item.linear_committed_power,
            )
            if not all(isfinite(float(value)) for value in values):
                return None
            model_power = item.u_ff_visible + item.u_ff3 + item.u_p + item.u_i
            weighted["ff1"] += overlap * item.u_ff1
            weighted["trim"] += overlap * item.trim_stored
            weighted["visible_ff"] += overlap * item.u_ff_visible
            weighted["ff3"] += overlap * item.u_ff3
            weighted["p"] += overlap * item.u_p
            weighted["i"] += overlap * item.u_i
            weighted["ki"] += overlap * item.ki
            weighted["model"] += overlap * model_power
            weighted["committed"] += overlap * item.linear_committed_power
            covered += overlap
            ff1_values.append(item.u_ff1)
            trim_values.append(item.trim_stored)
            p_values.append(item.u_p)
            i_values.append(item.u_i)
            ki_values.append(item.ki)
            residual_values.append(item.linear_committed_power - model_power)
            committed_values.append(item.linear_committed_power)
            regimes.add(item.regime)
            i_modes.add(item.i_mode)
            flags.update(item.constraint_flags)
            generations.add(item.gain_generation)
            qualities.add(item.quality)

        coverage = clamp(covered / duration, 0.0, 1.0)
        if covered <= 0.0 or coverage < FF_TRIM_MIN_POWER_COVERAGE:
            return None

        def _mean(name: str) -> float:
            return weighted[name] / covered

        def _range(values: Sequence[float]) -> float:
            return max(values) - min(values) if values else 0.0

        significant_i = [
            value
            for value in i_values
            if abs(value) > FF_TRIM_TRANSFER_COMMAND_EPSILON
        ]
        i_sign_persistent = not significant_i or all(
            value * significant_i[0] > 0.0 for value in significant_i
        )
        quality = next(iter(qualities)) if len(qualities) == 1 else "mixed"
        return _OwnershipStats(
            coverage=coverage,
            mean_ff1=_mean("ff1"),
            mean_trim=_mean("trim"),
            mean_visible_ff=_mean("visible_ff"),
            mean_ff3=_mean("ff3"),
            mean_p=_mean("p"),
            mean_i=_mean("i"),
            mean_ki=_mean("ki"),
            mean_model_power=_mean("model"),
            mean_committed_power=_mean("committed"),
            max_delivery_residual=max(abs(value) for value in residual_values),
            ff1_range=_range(ff1_values),
            trim_range=_range(trim_values),
            p_range=_range(p_values),
            i_range=_range(i_values),
            i_drift=abs(i_values[-1] - i_values[0]),
            committed_range=_range(committed_values),
            ki_min=min(ki_values),
            i_sign_persistent=i_sign_persistent,
            regimes=frozenset(regimes),
            i_modes=frozenset(i_modes),
            constraint_flags=frozenset(flags),
            gain_generations=frozenset(generations),
            quality=quality,
        )

    @staticmethod
    def _transfer_rejection_reason(
        *,
        samples: Sequence[FFTrimThermalSample],
        stats: _OwnershipStats,
        a: float,
        b: float,
        mean_error: float,
        mean_slope_h: float,
        mean_delivery_residual: float,
    ) -> str | None:
        """Return why ownership transfer is unsafe for this accepted window."""
        if stats.regimes != frozenset({GovernanceRegime.DEAD_BAND}):
            return "transfer_not_deadband"
        if stats.i_modes != frozenset({"I:FREEZE(deadband)"}):
            return "transfer_i_not_frozen"
        if stats.constraint_flags:
            return f"transfer_constraint_{sorted(stats.constraint_flags)[0]}"
        if abs(stats.mean_ff3) > FF_TRIM_TRANSFER_COMMAND_EPSILON:
            return "transfer_ff3_active"

        errors = [sample.target - sample.temperature for sample in samples]
        temperatures = [sample.temperature for sample in samples]
        steps = [
            abs(current - previous)
            for previous, current in zip(temperatures, temperatures[1:])
            if abs(current - previous) > 1e-9
        ]
        quantum = min(steps) if steps else 0.0
        signed_steps = [
            current - previous
            for previous, current in zip(temperatures, temperatures[1:])
        ]
        sigma_temperature = 0.0
        if signed_steps:
            step_median = float(median(signed_steps))
            sigma_temperature = 1.4826 * float(
                median(abs(value - step_median) for value in signed_steps)
            )
        duration_h = max(
            (samples[-1].observed_monotonic - samples[0].observed_monotonic)
            / 3600.0,
            1e-9,
        )
        epsilon_error = max(
            0.001,
            quantum / 2.0,
            3.0 * sigma_temperature / sqrt(len(samples)),
        )
        epsilon_slope = max(
            0.001,
            quantum / duration_h,
            3.0 * sqrt(2.0) * sigma_temperature / duration_h,
        )
        if (
            epsilon_error > FF_TRIM_TRANSFER_MAX_ERROR_C
            or epsilon_slope > FF_TRIM_TRANSFER_MAX_SLOPE_H
        ):
            return "transfer_temperature_noise"
        if abs(mean_error) > epsilon_error:
            return "transfer_mean_error"
        if max(abs(value) for value in errors) > FF_TRIM_TRANSFER_MAX_ERROR_C:
            return "transfer_sample_error"
        if abs(mean_slope_h) > epsilon_slope:
            return "transfer_slope"

        outside_values = [
            sample.outside_temperature
            for sample in samples
            if sample.outside_temperature is not None
        ]
        if len(outside_values) != len(samples):
            return "transfer_outdoor_temperature_missing"
        projected_outdoor_range = abs(b / a) * (
            max(outside_values) - min(outside_values)
        )
        if projected_outdoor_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE:
            return "transfer_outdoor_temperature_unstable"
        if (
            stats.ff1_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE
            or stats.trim_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE
        ):
            return "transfer_ff_unstable"
        if (
            abs(stats.mean_p) > FF_TRIM_TRANSFER_MAX_P_POWER
            or stats.p_range > 2.0 * FF_TRIM_TRANSFER_MAX_POWER_RANGE
        ):
            return "transfer_p_active"
        if (
            stats.i_range > FF_TRIM_TRANSFER_MAX_POWER_RANGE
            or stats.i_drift > FF_TRIM_TRANSFER_MAX_POWER_RANGE
            or not stats.i_sign_persistent
        ):
            return "transfer_i_unstable"
        if stats.ki_min < KI_MIN:
            return "transfer_invalid_ki"
        if (
            abs(mean_delivery_residual)
            > FF_TRIM_TRANSFER_MAX_DELIVERY_RESIDUAL
            or stats.max_delivery_residual
            > 2.0 * FF_TRIM_TRANSFER_MAX_DELIVERY_RESIDUAL
        ):
            return "transfer_delivery_residual"
        return None

    def _reject_completed_window(
        self,
        reason: str,
        samples: Sequence[FFTrimThermalSample],
        delay_s: float,
        *,
        coverage: float = 0.0,
        observation_mode: str = "stationary",
    ) -> CausalFFTrimResult:
        """Build one rejected result without consuming shared trace data."""
        duration_s = samples[-1].observed_monotonic - samples[0].observed_monotonic
        return CausalFFTrimResult(
            admissible=False,
            reason=reason,
            correction=None,
            target_trim=None,
            mean_causal_power=None,
            mean_ff1=None,
            mean_temperature=None,
            mean_error=None,
            mean_slope_h=None,
            observed_hold_power=None,
            duration_s=duration_s,
            measurement_count=len(samples),
            alignment_delay_s=delay_s,
            power_coverage_ratio=coverage,
            observation_mode=observation_mode,
        )

    @staticmethod
    def _empty_result(
        reason: str,
        *,
        measurement_count: int,
    ) -> CausalFFTrimResult:
        return CausalFFTrimResult(
            admissible=False,
            reason=reason,
            correction=None,
            target_trim=None,
            mean_causal_power=None,
            mean_ff1=None,
            mean_temperature=None,
            mean_error=None,
            mean_slope_h=None,
            observed_hold_power=None,
            duration_s=0.0,
            measurement_count=measurement_count,
            alignment_delay_s=None,
            power_coverage_ratio=0.0,
        )

    def _prune_power_history(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self._POWER_HISTORY_MAX_S
        self._discard_power_before(cutoff)

    def _discard_power_before(self, cutoff: float) -> None:
        while (
            self._power_segments
            and self._power_segments[0].end_monotonic <= cutoff
        ):
            self._power_segments.popleft()
        if (
            self._power_segments
            and self._power_segments[0].start_monotonic < cutoff
            < self._power_segments[0].end_monotonic
        ):
            first = self._power_segments[0]
            self._power_segments[0] = AppliedPowerSegment(
                cutoff,
                first.end_monotonic,
                first.linear_power,
            )
        while (
            self._ownership_segments
            and self._ownership_segments[0].end_monotonic <= cutoff
        ):
            self._ownership_segments.popleft()
        if (
            self._ownership_segments
            and self._ownership_segments[0].start_monotonic < cutoff
            < self._ownership_segments[0].end_monotonic
        ):
            first_ownership = self._ownership_segments[0]
            self._ownership_segments[0] = ControlOwnershipSegment(
                cutoff,
                first_ownership.end_monotonic,
                first_ownership.ownership,
            )

    def _record_ownership_segment(self, segment: ControlOwnershipSegment) -> None:
        """Append one non-overlapping ownership segment."""
        start = float(segment.start_monotonic)
        end = float(segment.end_monotonic)
        if not isfinite(start) or not isfinite(end) or end <= start:
            return
        if self._ownership_segments:
            previous = self._ownership_segments[-1]
            start = max(start, previous.end_monotonic)
            if end <= start:
                return
            if (
                abs(start - previous.end_monotonic) <= 1e-6
                and previous.ownership == segment.ownership
            ):
                self._ownership_segments[-1] = ControlOwnershipSegment(
                    previous.start_monotonic,
                    end,
                    segment.ownership,
                )
                return
        self._ownership_segments.append(
            ControlOwnershipSegment(start, end, segment.ownership)
        )

    def _clear_active_cycle(self) -> None:
        self._active_cycle_segments.clear()
        self._active_ownership_segments.clear()
        self._active_power_start = None
        self._active_linear_power = None
        self._active_ownership_start = None
        self._active_ownership = None


def evaluate_pi_eligibility_for_trim(
    regime: GovernanceRegime | str | None,
    i_mode: str | None,
    u_pi: float | None,
    previous_u_pi: float | None,
) -> FFTrimPIEligibility:
    """Validate that PI is neutral enough for trim learning."""
    if regime == GovernanceRegime.DEAD_BAND:
        if i_mode == "I:FREEZE(deadband)":
            return FFTrimPIEligibility(True, "pi_deadband_freeze")
        return FFTrimPIEligibility(False, f"pi_mode_{i_mode}")

    if regime != GovernanceRegime.NEAR_BAND:
        regime_value = regime.value if isinstance(regime, GovernanceRegime) else regime
        return FFTrimPIEligibility(False, f"regime_{regime_value}")

    if i_mode is None:
        return FFTrimPIEligibility(False, "pi_missing_mode")

    blocked_prefixes = (
        "I:GUARD",
        "I:CLAMP",
        "I:SKIP",
        "I:RESET",
        "I:BLEED",
        "I:HOLD",
    )
    if i_mode.startswith(blocked_prefixes):
        return FFTrimPIEligibility(False, f"pi_mode_{i_mode}")

    if u_pi is None or previous_u_pi is None:
        return FFTrimPIEligibility(False, "pi_pending_stability")

    if abs(u_pi - previous_u_pi) > FF_TRIM_PI_STABILITY_EPSILON:
        return FFTrimPIEligibility(False, "pi_unstable")

    return FFTrimPIEligibility(True, "pi_near_band_stable")


def prepare_bumpless_transfer(
    *,
    current_trim: float,
    current_ff1: float,
    current_i_power: float,
    observed_i_bias: float,
    physical_power_deficit: float,
    current_ki: float,
    current_raw_command: float,
) -> FFTrimBumplessPlan:
    """Prepare one bounded trim/I ownership transaction without mutation."""
    values = (
        current_trim,
        current_ff1,
        current_i_power,
        observed_i_bias,
        physical_power_deficit,
        current_ki,
        current_raw_command,
    )
    if not all(isfinite(float(value)) for value in values):
        return FFTrimBumplessPlan(False, "rejected", "non_finite_transfer")
    if float(current_ki) < KI_MIN:
        return FFTrimBumplessPlan(False, "rejected", "invalid_ki")

    trim = float(current_trim)
    ff1 = float(current_ff1)
    i_now = float(current_i_power)
    i_bias = float(observed_i_bias)
    deficit = float(physical_power_deficit)
    raw_command = float(current_raw_command)
    old_ff_raw = ff1 + trim
    margin = FF_TRIM_TRANSFER_COMMAND_EPSILON
    if old_ff_raw <= margin or old_ff_raw >= 1.0 - margin:
        return FFTrimBumplessPlan(False, "rejected", "ff_branch_clamped")
    if raw_command < -margin or raw_command > 1.0 + margin:
        return FFTrimBumplessPlan(False, "rejected", "command_clamped")

    transferable_bias = (
        0.0 if abs(i_bias) <= FF_TRIM_TRANSFER_COMMAND_EPSILON else i_bias
    )
    requested_i_transfer = FF_TRIM_LAMBDA * transferable_bias
    physical_delta = FF_TRIM_LAMBDA * deficit
    requested_trim_delta = requested_i_transfer + physical_delta
    opposing_ownership_compaction = (
        abs(requested_i_transfer) > 1e-12
        and trim * requested_i_transfer < 0.0
        and abs(trim + requested_trim_delta) < abs(trim)
    )
    if (
        abs(requested_trim_delta) <= FF_TRIM_DELTA_EPSILON
        and abs(requested_i_transfer) <= FF_TRIM_DELTA_EPSILON
        and not opposing_ownership_compaction
    ):
        return FFTrimBumplessPlan(
            False,
            "quiet",
            "quiet_trim_delta",
            requested_trim_delta=requested_trim_delta,
            requested_i_transfer=requested_i_transfer,
            physical_power_deficit=deficit,
        )

    if abs(requested_i_transfer) > 1e-12:
        if i_now * requested_i_transfer <= 0.0:
            return FFTrimBumplessPlan(
                False,
                "rejected",
                "integral_sign_changed",
                requested_trim_delta=requested_trim_delta,
                requested_i_transfer=requested_i_transfer,
                transferable_i_power=i_now,
                physical_power_deficit=deficit,
            )
        if abs(i_now - i_bias) > FF_TRIM_TRANSFER_MAX_POWER_RANGE:
            return FFTrimBumplessPlan(
                False,
                "rejected",
                "integral_bias_changed",
                requested_trim_delta=requested_trim_delta,
                requested_i_transfer=requested_i_transfer,
                transferable_i_power=i_now,
                physical_power_deficit=deficit,
            )

    authority = FF_TRIM_RHO * max(ff1, FF_TRIM_EPSILON)
    alpha_limits = [1.0]
    if requested_trim_delta > 0.0:
        alpha_limits.extend(
            (
                (authority - trim) / requested_trim_delta,
                (1.0 - margin - old_ff_raw) / requested_trim_delta,
            )
        )
    elif requested_trim_delta < 0.0:
        alpha_limits.extend(
            (
                (trim + authority) / -requested_trim_delta,
                (old_ff_raw - margin) / -requested_trim_delta,
            )
        )
    if abs(requested_i_transfer) > 1e-12:
        alpha_limits.append(abs(i_now) / abs(requested_i_transfer))
    if physical_delta > 0.0:
        alpha_limits.append((1.0 - margin - raw_command) / physical_delta)
    elif physical_delta < 0.0:
        alpha_limits.append((raw_command - margin) / -physical_delta)

    alpha = clamp(min(alpha_limits), 0.0, 1.0)
    if alpha <= 1e-12:
        return FFTrimBumplessPlan(
            False,
            "rejected",
            "no_atomic_headroom",
            requested_trim_delta=requested_trim_delta,
            requested_i_transfer=requested_i_transfer,
            transferable_i_power=i_now,
            physical_power_deficit=deficit,
        )

    stored_delta = alpha * requested_trim_delta
    applied_i_transfer = alpha * requested_i_transfer
    new_trim = trim + stored_delta
    old_visible = clamp(old_ff_raw, 0.0, 1.0)
    new_visible = clamp(ff1 + new_trim, 0.0, 1.0)
    visible_delta = new_visible - old_visible
    net_delta = visible_delta - applied_i_transfer
    expected_net_delta = alpha * physical_delta
    if (
        abs(visible_delta - stored_delta) > 1e-12
        or abs(net_delta - expected_net_delta) > 1e-12
    ):
        return FFTrimBumplessPlan(
            False,
            "rejected",
            "transfer_invariant",
            alpha=alpha,
            old_trim=trim,
            new_trim=trim,
            requested_trim_delta=requested_trim_delta,
            requested_i_transfer=requested_i_transfer,
            transferable_i_power=i_now,
            physical_power_deficit=deficit,
        )

    return FFTrimBumplessPlan(
        True,
        "ready",
        "quasi_equilibrium",
        alpha=alpha,
        old_trim=trim,
        new_trim=new_trim,
        requested_trim_delta=requested_trim_delta,
        stored_trim_delta=stored_delta,
        visible_ff_delta=visible_delta,
        transferable_i_power=i_now,
        requested_i_transfer=requested_i_transfer,
        applied_i_transfer=applied_i_transfer,
        physical_power_deficit=deficit,
        net_command_delta=net_delta,
    )


class FFTrim:
    """Slow trim correction on the FF principal u_ff_ab."""

    u_ff_trim: float
    frozen: bool
    freeze_reason: str

    def __init__(self) -> None:
        self.u_ff_trim: float = 0.0
        self.frozen: bool = False
        self.freeze_reason: str = "none"
        self._pending_proposals: Deque[FFTrimWindowProposal] = deque(
            maxlen=FF_TRIM_BUFFER_SIZE
        )
        self._periodic_pending_proposals: Deque[FFTrimWindowProposal] = deque(
            maxlen=FF_TRIM_BUFFER_SIZE
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, delta_power: float, u_ff_ab: float) -> None:
        """Update the trim using the observed power deficit.

        The signal is the difference between the causally observed trim target
        and the trim already applied over an independent thermal window.

        Args:
            delta_power: incremental power correction needed
                (positive = need more power).
            u_ff_ab: Current FF principal value (used to compute authority budget).
        """
        result = self._apply_correction(
            median_correction=float(delta_power),
            u_ff_ab=float(u_ff_ab),
            pending_count=len(self._pending_proposals),
        )

        _LOGGER.debug(
            "FFTrim: update reason=%s trim=%.4f correction=%.4f visible_delta=%.4f",
            result.reason,
            self.u_ff_trim,
            delta_power,
            result.visible_ff_delta,
        )

    def collect_persistent(
        self,
        proposal: FFTrimWindowProposal,
    ) -> FFTrimPersistentResult:
        """Collect one causal proposal in its observation-mode buffer."""
        observation_mode = proposal.observation_mode
        pending_proposals = self._pending_buffer(observation_mode)
        if self.frozen:
            self.clear_pending(observation_mode)
            return FFTrimPersistentResult(
                False,
                f"frozen_{self.freeze_reason}",
                0,
                observation_mode=observation_mode,
            )
        if not isfinite(float(proposal.correction)) or not isfinite(
            float(proposal.mean_ff1)
        ):
            self.clear_pending(observation_mode)
            return FFTrimPersistentResult(
                False,
                "invalid_proposal",
                0,
                observation_mode=observation_mode,
            )
        if proposal.transfer_eligible and (
            proposal.physical_power_deficit is None
            or proposal.integral_bias is None
            or not isfinite(float(proposal.physical_power_deficit))
            or not isfinite(float(proposal.integral_bias))
        ):
            self.clear_pending(observation_mode)
            return FFTrimPersistentResult(
                False,
                "invalid_transfer_proposal",
                0,
                observation_mode=observation_mode,
            )

        transfer_signal_present = proposal.transfer_eligible and (
            abs(float(proposal.physical_power_deficit)) > FF_TRIM_DELTA_EPSILON
            or abs(float(proposal.integral_bias)) > FF_TRIM_DELTA_EPSILON
        )
        if (
            abs(proposal.correction) <= FF_TRIM_DELTA_EPSILON
            and not transfer_signal_present
        ):
            self.clear_pending(observation_mode)
            return FFTrimPersistentResult(
                False,
                "quiet_delta",
                0,
                observation_mode=observation_mode,
            )

        def direction(value: float | None) -> int:
            if value is None or abs(float(value)) <= FF_TRIM_DELTA_EPSILON:
                return 0
            return 1 if float(value) > 0.0 else -1

        proposal_direction = (
            direction(proposal.correction),
            direction(proposal.physical_power_deficit),
            direction(proposal.integral_bias),
        )
        if pending_proposals:
            previous = pending_proposals[-1]
            previous_direction = (
                direction(previous.correction),
                direction(previous.physical_power_deficit),
                direction(previous.integral_bias),
            )
            same_transfer_context = (
                previous.transfer_eligible == proposal.transfer_eligible
                and previous.transfer_reason == proposal.transfer_reason
            )
            if (
                previous_direction != proposal_direction
                or not same_transfer_context
            ):
                self.clear_pending(observation_mode)

        pending_proposals.append(proposal)
        pending_count = len(pending_proposals)
        if pending_count < FF_TRIM_PERSISTENCE:
            return FFTrimPersistentResult(
                False,
                f"pending_{pending_count}/{FF_TRIM_PERSISTENCE}",
                pending_count,
                observation_mode=observation_mode,
            )

        proposals = tuple(pending_proposals)
        transfer_eligible = all(
            item.transfer_eligible
            and item.physical_power_deficit is not None
            and item.integral_bias is not None
            for item in proposals
        )
        physical_deficit = (
            float(median(item.physical_power_deficit for item in proposals))
            if transfer_eligible
            else None
        )
        integral_bias = (
            float(median(item.integral_bias for item in proposals))
            if transfer_eligible
            else None
        )
        return FFTrimPersistentResult(
            True,
            "persistent_ready",
            pending_count,
            median_correction=float(median(item.correction for item in proposals)),
            median_ff1=float(median(item.mean_ff1 for item in proposals)),
            median_physical_power_deficit=physical_deficit,
            median_integral_bias=integral_bias,
            transfer_eligible=transfer_eligible,
            transfer_reason=(
                "quasi_equilibrium"
                if transfer_eligible
                else proposal.transfer_reason
            ),
            observation_mode=observation_mode,
        )

    def update_persistent(
        self,
        delta_power: float,
        u_ff_ab: float,
    ) -> FFTrimUpdateResult:
        """Update trim only after same-direction thermal corrections persist."""
        persistent = self.collect_persistent(
            FFTrimWindowProposal(
                correction=float(delta_power),
                mean_ff1=float(u_ff_ab),
                physical_power_deficit=None,
                integral_bias=None,
                transfer_eligible=False,
                transfer_reason="causal_update_without_transfer",
                observation_mode="stationary",
            )
        )
        if not persistent.ready:
            return FFTrimUpdateResult(
                False,
                persistent.reason,
                0.0,
                persistent.pending_count,
            )
        return self.apply_persistent_result(
            persistent,
            u_ff_ab=float(u_ff_ab),
        )

    def apply_persistent_result(
        self,
        persistent: FFTrimPersistentResult,
        *,
        u_ff_ab: float,
    ) -> FFTrimUpdateResult:
        """Apply one robust persistent correction without integral transfer."""
        if not persistent.ready:
            return FFTrimUpdateResult(
                False,
                persistent.reason,
                0.0,
                persistent.pending_count,
            )
        return self._apply_correction(
            median_correction=persistent.median_correction,
            u_ff_ab=float(u_ff_ab),
            pending_count=persistent.pending_count,
        )

    def _apply_correction(
        self,
        *,
        median_correction: float,
        u_ff_ab: float,
        pending_count: int,
    ) -> FFTrimUpdateResult:
        """Apply lambda and clamps, returning stored and visible deltas."""
        if self.frozen:
            return FFTrimUpdateResult(
                False,
                f"frozen_{self.freeze_reason}",
                0.0,
                pending_count,
                median_correction=median_correction,
            )
        old_trim = self.u_ff_trim
        requested_delta = FF_TRIM_LAMBDA * median_correction
        authority = FF_TRIM_RHO * max(u_ff_ab, FF_TRIM_EPSILON)
        new_trim = clamp(old_trim + requested_delta, -authority, authority)
        stored_delta = new_trim - old_trim
        old_visible = clamp(u_ff_ab + old_trim, 0.0, 1.0)
        new_visible = clamp(u_ff_ab + new_trim, 0.0, 1.0)
        visible_delta = new_visible - old_visible
        if abs(visible_delta - stored_delta) > 1e-12:
            return FFTrimUpdateResult(
                False,
                "ff_branch_clamped",
                0.0,
                pending_count,
                median_correction=median_correction,
                requested_trim_delta=requested_delta,
                stored_trim_delta=0.0,
                visible_ff_delta=0.0,
            )
        if abs(stored_delta) <= 1e-12:
            return FFTrimUpdateResult(
                False,
                "trim_clamped",
                0.0,
                pending_count,
                median_correction=median_correction,
                requested_trim_delta=requested_delta,
                stored_trim_delta=0.0,
                visible_ff_delta=0.0,
            )
        self.u_ff_trim = new_trim
        self.clear_pending()
        return FFTrimUpdateResult(
            True,
            "updated_persistent",
            visible_delta,
            pending_count,
            median_correction=median_correction,
            requested_trim_delta=requested_delta,
            stored_trim_delta=stored_delta,
            visible_ff_delta=visible_delta,
        )

    def commit_bumpless_plan(self, plan: FFTrimBumplessPlan) -> bool:
        """Commit a previously prepared trim side without recomputing it."""
        if (
            not plan.applicable
            or abs(self.u_ff_trim - plan.old_trim) > 1e-12
        ):
            return False
        self.u_ff_trim = plan.new_trim
        return True

    def rollback_bumpless_plan(self, plan: FFTrimBumplessPlan) -> None:
        """Restore the trim side after an unexpected integral-side failure."""
        if abs(self.u_ff_trim - plan.new_trim) <= 1e-12:
            self.u_ff_trim = plan.old_trim

    def clear_pending(self, observation_mode: str | None = None) -> None:
        """Discard pending proposals for one mode or every mode."""
        if observation_mode is None:
            self._pending_proposals.clear()
            self._periodic_pending_proposals.clear()
            return
        self._pending_buffer(observation_mode).clear()

    def _pending_buffer(
        self,
        observation_mode: str,
    ) -> Deque[FFTrimWindowProposal]:
        """Return the persistence buffer belonging to one observer."""
        if observation_mode == "periodic":
            return self._periodic_pending_proposals
        return self._pending_proposals

    def compute_ff_base(self, u_ff_ab: float) -> float:
        """Return u_ff_base = clamp(u_ff_ab + u_ff_trim, 0, 1)."""
        return clamp(u_ff_ab + self.u_ff_trim, 0.0, 1.0)

    def freeze(self, reason: str) -> None:
        """Freeze trim updates."""
        if not self.frozen:
            _LOGGER.debug("FFTrim: frozen (%s)", reason)
        self.frozen = True
        self.freeze_reason = reason
        self.clear_pending()

    def unfreeze(self) -> None:
        """Unfreeze trim updates."""
        if self.frozen:
            _LOGGER.debug("FFTrim: unfrozen")
        self.frozen = False
        self.freeze_reason = "none"

    def reset(self) -> None:
        """Full reset of trim state."""
        self.u_ff_trim = 0.0
        self.frozen = False
        self.freeze_reason = "none"
        self.clear_pending()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_state(self) -> dict:
        return {"u_ff_trim": self.u_ff_trim}

    def load_state(self, state: dict) -> None:
        self.u_ff_trim = float(state.get("u_ff_trim", 0.0))
        self.clear_pending()
