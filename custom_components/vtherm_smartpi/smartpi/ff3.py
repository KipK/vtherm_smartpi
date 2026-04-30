"""
FF3 short-horizon predictive feed-forward for Smart-PI.

FF3 is a conservative pseudo-MPC layer:
  - local authority only
  - deadtime-aware horizon only
  - open-loop prediction only
  - disturbance-recovery context only
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..hvac_mode import VThermHvacMode_HEAT
from .const import (
    GovernanceRegime,
    FF3_ACTION_SENSITIVITY_EPS_C,
    FF3_COST_DU_WEIGHT,
    FF3_COST_OVERSHOOT_WEIGHT,
    FF3_COST_TERMINAL_WEIGHT,
    FF3_COST_TRACKING_WEIGHT,
    FF3_DELTA_U,
    FF3_MAX_AUTHORITY,
    FF3_NEARBAND_GAIN,
    FF3_SCORE_EPS_COST,
    clamp,
)
from .ff3_predictor import (
    FF3Horizon,
    FF3OpenLoopPrediction,
    compute_ff3_action_sensitivity,
    compute_ff3_horizon,
    predict_ff3_open_loop,
)
from .thermal_twin_1r1c import ThermalTwin1R1C


@dataclass(frozen=True)
class FF3Result:
    """FF3 computation result for one cycle."""

    u_ff3_raw: float
    u_ff3_applied: float
    enabled: bool
    reason_disabled: str
    candidate_scores: list[dict[str, float]]
    selected_candidate: float
    horizon_cycles: int = 1
    deadtime_cycles: int = 0
    horizon_capped: bool = False
    action_sensitivity: float = 0.0
    prediction_quality: str = "unavailable"


def _compute_near_band_ff3_scale(
    *,
    current_temp: float,
    setpoint: float,
    deadband_c: float,
    near_band_below_deg: float,
    near_band_above_deg: float,
) -> float:
    """Return a continuous FF3 authority scale inside the near-band."""
    error = setpoint - current_temp
    deadband_entry = max(deadband_c, 0.0)
    near_band_edge = near_band_below_deg if error >= 0.0 else near_band_above_deg
    near_band_span = max(near_band_edge - deadband_entry, 0.0)

    if near_band_span <= 1e-9:
        return 1.0

    return clamp((abs(error) - deadband_entry) / near_band_span, 0.0, 1.0)


def _score_prediction(
    *,
    prediction: FF3OpenLoopPrediction,
    setpoint: float,
    u: float,
    u_base: float,
) -> tuple[float, float, float]:
    tracking_cost = 0.0
    overshoot_cost = 0.0
    for temperature in prediction.temperatures:
        error = setpoint - temperature
        tracking_cost += FF3_COST_TRACKING_WEIGHT * error * error
        overshoot = max(0.0, temperature - setpoint)
        overshoot_cost += FF3_COST_OVERSHOOT_WEIGHT * overshoot * overshoot

    terminal_error = setpoint - prediction.terminal_temperature
    terminal_cost = FF3_COST_TERMINAL_WEIGHT * terminal_error * terminal_error
    move_cost = FF3_COST_DU_WEIGHT * (u - u_base) * (u - u_base)
    cost = tracking_cost + terminal_cost + overshoot_cost + move_cost
    return cost, terminal_error, overshoot_cost


def compute_ff3(
    *,
    enabled: bool,
    twin: ThermalTwin1R1C,
    twin_reliable: bool,
    twin_initialized: bool,
    tau_reliable: bool,
    ext_temp: float | None,
    hvac_mode: Any,
    regime: GovernanceRegime | None,
    in_deadband: bool,
    in_near_band: bool,
    is_calibrating: bool,
    power_shedding: bool,
    setpoint_changed: bool,
    startup_first_run: bool,
    last_sat: str,
    u_base: float,
    current_temp: float,
    setpoint: float,
    deadband_c: float,
    near_band_below_deg: float,
    near_band_above_deg: float,
    cycle_min: float,
    a: float,
    b: float,
    deadtime_heat_s: float | None,
    deadtime_cool_s: float | None,
    twin_disabled_reason: str = "twin_not_reliable",
    disturbance_context_active: bool = True,
    disturbance_context_reason: str = "none",
    authority_factor: float = 1.0,
    prediction_quality: str = "reliable",
) -> FF3Result:
    """Compute conservative FF3 for the next cycle."""
    horizon = compute_ff3_horizon(
        cycle_min=cycle_min,
        deadtime_heat_s=deadtime_heat_s,
    )

    def _disabled_result(
        *,
        reason: str,
        u_base: float,
        horizon: FF3Horizon,
        prediction_quality: str,
        candidate_scores: list[dict[str, float]] | None = None,
        action_sensitivity: float = 0.0,
    ) -> FF3Result:
        return FF3Result(
            u_ff3_raw=0.0,
            u_ff3_applied=0.0,
            enabled=False,
            reason_disabled=reason,
            candidate_scores=candidate_scores or [],
            selected_candidate=u_base,
            horizon_cycles=horizon.horizon_cycles,
            deadtime_cycles=horizon.deadtime_cycles,
            horizon_capped=horizon.horizon_capped,
            action_sensitivity=action_sensitivity,
            prediction_quality=prediction_quality,
        )

    max_delta_u = FF3_MAX_AUTHORITY * clamp(authority_factor, 0.0, 1.0)
    max_delta_steps = max(1, int(round(max_delta_u / FF3_DELTA_U)))

    if not enabled:
        return _disabled_result(reason="config_disabled", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if startup_first_run:
        return _disabled_result(reason="first_cycle_after_restart", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if hvac_mode != VThermHvacMode_HEAT:
        return _disabled_result(reason="cool_mode", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if ext_temp is None:
        return _disabled_result(reason="missing_ext_temp", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if not tau_reliable:
        return _disabled_result(reason="tau_not_reliable", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if not twin_initialized:
        return _disabled_result(reason="twin_not_initialized", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if not twin_reliable:
        return _disabled_result(reason=twin_disabled_reason, u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if is_calibrating:
        return _disabled_result(reason="calibration", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if power_shedding:
        return _disabled_result(reason="power_shedding", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if setpoint_changed:
        return _disabled_result(reason="recent_setpoint_change", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if in_deadband:
        return _disabled_result(reason="deadband", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if not in_near_band:
        return _disabled_result(reason="not_near_band", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if not disturbance_context_active:
        reason = disturbance_context_reason if disturbance_context_reason != "none" else "no_disturbance_context"
        return _disabled_result(reason=reason, u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if last_sat == "SAT_HI":
        return _disabled_result(reason="saturated_high", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if regime not in (GovernanceRegime.EXCITED_STABLE, GovernanceRegime.NEAR_BAND, GovernanceRegime.SATURATED):
        reason = "system_not_stable" if regime is None else f"regime_{regime.value}"
        return _disabled_result(reason=reason, u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
    if max_delta_u <= 1e-9:
        return _disabled_result(reason="authority_zero", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)

    du = FF3_DELTA_U
    candidates: list[float] = []
    for step in range(-max_delta_steps, max_delta_steps + 1):
        candidate = clamp(u_base + (step * du), 0.0, 1.0)
        if not any(abs(existing - candidate) <= 1e-9 for existing in candidates):
            candidates.append(candidate)
    if not any(abs(existing - u_base) <= 1e-9 for existing in candidates):
        candidates.append(clamp(u_base, 0.0, 1.0))
        candidates.sort()

    base_score: float | None = None
    best_u = u_base
    best_score: float | None = None
    candidate_scores: list[dict[str, float]] = []
    predictions: list[FF3OpenLoopPrediction] = []

    for u in candidates:
        prediction = predict_ff3_open_loop(
            twin=twin,
            current_temp=current_temp,
            ext_temp=ext_temp,
            a=a,
            b=b,
            u_first_cycle=u,
            u_base=u_base,
            cycle_min=cycle_min,
            deadtime_heat_s=deadtime_heat_s,
            horizon_cycles=horizon.horizon_cycles,
        )
        if prediction.status != "ok":
            return _disabled_result(reason=f"simulation_{prediction.status}", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality)
        predictions.append(prediction)

        cost, terminal_error, overshoot_cost = _score_prediction(
            prediction=prediction,
            setpoint=setpoint,
            u=u,
            u_base=u_base,
        )
        candidate_scores.append(
            {
                "u": round(u, 6),
                "t_pred": round(prediction.terminal_temperature, 6),
                "score": round(cost, 6),
                "terminal_error": round(terminal_error, 6),
                "overshoot_cost": round(overshoot_cost, 6),
            }
        )

        if abs(u - u_base) <= 1e-9:
            base_score = cost
        if (
            best_score is None
            or cost < best_score - 1e-12
            or (
                abs(cost - best_score) <= 1e-12
                and abs(u - u_base) < abs(best_u - u_base)
            )
        ):
            best_score = cost
            best_u = u

    if base_score is None or best_score is None:
        return _disabled_result(reason="scoring_failed", u_base=u_base, horizon=horizon, prediction_quality=prediction_quality, candidate_scores=candidate_scores)

    action_sensitivity = compute_ff3_action_sensitivity(predictions)
    if action_sensitivity <= FF3_ACTION_SENSITIVITY_EPS_C:
        return _disabled_result(
            reason="horizon_no_candidate_effect",
            u_base=u_base,
            horizon=horizon,
            prediction_quality=prediction_quality,
            candidate_scores=candidate_scores,
            action_sensitivity=action_sensitivity,
        )

    if base_score - best_score >= FF3_SCORE_EPS_COST:
        u_ff3_raw = clamp(best_u - u_base, -max_delta_u, max_delta_u)
    else:
        u_ff3_raw = 0.0

    if u_ff3_raw == 0.0:
        return _disabled_result(
            reason="score_not_better",
            u_base=u_base,
            horizon=horizon,
            prediction_quality=prediction_quality,
            candidate_scores=candidate_scores,
            action_sensitivity=action_sensitivity,
        )

    authority_scale = _compute_near_band_ff3_scale(
        current_temp=current_temp,
        setpoint=setpoint,
        deadband_c=deadband_c,
        near_band_below_deg=near_band_below_deg,
        near_band_above_deg=near_band_above_deg,
    )
    u_ff3_applied = clamp(
        FF3_NEARBAND_GAIN * authority_scale * u_ff3_raw,
        -max_delta_u,
        max_delta_u,
    )

    if abs(u_ff3_applied) <= 1e-9:
        return _disabled_result(
            reason="authority_tapered_to_zero",
            u_base=u_base,
            horizon=horizon,
            prediction_quality=prediction_quality,
            candidate_scores=candidate_scores,
            action_sensitivity=action_sensitivity,
        )

    return FF3Result(
        u_ff3_raw=u_ff3_raw,
        u_ff3_applied=u_ff3_applied,
        enabled=True,
        reason_disabled="none",
        candidate_scores=candidate_scores,
        selected_candidate=best_u,
        horizon_cycles=horizon.horizon_cycles,
        deadtime_cycles=horizon.deadtime_cycles,
        horizon_capped=horizon.horizon_capped,
        action_sensitivity=action_sensitivity,
        prediction_quality=prediction_quality,
    )
