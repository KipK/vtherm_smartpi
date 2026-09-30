"""Pure eligibility evaluation for terminal governor episode release."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .command_ownership import project_cycle_command
from .const import (
    DEADBAND_HYSTERESIS, REFERENCE_GOVERNOR_MARGIN_MAX_C, REFERENCE_GOVERNOR_RESERVE_SELECTOR,
    REFERENCE_GOVERNOR_RHO_MARGIN, TRAJECTORY_BUMPLESS_MAX_U_DELTA,
    TRAJECTORY_COMPLETE_EPS_C, TrajectoryPhase,
)
from .controller import PIOutputSnapshot
from .reference_governor import (
    PredictionSegment, SegmentedPrediction, _valid_pi_snapshot,
    compute_dynamic_reserve_candidates, normalize_model,
    predict_segmented_1r1c, select_dynamic_reserve,
)
from .reference_governor_authority import (
    ReferenceGovernorAuthorityContext,
)


@dataclass(frozen=True, slots=True)
class BrakingReleaseDecision:
    """Evidence for one proposed terminal retirement, without command authority."""

    release_candidate: bool
    model_admissible: bool
    reason: str
    raw_requested_power: float | None = None
    raw_projected_power: float | None = None
    forecast_power: float | None = None
    prediction: SegmentedPrediction | None = None
    signed_peak_above_target_c: float | None = None
    dynamic_reserve_c: float | None = None
    target_band_bound_c: float | None = None
    command_deltas: tuple[float, ...] = ()
    authoritative: bool = False


def _finite_number(value) -> bool:
    return type(value) in (int, float) and isfinite(value)


def evaluate_braking_release(
    *,
    context: ReferenceGovernorAuthorityContext,
    pi_snapshot: PIOutputSnapshot,
    phase: TrajectoryPhase,
    profile_nominal: float,
    deadband_c: float,
    cycle_min: float,
    previous_requested_power: float,
    previous_projected_power: float,
    guard_active: bool,
    context_is_current: bool,
    post_pi_branch_complete: bool,
    zero_delay_switch: bool,
) -> BrakingReleaseDecision:
    """Compare raw PI and projected switch power with the frozen current episode.

    The caller must attest that the context is current, the post-PI branch is
    complete, and the actuator is a switch with zero timing delays. Unsupported
    soft limits, hold branches, valves, stale observations, and incomplete PI
    decompositions cannot be promoted by this evaluator. The committed command
    in the context must already be in projected actuator power coordinates.
    """
    reject = lambda reason: BrakingReleaseDecision(False, False, reason)
    flags = (guard_active, context_is_current, post_pi_branch_complete, zero_delay_switch)
    if any(type(flag) is not bool for flag in flags):
        return reject("invalid_boolean_evidence")
    if not isinstance(context, ReferenceGovernorAuthorityContext):
        return reject("missing_context")
    if not context_is_current or context.bypass_reason is not None:
        return reject("stale_or_bypassed_context")
    if not post_pi_branch_complete or not zero_delay_switch:
        return reject("unsupported_post_pi_or_actuator_branch")
    if phase is not TrajectoryPhase.RELEASE:
        return reject("not_release_phase")
    if guard_active:
        return reject("guard_active")
    if not _valid_pi_snapshot(pi_snapshot):
        return reject("invalid_pi_snapshot")
    if pi_snapshot.p_state.freeze_deadband:
        return reject("frozen_proportional_branch")
    numbers = (
        context.target_temp, context.nominal_reference, context.current_temp,
        context.ext_temp, context.a, context.b, context.committed_u,
        context.remaining_cycle_min, context.stop_deadtime_s,
        profile_nominal, deadband_c, cycle_min,
        previous_requested_power, previous_projected_power,
    )
    if not all(_finite_number(value) for value in numbers):
        return reject("invalid_numeric_evidence")
    if context.measured_slope_h is not None and not _finite_number(context.measured_slope_h):
        return reject("invalid_slope")
    if (context.hvac_mode not in (VThermHvacMode_HEAT, VThermHvacMode_COOL)
            or normalize_model(context.a, context.b, context.hvac_mode) is None):
        return reject("invalid_signed_model")
    if (deadband_c < 0.0 or cycle_min <= 0.0 or not isfinite(cycle_min * 60.0)
            or not 0.0 <= context.remaining_cycle_min <= cycle_min
            or context.stop_deadtime_s <= 0.0
            or not all(0.0 <= value <= 1.0 for value in (
                context.committed_u, previous_requested_power, previous_projected_power))):
        return reject("invalid_command_or_horizon")
    if profile_nominal != context.nominal_reference:
        return reject("profile_context_mismatch")
    if pi_snapshot.p_state.deadband_c != deadband_c:
        return reject("snapshot_deadband_mismatch")
    if abs(profile_nominal - context.target_temp) > TRAJECTORY_COMPLETE_EPS_C:
        return reject("profile_not_converged")

    direction = -1.0 if context.hvac_mode == VThermHvacMode_COOL else 1.0
    raw_error = direction * (context.target_temp - context.current_temp)
    # RELEASE can begin far from the target. Restrict eligibility to the
    # terminal controller band.
    if not 0.0 < raw_error <= deadband_c + DEADBAND_HYSTERESIS:
        return reject("outside_terminal_approach_band")
    requested = pi_snapshot.u_i + pi_snapshot.u_ff + pi_snapshot.project_p(raw_error)
    if not isfinite(requested):
        return reject("invalid_raw_pi_projection")
    requested = min(max(requested, 0.0), 1.0)
    projected = project_cycle_command(requested, cycle_min).projected_power
    # Do not obtain admission merely through downward PWM truncation. The
    # demand-direction model sees the envelope of requested and projected power.
    forecast_power = max(requested, projected)
    prediction = predict_segmented_1r1c(
        current_temp=context.current_temp, ext_temp=context.ext_temp,
        a=context.a, b=context.b, hvac_mode=context.hvac_mode,
        segments=(
            PredictionSegment(context.remaining_cycle_min, context.committed_u),
            PredictionSegment(context.stop_deadtime_s / 60.0, forecast_power),
        ),
    )
    if prediction is None:
        return reject("raw_forecast_failed")
    peak = max(direction * (temperature - context.target_temp)
               for temperature in (context.current_temp, *prediction.temperatures))
    reserves = compute_dynamic_reserve_candidates(
        hvac_mode=context.hvac_mode, measured_slope_h=context.measured_slope_h,
        predicted_temperature_change_c=prediction.terminal_temp - context.current_temp,
        stop_deadtime_s=context.stop_deadtime_s,
        rho_margin=REFERENCE_GOVERNOR_RHO_MARGIN,
        margin_max_c=REFERENCE_GOVERNOR_MARGIN_MAX_C,
    )
    if reserves is None:
        return reject("invalid_reserve_evidence")
    reserve = select_dynamic_reserve(reserves, REFERENCE_GOVERNOR_RESERVE_SELECTOR)
    if reserve is None:
        return reject("invalid_reserve_selector")
    # Compare both commands against every existing command boundary. A small
    # proportional-reference difference alone does not establish continuity.
    deltas = tuple(abs(value - previous) for value in (requested, projected)
                   for previous in (previous_requested_power, previous_projected_power,
                                    context.committed_u))
    model_admissible = peak + reserve <= deadband_c
    continuous = max(deltas) <= TRAJECTORY_BUMPLESS_MAX_U_DELTA
    reason = ("raw_forecast_outside_target_band" if not model_admissible
              else "command_jump" if not continuous else "release_candidate")
    bound = context.target_temp + direction * deadband_c
    if not isfinite(bound):
        return reject("invalid_target_band_bound")
    return BrakingReleaseDecision(
        release_candidate=model_admissible and continuous,
        model_admissible=model_admissible, reason=reason,
        raw_requested_power=requested, raw_projected_power=projected,
        forecast_power=forecast_power, prediction=prediction,
        signed_peak_above_target_c=peak, dynamic_reserve_c=reserve,
        target_band_bound_c=bound, command_deltas=deltas,
    )
