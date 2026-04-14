"""
FF3 short-horizon predictive feed-forward for Smart-PI.

FF3 is a conservative pseudo-MPC layer:
  - local authority only
  - one- or two-cycle horizon only
  - scratch twin clone only
  - disturbance-recovery context only
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from ..hvac_mode import VThermHvacMode_HEAT
from .const import (
    GovernanceRegime,
    FF3_DELTA_U,
    FF3_MAX_AUTHORITY,
    FF3_SCORE_EPS_C,
    FF3_LAMBDA_DU,
    FF3_NEARBAND_GAIN,
    FF3_PREDICTION_HORIZON_MIN,
    clamp,
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


def _clone_twin(twin: ThermalTwin1R1C) -> ThermalTwin1R1C:
    """Create a scratch twin clone from the current live twin state."""
    clone = ThermalTwin1R1C(dt_s=twin.dt_s, gamma=twin.gamma)
    clone.load_state(twin.save_state())
    return clone


def _simulate_n_cycles(
    *,
    twin: ThermalTwin1R1C,
    current_temp: float,
    ext_temp: float,
    setpoint: float,
    a: float,
    b: float,
    u: float,
    cycle_min: float,
    deadtime_heat_s: float | None,
    deadtime_cool_s: float | None,
    n_cycles: int,
) -> tuple[float | None, str]:
    """Predict indoor temperature after n cycles with constant power."""
    clone = _clone_twin(twin)
    deadtime_s = (deadtime_heat_s or 0.0) if u > 0.01 else (deadtime_cool_s or 0.0)
    t_pred: float | None = None
    dt_s = max(cycle_min, 0.0) * 60.0

    for _ in range(max(n_cycles, 1)):
        result = clone.step(
            tin_meas=current_temp,
            text_meas=ext_temp,
            a=a,
            b=b,
            u_now=u,
            deadtime_s=deadtime_s,
            sp=setpoint,
            dt_s=dt_s,
            mode="heat",
        )
        status = result.get("status", "invalid_status")
        if status != "ok":
            return None, status
        t_pred = result.get("T_pred")
        if t_pred is None or not isfinite(float(t_pred)):
            return None, "invalid_prediction"
        current_temp = float(t_pred)

    return float(t_pred), "ok"


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
) -> FF3Result:
    """Compute conservative FF3 for the next cycle."""
    horizon_cycles = max(2, int(round(FF3_PREDICTION_HORIZON_MIN / max(cycle_min, 1.0))))
    max_delta_u = FF3_MAX_AUTHORITY
    max_delta_steps = max(1, int(round(max_delta_u / FF3_DELTA_U)))

    if not enabled:
        return FF3Result(0.0, 0.0, False, "config_disabled", [], u_base, horizon_cycles)
    if startup_first_run:
        return FF3Result(0.0, 0.0, False, "first_cycle_after_restart", [], u_base, horizon_cycles)
    if hvac_mode != VThermHvacMode_HEAT:
        return FF3Result(0.0, 0.0, False, "cool_mode", [], u_base, horizon_cycles)
    if ext_temp is None:
        return FF3Result(0.0, 0.0, False, "missing_ext_temp", [], u_base, horizon_cycles)
    if not tau_reliable:
        return FF3Result(0.0, 0.0, False, "tau_not_reliable", [], u_base, horizon_cycles)
    if not twin_initialized:
        return FF3Result(0.0, 0.0, False, "twin_not_initialized", [], u_base, horizon_cycles)
    if not twin_reliable:
        return FF3Result(0.0, 0.0, False, twin_disabled_reason, [], u_base, horizon_cycles)
    if is_calibrating:
        return FF3Result(0.0, 0.0, False, "calibration", [], u_base, horizon_cycles)
    if power_shedding:
        return FF3Result(0.0, 0.0, False, "power_shedding", [], u_base, horizon_cycles)
    if setpoint_changed:
        return FF3Result(0.0, 0.0, False, "recent_setpoint_change", [], u_base, horizon_cycles)
    if in_deadband:
        return FF3Result(0.0, 0.0, False, "deadband", [], u_base, horizon_cycles)
    if not in_near_band:
        return FF3Result(0.0, 0.0, False, "not_near_band", [], u_base, horizon_cycles)
    if not disturbance_context_active:
        reason = disturbance_context_reason if disturbance_context_reason != "none" else "no_disturbance_context"
        return FF3Result(0.0, 0.0, False, reason, [], u_base, horizon_cycles)
    if last_sat == "SAT_HI":
        return FF3Result(0.0, 0.0, False, "saturated_high", [], u_base, horizon_cycles)
    if regime not in (GovernanceRegime.EXCITED_STABLE, GovernanceRegime.NEAR_BAND, GovernanceRegime.SATURATED):
        reason = "system_not_stable" if regime is None else f"regime_{regime.value}"
        return FF3Result(0.0, 0.0, False, reason, [], u_base, horizon_cycles)

    du = FF3_DELTA_U
    candidates = [
        clamp(u_base + (step * du), 0.0, 1.0)
        for step in range(-max_delta_steps, max_delta_steps + 1)
    ]

    base_score: float | None = None
    best_u = u_base
    best_score: float | None = None
    candidate_scores: list[dict[str, float]] = []

    for u in candidates:
        t_pred, status = _simulate_n_cycles(
            twin=twin,
            current_temp=current_temp,
            ext_temp=ext_temp,
            setpoint=setpoint,
            a=a,
            b=b,
            u=u,
            cycle_min=cycle_min,
            deadtime_heat_s=deadtime_heat_s,
            deadtime_cool_s=deadtime_cool_s,
            n_cycles=horizon_cycles,
        )
        if t_pred is None:
            return FF3Result(0.0, 0.0, False, f"simulation_{status}", [], u_base, horizon_cycles)

        score = abs(t_pred - setpoint) + FF3_LAMBDA_DU * abs(u - u_base)
        candidate_scores.append(
            {
                "u": round(u, 6),
                "t_pred": round(t_pred, 6),
                "score": round(score, 6),
            }
        )

        if abs(u - u_base) <= 1e-9:
            base_score = score
        if best_score is None or score < best_score:
            best_score = score
            best_u = u

    if base_score is None or best_score is None:
        return FF3Result(0.0, 0.0, False, "scoring_failed", candidate_scores, u_base, horizon_cycles)

    if best_score < (base_score - FF3_SCORE_EPS_C):
        u_ff3_raw = clamp(best_u - u_base, -max_delta_u, max_delta_u)
    else:
        u_ff3_raw = 0.0

    u_ff3_applied = u_ff3_raw
    if in_near_band:
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

    return FF3Result(
        u_ff3_raw=u_ff3_raw,
        u_ff3_applied=u_ff3_applied,
        enabled=abs(u_ff3_applied) > 1e-9,
        reason_disabled="none" if abs(u_ff3_applied) > 1e-9 else "score_not_better",
        candidate_scores=candidate_scores,
        selected_candidate=best_u,
        horizon_cycles=horizon_cycles,
    )
