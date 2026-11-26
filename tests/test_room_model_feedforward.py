"""The room model is observation only: the holding power always comes from the learned b / a."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceDecision
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams

H = 3600.0
FULL = RoomParams("full", 0.0012, 1.0 / (2.5 * H), 1.0 / (80.0 * H), 0.65 / (2.5 * H), 600.0, 120.0)
SIMPLE = RoomParams.reduced(0.0012, 1.0 / (30.0 * H), 600.0, 120.0)
T_EXT = 4.0
T_SP = 20.0


def _algo() -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="ff", room_model=True)
    est = algo.est
    for _ in range(40):
        est.a_meas_hist.append(0.072)
        est.b_meas_hist.append(0.0012)
    est.a, est.b = 0.072, 0.0012
    est.learn_ok_count_a = est.learn_ok_count_b = 40
    est.learn_ok_count = 80
    algo.dt_est.deadtime_heat_s = 480.0
    algo.dt_est.deadtime_heat_reliable = True
    algo.dt_est.deadtime_cool_s = 900.0
    algo.dt_est.deadtime_cool_reliable = True
    algo._tau_reliable = True
    algo._cycles_since_reset = algo.ff_warmup_cycles
    return algo


def _publish(algo: SmartPI, params: RoomParams, *, healthy: bool = True, eb: float | None = None) -> None:
    """Publish params with an energy-balance ratio in force (a different one from the fit's own b_eq / a);
    healthy: tracking ok with the walls off equilibrium (a live wall extra power)."""
    room = algo.room_model
    room.params = params
    room.eb_slope = 1.04 * params.b_eq / params.a if eb is None else eb
    room.eb_gain = 0.02
    room.eb_day = 739600
    room.q = 0.01
    room.observer.configure(params, 0.01)
    if healthy:
        obs = room.observer
        obs.reinit(1e9, T_SP, T_EXT)
        obs.settle_until = None
        if params.full:
            obs.x[1] -= params.k_te


def _ff(algo: SmartPI, mode=VThermHvacMode_HEAT, ext: float | None = T_EXT) -> float:
    return algo._apply_gains_and_ff(GovernanceDecision.ADAPT_ON, target_temp_ff=T_SP, ext_current_temp=ext,
                                    hvac_mode=mode, error=0.0, e_p=0.0)


@pytest.mark.parametrize("params", [SIMPLE, FULL])
@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("valve", [False, True])
def test_the_holding_power_ignores_a_published_room_model(params, healthy, valve) -> None:
    plain = _algo()
    published = _algo()
    published._valve_mode_enabled = plain._valve_mode_enabled = valve
    _publish(published, params, healthy=healthy)
    if healthy and params.full:
        assert published.room_model.observer.wall_extra() > 0.01  # a live wall term exists and is not used
    expected = _ff(plain)
    assert expected > 0.0
    assert _ff(published) == expected
    assert published._last_ff_result.u_ff1 == plain._last_ff_result.u_ff1
    assert published._last_ff_result.warmup_scale == plain._last_ff_result.warmup_scale
    # Cooling and an unknown outdoor temperature as before
    assert _ff(published, VThermHvacMode_COOL) == _ff(plain, VThermHvacMode_COOL)
    assert _ff(published, ext=None) == 0.0


def test_the_room_model_still_publishes_while_the_holding_power_stays_learned() -> None:
    algo = _algo()
    _publish(algo, FULL)
    _ff(algo)
    published = algo.get_published_diagnostics()
    room = published["room_model"]
    assert room["used_by_regulation"] is False
    assert room["status"] == "identified"
    assert room["tracking"] == "ok"
    assert room["wall_extra_power_percent"] > 0.0
    assert room["forecast_c"] is not None
    assert room["energy_balance_loss_per_c"] == pytest.approx(1.04 * FULL.b_eq / FULL.a, abs=1e-6)
    assert published["feedforward"]["source"] == "learned model"
    assert "wall_power_percent" not in published["feedforward"]


def test_the_state_carries_no_holding_power_source() -> None:
    algo = _algo()
    _publish(algo, FULL)
    _ff(algo)
    state = json.loads(json.dumps(algo.save_state()))
    assert "ff_source" not in state and "ff_wall" not in state
    # A state saved by an earlier version, with a source, loads and is ignored
    state.update(ff_source={"kind": "walls", "gain": 0.03, "free_gain": 0.0, "wall": 0.07, "params": FULL.to_dict()},
                 ff_wall={"applied": 0.07, "from": 0.0, "start": None})
    other = _algo()
    other.load_state(state)
    assert _ff(other) == _ff(_algo())


def test_integral_absorbs_a_step_within_its_limits() -> None:
    ctl = SmartPIController("t")
    ki = 0.01
    ctl.integral = 5.0
    assert ctl.absorb_feedforward_step(ki=ki, delta_ff=0.2) == pytest.approx(0.2)
    assert ctl.integral == pytest.approx(5.0 - 20.0)  # crosses zero
    assert ctl.absorb_feedforward_step(ki=ki, delta_ff=-0.1) == pytest.approx(-0.1)
    assert ctl.integral == pytest.approx(-5.0)
    # Bounded by +-2 / Ki
    moved = ctl.absorb_feedforward_step(ki=ki, delta_ff=3.0)
    assert ctl.integral == pytest.approx(-200.0)
    assert moved == pytest.approx(1.95)
    assert ctl.absorb_feedforward_step(ki=ki, delta_ff=0.0) == 0.0


def test_closed_loop_publishing_the_true_model_leaves_the_command_unchanged() -> None:
    """Heavy room, the true model and a qualified energy balance published at 14:00 on the first day: the command,
    the holding power and the integral are the same, tick for tick, as without any publication."""
    p = dict(rsim.MASS)
    pub_t = rsim.T0 + 14 * H
    done = {"v": False}

    def publish(algo, t):
        if not done["v"] and t >= pub_t:
            done["eb"] = rsim.apply_energy_balance(algo, t)
            params = RoomParams("full", p["a"], p["alpha"], p["beta"], p["c"], p["tau_e"], p["L"])
            algo.room_model.params = params
            algo.room_model.q = 0.01
            algo.room_model.observer.configure(params, 0.01)
            done["v"] = True

    out = rsim.run_smartpi(p, 1.5, b_online_ratio=0.65, on_tick=publish, history_days=12.0)
    assert done["eb"]["reason"] is None
    assert out["algo"].room_model.params is not None
    ref = rsim.run_smartpi(p, 1.5, b_online_ratio=0.65, history_days=12.0)
    for key in ("u", "u_ff", "i_power", "ta"):
        assert out[key] == ref[key]
