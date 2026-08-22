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
from math import isfinite
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
    FF_TRIM_MAX_ERROR_C,
    FF_TRIM_MAX_SLOPE_H,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FFTrimUpdateResult:
    """Result of one persistent trim update attempt."""

    updated: bool
    reason: str
    applied_delta: float
    pending_count: int


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
    trim_frozen_reason: str | None = None


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


class CausalFFTrimObserver:
    """Estimate slow FF bias from causally aligned thermal windows."""

    _POWER_HISTORY_MAX_S = 24.0 * 60.0 * 60.0
    _MAX_CONTINUITY_GAP_S = 5.0

    def __init__(self, cycle_min: float) -> None:
        self._cycle_min = max(float(cycle_min), 0.0)
        self._power_segments: Deque[AppliedPowerSegment] = deque()
        self._active_cycle_segments: list[AppliedPowerSegment] = []
        self._active_power_start: float | None = None
        self._active_linear_power: float | None = None
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
    ) -> None:
        """Start the transient trace for one physical scheduler cycle."""
        self._active_cycle_segments.clear()
        self._active_power_start = float(now_monotonic)
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)

    def update_applied_power(
        self,
        *,
        now_monotonic: float,
        linear_power: float,
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
        self._active_power_start = now
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)

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

        model_rejection = self._model_rejection_reason(samples[-1].hvac_mode, a, b)
        if model_rejection is not None:
            return self._reject_completed_window(
                model_rejection,
                samples,
                delay_s,
            )

        target_span = max(sample.target for sample in samples) - min(
            sample.target for sample in samples
        )
        if target_span > 1e-6:
            return self._reject_completed_window(
                "setpoint_changed",
                samples,
                delay_s,
            )

        modes = {sample.hvac_mode for sample in samples}
        if len(modes) != 1:
            return self._reject_completed_window(
                "hvac_mode_changed",
                samples,
                delay_s,
            )

        u_pi_values = [sample.u_pi for sample in samples if sample.u_pi is not None]
        if len(u_pi_values) != len(samples):
            return self._reject_completed_window(
                "pi_missing",
                samples,
                delay_s,
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
            )

        causal_start = samples[0].observed_monotonic - delay_s
        causal_end = samples[-1].observed_monotonic - delay_s
        mean_power, coverage = self._mean_causal_power(causal_start, causal_end)
        if mean_power is None or coverage < FF_TRIM_MIN_POWER_COVERAGE:
            if self._power_segments[-1].end_monotonic < causal_end:
                self.state = "waiting_deadtime"
                self.last_reject_reason = "causal_power_not_covered"
                return None
            return self._reject_completed_window(
                "causal_power_not_covered",
                samples,
                delay_s,
                coverage=coverage,
            )

        mean_temperature = self._time_weighted_mean(
            samples,
            lambda sample: sample.temperature,
        )
        mean_target = self._time_weighted_mean(samples, lambda sample: sample.target)
        mean_ff1 = self._time_weighted_mean(samples, lambda sample: sample.ff1)
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
            )
        if abs(mean_slope_h) > FF_TRIM_MAX_SLOPE_H:
            return self._reject_completed_window(
                f"slope_{mean_slope_h:.3f}",
                samples,
                delay_s,
                coverage=coverage,
            )

        observed_hold_power = (
            mean_power
            - slope_per_min / float(a)
            + (float(b) / float(a)) * (mean_target - mean_temperature)
        )
        target_trim = observed_hold_power - mean_ff1
        correction = target_trim - float(current_trim)
        if not all(
            isfinite(value)
            for value in (observed_hold_power, target_trim, correction)
        ):
            return self._reject_completed_window(
                "non_finite_result",
                samples,
                delay_s,
                coverage=coverage,
            )

        result = CausalFFTrimResult(
            admissible=True,
            reason="causal_window_ready",
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
        )
        self.last_result = result
        self.state = "ready"
        self.last_reject_reason = "none"
        self.last_update_reason = "causal_window_ready"
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        self._discard_power_before(causal_end)
        return result

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

    def reset_runtime(self) -> None:
        """Clear transient observation state without changing persisted trim."""
        self._power_segments.clear()
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

    @property
    def diagnostics(self) -> dict[str, float | int | str | None]:
        """Return the observer state without exposing mutable internals."""
        result = self.last_result
        has_samples = bool(self._thermal_samples)
        duration_s = 0.0
        if len(self._thermal_samples) >= 2:
            duration_s = (
                self._thermal_samples[-1].observed_monotonic
                - self._thermal_samples[0].observed_monotonic
            )
        return {
            "state": self.state,
            "window_duration_s": duration_s if has_samples else result.duration_s,
            "window_target_duration_s": self.target_duration_s,
            "measurement_count": (
                len(self._thermal_samples) if has_samples else result.measurement_count
            ),
            "alignment_delay_s": (
                self._window_deadtime_s if has_samples else result.alignment_delay_s
            ),
            "power_coverage_ratio": (
                0.0 if has_samples else result.power_coverage_ratio
            ),
            "mean_causal_power": None if has_samples else result.mean_causal_power,
            "mean_ff1": None if has_samples else result.mean_ff1,
            "mean_temperature": None if has_samples else result.mean_temperature,
            "mean_error": None if has_samples else result.mean_error,
            "mean_slope_h": None if has_samples else result.mean_slope_h,
            "observed_hold_power": (
                None if has_samples else result.observed_hold_power
            ),
            "target_trim": None if has_samples else result.target_trim,
            "correction": None if has_samples else result.correction,
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

    def _reject_completed_window(
        self,
        reason: str,
        samples: Sequence[FFTrimThermalSample],
        delay_s: float,
        *,
        coverage: float = 0.0,
    ) -> CausalFFTrimResult:
        duration_s = samples[-1].observed_monotonic - samples[0].observed_monotonic
        result = CausalFFTrimResult(
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
        )
        self.last_result = result
        self.state = "rejected"
        self.last_reject_reason = reason
        self.last_update_reason = "skipped"
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        self._discard_power_before(
            samples[-1].observed_monotonic - delay_s
        )
        return result

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

    def _clear_active_cycle(self) -> None:
        self._active_cycle_segments.clear()
        self._active_power_start = None
        self._active_linear_power = None


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


class FFTrim:
    """Slow trim correction on the FF principal u_ff_ab."""

    u_ff_trim: float
    frozen: bool
    freeze_reason: str

    def __init__(self) -> None:
        self.u_ff_trim: float = 0.0
        self.frozen: bool = False
        self.freeze_reason: str = "none"
        self._pending_deltas: Deque[float] = deque(maxlen=FF_TRIM_BUFFER_SIZE)

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
        if self.frozen:
            return

        authority = FF_TRIM_RHO * max(u_ff_ab, FF_TRIM_EPSILON)
        # delta_power is an incremental correction derived from thermal drift.
        # The trim must therefore converge toward the current trim plus this
        # correction, not toward delta_power as an absolute target.
        target_trim = self.u_ff_trim + delta_power
        new_trim = (1.0 - FF_TRIM_LAMBDA) * self.u_ff_trim + FF_TRIM_LAMBDA * target_trim
        self.u_ff_trim = clamp(new_trim, -authority, authority)

        _LOGGER.debug(
            "FFTrim: updated u_ff_trim=%.4f (delta_power=%.4f, authority=%.4f)",
            self.u_ff_trim,
            delta_power,
            authority,
        )

    def update_persistent(
        self,
        delta_power: float,
        u_ff_ab: float,
    ) -> FFTrimUpdateResult:
        """Update trim only after same-direction thermal corrections persist."""
        if self.frozen:
            self.clear_pending()
            return FFTrimUpdateResult(
                False,
                f"frozen_{self.freeze_reason}",
                0.0,
                0,
            )

        if abs(delta_power) <= FF_TRIM_DELTA_EPSILON:
            self.clear_pending()
            return FFTrimUpdateResult(False, "quiet_delta", 0.0, 0)

        direction = 1.0 if delta_power > 0.0 else -1.0
        previous_direction = self._pending_direction()
        if previous_direction is not None and previous_direction != direction:
            self.clear_pending()

        self._pending_deltas.append(delta_power)
        pending_count = len(self._pending_deltas)
        if pending_count < FF_TRIM_PERSISTENCE:
            return FFTrimUpdateResult(
                False,
                f"pending_{pending_count}/{FF_TRIM_PERSISTENCE}",
                0.0,
                pending_count,
            )

        applied_delta = float(median(self._pending_deltas))
        self.update(applied_delta, u_ff_ab)
        return FFTrimUpdateResult(
            True,
            "updated_persistent",
            applied_delta,
            pending_count,
        )

    def clear_pending(self) -> None:
        """Discard pending trim samples that belong to an invalid context."""
        self._pending_deltas.clear()

    def _pending_direction(self) -> float | None:
        """Return the direction shared by pending deltas, if any."""
        for delta in reversed(self._pending_deltas):
            if abs(delta) > FF_TRIM_DELTA_EPSILON:
                return 1.0 if delta > 0.0 else -1.0
        return None

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
