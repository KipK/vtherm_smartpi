"""
Smart-PI Diagnostics Module.

This module builds the published live diagnostics tree directly from the
SmartPI state, with one builder per published section. As a "friend" module
tightly coupled to SmartPI internals, some protected member access is expected
and intentional.
"""
# pylint: disable=protected-access
from __future__ import annotations

import time
from typing import Any, Dict, TYPE_CHECKING
from .const import (
    ABConfidenceState,
    SmartPIPhase,
    AB_HISTORY_SIZE,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_B,
    clamp,
)

from .guards import GUARD_CUT_I_MODE, GUARD_CUT_STATE
from .learning_start import LEARNING_PULSES_CONFIDENCE, LEARNING_PULSES_PHASE, LEARNING_PULSES_STAGE, learning_by_pulses

if TYPE_CHECKING:
    from ..algo import SmartPI
    from .ab_estimator import TauReliability


def _round_optional(value: float | None, digits: int) -> float | None:
    """Return the rounded value, None when the value is None."""
    return round(value, digits) if value is not None else None


def _round_float(value: Any, digits: int = 6) -> float | None:
    """Return the value as a rounded float, None when the value is None."""
    return round(float(value), digits) if value is not None else None


def _percent(ratio: float) -> float:
    """Return a public percent value from an internal ratio rounded to 6 digits."""
    return round(float(round(ratio, 6)) * 100.0, 1)


def _session_counter(total: int, baseline: int) -> int:
    """Return a runtime counter relative to the current process baseline."""
    return max(0, int(total) - int(baseline))


def _integral_time_min(kp: float, ki: float) -> float | None:
    """Return the effective integral time Kp / Ki in minutes, None when Ki <= 0."""
    if ki is None or kp is None or ki <= 0.0:
        return None
    return round(kp / ki, 1)


def _cut_error(algo: SmartPI) -> float | None:
    """Setpoint error of the last calculation when it was in a guard cut, None otherwise. The PI is not computed during
    a cut: the error, the anticipation and the regulation mode are published from the cut instead of the values of
    the last regulation calculation."""
    if algo.guards.guard_cut_active and algo.ctl.last_i_mode == GUARD_CUT_I_MODE:
        return algo.guards.cut_error
    return None


def _errors(algo: SmartPI) -> tuple[float, float, float]:
    """Published error, error on the anticipated temperature and anticipation (0 during a guard cut)."""
    cut = _cut_error(algo)
    if cut is not None:
        return cut, cut, 0.0
    return algo.error, algo.error_p, algo.anticipation_c


def _build_control(algo: SmartPI) -> Dict[str, Any]:
    """Return the regulation phase, gains and band flags."""
    phase = algo.phase
    kp = round(algo.Kp, 6)
    ki = round(algo.Ki, 6)
    return {
        "phase": LEARNING_PULSES_PHASE if learning_by_pulses(algo) else phase,
        "mode": (
            "hysteresis" if algo.command_is_hysteresis
            else GUARD_CUT_STATE if _cut_error(algo) is not None
            else "smartpi"
        ),
        "hysteresis_state": algo.ctl.hysteresis_state,
        "kp": kp,
        "ki": ki,
        "ti_min": _integral_time_min(kp, ki),
        "restart_reason": algo._last_restart_reason,
        "last_restart": algo.restart.public(),
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
    indoor = algo._last_current_temp
    error, _, anticipation = _errors(algo)
    return {
        "indoor": indoor,
        "anticipated": None if indoor is None else round(indoor + anticipation, 2),
        "outdoor": algo._last_ext_temp,
        "error": round(error, 4),
        "integral_error": round(algo.integral, 6),
        "integral_mode": algo.last_i_mode,
        "integral_hold_mode": algo.ctl.integral_hold_mode,
        "integral_hold_source": algo.ctl.last_integral_hold_source,
    }


def _build_ab_confidence_state(algo: SmartPI) -> str:
    """Return the public A/B confidence state for the current phase."""
    if algo.phase == SmartPIPhase.HYSTERESIS:
        return LEARNING_PULSES_CONFIDENCE if learning_by_pulses(algo) else ABConfidenceState.AB_BOOTSTRAP.value

    return algo._ab_confidence.state.value


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
        **algo._start.model.public_model(est.model_hvac_mode),
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
      step1: waiting for the response model of the mode (no a/b window before it)
      step2: collecting initial emeas (<AB_MIN_SAMPLES_A/B)
    The HYSTERESIS phase ends once both sample targets are reached and the
    delivered power model is settled.
    """
    if algo.phase != SmartPIPhase.HYSTERESIS:
        return None

    nb_a = len(algo.est.a_meas_hist)
    nb_b = len(algo.est.b_meas_hist)
    if algo.learn_win.model(algo.est.model_hvac_mode) is None:
        return "step1 - waiting response model"
    return f"step2 - collecting emeas: A:{nb_a}/{AB_MIN_SAMPLES_A} B:{nb_b}/{AB_MIN_SAMPLES_B}"


def _build_ab_learning_stage(
    algo: SmartPI, samples_a: int, samples_b: int, tau_reliable: bool
) -> str:
    """Return a compact AB learning stage for the published summary."""
    if algo.phase == SmartPIPhase.HYSTERESIS:
        return LEARNING_PULSES_STAGE if learning_by_pulses(algo) else "bootstrap"

    if samples_a < AB_HISTORY_SIZE or samples_b < AB_HISTORY_SIZE:
        return "learning"

    if tau_reliable is True:
        return "monitoring"

    return "degraded"


def _build_learning(algo: SmartPI, tau_reliable: bool) -> Dict[str, Any]:
    """Return the A/B learning progress and the drift states."""
    est = algo.est
    samples_a = int(est.learn_ok_count_a)
    samples_b = int(est.learn_ok_count_b)
    return {
        "enabled": algo.learning_enabled,
        "block_reason": algo.learning_block.reason,
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
        "start": algo._start.public(),
    }


def _build_governance(algo: SmartPI) -> Dict[str, Any]:
    """Return the governance regime and the thermal update decision."""
    gov = algo.gov
    return {
        "regime": gov._current_regime.value,
        "thermal_update_decision": gov.last_decision_thermal.value,
        "thermal_update_reason": gov.last_freeze_reason_thermal.value,
    }


def _build_feedforward(algo: SmartPI) -> Dict[str, Any]:
    """Return the holding power source and the deadband power path."""
    holding = algo._ff_holding
    wall = holding.wall_shown
    return {
        "source": holding.source.plain,
        "wall_power_percent": round(100.0 * wall, 1) if wall is not None else None,
        "deadband_power_source": algo.ctl.deadband_power_source,
        "deadband_p_mode": algo.ctl.deadband_p_mode,
    }


def _build_setpoint(algo: SmartPI) -> Dict[str, Any]:
    """Return the boost flag of a setpoint change."""
    return {
        "boost_active": algo.sp_mgr.boost_active,
    }


def _build_calibration(algo: SmartPI) -> Dict[str, Any]:
    """Return the calibration in progress or the last one: state, pulses, result, last request, dead times, reading,
    values in force and corrections."""
    return algo._start.calibration_public()


def _build_analysis_control(algo: SmartPI) -> Dict[str, Any]:
    """Return the error terms, the anti-windup step and the band geometry."""
    deadband_mgr = algo.deadband_mgr
    horizon = algo.anticipation_horizon()
    _, error_p, anticipation = _errors(algo)
    return {
        "cycle_min": round(algo.cycle_min, 3),
        "error_filtered": (
            round(algo.error_filtered, 4)
            if algo.error_filtered != 0.0 or algo._e_filt is not None
            else None
        ),
        "error_p": round(error_p, 4),
        "anticipation_c": round(anticipation, 4),
        "anticipation_horizon_s": round(horizon.seconds, 1),
        "anticipation_horizon_source": horizon.source,
        "aw_du": round(algo.aw_du, 6),
        "forced_by_timing": algo.forced_by_timing,
        "in_core_deadband": (
            algo._tau_reliable and abs(algo.error) < max(algo.deadband_c, 0.0)
        ),
        "near_band_below_deg": deadband_mgr.near_band_below_deg,
        "near_band_above_deg": deadband_mgr.near_band_above_deg,
        "near_band_source": deadband_mgr.near_band_source,
        "temperature_slope_h": _round_optional(algo.temperature_slope_h, 6),
        "effective_dead_time_s": algo._start.model.public_effective_dead_time_s(algo.est.model_hvac_mode),
    }


def _build_analysis_learning(algo: SmartPI) -> Dict[str, Any]:
    """Return the session learning counters, the FF warm-up and the window power."""
    est = algo.est
    learn_win = algo.learn_win
    window_active = algo.learn_win_active
    return {
        "learn_ok_count": _session_counter(
            est.learn_ok_count, algo._session_learn_ok_count_base
        ),
        "learn_skip_count": _session_counter(
            est.learn_skip_count, algo._session_learn_skip_count_base
        ),
        "ff_warmup_ok_count": int(algo.ff_warmup_ok_count),
        "ff_warmup_cycles": int(algo.ff_warmup_cycles),
        "window_mean_power": (
            round(algo.learn_u_int / max(algo.learn_t_int_s, 1.0), 3)
            if window_active
            else None
        ),
        "window_power_cv": round(learn_win._u_cv, 3) if window_active else None,
        "window_power_std": round(learn_win._u_std, 4) if window_active else None,
        "window_outcomes_24h": learn_win.outcomes_summary(time.monotonic()),
        "balance_pool": {
            "segments_a": learn_win.balance.pool_size("a"),
            "segments_b": learn_win.balance.pool_size("b"),
            "resolution_c": round(learn_win.balance.resolution_c, 3),
        },
        "response_model": algo._start.model.public_learning(algo.est.model_hvac_mode),
    }


def _build_analysis_feedforward(algo: SmartPI) -> Dict[str, Any]:
    """Return the FF1 holding power."""
    ff_result = algo._last_ff_result
    u_ff1 = round(ff_result.u_ff1, 6) if ff_result else 0.0
    return {
        "reason": algo._last_ff_reason,
        "u_ff1": u_ff1,
        "u_ff_final": u_ff1,
        "u_ff_effective": u_ff1,
    }


def _build_analysis(algo: SmartPI) -> Dict[str, Any]:
    """Return advanced diagnostics that remain useful to live consumers."""
    gov = algo.gov
    analysis = {
        "control": _build_analysis_control(algo),
        "learning": _build_analysis_learning(algo),
        "deadtime": {
            "state": algo.dt_est.state,
            "last_power": algo.dt_est.last_power,
            "waiting_elapsed_s": _round_optional(algo.dt_est.waiting_elapsed_s, 0),
            "heat_last_outcome": algo.dt_est.last_outcome["heat"],
            "cool_last_outcome": algo.dt_est.last_outcome["cool"],
        },
        "governance": {
            "last_freeze_reason_gains": gov.last_freeze_reason_gains.value,
            "last_decision_gains": gov.last_decision_gains.value,
            "kp_source": algo.gain_scheduler.kp_source,
        },
        "feedforward": _build_analysis_feedforward(algo),
    }
    return analysis


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


def _build_tar_learning(algo: SmartPI) -> Dict[str, Any]:
    """Return the T_ar learning state (fast and refined values, applied value)."""
    dt_est = algo.dt_est
    return algo.tar_learning.diagnostics(dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable)


def _build_room_model(algo: SmartPI) -> Dict[str, Any]:
    """Return the room model state (its holding power is shown under feedforward)."""
    return algo.room_model.diagnostics()


def build_published_diagnostics(algo: SmartPI) -> Dict[str, Any]:
    """Return the canonical SmartPI live diagnostics."""
    tau_info = algo.est.tau_reliability()
    tree = {
        "control": _build_control(algo),
        "power": _build_power(algo),
        "temperature": _build_temperature(algo),
        "model": _build_model(algo, tau_info),
        "learning": _build_learning(algo, tau_info.reliable),
        "governance": _build_governance(algo),
        "feedforward": _build_feedforward(algo),
        "setpoint": _build_setpoint(algo),
        "calibration": _build_calibration(algo),
        "analysis": _build_analysis(algo),
        "valve": _build_valve_diagnostics(algo),
    }
    if getattr(algo, "tar_learning", None) is not None:
        tree["tar_learning"] = _build_tar_learning(algo)
    if getattr(algo, "room_model", None) is not None:
        tree["room_model"] = _build_room_model(algo)
    return tree
