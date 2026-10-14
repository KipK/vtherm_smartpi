"""Differential test of the periodic FF trim window selector.

The reference below is a frozen copy of the straightforward per-suffix
selector. The production observer must reproduce its outputs bit for bit.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from math import isfinite
import random
from statistics import median

import pytest

from custom_components.vtherm_smartpi.smartpi import ff_trim_periodic as periodic_module
from custom_components.vtherm_smartpi.smartpi.const import (
    FF_TRIM_DEADTIME_WINDOW_FACTOR,
    FF_TRIM_MIN_WINDOW_CYCLES,
    FF_TRIM_PERIODIC_MIN_DISTINCT_MEASUREMENTS,
    GovernanceRegime,
)
from custom_components.vtherm_smartpi.smartpi.ff_trim import FFTrimThermalSample
from custom_components.vtherm_smartpi.smartpi.ff_trim_periodic import (
    PeriodicFFTrimObserver,
    PeriodicFFTrimWindow,
)

_BASE_SAMPLE = FFTrimThermalSample(
    observed_monotonic=0.0,
    measurement_id="0",
    temperature=20.0,
    target=20.0,
    ff1=0.08,
    regime=GovernanceRegime.HOLD,
    i_mode="I:HOLD(deadband)",
    u_pi=-0.04,
    hvac_mode="heat",
    saturated=False,
    trajectory_active=False,
    ff3_active=False,
    setpoint_changed=False,
    outside_temperature_available=True,
    model_reliable=True,
    outside_temperature=10.0,
)


# ---------------------------------------------------------------------------
# Frozen reference implementation (do not edit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReferenceWindow:
    """One phase-closed thermal cycle ready for causal evaluation."""

    samples: tuple[FFTrimThermalSample, ...]
    deadtime_s: float
    duration_s: float
    amplitude_c: float
    closure_error_c: float
    quantum_c: float


class _ReferencePeriodicFFTrimObserver:
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
    ) -> _ReferenceWindow | None:
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
            return _ReferenceWindow(
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
        window: _ReferenceWindow,
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


# ---------------------------------------------------------------------------
# Differential test
# ---------------------------------------------------------------------------

_MODES = ("heat", "heat", "heat", "heat", "cool")
_REGIMES = (
    GovernanceRegime.HOLD,
    GovernanceRegime.NEAR_BAND,
    GovernanceRegime.DEAD_BAND,
    GovernanceRegime.SATURATED,
)


def _canon(value):
    """Return a bit-exact comparable form of nested observer outputs."""
    if isinstance(value, float):
        return ("f", value.hex())
    if isinstance(value, (tuple, list)):
        return (type(value).__name__, tuple(_canon(item) for item in value))
    if isinstance(value, dict):
        return tuple((key, _canon(item)) for key, item in value.items())
    if isinstance(value, FFTrimThermalSample):
        return ("sample", id(value))
    if isinstance(value, PeriodicFFTrimWindow):
        return (
            "window",
            _canon(value.samples),
            _canon(value.deadtime_s),
            _canon(value.duration_s),
            _canon(value.amplitude_c),
            _canon(value.closure_error_c),
            _canon(value.quantum_c),
        )
    if isinstance(value, _ReferenceWindow):
        return (
            "window",
            _canon(value.samples),
            _canon(value.deadtime_s),
            _canon(value.duration_s),
            _canon(value.amplitude_c),
            _canon(value.closure_error_c),
            _canon(value.quantum_c),
        )
    return (type(value).__name__, value)


def _observer_state(observer) -> tuple:
    return (
        _canon(observer.diagnostics),
        _canon(observer._thermal_samples),
        _canon(observer._window_deadtime_s),
        _canon(observer._washout_until_monotonic),
        _canon(observer.target_duration_s),
        _canon(observer.window_duration_s),
    )


class _Scenario:
    """Random thermal trace with varied cadence, quantum and disturbances."""

    def __init__(self, rng: random.Random, *, quiet: bool) -> None:
        self.rng = rng
        self.now = rng.uniform(0.0, 1000.0)
        self.phase = rng.uniform(0.0, 6.3)
        self.cadence_s = rng.choice((15.0, 60.0, 120.0, 300.0, 600.0))
        self.period_s = rng.choice((900.0, 1800.0, 3600.0, 7200.0))
        self.amplitude_c = rng.choice((0.0, 0.02, 0.1, 0.3, 0.6))
        self.quantum_c = rng.choice((0.0, 0.01, 0.05, 0.1, 0.5))
        self.noise_c = rng.choice((0.0, 0.0, 0.005, 0.02))
        self.deadtime_s = rng.choice((60.0, 300.0, 600.0, 1800.0))
        self.target = 20.0
        self.mode = "heat"
        self.power_start: float | None = None
        # Quiet traces avoid disturbances so the buffer grows to thousands of samples.
        self.quiet = quiet

    def next_sample(self) -> FFTrimThermalSample:
        rng = self.rng
        if rng.random() < 0.01:
            self.cadence_s = rng.choice((15.0, 60.0, 120.0, 300.0, 600.0))
        if rng.random() < 0.003:
            self.period_s = rng.choice((900.0, 1800.0, 3600.0, 7200.0))
        step = self.cadence_s * rng.choice((1.0, 1.0, 1.0, 0.5, 2.0, 3.0))
        if rng.random() < 0.02:
            step = 0.0
        self.now += step
        raw = (
            self.target
            + self.amplitude_c * math.sin(self.phase + 6.283185307 * self.now / self.period_s)
            + rng.gauss(0.0, self.noise_c)
        )
        if self.quantum_c > 0.0:
            temperature = round(raw / self.quantum_c) * self.quantum_c
        else:
            temperature = raw
        if rng.random() < 0.01:
            temperature = rng.choice((0.0, -0.0, temperature + 1e-10))
        if rng.random() < 0.002 and not self.quiet:
            self.target = rng.choice((19.0, 20.0, 21.0))
        if rng.random() < 0.002 and not self.quiet:
            self.mode = rng.choice(_MODES)
        rare = 1.0 if self.quiet else rng.random()
        return FFTrimThermalSample(
            observed_monotonic=(
                float("nan") if rare < 0.001 else self.now
            ),
            measurement_id=str(self.now),
            temperature=temperature,
            target=self.target,
            ff1=0.08,
            regime=(
                GovernanceRegime.EXCITED_STABLE
                if 0.001 <= rare < 0.003
                else rng.choice(_REGIMES)
            ),
            i_mode="I:HOLD(deadband)",
            u_pi=-0.04,
            hvac_mode=self.mode,
            saturated=False,
            trajectory_active=0.003 <= rare < 0.004,
            ff3_active=0.004 <= rare < 0.005,
            setpoint_changed=0.005 <= rare < 0.006,
            outside_temperature_available=not (0.006 <= rare < 0.007),
            model_reliable=not (0.007 <= rare < 0.008),
            outside_temperature=10.0,
            trim_frozen_reason=None,
            saturation_state="SAT_HI" if 0.008 <= rare < 0.009 else "NO_SAT",
        )

    def deadtime(self) -> tuple[float | None, bool]:
        rng = self.rng
        roll = 1.0 if self.quiet else rng.random()
        if roll < 0.003:
            return None, False
        if roll < 0.006:
            return self.deadtime_s, False
        if roll < 0.008:
            self.deadtime_s = rng.choice((60.0, 300.0, 600.0, 1800.0))
        if roll < 0.010:
            return self.deadtime_s * 1.2, True
        return self.deadtime_s, True

    def earliest_power_start(self) -> float | None:
        rng = self.rng
        if self.quiet and self.power_start is None:
            self.power_start = self.now - 100.0
        if self.quiet:
            return self.power_start
        roll = rng.random()
        if roll < 0.01:
            return None
        if roll < 0.02 or self.power_start is None:
            self.power_start = self.now - rng.uniform(0.0, 20000.0)
        return self.power_start


def _run_differential(
    seed: int,
    steps: int,
    *,
    quiet: bool,
    close_probability: float = 0.35,
) -> dict[str, int]:
    rng = random.Random(seed)
    scenario = _Scenario(rng, quiet=quiet)
    cycle_min = rng.choice((5.0, 10.0, 15.0))
    reference = _ReferencePeriodicFFTrimObserver(cycle_min)
    candidate = PeriodicFFTrimObserver(cycle_min)
    outcomes: dict[str, int] = {}
    max_buffer = 0

    for _ in range(steps):
        sample = scenario.next_sample()
        deadtime_s, reliable = scenario.deadtime()
        for observer in (reference, candidate):
            observer.record_thermal_sample(
                sample, deadtime_s=deadtime_s, deadtime_reliable=reliable
            )
        assert _observer_state(candidate) == _observer_state(reference)

        if rng.random() < close_probability:
            power_start = scenario.earliest_power_start()
            deadtime_s, reliable = scenario.deadtime()
            expected = reference.try_close_window(
                earliest_power_start=power_start,
                deadtime_s=deadtime_s,
                deadtime_reliable=reliable,
            )
            actual = candidate.try_close_window(
                earliest_power_start=power_start,
                deadtime_s=deadtime_s,
                deadtime_reliable=reliable,
            )
            assert _canon(actual) == _canon(expected)
            assert _observer_state(candidate) == _observer_state(reference)
            max_buffer = max(max_buffer, len(reference._thermal_samples))
            key = "window" if expected is not None else reference.last_reject_reason
            outcomes[key] = outcomes.get(key, 0) + 1
            if expected is not None and rng.random() < 0.8:
                admissible = rng.random() < 0.5
                reason = "updated" if admissible else "rejected_test"
                reference.complete_window(expected, admissible=admissible, reason=reason)
                candidate.complete_window(actual, admissible=admissible, reason=reason)
                assert _observer_state(candidate) == _observer_state(reference)

        roll = 1.0 if scenario.quiet else rng.random()
        if roll < 0.002:
            reference.invalidate("test", now_monotonic=scenario.now, washout_s=300.0)
            candidate.invalidate("test", now_monotonic=scenario.now, washout_s=300.0)
        elif roll < 0.003:
            reference.reset_after_trim_update(now_monotonic=scenario.now, washout_s=600.0)
            candidate.reset_after_trim_update(now_monotonic=scenario.now, washout_s=600.0)
        elif roll < 0.0035:
            reference.reset_runtime()
            candidate.reset_runtime()
        assert _observer_state(candidate) == _observer_state(reference)
    outcomes["max_buffer"] = max_buffer
    return outcomes


@pytest.mark.parametrize("seed", range(24))
def test_periodic_window_selection_matches_frozen_reference(seed: int) -> None:
    """Window choice, statistics and diagnostics match the frozen reference."""
    _run_differential(seed, 3000, quiet=False)


def test_differential_sequences_cover_every_outcome() -> None:
    """The random traces exercise windows and every rejection reason."""
    outcomes: dict[str, int] = {}
    for seed in range(8):
        for key, count in _run_differential(seed, 3000, quiet=False).items():
            if key == "max_buffer":
                count = max(outcomes.get(key, 0), count)
                outcomes[key] = count
                continue
            outcomes[key] = outcomes.get(key, 0) + count
    for key in (
        "window",
        "cycle_not_reversed",
        "cycle_amplitude_too_small",
        "cycle_not_closed",
        "window_too_short",
        "not_enough_measurements",
        "causal_power_not_covered",
        "deadtime_unreliable",
    ):
        assert outcomes.get(key, 0) > 0, (key, outcomes)


@pytest.mark.parametrize("seed", (1000, 1002))
def test_long_buffers_match_frozen_reference(seed: int) -> None:
    """Undisturbed traces grow the buffer to thousands of samples."""
    outcomes = _run_differential(
        seed, 3000, quiet=True, close_probability=0.003
    )
    assert outcomes["max_buffer"] >= 500, outcomes


def test_window_scan_cost_is_linear_in_buffer_size(monkeypatch) -> None:
    """One window close performs at most a bounded number of heap pushes per sample."""
    pushes = 0
    original_push = periodic_module.heappush

    def counting_push(heap, item):
        nonlocal pushes
        pushes += 1
        original_push(heap, item)

    monkeypatch.setattr(periodic_module, "heappush", counting_push)
    observer = PeriodicFFTrimObserver(10.0)
    # Monotonic ramp: never reversed, so every suffix is scanned.
    count = 4000
    for index in range(count):
        observer.record_thermal_sample(
            replace(_BASE_SAMPLE, observed_monotonic=60.0 * index, temperature=0.01 * index),
            deadtime_s=300.0,
            deadtime_reliable=True,
        )
    assert observer.try_close_window(
        earliest_power_start=-1000.0, deadtime_s=300.0, deadtime_reliable=True
    ) is None
    assert observer.last_reject_reason == "cycle_not_reversed"
    assert pushes <= 2 * count
