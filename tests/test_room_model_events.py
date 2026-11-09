"""Room model observer through an open window and through power shedding."""

from __future__ import annotations

import json

import numpy as np
import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.room_model import observer as observer_mod
from custom_components.vtherm_smartpi.smartpi.room_model.const import RM_WALL_FADE_S
from custom_components.vtherm_smartpi.smartpi.room_model.feedforward import (
    SOURCE_STATIC,
    SOURCE_WALLS,
    HoldingTransfer,
    select_source,
)
from custom_components.vtherm_smartpi.smartpi.room_model.manager import RoomModelManager
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams

H = 3600.0

# Heavy room with slow walls (air capacity 2 MJ/K, walls 20 MJ/K, air-wall 150 W/K, air-outdoor 12 W/K,
# wall-outdoor 10 W/K, heater 1500 W, emitter first order 300 s).
_CA, _CM, _KAM, _KAO, _KMO = 2e6, 2e7, 150.0, 12.0, 10.0
HEAVY = {"a": 1500.0 / _CA, "alpha": (_KAM + _KAO) / _CA, "beta": (_KAM + _KMO) / _CM,
         "c": _KAM ** 2 / (_CA * (_KAM + _KMO)), "tau_e": 300.0, "L": 0.0}
# A wide-open window: heater off and 150 W/K more air loss for 2 h (the air falls from 19.9 to 14.3 C).
W0, W1 = rsim.T0 + 34.0 * H, rsim.T0 + 36.0 * H
WINDOW_LOSS = 150.0 / _CA


def _params(p: dict) -> RoomParams:
    return RoomParams("full", p["a"], p["alpha"], p["beta"], p["c"], p["tau_e"], p["L"])


def _manager(p: dict) -> RoomModelManager:
    m = RoomModelManager("room", "UTC", None)
    m.params, m.q = _params(p), 0.01
    m.observer.configure(m.params, 0.01)
    return m


def _wall_error(m: RoomModelManager, p: dict, truth, t: float) -> float:
    """Wall extra power of the observer minus the true one (fraction of full power)."""
    tt, ta, tw = truth
    j = min(int(np.searchsorted(tt, t)), len(tt) - 1)
    true = (p["c"] * (ta[j] - rsim.outdoor(tt[j])) - tw[j]) / p["a"]
    return m.observer.wall_extra() - true


def _run_window(data: dict, reason: str, t_from: float, w_end: float, t_to: float, on_tick=None):
    """Feed the manager like the regulation; reason is the exclusion given while the window is open.

    on_tick(m, t, mode) runs after each observation.
    """
    m = _manager(HEAVY)
    log = []

    def feed(t, kind, pl):
        win = W0 <= t < w_end
        mode = VThermHvacMode_OFF if win else VThermHvacMode_HEAT  # VT turns the heater off
        if kind == "cycle":
            m.on_applied(t, pl, True, 10.0, mode)
            return
        y, yt, te = pl
        m.observe(t, hvac_mode=mode, temp=y, measure_id=yt, ext=te, excluded=reason if win else None,
                  a_per_min=HEAVY["a"] * 60.0, first_moment_s=HEAVY["tau_e"])
        if on_tick is not None:
            on_tick(m, t, mode)
        if m.observer.x is not None:
            diag = m.diagnostics()
            log.append((t, _wall_error(m, HEAVY, data["truth"], t), diag["tracking"],
                        diag["wall_extra_power_percent"], m.observer.innov_n))

    rsim.replay(data, feed, t_from, t_to)
    return m, log


@pytest.fixture(scope="module")
def window_data():
    return rsim.simulate(HEAVY, 3.0, window=(W0, W1, WINDOW_LOSS))


def _max_err(log, a: float, b: float) -> float:
    return max(abs(e) for t, e, *_ in log if a <= t < b)


def test_wall_state_survives_an_open_window(window_data) -> None:
    m, log = _run_window(window_data, "window open", rsim.T0 + 600.0, W1, rsim.T0 + 3 * 86400.0)
    before = _max_err(log, W0 - 12 * H, W0)
    after = _max_err(log, W1, W1 + 24 * H)
    # The walls follow the measured air through the window: after it, the wall term is as close to the
    # truth as it was before (measured 0.0011 after, 0.0010 before, in fraction of full power).
    assert after <= 2.0 * before
    assert after < 0.003
    # Health: paused while open, then ok at once with the wall term shown (no hold)
    during = {s for t, _, s, *_ in log if W0 + 60.0 <= t < W1}
    assert during == {"paused (SmartPI not heating or not in control)"}
    later = [(s, wx, n) for t, _, s, wx, n in log if W1 <= t < W1 + 6 * H]
    assert {s for s, *_ in later} == {"ok"}
    assert all(isinstance(wx, float) for _, wx, _ in later)
    # The innovation statistics start over at the resume
    n_before = [n for t, *_, n in log if W0 - 120.0 <= t < W0]
    n_resume = [n for t, *_, n in log if W1 <= t < W1 + 120.0]
    assert min(n_before) > 20 and min(n_resume) <= 1
    assert m.observer.restarts == 1


def test_wall_part_fades_back_in_over_30_minutes_after_a_window(window_data) -> None:
    """The holding power drops its wall part while the window is open and blends it back in over
    RM_WALL_FADE_S from the resume, following the live wall term of the tracking."""
    tr = HoldingTransfer()
    seen = []

    def on_tick(m, t, mode):
        m.eb_slope = (HEAVY["alpha"] - HEAVY["c"]) / HEAVY["a"]
        src = select_source(m, VThermHvacMode_HEAT, False)
        tr.update(src, now=t, learned_power=0.3, target_temp=20.0, ext_temp=rsim.outdoor(t), heat=True)
        seen.append((t, src.kind, src.wall, tr.wall_applied))

    _run_window(window_data, "window open", W0 - 6 * H, W1, W1 + 2 * H, on_tick=on_tick)
    # Open: static part only, the wall part fades out over RM_WALL_FADE_S and stays at zero
    assert {k for t, k, *_ in seen if W0 + 60.0 <= t < W1} == {SOURCE_STATIC}
    assert all(w == 0.0 for t, *_, w in seen if W0 + RM_WALL_FADE_S + 60.0 <= t < W1)
    # Resumed: the walls at once, their part from 0 to the live wall term over RM_WALL_FADE_S
    after = [(t, k, live, w) for t, k, live, w in seen if t >= W1]
    assert {k for _, k, *_ in after} == {SOURCE_WALLS}
    t_res = after[0][0]
    assert t_res < W1 + 120.0 and after[0][3] == 0.0
    for t, _, live, w in after:
        frac = min((t - t_res) / RM_WALL_FADE_S, 1.0)
        assert w == pytest.approx(frac * live, abs=1e-12)
    assert after[-1][3] == after[-1][2] and abs(after[-1][2]) > 0.01


def test_a_plain_pause_lets_the_window_leak_into_the_walls(window_data) -> None:
    """Guard: the same window seen as a plain pause (open loop, wall correlations kept) drifts.

    Measured: largest error 0.013 after the window and a median of -0.005 over 18-40 h after it,
    against 0.0011 with the window handling (and 0.0010 before the window).
    """
    _, log = _run_window(window_data, "safety mode", rsim.T0 + 600.0, W1, rsim.T0 + 3 * 86400.0)
    after = _max_err(log, W1, W1 + 24 * H)
    assert after > 0.008
    late = float(np.median([e for t, e, *_ in log if W1 + 18 * H <= t < W1 + 40 * H]))
    assert late < -0.003


def test_long_window_restarts_at_the_wall_equilibrium(window_data) -> None:
    m, log = _run_window(window_data, "window open", W0 - 4 * H, W0 + 3.5 * H, W0 + 3.5 * H + 600.0)
    assert m.observer.restarts == 2
    assert log[-1][2] == "settling after a restart"


def test_window_state_is_saved_and_restored(window_data) -> None:
    m, _ = _run_window(window_data, "window open", W0 - 4 * H, W1, W1 - H)
    assert m.observer.health() == observer_mod.HEALTH_PAUSED and m.observer.pause_window
    m2 = RoomModelManager("room", "UTC", None)
    state = json.loads(json.dumps(m.save_state()))
    m2.load_state(state)
    assert m2.observer.pause_window and m2.observer.paused_since == m.observer.paused_since
    assert m2.observer.health() == observer_mod.HEALTH_PAUSED


# ---------------------------------------------------------------------------
# Power shedding
# ---------------------------------------------------------------------------
# Shedding from 06:01 on the second day, one minute into a full-power cycle, for 1 h.
S0, S1 = rsim.T0 + 30.0 * H + 60.0, rsim.T0 + 31.0 * H + 60.0


def test_power_shedding_keeps_tracking_with_the_heater_off(monkeypatch) -> None:
    # The plant: heater off over the shedding, no extra loss
    data = rsim.simulate(HEAVY, 1.5, window=(S0, S1, 0.0))
    cyc = [c for c in data["cycles"] if c[0] < S0][-1]
    assert S0 - cyc[0] == 60.0 and cyc[1] > 0.5  # the shedding cuts a heating cycle
    m = _manager(HEAVY)
    reopened = []
    monkeypatch.setattr(m.observer, "_reopen", lambda: reopened.append(m.observer.t))
    seen = {}

    def feed(t, kind, pl):
        shed = S0 <= t < S1
        if kind == "cycle":
            # the cycles started during the shedding carry a zero command; one is sent with power to
            # check that the timeline stays at zero anyway
            m.on_applied(t, 0.6 if shed else pl, True, 10.0, VThermHvacMode_HEAT)
            return
        y, yt, te = pl
        if t == S0:
            seen["innov_n"] = m.observer.innov_n
            seen["restarts"] = m.observer.restarts
        m.observe(t, hvac_mode=VThermHvacMode_HEAT, temp=y, measure_id=yt, ext=te,
                  excluded="power shedding" if shed else None, a_per_min=HEAVY["a"] * 60.0,
                  first_moment_s=HEAVY["tau_e"])
        if shed:
            assert m.observer.paused_since is None
            assert m.observer.timeline.mean(S0, t) == 0.0
        if t == S1 - 60.0:
            seen["innov_end"] = m.observer.innov_n
            tt, ta, _ = data["truth"]
            j = int(np.searchsorted(tt, t))
            seen["air_err"] = abs(m.observer.x[0] - ta[j])
            seen["health"] = m.observer.health()
        if t == S1 + 1200.0:
            seen["power_after"] = m.observer.timeline.power

    rsim.replay(data, feed, rsim.T0 + 600.0, S1 + 1800.0)
    tl = m.observer.timeline
    # The rest of the interrupted cycle is not counted as heating
    assert tl.mean(S0, S1) == 0.0
    # Corrections go on at each new sensor value (12 over the hour, the heavy room cools slowly), with
    # no pause and no reopening of the uncertainty
    assert seen["innov_end"] - seen["innov_n"] >= 8
    assert seen["restarts"] == m.observer.restarts
    assert reopened == []
    assert seen["air_err"] < 0.02
    assert seen["health"] == observer_mod.HEALTH_OK
    # Back in control, the next command is recorded again
    assert seen["power_after"] > 0.0
