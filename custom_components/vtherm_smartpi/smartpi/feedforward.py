"""
Feed-Forward Orchestrator for Smart-PI Algorithm.

Computes the FF1 holding power, the structural feed-forward of the command:
  u_ff1 + u_pi -> u_cmd -> u_limited -> u_applied -> e_eff

u_ff1 always comes from the learned b / a (compute_ff). The room model never
enters the command: it observes the room and publishes its diagnostics only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL
from .const import AB_MIN_SAMPLES_A, AB_MIN_SAMPLES_B, clamp

if TYPE_CHECKING:
    from .ab_estimator import ABEstimator


# Plain value of the diagnostics: the holding power always comes from the learned b / a
FF_SOURCE_PLAIN = "learned model"


@dataclass(frozen=True)
class FFResult:
    """FF1 computation result for one cycle."""

    u_ff1: float           # Holding power injected into the command
    ff_reason: str         # Diagnostic reason string for the current FF state
    warmup_scale: float = 1.0  # FF1 warmup factor in [0, 1]


def compute_ff_gain(
    estimator: ABEstimator,
    hvac_mode: VThermHvacMode,
    tau_reliable: bool,
) -> float:
    """Return the signed FF gain b / a of the active mono-mode model, 0 when unusable."""
    # a is usable once published, which may happen before AB_MIN_SAMPLES_A
    # measurements when the first ones agree; a and b count as usable once a starting value stands.
    model_ready = (
        (len(estimator.a_meas_hist) >= AB_MIN_SAMPLES_A or estimator.usable_a)
        and (len(estimator.b_meas_hist) >= AB_MIN_SAMPLES_B or estimator.start.standing("b"))
    )
    model_sign_valid = (
        estimator.a < -1e-6
        if hvac_mode == VThermHvacMode_COOL
        else estimator.a > 1e-6
    )
    if model_ready and tau_reliable and model_sign_valid:
        return clamp(estimator.b / estimator.a, -3.0, 3.0)
    return 0.0


def compute_ff_warmup_scale(
    *,
    learn_ok_count: int,
    ff_warmup_ok_count: int,
    cycles_since_reset: int,
    ff_warmup_cycles: int,
) -> float:
    """Return the FF1 warmup factor: learned-sample ramp times cycle ramp, in [0, 1]."""
    learn_scale = clamp(learn_ok_count / float(ff_warmup_ok_count), 0.0, 1.0)
    time_scale = clamp(cycles_since_reset / float(ff_warmup_cycles), 0.0, 1.0)
    return clamp(learn_scale * time_scale, 0.0, 1.0)


def compute_ff(
    *,
    k_ff: float,
    target_temp_ff: float,
    ext_temp: float | None,
    warmup_scale: float,
) -> FFResult:
    """Compute the FF1 signal for one cycle.

    Args:
        k_ff: Signed FF gain b / a (positive in HEAT, negative in COOL).
        target_temp_ff: Feedforward reference setpoint (°C).
        ext_temp: Outdoor temperature (°C), or None if unavailable (FF1 is then 0).
        warmup_scale: Combined warmup scaling factor (learn_scale * time_scale).

    Returns:
        FFResult with the FF1 power and its reason.
    """
    if ext_temp is not None:
        u_ff1 = clamp(k_ff * (target_temp_ff - ext_temp), 0.0, 1.0) * warmup_scale
        ff_reason = "ff_none"
    else:
        u_ff1 = 0.0
        ff_reason = "ff_no_ext_temp"
    return FFResult(u_ff1=u_ff1, ff_reason=ff_reason, warmup_scale=float(warmup_scale))


class LearnedStepTransfer:
    """Bumpless transfer of a calibration step of the learned holding power into the integral."""

    def __init__(self) -> None:
        self._gain_before: float | None = None
        self.last_delta = 0.0

    def note(self, gain_before: float) -> None:
        """A calibration corrected the learned model: the next update returns the step of the learned holding power
        from ``gain_before`` (the learned gain b / a before the correction)."""
        self._gain_before = float(gain_before)

    def reset(self) -> None:
        """Forget a step not yet transferred (PI memory reset)."""
        self._gain_before = None
        self.last_delta = 0.0

    def update(self, *, learned: FFResult, target_temp: float, ext_temp: float | None) -> float:
        """Step dU of the learned holding power since the correction, 0 without a pending one; evaluated at the
        current setpoint and outdoor temperature with the warmup factor of this cycle."""
        if self._gain_before is None:
            self.last_delta = 0.0
            return 0.0
        before = compute_ff(k_ff=self._gain_before, target_temp_ff=target_temp, ext_temp=ext_temp,
                            warmup_scale=learned.warmup_scale).u_ff1
        self._gain_before = None
        self.last_delta = learned.u_ff1 - before
        return self.last_delta
