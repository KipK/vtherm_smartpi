"""Room model manager: daily identification, publication, persistence, diagnostics and fallbacks."""

from __future__ import annotations

import asyncio
import json

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
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
}


class _SyncHass:
    """Executor stand-in running the job in the test thread."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _tar(data: dict) -> TarLearningManager:
    tar = TarLearningManager("room", "UTC")
    tar.set_dependencies(True)
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
    m = RoomModelManager("room", "UTC", _tar(data))
    _drive(m, data, rsim.MASS, rsim.T0 + 600.0, rsim.T0 + DAYS * 86400.0)
    return m, data


def test_end_to_end_identification_and_observation(mass_manager) -> None:
    m, _ = mass_manager
    outcomes = [(r["day"] - D0, r["why"], r["struct"]) for r in m.records]
    assert outcomes[0] == (0, gates.WHY_HISTORY, None)
    assert (3, gates.WHY_NO_PREVIOUS, None) in outcomes
    assert all(s == "full" for d, _, s in outcomes if d >= 5)
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
    assert public.status(None, gates.WHY_HEAT, True) == "not identified: too little heating during the nights"
    assert public.status(None, gates.WHY_NO_PREVIOUS, True) == "learning"
    assert public.status("reduced", None, True) == "simple model"


def test_persistence_round_trip(mass_manager) -> None:
    m, data = mass_manager
    state = json.loads(json.dumps(m.save_state()))
    m2 = RoomModelManager("room", "UTC", m._tar)
    m2.load_state(state)
    assert m2.params == m.params
    assert m2.observer.x == m.observer.x
    assert m2.records == m.records and m2.windows == m.windows
    d1, d2 = m.diagnostics(), m2.diagnostics()
    for k in ("status", "a", "b_eq", "b_air", "b_wall", "forecast_error_c", "identified_day", "walls"):
        assert d1[k] == d2[k]
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

    def boom(snap):
        raise RuntimeError("bad fit")

    monkeypatch.setattr(manager_mod, "_run_job", boom)
    await m.async_run(_SyncHass(), now=now)
    diag = m.diagnostics()
    assert diag["status"] == "not identified: computation failed"
    assert diag["last_error"] == "bad fit"
    # Once a day only
    await m.async_run(_SyncHass(), now=now + 3600.0)
    assert len(m.records) == 1

    def no_numpy(snap):
        raise ImportError("No module named 'scipy'")

    monkeypatch.setattr(manager_mod, "_run_job", no_numpy)
    await m.async_run(_SyncHass(), now=now + 86400.0)
    assert not tar.active and not m.active
    assert m.diagnostics()["status"] == "not identified: numerical libraries unavailable"


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
