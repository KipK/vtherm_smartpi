"""The T_ar learning reproduces the validated reference pipeline on simulated rooms.

Each fixture holds the signals of one simulated 12-day run (sensor publications,
heater edges, outdoor temperature, SmartPI phase and published parameters) and
the decisions of the reference pipeline: per morning (fast value, path, room
class, night indicator, gate observables), per night (regulated or not), per
identification window and per evaluation (class, tau_e, in-force values).

The reference pipeline selected its data by local hours: nights from 18:00 to
08:00, a sleep span from 23:00 to the comfort step, windows of three days ending
at 08:00. The integration selects quiet periods instead; these tests give its
numerical methods the reference periods as explicit intervals, so they check
the methods themselves against the reference.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from custom_components.vtherm_smartpi.smartpi.tar_learning import (
    gates,
    identification,
    morning,
    policy,
    room_mass,
)
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import OffsetClock
from custom_components.vtherm_smartpi.smartpi.tar_learning.signals import Anchors, ExtTL

H = 3600.0

FIXTURES = Path(__file__).parent / "fixtures" / "tar_learning"
RUNS = ("m3_d5n3", "pr_d5n3", "pr_d11n7", "m2_d9n6")
ECO = {"N3": 19.0, "N6": 17.0, "N7": 16.0}
CLOCK = OffsetClock(6.0)  # simulation time 0 is 06:00 local
# Labels of the reference pipeline -> labels of the integration
PATHS = {"v3": "night", "v3reg": "night_regulated", "R": "regulated"}


# Periods of the reference pipeline (local hours of the simulated days)
REF_NIGHTS = [(CLOCK.at(d, 18.0), CLOCK.at(d + 1, 8.0)) for d in range(-1, 15)]


def ref_sleep_start(t_sp: float) -> float:
    """23:00 of the evening before the morning of t_sp."""
    return CLOCK.at(CLOCK.day_of(t_sp) - 1, 23.0)


def ref_sleep_span(night_day: int, sp_events) -> tuple[float, float]:
    """[23:00, first rise in [03:00, 08:00) or 08:00) of the night starting on the evening of night_day."""
    lo, hi = CLOCK.at(night_day + 1, 3.0), CLOCK.at(night_day + 1, 8.0)
    rises = [te for te, old, new in gates.sp_changes_in(20.0, sp_events, lo - 1e-6, hi) if new > old and te >= lo]
    return CLOCK.at(night_day, 23.0), (rises[0] if rises else hi)


def ref_path(path: str) -> str:
    return PATHS[path]


def ref_why(why: str) -> str:
    return why.replace("no branch R setup", "no regulated-branch setup").replace("R: ", "regulated: ")


def _load(name: str):
    z = np.load(FIXTURES / f"{name}.npz")
    exp = json.loads((FIXTURES / f"{name}.json").read_text())
    pub_v = (z["pub_k"].astype(np.float64) / 100.0).astype(np.float32)
    data = {
        "pub_t": z["pub_t"].astype(np.float64), "pub_v": pub_v,
        "on": z["on"].astype(np.float64), "off": z["off"].astype(np.float64),
        "ext_t": z["ext_t"], "ext_v": z["ext_v"], "t_last": float(z["t_last"]),
        "phase_t": z["phase_t"], "phase_i": z["phase_i"], "hourly_dtc": z["hourly_dtc"],
        "lag_t_h": z["lag_t_h"], "lag_s": z["lag_s"],
    }
    return data, exp


def _inputs(data, exp) -> morning.MorningInputs:
    phases = exp["phases"]
    hourly_dtc = data["hourly_dtc"]

    def params_at(t):
        i = min(max(int(t // 3600.0), 0), len(hourly_dtc) - 1)
        dtc = None if math.isnan(hourly_dtc[i]) else float(hourly_dtc[i])
        lag = None
        for th, lg in zip(data["lag_t_h"], data["lag_s"]):
            if th * 3600.0 <= t:
                lag = None if math.isnan(lg) else float(lg)
        return dtc, lag

    def phase_at(t):
        i = int(np.searchsorted(data["phase_t"], t, side="right")) - 1
        return phases[int(data["phase_i"][max(i, 0)])]

    on, off = data["on"], data["off"]
    return morning.MorningInputs(
        anch=Anchors(data["pub_t"], data["pub_v"]), ext=ExtTL(data["ext_t"], data["ext_v"]),
        ext_t=data["ext_t"], ext_v=data["ext_v"], on=on, off=off,
        spans=room_mass.passive_spans(on, off, data["t_last"]), params_at=params_at, phase_at=phase_at,
        quiet=REF_NIGHTS, cycle_s=exp["cycle_s"], b_pub=exp["b_true"],
    )


@pytest.fixture(scope="module", params=RUNS)
def run(request):
    data, exp = _load(request.param)
    return request.param, data, exp


def test_mornings_match_reference(run) -> None:
    """Fast-path decision of every morning, its night indicator and its room class."""
    _name, data, exp = run
    inp = _inputs(data, exp)
    eco = ECO[exp["night_cell"][-2:]]
    nights: list[dict] = []
    for m in exp["mornings"]:
        out = morning.evaluate_morning(inp, m["t_sp"], eco, 20.0, nights, m["cls_in_force"], m["params_in_force"])
        night = nights[-1]
        assert night.get("cls") == m["night_cls"], m["day"]
        if m["tau1_h"] is not None:
            assert night["tau1_h"] == pytest.approx(m["tau1_h"], rel=1e-9)
            assert night["tau2_h"] == pytest.approx(m["tau2_h"], rel=1e-9)
        assert out["room_class"] == m["room_class"], m["day"]
        assert (out["path"], out["accepted"], out["T"], out["cls"], out["why"], out["ref"]) == (
            ref_path(m["path"]), m["acc"], m["T"], m["cls"], ref_why(m["why"]), m["ref"]), m["day"]


def test_gates_and_publication_match_reference(run) -> None:
    """Gate observables, G1 and GT4 decisions and the published fast value."""
    _name, data, exp = run
    for gate in ("G1", "GT4"):
        values, pubs = [], []
        for m in exp["mornings"]:
            if m["max_idle_sleep_h"] is None and m["passive_2h"] is None:
                continue
            t23 = ref_sleep_start(m["t_sp"])
            obs = gates.gate_observables(data["on"], m["t_sp"], m["t_s"], t23)
            assert obs["max_idle_h"] == pytest.approx(m["max_idle_sleep_h"], abs=1e-12)
            assert obs["regulated_h"] >= 4.0
            assert obs["passive_2h"] == m["passive_2h"]
            if not m["acc"]:
                continue
            idle = m["max_idle_sleep_h"] >= 1.0
            expected = idle if gate == "G1" else idle and (
                m["T"] < 900 or (m["b_fit_rel"] is not None and m["b_fit_rel"] < 0.6) or not m["passive_2h"])
            reason = gates.unconfirmed(gate, obs, m["T"], m["b_fit_rel"])
            assert (reason is not None) == expected, (gate, m["day"])
            if reason is None:
                values.append(m["T"])
                pub = policy.fast_publication(values)
                if pub is not None:
                    pubs.append(pub)
        assert all(p > 0 for p in pubs)


def test_regulated_nights_match_reference(run) -> None:
    """The regulated-night rule on the reference sleep spans."""
    _name, data, exp = run
    sp_events = [tuple(x) for x in exp["sp_events"]]
    for n, rec in exp["rnights"].items():
        a, b = ref_sleep_span(int(n), sp_events)
        mine = gates.regulated_span(a, b, gates.sp_at(20.0, sp_events, a), data["on"], data["off"], data["pub_t"],
                                    data["pub_v"], exp["cycle_s"], sp_timeline=(20.0, sp_events))
        why = (rec["why"] or "").replace("in the sleep span", "in the regulated part") or None
        assert (mine["ok"], mine["why"]) == (rec["ok"], why), n


def test_evaluations_and_values_in_force_match_reference(run) -> None:
    """Refined evaluations under the regulated-night policy and the class, wall and tau_e in force."""
    _name, _data, exp = run
    wins = {w["day"]: w for w in exp["windows"]}
    nights = {int(k): v for k, v in exp["rnights"].items()}
    evals = []
    for day in sorted(wins):
        if day - 3 not in wins or day < 6:
            continue
        e = identification.evaluate_day(wins[day - 3], wins[day], exp["b_true"] / 60.0)
        e["day"] = e["t"] = day
        if not gates.window_regulated(nights, list(range(day - 6, day))):
            e = identification.silence(e, "not regulated (R)")
        evals.append(e)
    for e, ref in zip(evals, exp["evals"]):
        assert (e["day"], e["cls"], e["te_ok"], e["te"]) == (ref["day"], ref["cls"], ref["te_ok"], ref["te"])
    assert len(evals) == len(exp["evals"])
    for m in exp["mornings"]:
        assert identification.class_in_force([(e["day"], e["cls"]) for e in evals], m["day"]) == m["cls_in_force"]
        assert identification.params_in_force(evals, m["day"]) == m["params_in_force"]
        assert identification.te_in_force(evals, m["day"]) == m["te_in_force"]


@pytest.mark.parametrize("name", ["m3_d5n3", "pr_d5n3"])
def test_identification_window_matches_reference(name: str) -> None:
    """Nights-only multi-day fit of the first window (class 'mass' or 'one', tau_e, k, tau_w)."""
    data, exp = _load(name)
    d = identification.grid_data(data["pub_t"], data["pub_v"], data["on"], data["off"], data["ext_t"],
                                 data["ext_v"], 0.0, data["t_last"])
    wins = {w["day"]: w for w in exp["windows"]}
    for day in exp["window_days_fit"]:
        ref = wins[day]
        t0, t1 = CLOCK.at(day - 3, 8.0), CLOCK.at(day, 8.0)
        nights = [(a, b) for a, b in REF_NIGHTS if a >= t0 and b <= t1]
        j = int(np.searchsorted(data["pub_t"], t1, side="right"))
        mine = identification.window_fit(d, t0, t1, nights, identification.q_obs(data["pub_v"][:j]))
        assert mine["cls"] == ref["cls"]
        assert mine["q_obs"] == pytest.approx(ref["q_obs"], rel=1e-12)
        assert mine["dbic"] == pytest.approx(ref["dbic"], rel=1e-9)
        for pk in ("p1", "p2"):
            for k in ("a", "b", "k", "tau_w", "tau_e"):
                a, b = (mine.get(pk) or {}).get(k), (ref.get(pk) or {}).get(k)
                if b is None:
                    assert a is None
                else:
                    assert a == pytest.approx(b, rel=1e-9, abs=1e-15), (pk, k)
