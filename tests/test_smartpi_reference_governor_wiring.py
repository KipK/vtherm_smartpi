"""Focused wiring tests for authoritative signed setpoint governance."""

import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.controller import PIOutputSnapshot
from custom_components.vtherm_smartpi.smartpi.causal_power_trace import AppliedPowerSegment
from custom_components.vtherm_smartpi.smartpi.const import TrajectoryPhase
from custom_components.vtherm_smartpi.smartpi.deadband_output import ProportionalState
from custom_components.vtherm_smartpi.smartpi.reference_governor_runtime import (
    ReferenceGovernorPhase,
)
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from helpers import force_smartpi_stable_mode


def snapshot() -> PIOutputSnapshot:
    return PIOutputSnapshot(1.0, 0.0, 0.0, ProportionalState(0.05, False, False, 0, None))


def filter_step(
    manager: SmartPISetpointManager,
    *,
    target: float,
    current: float,
    now: float,
    hvac_mode=VThermHvacMode_HEAT,
    a: float = 0.4,
    ext_temp: float = 20.0,
) -> float:
    """Run one model-ready setpoint calculation."""
    return manager.filter_setpoint(
        target_temp=target,
        current_temp=current,
        hvac_mode=hvac_mode,
        a=a,
        b=0.02,
        ext_current_temp=ext_temp,
        u_ref=1.0,
        deadtime_cool_s=240.0,
        deadtime_cool_reliable=True,
        tau_reliable=True,
        deadband_c=0.05,
        kp=1.0,
        next_cycle_u_ref=0.9,
        cycle_min=10.0,
        remaining_cycle_min=0.0,
        now_monotonic=now,
        temperature_slope_h=0.01,
    )


def start_setpoint_governance(manager: SmartPISetpointManager) -> float:
    """Arm a target increase and enter its late-braking trajectory."""
    filter_step(manager, target=21.0, current=21.0, now=0.0)
    filter_step(manager, target=22.0, current=21.0, now=60.0)
    return filter_step(manager, target=22.0, current=21.7, now=120.0)


def test_heat_setpoint_episode_prepares_authority() -> None:
    manager = SmartPISetpointManager("test")

    reference = start_setpoint_governance(manager)

    assert manager.trajectory_active is True
    assert manager.trajectory_source == "setpoint"
    assert manager.reference_governor_authority_selected is True
    assert manager.reference_governor_authority_pending is True
    assert manager.governor_active is False

    result = manager.resolve_reference_governor(
        requested_u=0.9,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=snapshot(),
    )

    assert result is not None
    assert result.phase is ReferenceGovernorPhase.GOVERNED
    assert result.command_cap is not None
    assert result.command_cap < 0.9
    assert result.kernel_decision.nominal_reference == pytest.approx(reference)


def test_next_cycle_uses_admissible_reference_without_rewriting_profile() -> None:
    manager = SmartPISetpointManager("test")
    start_setpoint_governance(manager)
    governed = manager.resolve_reference_governor(
        requested_u=0.9,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=snapshot(),
    )
    assert governed is not None

    reference = filter_step(
        manager,
        target=22.0,
        current=21.72,
        now=180.0,
    )
    nominal = manager.reference_governor_authority_nominal_reference

    assert nominal is not None
    assert nominal >= reference
    assert reference <= governed.effective_reference + 0.03


def test_cool_setpoint_episode_uses_the_same_authoritative_path() -> None:
    manager = SmartPISetpointManager("test")
    cool = {
        "hvac_mode": VThermHvacMode_COOL,
        "a": -0.4,
        "ext_temp": 30.0,
    }
    filter_step(manager, target=24.0, current=24.0, now=0.0, **cool)
    filter_step(manager, target=22.0, current=24.0, now=60.0, **cool)
    reference = filter_step(
        manager,
        target=22.0,
        current=22.3,
        now=120.0,
        **cool,
    )

    assert manager.trajectory_active is True
    assert manager.reference_governor_authority_selected is True
    assert manager.governor_active is False

    result = manager.resolve_reference_governor(
        requested_u=0.9,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=snapshot(),
    )

    assert result is not None
    assert result.phase is ReferenceGovernorPhase.GOVERNED
    assert result.command_cap is not None
    assert result.command_cap < 0.9
    assert result.kernel_decision.nominal_reference == pytest.approx(reference)
    assert result.effective_reference >= 22.0
    assert manager.governor_active is True
    assert manager.governor_phase == "governed"
    assert manager.governor_command_cap == result.command_cap


def test_missing_controller_snapshot_bypasses_governor() -> None:
    manager = SmartPISetpointManager("test")
    filter_step(manager, target=21.0, current=21.0, now=0.0)
    filter_step(manager, target=22.0, current=21.0, now=60.0)

    filter_step(manager, target=22.0, current=21.7, now=120.0)

    assert manager.reference_governor_authority_selected is True
    assert manager.reference_governor_authority_pending is True
    assert manager.governor_active is False

    result = manager.resolve_reference_governor(
        requested_u=0.9,
        now_monotonic=120.0,
        cycle_min=10.0,
        pi_snapshot=None,
    )
    assert result is not None
    assert result.command_cap is None
    assert result.kernel_decision.reason == "authority_missing_pi_snapshot"


@pytest.mark.parametrize(
    ("mode", "target", "external", "a"),
    [
        (VThermHvacMode_HEAT, 20.5, 10.0, 0.4),
        (VThermHvacMode_COOL, 19.5, 30.0, -0.4),
    ],
)
def test_smartpi_calculate_resolves_with_effective_post_compute_terms(
    monkeypatch, mode, target, external, a
) -> None:
    smartpi = SmartPI(
        hass=MagicMock(), cycle_min=10, minimal_activation_delay=0,
        minimal_deactivation_delay=0, name="governor wiring", deadband_c=0.1,
    )
    smartpi.est.ensure_hvac_mode(mode)
    force_smartpi_stable_mode(smartpi)
    # The generic fixture seeds HEAT gains; retain mode-consistent history.
    history_size = len(smartpi.est.a_meas_hist)
    smartpi.est.a_meas_hist.clear()
    smartpi.est.a_meas_hist.extend([a] * history_size)
    smartpi.est.a = a
    smartpi.est.b = 0.02
    smartpi._last_hvac_mode = mode
    smartpi._last_target_temp = target
    smartpi._last_calculate_time = time.monotonic() - 60.0
    smartpi.Kp = 0.5
    smartpi.Ki = 0.01
    smartpi.integral = 0.0

    direction = 1.0 if mode == VThermHvacMode_HEAT else -1.0

    def manage_setpoint(*args, **kwargs):
        smartpi.sp_mgr._reference_governor_authority.prepare(
            target_temp=target,
            nominal_reference=20.0 + direction * 0.4,
            current_temp=20.0, hvac_mode=mode,
            trajectory_active=True, trajectory_source="setpoint",
            a=a, b=0.02, ext_temp=external, committed_u=0.0,
            remaining_cycle_min=0.0, stop_deadtime_s=300.0,
            measured_slope_h=0.0, model_reliable=True,
            deadtime_reliable=True, now_monotonic=time.monotonic(),
        )
        smartpi.sp_mgr._reference_governor_authority_selected = True
        return target, False, 0.5, 0.5, target

    def update_gains_and_ff(*args, **kwargs):
        smartpi.Kp = 1.25
        smartpi.Ki = 0.05
        smartpi.integral = 2.0
        return 0.2, False

    seen = []
    original_resolve = smartpi.sp_mgr.resolve_reference_governor

    def resolve(**kwargs):
        seen.append(kwargs["pi_snapshot"])
        return original_resolve(**kwargs)

    monkeypatch.setattr(smartpi, "_manage_setpoint", manage_setpoint)
    monkeypatch.setattr(smartpi, "_apply_gains_and_ff", update_gains_and_ff)
    monkeypatch.setattr(smartpi.sp_mgr, "resolve_reference_governor", resolve)

    smartpi.calculate(
        target_temp=target, current_temp=20.0, ext_current_temp=external,
        hvac_mode=mode, slope=0.0, integrator_hold=True,
    )

    assert len(seen) == 1
    assert seen[0] is smartpi.ctl.pi_output_snapshot
    assert seen[0].kp == pytest.approx(1.25)
    assert seen[0].u_i == pytest.approx(0.1)
    assert seen[0].u_ff == pytest.approx(0.2)
    assert smartpi.sp_mgr.reference_governor_authority_decision is not None


def _late_release_contract(mode):
    """Build a lifecycle contract from rounded field observations, not a replay.

    The private P persistence state and original episode start are unavailable.
    The reported deadzone_edge mode and public P decomposition select an
    immediate non-frozen P projection with allow_p=True (0.025 C threshold).
    The four-minute cycle is exported. No plant, calculate,
    integration, scheduler advancement, or thermal energy measurement runs.
    """
    direction = 1.0 if mode == VThermHvacMode_HEAT else -1.0
    now = 20000.0
    target = 19.5
    current = target - direction * 0.07
    external = target - direction * 14.5
    smartpi = SmartPI(
        hass=MagicMock(), cycle_min=4, minimal_activation_delay=0,
        minimal_deactivation_delay=0, name="late release contract", deadband_c=0.05,
    )
    smartpi.est.a = direction * 0.180385
    smartpi.est.b = 0.003060
    smartpi.Kp = 0.280137
    smartpi.Ki = 0.001
    smartpi.integral = 52.887531
    smartpi._committed_on_percent = 0.2875
    smartpi._on_percent = 0.287468
    smartpi._current_cycle_start_monotonic = now - (4.0 - 0.985) * 60.0
    smartpi.dt_est.deadtime_cool_s = 494.2342618
    smartpi.dt_est.deadtime_cool_reliable = True
    pi = PIOutputSnapshot(
        smartpi.Kp, smartpi.Ki * smartpi.integral, 0.245985,
        ProportionalState(smartpi.deadband_c, False, True, 0, None),
    )
    smartpi.ctl._pi_output_snapshot = pi
    smartpi.ctl.u_i = pi.u_i
    smartpi.ctl.u_ff = pi.u_ff
    smartpi.integral_guard.clear("released_stabilized")
    smartpi._causal_power_trace.record_applied_power(
        AppliedPowerSegment(now - 60.0, now, 0.2875, "fixture_committed_interval")
    )
    manager = smartpi.sp_mgr
    manager._last_user_target_temp = target
    manager.filtered_setpoint = target
    manager.effective_setpoint = target
    manager._trajectory.start(target, target, 7.392, now)
    manager._trajectory.phase = TrajectoryPhase.RELEASE
    manager._trajectory.elapsed_s = 12672.0
    manager._trajectory_source = "setpoint"
    manager._reference_governor_authority_episode = True
    return SimpleNamespace(
        smartpi=smartpi, mode=mode, direction=direction, target=target,
        current=current, external=external, now=now, pi=pi,
    )


@pytest.fixture(params=[VThermHvacMode_HEAT, VThermHvacMode_COOL], ids=["heat", "cool"])
def late_release_contract(request):
    """Provide signed mirror contracts without asserting thermal robustness."""
    return _late_release_contract(request.param)


def _late_release_observation(case, now):
    """Apply the same fixed observation and project P without advancing PI."""
    smartpi = case.smartpi
    reference = smartpi.sp_mgr.filter_setpoint(
        target_temp=case.target, current_temp=case.current, hvac_mode=case.mode,
        a=smartpi.est.a, b=smartpi.est.b, ext_current_temp=case.external,
        u_ref=smartpi._committed_on_percent,
        deadtime_cool_s=smartpi.dt_est.deadtime_cool_s,
        deadtime_cool_reliable=True, tau_reliable=True,
        deadband_c=smartpi.deadband_c, kp=smartpi.Kp,
        next_cycle_u_ref=smartpi._on_percent, cycle_min=smartpi._cycle_min,
        remaining_cycle_min=0.985, now_monotonic=now,
        allow_disturbance_trigger=False, temperature_slope_h=-case.direction * 0.45,
    )
    projected_request = case.pi.u_ff + case.pi.u_i + case.pi.project_p(
        case.direction * (reference - case.current)
    )
    decision = smartpi.sp_mgr.resolve_reference_governor(
        requested_u=projected_request, now_monotonic=now,
        cycle_min=smartpi._cycle_min, pi_snapshot=case.pi,
    )
    return reference, projected_request, decision


def _non_episode_state(case):
    """Capture concrete adjacent state that a manager-only retirement must keep."""
    smartpi = case.smartpi
    return deepcopy({
        "controller": smartpi.ctl.__dict__,
        "guard": smartpi.integral_guard.__dict__,
        "model": smartpi.est.save_state(),
        "gains": (smartpi.Kp, smartpi.Ki),
        "deadtime": (smartpi.dt_est.deadtime_cool_s, smartpi.dt_est.deadtime_cool_reliable),
        "cycle": (smartpi._cycle_min, smartpi._current_cycle_start_monotonic,
                  smartpi._committed_on_percent, smartpi._on_percent),
        "trace": smartpi._causal_power_trace.read_window(case.now - 60.0, case.now),
    })


def test_converged_late_release_still_has_a_cap_below_its_i_ff_floor(late_release_contract):
    case = late_release_contract
    manager = case.smartpi.sp_mgr
    reference, projected_request, decision = _late_release_observation(case, case.now)

    assert reference == case.target
    assert manager.trajectory_phase is TrajectoryPhase.RELEASE
    assert manager.trajectory_elapsed_s == 12672.0
    assert manager.reference_governor_authority_nominal_reference is None
    assert decision is not None
    assert decision.kernel_decision.nominal_reference == case.target
    assert decision.phase is ReferenceGovernorPhase.GOVERNED
    assert decision.reason == "cap"
    assert decision.handoff_ready is False
    assert decision.command_cap is not None
    assert case.pi.u_i + case.pi.u_ff == pytest.approx(0.298872531)
    assert projected_request == pytest.approx(0.311478696, abs=1e-9)
    assert case.pi.u_i + case.pi.u_ff > decision.command_cap
    assert projected_request > decision.command_cap
    assert decision.kernel_decision.admissible_reference == pytest.approx(case.current)


def test_retiring_only_late_release_episode_removes_authority_and_keeps_adjacent_state(
    late_release_contract,
):
    case = late_release_contract
    smartpi = case.smartpi
    manager = smartpi.sp_mgr
    _late_release_observation(case, case.now)
    reference_with_episode, projected_with_episode, governed = _late_release_observation(
        case, case.now + 10.0)
    assert governed is not None and governed.command_cap is not None
    assert manager.trajectory_phase is TrajectoryPhase.RELEASE
    assert manager._trajectory.current_setpoint == case.target
    assert case.direction * (reference_with_episode - case.current) < 0.07 - 1e-9
    assert projected_with_episode == pytest.approx(0.305875956, abs=1e-9)
    before = _non_episode_state(case)
    snapshot_identity = smartpi.ctl.pi_output_snapshot
    trace_identity = smartpi._causal_power_trace

    # This is the sole intervention. In particular, no OFF/resume or composite
    # SmartPI reset runs. The next observation is exactly the one above.
    manager.set_passthrough(case.target)
    raw_reference, projected_without_episode, decision = _late_release_observation(
        case, case.now + 10.0)

    assert raw_reference == case.target
    assert decision is None
    assert not manager.trajectory_active
    assert not manager.reference_governor_authority_pending
    assert not manager.reference_governor_authority_selected
    assert not manager._reference_governor_authority_episode
    assert not manager._reference_governor_authority.runtime.armed
    assert manager.governor_command_cap is None
    assert manager.governor_phase == "idle"
    assert projected_without_episode > projected_with_episode
    assert projected_without_episode == pytest.approx(0.311478696, abs=1e-9)
    assert projected_without_episode > governed.command_cap
    assert smartpi.ctl.pi_output_snapshot is snapshot_identity
    assert smartpi._causal_power_trace is trace_identity
    assert _non_episode_state(case) == before
    # The comparison concerns pure snapshot projections, not actual delivered
    # power, future temperature, or safe automatic retirement of this episode.


def test_resetting_only_authority_runtime_rearms_the_retained_manager_episode(
    late_release_contract,
):
    case = late_release_contract
    manager = case.smartpi.sp_mgr
    _late_release_observation(case, case.now)
    retained = (manager._trajectory, manager._trajectory.current_setpoint,
                manager.trajectory_phase, manager.trajectory_source)
    manager._reference_governor_authority.runtime.reset()
    assert not manager._reference_governor_authority.runtime.armed
    assert manager._reference_governor_authority_episode

    _, _, decision = _late_release_observation(case, case.now + 10.0)

    assert retained == (manager._trajectory, manager._trajectory.current_setpoint,
                        manager.trajectory_phase, manager.trajectory_source)
    assert manager._reference_governor_authority.runtime.armed
    assert decision is not None
    assert decision.phase is ReferenceGovernorPhase.GOVERNED
    assert decision.command_cap is not None


def test_late_release_signed_mirror_preserves_kernel_cap_and_snapshot_projection():
    heat = _late_release_contract(VThermHvacMode_HEAT)
    cool = _late_release_contract(VThermHvacMode_COOL)
    _, heat_projection, heat_decision = _late_release_observation(heat, heat.now)
    _, cool_projection, cool_decision = _late_release_observation(cool, cool.now)

    assert heat_decision is not None and cool_decision is not None
    assert heat_decision.phase is cool_decision.phase is ReferenceGovernorPhase.GOVERNED
    assert heat_decision.command_cap == pytest.approx(cool_decision.command_cap, abs=1e-12)
    assert heat_projection == pytest.approx(cool_projection, abs=1e-12)
    assert heat_decision.effective_reference + cool_decision.effective_reference == pytest.approx(
        2.0 * heat.target, abs=1e-12)
