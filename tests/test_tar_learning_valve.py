"""T_ar learning in valve mode: the applied power is a level timeline consumed like the PWM heater edges."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import numpy as np
import pytest

import tar_learning_sim as sim
import test_tar_learning_manager as mgr_tests
import test_tar_learning_port as port
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.tar_learning import gates, identification, morning
from custom_components.vtherm_smartpi.smartpi.tar_learning import manager as manager_mod
from custom_components.vtherm_smartpi.smartpi.tar_learning.const import MORNING_EVAL_DELAY_S
from custom_components.vtherm_smartpi.smartpi.tar_learning.history import TarHistory
from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager
from custom_components.vtherm_smartpi.smartpi.tar_learning.signals import level_bins, level_on_grid
from custom_components.vtherm_smartpi.smartpi.valve_curve import VALVE_CURVE_DEFAULTS

W = mgr_tests.W


@pytest.fixture
def valve_supported(monkeypatch):
    """Valve mode switched on, to exercise the valve recording path."""
    monkeypatch.setattr(manager_mod, "TAR_VALVE_MODE_SUPPORTED", True)


def _levels(on, off) -> tuple[np.ndarray, np.ndarray]:
    """Level timeline of a PWM heater: 1 from each on edge, 0 from each off edge."""
    ev = sorted([(float(t), 1.0) for t in on] + [(float(t), 0.0) for t in off])
    return np.asarray([e[0] for e in ev]), np.asarray([e[1] for e in ev])


def _tick(m: TarLearningManager, t: float, valve: bool = True, hvac_mode=VThermHvacMode_HEAT) -> None:
    m.observe(t, hvac_mode=hvac_mode, temp=19.0, ext=2.0, setpoint=19.0, phase="Stable", b=0.002,
              dtc=None, dtc_reliable=False, lag=None, cycle_min=10.0, valve=valve)


# ---------------------------------------------------------------------------
# Signal primitives
# ---------------------------------------------------------------------------
def test_level_bins_is_the_mean_of_the_held_level() -> None:
    pw_t, pw_v = [100.0, 250.0, 400.0], [0.5, 0.2, 0.0]
    grid = np.asarray([0.0, 100.0, 200.0, 300.0, 400.0])
    u = level_bins(pw_t, pw_v, grid, 100.0)
    # 0 before the first sample; 0.5 on [100, 250); 0.2 on [250, 400); 0 after
    assert u == pytest.approx([0.0, 0.5, 0.5 * 0.5 + 0.2 * 0.5, 0.2, 0.0])
    grid10, u10 = level_on_grid(pw_t, pw_v, 90.0, 130.0)
    assert grid10.tolist() == [90.0, 100.0, 110.0, 120.0]
    assert u10 == pytest.approx([0.0, 0.5, 0.5, 0.5])
    assert level_bins([], [], grid, 100.0).tolist() == [0.0] * 5


@pytest.mark.parametrize("name", ["m3_d5n3", "pr_d11n7"])
def test_level_path_reproduces_the_reference_on_a_pwm_signal(name: str) -> None:
    """Fed the level timeline of the PWM heater, the consumers give the reference decisions exactly."""
    data, exp = port._load(name)
    pw = _levels(data["on"], data["off"])
    inp = dataclasses.replace(port._inputs(data, exp), power=pw)
    eco = port.ECO[exp["night_cell"][-2:]]
    nights: list[dict] = []
    for m in exp["mornings"]:
        out = morning.evaluate_morning(inp, m["t_sp"], eco, 20.0, nights, m["cls_in_force"], m["params_in_force"])
        assert (out["path"], out["accepted"], out["T"], out["cls"], out["why"]) == (
            port.ref_path(m["path"]), m["acc"], m["T"], m["cls"], port.ref_why(m["why"])), m["day"]
    sp_events = [tuple(x) for x in exp["sp_events"]]
    for n, rec in exp["rnights"].items():
        mine = gates.regulated_night(port.CLOCK, int(n), data["on"], data["off"], data["pub_t"], data["pub_v"],
                                     20.0, sp_events, exp["cycle_s"], power=pw)
        assert (mine["ok"], mine["why"]) == (rec["ok"], rec["why"]), n
    edges = identification.grid_data(data["pub_t"], data["pub_v"], data["on"], data["off"], data["ext_t"],
                                     data["ext_v"], 0.0, data["t_last"], port.CLOCK)
    levels = identification.grid_data(data["pub_t"], data["pub_v"], [], [], data["ext_t"], data["ext_v"], 0.0,
                                      data["t_last"], port.CLOCK, power=pw)
    assert np.array_equal(edges["u"], levels["u"])
    assert np.array_equal(edges["u_fine"], levels["u_fine"])


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def test_valve_history_records_levels_starts_and_cuts() -> None:
    h = TarHistory(valve=True)
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
    assert h.on == [0.0, 300.0, 600.0, 1500.0]
    assert h.off == [700.0]
    assert h.power_levels() == (h.pw_t, h.pw_v)
    assert TarHistory().power_levels() is None


def test_valve_history_round_trip_closes_the_level_at_a_restart() -> None:
    h = TarHistory(valve=True)
    h.record(0.0, temp=19.0, ext=2.0, setpoint=19.0, phase="STABLE", b=0.002, dtc=None, lag=None)
    h.applied_level(0.0, 0.4, True)
    h.record(600.0, temp=19.1, ext=2.0, setpoint=19.0, phase="STABLE", b=0.002, dtc=None, lag=None)
    back = TarHistory.from_dict(h.to_dict())
    assert back.valve
    assert back.pw_t == [0.0, 600.0] and back.pw_v == [0.4, 0.0]
    assert back.off == [600.0]
    # A PWM history keeps its kind and its edges
    pwm = TarHistory.from_dict(TarHistory().to_dict())
    assert not pwm.valve and pwm.pw_t == []


def test_valve_history_prune_keeps_the_level_in_force() -> None:
    h = TarHistory(valve=True)
    h.record(0.0, temp=19.0, ext=2.0, setpoint=19.0, phase="STABLE", b=0.002, dtc=None, lag=None)
    h.applied_level(0.0, 0.4, True)
    h.applied_level(3600.0, 0.6, False)
    h.prune(3600.0 + 8 * 86400.0)
    assert h.pw_t == [3600.0] and h.pw_v == [0.6]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
def test_valve_manager_records_the_level_timeline(valve_supported) -> None:
    m = TarLearningManager("valve", "UTC")
    m.set_dependencies(True)
    _tick(m, W)
    m.on_applied(W, 0.35, True, 10.0, VThermHvacMode_HEAT, valve=True)
    m.on_applied(W + 60.0, 0.8, False, 10.0, VThermHvacMode_HEAT, valve=True)
    m.on_applied(W + 120.0, 0.8, False, 10.0, VThermHvacMode_OFF, valve=True)  # not heating: no power
    m.on_applied(W + 180.0, 0.5, True, 10.0, VThermHvacMode_HEAT, valve=False)  # other actuator: ignored
    assert m.active
    assert m.diagnostics(480.0, True)["status"] == "active"
    snap = m._snapshot_signals(W + 600.0)
    assert snap["pw"] == ([W, W + 60.0, W + 120.0], [0.35, 0.8, 0.0])
    assert snap["on"] == [W, W + 60.0] and snap["off"] == [W + 120.0]


def test_a_change_of_actuator_restarts_the_history(valve_supported) -> None:
    m = TarLearningManager("valve", "UTC")
    m.set_dependencies(True)
    m.fast_pub = {"value_s": 300.0, "day": 0}
    _tick(m, W, valve=False)
    m.on_applied(W, 0.5, True, 10.0, VThermHvacMode_HEAT)
    assert m.history.on == [W] and not m.history.valve
    _tick(m, W + 60.0, valve=True)
    assert m.history.valve and m.history.on == [] and m.history.temp_v == [19.0]
    assert m.fast_pub == {"value_s": 300.0, "day": 0}  # learned values are kept


def test_valve_mode_is_not_supported() -> None:
    m = TarLearningManager("valve", "UTC")
    m.set_dependencies(True)
    _tick(m, W)
    assert not m.active
    assert m.diagnostics(480.0, True)["status"] == "valve_mode_not_supported"
    assert m.history.is_empty()
    _tick(m, W + 60.0, valve=False)  # a PWM heater stays supported
    assert m.active and not m.history.is_empty()


async def test_valve_end_to_end_run_reproduces_the_reference_pipeline() -> None:
    """The manager on a valve history (levels of the reference heater) gives the reference decisions."""
    m, exp = mgr_tests._feed("m3_d5n3")
    h = m.history
    h.valve = True
    pw_t, pw_v = _levels(h.on, h.off)
    h.pw_t, h.pw_v = pw_t.tolist(), pw_v.tolist()
    hass = mgr_tests._SyncHass()
    for k in range(1, 12):
        await m.async_run(hass, W + k * 86400.0 + 2 * 3600.0 + 700.0)
        await m.async_run(hass, W + k * 86400.0 + MORNING_EVAL_DELAY_S + 60.0)
    base = m._clock.day_of(W)
    ref = {x["day"]: x for x in exp["mornings"]}
    assert len(m.mornings) == len(ref)
    for rec in m.mornings:
        r = ref[rec["day"] - base]
        assert (rec["path"], rec["accepted"], rec["T"], rec["cls"], rec["why"]) == (
            port.ref_path(r["path"]), r["acc"], r["T"], r["cls"], port.ref_why(r["why"]))
    got = [(e["day"] - base, e["cls"], e["te"]) for e in m.evals]
    assert [(g[0], g[1]) for g in got] == [(e["day"], e["cls"]) for e in exp["evals"]]
    for g, e in zip(got, exp["evals"]):
        assert g[2] == pytest.approx(e["te"], rel=1e-3)


# ---------------------------------------------------------------------------
# SmartPI wiring
# ---------------------------------------------------------------------------
def _valve_algo(curve: bool) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="TarValve", tar_learning=True)
    algo.configure_valve_linearization(curve, VALVE_CURVE_DEFAULTS, valve_mode=True)
    sim.seed_stable(algo)
    algo.tar_learning.set_dependencies(True)
    return algo


@pytest.mark.parametrize(("curve", "opening", "expected"), [(False, 0.15, 0.15), (True, 0.15, 0.8)])
def test_smartpi_records_the_heat_equivalent_valve_power(valve_supported, curve: bool, opening: float,
                                                        expected: float) -> None:
    """The recorded level is the applied opening through the inverse valve curve."""
    algo = _valve_algo(curve)
    algo.observe_tar_learning(19.0, 18.5, 2.0, VThermHvacMode_HEAT)
    algo._commit_applied_command(opening, VThermHvacMode_HEAT, None, cycle_start=True)
    tar = algo.tar_learning
    assert tar.active
    assert tar.history.valve
    assert tar.history.pw_v[-1] == pytest.approx(expected)
    assert len(tar.history.on) == 1
    assert algo.get_published_diagnostics()["tar_learning"]["status"] == "active"


def test_smartpi_in_valve_mode_keeps_the_bootstrap_dead_time() -> None:
    algo = _valve_algo(True)
    algo.tar_learning.fast_pub = {"value_s": 300.0, "day": 0}
    algo.observe_tar_learning(19.0, 18.5, 2.0, VThermHvacMode_HEAT)
    algo._commit_applied_command(0.15, VThermHvacMode_HEAT, None, cycle_start=True)
    assert algo.regulation_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    assert algo.tar_learning.history.is_empty()
    assert algo.get_published_diagnostics()["tar_learning"]["status"] == "valve_mode_not_supported"
