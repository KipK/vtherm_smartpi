"""
Feed-Forward Orchestrator for Smart-PI Algorithm.

Computes the full FF signal chain:
  u_ff1 -> u_ff2 -> u_ff_final -> u_ff3 -> u_ff_eff

u_ff1 comes from the learned b / a (compute_ff), or from the published room
model in heating with PWM heaters (compute_room_ff, see
room_model/feedforward.py).

Compatibility alias kept for the current codebase:
  u_ff_trim == u_ff2

Source of truth for runtime FF is u_ff_eff.

Chain:
  u_ff_eff + u_pi -> u_cmd -> u_limited -> u_applied -> e_eff
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..hvac_mode import VThermHvacMode, VThermHvacMode_COOL
from .const import AB_MIN_SAMPLES_A, AB_MIN_SAMPLES_B, clamp
from .ff_trim import FFTrim

if TYPE_CHECKING:
    from .ab_estimator import ABEstimator
    from .room_model.feedforward import HoldingSource


@dataclass(frozen=True)
class FFResult:
    """Complete FF computation result for one cycle."""

    u_ff1: float           # Structural FF branch derived from a,b
    u_ff2: float           # Slow corrective bias branch
    u_ff_final: float      # Final FF command: clamp(u_ff1 + u_ff2, 0, 1)
    u_ff3: float           # Predictive FF3 correction, applied after FF1 + FF2
    u_ff_trim: float       # Slow trim correction applied (additive)
    u_ff_eff: float        # FF effectively injected after FF3: clamp(u_ff_final + u_ff3, 0, 1)
    ff_reason: str         # Diagnostic reason string for the current FF state
    warmup_scale: float = 1.0  # FF1 warmup factor in [0, 1]; the trim stays frozen below 1
    trim_applied: bool = True  # False without outdoor temperature (no FF1 to correct)


def compute_ff_gain(
    estimator: ABEstimator,
    hvac_mode: VThermHvacMode,
    tau_reliable: bool,
) -> float:
    """Return the signed FF gain b / a of the active mono-mode model, 0 when unusable."""
    # a is usable once published, which may happen before AB_MIN_SAMPLES_A
    # measurements when the first ones agree.
    model_ready = (
        (len(estimator.a_meas_hist) >= AB_MIN_SAMPLES_A or estimator.learn_ok_count_a > 0)
        and len(estimator.b_meas_hist) >= AB_MIN_SAMPLES_B
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
    trim: FFTrim,
    u_ff3: float = 0.0,
) -> FFResult:
    """Compute the full FF signal for one cycle.

    Args:
        k_ff: Signed FF gain b / a (positive in HEAT, negative in COOL).
        target_temp_ff: Feedforward reference setpoint (°C).
        ext_temp: Outdoor temperature (°C), or None if unavailable (FF1 and the
            trim are then both 0).
        warmup_scale: Combined warmup scaling factor (learn_scale * time_scale).
        trim: FFTrim instance (provides additive u_ff2 correction).
        u_ff3: Predictive FF3 correction applied after FF1 + FF2.

    Returns:
        FFResult with all FF signal components.
    """
    # --- Step 1: Compute u_ff1 (structural branch) ---
    # Without outdoor temperature there is no FF1 for the trim to correct:
    # both branches are off.
    if ext_temp is not None:
        u_ff1 = clamp(k_ff * (target_temp_ff - ext_temp), 0.0, 1.0) * warmup_scale
        u_ff2 = trim.u_ff_trim
        ff_reason = "ff_none"
    else:
        u_ff1 = 0.0
        u_ff2 = 0.0
        ff_reason = "ff_no_ext_temp"

    # --- Step 2: final FF and effective command ---
    u_ff_final = clamp(u_ff1 + u_ff2, 0.0, 1.0)
    u_ff_eff = clamp(u_ff_final + u_ff3, 0.0, 1.0)

    return FFResult(
        u_ff1=u_ff1,
        u_ff2=u_ff2,
        u_ff_final=u_ff_final,
        u_ff3=u_ff3,
        u_ff_trim=u_ff2,
        u_ff_eff=u_ff_eff,
        ff_reason=ff_reason,
        warmup_scale=float(warmup_scale),
        trim_applied=ext_temp is not None,
    )


def compute_room_ff(
    *,
    source: HoldingSource,
    target_temp_ff: float,
    ext_temp: float | None,
    wall: float | None = None,
) -> FFResult:
    """FF1 from a published room model: full weight, no FF2 trim.

    The holding power of the source (static part, plus the wall part applied
    at this cycle: wall, or the source's live wall term when None) replaces
    the learned b / a branch. The trim is not applied: the room model
    replaces the bias it learns.
    """
    if ext_temp is not None:
        u_ff1 = source.power(target_temp_ff, ext_temp, wall)
        ff_reason = "ff_none"
    else:
        u_ff1 = 0.0
        ff_reason = "ff_no_ext_temp"
    return FFResult(
        u_ff1=u_ff1,
        u_ff2=0.0,
        u_ff_final=u_ff1,
        u_ff3=0.0,
        u_ff_trim=0.0,
        u_ff_eff=u_ff1,
        ff_reason=ff_reason,
        warmup_scale=1.0,
        trim_applied=False,
    )


def add_wall_part(result: FFResult, wall: float, ext_temp: float | None) -> FFResult:
    """Learned FF1 + FF2 with a room model wall part still fading out on top (unchanged without one)."""
    if wall == 0.0 or ext_temp is None:
        return result
    u_ff_final = clamp(result.u_ff_final + wall, 0.0, 1.0)
    return replace(
        result,
        u_ff1=result.u_ff1 + wall,
        u_ff_final=u_ff_final,
        u_ff_eff=clamp(u_ff_final + result.u_ff3, 0.0, 1.0),
    )
