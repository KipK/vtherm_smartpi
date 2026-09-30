"""Terminal release contracts using the production controller and manager."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import AB_HISTORY_SIZE
from tests.test_smartpi_reference_governor_wiring import _late_release_contract
from helpers import force_smartpi_stable_mode


def _prepared(mode=VThermHvacMode_HEAT):
    case = _late_release_contract(mode)
    rt = case.smartpi
    rt._last_u_cmd = rt._last_u_limited = 0.287468
    rt._output_initialized = True
    rt._last_calculate_time = case.now
    manager = rt.sp_mgr
    manager._trajectory.current_setpoint = case.target
    manager._reference_governor_authority.prepare(
        target_temp=case.target, nominal_reference=case.target,
        current_temp=case.current, ext_temp=case.external, hvac_mode=case.mode,
        a=rt.est.a, b=rt.est.b, committed_u=rt._committed_on_percent,
        remaining_cycle_min=0.985, stop_deadtime_s=494.2342618,
        measured_slope_h=-case.direction * 0.45, trajectory_active=True,
        trajectory_source="setpoint", model_reliable=True, deadtime_reliable=True,
        now_monotonic=case.now,
    )
    manager._reference_governor_prepared_at = case.now
    manager._reference_governor_authority_selected = True
    inputs = dict(
        error=case.direction * (case.target - case.current), error_p=0.05,
        kp=rt.Kp, ki=rt.Ki, u_ff=case.pi.u_ff,
        dt_min=1.0, cycle_min=rt._cycle_min, in_deadband=False,
        in_near_band=True, integrator_hold=False, u_db_nominal=case.pi.u_ff,
        hvac_mode=case.mode, current_temp=case.current, target_temp=case.target,
        is_tau_reliable=True, learn_ok_count_a=10, deadband_c=rt.deadband_c,
        core_deadband=False, deadband_allow_p=True,
    )
    release_inputs = dict(
        now=case.now, current_temp=case.current, hvac_mode=case.mode,
        error_i=inputs["error"], dt_min=1.0, integrator_hold=False,
        block_positive_integral=False, block_negative_integral=False,
        trajectory_shaping_active=False,
    )
    return case, inputs, release_inputs


def _protected_state(rt):
    return deepcopy((
        rt.ctl.save_state(), rt.est.save_state(), rt.integral_guard.save_state(),
        rt.guards.save_state(), rt._current_cycle_start_monotonic,
        rt._committed_on_percent, vars(rt._causal_power_trace),
        rt._last_u_cmd, rt._last_u_limited, rt._on_percent,
        rt.ctl.u_i, rt.ctl.u_ff, rt.ctl.last_i_mode, rt.ctl.last_sat,
        rt.ctl.sat_p, rt.ctl.sat_i,
    ))


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_terminal_release_replays_p_once_and_retires_only_governor_episode(mode):
    case, inputs, release_inputs = _prepared(mode)
    rt = case.smartpi
    rt.ctl.compute_pwm(**inputs)
    pi = rt.ctl.pi_output_snapshot
    protected = _protected_state(rt)
    p_error, p_mode, p_count, p_sign = pi.p_state.project(inputs["error"])

    command = rt._try_release_terminal_braking(**release_inputs)

    assert command == (pi.u_i + pi.u_ff) + pi.kp * p_error
    assert rt.integral == pytest.approx(52.887531 + inputs["error"])
    assert rt.ctl.pi_output_snapshot is pi
    assert _protected_state(rt) == protected
    assert rt.ctl.u_p == pi.kp * p_error
    assert rt.ctl.u_pi == rt.ctl.u_p + pi.u_i
    assert rt.ctl.u_cmd == command
    assert rt.ctl.last_error_p == inputs["error"]
    assert rt.ctl.last_error_p_db == p_error
    assert (rt.ctl.deadband_p_mode, rt.ctl._deadband_edge_count,
            rt.ctl._deadband_edge_sign) == (p_mode, p_count, p_sign)
    assert rt.error_p == inputs["error"]
    assert rt.sp_mgr.filtered_setpoint == case.target
    assert rt.sp_mgr.effective_setpoint == case.target
    assert not rt.sp_mgr.trajectory_active
    assert not rt.sp_mgr.reference_governor_authority_pending
    assert not rt.sp_mgr.reference_governor_authority_selected
    assert not rt.sp_mgr._reference_governor_authority_episode
    assert rt.sp_mgr._reference_governor_prepared_at is None


@pytest.mark.parametrize("mutation", [
    "hold", "guard_cut", "guard_kick", "stale_timestamp", "stale_prepared_at",
    "stale_target", "timing_on", "timing_off", "valve", "rate", "max_on",
    "raw_p_saturation", "positive_integral_block", "negative_integral_block",
])
def test_terminal_release_rejection_preserves_controller_and_episode(mutation):
    case, inputs, release_inputs = _prepared()
    rt = case.smartpi
    if mutation == "hold":
        inputs["integrator_hold"] = release_inputs["integrator_hold"] = True
    elif mutation == "guard_cut":
        rt.guards._guard_cut_active = True
    elif mutation == "guard_kick":
        rt.guards._guard_kick_active = True
    elif mutation == "stale_timestamp":
        rt._last_calculate_time += 1.0
    elif mutation == "stale_prepared_at":
        rt.sp_mgr._reference_governor_prepared_at += 1.0
    elif mutation == "stale_target":
        rt.sp_mgr._last_user_target_temp += 0.1
    elif mutation == "timing_on":
        rt._minimal_activation_delay = 1
    elif mutation == "timing_off":
        rt._minimal_deactivation_delay = 1
    elif mutation == "valve":
        rt._valve_mode_enabled = True
    elif mutation == "rate":
        release_inputs["dt_min"] = 0.001
    elif mutation == "max_on":
        rt._max_on_percent = 0.30
    elif mutation == "raw_p_saturation":
        inputs["u_ff"] = 0.99
        rt.integral = -700.0
    elif mutation == "positive_integral_block":
        release_inputs["block_positive_integral"] = True
    elif mutation == "negative_integral_block":
        release_inputs["block_negative_integral"] = True
    rt.ctl.compute_pwm(**inputs)
    pi = rt.ctl.pi_output_snapshot
    protected = _protected_state(rt)
    controller = deepcopy(vars(rt.ctl))
    manager = deepcopy(rt.sp_mgr.save_state())
    context = rt.sp_mgr._reference_governor_authority.pending_context

    assert rt._try_release_terminal_braking(**release_inputs) is None

    assert _protected_state(rt) == protected
    assert vars(rt.ctl) == controller
    assert rt.ctl.pi_output_snapshot is pi
    assert rt.sp_mgr.save_state() == manager
    assert rt.sp_mgr._reference_governor_authority.pending_context is context
    assert rt.sp_mgr.reference_governor_authority_selected
    assert rt.sp_mgr._reference_governor_authority_episode


def test_calculate_checks_release_after_pi_before_resolving_authority(monkeypatch):
    case, inputs, _ = _prepared()
    rt = case.smartpi
    rt.est.ensure_hvac_mode(case.mode)
    force_smartpi_stable_mode(rt)
    rt._last_hvac_mode = case.mode
    rt._last_target_temp = case.target
    rt._last_calculate_time = case.now - 60.0
    monkeypatch.setattr("custom_components.vtherm_smartpi.algo.time.monotonic",
                        lambda: case.now)
    monkeypatch.setattr(rt, "_manage_setpoint", lambda *args, **kwargs: (
        case.target, False, inputs["error"], inputs["error_p"], case.target,
    ))
    monkeypatch.setattr(rt, "_apply_gains_and_ff", lambda *args, **kwargs: (
        case.pi.u_ff, False,
    ))
    events = []
    compute = rt.ctl.compute_pwm
    release = rt._try_release_terminal_braking
    resolve = rt.sp_mgr.resolve_reference_governor

    def compute_once(*args, **kwargs):
        result = compute(*args, **kwargs)
        events.append(("compute", rt.ctl.pi_output_snapshot))
        return result

    def check_release(**kwargs):
        events.append(("release", rt.ctl.pi_output_snapshot))
        before = dict(vars(rt.ctl))
        assert kwargs["integrator_hold"] is True
        assert release(**kwargs) is None
        assert vars(rt.ctl) == before
        return None

    def resolve_after_release(**kwargs):
        events.append(("resolve", kwargs["pi_snapshot"]))
        return resolve(**kwargs)

    monkeypatch.setattr(rt.ctl, "compute_pwm", Mock(side_effect=compute_once))
    monkeypatch.setattr(rt, "_try_release_terminal_braking", check_release)
    monkeypatch.setattr(rt.sp_mgr, "resolve_reference_governor", resolve_after_release)

    rt.calculate(
        target_temp=case.target, current_temp=case.current,
        ext_current_temp=case.external, hvac_mode=case.mode,
        slope=-0.45, integrator_hold=True,
    )

    assert [event for event, _ in events] == ["compute", "release", "resolve"]
    assert all(snapshot is events[0][1] for _, snapshot in events)
    rt.ctl.compute_pwm.assert_called_once()


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_calculate_accepts_terminal_release_and_delivers_raw_command(monkeypatch, mode):
    case, inputs, _ = _prepared(mode)
    rt = case.smartpi
    model = (rt.est.a, rt.est.b)
    rt.est.ensure_hvac_mode(case.mode)
    force_smartpi_stable_mode(rt)
    # Keep the signed field model and its gains when supplying stable history.
    rt.est.a, rt.est.b = model
    rt.est.a_meas_hist.clear()
    rt.est.a_meas_hist.extend([model[0]] * AB_HISTORY_SIZE)
    rt.est.b_meas_hist.clear()
    rt.est.b_meas_hist.extend([model[1]] * AB_HISTORY_SIZE)
    rt.Kp, rt.Ki = inputs["kp"], inputs["ki"]
    rt.integral = 52.887531
    rt._learning_enabled = False
    rt._deadband_allow_p = True
    rt._last_hvac_mode = case.mode
    rt._last_target_temp = case.target
    rt._last_calculate_time = case.now - 60.0
    monkeypatch.setattr("custom_components.vtherm_smartpi.algo.time.monotonic",
                        lambda: case.now)
    monkeypatch.setattr(rt, "_manage_setpoint", lambda *args, **kwargs: (
        case.target, False, inputs["error"], inputs["error_p"], case.target,
    ))
    monkeypatch.setattr(rt, "_apply_gains_and_ff", lambda *args, **kwargs: (
        case.pi.u_ff, False,
    ))
    release = rt._try_release_terminal_braking
    accepted = []
    compute = Mock(wraps=rt.ctl.compute_pwm)
    resolve = Mock(wraps=rt.sp_mgr.resolve_reference_governor)

    def record_release(**kwargs):
        snapshot = rt.ctl.pi_output_snapshot
        integral = rt.integral
        guard = rt.integral_guard.save_state()
        result = release(**kwargs)
        assert result is not None
        assert rt.ctl.pi_output_snapshot is snapshot
        assert rt.integral == integral
        assert rt.integral_guard.save_state() == guard
        accepted.append((result, snapshot))
        return result

    monkeypatch.setattr(rt.ctl, "compute_pwm", compute)
    monkeypatch.setattr(rt, "_try_release_terminal_braking", record_release)
    monkeypatch.setattr(rt.sp_mgr, "resolve_reference_governor", resolve)

    rt.calculate(
        target_temp=case.target, current_temp=case.current,
        ext_current_temp=case.external, hvac_mode=case.mode,
        slope=-case.direction * 0.45,
    )

    compute.assert_called_once()
    resolve.assert_not_called()
    assert len(accepted) == 1
    command, snapshot = accepted[0]
    assert command == snapshot.u_i + snapshot.u_ff + snapshot.project_p(inputs["error"])
    assert rt.integral == pytest.approx(52.887531 + inputs["error"])
    assert (rt.est.a, rt.est.b) == model
    assert rt._last_u_cmd == command
    assert rt._last_u_limited == command
    assert rt.ctl.u_cmd_cap is None
    assert not rt.sp_mgr.reference_governor_authority_pending
    assert not rt.sp_mgr.reference_governor_authority_selected
    assert not rt.sp_mgr._reference_governor_authority_episode
