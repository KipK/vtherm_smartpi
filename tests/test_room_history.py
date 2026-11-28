"""Room history: recorded signals, data-quality rules, catalogue of the quiet periods, persistence."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import regulation_sim as sim
import room_model_sim as rsim
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.const import HVAC_OFF_REASON_WINDOW_DETECTION
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.room_history import manager as manager_mod
from custom_components.vtherm_smartpi.smartpi.room_history.clock import LocalClock
from custom_components.vtherm_smartpi.smartpi.room_history.const import (
    HISTORY_GAP_S,
    HISTORY_RETENTION_S,
    HISTORY_SAVE_DELAY_S,
)
from custom_components.vtherm_smartpi.smartpi.room_history.history import RoomHistory
from custom_components.vtherm_smartpi.smartpi.room_history.manager import RoomHistoryManager
from custom_components.vtherm_smartpi.smartpi.room_history.signals import level_bins, q_obs
from custom_components.vtherm_smartpi.smartpi.valve_curve import VALVE_CURVE_DEFAULTS
from custom_components.vtherm_smartpi.state_store import SmartPIRoomHistoryStore

W = rsim.T0


def _obs(h: RoomHistory, t: float, temp=20.0, ext=5.0, **kw):
    return h.record(t, temp=temp, ext=ext, setpoint=kw.pop("sp", 20.0), **kw)


def _run(h: RoomHistory, t0: float, t1: float, step: float = 60.0, **kw) -> None:
    t = t0
    while t <= t1:
        _obs(h, t, **kw)
        t += step


def _invalid(h: RoomHistory, a: float, b: float) -> bool:
    return any(g0 < b and g1 > a for g0, g1 in h.invalid_intervals())


def _clean(h: RoomHistory, t0: float, t1: float, start: float = 20.0) -> None:
    """Valid samples: a precise sensor rising by 0.01 C per minute, measured each minute."""
    t, v = t0, start
    while t <= t1:
        _obs(h, t, temp=v)
        t += 60.0
        v += 0.01


def _manager(data: dict | None = None, position: bool = True, tz: str = "UTC") -> RoomHistoryManager:
    """Manager whose history holds a simulated record (setback nights at the test position)."""
    m = RoomHistoryManager("room", tz)
    m.set_dependencies(True)
    if position:
        m.set_position(rsim.LAT, rsim.LON)
    if data is not None:
        h = m.history
        h.temp_t, h.temp_v = list(data["pub_t"]), list(data["pub_v"])
        h.ext_t, h.ext_v = list(data["ext_t"]), list(data["ext_v"])
        h.on, h.off = list(data["on"]), list(data["off"])
        h.setpoints = [tuple(x) for x in data["sp"]]
        h.first_t, h.last_t = data["first_t"], data["last_t"]
    return m


@pytest.fixture(scope="module")
def week():
    return rsim.simulate(rsim.NO_MASS, 7.5)


class _RecordingHass:
    def __init__(self) -> None:
        self.funcs: list = []

    async def async_add_executor_job(self, func, *args):
        self.funcs.append(func)
        return func(*args)


# ---------------------------------------------------------------------------
# Clock and signals
# ---------------------------------------------------------------------------
def test_local_clock_follows_daylight_saving() -> None:
    c = LocalClock("Europe/Paris")
    day = dt.date(2026, 3, 29).toordinal()  # spring forward at 02:00
    t8 = c.at(day, 8.0)
    assert dt.datetime.fromtimestamp(t8, dt.timezone.utc).hour == 6  # 08:00 CEST
    assert c.day_of(t8) == day
    # 23:00 of the previous evening is 8 hours (not 9) before 08:00 on that night
    assert t8 - c.at(day - 1, 23.0) == 8 * 3600.0


def test_level_bins_and_sensor_step() -> None:
    pw_t, pw_v = [100.0, 250.0, 400.0], [0.5, 0.2, 0.0]
    grid = np.asarray([0.0, 100.0, 200.0, 300.0, 400.0])
    # 0 before the first sample; 0.5 on [100, 250); 0.2 on [250, 400); 0 after
    assert level_bins(pw_t, pw_v, grid, 100.0) == pytest.approx([0.0, 0.5, 0.5 * 0.5 + 0.2 * 0.5, 0.2, 0.0])
    assert level_bins([], [], grid, 100.0).tolist() == [0.0] * 5
    assert q_obs([20.0, 20.1, 20.1, 20.3]) == pytest.approx(0.1)
    assert q_obs([20.0, 20.0]) is None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def test_history_keeps_changes_only_and_reports_setpoint_changes() -> None:
    h = RoomHistory()
    assert _obs(h, 0.0, 20.0, sp=19.0) is None
    _obs(h, 60.0, 20.0, sp=19.0)
    _obs(h, 120.0, 20.1, sp=19.0)
    assert h.temp_t == [0.0, 120.0]
    assert _obs(h, 180.0, 20.1, sp=20.0) == 19.0
    assert h.setpoints == [(0.0, 19.0), (180.0, 20.0)] and len(h.ext_t) == 1


def test_history_rebuilds_heater_edges_from_cycles() -> None:
    h = RoomHistory()
    _obs(h, 0.0)
    h.applied_cycle(0.0, 0.25, 600.0, True)       # on 0 -> 150
    _obs(h, 300.0)
    h.applied_cycle(600.0, 1.0, 600.0, True)      # on 600 ...
    h.applied_cycle(1200.0, 0.5, 600.0, True)     # ... stays on -> 1500
    h.applied_cycle(1800.0, 0.0, 600.0, True)
    h.flush(2400.0)
    assert h.on == [0.0, 600.0]
    assert h.off == [150.0, 1500.0]
    # An early restart of the cycle while on keeps the heater on, a zero power cuts it
    h.applied_cycle(2400.0, 0.5, 600.0, True)
    h.applied_cycle(2500.0, 0.0, 600.0, True)
    assert h.on[-1] == 2400.0 and h.off[-1] == 2500.0


def test_history_gaps_prune_and_persistence() -> None:
    h = RoomHistory()
    _obs(h, 0.0)
    _obs(h, 100.0, 20.2)
    _obs(h, 100.0 + HISTORY_GAP_S + 10.0, 20.3)
    assert h.gaps == [(100.0, 100.0 + HISTORY_GAP_S + 10.0)]
    assert _invalid(h, 50.0, 500.0) and not _invalid(h, 0.0, 90.0)
    h.applied_cycle(5000.0, 0.5, 600.0, True)
    _obs(h, 5100.0, 20.4)
    restored = RoomHistory.from_dict(h.to_dict())
    assert restored.temp_v == h.temp_v and restored.gaps == h.gaps
    # A heater on at save time is closed at the last observation when the restart is not
    # bridged (no open on period across it)
    restored.resolve_restart(False, 5200.0)
    assert len(restored.on) == len(restored.off) and restored.off[-1] == 5100.0
    later = HISTORY_RETENTION_S + 6000.0
    _obs(restored, later, 21.0)
    restored.prune(later)
    assert restored.temp_t == [later]
    assert len(restored.setpoints) == 1  # last setpoint before the cutoff is kept
    assert RoomHistory.from_dict({"temp_t": [1.0], "temp_v": []}).is_empty()


def test_history_of_an_earlier_version_loads_and_its_extra_series_are_dropped() -> None:
    """The phases and parameters an earlier version recorded with the history are not read."""
    h = RoomHistory()
    _clean(h, 0.0, 600.0)
    data = dict(h.to_dict(), phases=[[0.0, "STABLE"]], params=[[0.0, 0.002, None, 300.0]])
    back = RoomHistory.from_dict(json.loads(json.dumps(data)))
    assert back.temp_v == h.temp_v and back.setpoints == h.setpoints
    assert "phases" not in back.to_dict() and "params" not in back.to_dict()


# ---------------------------------------------------------------------------
# Data quality: every rule marks an invalid interval
# ---------------------------------------------------------------------------
def test_clean_data_holds_no_invalid_interval() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 6 * 3600.0)
    assert h.invalid_intervals() == []


def test_observation_gap_is_invalid_and_never_bridged() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 3600.0)
    _clean(h, 3600.0 + HISTORY_GAP_S + 60.0, 3 * 3600.0, start=21.0)
    assert h.gaps == [(3600.0, 3600.0 + HISTORY_GAP_S + 60.0)]
    assert _invalid(h, 3000.0, 4000.0)
    assert not _invalid(h, 600.0, 3000.0)


@pytest.mark.parametrize("field", ["temp", "ext"])
def test_unavailable_temperature_is_invalid(field: str) -> None:
    h = RoomHistory()
    _clean(h, 0.0, 3600.0)
    missing = {"temp": None} if field == "temp" else {"temp": 20.6, "ext": None}
    _obs(h, 3660.0, **missing)
    _obs(h, 3720.0, **missing)
    assert h.invalid_reason == ("indoor" if field == "temp" else "outdoor") + " temperature unavailable"
    _obs(h, 3780.0, temp=20.7)
    assert h.gaps == [(3660.0, 3780.0)]
    assert h.invalid_since is None


def test_restart_not_bridged_marks_only_its_downtime_invalid() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 3600.0)
    restored = RoomHistory.from_dict(h.to_dict())
    _obs(restored, 3900.0, temp=20.62)  # 5 min later, no restart decision: not bridged
    assert restored.gaps == [(3600.0, 3900.0)]
    assert not _invalid(restored, 3000.0, 3600.0) and not _invalid(restored, 3900.0, 4500.0)
    assert restored.restarts == [(3600.0, 3900.0, False)]


def test_invalid_period_open_at_save_is_kept_after_restart() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 600.0)
    _obs(h, 660.0, excluded="window open")
    restored = RoomHistory.from_dict(h.to_dict())
    assert (660.0, 660.0) not in restored.gaps
    assert any(a == 660.0 for a, _ in restored.gaps)


def test_spike_is_dropped_and_a_confirmed_new_level_is_kept() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 1200.0)
    n = len(h.temp_v)
    _obs(h, 1230.0, temp=35.0)
    assert len(h.temp_v) == n and h.invalid_reason == "temperature jump"
    _obs(h, 1260.0, temp=20.22)
    assert 35.0 not in h.temp_v and _invalid(h, 1230.0, 1260.0)
    for k, t in enumerate((1290.0, 1300.0, 1310.0)):
        _obs(h, t, temp=23.0 + 0.01 * k)
    assert h.temp_v[-1] == 23.02 and _invalid(h, 1290.0, 1310.0)


@pytest.mark.parametrize("step_c, rate_c_per_h, period_s", [(0.1, 6.0, 30.0), (0.01, 6.0, 10.0), (0.1, 12.0, 10.0)])
def test_a_fast_real_heating_is_never_cut(step_c: float, rate_c_per_h: float, period_s: float) -> None:
    """A strong heating raising the room by up to 12 C/h, seen by a 0.1 C or a 0.01 C sensor."""
    h = RoomHistory()
    _clean(h, 0.0, 3600.0)
    base = h.temp_v[-1]
    t = 3600.0
    while t < 3600.0 + 7200.0:
        t += period_s
        true = base + rate_c_per_h * (t - 3600.0) / 3600.0
        _obs(h, t, temp=round(round(true / step_c) * step_c, 3))
    assert h.invalid_intervals() == []
    assert h.temp_v[-1] > base + rate_c_per_h * 1.9


@pytest.mark.parametrize("reason", ["window open", "power shedding", "safety mode", "command overridden",
                                    "command unknown"])
def test_excluded_period_is_invalid(reason: str) -> None:
    h = RoomHistory()
    _clean(h, 0.0, 3600.0)
    _run(h, 3660.0, 5400.0, excluded=reason, temp=20.6)
    assert h.invalid_reason == reason
    _obs(h, 5460.0, temp=20.61)
    assert h.gaps == [(3660.0, 5460.0)]


def test_manager_records_hvac_off_as_invalid() -> None:
    m = _manager()
    t = W
    while t < W + 5 * 3600.0:
        mode = VThermHvacMode_OFF if W + 4 * 3600.0 <= t < W + 4.5 * 3600.0 else VThermHvacMode_HEAT
        m.observe(t, hvac_mode=mode, temp=19.0 + (t - W) / 360000.0, ext=3.0, setpoint=18.0)
        t += 60.0
    assert m.history.gaps == [(W + 4 * 3600.0, W + 4.5 * 3600.0)]


def test_clock_going_back_starts_a_new_origin_without_data_on_the_interval() -> None:
    h = RoomHistory()
    _clean(h, 3600.0, 7200.0)
    # The clock goes back by one hour: the observations are not frozen until it catches up.
    _clean(h, 5400.0, 5400.0 + 1800.0, start=21.0)
    assert h.last_t == 5400.0 + 1800.0
    assert h.temp_t == sorted(h.temp_t) and max(h.temp_t) <= h.last_t
    assert h.temp_v[-1] > 21.2
    # Back before the first observation: nothing of the old origin is kept.
    _clean(h, 600.0, 1200.0, start=19.0)
    assert h.first_t == 600.0 and h.last_t == 1200.0
    assert min(h.temp_t) >= 600.0 and max(h.temp_t) <= 1200.0
    assert h.invalid_intervals() == []


def test_clock_going_back_closes_the_heater_state() -> None:
    h = RoomHistory()
    _clean(h, 0.0, 600.0)
    h.applied_cycle(600.0, 1.0, 600.0, True)
    _clean(h, 660.0, 1200.0, start=20.7)
    _obs(h, 300.0, temp=21.0)
    assert h.on == [] and h.off == []
    assert h._heater_on is False


# ---------------------------------------------------------------------------
# Valve recording (the room model stays off for valves)
# ---------------------------------------------------------------------------
@pytest.fixture
def valve_supported(monkeypatch):
    """Valve mode switched on, to exercise the valve recording path."""
    monkeypatch.setattr(manager_mod, "HISTORY_VALVE_MODE_SUPPORTED", True)


def test_valve_history_records_levels_starts_and_cuts() -> None:
    h = RoomHistory(valve=True)
    h.applied_level(0.0, 0.3, True)      # cycle start with a level: start
    h.applied_level(120.0, 0.3, False)   # same level: nothing new
    h.applied_level(300.0, 0.5, False)   # rise: start
    h.applied_level(400.0, 0.2, False)   # fall: neither start nor cut
    h.applied_level(600.0, 0.2, True)    # next cycle: start
    h.applied_level(700.0, 0.0, False)   # cut
    h.applied_level(1200.0, 0.0, True)   # closed cycle: nothing
    h.applied_level(1500.0, 1.4, False)  # clamped to full power: start
    assert h.pw_t == [0.0, 300.0, 400.0, 700.0, 1500.0]
    assert h.pw_v == [0.3, 0.5, 0.2, 0.0, 1.0]
    assert h.on == [0.0, 300.0, 600.0, 1500.0] and h.off == [700.0]
    assert h.power_levels() == (h.pw_t, h.pw_v)
    assert RoomHistory().power_levels() is None


def test_valve_history_round_trip_and_prune() -> None:
    h = RoomHistory(valve=True)
    _obs(h, 0.0, 19.0)
    h.applied_level(0.0, 0.4, True)
    _obs(h, 600.0, 19.1)
    back = RoomHistory.from_dict(h.to_dict())
    assert back.valve
    # A restart that is not bridged cuts the level at the last observation
    back.resolve_restart(False, 700.0)
    assert back.pw_t == [0.0, 600.0] and back.pw_v == [0.4, 0.0] and back.off == [600.0]
    pwm = RoomHistory.from_dict(RoomHistory().to_dict())
    assert not pwm.valve and pwm.pw_t == []
    h.applied_level(3600.0, 0.6, False)
    h.prune(3600.0 + 8 * 86400.0)
    assert h.pw_t == [3600.0] and h.pw_v == [0.6]


def test_valve_manager_records_the_level_timeline(valve_supported) -> None:
    m = _manager()
    m.observe(W, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=2.0, setpoint=19.0, valve=True)
    m.on_applied(W, 0.35, True, 10.0, VThermHvacMode_HEAT, valve=True)
    m.on_applied(W + 60.0, 0.8, False, 10.0, VThermHvacMode_HEAT, valve=True)
    m.on_applied(W + 120.0, 0.8, False, 10.0, VThermHvacMode_OFF, valve=True)  # not heating: no power
    m.on_applied(W + 180.0, 0.5, True, 10.0, VThermHvacMode_HEAT, valve=False)  # other actuator: ignored
    assert m.active
    snap = m.signals_snapshot(W + 600.0)
    assert snap["pw"] == ([W, W + 60.0, W + 120.0], [0.35, 0.8, 0.0])
    assert snap["on"] == [W, W + 60.0] and snap["off"] == [W + 120.0]
    # A change of actuator restarts the history
    m.observe(W + 660.0, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=2.0, setpoint=19.0, valve=False)
    assert not m.history.valve and m.history.on == [] and m.history.temp_v == [19.0]


def test_valve_mode_is_not_supported() -> None:
    m = _manager()
    m.observe(W, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=2.0, setpoint=19.0, valve=True)
    assert not m.active and m.history.is_empty()
    m.observe(W + 60.0, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=2.0, setpoint=19.0)  # PWM stays supported
    assert m.active and not m.history.is_empty()


def _valve_algo(curve: bool) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="Valve", room_model=True)
    algo.configure_valve_linearization(curve, VALVE_CURVE_DEFAULTS, valve_mode=True)
    sim.seed_stable(algo)
    algo.room_history.set_dependencies(True)
    return algo


@pytest.mark.parametrize(("curve", "opening", "expected"), [(False, 0.15, 0.15), (True, 0.15, 0.8)])
def test_smartpi_records_the_heat_equivalent_valve_power(valve_supported, curve: bool, opening: float,
                                                        expected: float) -> None:
    """The recorded level is the applied opening through the inverse valve curve."""
    algo = _valve_algo(curve)
    algo.observe_room_history(19.0, 18.5, 2.0, VThermHvacMode_HEAT)
    algo._commit_applied_command(opening, VThermHvacMode_HEAT, cycle_start=True)
    history = algo.room_history.history
    assert history.valve and history.pw_v[-1] == pytest.approx(expected) and len(history.on) == 1


def test_smartpi_in_valve_mode_records_nothing() -> None:
    algo = _valve_algo(True)
    algo.observe_room_history(19.0, 18.5, 2.0, VThermHvacMode_HEAT)
    algo._commit_applied_command(0.15, VThermHvacMode_HEAT, cycle_start=True)
    assert algo.room_history.history.is_empty()
    assert algo.get_published_diagnostics()["room_model"]["last_refusal_reason"] == (
        "valve heaters are not supported yet")


# ---------------------------------------------------------------------------
# Quiet periods and home position
# ---------------------------------------------------------------------------
def test_position_is_validated() -> None:
    m = RoomHistoryManager("pos", "UTC")
    for lat, lon in ((None, None), (91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0), ("48.8", 2.3), (True, 2.0)):
        m.set_position(lat, lon)
        assert m.position is None
    m.set_position(0.0, 0.0)  # zero is a valid coordinate
    assert m.position is not None


def test_catalogue_keys_do_not_depend_on_the_refresh_the_time_zone_or_the_pruning(week) -> None:
    end = week["last_t"]
    m = _manager(week)
    for k in range(1, 7 * 24):
        m._cat_t = None
        m.quiet_periods(W + k * 3600.0)
    ids = [e["id"] for e in m.quiet_periods(end)]
    assert len(ids) >= 5
    once = _manager(week, tz="Europe/Paris")
    assert [e["id"] for e in once.quiet_periods(end)] == ids
    # Pruning the history in the middle of the stored periods neither changes nor duplicates them
    m.history.prune(end + 7 * 86400.0 - 3 * 86400.0)
    m._cat_t = None
    assert [e["id"] for e in m.quiet_periods(end + 7200.0)] == ids


def test_without_position_there_is_no_quiet_period(week) -> None:
    m = _manager(week, position=False)
    assert m.quiet_periods(week["last_t"]) == []
    diag = m.diagnostics()
    assert diag["quiet_periods"] == {"reason": "home position unknown: the absence of sun cannot be checked",
                                     "count": 0, "last": None}
    m2 = _manager(week)
    m2.quiet_periods(week["last_t"])
    diag = m2.diagnostics()
    assert diag["quiet_periods"]["reason"] is None and diag["quiet_periods"]["count"] == len(m2.episodes)
    assert set(diag["quiet_periods"]["last"]) == {"start", "end", "regulated_from"}
    assert diag["history_h"] == pytest.approx((week["last_t"] - week["first_t"]) / 3600.0, abs=0.1)
    assert diag["data_invalid_reason"] is None


def test_a_new_position_forgets_the_periods_not_the_history(week) -> None:
    m = _manager(week)
    m.quiet_periods(week["last_t"])
    state = json.loads(json.dumps(m.save_state()))
    moved = RoomHistoryManager("room", "UTC")
    moved.load_state(state)
    moved.set_position(rsim.LAT + 1.0, rsim.LON)
    assert moved.episodes == [] and moved.history.temp_v == m.history.temp_v
    same = RoomHistoryManager("room", "UTC")
    same.set_position(rsim.LAT, rsim.LON)
    same.load_state(state)
    assert same.episodes == m.episodes


async def test_the_sun_is_scanned_in_the_executor_once_a_day(week) -> None:
    m = _manager(week)
    hass = _RecordingHass()
    now = week["last_t"]
    await m.async_run(hass, now)
    await m.async_run(hass, now + 600.0)
    assert hass.funcs.count(manager_mod.dark_intervals) == 1
    await m.async_run(hass, now + 86400.0)
    assert hass.funcs.count(manager_mod.dark_intervals) == 2
    m.set_dependencies(False)
    await m.async_run(hass, now + 2 * 86400.0)
    assert hass.funcs.count(manager_mod.dark_intervals) == 2 and m.history.is_empty()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def test_store_round_trip_keeps_the_history_and_the_periods(hass, week) -> None:
    m = _manager(week)
    m.quiet_periods(week["last_t"])
    store = SmartPIRoomHistoryStore(hass, "room_history_test", HISTORY_SAVE_DELAY_S)
    await store.save_now(m)
    loaded = RoomHistoryManager("room", "UTC")
    loaded.set_position(rsim.LAT, rsim.LON)
    loaded.load_state(await store.async_load())
    assert loaded.history.on == m.history.on and loaded.history.temp_v == m.history.temp_v
    assert loaded.episodes == m.episodes
    # The catalogue goes on from the loaded periods: the keys the room model knows are kept
    assert [e["id"] for e in loaded.quiet_periods(week["last_t"] + 7200.0)] == [e["id"] for e in m.episodes]


def _emitter_lag_state(m: RoomHistoryManager, version: int) -> dict:
    """A state saved by the emitter lag learning of an earlier version, in the same store."""
    state = json.loads(json.dumps(m.save_state()))
    hist = dict(state["history"], phases=[[W, "STABLE"]], params=[[W, 0.002, None, 600.0]])
    episodes = [dict(e, day=rsim.day_of(e["end"])) for e in state["episodes"]]
    key = episodes[-1]["id"] if episodes else "q1:0.0:0.0"
    return dict(state, version=version, history=hist, episodes=episodes, bootstrap_heat_s=300.0,
                pending=[{"t_sp": W + 3600.0, "eco": 17.0, "sp_to": 20.0}], fast_ids=[key],
                nights=[{"night_id": key, "cls": "mass"}], mornings=[{"day": 1, "why": "accepted", "T": 640.0}],
                windows={key: {"cls": "mass", "nights": [key]}}, rnights={key: {"ok": True}},
                evals=[{"day": 1, "cls": "mass", "te": 600.0}], fast_values=[[key, 640.0]], fast_count=3,
                fast_pub={"value_s": 640.0, "day": 3}, bound_values=[[key, 500.0]], bound_ids=[key],
                bound_count=1, te_pub={"value": 600.0, "day": 3}, refined_cls="mass",
                refined_wall={"k_per_s": 1e-4, "tau_w_s": 3e4}, reference_heat_s=300.0)


@pytest.mark.parametrize("version", [2, 3])
def test_a_saved_emitter_lag_learning_loads_its_history_and_periods_and_ignores_the_rest(week, version) -> None:
    m = _manager(week)
    m.quiet_periods(week["last_t"])
    old = _emitter_lag_state(m, version)
    back = RoomHistoryManager("room", "UTC")
    back.set_position(rsim.LAT, rsim.LON)
    back.load_state(old)
    assert back.history.temp_v == m.history.temp_v and back.history.on == m.history.on
    assert [e["id"] for e in back.episodes] == [e["id"] for e in m.episodes]
    saved = back.save_state()
    assert set(saved) == {"version", "position", "history", "episodes"}
    assert "phases" not in saved["history"] and "params" not in saved["history"]
    # The room model built on it keeps identifying the same periods
    assert [e["id"] for e in back.quiet_periods(week["last_t"])] == [e["id"] for e in m.episodes]


def test_older_and_later_formats(week) -> None:
    m = _manager(week)
    m.quiet_periods(week["last_t"])
    state = json.loads(json.dumps(m.save_state()))
    # Format 1 (fixed-hour selection): the recorded signals only, the periods are found again
    v1 = RoomHistoryManager("room", "UTC")
    v1.load_state({"version": 1, "history": state["history"], "pending": [{"t_sp": W, "eco": 17.0, "sp_to": 19.0}],
                   "fast_pub": {"value_s": 310.0, "day": 1}, "episodes": [{"id": "x"}]})
    assert v1.history.temp_v == m.history.temp_v and v1.episodes == []
    # A later format and a malformed one are ignored
    later = RoomHistoryManager("room", "UTC")
    later.load_state(dict(state, version=4))
    assert later.history.is_empty()
    bad = RoomHistoryManager("room", "UTC")
    bad.load_state(dict(state, episodes=[{"id": 3}]))
    assert bad.history.is_empty() and bad.episodes == []
    bad.load_state(None)
    assert bad.history.is_empty()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def _thermostat(**kw):
    base = {"vtherm_hvac_mode": VThermHvacMode_HEAT, "hvac_off_reason": None, "is_overpowering_detected": False,
            "on_percent": 0.4, "window_manager": SimpleNamespace(is_window_detected=False)}
    base.update(kw)
    return SimpleNamespace(**base)


ALGO = SimpleNamespace(vt_safety_override=False, on_percent=0.4)


@pytest.mark.parametrize(
    "thermostat, algo, expected",
    [
        (_thermostat(), ALGO, None),
        (_thermostat(vtherm_hvac_mode=VThermHvacMode_OFF), ALGO, "hvac not heating"),
        (_thermostat(window_manager=SimpleNamespace(is_window_detected=True)), ALGO, "window open"),
        (_thermostat(hvac_off_reason=HVAC_OFF_REASON_WINDOW_DETECTION), ALGO, "window open"),
        (_thermostat(is_overpowering_detected=True), ALGO, "power shedding"),
        (_thermostat(), SimpleNamespace(vt_safety_override=True, on_percent=0.4), "safety mode"),
        (_thermostat(on_percent=None), ALGO, "command unknown"),
        (_thermostat(on_percent=0.1), ALGO, "command overridden"),
    ],
)
def test_exclusion_reasons(thermostat, algo, expected) -> None:
    assert SmartPIHandler._history_exclusion(thermostat, algo) == expected
