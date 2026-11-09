"""Feed-forward holding power from the published room model."""

from __future__ import annotations

import logging
import math
from unittest.mock import MagicMock

import numpy as np
import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff
from custom_components.vtherm_smartpi.smartpi.room_model.feedforward import (
    LEARNED,
    SOURCE_LEARNED,
    SOURCE_SIMPLE,
    SOURCE_STATIC,
    SOURCE_WALLS,
    HoldingSource,
    HoldingTransfer,
    select_source,
    static_gain,
)
from custom_components.vtherm_smartpi.smartpi.room_model.const import RM_WALL_FADE_S
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams
from custom_components.vtherm_smartpi.smartpi.room_model.observer import HEALTH_OK, HEALTH_SETTLING
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from custom_components.vtherm_smartpi.smartpi.thermal_model import propagate_1r1c

H = 3600.0
FULL = RoomParams("full", 0.0012, 1.0 / (2.5 * H), 1.0 / (80.0 * H), 0.65 / (2.5 * H), 600.0, 120.0)
SIMPLE = RoomParams.reduced(0.0012, 1.0 / (30.0 * H), 600.0, 120.0)
T_EXT = 4.0
T_SP = 20.0
T0 = 1.0e9
HEAT = VThermHvacMode_HEAT


def _eb(params: RoomParams) -> float:
    """Energy-balance ratio in force for a published model: 4 % above the fitted b_eq / a, so that a holding
    power built on the fit would show."""
    return 1.04 * params.b_eq / params.a


def _algo() -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="ff", tar_learning=True, room_model=True)
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


def _publish(algo: SmartPI, params: RoomParams, *, healthy: bool = True, wall_offset_c: float = -1.0,
             eb: float | None | str = "default") -> None:
    """Publish params with an energy-balance ratio in force (default _eb(params)); healthy: tracking ok with the
    wall wall_offset_c (C of wall temperature) off equilibrium."""
    room = algo.room_model
    room.params = params
    room.eb_ratio = _eb(params) if eb == "default" else eb
    room.eb_day = 739600 if room.eb_ratio is not None else None
    room.q = 0.01
    room.observer.configure(params, 0.01)
    if healthy:
        obs = room.observer
        obs.reinit(1e9, T_SP, T_EXT)
        obs.settle_until = None
        if params.full:
            obs.x[1] += params.k_te * wall_offset_c


def _learned(algo: SmartPI):
    k_ff = algo.est.b / algo.est.a
    return compute_ff(k_ff=k_ff, target_temp_ff=T_SP, ext_temp=T_EXT, warmup_scale=0.5, trim=algo._ff_trim)


# ---------------------------------------------------------------------------
# Source selection and holding power
# ---------------------------------------------------------------------------
def test_source_without_published_model_is_the_learned_one() -> None:
    algo = _algo()
    assert select_source(None, VThermHvacMode_HEAT, False) is LEARNED
    assert select_source(algo.room_model, VThermHvacMode_HEAT, False) is LEARNED
    assert static_gain(algo.room_model, VThermHvacMode_HEAT, False) is None


def test_simple_model_gives_the_static_part() -> None:
    algo = _algo()
    _publish(algo, SIMPLE)
    src = select_source(algo.room_model, VThermHvacMode_HEAT, False)
    assert src.kind == SOURCE_SIMPLE
    assert src.gain == _eb(SIMPLE)
    assert src.wall is None
    assert src.power(T_SP, T_EXT) == pytest.approx(_eb(SIMPLE) * (T_SP - T_EXT))


def test_walls_with_healthy_tracking_add_the_wall_extra_power() -> None:
    algo = _algo()
    _publish(algo, FULL)
    obs = algo.room_model.observer
    assert obs.health() == HEALTH_OK
    src = select_source(algo.room_model, VThermHvacMode_HEAT, False)
    assert src.kind == SOURCE_WALLS
    assert src.wall == pytest.approx(obs.wall_extra())
    assert src.wall > 0.0  # cold walls: extra power
    static = _eb(FULL) * (T_SP - T_EXT)
    assert src.power(T_SP, T_EXT) == pytest.approx(static + src.wall)
    # Both signs: warm walls give heat back
    _publish(algo, FULL, wall_offset_c=0.5)
    src = select_source(algo.room_model, VThermHvacMode_HEAT, False)
    assert src.wall < 0.0
    assert src.power(T_SP, T_EXT) == pytest.approx(static + src.wall)


def test_walls_with_tracking_not_ok_give_the_static_part() -> None:
    algo = _algo()
    _publish(algo, FULL, healthy=False)
    assert algo.room_model.observer.health() != HEALTH_OK
    src = select_source(algo.room_model, VThermHvacMode_HEAT, False)
    assert (src.kind, src.wall) == (SOURCE_STATIC, None)
    _publish(algo, FULL)
    algo.room_model.observer.settle_until = 2e9
    assert algo.room_model.observer.health() == HEALTH_SETTLING
    assert select_source(algo.room_model, VThermHvacMode_HEAT, False).kind == SOURCE_STATIC


def test_cooling_and_valve_keep_the_learned_source() -> None:
    algo = _algo()
    _publish(algo, FULL)
    assert select_source(algo.room_model, VThermHvacMode_COOL, False) is LEARNED
    assert select_source(algo.room_model, VThermHvacMode_HEAT, True) is LEARNED
    assert static_gain(algo.room_model, VThermHvacMode_COOL, False) is None
    assert static_gain(algo.room_model, VThermHvacMode_HEAT, True) is None


def test_holding_power_is_clamped() -> None:
    src = HoldingSource(SOURCE_WALLS, 0.05, 0.4, FULL)
    assert src.power(20.0, 0.0) == 1.0
    assert HoldingSource(SOURCE_WALLS, 0.05, -0.9, FULL).power(20.0, 10.0) == 0.0
    assert HoldingSource(SOURCE_STATIC, 0.05, None, FULL).power(20.0, 25.0) == 0.0
    assert HoldingSource(SOURCE_STATIC, 0.05, None, FULL).power(20.0, None) == 0.0
    assert HoldingSource(SOURCE_STATIC, 0.05, None, FULL).power(20.0, 10.0) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Discrete changes and the transfer
# ---------------------------------------------------------------------------
def _upd(tr, src, now, learned=0.5, sp=T_SP, ext=T_EXT, heat=True):
    return tr.update(src, now=now, learned_power=learned, target_temp=sp, ext_temp=ext, heat=heat)


def test_only_static_changes_are_transferred_and_the_wall_part_blends() -> None:
    tr = HoldingTransfer()
    walls = HoldingSource(SOURCE_WALLS, 0.03, 0.10, FULL)
    # First source: nothing to compare with; the learned holding power's own evolution is never transferred
    assert _upd(tr, LEARNED, 0.0, learned=0.3) == 0.0
    assert _upd(tr, LEARNED, 60.0, learned=0.5) == 0.0
    assert tr.wall_shown is None
    # Change of source: only the static part steps, the wall part starts from 0
    t0 = 120.0
    assert _upd(tr, walls, t0) == pytest.approx(0.03 * (T_SP - T_EXT) - 0.5)
    assert tr.wall_applied == 0.0 and tr.fading
    # Linear blend to the live wall term over RM_WALL_FADE_S, nothing transferred
    assert _upd(tr, walls, t0 + 0.5 * RM_WALL_FADE_S) == 0.0
    assert tr.wall_applied == pytest.approx(0.05)
    assert _upd(tr, walls, t0 + RM_WALL_FADE_S) == 0.0
    assert tr.wall_applied == pytest.approx(0.10) and not tr.fading
    # Continuous evolution of the wall term, of the setpoint and of the outdoor temperature: followed, no transfer
    walls2 = HoldingSource(SOURCE_WALLS, 0.03, 0.07, FULL)
    t1 = t0 + 2 * RM_WALL_FADE_S
    assert _upd(tr, walls2, t1, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(0.07)
    # The tracking drops the wall term: linear fade-out from the value applied last, no transfer
    static = HoldingSource(SOURCE_STATIC, 0.03, None, FULL)
    assert _upd(tr, static, t1 + 60.0, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(0.07)
    assert tr.wall_shown == tr.wall_applied
    assert _upd(tr, static, t1 + 60.0 + 0.5 * RM_WALL_FADE_S, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(0.035)
    # ... and back on during the fade-out: blend from the value applied last to the live term
    back_from = tr.wall_applied
    t2 = t1 + 60.0 + 0.5 * RM_WALL_FADE_S + 60.0
    assert _upd(tr, walls2, t2, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(back_from)
    assert _upd(tr, walls2, t2 + 60.0, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(back_from + (0.07 - back_from) * 60.0 / RM_WALL_FADE_S)
    # New daily publication: the static step only, at the wall part applied at this cycle; the wall part blends
    new_params = RoomParams("full", 0.0013, FULL.alpha, FULL.beta, FULL.c, 600.0, 120.0)
    walls3 = HoldingSource(SOURCE_WALLS, 0.028, 0.065, new_params)
    w_before = tr.wall_applied
    t3 = t2 + 180.0
    d = _upd(tr, walls3, t3, sp=21.0, ext=2.0)
    assert d == pytest.approx((0.028 - 0.03) * 19.0)
    w_pub = tr.wall_applied
    assert w_pub == pytest.approx(w_before)  # restarted from the value applied last, at elapsed 0
    assert _upd(tr, walls3, t3 + RM_WALL_FADE_S, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == pytest.approx(0.065)
    # Same parameters published again: nothing, no new blend
    walls4 = HoldingSource(SOURCE_WALLS, 0.028, 0.065, RoomParams.from_dict(new_params.to_dict()))
    assert _upd(tr, walls4, t3 + RM_WALL_FADE_S + 60.0, sp=21.0, ext=2.0) == 0.0
    assert not tr.fading
    # Back to the learned source in heating: the static step is transferred, the wall part fades out on top
    t4 = t3 + RM_WALL_FADE_S + 120.0
    d = _upd(tr, LEARNED, t4, learned=0.4, sp=21.0, ext=2.0)
    assert d == pytest.approx(0.4 - 0.028 * 19.0)
    assert tr.wall_applied == pytest.approx(0.065) and tr.fading
    assert _upd(tr, LEARNED, t4 + RM_WALL_FADE_S, learned=0.4, sp=21.0, ext=2.0) == 0.0
    assert tr.wall_applied == 0.0 and tr.wall_shown is None
    # A reset (PI memory purged) forgets the source and the wall part
    tr.reset()
    assert _upd(tr, walls, t4, learned=0.4) == 0.0
    assert tr.wall_applied == 0.0 and tr.fading


def test_wall_part_is_dropped_on_the_learned_source_outside_heating() -> None:
    tr = HoldingTransfer()
    walls = HoldingSource(SOURCE_WALLS, 0.03, 0.10, FULL)
    _upd(tr, walls, 0.0)
    _upd(tr, walls, RM_WALL_FADE_S)
    assert tr.wall_applied == pytest.approx(0.10)
    _upd(tr, LEARNED, RM_WALL_FADE_S + 60.0, heat=False)
    assert tr.wall_applied == 0.0 and not tr.fading


def test_wall_blend_restarts_when_the_clock_goes_back() -> None:
    tr = HoldingTransfer()
    walls = HoldingSource(SOURCE_WALLS, 0.03, 0.10, FULL)
    _upd(tr, walls, 1000.0)
    _upd(tr, walls, 1000.0 + 0.5 * RM_WALL_FADE_S)
    w = tr.wall_applied
    _upd(tr, walls, 0.0)
    assert tr.wall_applied == pytest.approx(w)
    _upd(tr, walls, RM_WALL_FADE_S)
    assert tr.wall_applied == pytest.approx(0.10)


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


def test_switch_to_the_room_model_is_bumpless_and_hands_the_trim_over() -> None:
    algo = _algo()
    algo._ff_trim.u_ff_trim = 0.05
    algo.ctl.integral = 10.0
    learned = _learned(algo)
    assert algo._select_holding_power(learned, T_SP, T_EXT, VThermHvacMode_HEAT) is learned
    before = learned.u_ff_final + algo.Ki * algo.ctl.integral
    _publish(algo, FULL)
    result = algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    # Full weight, no trim
    src = algo._ff_holding.source
    assert src.kind == SOURCE_WALLS
    # The wall part starts from 0 and blends in
    assert (result.u_ff1, result.u_ff2, result.warmup_scale) == (src.power(T_SP, T_EXT, 0.0), 0.0, 1.0)
    assert abs(result.u_ff_final - learned.u_ff_final) > 0.1
    # FF + I unchanged: the step, trim included, went into the integral
    assert result.u_ff_final + algo.Ki * algo.ctl.integral == pytest.approx(before, abs=1e-12)
    assert algo._ff_trim.u_ff_trim == 0.0
    algo.fftrim_coord.update_trim_freeze(GovernanceRegime.DEAD_BAND)
    assert (algo._ff_trim.frozen, algo._ff_trim.freeze_reason) == (True, "room_model")
    # The trim stays at zero and frozen while the room model is in force; it is released after
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    algo.room_model.reset()
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    algo.fftrim_coord.update_trim_freeze(GovernanceRegime.DEAD_BAND)
    assert algo._ff_trim.freeze_reason != "room_model"


def test_transfer_is_bounded_by_the_integral_limits() -> None:
    algo = _algo()
    i_max = 2.0 / algo.Ki
    algo.ctl.integral = -i_max + 1.0
    learned = _learned(algo)
    algo._select_holding_power(learned, T_SP, T_EXT, VThermHvacMode_HEAT)
    _publish(algo, FULL)
    result = algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    assert (result.u_ff_final - learned.u_ff_final) / algo.Ki > 1.0
    assert algo.ctl.integral == pytest.approx(-i_max)  # the step is cut at the integral limit
    algo.ctl.integral = i_max - 1.0
    algo.room_model.reset()
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    assert algo.ctl.integral == pytest.approx(i_max)


# ---------------------------------------------------------------------------
# Wall part blend
# ---------------------------------------------------------------------------
STATIC_POWER = _eb(FULL) * (T_SP - T_EXT)


def _set_tracking(algo: SmartPI, ok: bool) -> None:
    """Tracking ok (walls) or settling (static part only)."""
    algo.room_model.observer.settle_until = None if ok else 2e9


def _on_static(integral: float = 10.0) -> SmartPI:
    """A published full model on its static part (tracking settling), the static step already transferred."""
    algo = _algo()
    _publish(algo, FULL)
    _set_tracking(algo, False)
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, HEAT, now=T0)
    assert algo._ff_holding.source.kind == SOURCE_STATIC
    algo.ctl.integral = integral
    return algo


def _select(algo: SmartPI, now: float):
    return algo._select_holding_power(_learned(algo), T_SP, T_EXT, HEAT, now=now)


def test_static_to_walls_raises_the_command_by_the_wall_term_without_touching_the_integral() -> None:
    algo = _on_static()
    _set_tracking(algo, True)
    wall = algo.room_model.observer.wall_extra()
    assert wall > 0.02
    t1 = T0 + 600.0
    seq = []
    for k in range(41):
        seq.append(_select(algo, t1 + 60.0 * k).u_ff_final)
        assert algo._ff_holding.source.kind == SOURCE_WALLS
        assert algo.ctl.integral == 10.0
        if k == 15:
            ff = algo.get_published_diagnostics()["feedforward"]
            assert ff["wall_power_percent"] == round(100.0 * 0.5 * wall, 1)
    assert seq[0] == pytest.approx(STATIC_POWER)
    assert seq[15] == pytest.approx(STATIC_POWER + 0.5 * wall)
    assert seq[30] == pytest.approx(STATIC_POWER + wall)
    assert seq[40] == pytest.approx(STATIC_POWER + wall)
    steps = [b - a for a, b in zip(seq, seq[1:31])]
    assert all(st == pytest.approx(wall / 30.0) for st in steps)


def test_walls_to_static_fades_the_wall_term_out_without_transfer() -> None:
    algo = _on_static()
    _set_tracking(algo, True)
    _select(algo, T0 + 600.0)
    _select(algo, T0 + 600.0 + RM_WALL_FADE_S)
    wall = algo._ff_holding.wall_applied
    assert wall == pytest.approx(algo.room_model.observer.wall_extra())
    t2 = T0 + 600.0 + 2 * RM_WALL_FADE_S
    _set_tracking(algo, False)
    obs = algo.room_model.observer
    seq = []
    for k in range(31):
        if k == 10:
            # A fresh wall state of the unhealthy tracking is not used
            obs.x[1] -= FULL.k_te * 2.0
        seq.append(_select(algo, t2 + 60.0 * k).u_ff_final)
        assert algo._ff_holding.source.kind == SOURCE_STATIC
        assert algo.ctl.integral == 10.0
    for k, u in enumerate(seq):
        assert u == pytest.approx(STATIC_POWER + wall * (1.0 - k / 30.0))
    assert algo.get_published_diagnostics()["feedforward"]["wall_power_percent"] is None


def test_rapid_toggling_accumulates_nothing_in_the_integral() -> None:
    algo = _on_static()
    wall = None
    for k in range(240):
        _set_tracking(algo, k % 3 != 0)
        r = _select(algo, T0 + 60.0 * (k + 1))
        wall = algo.room_model.observer.wall_extra()
        assert algo.ctl.integral == 10.0
        assert STATIC_POWER - 1e-12 <= r.u_ff_final <= STATIC_POWER + wall + 1e-12
    # Back to steady walls: the full wall term within the fade time
    _set_tracking(algo, True)
    r = _select(algo, T0 + 60.0 * 241 + RM_WALL_FADE_S)
    assert r.u_ff_final == pytest.approx(STATIC_POWER + wall)
    assert algo.ctl.integral == 10.0


def test_publication_keeps_the_static_part_and_blends_the_wall_part() -> None:
    """A new publication changes the fitted b_eq / a and the walls: the static part stays the energy balance
    (nothing moved into the integral), the wall part blends to the new wall term."""
    algo = _on_static()
    _set_tracking(algo, True)
    _select(algo, T0 + 600.0)
    _select(algo, T0 + 600.0 + RM_WALL_FADE_S)
    old_wall = algo._ff_holding.wall_applied
    new = RoomParams("full", FULL.a * 1.1, FULL.alpha, FULL.beta * 0.8, FULL.c * 0.97, FULL.tau_e, FULL.L)
    assert abs(new.b_eq / new.a - FULL.b_eq / FULL.a) > 0.001
    algo.room_model.params = new
    algo.room_model.observer.configure(new, 0.01)
    assert algo.room_model.observer.health() == HEALTH_OK
    new_wall = algo.room_model.observer.wall_extra()
    assert abs(new_wall - old_wall) > 0.005
    t3 = T0 + 600.0 + 2 * RM_WALL_FADE_S
    r = _select(algo, t3)
    assert algo._ff_holding.source.gain == _eb(FULL)
    assert algo.ctl.integral == 10.0
    assert r.u_ff_final == pytest.approx(STATIC_POWER + old_wall)
    r = _select(algo, t3 + 0.5 * RM_WALL_FADE_S)
    assert r.u_ff_final == pytest.approx(STATIC_POWER + 0.5 * (old_wall + new_wall))
    r = _select(algo, t3 + RM_WALL_FADE_S)
    assert r.u_ff_final == pytest.approx(STATIC_POWER + new_wall)
    assert algo.ctl.integral == 10.0


def test_new_energy_balance_ratio_moves_the_static_step_only() -> None:
    """A new qualified energy-balance ratio is a discrete change of the static part: its step goes into the
    integral, the wall part goes on following the tracking without a new blend."""
    algo = _on_static()
    _set_tracking(algo, True)
    _select(algo, T0 + 600.0)
    _select(algo, T0 + 600.0 + RM_WALL_FADE_S)
    wall = algo._ff_holding.wall_applied
    assert not algo._ff_holding.fading
    before = _select(algo, T0 + 660.0 + RM_WALL_FADE_S).u_ff_final + algo.Ki * algo.ctl.integral
    new_ratio = 0.95 * _eb(FULL)
    algo.room_model.eb_ratio = new_ratio
    t = T0 + 720.0 + RM_WALL_FADE_S
    r = _select(algo, t)
    step = (new_ratio - _eb(FULL)) * (T_SP - T_EXT)
    assert algo._ff_holding.last_delta == pytest.approx(step)
    assert algo.ctl.integral == pytest.approx(10.0 - step / algo.Ki)
    assert r.u_ff_final == pytest.approx(new_ratio * (T_SP - T_EXT) + wall)
    assert r.u_ff_final + algo.Ki * algo.ctl.integral == pytest.approx(before, abs=1e-12)
    assert not algo._ff_holding.fading
    # Same ratio again: nothing more
    integral = algo.ctl.integral
    _select(algo, t + 60.0)
    assert algo.ctl.integral == integral and algo._ff_holding.last_delta == 0.0


def test_static_part_is_the_energy_balance_never_the_fit() -> None:
    algo = _algo()
    for params in (FULL, SIMPLE):
        for ratio in (0.9 * params.b_eq / params.a, 1.2 * params.b_eq / params.a):
            _publish(algo, params, eb=ratio)
            src = select_source(algo.room_model, HEAT, False)
            assert src.gain == ratio
            assert static_gain(algo.room_model, HEAT, False) == ratio
            wall = src.wall if src.wall is not None else 0.0
            assert src.power(T_SP, T_EXT) == pytest.approx(ratio * (T_SP - T_EXT) + wall)
    # Clamped like the learned gain
    _publish(algo, SIMPLE, eb=5.0)
    assert static_gain(algo.room_model, HEAT, False) == 3.0
    assert select_source(algo.room_model, HEAT, False).power(T_SP, T_EXT) == 1.0


def test_published_model_without_energy_balance_keeps_the_learned_holding_power() -> None:
    """Published walls but no qualified energy-balance ratio yet: learned holding power, no wall term, no
    transfer, no trim hand-over."""
    algo = _algo()
    algo._ff_trim.u_ff_trim = 0.03
    algo.ctl.integral = 10.0
    _publish(algo, FULL, eb=None)
    assert algo.room_model.observer.health() == HEALTH_OK
    assert algo.room_model.observer.wall_extra() > 0.02
    assert select_source(algo.room_model, HEAT, False) is LEARNED
    assert static_gain(algo.room_model, HEAT, False) is None
    for k in range(3):
        learned = _learned(algo)
        assert _select_learned(algo, learned, T0 + 60.0 * k) is learned
    assert algo.ctl.integral == 10.0
    assert algo._ff_trim.u_ff_trim == 0.03
    ff = algo.get_published_diagnostics()["feedforward"]
    assert ff["source"] == "learned model" and ff["wall_power_percent"] is None
    # The ratio qualifies: the room model takes over through the transfer
    algo.room_model.eb_ratio = _eb(FULL)
    _select(algo, T0 + 600.0)
    assert algo._ff_holding.source.kind == SOURCE_WALLS


def _select_learned(algo: SmartPI, learned, now: float):
    return algo._select_holding_power(learned, T_SP, T_EXT, HEAT, now=now)


def test_braking_equilibrium_is_the_energy_balance(monkeypatch) -> None:
    """The braking prediction of the setpoint trajectory gets the energy-balance ratio in force."""
    algo = _algo()
    seen = []
    real = algo.sp_mgr.filter_setpoint

    def spy(**kw):
        seen.append(kw.get("equilibrium_gain"))
        return real(**kw)

    monkeypatch.setattr(algo.sp_mgr, "filter_setpoint", spy)
    from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase

    def run():
        seen.clear()
        algo.calculate(target_temp=T_SP, current_temp=19.5, ext_current_temp=T_EXT, slope=None, hvac_mode=HEAT,
                       cycle_boundary=True)
        return seen[-1] if seen else "not called"

    assert algo.phase == SmartPIPhase.STABLE
    run()
    assert run() is None
    _publish(algo, FULL, eb=0.9 * FULL.b_eq / FULL.a)
    assert run() == 0.9 * FULL.b_eq / FULL.a
    _publish(algo, FULL, eb=None)
    assert run() is None


def test_restart_during_a_fade_goes_on_with_the_applied_wall_part() -> None:
    algo = _on_static()
    _set_tracking(algo, True)
    wall = algo.room_model.observer.wall_extra()
    t1 = T0 + 600.0
    _select(algo, t1)
    mid = t1 + 0.4 * RM_WALL_FADE_S
    r_mid = _select(algo, mid)
    assert r_mid.u_ff_final == pytest.approx(STATIC_POWER + 0.4 * wall)
    state, room_state = algo.save_state(), algo.room_model.save_state()
    assert state["ff_wall"]["start"] == t1

    restored = _algo()
    restored.load_state(state)
    restored.room_model.load_state(room_state)
    assert restored._ff_holding.wall_applied == pytest.approx(0.4 * wall)
    assert restored.get_published_diagnostics()["feedforward"]["wall_power_percent"] == round(40.0 * wall, 1)
    # The same instant gives the same holding power, then the blend goes on on the wall clock
    assert _select(restored, mid).u_ff_final == pytest.approx(r_mid.u_ff_final)
    assert _select(restored, t1 + 0.7 * RM_WALL_FADE_S).u_ff_final == pytest.approx(STATIC_POWER + 0.7 * wall)
    assert _select(restored, t1 + RM_WALL_FADE_S).u_ff_final == pytest.approx(STATIC_POWER + wall)
    assert restored.ctl.integral == 10.0
    # Restarting on the static part (tracking not ok): the fade-out starts from the restored applied value
    again = _algo()
    again.load_state(state)
    again.room_model.load_state(room_state)
    _set_tracking(again, False)
    assert _select(again, mid + 60.0).u_ff_final == pytest.approx(STATIC_POWER + 0.4 * wall)
    assert _select(again, mid + 60.0 + 0.5 * RM_WALL_FADE_S).u_ff_final == pytest.approx(STATIC_POWER + 0.2 * wall)
    assert again.ctl.integral == 10.0


def test_state_without_the_wall_blend_applies_the_saved_wall_term() -> None:
    algo = _on_static()
    _set_tracking(algo, True)
    _select(algo, T0 + 600.0)
    _select(algo, T0 + 600.0 + RM_WALL_FADE_S)
    state, room_state = algo.save_state(), algo.room_model.save_state()
    state.pop("ff_wall")
    restored = _algo()
    restored.load_state(state)
    restored.room_model.load_state(room_state)
    assert restored._ff_holding.wall_applied == pytest.approx(state["ff_source"]["wall"])
    assert not restored._ff_holding.fading
    for bad in ({"applied": "x", "from": 0.0, "start": None}, {"applied": 5.0, "from": 0.0, "start": None},
                {"applied": 0.01, "from": 0.0, "start": "y"}, ["no"]):
        restored._ff_holding.load(state["ff_source"], bad)
        assert restored._ff_holding.wall_applied == pytest.approx(state["ff_source"]["wall"])
        assert not restored._ff_holding.fading


# ---------------------------------------------------------------------------
# Braking prediction of the setpoint trajectory
# ---------------------------------------------------------------------------
def test_braking_prediction_uses_the_room_model_equilibrium() -> None:
    mgr = SmartPISetpointManager(name="t", enabled=True)
    a, b, gain = 0.4, 0.02, 0.03
    kw = dict(current_temp=19.0, ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT, a=a, b=b, u_ref=0.6,
              horizon_min=12.0)
    base = mgr._compute_predicted_signed_change(**kw)
    expected = propagate_1r1c(temperature=19.0, external_temperature=5.0, a=b / gain, b=b, power=0.6,
                              duration_min=12.0) - 19.0
    assert mgr._compute_predicted_signed_change(**kw, equilibrium_gain=gain) == pytest.approx(expected)
    # The learned b / a as equilibrium gain gives the learned prediction
    assert mgr._compute_predicted_signed_change(**kw, equilibrium_gain=b / a) == pytest.approx(base)
    # Equilibrium only: the learned rate b keeps the dynamics (same relaxation factor)
    decay = -math.expm1(-b * 12.0)
    assert expected == pytest.approx((5.0 - 19.0 + 0.6 / gain) * decay)
    # Cooling ignores it
    cool = dict(kw, hvac_mode=VThermHvacMode_COOL, a=-a, current_temp=27.0, ext_current_temp=30.0)
    assert mgr._compute_predicted_signed_change(**cool, equilibrium_gain=gain) == \
        mgr._compute_predicted_signed_change(**cool)


def test_low_learned_ratio_no_longer_arms_braking_with_the_room_model() -> None:
    """A learned b / a 35 % low overstates the committed rise and arms the braking; b_eq / a does not."""
    room_gain = 1.0 / 15.5  # equilibrium 0.5 C above the setpoint at full power
    a, b = 0.4, 0.65 * room_gain * 0.4

    def armed(gain):
        mgr = SmartPISetpointManager(name="t", enabled=True)
        mgr.filter_setpoint(target_temp=20.0, current_temp=19.5, hvac_mode=VThermHvacMode_HEAT, a=a, b=b,
                            ext_current_temp=5.0, u_ref=1.0, stop_deadtime_s=600.0, stop_deadtime_reliable=True,
                            tau_reliable=True, deadband_c=0.05, kp=1.0, cycle_min=10.0, remaining_cycle_min=5.0,
                            now_monotonic=0.0, equilibrium_gain=gain)
        return mgr.trajectory_braking_needed

    assert armed(None) is True
    assert armed(room_gain) is False


# ---------------------------------------------------------------------------
# Diagnostics and persistence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source", "plain", "percent"),
    [
        (LEARNED, "learned model", None),
        (HoldingSource(SOURCE_WALLS, 0.03, 0.0734, FULL), "room model (air and walls)", 7.3),
        (HoldingSource(SOURCE_STATIC, 0.03, None, FULL), "room model (static part)", None),
        (HoldingSource(SOURCE_SIMPLE, 0.03, None, SIMPLE), "room model (simple)", None),
    ],
)
def test_diagnostics_show_the_source_in_plain_words(source, plain, percent) -> None:
    algo = _algo()
    algo._ff_holding.ref = source
    algo._ff_holding.wall_applied = source.wall or 0.0
    ff = algo.get_published_diagnostics()["feedforward"]
    assert ff["source"] == plain
    assert ff["wall_power_percent"] == percent


def test_source_persists_and_restarts_without_transfer() -> None:
    algo = _algo()
    _publish(algo, FULL)
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT, now=T0)
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT, now=T0 + RM_WALL_FADE_S)
    algo.ctl.integral = 12.0
    ref = algo._ff_holding.ref
    state = algo.save_state()
    room_state = algo.room_model.save_state()

    restored = _algo()
    restored.load_state(state)
    restored.room_model.load_state(room_state)
    assert restored._ff_holding.ref == ref
    assert restored.ctl.integral == 12.0
    # Room model source at full weight whatever the warmup cycle count, restored integral unchanged
    restored._cycles_since_reset = 0
    restored._tau_reliable = True
    k_ff = restored.est.b / restored.est.a
    learned = compute_ff(k_ff=k_ff, target_temp_ff=T_SP, ext_temp=T_EXT, warmup_scale=0.0, trim=restored._ff_trim)
    result = restored._select_holding_power(learned, T_SP, T_EXT, VThermHvacMode_HEAT, now=T0 + 2 * RM_WALL_FADE_S)
    assert result.u_ff_final == pytest.approx(ref.power(T_SP, T_EXT))
    assert restored.ctl.integral == 12.0
    assert restored._ff_holding.last_delta == 0.0


def test_restart_without_the_room_model_transfers_back_to_the_learned_source() -> None:
    algo = _algo()
    _publish(algo, SIMPLE)
    algo._select_holding_power(_learned(algo), T_SP, T_EXT, VThermHvacMode_HEAT)
    algo.ctl.integral = 12.0
    room_ff = algo._ff_holding.ref.power(T_SP, T_EXT)
    restored = _algo()
    restored.load_state(algo.save_state())  # room model store lost
    learned = _learned(restored)
    restored._select_holding_power(learned, T_SP, T_EXT, VThermHvacMode_HEAT)
    assert restored._ff_holding.source is LEARNED
    assert learned.u_ff_final + restored.Ki * restored.ctl.integral == pytest.approx(room_ff + algo.Ki * 12.0)


def test_state_without_source_is_the_learned_source() -> None:
    algo = _algo()
    state = algo.save_state()
    state.pop("ff_source")
    algo._ff_trim.u_ff_trim = 0.02
    state["ff_v2_trim"] = {"u_ff_trim": 0.02}
    state["ctl_state"] = {"integral": 3.0}
    restored = _algo()
    restored.load_state(state)
    assert restored._ff_holding.ref is LEARNED
    _publish(restored, SIMPLE)
    learned = _learned(restored)
    before = learned.u_ff_final + restored.Ki * restored.ctl.integral
    result = restored._select_holding_power(learned, T_SP, T_EXT, VThermHvacMode_HEAT)
    assert result.u_ff_final + restored.Ki * restored.ctl.integral == pytest.approx(before)
    assert restored._ff_trim.u_ff_trim == 0.0


def test_invalid_saved_source_is_ignored() -> None:
    assert HoldingSource.from_dict({"kind": "walls", "gain": 0.03, "wall": None, "params": FULL.to_dict()}) is None
    assert HoldingSource.from_dict({"kind": "other"}) is None
    assert HoldingSource.from_dict({"kind": "static", "gain": "x", "params": FULL.to_dict()}) is None
    src = HoldingSource(SOURCE_STATIC, 0.03, None, FULL)
    assert HoldingSource.from_dict(src.to_dict()) == src
    assert HoldingSource.from_dict(LEARNED.to_dict()) is LEARNED


# ---------------------------------------------------------------------------
# Closed loop: heavy room with published walls
# ---------------------------------------------------------------------------
def test_closed_loop_heavy_room_switch_is_bumpless_and_mornings_reach_the_setpoint(caplog) -> None:
    """Heavy room (walls 65 % of the air loss, wall time constant 80 h), precise sensor, learned b / a 35 % low.

    Eight days of recorded history before the run let the energy balance of the 3 days before the first day
    qualify. The true model is published at 14:00 on the first day: static part (the energy balance) at once
    (tracking settling), walls 3 h later. Bounds:
    - each switch moves FF + I by less than 0.01 while the holding power itself steps by more than 0.03
      (what moves is the integral of one tick);
    - with the walls in force, every morning rise (17 -> 20 C at 06:00) reaches 19.9 C within 3 h, against
      none within 8 h with the learned holding power (it stays more than 0.2 C short all day);
    - overshoot below 0.2 C (four times the deadband; the wall term of both signs costs about 0.1 C here), and
      the room stays within 0.2 C of the setpoint from 4 h after the step to 22:00 (the learned run stays
      0.4 C short).
    """
    caplog.set_level(logging.WARNING)
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

    days = 4
    out = rsim.run_smartpi(p, days, b_online_ratio=0.65, on_tick=publish, history_days=8.0)
    # The energy balance qualifies within 2 % of the true b_eq / a and gives the static part
    true_gain = (p["alpha"] - p["c"]) / p["a"]
    assert done["eb"]["reason"] is None
    assert done["eb"]["value"] == pytest.approx(true_gain, rel=0.02)
    assert out["algo"]._ff_holding.source.gain == done["eb"]["value"]
    ref = rsim.run_smartpi(p, days, b_online_ratio=0.65)
    t = np.array(out["t"])
    src = out["source"]
    switches = [i for i in range(1, len(src)) if src[i] != src[i - 1]]
    assert [(src[i - 1], src[i]) for i in switches] == [(SOURCE_LEARNED, SOURCE_STATIC), (SOURCE_STATIC, SOURCE_WALLS)]
    for i in switches:
        step = out["u_ff"][i] - out["u_ff"][i - 1]
        total = (out["u_ff"][i] + out["i_power"][i]) - (out["u_ff"][i - 1] + out["i_power"][i - 1])
        assert abs(total) < 0.01
        assert abs(out["u"][i] - out["u"][i - 1]) < 0.01
    # Static step moved into the integral; the wall term blends in over RM_WALL_FADE_S, the integral keeps
    # its own evolution (one tick of PI action)
    i_static, i_walls = switches
    assert abs(out["u_ff"][i_static] - out["u_ff"][i_static - 1]) > 0.03
    assert abs(out["u_ff"][i_walls] - out["u_ff"][i_walls - 1]) < 0.005
    assert abs(out["i_power"][i_walls] - out["i_power"][i_walls - 1]) < 0.005
    j = i_walls + int(RM_WALL_FADE_S // 60)
    assert out["u_ff"][j] - out["u_ff"][i_walls] > 0.03

    def mornings(run):
        ta, sp, tt = np.array(run["ta"]), np.array(run["sp"]), np.array(run["t"])
        res = []
        for d in range(1, days):
            m = rsim.T0 + d * rsim.DAY + 6 * H
            idx = np.where((tt >= m) & (tt < m + 8 * H))[0]
            hit = [i for i in idx if ta[i] >= 19.9]
            day = np.where((tt >= m + 4 * H) & (tt < m + 16 * H))[0]
            res.append(((tt[hit[0]] - m) / H if hit else None, ta[idx].max(), np.abs(ta[day] - sp[day]).max(),
                        np.mean(sp[day] - ta[day])))
        return res

    for reach, peak, dev, _ in mornings(out):
        assert reach is not None and reach < 3.0
        assert peak < 20.2
        assert dev < 0.2
    for reach, _, _, mean_short in mornings(ref):
        assert reach is None
        assert mean_short > 0.2
    assert all(s == SOURCE_WALLS for s, tt in zip(src, t) if tt >= rsim.T0 + rsim.DAY)


def test_closed_loop_restart_keeps_the_room_model_source(caplog) -> None:
    """A restart during the morning rise resumes the wall source with the restored integral, without transfer."""
    caplog.set_level(logging.WARNING)
    p = dict(rsim.MASS)
    done = {"v": False}

    def publish(algo, t):
        if not done["v"] and t >= rsim.T0 + 14 * H:
            assert rsim.apply_energy_balance(algo, t)["reason"] is None
            params = RoomParams("full", p["a"], p["alpha"], p["beta"], p["c"], p["tau_e"], p["L"])
            algo.room_model.params = params
            algo.room_model.q = 0.01
            algo.room_model.observer.configure(params, 0.01)
            done["v"] = True

    out = rsim.run_smartpi(p, 1.5, b_online_ratio=0.65, on_tick=publish, restart_at=rsim.T0 + rsim.DAY + 7 * H,
                           history_days=8.0)
    i = out["restart"]["i"]
    assert out["restart"]["state"]["ff_source"]["kind"] == SOURCE_WALLS
    assert out["source"][i - 1] == out["source"][i] == SOURCE_WALLS
    # Same holding power law, integral restored: the command goes on without a step
    assert abs(out["u_ff"][i] - out["u_ff"][i - 1]) < 0.005
    assert abs(out["i_power"][i] - out["i_power"][i - 1]) < 0.005
    assert abs(out["u"][i] - out["u"][i - 1]) < 0.01
