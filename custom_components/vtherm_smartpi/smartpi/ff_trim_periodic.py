"""Periodic-equilibrium window selection for the FF trim observer."""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from math import isfinite

from .const import (
    FF_TRIM_DEADTIME_WINDOW_FACTOR,
    FF_TRIM_MIN_WINDOW_CYCLES,
    FF_TRIM_PERIODIC_ALLOWED_REGIMES,
    FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS,
    FF_TRIM_PERIODIC_TOLERATED_FREEZE_REASONS,
    GovernanceRegime,
)
from .ff_trim import FFTrimThermalSample, _ThermalWindowCollector


@dataclass(frozen=True)
class PeriodicFFTrimWindow:
    """One phase-closed thermal cycle ready for causal evaluation."""

    samples: tuple[FFTrimThermalSample, ...]
    deadtime_s: float
    duration_s: float
    amplitude_c: float
    closure_error_c: float
    quantum_c: float


class PeriodicFFTrimObserver(_ThermalWindowCollector):
    """Select complete thermal cycles without owning physical trace data."""

    def __init__(self, cycle_min: float) -> None:
        super().__init__(cycle_min)
        self.target_duration_s = 0.0
        self.last_duration_s = 0.0
        self.last_amplitude_c = 0.0
        self.last_closure_error_c = 0.0
        self.last_measurement_count = 0

    def try_close_window(
        self,
        *,
        earliest_power_start: float | None,
        deadtime_s: float | None,
        deadtime_reliable: bool,
    ) -> PeriodicFFTrimWindow | None:
        """Return the first complete phase-aligned thermal cycle."""
        if not self._deadtime_usable(deadtime_s, deadtime_reliable):
            self.state = "waiting_deadtime"
            self.last_reject_reason = "deadtime_unreliable"
            return None

        delay_s = self._window_deadtime_s or float(deadtime_s)
        self.target_duration_s = max(
            FF_TRIM_MIN_WINDOW_CYCLES * self._cycle_min * 60.0,
            FF_TRIM_DEADTIME_WINDOW_FACTOR * delay_s,
        )

        if earliest_power_start is None:
            self.state = "warming_up"
            self.last_reject_reason = "causal_power_not_covered"
            return None

        earliest_observable = float(earliest_power_start) + delay_s
        stale_count = 0
        for sample in self._thermal_samples:
            if not sample.observed_monotonic < earliest_observable:
                break
            stale_count += 1
        if stale_count:
            del self._thermal_samples[:stale_count]

        if len(self._thermal_samples) < FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS:
            self.state = "collecting"
            self.last_reject_reason = "not_enough_measurements"
            return None

        samples = tuple(self._thermal_samples)
        total_duration_s = (
            samples[-1].observed_monotonic - samples[0].observed_monotonic
        )
        if total_duration_s < self.target_duration_s:
            self.state = "collecting"
            self.last_reject_reason = "window_too_short"
            return None

        if max(sample.target for sample in samples) - min(
            sample.target for sample in samples
        ) > 1e-6:
            self.invalidate("setpoint_changed")
            return None
        if len({sample.hvac_mode for sample in samples}) != 1:
            self.invalidate("hvac_mode_changed")
            return None

        # Suffixes are scanned from the shortest to the longest one, so every
        # statistic is extended by prepending one sample instead of being
        # recomputed from scratch. Each value reproduces the expression of a
        # per-suffix evaluation exactly (same operands, same tie handling).
        saw_reversal = False
        saw_sufficient_amplitude = False
        last_start_index = len(samples) - FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS
        last_observed = samples[-1].observed_monotonic
        last_temperature = samples[-1].temperature
        max_temperature = last_temperature
        min_temperature = last_temperature
        # Two heaps holding |delta| values: lower half negated, upper half.
        lower_half: list[float] = []
        upper_half: list[float] = []
        delta_count = 0
        has_rise = False
        has_fall = False
        first_delta = 0.0
        last_delta = 0.0
        for start_index in range(len(samples) - 2, -1, -1):
            temperature = samples[start_index].temperature
            # max()/min() keep the leftmost extreme on ties.
            if temperature >= max_temperature:
                max_temperature = temperature
            if temperature <= min_temperature:
                min_temperature = temperature
            delta = samples[start_index + 1].temperature - temperature
            if abs(delta) > 1e-9:
                if delta_count == 0:
                    last_delta = delta
                first_delta = delta
                delta_count += 1
                if delta > 0.0:
                    has_rise = True
                elif delta < 0.0:
                    has_fall = True
                magnitude = abs(delta)
                if lower_half and magnitude > -lower_half[0]:
                    heappush(upper_half, magnitude)
                else:
                    heappush(lower_half, -magnitude)
                if len(lower_half) > len(upper_half) + 1:
                    heappush(upper_half, -heappop(lower_half))
                elif len(upper_half) > len(lower_half):
                    heappush(lower_half, -heappop(upper_half))
            if start_index > last_start_index:
                continue

            duration_s = last_observed - samples[start_index].observed_monotonic
            if duration_s < self.target_duration_s:
                continue
            if not delta_count or not has_rise or not has_fall:
                continue
            saw_reversal = True
            if delta_count % 2 == 1:
                quantum_c = float(-lower_half[0])
            else:
                quantum_c = float((-lower_half[0] + upper_half[0]) / 2)
            amplitude_c = max_temperature - min_temperature
            if amplitude_c < 2.0 * quantum_c:
                continue
            saw_sufficient_amplitude = True
            closure_error_c = abs(last_temperature - temperature)
            closure_tolerance_c = max(0.001, quantum_c / 2.0)
            same_direction = first_delta * last_delta > 0.0
            if closure_error_c > closure_tolerance_c or not same_direction:
                continue
            self.state = "ready"
            self.last_reject_reason = "none"
            return PeriodicFFTrimWindow(
                samples=samples[start_index:],
                deadtime_s=delay_s,
                duration_s=duration_s,
                amplitude_c=amplitude_c,
                closure_error_c=closure_error_c,
                quantum_c=quantum_c,
            )

        self.state = "waiting_phase"
        if not saw_reversal:
            self.last_reject_reason = "cycle_not_reversed"
        elif not saw_sufficient_amplitude:
            self.last_reject_reason = "cycle_amplitude_too_small"
        else:
            self.last_reject_reason = "cycle_not_closed"
        return None

    def complete_window(
        self,
        window: PeriodicFFTrimWindow,
        *,
        admissible: bool,
        reason: str,
    ) -> None:
        """Close the selected window after its shared-trace evaluation."""
        self.last_duration_s = window.duration_s
        self.last_measurement_count = len(window.samples)
        self.last_amplitude_c = window.amplitude_c
        self.last_closure_error_c = window.closure_error_c
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        self.state = "ready" if admissible else "rejected"
        self.last_reject_reason = "none" if admissible else reason
        self.last_update_reason = reason

    def reset_runtime(self) -> None:
        """Clear all transient periodic observation state."""
        super().reset_runtime()
        self.target_duration_s = 0.0
        self.last_duration_s = 0.0
        self.last_amplitude_c = 0.0
        self.last_closure_error_c = 0.0
        self.last_measurement_count = 0

    @property
    def diagnostics(self) -> dict[str, float | int | str | None]:
        """Return periodic selector diagnostics."""
        has_samples = bool(self._thermal_samples)
        temperatures = [sample.temperature for sample in self._thermal_samples]
        amplitude_c = (
            max(temperatures) - min(temperatures)
            if temperatures
            else self.last_amplitude_c
        )
        closure_error_c = (
            abs(temperatures[-1] - temperatures[0])
            if temperatures
            else self.last_closure_error_c
        )
        return {
            "state": self.state,
            "window_duration_s": (
                self.window_duration_s if has_samples else self.last_duration_s
            ),
            "window_target_duration_s": self.target_duration_s,
            "measurement_count": (
                len(self._thermal_samples)
                if has_samples
                else self.last_measurement_count
            ),
            "amplitude_c": amplitude_c,
            "closure_error_c": closure_error_c,
            "last_reject_reason": self.last_reject_reason,
            "last_update_reason": self.last_update_reason,
        }

    @staticmethod
    def _sample_rejection_reason(sample: FFTrimThermalSample) -> str | None:
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
        if sample.saturation_state == "SAT_HI":
            return "saturated_high"
        if sample.saturated and sample.saturation_state != "SAT_LO":
            return "saturation_unknown"
        if (
            sample.trim_frozen_reason not in (None, "none")
            and sample.trim_frozen_reason
            not in FF_TRIM_PERIODIC_TOLERATED_FREEZE_REASONS
        ):
            return f"frozen_{sample.trim_frozen_reason}"
        if sample.setpoint_changed:
            return "setpoint_changed"
        if sample.trajectory_active:
            return "trajectory_active"
        if sample.ff3_active:
            return "ff3_active"
        if sample.regime not in FF_TRIM_PERIODIC_ALLOWED_REGIMES:
            regime = (
                sample.regime.value
                if isinstance(sample.regime, GovernanceRegime)
                else sample.regime
            )
            return f"regime_{regime}"
        return None
