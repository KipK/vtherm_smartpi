"""Room model manager: identification on the quiet periods, energy balance, publication, persistence,
migration, diagnostics and fallbacks."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import job_abort
from custom_components.vtherm_smartpi.smartpi.room_model import energy_balance as eb_mod
from custom_components.vtherm_smartpi.smartpi.room_model import gates, public
from custom_components.vtherm_smartpi.smartpi.room_model import manager as manager_mod
from custom_components.vtherm_smartpi.smartpi.room_model import observer as observer_mod
from custom_components.vtherm_smartpi.smartpi.room_model.const import RM_SAVE_DELAY_S
from custom_components.vtherm_smartpi.smartpi.room_model.manager import RoomModelManager
from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager
from custom_components.vtherm_smartpi.state_store import SmartPIRoomModelStore

DAYS = 9
D0 = rsim.day_of(rsim.T0)

DIAG_KEYS = {
    "status", "tracking", "walls", "a", "b_eq", "b_air", "b_wall", "forecast_c", "forecast_error_c",
    "forecast_checks", "unchanged_temperature_error_c", "wall_extra_power_percent", "identified_day",
    "last_identification_day", "last_refusal_reason", "sensor_resolution_c", "last_error",
    "energy_balance_loss_per_c", "energy_balance_free_gain_percent", "energy_balance_days", "energy_balance_day",
    "energy_balance_refusal_reason",
}


class _SyncHass:
    """Executor stand-in running the job in the test thread."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _tar(data: dict, position: bool = True) -> TarLearningManager:
    tar = TarLearningManager("room", "UTC")
    tar.set_dependencies(True)
    if position:
        tar.set_position(rsim.LAT, rsim.LON)
    h = tar.history
    h.temp_t, h.temp_v = list(data["pub_t"]), list(data["pub_v"])
    h.ext_t, h.ext_v = list(data["ext_t"]), list(data["ext_v"])
    h.on, h.off = list(data["on"]), list(data["off"])
    h.setpoints = [tuple(x) for x in data["sp"]]
    h.first_t, h.last_t = data["first_t"], data["last_t"]
    return tar


def _drive(m: RoomModelManager, data: dict, plant: dict, t_from: float, t_to: float) -> None:
    """Feed the manager like the regulation does, and run the due computations every hour."""
    loop = asyncio.new_event_loop()
    hass = _SyncHass()
    n = [0]

    def feed(t, kind, pl):
        if kind == "cycle":
            m.on_applied(t, pl, True, 10.0, VThermHvacMode_HEAT)
            return
        y, yt, te = pl
        m.observe(t, hvac_mode=VThermHvacMode_HEAT, temp=y, measure_id=yt, ext=te, excluded=None,
                  a_per_min=plant["a"] * 60.0, first_moment_s=plant["tau_e"] + plant["L"])
        n[0] += 1
        if n[0] % 60 == 0:
            loop.run_until_complete(m.async_run(hass, now=t))

    rsim.replay(data, feed, t_from, t_to)
    loop.close()


@pytest.fixture(scope="module")
def mass_manager():
    data = rsim.simulate(rsim.MASS, DAYS)
    tar = _tar(data)
    # The refined first-order part of the emitter is known (600 s of the 720 s first moment): the fitted
    # static gain is sensitive to the emitter shape and must agree with the energy balance within 5 %.
    tar.te_pub = {"value": rsim.MASS["tau_e"]}
    m = RoomModelManager("room", "UTC", tar)
    # A qualified heat balance is in force from the start (the true static gain): nothing is published without one
    m.apply_energy_balance(D0 - 1, {"value": _true_beq_a(rsim.MASS), "gain": 0.0, "days": 4})
    _drive(m, data, rsim.MASS, rsim.T0 + 600.0, rsim.T0 + DAYS * 86400.0)
    return m, data


def _true_beq_a(p: dict) -> float:
    return (p["alpha"] - p["c"]) / p["a"]


def test_end_to_end_identification_and_observation(mass_manager) -> None:
    m, _ = mass_manager
    outcomes = [(r["day"] - D0, r["why"], r["struct"]) for r in m.records]
    # One identification per window of three closed quiet periods (setback 22:00-06:00 and the response to
    # the rise until 08:00), from the morning the third period closes; the first three windows have no
    # earlier disjoint window to agree with
    pers = rsim.quiet_periods(rsim.simulate(rsim.MASS, DAYS))
    assert [r["id"] for r in m.records] == [p["id"] for p in pers[2:]]
    assert [d for d, _, _ in outcomes] == list(range(3, DAYS))
    assert all(why == gates.WHY_NO_PREVIOUS for _, why, _ in outcomes[:3])
    assert all(s == "full" for d, _, s in outcomes if d >= 6)
    # The energy balance runs on its own, once per finished day. Here the walls release the heat of the start of
    # the simulation (wall time 80 h): the daily balances drift and are refused, so the balance in force stays the
    # one given at the start, and every publication is checked against it.
    assert m.eb_run_day == D0 + DAYS - 1 and m.eb_reason == eb_mod.REASON_DRIFT
    assert all(r["eb"] == pytest.approx(_true_beq_a(rsim.MASS)) for r in m.records)
    diag = m.diagnostics()
    assert set(diag) == DIAG_KEYS
    assert diag["status"] == "identified"
    assert diag["walls"] == "identified"
    assert diag["tracking"] == "ok"
    p = rsim.MASS
    assert diag["a"] == pytest.approx(p["a"] * 60.0, rel=0.03)
    assert diag["b_eq"] == pytest.approx((p["alpha"] - p["c"]) * 60.0, rel=0.06)
    assert diag["b_air"] == pytest.approx(p["alpha"] * 60.0, rel=0.03)
    assert diag["b_wall"] == pytest.approx(p["c"] * 60.0, rel=0.03)
    assert diag["b_eq"] == pytest.approx(diag["b_air"] - diag["b_wall"], abs=2e-6)
    assert set(diag["forecast_c"]) == {"1h", "2h", "6h", "12h"}
    assert diag["forecast_checks"]["12h"] > 0
    assert diag["forecast_error_c"]["12h"] < 0.1 * diag["unchanged_temperature_error_c"]["12h"]
    assert isinstance(diag["wall_extra_power_percent"], float)
    assert diag["identified_day"] == diag["last_identification_day"]
    assert diag["last_refusal_reason"] is None
    assert diag["sensor_resolution_c"] == 0.01
    # The walls still release the heat of the start of the simulation (wall time 80 h): the heating need drifts
    # from day to day and the balance given at the start stays in force
    assert diag["energy_balance_loss_per_c"] == pytest.approx(_true_beq_a(rsim.MASS), abs=1e-6)
    assert diag["energy_balance_day"] == (dt.date.fromordinal(D0 - 1)).isoformat()
    assert diag["energy_balance_refusal_reason"] == public.energy_balance_reason(eb_mod.REASON_DRIFT)
    json.dumps(diag)


def test_diagnostics_hold_plain_values_only(mass_manager) -> None:
    m, _ = mass_manager
    codes = {v for k, v in vars(gates).items() if k.startswith(("WHY_", "WALL_"))}
    whys = {v for k, v in vars(gates).items() if k.startswith("WHY_")}
    for why in whys | {public.REASON_VALVE, public.REASON_DEPENDENCIES}:
        assert public.reason(why) not in codes
    for k, v in vars(observer_mod).items():
        if k.startswith("HEALTH_") and v != observer_mod.HEALTH_OK:
            assert public.tracking(v) != v
    for code in (gates.WALL_COARSE, gates.WALL_SMALL, gates.WALL_NOT_CONFIRMED, gates.WALL_NOT_BETTER):
        assert public.walls("reduced", code).startswith("not identified: ")
    assert public.status(None, gates.WHY_HEAT, True) == "not identified: too little heating during the quiet periods"
    assert public.status(None, gates.WHY_INFO, True) == (
        "not identified: the quiet periods do not determine the model precisely enough yet")
    for why in whys:
        assert not any(w in public.reason(why) for w in ("night", "08:00", "18:00", "day"))
    assert public.status(None, gates.WHY_NO_PREVIOUS, True) == "learning"
    assert public.status("reduced", None, True) == "simple model"
    eb_codes = {v for k, v in vars(eb_mod).items() if k.startswith("REASON_")}
    for code in eb_codes | {gates.WHY_FAILED}:
        assert public.energy_balance_reason(code) not in eb_codes | codes
    assert public.energy_balance_reason(None) is None
    assert public.reason(gates.WHY_EB) == "the model's heat loss disagrees with the measured heat balance"


def test_no_publication_without_a_qualified_energy_balance(mass_manager) -> None:
    """The energy balance is the only check of b_eq / a that does not come from the fits: without one in force,
    a window that passes every other check publishes nothing, and the published median is checked again
    against the balance in force when it is published."""
    m, _ = mass_manager
    truth = _true_beq_a(rsim.MASS)
    last = m.records[-1]
    cur = dict(m.windows[last["id"]], why=None)
    prev = m.windows[cur["prev"]]
    ok = gates.decide(cur, prev, last["held"], True, eb=truth)
    assert ok.get("candidate") is not None
    none = gates.decide(cur, prev, last["held"], True, eb=None)
    assert none.get("candidate") is None and none["why"] == gates.WHY_NO_EB
    far = gates.decide(cur, prev, last["held"], True, eb=truth * 1.10)
    assert far.get("candidate") is None and far["why"] == gates.WHY_EB
    assert gates.eb_check(ok["candidate"], None) == gates.WHY_NO_EB
    assert not gates.eb_agrees(ok["candidate"], None)
    assert public.reason(gates.WHY_NO_EB) == "waiting for a qualified heat balance to check the model's heat loss"
    assert public.status(None, gates.WHY_NO_EB, True) == "learning"

    # At publication time: a candidate decided against a balance that is no longer the one in force
    def fresh(eb):
        x = RoomModelManager("room", "UTC", m._tar)
        x.eb_slope = eb
        return x

    rec = {"id": "q9:1:1", "day": D0 + DAYS, "t_end": rsim.T0 + DAYS * 86400.0, "nights": [], "prev_id": None,
           "q": 0.01, "decision": ok, "eb": truth}
    for eb, why in ((None, gates.WHY_NO_EB), (truth * 1.10, gates.WHY_EB)):
        x = fresh(eb)
        x._apply(dict(rec))
        assert x.params is None and x.records[-1]["why"] == why
        assert x.diagnostics()["last_refusal_reason"] == public.reason(why)
    x = fresh(truth)
    x._apply(dict(rec))
    assert x.params is not None and x.records[-1]["why"] is None
    # A model already published stays in force when a later median disagrees with the balance in force
    x.eb_slope = truth * 1.10
    params = x.params
    x._apply(dict(rec, id="q9:1:2"))
    assert x.params == params and x.records[-1]["why"] == gates.WHY_EB


def test_energy_balance_in_force_keeps_the_last_qualified_value() -> None:
    m = RoomModelManager("x", "UTC", None)
    diag = m.diagnostics()
    assert (diag["energy_balance_loss_per_c"], diag["energy_balance_free_gain_percent"], diag["energy_balance_days"],
            diag["energy_balance_day"], diag["energy_balance_refusal_reason"]) == (None, None, None, None, None)
    # A refusal before any qualified value: none, the plain reason
    m.apply_energy_balance(D0, {"value": None, "gain": None, "reason": "history", "slope": None, "days": 0})
    assert m.eb_slope is None and m.eb_gain is None
    assert m.diagnostics()["energy_balance_refusal_reason"] == "not enough recorded days yet"
    # A qualified static model comes into force with its day
    m.apply_energy_balance(D0 + 1, {"value": 0.0321234567, "gain": 0.0612, "reason": None, "slope": 0.0321234567,
                                    "days": 6})
    diag = m.diagnostics()
    assert diag["energy_balance_loss_per_c"] == 0.032123
    assert diag["energy_balance_free_gain_percent"] == 6.1
    assert diag["energy_balance_days"] == 6
    assert diag["energy_balance_day"] == "2026-01-06"
    assert diag["energy_balance_refusal_reason"] is None
    # A refused day keeps the model in force and says why; so does a failed computation
    m.apply_energy_balance(D0 + 2, {"value": None, "gain": None, "reason": "scatter", "slope": 0.04, "days": 5})
    assert (m.eb_slope, m.eb_gain, m.eb_day) == (0.0321234567, 0.0612, D0 + 1)
    assert m.diagnostics()["energy_balance_refusal_reason"] == \
        "the heating need varies too much from one day to the next"
    m.apply_energy_balance(D0 + 3, {"value": None, "gain": None, "reason": "gain", "slope": 0.05, "days": 6})
    assert m.diagnostics()["energy_balance_refusal_reason"].startswith("free heat gains detected")
    m.apply_energy_balance(D0 + 4, None)
    assert (m.eb_slope, m.eb_day) == (0.0321234567, D0 + 1)
    assert m.diagnostics()["energy_balance_refusal_reason"] == "computation failed"
    # Persistence
    state = json.loads(json.dumps(m.save_state()))
    m2 = RoomModelManager("x", "UTC", None)
    m2.load_state(state)
    assert (m2.eb_slope, m2.eb_gain, m2.eb_days, m2.eb_day, m2.eb_reason) == \
        (m.eb_slope, m.eb_gain, m.eb_days, m.eb_day, m.eb_reason)
    # A state saved without it, with an invalid value, or in the earlier form (a ratio without its gain) has none
    for bad in (None, {"slope": "x", "gain": 0.0, "day": D0}, {"slope": -0.01, "gain": 0.0, "day": D0},
                {"slope": float("nan"), "gain": 0.0, "day": D0}, {"slope": 0.03, "gain": 0.0, "day": None},
                {"slope": 0.03, "gain": float("inf"), "day": D0}, {"ratio": 0.03, "day": D0}):
        m3 = RoomModelManager("x", "UTC", None)
        m3.load_state(dict(state, energy_balance=bad))
        assert m3.eb_slope is None and m3.eb_gain is None and m3.eb_day is None
    # A reset forgets it
    m2.reset()
    assert (m2.eb_slope, m2.eb_gain, m2.eb_day, m2.eb_reason) == (None, None, None, None)


def test_persistence_round_trip(mass_manager) -> None:
    m, data = mass_manager
    state = json.loads(json.dumps(m.save_state()))
    m2 = RoomModelManager("room", "UTC", m._tar)
    m2.load_state(state)
    assert m2.params == m.params
    assert m2.observer.x == m.observer.x
    assert m2.records == m.records and m2.windows == m.windows
    d1, d2 = m.diagnostics(), m2.diagnostics()
    for k in ("status", "a", "b_eq", "b_air", "b_wall", "forecast_error_c", "identified_day", "walls",
              "energy_balance_loss_per_c", "energy_balance_free_gain_percent", "energy_balance_days",
              "energy_balance_day", "energy_balance_refusal_reason"):
        assert d1[k] == d2[k]
    assert (m2.eb_slope, m2.eb_gain, m2.eb_day, m2.eb_reason) == (m.eb_slope, m.eb_gain, m.eb_day, m.eb_reason)
    # After a short downtime the observer resumes its state
    t_end = rsim.T0 + DAYS * 86400.0
    t_back = m.observer.t + 900.0
    restarts = m2.observer.restarts
    _drive(m2, data, rsim.MASS, t_back, min(t_back + 600.0, t_end))
    assert m2.observer.restarts == restarts
    # Unknown or corrupt states are ignored
    m3 = RoomModelManager("room", "UTC", m._tar)
    m3.load_state({"version": 99, "params": state["params"]})
    assert m3.params is None
    m3.load_state(dict(state, params={"struct": "full", "a": -1}))
    assert m3.params is None


async def test_store_round_trip(hass, mass_manager) -> None:
    m, _ = mass_manager
    store = SmartPIRoomModelStore(hass, "room_store_test", RM_SAVE_DELAY_S)
    await store.save_now(m)
    loaded = RoomModelManager("room", "UTC", m._tar)
    loaded.load_state(await store.async_load())
    assert loaded.params == m.params
    assert loaded.observer.t == m.observer.t


def test_inactive_learning_and_reset(mass_manager) -> None:
    m, _ = mass_manager
    tar = TarLearningManager("x", "UTC")
    tar.set_dependencies(False)
    off = RoomModelManager("x", "UTC", tar)
    assert not off.active
    assert off.diagnostics()["status"] == "not identified: numerical libraries unavailable"
    tar2 = TarLearningManager("x", "UTC")
    tar2.set_dependencies(True)
    tar2.observe(0.0, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=5.0, setpoint=20.0, phase="stable", b=0.001,
                 dtc=None, dtc_reliable=False, lag=None, cycle_min=10.0, valve=True)
    valve = RoomModelManager("x", "UTC", tar2)
    assert valve.diagnostics()["status"] == "not identified: valve heaters are not supported yet"
    fresh = RoomModelManager("x", "UTC", _tar(rsim.simulate(rsim.NO_MASS, 0.5)))
    diag = fresh.diagnostics()
    assert diag["status"] == "learning" and diag["tracking"] == "waiting for an identified model"
    assert diag["forecast_c"] is None and diag["a"] is None
    state = m.save_state()
    copy = RoomModelManager("room", "UTC", m._tar)
    copy.load_state(state)
    copy.reset()
    assert copy.params is None and copy.records == [] and copy.dirty


def test_other_hvac_modes_pause_the_observer(mass_manager) -> None:
    m, _ = mass_manager
    copy = RoomModelManager("room", "UTC", m._tar)
    copy.load_state(json.loads(json.dumps(m.save_state())))
    t = copy.observer.t + 60.0
    copy.on_applied(t, 0.5, True, 10.0, VThermHvacMode_COOL)
    assert copy.observer.timeline.power == 0.0
    copy.observe(t, hvac_mode=VThermHvacMode_COOL, temp=19.0, measure_id="z", ext=5.0, excluded=None,
                 a_per_min=0.07, first_moment_s=720.0)
    assert copy.diagnostics()["tracking"] == "paused (SmartPI not heating or not in control)"
    assert copy.diagnostics()["forecast_c"] is None
    copy.observe(t + 60.0, hvac_mode=VThermHvacMode_HEAT, temp=19.0, measure_id="z", ext=5.0,
                 excluded="window open", a_per_min=0.07, first_moment_s=720.0)
    assert copy.observer.paused_since is not None


async def test_failed_computation_and_missing_packages(monkeypatch) -> None:
    data = rsim.simulate(rsim.NO_MASS, 5.0)
    tar = _tar(data)
    m = RoomModelManager("room", "UTC", tar)
    m._emitter = (600.0, 120.0)
    now = rsim.T0 + 4 * 86400.0 + 9 * 3600.0

    def boom(kind, snap):
        raise RuntimeError("bad fit")

    monkeypatch.setattr(manager_mod, "_run_job", boom)
    await m.async_run(_SyncHass(), now=now)
    diag = m.diagnostics()
    assert diag["status"] == "not identified: computation failed"
    assert diag["last_error"] == "the calculation failed, see the log"
    assert diag["energy_balance_loss_per_c"] is None
    assert diag["energy_balance_refusal_reason"] == "computation failed"
    # A window is computed once, failed or not; the next one waits for the cadence
    assert len(m.records) == 1 and m.eb_run_day == D0 + 4
    await m.async_run(_SyncHass(), now=now + 600.0)
    assert len(m.records) == 1
    await m.async_run(_SyncHass(), now=now + 3600.0)
    assert len(m.records) == 2 and m.records[0]["id"] != m.records[1]["id"]

    def no_numpy(kind, snap):
        raise ImportError("No module named 'scipy'")

    monkeypatch.setattr(manager_mod, "_run_job", no_numpy)
    await m.async_run(_SyncHass(), now=now + 86400.0)
    assert not tar.active and not m.active
    assert m.diagnostics()["status"] == "not identified: numerical libraries unavailable"


def _fake_jobs(monkeypatch) -> list:
    """_run_job replaced by a recorder: a refused balance, a window refused for its data."""
    calls = []

    def fake(kind, snap):
        calls.append((kind, snap))
        if kind == manager_mod.KIND_BALANCE:
            return {"day": snap["day"], "eb": {"value": None, "gain": None, "reason": "history", "slope": None,
                                               "days": 0}}
        w = snap["window"]
        return {"id": w["id"], "day": D0, "t_end": w["t_end"], "nights": w["nights"], "prev_id": w["prev"],
                "why": gates.WHY_DATA, "decision": {"why": gates.WHY_DATA, "wall": None}}

    monkeypatch.setattr(manager_mod, "_run_job", fake)
    return calls


async def test_identification_starts_when_a_window_of_periods_closes(monkeypatch) -> None:
    """A window of three closed quiet periods starts one identification, oldest first, at most one per hour of
    elapsed time; each window once, compared with the window of the three periods before it."""
    data = rsim.simulate(rsim.NO_MASS, 8.0)
    m = RoomModelManager("room", "UTC", _tar(data))
    m._emitter = (600.0, 120.0)
    calls = _fake_jobs(monkeypatch)
    pers = rsim.quiet_periods(data)
    ends = [p["ident"][1] for p in pers]
    # Before the third period has closed: the balance of the day, no identification
    await m.async_run(_SyncHass(), now=ends[2] - 60.0)
    assert [k for k, _ in calls] == ["balance"]
    # Catch-up after a downtime: the closed windows one at a time, oldest first, one per hour
    t = ends[5] + 120.0
    for i in range(8):
        await m.async_run(_SyncHass(), now=t + i * 1800.0)
    ident = [snap for k, snap in calls if k == "identify"]
    assert [s["window"]["id"] for s in ident] == [p["id"] for p in pers[2:6]]
    assert [s["window"]["nights"] for s in ident][-1] == [p["id"] for p in pers[3:6]]
    # The window of periods 3-5 is compared with the one of periods 0-2: disjoint, and it was computed
    last = ident[-1]
    assert last["window"]["prev"] == pers[2]["id"] and last["prev"] is not None
    assert not set(m.windows[pers[2]["id"]]["nights"]) & set(last["window"]["nights"])
    assert all(s["prev"] is None for s in ident[:3])
    # Never twice on the same periods, even after a reload
    n = len(ident)
    back = RoomModelManager("room", "UTC", m._tar)
    back.load_state(json.loads(json.dumps(m.save_state())))
    await back.async_run(_SyncHass(), now=ends[5] + 6 * 3600.0)
    await m.async_run(_SyncHass(), now=ends[5] + 6 * 3600.0)
    assert len([k for k, _ in calls if k == "identify"]) == n
    # The next period closes the next window
    await m.async_run(_SyncHass(), now=ends[6] + 120.0)
    assert calls[-1][1]["window"]["id"] == pers[6]["id"]


async def test_energy_balance_of_each_finished_day_without_any_quiet_period(monkeypatch) -> None:
    """Without a home position there is no quiet period and no identification; the balance of each finished
    local day still runs, once, from 20 minutes after its end."""
    p = rsim.NO_MASS
    data = rsim.simulate(p, 6.5)
    m = RoomModelManager("room", "UTC", _tar(data, position=False))
    m._a_per_min = p["a"] * 60.0
    m._emitter = (600.0, 120.0)
    kinds = []
    real = manager_mod._run_job

    def spy(kind, snap):
        kinds.append((kind, snap.get("day")))
        return real(kind, snap)

    monkeypatch.setattr(manager_mod, "_run_job", spy)
    for d in range(1, 7):
        for dt_s in (600.0, 1500.0, 9 * 3600.0):
            await m.async_run(_SyncHass(), now=rsim.T0 + d * 86400.0 + dt_s)
    assert kinds == [("balance", D0 + d) for d in range(1, 7)]
    assert m.records == [] and m.windows == {}
    assert m.eb_day == D0 + 6 and m.eb_slope == pytest.approx(p["alpha"] / p["a"], rel=0.03)


async def test_energy_balance_follows_the_local_days_across_clock_changes(monkeypatch) -> None:
    """Days of 23 h and 25 h: one balance per local day, each after its local midnight."""
    from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock

    tz = "Europe/Paris"
    clock = LocalClock(tz)
    for first in (dt.date(2026, 3, 27), dt.date(2026, 10, 23)):
        tar = TarLearningManager("room", tz)
        tar.set_dependencies(True)
        t0 = clock.at(first.toordinal(), 0.0)
        tar.history.temp_t, tar.history.temp_v = [t0], [19.0]
        tar.history.first_t, tar.history.last_t = t0, t0 + 5 * 86400.0
        m = RoomModelManager("room", tz, tar)
        calls = _fake_jobs(monkeypatch)
        t = t0 + 600.0
        while t < clock.at(first.toordinal() + 5, 0.0):
            await m.async_run(_SyncHass(), now=t)
            t += 600.0
        days = [snap["day"] for k, snap in calls if k == "balance"]
        assert days == list(range(first.toordinal(), first.toordinal() + 5))
        for k, snap in calls:
            assert snap["t_last"] >= clock.at(snap["day"], 0.0) + 1200.0


def test_earlier_state_keeps_the_model_and_drops_the_day_keyed_evidence(mass_manager) -> None:
    """A state of the fixed-hour nights: the published model, its observer state, the energy balance in force and
    the records are kept; the windows and candidates keyed by day and the night disturbances summed over local
    hours are dropped, never read as periods."""
    m, _ = mass_manager
    state = json.loads(json.dumps(m.save_state()))
    old = dict(state, version=1, windows={str(D0 + 7): dict(next(iter(state["windows"].values())), day=D0 + 7)},
               candidates=[dict(c, day=D0 + 7) for c in state["candidates"]], last_job_t=None)
    obs = {k: v for k, v in state["observer"].items() if k not in ("steps", "attached_t")}
    old["observer"] = dict(obs, nights={str(D0 + 6): [0.001, 50000.0], str(D0 + 7): [0.002, 50400.0]})
    old["energy_balance"] = {k: v for k, v in state["energy_balance"].items() if k != "run_day"}
    back = RoomModelManager("room", "UTC", m._tar)
    back.load_state(old)
    assert back.params == m.params and back.q == m.q and back.published_day == m.published_day
    assert back.observer.x == m.observer.x and back.observer.P == m.observer.P and back.observer.t == m.observer.t
    assert back.observer.timeline.to_dict() == m.observer.timeline.to_dict()
    assert (back.eb_slope, back.eb_gain, back.eb_day) == (m.eb_slope, m.eb_gain, m.eb_day)
    assert back.records == m.records
    assert back.windows == {} and back.candidates == []
    assert back.observer.nights == {} and back.observer.steps == [] and back.observer.attached_t is None
    assert back.eb_run_day is None and back.last_job_t is None and back.dirty
    assert back.diagnostics()["status"] == m.diagnostics()["status"]
    # Saved again, it is the current format; loading it twice changes nothing
    again = json.loads(json.dumps(back.save_state()))
    assert again["version"] == manager_mod.STATE_VERSION
    twice = RoomModelManager("room", "UTC", m._tar)
    twice.load_state(again)
    twice.load_state(again)
    assert twice.windows == {} and twice.candidates == [] and twice.params == m.params
    # A later format is ignored; a malformed one too
    later = RoomModelManager("room", "UTC", m._tar)
    later.load_state(dict(state, version=manager_mod.STATE_VERSION + 1))
    assert later.params is None
    later.load_state(dict(state, observer=dict(state["observer"], nights={"q1:1:1": ["x"]})))
    assert later.params == m.params and later.observer.nights == {}


def test_emitter_split_uses_the_refined_tau_e() -> None:
    tar = TarLearningManager("x", "UTC")
    m = RoomModelManager("x", "UTC", tar)
    assert m._emitter_split(None) is None
    assert m._emitter_split(900.0) == (900.0, 0.0)
    tar.te_pub = {"value": 600.0}
    assert m._emitter_split(900.0) == (600.0, 300.0)
    assert m._emitter_split(400.0) == (400.0, 0.0)


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------
def _handler():
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.vtherm_smartpi.algo import SmartPI
    from custom_components.vtherm_smartpi.handler import SmartPIHandler
    from fakes.fake_thermostat_runtime import FakeThermostatRuntime

    thermostat = FakeThermostatRuntime()
    thermostat.turn_off()
    thermostat.cycle_min = 5
    thermostat.hass.async_create_task = MagicMock()
    thermostat.hass.async_create_background_task = MagicMock()
    thermostat.prop_algorithm = SmartPI(hass=MagicMock(), cycle_min=5, minimal_activation_delay=0,
                                        minimal_deactivation_delay=0, name="room", tar_learning=True,
                                        room_model=True)
    handler = SmartPIHandler(thermostat)
    for attr in ("_store", "_tar_store", "_room_store"):
        store = MagicMock()
        store.save_now = MagicMock(side_effect=lambda _x: AsyncMock()())
        store.async_load = AsyncMock(return_value=None)
        setattr(handler, attr, store)
    return handler, thermostat


async def test_handler_feeds_saves_and_loads_the_room_model() -> None:
    from unittest.mock import AsyncMock, patch

    handler, thermostat = _handler()
    algo = thermostat.prop_algorithm
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        await handler.control_heating(force=False)
    handler._room_store.schedule_save.assert_called_once_with(algo.room_model)
    handler.remove()
    handler._room_store.save_now.assert_called_once_with(algo.room_model)
    for call in thermostat.hass.async_create_task.call_args_list:
        call.args[0].close()
    state = algo.room_model.save_state()
    handler._room_store.async_load = AsyncMock(return_value=state)
    with patch.object(algo.room_model, "load_state") as load:
        await handler.async_added_to_hass()
    load.assert_called_once_with(state)


def test_room_model_exclusion_names_the_window_that_turns_the_heater_off() -> None:
    from types import SimpleNamespace

    from custom_components.vtherm_smartpi.handler import HVAC_OFF_REASON_WINDOW_DETECTION, SmartPIHandler
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_OFF

    algo = SimpleNamespace(vt_safety_override=False, on_percent=0.0)
    base = {"vtherm_hvac_mode": VThermHvacMode_OFF, "requested_hvac_mode": VThermHvacMode_HEAT,
            "window_manager": SimpleNamespace(is_window_detected=True),
            "hvac_off_reason": HVAC_OFF_REASON_WINDOW_DETECTION, "is_overpowering_detected": False,
            "on_percent": 0.0}
    t = SimpleNamespace(**base)
    # The T_ar learning keeps its reason; the room model is told it is the window
    assert SmartPIHandler._tar_exclusion(t, algo) == "hvac not heating"
    assert SmartPIHandler._room_model_exclusion(t, algo) == "window open"
    # A heater turned off by the user, window open or not, is not a window period
    t = SimpleNamespace(**dict(base, requested_hvac_mode=VThermHvacMode_OFF, hvac_off_reason="manual"))
    assert SmartPIHandler._room_model_exclusion(t, algo) == "hvac not heating"
    t = SimpleNamespace(**dict(base, vtherm_hvac_mode=VThermHvacMode_HEAT, hvac_off_reason=None,
                               window_manager=SimpleNamespace(is_window_detected=False),
                               is_overpowering_detected=True))
    assert SmartPIHandler._room_model_exclusion(t, algo) == "power shedding"


class _ResetDuringJob:
    """Executor stand-in: the manager is reset while the job runs."""

    def __init__(self, manager: RoomModelManager) -> None:
        self.manager = manager

    async def async_add_executor_job(self, func, *args):
        result = func(*args)
        self.manager.reset()
        return result


async def test_results_arriving_after_a_reset_are_dropped(monkeypatch) -> None:
    data = rsim.simulate(rsim.NO_MASS, 5.0)
    m = RoomModelManager("room", "UTC", _tar(data))
    m._emitter = (600.0, 120.0)
    calls = _fake_jobs(monkeypatch)
    await m.async_run(_ResetDuringJob(m), now=rsim.T0 + 4 * 86400.0 + 9 * 3600.0)
    assert calls
    assert m.records == [] and m.windows == {} and m.eb_run_day is None and m.eb_reason is None


async def test_failure_after_a_reset_publishes_nothing(monkeypatch) -> None:
    data = rsim.simulate(rsim.NO_MASS, 5.0)
    m = RoomModelManager("room", "UTC", _tar(data))
    m._emitter = (600.0, 120.0)

    def boom(kind, snap):
        m.reset()  # the learning is reset while the failing fit runs
        raise RuntimeError("bad fit")

    monkeypatch.setattr(manager_mod, "_run_job", boom)
    await m.async_run(_SyncHass(), now=rsim.T0 + 4 * 86400.0 + 9 * 3600.0)
    assert m.last_error is None and m.records == []


async def test_a_stopped_manager_starts_no_computation(monkeypatch) -> None:
    data = rsim.simulate(rsim.NO_MASS, 5.0)
    m = RoomModelManager("room", "UTC", _tar(data))
    m._emitter = (600.0, 120.0)
    calls = _fake_jobs(monkeypatch)
    m.stop()
    await m.async_run(_SyncHass(), now=rsim.T0 + 4 * 86400.0 + 9 * 3600.0)
    assert calls == [] and m.last_error is None


async def test_a_stop_ends_the_running_computation_without_an_error(monkeypatch) -> None:
    data = rsim.simulate(rsim.NO_MASS, 5.0)
    m = RoomModelManager("room", "UTC", _tar(data))
    m._emitter = (600.0, 120.0)

    def job(kind, snap):
        m.stop()  # the thermostat is removed while the fit runs
        job_abort.check()
        raise AssertionError("the job went on after the stop")

    monkeypatch.setattr(manager_mod, "_run_job", job)
    await m.async_run(_SyncHass(), now=rsim.T0 + 4 * 86400.0 + 9 * 3600.0)
    assert m.last_error is None and m.records == []
    assert not m._running


def test_the_abort_of_one_job_does_not_reach_another_thread() -> None:
    stopped = threading.Event()
    stopped.set()
    seen = []
    worker = threading.Thread(target=lambda: seen.append(job_abort.check()))
    with pytest.raises(job_abort.JobAborted):
        job_abort.run(stopped, lambda: None)
    worker.start()
    worker.join()
    assert seen == [None]
    job_abort.check()  # nothing attached to this thread any more
