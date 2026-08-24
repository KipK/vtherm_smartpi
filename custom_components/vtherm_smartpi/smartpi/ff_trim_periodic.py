"""Periodic-equilibrium window selection for the FF trim observer."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median

from .const import (
    FF_TRIM_DEADTIME_WINDOW_FACTOR,
    FF_TRIM_MIN_WINDOW_CYCLES,
    FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS,
    GovernanceRegime,
)
from .ff_trim import FFTrimThermalSample


@dataclass(frozen=True)
class PeriodicFFTrimWindow:
    """One phase-closed thermal cycle ready for causal evaluation."""

    samples: tuple[FFTrimThermalSample, ...]
    deadtime_s: float
    duration_s: float
    amplitude_c: float
    closure_error_c: float
    quantum_c: float


class PeriodicFFTrimObserver:
    """Select complete thermal cycles without owning physical trace data."""

    _ALLOWED_REGIMES = frozenset(
        {
            GovernanceRegime.NEAR_BAND,
            GovernanceRegime.DEAD_BAND,
            GovernanceRegime.HOLD,
            GovernanceRegime.SATURATED,
        }
    )

    def __init__(self, cycle_min: float) -> None:
        self._cycle_min = max(float(cycle_min), 0.0)
        self._thermal_samples: list[FFTrimThermalSample] = []
        self._last_measurement_id: str | None = None
        self._window_deadtime_s: float | None = None
        self._washout_until_monotonic = 0.0
        self.state = "warming_up"
        self.last_reject_reason = "none"
        self.last_update_reason = "none"
        self.target_duration_s = 0.0
        self.last_duration_s = 0.0
        self.last_amplitude_c = 0.0
        self.last_closure_error_c = 0.0
        self.last_measurement_count = 0

    def record_thermal_sample(
        self,
        sample: FFTrimThermalSample,
        *,
        deadtime_s: float | None,
        deadtime_reliable: bool,
    ) -> None:
        """Record one distinct sample when the periodic context is usable."""
        if sample.measurement_id == self._last_measurement_id:
            return
        self._last_measurement_id = sample.measurement_id

        rejection = self._sample_rejection_reason(sample)
        if rejection is not None:
            self.invalidate(
                rejection,
                now_monotonic=sample.observed_monotonic,
                washout_s=deadtime_s if deadtime_reliable else 0.0,
            )
            return

        if (
            not deadtime_reliable
            or deadtime_s is None
            or not isfinite(float(deadtime_s))
            or float(deadtime_s) <= 0.0
        ):
            self.invalidate(
                "deadtime_unreliable",
                now_monotonic=sample.observed_monotonic,
            )
            return

        delay_s = float(deadtime_s)
        if (
            self._window_deadtime_s is not None
            and abs(delay_s - self._window_deadtime_s)
            > max(1.0, 0.05 * self._window_deadtime_s)
        ):
            self.invalidate(
                "deadtime_changed",
                now_monotonic=sample.observed_monotonic,
                washout_s=delay_s,
            )
            return

        if sample.observed_monotonic < self._washout_until_monotonic:
            self.state = "waiting_deadtime"
            self.last_reject_reason = "washout"
            return

        if not self._thermal_samples:
            self._window_deadtime_s = delay_s
        self._thermal_samples.append(sample)
        self.state = "collecting"
        self.last_reject_reason = "none"

    def try_close_window(
        self,
        *,
        earliest_power_start: float | None,
        deadtime_s: float | None,
        deadtime_reliable: bool,
    ) -> PeriodicFFTrimWindow | None:
        """Return the first complete phase-aligned thermal cycle."""
        if (
            not deadtime_reliable
            or deadtime_s is None
            or not isfinite(float(deadtime_s))
            or float(deadtime_s) <= 0.0
        ):
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
        while (
            self._thermal_samples
            and self._thermal_samples[0].observed_monotonic < earliest_observable
        ):
            self._thermal_samples.pop(0)

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

        saw_reversal = False
        saw_sufficient_amplitude = False
        last_start_index = len(samples) - FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS
        for start_index in range(last_start_index, -1, -1):
            candidate = samples[start_index:]
            duration_s = (
                candidate[-1].observed_monotonic
                - candidate[0].observed_monotonic
            )
            if duration_s < self.target_duration_s:
                continue
            temperatures = [sample.temperature for sample in candidate]
            deltas = [
                current - previous
                for previous, current in zip(temperatures, temperatures[1:])
                if abs(current - previous) > 1e-9
            ]
            if not deltas or not any(delta > 0.0 for delta in deltas) or not any(
                delta < 0.0 for delta in deltas
            ):
                continue
            saw_reversal = True
            quantum_c = float(median(abs(delta) for delta in deltas))
            amplitude_c = max(temperatures) - min(temperatures)
            if amplitude_c < 2.0 * quantum_c:
                continue
            saw_sufficient_amplitude = True
            closure_error_c = abs(temperatures[-1] - temperatures[0])
            closure_tolerance_c = max(0.001, quantum_c / 2.0)
            same_direction = deltas[0] * deltas[-1] > 0.0
            if closure_error_c > closure_tolerance_c or not same_direction:
                continue
            self.state = "ready"
            self.last_reject_reason = "none"
            return PeriodicFFTrimWindow(
                samples=candidate,
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

    def invalidate(
        self,
        reason: str,
        *,
        now_monotonic: float | None = None,
        washout_s: float | None = None,
    ) -> None:
        """Discard only the periodic window and optionally impose washout."""
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        if now_monotonic is not None and washout_s is not None:
            self._washout_until_monotonic = max(
                self._washout_until_monotonic,
                float(now_monotonic) + max(float(washout_s), 0.0),
            )
        self.state = "rejected"
        self.last_reject_reason = reason
        self.last_update_reason = "skipped"

    def reset_after_trim_update(
        self,
        *,
        now_monotonic: float,
        washout_s: float,
    ) -> None:
        """Start a fresh periodic episode after the reference trim changes."""
        self._thermal_samples.clear()
        self._window_deadtime_s = None
        self._washout_until_monotonic = max(
            self._washout_until_monotonic,
            float(now_monotonic) + max(float(washout_s), 0.0),
        )
        self.state = "waiting_deadtime"

    def reset_runtime(self) -> None:
        """Clear all transient periodic observation state."""
        self._thermal_samples.clear()
        self._last_measurement_id = None
        self._window_deadtime_s = None
        self._washout_until_monotonic = 0.0
        self.state = "warming_up"
        self.last_reject_reason = "none"
        self.last_update_reason = "none"
        self.target_duration_s = 0.0
        self.last_duration_s = 0.0
        self.last_amplitude_c = 0.0
        self.last_closure_error_c = 0.0
        self.last_measurement_count = 0

    @property
    def window_duration_s(self) -> float:
        """Return the active periodic-window duration."""
        if len(self._thermal_samples) < 2:
            return 0.0
        return (
            self._thermal_samples[-1].observed_monotonic
            - self._thermal_samples[0].observed_monotonic
        )

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

    @classmethod
    def _sample_rejection_reason(
        cls,
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
        if sample.saturation_state == "SAT_HI":
            return "saturated_high"
        if sample.saturated and sample.saturation_state != "SAT_LO":
            return "saturation_unknown"
        if sample.trim_frozen_reason not in (None, "none", "saturated"):
            return f"frozen_{sample.trim_frozen_reason}"
        if sample.setpoint_changed:
            return "setpoint_changed"
        if sample.trajectory_active:
            return "trajectory_active"
        if sample.ff3_active:
            return "ff3_active"
        if sample.regime not in cls._ALLOWED_REGIMES:
            regime = (
                sample.regime.value
                if isinstance(sample.regime, GovernanceRegime)
                else sample.regime
            )
            return f"regime_{regime}"
        return None
