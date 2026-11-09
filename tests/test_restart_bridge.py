"""Restart bridge: the decision, the T_ar history and the room model across a restart."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.smartpi import restart_bridge as rb
from custom_components.vtherm_smartpi.smartpi.const import RESTART_BRIDGE_MAX_GAP_S
from custom_components.vtherm_smartpi.smartpi.room_model.manager import RoomModelManager
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams
from custom_components.vtherm_smartpi.smartpi.room_model.observer import RoomObserver
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock
from custom_components.vtherm_smartpi.smartpi.tar_learning.history import TarHistory

CYCLE = (10.0, 0, 0, False)
T_STOP = 1_000_000.0


def _before(**kw) -> rb.RestartBefore:
    base = dict(last_ticks=(T_STOP,), heater_known=True, hvac_mode="heat", cycle=CYCLE, excluded=None,
                temp=20.0, temp_since=T_STOP - 600.0)
    base.update(kw)
    return rb.RestartBefore(**base)


def _after(**kw) -> rb.RestartAfter:
    base = dict(now=T_STOP + 52.0, hvac_mode="heat", cycle=CYCLE, excluded=None, temp=20.02)
    base.update(kw)
    return rb.RestartAfter(**base)


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def test_short_stop_with_every_condition_is_bridged() -> None:
    d = rb.decide(_before(), _after())
    assert d.bridged and d.reason is None
    assert d.downtime_s == pytest.approx(52.0)
    assert d.t_stop == T_STOP and d.t_resume == T_STOP + 52.0
    assert d.public() == {"bridged": True, "downtime_s": 52, "reason": None}


@pytest.mark.parametrize(
    "before, after, reason",
    [
        (None, {}, rb.REASON_NO_SNAPSHOT),
        ({"last_ticks": ()}, {}, rb.REASON_NO_SNAPSHOT),
        ({}, {"now": T_STOP + RESTART_BRIDGE_MAX_GAP_S + 1.0}, rb.REASON_LONG),
        # A store written long before the stop makes the downtime long
        ({"last_ticks": (T_STOP, T_STOP - 1500.0)}, {}, rb.REASON_LONG),
        ({}, {"now": T_STOP - 5.0}, rb.REASON_CLOCK),
        ({"last_ticks": (T_STOP, T_STOP + 3600.0)}, {}, rb.REASON_CLOCK),
        ({"heater_known": False}, {}, rb.REASON_HEATER),
        ({"hvac_mode": "off"}, {}, rb.REASON_NOT_ACTIVE_BEFORE),
        ({}, {"hvac_mode": "off"}, rb.REASON_NOT_ACTIVE_AFTER),
        ({"hvac_mode": "heat"}, {"hvac_mode": "cool"}, rb.REASON_MODE_CHANGED),
        ({"hvac_mode": "cool"}, {"hvac_mode": "heat"}, rb.REASON_MODE_CHANGED),
        ({"hvac_mode": "cool", "committed_known": False}, {"hvac_mode": "cool"}, rb.REASON_HEATER),
        ({}, {"cycle": (5.0, 0, 0, False)}, rb.REASON_CYCLE),
        ({"cycle": None}, {}, rb.REASON_CYCLE),
        ({"excluded": "window open"}, {}, "window open before the stop"),
        ({"excluded": "command overridden"}, {}, "command overridden before the stop"),
        ({}, {"excluded": "power shedding"}, "power shedding after the restart"),
        ({}, {"excluded": "safety mode"}, "safety mode after the restart"),
        ({"temp": None}, {}, rb.REASON_TEMPERATURE_UNKNOWN),
        ({}, {"temp": None}, rb.REASON_TEMPERATURE_UNKNOWN),
        ({"temp_since": T_STOP}, {"temp": 20.6}, rb.REASON_TEMPERATURE_JUMP),
    ],
)
def test_each_failed_condition_refuses_the_bridge(before, after, reason) -> None:
    d = rb.decide(None if before is None else _before(**before), _after(**after))
    assert not d.bridged
    assert d.reason == reason


def test_short_restart_in_cool_is_bridged_without_the_heat_histories() -> None:
    # The learning stores hold no heater history in COOL: the committed power is enough
    d = rb.decide(_before(hvac_mode="cool", heater_known=False), _after(hvac_mode="cool"))
    assert d.bridged and d.hvac_mode == "cool"
    assert not d.heat_bridged
    heat = rb.decide(_before(), _after())
    assert heat.heat_bridged and heat.hvac_mode == "heat"


def test_downtime_bound_is_inclusive() -> None:
    assert rb.decide(_before(), _after(now=T_STOP + RESTART_BRIDGE_MAX_GAP_S)).bridged


def test_a_slow_change_of_the_temperature_is_not_a_jump() -> None:
    # 0.6 C since the value held for an hour: 0.6 C/h, physical
    assert rb.decide(_before(temp_since=T_STOP - 3600.0), _after(temp=20.6)).bridged
    # The same step within 52 s is not
    assert not rb.decide(_before(temp_since=T_STOP), _after(temp=20.6)).bridged


def test_bridge_keeps_the_saved_context_until_the_decision() -> None:
    bridge = rb.RestartBridge()
    bridge.note_tick(T_STOP - 60.0, hvac_mode="heat", temp=20.0, cycle=CYCLE, excluded=None)
    bridge.note_tick(T_STOP, hvac_mode="heat", temp=20.0, cycle=CYCLE, excluded=None)
    bridge.note_exclusion(None)
    saved = json.loads(json.dumps(dict(bridge.save(), committed=[0.4, 0.4])))
    assert saved["temp_since"] == T_STOP - 60.0
    loaded = rb.RestartBridge()
    loaded.load(saved)
    assert loaded.pending and loaded.save() == saved  # saved again before the first tick
    before = loaded.before([(T_STOP - 30.0, True)])
    assert before.last_ticks == (T_STOP, T_STOP - 30.0) and before.heater_known
    assert loaded.before([(None, False)]).heater_known is False
    decision = rb.decide(before, _after())
    loaded.resolve(decision)
    assert not loaded.pending and loaded.bridged
    assert loaded.public()["downtime_s"] == 82
    # A thermostat exclusion is kept in the tick context
    bridge.note_exclusion("window open")
    bridge.note_tick(T_STOP + 60.0, hvac_mode="heat", temp=20.0, cycle=CYCLE, excluded=None)
    assert bridge.save()["excluded"] == "window open"


# ---------------------------------------------------------------------------
# T_ar history
# ---------------------------------------------------------------------------
def _obs(h: TarHistory, t: float, temp: float = 20.0) -> None:
    h.record(t, temp=temp, ext=5.0, setpoint=20.0, phase="STABLE", b=0.002, dtc=None, lag=None)


def _pwm_history() -> TarHistory:
    """A PWM heater observed each minute; its last cycle (40 %) started at 3000 s, on until 3240 s."""
    h = TarHistory()
    for i in range(0, 61):
        t = 60.0 * i
        if i % 10 == 0:
            h.applied_cycle(t, 0.4, 600.0, True)
        _obs(h, t, 20.0 + 0.001 * i)
    return h


def _restore(h: TarHistory, saved_t: float) -> TarHistory:
    return TarHistory.from_dict(json.loads(json.dumps(h.to_dict(saved_t))))


def test_bridged_restart_adds_no_invalid_interval_and_no_heater_edge() -> None:
    h = _pwm_history()
    h.applied_cycle(3600.0, 0.4, 600.0, True)  # on from 3600 s
    assert h._heater_on
    edges = (list(h.on), list(h.off))
    back = _restore(h, 3620.0)  # saved at the stop, heater on
    back.resolve_restart(True, 3672.0)
    _obs(back, 3672.0, 20.061)
    back.applied_cycle(3672.0, 0.4, 600.0, True)  # first cycle after the restart, heater still on
    assert back.invalid_intervals() == []
    assert (back.on, back.off) == edges  # the on period goes on: no false stop or start
    back.flush(3672.0 + 241.0)
    assert back.off == edges[1] + [3912.0]
    assert back.restarts == [(3600.0, 3672.0, True)]


def test_bridged_restart_closes_an_on_time_that_ended_before_the_stop() -> None:
    h = _pwm_history()
    h.applied_cycle(3600.0, 0.05, 600.0, True)  # on 3600 -> 3630
    back = _restore(h, 3650.0)  # stopped after the planned end of the on time
    back.resolve_restart(True, 3700.0)
    _obs(back, 3700.0, 20.061)
    back.applied_cycle(3700.0, 0.0, 600.0, True)
    assert back.on[-1] == 3600.0 and back.off[-1] == 3630.0
    assert back.invalid_intervals() == []


def test_bridged_restart_holds_a_heater_on_over_the_stop() -> None:
    h = _pwm_history()
    h.applied_cycle(3600.0, 0.05, 600.0, True)  # on 3600 -> 3630, stopped at 3610 while on
    back = _restore(h, 3610.0)
    back.resolve_restart(True, 3700.0)
    _obs(back, 3700.0, 20.061)
    back.applied_cycle(3700.0, 0.0, 600.0, True)  # the first command after the restart cuts it
    assert back.on[-1] == 3600.0 and back.off[-1] == 3700.0


def test_restart_markers_are_persisted() -> None:
    h = _pwm_history()
    back = _restore(h, 3610.0)
    back.resolve_restart(True, 3660.0)
    _obs(back, 3660.0, 20.06)
    again = _restore(back, 3700.0)
    assert again.restarts == [(3600.0, 3660.0, True)]
    assert again.restarts_between(3500.0, 3610.0) == [(3600.0, 3660.0, True)]
    assert again.restarts_between(3700.0, 4000.0) == []


def test_not_bridged_restart_excludes_only_the_downtime() -> None:
    h = _pwm_history()
    h.applied_cycle(3600.0, 0.4, 600.0, True)
    back = _restore(h, 3620.0)
    back.resolve_restart(False, 3672.0)
    # The heater is not taken as on over the downtime: its on period closes at the last observation
    assert back.off[-1] == 3600.0 and len(back.on) == len(back.off)
    _obs(back, 3672.0, 20.061)
    assert back.gaps == [(3600.0, 3672.0)]
    assert not back.gap_overlaps(3000.0, 3600.0) and not back.gap_overlaps(3672.0, 4200.0)
    assert back.restarts == [(3600.0, 3672.0, False)]


def test_a_store_without_heater_state_is_not_bridged() -> None:
    h = _pwm_history()
    data = h.to_dict(3620.0)
    del data["heater"]
    back = TarHistory.from_dict(data)
    assert back.restart_pending and not back.heater_saved
    back.resolve_restart(True, 3672.0)
    _obs(back, 3672.0, 20.061)
    assert back.gaps == [(3600.0, 3672.0)]


def test_valve_level_is_held_over_a_bridged_restart() -> None:
    h = TarHistory(valve=True)
    _obs(h, 0.0)
    h.applied_level(0.0, 0.4, True)
    _obs(h, 600.0, 20.1)
    back = _restore(h, 620.0)
    back.resolve_restart(True, 660.0)
    _obs(back, 660.0, 20.1)
    back.applied_level(660.0, 0.45, True)
    assert back.pw_t == [0.0, 660.0] and back.pw_v == [0.4, 0.45]
    assert back.off == [] and back.invalid_intervals() == []
    # Not bridged: the level is cut at the last observation
    cut = _restore(h, 620.0)
    cut.resolve_restart(False, 660.0)
    assert cut.pw_v[-1] == 0.0 and cut.off == [600.0]


def test_a_store_saved_twice_before_the_first_observation_keeps_the_first_stop() -> None:
    h = _pwm_history()
    once = _restore(h, 3610.0)
    twice = _restore(once, 3700.0)
    assert twice.to_dict(3800.0)["heater"]["saved_t"] == 3610.0


# ---------------------------------------------------------------------------
# Room model
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mass_run():
    return rsim.simulate(rsim.MASS, 3.0)


def _params(p: dict) -> RoomParams:
    return RoomParams("full", p["a"], p["alpha"], p["beta"], p["c"], p["tau_e"], p["L"])


def _observer() -> RoomObserver:
    ob = RoomObserver(LocalClock("UTC"))
    ob.configure(_params(rsim.MASS), 0.01)
    return ob


def _replay(ob: RoomObserver, data: dict, t_from: float, t_to: float) -> None:
    def feed(t, kind, pl):
        if kind == "cycle":
            ob.timeline.applied_cycle(t, pl, 600.0, True)
            return
        y, yt, te = pl
        ob.observe(t, temp=y, meas_key=yt, meas_t=yt, ext=te, active=True)

    rsim.replay(data, feed, t_from, t_to)


def _restarted(ob: RoomObserver, bridged: bool, stop_t: float) -> RoomObserver:
    back = _observer()
    back.load(json.loads(json.dumps(ob.to_dict())))
    back.resolve_restart(bridged, stop_t)
    return back


def test_room_observer_continues_across_a_bridged_restart(mass_run) -> None:
    ref = _observer()
    # Stop 1 min before a cycle start, restart at it: the simulated heater (which does not hold its
    # state over a stop) and the held command then agree
    t_cut = rsim.T0 + 2 * 86400.0 + 540.0
    _replay(ref, mass_run, rsim.T0 + 600.0, t_cut)
    assert ref.score.origins
    level = ref.timeline.at(t_cut)
    assert level == ref.timeline.at(t_cut + 50.0)
    back = _restarted(ref, True, t_cut + 5.0)
    innov_n, restarts = ref.innov_n, ref.restarts
    # The command at the stop is held over the downtime instead of stopping
    assert back.timeline.at(t_cut + 50.0) == level
    _replay(back, mass_run, t_cut + 60.0, t_cut + 1200.0)
    _replay(ref, mass_run, t_cut, t_cut + 1200.0)
    assert back.restarts == restarts
    assert back.innov_n > innov_n  # no reopening of the innovation checks
    # The forecasts in progress go on: the same origins as without the restart
    assert back.score.origins
    assert {o["t0"] for o in back.score.origins} == {o["t0"] for o in ref.score.origins}
    assert back.x[0] == pytest.approx(ref.x[0], abs=0.02)
    assert back.x[1] == pytest.approx(ref.x[1], rel=0.01)


def test_room_observer_reopens_after_a_restart_that_is_not_bridged(mass_run) -> None:
    ref = _observer()
    t_cut = rsim.T0 + 2 * 86400.0 + 120.0
    _replay(ref, mass_run, rsim.T0 + 600.0, t_cut)
    back = _restarted(ref, False, None)
    assert back.timeline.at(t_cut + 50.0) == 0.0  # the heater is taken as stopped
    assert not back.score.origins
    restarts = back.restarts
    _replay(back, mass_run, t_cut + 60.0, t_cut + 130.0)
    assert back.restarts == restarts and back.innov_n <= 2


def test_room_manager_saves_its_last_tick_and_applies_the_decision() -> None:
    m = RoomModelManager("room", "UTC", None)
    assert m.restart_context() is None
    m.observe(1000.0, hvac_mode="heat", temp=20.0, measure_id=None, ext=5.0, excluded=None, a_per_min=None,
              first_moment_s=None)
    m.on_applied(1000.0, 1.0, True, 10.0, "heat")
    state = json.loads(json.dumps(m.save_state()))
    assert state["restart"]["last_t"] == 1000.0
    back = RoomModelManager("room", "UTC", None)
    back.load_state(state)
    assert back.restart_context() == (1000.0, True)
    back.on_restart(True, 1060.0)
    assert back.observer.timeline.at(1050.0) == 1.0
    assert back.restart_context() is None
    # A state without its last tick (older format): the command over the stop is unknown
    legacy = RoomModelManager("room", "UTC", None)
    legacy.load_state(dict(state, restart=None))
    assert legacy.restart_context() == (None, False)
    legacy.on_restart(False, 1060.0)
    assert legacy.restart_context() is None


def test_a_decision_object_is_immutable() -> None:
    d = rb.decide(_before(), _after())
    with pytest.raises(Exception):
        d.bridged = False  # type: ignore[misc]
    assert replace(d, bridged=False).bridged is False
