"""T_ar learning manager: step detection, executor jobs, end-to-end run, persistence, fallbacks."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

import test_tar_learning_port as port
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.tar_learning import manager as manager_mod
from custom_components.vtherm_smartpi.smartpi.tar_learning.const import MORNING_EVAL_DELAY_S, TAR_SAVE_DELAY_S
from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager
from custom_components.vtherm_smartpi.state_store import SmartPITarStore

W = dt.datetime(2026, 1, 5, 6, 0, tzinfo=dt.timezone.utc).timestamp()  # simulation time 0 = 06:00 local (UTC)


class _SyncHass:
    """Executor stand-in running the job in the test thread."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _feed(name: str) -> tuple[TarLearningManager, dict]:
    """Manager whose history holds the signals of a reference run."""
    data, exp = port._load(name)
    m = TarLearningManager("e2e", "UTC")
    m.set_dependencies(True)
    h = m.history
    h.temp_t = [W + float(x) for x in data["pub_t"]]
    h.temp_v = [float(x) for x in data["pub_v"]]
    h.ext_t = [W + float(x) for x in data["ext_t"]]
    h.ext_v = [float(x) for x in data["ext_v"]]
    h.on = [W + float(x) for x in data["on"]]
    h.off = [W + float(x) for x in data["off"]]
    h.setpoints = [(W - 1.0, 20.0)] + [(W + t, v) for t, v in exp["sp_events"]]
    h.phases = [(W + float(t), exp["phases"][int(i)]) for t, i in zip(data["phase_t"], data["phase_i"])]
    params = []
    for i, dtc in enumerate(data["hourly_dtc"]):
        lag = None
        for th, lg in zip(data["lag_t_h"], data["lag_s"]):
            if th * 3600.0 <= i * 3600.0:
                lag = None if np.isnan(lg) else float(lg)
        params.append((W + i * 3600.0, exp["b_true"], None if np.isnan(dtc) else float(dtc), lag))
    h.params = params
    h.first_t, h.last_t = W - 1.0, W + data["t_last"]
    for (_, a), (t1, b) in zip(h.setpoints, h.setpoints[1:]):
        m._on_setpoint_change(t1, a, b)
    m._cycle_s = exp["cycle_s"]
    return m, exp


async def test_end_to_end_run_reproduces_the_reference_pipeline() -> None:
    """Daily windows, evaluations and morning steps scheduled by the manager give the reference decisions."""
    m, exp = _feed("m3_d5n3")
    hass = _SyncHass()
    for k in range(1, 12):
        await m.async_run(hass, W + k * 86400.0 + 2 * 3600.0 + 700.0)  # identification after 08:10
        await m.async_run(hass, W + k * 86400.0 + MORNING_EVAL_DELAY_S + 60.0)  # morning of day k
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
    diag = m.diagnostics(480.0, True)
    assert diag["source"] == "refined" and diag["refined"]["class"] == "mass"
    assert diag["applied_s"] == pytest.approx(m.te_pub["value"], abs=0.05)

    # Persistence across a restart: same learned values, same applied value
    restored = TarLearningManager("e2e", "UTC")
    restored.load_state(m.save_state())
    restored.set_dependencies(True)
    assert restored.applied(480.0, True) == m.applied(480.0, True)
    assert restored.evals == m.evals and restored.mornings == m.mornings
    assert restored.history.temp_v == m.history.temp_v


def _tick(m: TarLearningManager, t: float, sp: float, temp: float = 19.0) -> None:
    m.observe(t, hvac_mode=VThermHvacMode_HEAT, temp=temp, ext=2.0, setpoint=sp, phase="Stable", b=0.002,
              dtc=900.0, dtc_reliable=True, lag=300.0, cycle_min=10.0)


def test_morning_step_detection() -> None:
    m = TarLearningManager("steps", "Europe/Paris")
    day = dt.datetime(2026, 1, 6, tzinfo=dt.timezone.utc)
    at = lambda h: (day + dt.timedelta(hours=h)).timestamp()  # noqa: E731 - UTC hour, Paris = UTC + 1
    _tick(m, at(4.0), 17.0)
    _tick(m, at(4.5), 18.5)   # 05:30 local, rise 1.5 C
    _tick(m, at(4.6), 19.5)   # merged (6 min later)
    _tick(m, at(10.0), 17.0)
    _tick(m, at(10.5), 20.0)  # 11:30 local: outside the morning window
    _tick(m, at(1.0 + 24), 19.0)
    _tick(m, at(5.0 + 24), 19.5)  # rise of 0.5 C only
    assert m.pending == [{"t_sp": at(4.5), "eco": 17.0, "sp_to": 19.5}]


async def test_missing_numerical_packages_fall_back_to_bootstrap(monkeypatch) -> None:
    m, _ = _feed("pr_d5n3")
    m.fast_pub = {"value_s": 300.0, "day": 0}
    assert m.applied(480.0, True).source == "fast"

    def boom(kind, snap):
        raise ImportError("No module named 'scipy'")

    monkeypatch.setattr(manager_mod, "_run_job", boom)
    await m.async_run(_SyncHass(), W + 3 * 86400.0 + 2 * 3600.0 + 700.0)
    assert m.status == "dependencies_unavailable"
    assert m.applied(480.0, True).source == "bootstrap"
    assert m.history.is_empty()


async def test_a_failed_computation_is_skipped(monkeypatch) -> None:
    m, _ = _feed("pr_d5n3")

    def boom(kind, snap):
        raise RuntimeError("fit diverged")

    monkeypatch.setattr(manager_mod, "_run_job", boom)
    await m.async_run(_SyncHass(), W + 1 * 86400.0 + MORNING_EVAL_DELAY_S + 60.0)
    assert m.status == "active"
    assert m.mornings[-1]["why"] == "computation failed"
    assert m.last_error.endswith("fit diverged")


def test_valve_mode_is_not_supported() -> None:
    m = TarLearningManager("valve", "UTC")
    m.set_dependencies(True)
    m.observe(W, hvac_mode=VThermHvacMode_HEAT, temp=19.0, ext=2.0, setpoint=19.0, phase="Stable", b=0.002,
              dtc=None, dtc_reliable=False, lag=None, cycle_min=10.0, supported=False)
    assert not m.active
    assert m.diagnostics(480.0, True)["status"] == "valve_mode_not_supported"
    assert m.history.is_empty()


async def test_learning_store_round_trip(hass) -> None:
    m, _ = _feed("pr_d5n3")
    m.fast_pub = {"value_s": 320.0, "day": 3}
    store = SmartPITarStore(hass, "tar_store_test", TAR_SAVE_DELAY_S)
    await store.save_now(m)
    loaded = TarLearningManager("e2e", "UTC")
    loaded.load_state(await store.async_load())
    assert loaded.fast_pub == {"value_s": 320.0, "day": 3}
    assert loaded.history.on == m.history.on
    assert loaded.pending == m.pending
