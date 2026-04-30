"""
Smart-PI Diagnostics Module.

This module builds a diagnostics dict by reading internal state of the SmartPI
algorithm. As a "friend" module tightly coupled to SmartPI internals, some
protected member access is expected and intentional.
"""
# pylint: disable=protected-access
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, TYPE_CHECKING
from .const import (
    AB_A_SOFT_GATE_MIN_B,
    SmartPIPhase,
    DT_MAX_MIN,
    U_ON_MIN,
    AB_HISTORY_SIZE,
    FF_TRIM_EPSILON,
    FF_TRIM_RHO,
)

if TYPE_CHECKING:
    from ..algo import SmartPI

ESSENTIAL_KEYS = {
    "phase",
    "regulation_mode",
    "hysteresis_state",
    "on_percent",
    "error",
    "a",
    "b",
    "u_pi",
    "u_ff",
    "u_hold",
    "Kp",
    "Ki",
    "integral_error",
    "governance_regime",
    "last_decision_thermal",
    # Bootstrap / Learning (Always published)
    "bootstrap_progress",
    "bootstrap_state",
    "a_drift_state",
    "b_drift_state",
    "a_drift_buffer_count",
    "b_drift_buffer_count",
    "a_drift_last_reason",
    "b_drift_last_reason",
    # Deadtimes (Always published)
    "deadtime_heat_s",
    "deadtime_cool_s",
    # AutoCalibTrigger §6.1
    "autocalib_last_trigger_ts",
    "autocalib_next_check_ts",
    "autocalib_snapshot_age_h",
    # Sensor temperature
    "sensor_temperature",
    "ext_sensor_temperature",
    # Adaptive T_int filter
    "t_int_clean",
    # FFv2 essential keys
    "u_ff1",
    "u_ff2",
    "u_ff_final",
    "u_ff3",
    "u_db_nominal",
    "u_ff_eff",
    "ff3_enabled",
    "ff3_reason_disabled",
    "ff3_horizon_cycles",
    "twin_status",
    "ff3_twin_usable",
    "ab_confidence_state",
    "deadband_power_source",
    "ff2_trim_delta",
    "fftrim_last_reject_reason",
    "fftrim_last_update_reason",
    "fftrim_cycles_since_update",
    "integral_hold_active",
    "integral_hold_mode",
    "restart_reason",
    # Setpoint trajectory
    "filtered_setpoint",
    "setpoint_trajectory_active",
}


def _effective_ff3_reason_disabled(algo: SmartPI) -> str:
    """Return an FF3 disabled reason consistent with the current runtime state."""
    if algo._last_ff3_reason_disabled == "config_disabled":
        return "config_disabled"

    if algo._last_ff3_enabled or abs(algo.u_ff3) > 1e-9:
        return "none"
    if algo.deadband_mgr.in_deadband:
        return "deadband"
    if not algo.deadband_mgr.in_near_band:
        return "not_near_band"
    if algo._last_ff3_reason_disabled in {"deadband", "not_near_band"}:
        return "pending_cycle_boundary"
    return algo._last_ff3_reason_disabled


def _build_ff3_status(diag: Dict[str, Any]) -> str:
    """Return a compact FF3 runtime status for the published summary."""
    if diag.get("ff3_enabled") is True:
        return "active"

    reason = diag.get("ff3_reason_disabled") or "disabled"
    if reason == "none":
        return "inactive"
    return f"disabled:{reason}"


def _build_ab_learning_stage(algo: SmartPI, diag: Dict[str, Any]) -> str:
    """Return a compact AB learning stage for the published summary."""
    if algo.phase == SmartPIPhase.HYSTERESIS:
        return "bootstrap"

    samples_a = int(diag.get("learn_ok_count_a", 0))
    samples_b = int(diag.get("learn_ok_count_b", 0))
    if samples_a < AB_HISTORY_SIZE or samples_b < AB_HISTORY_SIZE:
        return "learning"

    if diag.get("tau_reliable") is True:
        return "monitoring"

    return "degraded"


def build_published_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return the compact SmartPI summary published in Home Assistant."""
    diag = _build_full_diagnostics(algo)

    return {
        "control": {
            "phase": diag["phase"],
            "mode": diag["regulation_mode"],
            "hysteresis_state": diag["hysteresis_state"],
            "kp": diag["Kp"],
            "ki": diag["Ki"],
            "restart_reason": diag["restart_reason"],
        },
        "power": {
            "current_cycle_percent": diag["committed_on_percent"],
            "next_cycle_percent": diag["on_percent"],
            "linear_current_cycle_percent": diag["linear_committed_on_percent"],
            "linear_next_cycle_percent": diag["linear_on_percent"],
            "valve_linearization_enabled": diag["valve_linearization_enabled"],
            "pi_percent": diag["u_pi"],
            "ff_percent": diag["u_ff"],
            "hold_percent": diag["u_hold"],
        },
        "temperature": {
            "sensor": diag["sensor_temperature"],
            "ext_sensor": diag["ext_sensor_temperature"],
            "error": diag["error"],
            "integral_error": diag["integral_error"],
            "integral_mode": diag["i_mode"],
            "integral_hold_mode": diag["integral_hold_mode"],
            "integral_guard_source": diag["integral_guard_source"],
        },
        "model": {
            "a": diag["a"],
            "b": diag["b"],
            "confidence": diag["ab_confidence_state"],
            "tau_reliable": diag["tau_reliable"],
            "deadtime_heat_s": diag["deadtime_heat_s"],
            "deadtime_cool_s": diag["deadtime_cool_s"],
        },
        "ab_learning": {
            "stage": _build_ab_learning_stage(algo, diag),
            "bootstrap_progress_percent": diag.get("bootstrap_progress"),
            "bootstrap_status": diag.get("bootstrap_state"),
            "accepted_samples_a": diag["learn_ok_count_a"],
            "accepted_samples_b": diag["learn_ok_count_b"],
            "target_samples": AB_HISTORY_SIZE,
            "last_reason": diag["learn_last_reason"],
            "a_drift_state": diag["a_drift_state"],
            "b_drift_state": diag["b_drift_state"],
        },
        "governance": {
            "regime": diag["governance_regime"],
            "thermal_update_decision": diag["last_decision_thermal"],
            "thermal_update_reason": diag["last_freeze_reason_thermal"],
        },
        "feedforward": {
            "ff3_status": _build_ff3_status(diag),
            "ff3_twin_usable": diag["ff3_twin_usable"],
            "twin_status": diag["twin_status"],
            "deadband_power_source": diag["deadband_power_source"],
        },
        "setpoint": {
            "filtered_setpoint": diag["filtered_setpoint"],
            "trajectory_active": diag["setpoint_trajectory_active"],
            "trajectory_source": diag["trajectory_source"],
            "landing_active": diag["landing_active"],
            "landing_reason": diag["landing_reason"],
            "landing_u_cap": diag["landing_u_cap"],
            "landing_coast_required": diag["landing_coast_required"],
        },
        "autocalib": {
            "state": diag["autocalib_state"],
            "model_degraded": diag["autocalib_model_degraded"],
            "last_trigger_ts": diag["autocalib_last_trigger_ts"],
            "next_check_ts": diag["autocalib_next_check_ts"],
            "snapshot_age_h": diag["autocalib_snapshot_age_h"],
        },
        "calibration": {
            "state": diag["calibration_state"],
            "retry_count": diag["calibration_retry_count"],
            "last_time": diag["last_calibration_time"],
        },
    }


def build_debug_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return published SmartPI blocks plus a nested debug payload."""
    published = build_published_diagnostics(algo)
    published["debug"] = _build_full_diagnostics(algo)
    return published


def _build_full_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return the complete SmartPI diagnostics payload."""
    tau_info = algo.est.tau_reliability()
    ff_result = algo._last_ff_result
    twin_diag = getattr(algo, '_last_twin_diag', None) or {}
    twin_status = twin_diag.get("status", "unavailable")
    ff3_twin_usable = (
        twin_status == "ok"
        and twin_diag.get("model_reliable") is True
        and twin_diag.get("warming_up") is not True
    )
    u_ff1 = ff_result.u_ff1 if ff_result else 0.0
    u_ff2 = ff_result.u_ff2 if ff_result else 0.0
    u_ff_final = ff_result.u_ff_final if ff_result else 0.0
    u_ff3 = ff_result.u_ff3 if ff_result else algo.u_ff3
    u_db_nominal = ff_result.u_db_nominal if ff_result else 0.0
    ff2_authority = FF_TRIM_RHO * max(u_ff1, FF_TRIM_EPSILON)

    diag = {
        # Phase / Mode
        "phase": algo.phase,
        "regulation_mode": "hysteresis" if algo.phase == SmartPIPhase.HYSTERESIS else "smartpi",
        "hysteresis_state": algo.ctl.hysteresis_state,
        # Model
        "a": round(algo.est.a, 6),
        "b": round(algo.est.b, 6),
        "tau_min": round(tau_info.tau_min, 1),
        "tau_reliable": tau_info.reliable,
        "learn_ok_count": int(algo.est.learn_ok_count),
        "learn_ok_count_a": int(algo.est.learn_ok_count_a),
        "learn_ok_count_b": int(algo.est.learn_ok_count_b),
        "learn_skip_count": int(algo.est.learn_skip_count),
        "learn_last_reason": str(algo.est.learn_last_reason),
        "learn_b_converged": algo.est.b_converged_for_a(),
        "learn_a_blocked_by_b": len(algo.est.b_meas_hist) < AB_A_SOFT_GATE_MIN_B,
        # A1/A2/A3 Diagnostics
        "diag_dTdt_method": algo.est.diag_dTdt_method,
        "diag_b_mad_over_med": round(algo.est.diag_b_mad_over_med, 3) if algo.est.diag_b_mad_over_med is not None else None,
        "diag_a_mad_over_med": round(algo.est.diag_a_mad_over_med, 3) if algo.est.diag_a_mad_over_med is not None else None,
        "diag_ab_bootstrap": algo.est.diag_ab_bootstrap,
        "diag_ab_points": algo.est.diag_ab_points,
        "diag_ab_mode_effective": algo.est.diag_ab_mode_effective,
        "a_drift_state": algo.est.a_drift.state,
        "b_drift_state": algo.est.b_drift.state,
        "a_drift_buffer_count": len(algo.est.a_drift.drift_buffer),
        "b_drift_buffer_count": len(algo.est.b_drift.drift_buffer),
        "a_drift_last_reason": algo.est.a_drift.last_reason,
        "b_drift_last_reason": algo.est.b_drift.last_reason,
        # Learning metadata
        "learning_start_dt": algo._learning_start_date,
        "learn_progress_percent": (
            round((algo.learn_t_int_s / (DT_MAX_MIN * 60)) * 100, 1)
            if algo.learn_win_active
            else 0
        ),
        "learn_u_avg": round(algo.learn_u_int / max(algo.learn_t_int_s, 1.0), 3) if algo.learn_win_active else None,
        "learn_u_cv": round(algo.learn_win._u_cv, 3) if algo.learn_win_active else None,
        "learn_u_std": round(algo.learn_win._u_std, 4) if algo.learn_win_active else None,
        "learn_time_remaining": (
            round(max(0, DT_MAX_MIN * 60 - algo.learn_t_int_s), 0)
            if algo.learn_win_active
            else None
        ),
        # PI
        "Kp": round(algo.Kp, 6),
        "Ki": round(algo.Ki, 6),
        "integral_error": round(algo.integral, 6),
        "i_mode": algo.last_i_mode,
        "integral_guard_active": algo.integral_guard.active,
        "integral_guard_source": algo.integral_guard.source.value,
        "integral_guard_mode": algo._last_integral_guard_mode,
        "sat": algo.last_sat,
        # Errors
        "error": round(algo.error, 4),
        "error_p": round(algo.error_p, 4),
        "error_filtered": round(algo.error_filtered, 4) if algo.error_filtered != 0.0 or algo._e_filt is not None else None,
        "temperature_slope_h": (
            round(algo.temperature_slope_h, 6)
            if algo.temperature_slope_h is not None else None
        ),
        # 2DOF/scheduling
        "near_band_deg": round(algo.near_band_deg, 3),
        "kp_near_factor": round(algo.kp_near_factor, 3),
        "ki_near_factor": round(algo.ki_near_factor, 3),
        "sign_flip_leak": round(algo.sign_flip_leak, 3),
        "sign_flip_active": algo.sign_flip_active,
        # Output
        "u_ff": round(algo.u_ff, 6),
        "ff_raw": round(algo._last_ff_raw, 6),
        "ff_reason": algo._last_ff_reason,
        "u_pi": round(algo.u_pi, 6),
        "u_hold": round(algo.ctl.u_hold, 6),
        "ff_warmup_ok_count": int(algo.ff_warmup_ok_count),
        "ff_warmup_cycles": int(algo.ff_warmup_cycles),
        "ff_scale_unreliable_max": round(algo.ff_scale_unreliable_max, 3),
        # FFv3 signal chain
        "u_ff1": round(u_ff1, 6),
        "u_ff2": round(u_ff2, 6),
        "u_ff_final": round(u_ff_final, 6),
        "u_ff3": round(u_ff3, 6),
        "u_db_nominal": round(u_db_nominal, 6),
        "ff2_trim_delta": round(algo._last_ff_trim_delta, 6),
        "ff2_authority": round(ff2_authority, 6),
        "ff2_frozen": algo._ff_trim.frozen,
        "ff2_freeze_reason": algo._ff_trim.freeze_reason,
        "fftrim_last_reject_reason": algo._last_fftrim_reject_reason,
        "fftrim_last_update_reason": algo._last_fftrim_update_reason,
        "fftrim_cycles_since_update": int(algo._cycles_since_fftrim_update),
        "fftrim_cycle_admissible": algo._last_fftrim_cycle_admissible,
        # FF compatibility aliases
        "u_ff_ab": round(ff_result.u_ff_ab, 6) if ff_result else 0.0,
        "u_ff_trim": round(algo._ff_trim.u_ff_trim, 6),
        "u_ff_base": round(ff_result.u_ff_base, 6) if ff_result else 0.0,
        "u_ff_eff": round(ff_result.u_ff_eff, 6) if ff_result else 0.0,
        "ff3_enabled": algo._last_ff3_enabled,
        "ff3_reason_disabled": _effective_ff3_reason_disabled(algo),
        "ff3_candidate_scores": algo._last_ff3_candidate_scores,
        "ff3_selected_candidate": round(algo._last_ff3_selected_candidate, 6),
        "ff3_horizon_cycles": algo._last_ff3_horizon_cycles,
        "ff3_disturbance_active": algo._last_ff3_disturbance_active,
        "ff3_disturbance_reason": algo._last_ff3_disturbance_reason,
        "ff3_disturbance_kind": algo._last_ff3_disturbance_kind,
        "ff3_residual_persistent": algo._last_ff3_residual_persistent,
        "ff3_dynamic_coherent": algo._last_ff3_dynamic_coherent,
        "twin_status": twin_status,
        "ff3_twin_usable": ff3_twin_usable,
        "integral_hold_active": algo.ctl.integral_hold_active,
        "integral_hold_mode": algo.ctl.integral_hold_mode,
        "integral_hold_reason": algo.ctl.integral_hold_mode,
        "restart_reason": algo._last_restart_reason,
        "signed_error_mode": "positive_means_hvac_demand",
        "ab_confidence_state": algo._ab_confidence.state.value,
        # FFv2 freeze reasons
        "trim_freeze_reason": algo._ff_trim.freeze_reason,
        # FFv2 regime tracking
        "regime_prev": algo._last_regime_prev,
        "sat_persistent_cycles": algo._sat_persistent_cycles,
        "cycles_since_reset": int(algo.cycles_since_reset),
        "on_percent": round(algo.on_percent, 6),
        "calculated_on_percent": round(algo.calculated_on_percent, 6),
        "committed_on_percent": round(algo.committed_on_percent, 6),
        "linear_on_percent": round(algo.linear_on_percent, 6),
        "linear_committed_on_percent": round(algo.linear_committed_on_percent, 6),
        "valve_linearization_enabled": algo.valve_curve.params is not None,
        "cycle_min": round(algo.cycle_min, 3),
        # Setpoint trajectory
        "filtered_setpoint": None if algo.sp_mgr.effective_setpoint is None else round(algo.sp_mgr.effective_setpoint, 2),
        "setpoint_trajectory_active": algo.sp_mgr.trajectory_active,
        "trajectory_start_sp": (
            round(algo.sp_mgr.trajectory_start_setpoint, 3)
            if algo.sp_mgr.trajectory_start_setpoint is not None else None
        ),
        "trajectory_target_sp": (
            round(algo.sp_mgr.trajectory_target_setpoint, 3)
            if algo.sp_mgr.trajectory_target_setpoint is not None else None
        ),
        "trajectory_tau_ref": (
            round(algo.sp_mgr.trajectory_tau_ref_min, 3)
            if algo.sp_mgr.trajectory_tau_ref_min is not None else None
        ),
        "trajectory_elapsed_s": round(algo.sp_mgr.trajectory_elapsed_s, 3),
        "trajectory_phase": algo.sp_mgr.trajectory_phase.value,
        "trajectory_source": algo.sp_mgr.trajectory_source,
        "trajectory_pending_target_change_braking": algo.sp_mgr.trajectory_pending_target_change_braking,
        "trajectory_braking_needed": algo.sp_mgr.trajectory_braking_needed,
        "trajectory_model_ready": algo.sp_mgr.trajectory_model_ready,
        "trajectory_remaining_cycle_min": round(algo.sp_mgr.trajectory_remaining_cycle_min, 3),
        "trajectory_next_cycle_u_ref": round(algo.sp_mgr.trajectory_next_cycle_u_ref, 6),
        "trajectory_bumpless_u_delta": (
            round(algo.sp_mgr.trajectory_bumpless_u_delta, 6)
            if algo.sp_mgr.trajectory_bumpless_u_delta is not None else None
        ),
        "trajectory_bumpless_ready": algo.sp_mgr.trajectory_bumpless_ready,
        # Setpoint landing (HEAT-only)
        "landing_active": algo.sp_mgr.landing_active,
        "landing_reason": algo.sp_mgr.landing_reason,
        "landing_u_cap": (
            round(algo.sp_mgr.landing_u_cap, 6)
            if algo.sp_mgr.landing_u_cap is not None else None
        ),
        "landing_sp_for_p_cap": (
            round(algo.sp_mgr.landing_sp_for_p_cap, 3)
            if algo.sp_mgr.landing_sp_for_p_cap is not None else None
        ),
        "landing_predicted_temperature": (
            round(algo.sp_mgr.landing_predicted_temperature, 3)
            if algo.sp_mgr.landing_predicted_temperature is not None else None
        ),
        "landing_predicted_rise": (
            round(algo.sp_mgr.landing_predicted_rise, 3)
            if algo.sp_mgr.landing_predicted_rise is not None else None
        ),
        "landing_target_margin": (
            round(algo.sp_mgr.landing_target_margin, 3)
            if algo.sp_mgr.landing_target_margin is not None else None
        ),
        "landing_release_allowed": algo.sp_mgr.landing_release_allowed,
        "landing_coast_required": algo.sp_mgr.landing_coast_required,
        "landing_non_constraining_count": algo.sp_mgr.landing_non_constraining_count,
        "landing_u_cmd_before_cap": (
            round(algo.sp_mgr.landing_u_cmd_before_cap, 6)
            if algo.sp_mgr.landing_u_cmd_before_cap is not None else None
        ),
        "landing_u_cmd_after_cap": (
            round(algo.sp_mgr.landing_u_cmd_after_cap, 6)
            if algo.sp_mgr.landing_u_cmd_after_cap is not None else None
        ),
        # Resume skip
        "learning_resume_ts": int(algo.learning_resume_ts) if algo.learning_resume_ts else None,
        # Anti-windup tracking diagnostics
        "u_cmd": round(algo.u_cmd, 6),
        "u_limited": round(algo.u_limited, 6),
        "u_applied": round(algo.u_applied, 6),
        "aw_du": round(algo.aw_du, 6),
        "forced_by_timing": algo.forced_by_timing,
        # Deadband state
        "in_deadband": algo.in_deadband,
        "in_core_deadband": algo._tau_reliable and abs(algo.error) < max(algo.deadband_c, 0.0),
        "in_near_band": algo.in_near_band,
        "deadband_power_source": algo.ctl.deadband_power_source,
        # Setpoint boost state
        "setpoint_boost_active": algo.sp_mgr.boost_active,
        "hysteresis_thermal_guard": algo.ctl.hysteresis_thermal_guard,
        # Dead Time (Smart-PI v2)
        "deadtime_heat_s": algo.dt_est.deadtime_heat_s,
        "deadtime_heat_reliable": algo.dt_est.deadtime_heat_reliable,
        "deadtime_cool_s": algo.dt_est.deadtime_cool_s,
        "deadtime_cool_reliable": algo.dt_est.deadtime_cool_reliable,
        "in_deadtime_window": algo.in_deadtime_window,
        "kp_source": algo.gain_scheduler.kp_source,
        "deadtime_skip_count_a": algo._deadtime_skip_count_a,
        "deadtime_skip_count_b": algo._deadtime_skip_count_b,
        "deadtime_state": algo.dt_est.state,
        "deadtime_last_power": algo.dt_est.last_power,
        "deadtime_heat_start_time": algo.dt_est.heat_start_time,
        "deadtime_cool_start_time": algo.dt_est.cool_start_time,
        # Near-Band Auto (Phase 2) - delegated to DeadbandManager
        "near_band_below_deg": algo.deadband_mgr.near_band_below_deg,
        "near_band_above_deg": algo.deadband_mgr.near_band_above_deg,
        "near_band_source": algo.deadband_mgr.near_band_source,
        # Guard Cut
        "guard_cut_active": algo.guard_cut_active,
        "guard_cut_count": algo.guard_cut_count,
        "guard_kick_active": algo.guard_kick_active,
        "guard_kick_count": algo.guard_kick_count,
        # Forced Calibration
        "calibration_state": algo.calibration_state,
        "last_calibration_time": (datetime.fromtimestamp(algo.calibration_mgr.last_calibration_time).isoformat() if algo.calibration_mgr.last_calibration_time else None),
        "calibration_retry_count": algo.calibration_mgr.retry_count,
        # AutoCalibTrigger §6.1
        "autocalib_state": algo.autocalib.state.value,
        "autocalib_waiting_reason": algo.autocalib.waiting_reason.value,
        "autocalib_model_degraded": algo.autocalib.model_degraded,
        "autocalib_triggered_params": algo.autocalib.triggered_params,
        "autocalib_retry_count": algo.autocalib.retry_count,
        "autocalib_last_trigger_ts": algo.autocalib.last_trigger_ts_iso,
        "autocalib_next_check_ts": algo.autocalib.next_check_ts_iso,
        "autocalib_snapshot_age_h": algo.autocalib.snapshot_age_h,
        "autocalib_dt_cool_unavailable": algo.autocalib.snap_dt_cool_unavailable,
        # Safety-First Governance
        "governance_regime": algo.gov._current_regime.value,
        "governance_cycle_regimes": [r.value for r in algo.gov._cycle_regimes],
        # Governance diagnostics
        "last_freeze_reason_thermal": algo.gov.last_freeze_reason_thermal.value,
        "last_freeze_reason_gains": algo.gov.last_freeze_reason_gains.value,
        "last_decision_thermal": algo.gov.last_decision_thermal.value,
        "last_decision_gains": algo.gov.last_decision_gains.value,
        # Setpoint boost aliases
        "boost_active": algo.sp_mgr.boost_active,
        # Sensor temperature (unrounded) used in last calculation cycle
        "sensor_temperature": algo._last_current_temp,
        "ext_sensor_temperature": algo._last_ext_temp,
        # Adaptive T_int filter
        "t_int_raw": algo._last_current_temp,
        "t_int_lp": round(algo.tint_filter.t_int_lp, 4) if algo.tint_filter.t_int_lp is not None else None,
        "t_int_clean": round(algo.tint_filter.t_int_clean, 4) if algo.tint_filter.t_int_clean is not None else None,
        "sigma_t_int": round(algo.tint_filter.sigma, 5),
        "adaptive_tint_update": algo.tint_filter.last_update_was_publish,
        "adaptive_tint_hold_duration_s": round(algo.tint_filter.hold_duration_s, 1),
    }

    # --- Bootstrap / Learning Diagnostics ---
    # Only added if available (Hysteresis phase)
    if algo.bootstrap_progress is not None:
        diag["bootstrap_progress"] = algo.bootstrap_progress
        diag["bootstrap_state"] = algo.bootstrap_state


    # --- Thermal Twin / ETA (diagnostics-only) ---
    if twin_diag and twin_diag.get("status") == "ok":
        diag["pred"] = {
            "twin_status": twin_status,
            "twin_T_hat": round(twin_diag.get("T_hat_next", 0), 3),
            "twin_T_pred": round(twin_diag.get("T_pred", 0), 3),
            "twin_innovation": round(twin_diag.get("innovation", 0), 3),
            "twin_rmse_30": twin_diag.get("rmse_30"),
            "twin_model_reliable": twin_diag.get("model_reliable"),
            "twin_perturbation_dTdt": twin_diag.get("perturbation_dTdt"),
            "twin_cusum_pos": twin_diag.get("cusum_pos"),
            "twin_cusum_neg": twin_diag.get("cusum_neg"),
            "twin_external_gain": twin_diag.get("external_gain_detected"),
            "twin_external_loss": twin_diag.get("external_loss_detected"),
            "twin_T_steady": twin_diag.get("T_steady"),
            "twin_T_steady_reliable": twin_diag.get("T_steady_reliable"),
            "twin_T_steady_max": twin_diag.get("T_steady_max"),
            "twin_T_steady_immediate": twin_diag.get("T_steady_immediate"),
            "twin_T_steady_passive": twin_diag.get("T_steady_passive"),
            "twin_setpoint_reachable": twin_diag.get("setpoint_reachable"),
            "twin_setpoint_reachable_max": twin_diag.get("setpoint_reachable_max"),
            "twin_emitter_saturated": twin_diag.get("emitter_saturated"),
            "twin_cooling_model_available": twin_diag.get("cooling_model_available"),
            "twin_d_hat_fresh": twin_diag.get("d_hat_fresh"),
            "twin_warming_up": twin_diag.get("warming_up"),
            "twin_u_eff": twin_diag.get("u_eff"),
            "twin_deadtime_s": twin_diag.get("deadtime_s"),
            "twin_dead_steps": twin_diag.get("dead_steps"),
            "twin_T_hat_error": twin_diag.get("T_hat_error"),
            "twin_rmse_pure": twin_diag.get("rmse_pure"),
            "twin_innovation_bias": twin_diag.get("innovation_bias"),
            "twin_bias_warning": twin_diag.get("bias_warning", False),
            "twin_auto_reset_triggered": twin_diag.get("auto_reset_triggered", False),
            "twin_reset_count": twin_diag.get("reset_count", 0),
            "eta_s": twin_diag.get("eta_eta_s"),
            "eta_u": twin_diag.get("eta_u"),
            "eta_reason": twin_diag.get("eta_reason"),
            "twin_d_hat": twin_diag.get("d_hat_ema"),
        }

    return diag


def build_diagnostics(algo: SmartPI, debug_mode: bool = False) -> Dict[str, Any]:
    """Return diagnostic information for the SmartPI algorithm API."""
    if debug_mode:
        return _build_full_diagnostics(algo)

    diag = _build_full_diagnostics(algo)

    return {k: v for k, v in diag.items() if k in ESSENTIAL_KEYS}
