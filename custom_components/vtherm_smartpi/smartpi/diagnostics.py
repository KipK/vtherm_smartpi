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
    ABConfidenceState,
    SmartPIPhase,
    AB_HISTORY_SIZE,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_B,
    FF_TRIM_EPSILON,
    FF_TRIM_RHO,
    clamp,
)

if TYPE_CHECKING:
    from ..algo import SmartPI
    from .ab_estimator import TauReliability
    from .ff_trim_runtime import FFTrimCoordinator


def _effective_ff3_reason_disabled(algo: SmartPI) -> str:
    """Return an FF3 disabled reason consistent with the current runtime state."""
    if algo.ff3_state.reason_disabled == "config_disabled":
        return "config_disabled"

    if algo.ff3_state.enabled or abs(algo.u_ff3) > 1e-9:
        return "none"
    if algo.deadband_mgr.in_deadband:
        return "deadband"
    if not algo.deadband_mgr.in_near_band:
        return "not_near_band"
    if algo.ff3_state.reason_disabled in {"deadband", "not_near_band"}:
        return "pending_cycle_boundary"
    return algo.ff3_state.reason_disabled


def _build_ff3_status(algo: SmartPI) -> str:
    """Return a compact FF3 runtime status for the published summary."""
    if algo.ff3_state.enabled is True:
        return "active"

    reason = _effective_ff3_reason_disabled(algo) or "disabled"
    if reason == "none":
        return "inactive"
    return f"disabled:{reason}"


def _build_ab_learning_stage(
    algo: SmartPI, samples_a: int, samples_b: int, tau_reliable: bool
) -> str:
    """Return a compact AB learning stage for the published summary."""
    if algo.phase == SmartPIPhase.HYSTERESIS:
        return "bootstrap"

    if samples_a < AB_HISTORY_SIZE or samples_b < AB_HISTORY_SIZE:
        return "learning"

    if tau_reliable is True:
        return "monitoring"

    return "degraded"


def _build_ab_confidence_state(algo: SmartPI) -> str:
    """Return the public A/B confidence state for the current phase."""
    if algo.phase == SmartPIPhase.HYSTERESIS:
        return ABConfidenceState.AB_BOOTSTRAP.value

    return algo._ab_confidence.state.value


def _round_optional(value: float | None, digits: int) -> float | None:
    """Return the rounded value, None when the value is None."""
    return round(value, digits) if value is not None else None


def _session_counter(total: int, baseline: int) -> int:
    """Return a runtime counter relative to the current process baseline."""
    return max(0, int(total) - int(baseline))


def _percent(ratio: float) -> float:
    """Return a public percent value from an internal ratio rounded to 6 digits."""
    return round(float(round(ratio, 6)) * 100.0, 1)


def _build_fftrim_result(result: Any | None) -> Dict[str, Any] | None:
    """Build a stable public snapshot of one admissible FF trim result."""
    if result is None:
        return None

    def optional_round(value: float | None, digits: int = 6) -> float | None:
        return round(float(value), digits) if value is not None else None

    return {
        "reason": str(result.reason),
        "observation_mode": str(result.observation_mode),
        "duration_s": round(float(result.duration_s), 3),
        "measurement_count": int(result.measurement_count),
        "alignment_delay_s": optional_round(result.alignment_delay_s, 3),
        "power_coverage_ratio": round(float(result.power_coverage_ratio), 6),
        "mean_causal_power": optional_round(result.mean_causal_power),
        "mean_ff1": optional_round(result.mean_ff1),
        "mean_temperature": optional_round(result.mean_temperature),
        "mean_error": optional_round(result.mean_error),
        "mean_slope_h": optional_round(result.mean_slope_h),
        "observed_hold_power": optional_round(result.observed_hold_power),
        "target_trim": optional_round(result.target_trim),
        "correction": optional_round(result.correction),
        "mean_p_power": optional_round(result.mean_p_power),
        "mean_i_power": optional_round(result.mean_i_power),
        "mean_visible_ff_power": optional_round(result.mean_visible_ff_power),
        "mean_delivery_residual": optional_round(result.mean_delivery_residual),
        "physical_power_deficit": optional_round(result.physical_power_deficit),
        "decomposed_correction": optional_round(result.decomposed_correction),
    }


def _build_fftrim_transaction(transaction: Any | None) -> Dict[str, Any] | None:
    """Build a stable public snapshot of one applied FF trim transaction."""
    if transaction is None:
        return None

    return {
        "timestamp_utc": str(transaction.timestamp_utc),
        "observation_mode": str(transaction.observation_mode),
        "state": str(transaction.state),
        "reason": str(transaction.reason),
        "quality": str(transaction.quality),
        "requested_trim_delta": round(
            float(transaction.requested_trim_delta), 6
        ),
        "stored_trim_delta": round(float(transaction.stored_trim_delta), 6),
        "applied_trim_delta": round(float(transaction.applied_trim_delta), 6),
        "transferable_i_power": round(
            float(transaction.transferable_i_power), 6
        ),
        "requested_i_transfer": round(
            float(transaction.requested_i_transfer), 6
        ),
        "applied_i_transfer": round(float(transaction.applied_i_transfer), 6),
        "net_command_delta": round(float(transaction.net_command_delta), 6),
    }


def _build_valve_diagnostics(algo: SmartPI) -> Dict[str, Any] | None:
    """Return the VT valve settings seen by SmartPI, or None outside valve mode."""
    config = algo.valve_config_diag
    if config is None:
        return None

    # Linear demand (in percent) moved by one 1 % valve step at the committed
    # valve position, from the local slope of the inverse curve.
    step = None
    valve = algo.committed_on_percent
    if valve > 0.0:
        low = max(0.0, valve - 0.005)
        high = min(1.0, valve + 0.005)
        if high > low:
            slope = (algo.valve_curve.invert(high) - algo.valve_curve.invert(low)) / (high - low)
            step = round(slope, 3)
    return {**config, "quantization_step_linear_percent": step}


def _build_command_ownership(algo: SmartPI) -> Dict[str, Any]:
    """Build the latest request-to-receipt binding diagnostics."""
    tracker = algo._command_ownership
    binding = tracker.last_binding

    def _round(value: float | None) -> float | None:
        return round(float(value), 6) if value is not None else None

    requested = binding.requested_power
    realized = binding.realized_power
    return {
        "receipts_supported": algo.receipts_supported,
        "status": binding.status.value,
        "reason": binding.reason,
        "request_sequence": binding.request_id,
        "kind": binding.kind,
        "requested_power": _round(requested),
        "realized_power": _round(realized),
        "power_delta": (
            _round(realized - requested)
            if realized is not None and requested is not None
            else None
        ),
        "realized_on_time_sec": binding.realized_on_time_sec,
        "realized_off_time_sec": binding.realized_off_time_sec,
        "alterations": list(binding.alterations),
        "forced_by_timing": (
            "min_on_off" in binding.alterations if binding.kind is not None else None
        ),
        "rejection_counts": dict(tracker.rejection_counts),
    }


def _build_analysis_diagnostics(diag: Dict[str, Any]) -> Dict[str, Any]:
    """Return advanced diagnostics that remain useful to live consumers."""
    analysis = {
        "control": {
            "cycle_min": diag["cycle_min"],
            "error_filtered": diag["error_filtered"],
            "error_p": diag["error_p"],
            "aw_du": diag["aw_du"],
            "forced_by_timing": diag["forced_by_timing"],
            "integral_guard_mode": diag["integral_guard_mode"],
            "in_core_deadband": diag["in_core_deadband"],
            "near_band_below_deg": diag["near_band_below_deg"],
            "near_band_above_deg": diag["near_band_above_deg"],
            "near_band_source": diag["near_band_source"],
            "temperature_slope_h": diag["temperature_slope_h"],
        },
        "learning": {
            "learn_ok_count": diag["learn_ok_count"],
            "learn_skip_count": diag["learn_skip_count"],
            "ff_warmup_ok_count": diag["ff_warmup_ok_count"],
            "ff_warmup_cycles": diag["ff_warmup_cycles"],
            "window_mean_power": diag["learn_u_avg"],
            "window_power_cv": diag["learn_u_cv"],
            "window_power_std": diag["learn_u_std"],
        },
        "trajectory": {
            "start_setpoint": diag["trajectory_start_sp"],
            "target_setpoint": diag["trajectory_target_sp"],
            "tau_ref_min": diag["trajectory_tau_ref"],
            "elapsed_s": diag["trajectory_elapsed_s"],
            "phase": diag["trajectory_phase"],
            "pending_target_change_braking": diag[
                "trajectory_pending_target_change_braking"
            ],
            "braking_needed": diag["trajectory_braking_needed"],
            "model_ready": diag["trajectory_model_ready"],
            "remaining_cycle_min": diag["trajectory_remaining_cycle_min"],
            "next_cycle_reference": diag["trajectory_next_cycle_u_ref"],
            "bumpless_delta": diag["trajectory_bumpless_u_delta"],
            "bumpless_ready": diag["trajectory_bumpless_ready"],
        },
        "deadtime": {
            "state": diag["deadtime_state"],
            "last_power": diag["deadtime_last_power"],
        },
        "governance": {
            "last_freeze_reason_gains": diag["last_freeze_reason_gains"],
            "last_decision_gains": diag["last_decision_gains"],
            "kp_source": diag["kp_source"],
        },
        "feedforward": {
            "reason": diag["ff_reason"],
            "u_ff1": diag["u_ff1"],
            "u_ff2": diag["u_ff2"],
            "u_ff3": diag["u_ff3"],
            "u_ff_final": diag["u_ff_final"],
            "u_ff_effective": diag["u_ff_eff"],
            "ff2_authority": diag["ff2_authority"],
            "ff2_frozen": diag["ff2_frozen"],
            "ff2_freeze_reason": diag["ff2_freeze_reason"],
            "ff2_trim_delta": diag["ff2_trim_delta"],
            "ff3_raw_reason_disabled": diag["ff3_raw_reason_disabled"],
            "ff3_selected_candidate": diag["ff3_selected_candidate"],
            "ff3_horizon_cycles": diag["ff3_horizon_cycles"],
            "ff3_deadtime_cycles": diag["ff3_deadtime_cycles"],
            "ff3_horizon_capped": diag["ff3_horizon_capped"],
            "ff3_action_sensitivity": diag["ff3_action_sensitivity"],
            "ff3_prediction_quality": diag["ff3_prediction_quality"],
            "ff3_authority_factor": diag["ff3_authority_factor"],
            "ff3_disturbance_active": diag["ff3_disturbance_active"],
            "ff3_disturbance_reason": diag["ff3_disturbance_reason"],
            "ff3_disturbance_kind": diag["ff3_disturbance_kind"],
            "ff3_residual_persistent": diag["ff3_residual_persistent"],
            "ff3_dynamic_coherent": diag["ff3_dynamic_coherent"],
        },
    }
    if pred := diag.get("pred"):
        analysis["twin"] = pred
    return analysis


def _integral_time_min(kp: float, ki: float) -> float | None:
    """Return the effective integral time Kp / Ki in minutes, None when Ki <= 0."""
    if ki is None or kp is None or ki <= 0.0:
        return None
    return round(kp / ki, 1)


def _build_control(algo: SmartPI) -> Dict[str, Any]:
    """Return the regulation phase, gains and band flags."""
    phase = algo.phase
    kp = round(algo.Kp, 6)
    ki = round(algo.Ki, 6)
    return {
        "phase": phase,
        "mode": "hysteresis" if phase == SmartPIPhase.HYSTERESIS else "smartpi",
        "hysteresis_state": algo.ctl.hysteresis_state,
        "kp": kp,
        "ki": ki,
        "ti_min": _integral_time_min(kp, ki),
        "restart_reason": algo._last_restart_reason,
        "saturation_state": algo.last_sat,
        "in_deadband": algo.in_deadband,
        "in_near_band": algo.in_near_band,
        "in_deadtime_window": algo.in_deadtime_window,
    }


def _build_power(algo: SmartPI) -> Dict[str, Any]:
    """Return the command chain in percent."""
    return {
        "current_cycle_percent": _percent(algo.committed_on_percent),
        "next_cycle_percent": _percent(algo.on_percent),
        "linear_current_cycle_percent": _percent(algo.linear_committed_on_percent),
        "linear_next_cycle_percent": _percent(algo.linear_on_percent),
        "valve_linearization_enabled": algo.valve_curve.params is not None,
        "pi_percent": _percent(algo.u_pi),
        "ff_percent": _percent(algo.u_ff),
        "hold_percent": _percent(algo.ctl.u_hold),
        "command_percent": _percent(algo.u_cmd),
        "limited_percent": _percent(algo.u_limited),
        "applied_percent": _percent(algo.u_applied),
    }


def _build_temperature(algo: SmartPI) -> Dict[str, Any]:
    """Return the measured temperatures, the error and the integral state."""
    return {
        "indoor": algo._last_current_temp,
        "outdoor": algo._last_ext_temp,
        "error": round(algo.error, 4),
        "integral_error": round(algo.integral, 6),
        "integral_mode": algo.last_i_mode,
        "integral_hold_mode": algo.ctl.integral_hold_mode,
        "integral_hold_source": algo.ctl.last_integral_hold_source,
        "integral_guard_source": algo.integral_guard.source.value,
    }


def _build_model(algo: SmartPI, tau_info: TauReliability) -> Dict[str, Any]:
    """Return the learned A/B model and the dead times."""
    est = algo.est
    dt_est = algo.dt_est
    return {
        "a": round(est.a, 6),
        "b": round(est.b, 6),
        "confidence": _build_ab_confidence_state(algo),
        "tau_reliable": tau_info.reliable,
        "tau_min": round(tau_info.tau_min, 1),
        "deadtime_heat_s": dt_est.deadtime_heat_s,
        "deadtime_cool_s": dt_est.deadtime_cool_s,
        "deadtime_heat_reliable": dt_est.deadtime_heat_reliable,
        "deadtime_cool_reliable": dt_est.deadtime_cool_reliable,
        "a_stability_ratio": _round_optional(est.diag_a_mad_over_med, 3),
        "b_stability_ratio": _round_optional(est.diag_b_mad_over_med, 3),
    }


def _build_learning(algo: SmartPI, tau_reliable: bool) -> Dict[str, Any]:
    """Return the A/B learning progress and the drift states."""
    est = algo.est
    samples_a = int(est.learn_ok_count_a)
    samples_b = int(est.learn_ok_count_b)
    return {
        "enabled": algo.learning_enabled,
        "stage": _build_ab_learning_stage(algo, samples_a, samples_b, tau_reliable),
        "bootstrap_progress_percent": _bootstrap_progress(algo),
        "bootstrap_status": _bootstrap_state(algo),
        "emea_samples_a": algo.meas_count_a,
        "emea_samples_b": algo.meas_count_b,
        "bootstrap_target_a": AB_MIN_SAMPLES_A,
        "bootstrap_target_b": AB_MIN_SAMPLES_B,
        "history_target": AB_HISTORY_SIZE,
        "accepted_updates_a": samples_a,
        "accepted_updates_b": samples_b,
        "learn_b_converged": est.b_converged_for_a(),
        "last_reason": str(est.learn_last_reason),
        "a_drift_state": est.a_drift.state,
        "b_drift_state": est.b_drift.state,
    }


def _build_governance(algo: SmartPI) -> Dict[str, Any]:
    """Return the governance regime and the thermal update decision."""
    gov = algo.gov
    return {
        "regime": gov._current_regime.value,
        "thermal_update_decision": gov.last_decision_thermal.value,
        "thermal_update_reason": gov.last_freeze_reason_thermal.value,
    }


def _build_fftrim_stationary(observer: Dict[str, Any]) -> Dict[str, Any]:
    """Return the stationary FF trim observer window."""
    alignment_delay_s = observer["current_alignment_delay_s"]
    return {
        "state": observer["state"],
        "window_duration_s": round(float(observer["window_duration_s"]), 3),
        "window_target_duration_s": round(
            float(observer["window_target_duration_s"]), 3
        ),
        "measurement_count": int(observer["measurement_count"]),
        "alignment_delay_s": (
            round(float(alignment_delay_s), 3)
            if alignment_delay_s is not None
            else None
        ),
        "last_reject_reason": observer["last_reject_reason"],
    }


def _build_fftrim_periodic(observer: Dict[str, Any]) -> Dict[str, Any]:
    """Return the periodic FF trim observer window."""
    return {
        "state": observer["state"],
        "window_duration_s": round(float(observer["window_duration_s"]), 3),
        "target_duration_s": round(float(observer["window_target_duration_s"]), 3),
        "measurement_count": int(observer["measurement_count"]),
        "amplitude_c": round(float(observer["amplitude_c"]), 6),
        "closure_error_c": round(float(observer["closure_error_c"]), 6),
        "last_reject_reason": observer["last_reject_reason"],
    }


def _build_fftrim_transfer(coord: FFTrimCoordinator) -> Dict[str, Any]:
    """Return the latest FF trim and bumpless integral transfer amounts."""
    return {
        "requested_trim_delta": round(coord.requested_trim_delta, 6),
        "stored_trim_delta": round(coord.stored_trim_delta, 6),
        "applied_trim_delta": round(coord.applied_trim_delta, 6),
        "transferable_i_power": round(coord.transferable_i_power, 6),
        "requested_i_transfer": round(coord.requested_i_transfer, 6),
        "applied_i_transfer": round(coord.applied_i_transfer, 6),
        "net_command_delta": round(coord.net_command_delta, 6),
        "state": coord.transfer_state,
        "reason": coord.transfer_reason,
        "pending_engagement": coord.transfer_pending_request_id is not None,
        "quality": coord.transfer_quality,
    }


def _build_fftrim(algo: SmartPI) -> Dict[str, Any]:
    """Return the FF trim observers, the last update and the command ownership."""
    coord = algo.fftrim_coord
    return {
        "stationary": _build_fftrim_stationary(algo._fftrim_observer.diagnostics),
        "periodic": _build_fftrim_periodic(
            algo._fftrim_periodic_observer.diagnostics
        ),
        "observation_mode": coord.observation_mode,
        "last_reject_reason": coord.last_reject_reason,
        "last_update_reason": coord.last_update_reason,
        "last_result": _build_fftrim_result(
            algo._fftrim_observer.last_admissible_result
        ),
        "last_transaction": _build_fftrim_transaction(algo._ff_trim.last_transaction),
        "windows_since_update": int(coord.windows_since_update),
        "transfer": _build_fftrim_transfer(coord),
        "command_ownership": _build_command_ownership(algo),
    }


def _build_feedforward(algo: SmartPI, twin_diag: Dict[str, Any]) -> Dict[str, Any]:
    """Return the FF3 and twin status, the deadband power path and the FF trim."""
    twin_status = twin_diag.get("status", "unavailable")
    return {
        "ff3_status": _build_ff3_status(algo),
        "ff3_twin_usable": (
            twin_status == "ok"
            and twin_diag.get("model_reliable") is True
            and twin_diag.get("warming_up") is not True
        ),
        "twin_status": twin_status,
        "deadband_power_source": algo.ctl.deadband_power_source,
        "deadband_p_mode": algo.ctl.deadband_p_mode,
        "fftrim": _build_fftrim(algo),
    }


def _build_setpoint(algo: SmartPI) -> Dict[str, Any]:
    """Return the effective setpoint and the trajectory and boost flags."""
    sp_mgr = algo.sp_mgr
    return {
        "filtered_setpoint": _round_optional(sp_mgr.effective_setpoint, 2),
        "trajectory_active": sp_mgr.trajectory_active,
        "trajectory_source": sp_mgr.trajectory_source,
        "boost_active": sp_mgr.boost_active,
    }


def _build_autocalib(algo: SmartPI) -> Dict[str, Any]:
    """Return the automatic calibration trigger state."""
    autocalib = algo.autocalib
    return {
        "state": autocalib.state.value,
        "model_degraded": autocalib.model_degraded,
        "last_trigger_ts": autocalib.last_trigger_ts_iso,
        "next_check_ts": autocalib.next_check_ts_iso,
        "snapshot_age_h": autocalib.snapshot_age_h,
    }


def _build_calibration(algo: SmartPI) -> Dict[str, Any]:
    """Return the calibration phase, its retry count and its last run time."""
    last_time = algo.calibration_mgr.last_calibration_time
    return {
        "state": algo.calibration_state,
        "retry_count": algo.autocalib.retry_count,
        "last_time": datetime.fromtimestamp(last_time).isoformat() if last_time else None,
    }


def build_published_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return the canonical SmartPI live diagnostics."""
    diag = _build_full_diagnostics(algo)
    tau_info = algo.est.tau_reliability()
    twin_diag = getattr(algo, "_last_twin_diag", None) or {}

    return {
        "control": _build_control(algo),
        "power": _build_power(algo),
        "temperature": _build_temperature(algo),
        "model": _build_model(algo, tau_info),
        "learning": _build_learning(algo, tau_info.reliable),
        "governance": _build_governance(algo),
        "feedforward": _build_feedforward(algo, twin_diag),
        "setpoint": _build_setpoint(algo),
        "autocalib": _build_autocalib(algo),
        "calibration": _build_calibration(algo),
        "analysis": _build_analysis_diagnostics(diag),
        "valve": _build_valve_diagnostics(algo),
    }


def _bootstrap_progress(algo: SmartPI) -> int | None:
    """
    Progress of the bootstrap (hysteresis) phase in percent (0-100).
    Returns None if not in Hysteresis phase.
    """
    if algo.phase != SmartPIPhase.HYSTERESIS:
        return None

    total_needed = AB_MIN_SAMPLES_A + AB_MIN_SAMPLES_B
    current_count = min(len(algo.est.a_meas_hist), AB_MIN_SAMPLES_A) + min(
        len(algo.est.b_meas_hist), AB_MIN_SAMPLES_B
    )

    pct = (current_count / total_needed) * 100.0
    return int(clamp(pct, 0, 100))


def _bootstrap_state(algo: SmartPI) -> str | None:
    """
    Detailed state message for the bootstrap process.
    Returns None if not in Hysteresis phase.

    Two steps:
      step1: waiting for deadtimes (A/B collection blocked per mode)
      step2: both deadtimes acquired, collecting initial emeas (<AB_MIN_SAMPLES_A/B)
    The HYSTERESIS phase ends once both sample targets are reached.
    """
    if algo.phase != SmartPIPhase.HYSTERESIS:
        return None

    nb_a = len(algo.est.a_meas_hist)
    nb_b = len(algo.est.b_meas_hist)

    dt_est = algo.dt_est
    dt_heat_ok = dt_est.deadtime_heat_reliable
    dt_cool_ok = dt_est.deadtime_cool_reliable

    # Step 1: at least one deadtime missing
    if not (dt_heat_ok and dt_cool_ok):
        heat_str = (
            f"{int(dt_est.deadtime_heat_s)}s"
            if dt_heat_ok and dt_est.deadtime_heat_s is not None
            else "null"
        )
        cool_str = (
            f"{int(dt_est.deadtime_cool_s)}s"
            if dt_cool_ok and dt_est.deadtime_cool_s is not None
            else "null"
        )
        parts = [f"step1 - deadtime: heat:{heat_str} cool:{cool_str}"]
        if dt_heat_ok and nb_a > 0:
            parts.append(f"[A:{nb_a}/{AB_MIN_SAMPLES_A}]")
        if dt_cool_ok and nb_b > 0:
            parts.append(f"[B:{nb_b}/{AB_MIN_SAMPLES_B}]")
        return " ".join(parts)

    # Step 2: both deadtimes acquired, collecting initial emeas
    return f"step2 - collecting emeas: A:{nb_a}/{AB_MIN_SAMPLES_A} B:{nb_b}/{AB_MIN_SAMPLES_B}"


def _build_full_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return the complete SmartPI diagnostics payload."""
    coord = algo.fftrim_coord
    ff_result = algo._last_ff_result
    twin_diag = getattr(algo, '_last_twin_diag', None) or {}
    twin_status = twin_diag.get("status", "unavailable")
    u_ff1 = ff_result.u_ff1 if ff_result else 0.0
    u_ff2 = ff_result.u_ff2 if ff_result else 0.0
    u_ff_final = ff_result.u_ff_final if ff_result else 0.0
    u_ff3 = ff_result.u_ff3 if ff_result else algo.u_ff3
    ff2_authority = FF_TRIM_RHO * max(u_ff1, FF_TRIM_EPSILON)

    diag = {
        # Learning counters
        "learn_ok_count": _session_counter(
            algo.est.learn_ok_count,
            getattr(algo, "_session_learn_ok_count_base", 0),
        ),
        "learn_skip_count": _session_counter(
            algo.est.learn_skip_count,
            getattr(algo, "_session_learn_skip_count_base", 0),
        ),
        # Learning metadata
        "learn_u_avg": round(algo.learn_u_int / max(algo.learn_t_int_s, 1.0), 3) if algo.learn_win_active else None,
        "learn_u_cv": round(algo.learn_win._u_cv, 3) if algo.learn_win_active else None,
        "learn_u_std": round(algo.learn_win._u_std, 4) if algo.learn_win_active else None,
        "integral_guard_mode": algo._last_integral_guard_mode,
        # Errors
        "error_p": round(algo.error_p, 4),
        "error_filtered": round(algo.error_filtered, 4) if algo.error_filtered != 0.0 or algo._e_filt is not None else None,
        "temperature_slope_h": (
            round(algo.temperature_slope_h, 6)
            if algo.temperature_slope_h is not None else None
        ),
        # Output
        "ff_reason": algo._last_ff_reason,
        "ff_warmup_ok_count": int(algo.ff_warmup_ok_count),
        "ff_warmup_cycles": int(algo.ff_warmup_cycles),
        # FFv3 signal chain
        "u_ff1": round(u_ff1, 6),
        "u_ff2": round(u_ff2, 6),
        "u_ff_final": round(u_ff_final, 6),
        "u_ff3": round(u_ff3, 6),
        "ff2_trim_delta": round(coord.last_ff_trim_delta, 6),
        "ff2_authority": round(ff2_authority, 6),
        "ff2_frozen": algo._ff_trim.frozen,
        "ff2_freeze_reason": algo._ff_trim.freeze_reason,
        # Effective feed-forward and FF3
        "u_ff_eff": round(ff_result.u_ff_eff, 6) if ff_result else 0.0,
        "ff3_selected_candidate": round(algo.ff3_state.selected_candidate, 6),
        "ff3_horizon_cycles": algo.ff3_state.horizon_cycles,
        "ff3_raw_reason_disabled": algo.ff3_state.raw_reason_disabled,
        "ff3_deadtime_cycles": algo.ff3_state.deadtime_cycles,
        "ff3_horizon_capped": algo.ff3_state.horizon_capped,
        "ff3_action_sensitivity": round(algo.ff3_state.action_sensitivity, 6),
        "ff3_prediction_quality": algo.ff3_state.prediction_quality,
        "ff3_authority_factor": round(algo.ff3_state.authority_factor, 6),
        "ff3_disturbance_active": algo.ff3_state.disturbance_active,
        "ff3_disturbance_reason": algo.ff3_state.disturbance_reason,
        "ff3_disturbance_kind": algo.ff3_state.disturbance_kind,
        "ff3_residual_persistent": algo.ff3_state.residual_persistent,
        "ff3_dynamic_coherent": algo.ff3_state.dynamic_coherent,
        "cycle_min": round(algo.cycle_min, 3),
        # Setpoint trajectory
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
        # Anti-windup tracking diagnostics
        "aw_du": round(algo.aw_du, 6),
        "forced_by_timing": algo.forced_by_timing,
        # Deadband state
        "in_core_deadband": algo._tau_reliable and abs(algo.error) < max(algo.deadband_c, 0.0),
        # Dead Time (Smart-PI v2)
        "kp_source": algo.gain_scheduler.kp_source,
        "deadtime_state": algo.dt_est.state,
        "deadtime_last_power": algo.dt_est.last_power,
        # Near-Band Auto (Phase 2) - delegated to DeadbandManager
        "near_band_below_deg": algo.deadband_mgr.near_band_below_deg,
        "near_band_above_deg": algo.deadband_mgr.near_band_above_deg,
        "near_band_source": algo.deadband_mgr.near_band_source,
        # Governance diagnostics
        "last_freeze_reason_gains": algo.gov.last_freeze_reason_gains.value,
        "last_decision_gains": algo.gov.last_decision_gains.value,
    }

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
