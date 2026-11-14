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


# A home at 60 N on the Greenwich meridian: in January the simulated nights (setback at 18:00 UTC, comfort step
# at 06:00 UTC, response until 08:00 UTC) lie in the dark (sunset before 15:00 UTC, sunrise after 08:30 UTC).
LAT, LON = 60.0, 0.0


def _feed(name: str, position: bool = True) -> tuple[TarLearningManager, dict]:
    """Manager whose history holds the signals of a reference run."""
    data, exp = port._load(name)
    m = TarLearningManager("e2e", "UTC")
    m.set_dependencies(True)
    if position:
        m.set_position(LAT, LON)
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


async def _run_days(m: TarLearningManager, days: int = 12) -> None:
    hass = _SyncHass()
    for k in range(1, days):
        await m.async_run(hass, W + k * 86400.0 + 2 * 3600.0 + 700.0)
        await m.async_run(hass, W + k * 86400.0 + MORNING_EVAL_DELAY_S + 60.0)


@pytest.mark.parametrize("name", ["m3_d5n3", "pr_d5n3", "m2_d9n6"])
async def test_end_to_end_run_on_quiet_periods(name: str) -> None:
    """Quiet periods, comfort steps, windows and evaluations scheduled by the manager.

    The periods run from the setback (18:00) to the comfort step (06:00); an identification window keeps
    each period with the 2 h response to its step, still in the dark (06:00 to 08:00). On these simulated
    rooms that is the data of the reference nights (18:00 to 08:00), so the step decisions and the
    evaluations are those of the reference.
    """
    m, exp = _feed(name)
    await _run_days(m)
    base = m._clock.day_of(W)
    # One period per night, from the setback to the comfort step, keyed by its events
    assert len(m.episodes) == 11
    for k, ep in enumerate(m.episodes):
        assert ep["quiet"] == [W + (12 + 24 * k) * 3600.0, W + (24 + 24 * k) * 3600.0]
        assert ep["ident"] == [W + (12 + 24 * k) * 3600.0, W + (26 + 24 * k) * 3600.0]
        assert ep["id"] == f"q1:{ep['quiet'][0]:.1f}:{ep['quiet'][0]:.1f}"
    ref = {x["day"]: x for x in exp["mornings"]}
    assert len(m.mornings) == len(ref)
    for rec in m.mornings:
        r = ref[rec["day"] - base]
        assert (rec["path"], rec["accepted"], rec["T"], rec["cls"], rec["why"]) == (
            port.ref_path(r["path"]), r["acc"], r["T"], r["cls"], port.ref_why(r["why"]))
    mine = m.evals
    assert [(e["day"] - base, e["cls"]) for e in mine] == [(e["day"], e["cls"]) for e in exp["evals"]]
    for e, r in zip(mine, exp["evals"]):
        assert (e["te"] is None) == (r["te"] is None)
        if r["te"] is not None:
            assert e["te"] == pytest.approx(r["te"], rel=1e-3)
    # Each evaluation compares two windows of three disjoint periods, the second ending 3 periods later
    ids = [ep["id"] for ep in m.episodes]
    for e in m.evals:
        w2 = m.windows[e["window"]]
        w1 = m.windows[w2["prev"]]
        assert not set(w1["nights"]) & set(w2["nights"])
        assert ids.index(w2["nights"][0]) == ids.index(w1["nights"][-1]) + 1
    diag = m.diagnostics(480.0, True)
    assert diag["quiet_periods"]["reason"] is None and diag["quiet_periods"]["count"] == 11
    assert diag["quiet_periods"]["last"]["start"].startswith("2026-01-15T18:00")
    assert "gate" not in diag and "path" not in diag["fast"]["last_morning"]

    # Persistence across a restart: same learned values, same applied value, nothing evaluated again
    restored = TarLearningManager("e2e", "UTC")
    restored.load_state(m.save_state())
    restored.set_position(LAT, LON)
    restored.set_dependencies(True)
    assert restored.applied(480.0, True) == m.applied(480.0, True)
    assert restored.evals == m.evals and restored.mornings == m.mornings and restored.episodes == m.episodes
    assert restored.history.temp_v == m.history.temp_v
    n_eval, n_morn = len(restored.evals), len(restored.mornings)
    await _run_days(restored)
    assert len(restored.evals) == n_eval and len(restored.mornings) == n_morn
    assert restored.fast_count == m.fast_count


async def test_splitting_the_run_keeps_the_keys() -> None:
    """Building the catalogue at every hour or once at the end gives the same periods."""
    m, _ = _feed("pr_d5n3")
    once = TarLearningManager("e2e", "UTC")
    once.history = m.history
    once.set_position(LAT, LON)
    for k in range(1, 12 * 24):
        m._refresh(W + k * 3600.0, force=True)
    once._refresh(W + 12 * 86400.0, force=True)
    assert [e["id"] for e in m.episodes] == [e["id"] for e in once.episodes]


async def test_a_step_counts_once_and_dedups_the_night() -> None:
    """A night evaluated twice (same period key) keeps one night entry and one confirmed value."""
    m, _ = _feed("m2_d9n6")
    await _run_days(m)
    ids = [n["night_id"] for n in m.nights]
    assert len(ids) == len(set(ids))
    count, values = m.fast_count, list(m.fast_values)
    last = m.mornings[-1]
    res = {"night_id": last["night_id"], "accepted": True, "T": 310.0, "b_fit_rel": None,
           "gate_obs": last["gate_obs"], "night_summary": dict(m.nights[-1]), "t_sp": last["t_sp"]}
    m._apply_morning({"t_sp": last["t_sp"]}, res)
    assert m.fast_count == count
    assert len(m.fast_values) == len(values) and m.fast_values[-1] == [last["night_id"], 310.0]
    assert [n["night_id"] for n in m.nights].count(last["night_id"]) == 1


async def test_without_position_nothing_is_learned() -> None:
    m, _ = _feed("m2_d9n6", position=False)
    await _run_days(m)
    assert m.episodes == [] and m.evals == [] and m.fast_pub is None
    assert {r["why"] for r in m.mornings} == {"home position unknown"}
    diag = m.diagnostics(480.0, True)
    assert diag["quiet_periods"]["reason"] == "home position unknown: the absence of sun cannot be checked"
    assert diag["fast"]["last_morning"]["reason"] == diag["quiet_periods"]["reason"]


def test_position_is_validated() -> None:
    m = TarLearningManager("pos", "UTC")
    for lat, lon in ((None, None), (91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0), ("48.8", 2.3), (True, 2.0)):
        m.set_position(lat, lon)
        assert m.position is None
    m.set_position(0.0, 0.0)  # zero is a valid coordinate
    assert m.position is not None


async def test_a_new_position_forgets_the_periods() -> None:
    m, _ = _feed("m2_d9n6")
    await _run_days(m)
    assert m.episodes and m.fast_pub
    state = m.save_state()
    moved = TarLearningManager("e2e", "UTC")
    moved.load_state(state)
    moved.set_position(LAT + 1.0, LON)
    assert moved.episodes == [] and moved.fast_pub is None and moved.evals == []
    assert moved.history.temp_v == m.history.temp_v
    same = TarLearningManager("e2e", "UTC")
    same.set_position(LAT, LON)
    same.load_state(state)
    assert same.episodes == m.episodes and same.fast_pub == m.fast_pub


def test_state_of_the_fixed_hour_selection_keeps_only_raw_data() -> None:
    m, _ = _feed("pr_d5n3")
    old = {"version": 1, "history": m.history.to_dict(), "pending": [{"t_sp": W + 3600.0, "eco": 17.0, "sp_to": 19.0}],
           "nights": [{"cls": "mass", "len_h": 7.0}], "mornings": [{"day": 1, "why": "accepted"}],
           "windows": {"739000": {"cls": "mass"}}, "rnights": {"739000": {"ok": True}}, "evals": [{"day": 1}],
           "fast_values": [300.0, 310.0, 320.0], "fast_count": 3, "fast_pub": {"value_s": 310.0, "day": 1},
           "te_pub": {"value": 300.0}, "refined_cls": "mass", "refined_wall": {"k_per_s": 1e-4, "tau_w_s": 3e4}}
    loaded = TarLearningManager("old", "UTC")
    loaded.load_state(old)
    assert loaded.history.temp_v == m.history.temp_v
    assert loaded.pending == [{"t_sp": W + 3600.0, "eco": 17.0, "sp_to": 19.0}]
    assert (loaded.nights, loaded.mornings, loaded.windows, loaded.rnights, loaded.evals) == ([], [], {}, {}, [])
    assert (loaded.fast_values, loaded.fast_count, loaded.fast_pub, loaded.te_pub) == ([], 0, None, None)
    assert loaded.refined_cls == "unknown" and loaded.refined_wall is None
    # A later format is ignored, a malformed one too
    future = TarLearningManager("new", "UTC")
    future.load_state({"version": 99, "history": m.history.to_dict()})
    assert future.history.is_empty()
    bad = TarLearningManager("bad", "UTC")
    bad.load_state(dict(m.save_state(), fast_values=[["k", float("nan")]]))
    assert bad.history.is_empty() and bad.fast_values == []


def _tick(m: TarLearningManager, t: float, sp: float, temp: float = 16.5) -> None:
    m.observe(t, hvac_mode=VThermHvacMode_HEAT, temp=temp, ext=2.0, setpoint=sp, phase="Stable", b=0.002,
              dtc=900.0, dtc_reliable=True, lag=300.0, cycle_min=10.0)


def test_comfort_step_detection() -> None:
    """A rise of at least 1 C at any hour after a low setpoint held 2 h; restorations are not steps."""
    m = TarLearningManager("steps", "Europe/Paris")
    day = dt.datetime(2026, 1, 6, tzinfo=dt.timezone.utc)
    at = lambda h: (day + dt.timedelta(hours=h)).timestamp()  # noqa: E731 - UTC hour
    _tick(m, at(1.0), 17.0, 17.0)
    _tick(m, at(4.5), 18.5, 17.0)   # rise 1.5 C after 3.5 h at 17 C
    _tick(m, at(4.6), 19.5, 17.0)   # merged (6 min later)
    _tick(m, at(10.0), 17.0, 17.0)
    _tick(m, at(13.0), 20.0, 17.0)  # an afternoon rise is a step too (the sun is checked later)
    _tick(m, at(13.5), 17.0, 17.0)
    _tick(m, at(14.5), 19.0, 17.0)  # low setpoint held 1 h only: not a step
    _tick(m, at(25.0), 19.0, 17.0)
    _tick(m, at(29.0), 19.5, 17.0)  # rise of 0.5 C only
    _tick(m, at(31.0), 16.0, 17.5)
    _tick(m, at(34.0), 17.0, 17.4)  # room still above the new setpoint: no heat called, not a step
    assert m.pending == [{"t_sp": round(at(4.5), 1), "eco": 17.0, "sp_to": 19.5},
                         {"t_sp": round(at(13.0), 1), "eco": 17.0, "sp_to": 20.0}]


@pytest.mark.parametrize("sequence", [((120.0, 7.0), (125.0, 18.0), (130.0, 20.0)), ((120.0, 18.0), (190.0, 20.0))])
def test_restoration_after_a_restart_is_not_a_step(sequence) -> None:
    """After a restart the setpoint is restored in steps (through 7 C, or straight to comfort): no step, even
    after a long low setpoint, whether the restart is bridged or not."""
    for bridged in (True, False):
        m = TarLearningManager("restart", "UTC")
        t0 = dt.datetime(2026, 1, 6, tzinfo=dt.timezone.utc).timestamp()
        t = t0
        while t < t0 + 5 * 3600.0:
            _tick(m, t, 16.0)
            t += 60.0
        m.history = type(m.history).from_dict(m.history.to_dict(t))
        m.on_restart(bridged, t + 120.0)
        for dt_s, sp in sequence:
            _tick(m, t + dt_s, sp)
        _tick(m, t + 900.0, 20.0)
        assert m.history.restarts and m.pending == []
    # The same rise without a restart is a step
    m = TarLearningManager("no restart", "UTC")
    t = t0
    while t < t0 + 5 * 3600.0:
        _tick(m, t, 16.0)
        t += 60.0
    _tick(m, t + 120.0, 18.0)
    assert len(m.pending) == 1


async def test_missing_numerical_packages_fall_back_to_bootstrap(monkeypatch) -> None:
    m, _ = _feed("pr_d5n3")
    m.fast_pub = {"value_s": 1500.0, "day": 0}
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
    assert m.last_error == "the calculation failed, see the log"


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


async def test_response_windows_stop_at_sunrise() -> None:
    """The fit window ends at the end of the darkness; a cut window gives a value only when it holds three
    times the largest of its fit and the longest known delay, else at most a lower bound."""
    cut, bound = "excitation: cut by sunrise", "excitation: cut by sunrise, lower bound"
    # 35 E: the darkness ends about 30 min after the 06:00 UTC step. A 300 s emitter whose bootstrap dead time
    # is 150 s (first moment about 300 s) is still measured.
    m, _ = _feed("m2_d9n6", position=False)
    m.set_position(LAT, 35.0)
    m.bootstrap_heat_s = 150.0
    await _run_days(m)
    assert all(r["accepted"] for r in m.mornings[:9])
    # Once the fast value is published (about 340 s) the shorter windows of the later mornings do not hold
    # three times it: lower bounds below the true 300 s
    assert m.fast_pub is not None
    assert all(r["accepted"] or (r["why"] == bound and r["bound_s"] < 300.0) for r in m.mornings)
    # Same cut, but the bootstrap says the emitter is slow (first moment about 2300 s): 30 min cannot hold it
    m, _ = _feed("m2_d9n6", position=False)
    m.set_position(LAT, 35.0)
    m.bootstrap_heat_s = 1150.0
    await _run_days(m)
    assert not any(r["accepted"] for r in m.mornings)
    assert {r["why"] for r in m.mornings} <= {cut, bound}
    # Cut late (20 E: about 1 h 40 left), still under three times the slow reference: lower bounds only,
    # 0.6 times a fit near the true 300 s, never a value
    m, _ = _feed("m2_d9n6", position=False)
    m.set_position(LAT, 20.0)
    m.bootstrap_heat_s = 1150.0
    await _run_days(m)
    assert {r["why"] for r in m.mornings} == {bound}
    assert all(100.0 < r["bound_s"] < 300.0 for r in m.mornings)
    assert m.fast_values == [] and m.fast_pub is None
    # Without any reference, the 30 min window holds three times the fitted value: an estimate
    m, _ = _feed("m2_d9n6", position=False)
    m.set_position(LAT, 35.0)
    await _run_days(m)
    assert all(r["accepted"] for r in m.mornings[:9])
    # 40 E: about 10 min left, then none: refused, never fitted on sunlit data
    m, _ = _feed("m2_d9n6", position=False)
    m.set_position(LAT, 40.0)
    m.bootstrap_heat_s = 150.0
    await _run_days(m)
    whys = {r["why"] for r in m.mornings}
    assert not any(r["accepted"] for r in m.mornings)
    assert whys <= {"excitation: energy", "excitation: window", cut, bound,
                    "sunrise too close to the comfort step", "comfort step in daylight"}
    assert "comfort step in daylight" in whys


async def test_keys_survive_pruning_and_do_not_depend_on_the_time_zone() -> None:
    m, _ = _feed("pr_d5n3")
    await _run_days(m)
    ids = [e["id"] for e in m.episodes]
    paris = TarLearningManager("e2e", "Europe/Paris")
    paris.history = m.history
    paris.set_position(LAT, LON)
    paris._refresh(W + 12 * 86400.0, force=True)
    assert [e["id"] for e in paris.episodes] == ids
    # Pruning the history in the middle of the stored periods neither changes nor duplicates them
    m.history.prune(W + 12 * 86400.0 - 3 * 86400.0 + 3600.0)
    m._refresh(W + 12 * 86400.0 + 7200.0, force=True)
    assert [e["id"] for e in m.episodes] == ids


class _RecordingHass(_SyncHass):
    def __init__(self) -> None:
        self.funcs: list = []

    async def async_add_executor_job(self, func, *args):
        self.funcs.append(func)
        return func(*args)


async def test_the_sun_is_scanned_in_the_executor_once_a_day() -> None:
    m, _ = _feed("pr_d5n3")
    hass = _RecordingHass()
    now = W + 1 * 86400.0 + MORNING_EVAL_DELAY_S + 60.0
    await m.async_run(hass, now)
    await m.async_run(hass, now + 600.0)
    assert hass.funcs.count(manager_mod.dark_intervals) == 1
    await m.async_run(hass, now + 86400.0)
    assert hass.funcs.count(manager_mod.dark_intervals) == 2


async def test_a_stopped_manager_runs_nothing() -> None:
    m, _ = _feed("pr_d5n3")
    hass = _RecordingHass()
    m.stop()
    await m.async_run(hass, W + 1 * 86400.0 + MORNING_EVAL_DELAY_S + 60.0)
    assert hass.funcs == []
