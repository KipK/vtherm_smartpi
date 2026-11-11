"""Room model identification: fits on synthetic rooms, publication rules, energy balance, fit periods,
information of a fit."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.smartpi.room_model import gates, jobs, public
from custom_components.vtherm_smartpi.smartpi.room_model import energy_balance
from custom_components.vtherm_smartpi.smartpi.room_model import identification
from custom_components.vtherm_smartpi.smartpi.room_model.const import (
    RM_AGREE_TOL,
    RM_EMITTER_CHI2,
    RM_EMITTER_GRID_S,
    RM_INFO_Z,
)
from custom_components.vtherm_smartpi.smartpi.room_model.energy_balance import heater_timeline
from custom_components.vtherm_smartpi.smartpi.room_model.periods import fit_periods, windows
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock

D0 = rsim.day_of(rsim.T0)
H = 3600.0
DAY = 86400.0


def _slim(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("day", "why", "q", "precise", "full", "reduced")}


def _two_windows(data: dict, plant: dict, days=(4, 7)) -> tuple[dict, dict, float]:
    """Job records of two disjoint windows (the second checked against the first) and the longest job time."""
    prev = None
    recs = []
    worst = 0.0
    for d in days:
        snap = rsim.snapshot(data, D0 + d, prev=prev, tau_e=plant["tau_e"], L=plant["L"], a_per_min=plant["a"] * 60.0)
        t = time.perf_counter()
        rec = jobs.identify_job(snap)
        worst = max(worst, time.perf_counter() - t)
        recs.append(rec)
        prev = _slim(rec)
    return recs[0], recs[1], worst


@pytest.fixture(scope="module")
def mass_data():
    return rsim.simulate(rsim.MASS, 8.5)


@pytest.fixture(scope="module")
def no_mass_data():
    return rsim.simulate(rsim.NO_MASS, 8.5)


def test_full_model_identified_on_a_mass_room(mass_data) -> None:
    p = rsim.MASS
    first, second, worst = _two_windows(mass_data, p)
    assert first["decision"] == {"why": gates.WHY_NO_PREVIOUS, "wall": gates.WALL_NOT_CONFIRMED}
    # The two windows are three quiet periods each, disjoint, the second compared with the first
    assert second["prev_id"] == first["id"] and not set(first["nights"]) & set(second["nights"])
    assert first["fitted"] == first["nights"] and len(first["nights"]) == 3
    # The energy balance of the whole days before the day: the walls still release the heat of the start of the
    # simulation (wall time 80 h), the heating need drifts by several % from day to day, and the balance is
    # refused; its slope (the outdoor temperature repeats every day: the slope alone) is a few % high
    for d in (4, 7):
        eb = jobs.balance_job(rsim.snapshot(mass_data, D0 + d, a_per_min=p["a"] * 60.0))["eb"]
        assert eb["reason"] == energy_balance.REASON_DRIFT and eb["value"] is None
    assert eb["slope"] == pytest.approx((p["alpha"] - p["c"]) / p["a"], rel=0.06)
    early = jobs.balance_job(rsim.snapshot(mass_data, D0 + 3, a_per_min=p["a"] * 60.0))
    assert early["day"] == D0 + 3 and early["eb"]["reason"] == "history" and early["eb"]["value"] is None
    # Each fit carries the precision of its identifiable coordinates, here within the tolerances
    info = second["full"]["info"]
    assert gates.info_ok(info, "full", second["full"]["params"]) and info["sd"]["a"] < 0.01
    dec = second["decision"]
    assert dec["struct"] == "full"
    c = dec["candidate"]
    for k in ("a", "alpha", "c"):
        assert c[k] == pytest.approx(p[k], rel=0.03)
    assert c["alpha"] - c["c"] == pytest.approx(p["alpha"] - p["c"], rel=0.05)
    assert c["beta"] == pytest.approx(p["beta"], rel=0.2)
    # The emitter search finds the first moment of the plant (600 s lag + 120 s delay) on its fixed grid
    assert c["tau_e"] + c["L"] == pytest.approx(p["tau_e"] + p["L"], rel=0.08)
    assert gates.residuals_ok(second["held"]["full"], "precise", held_out=True, q=0.01)
    # The reduced model cannot follow a mass room
    assert second["reduced"]["ok"] is False
    assert worst < 120.0  # up to 23 extra full fits search the emitter


def test_reduced_model_on_a_room_without_mass(no_mass_data) -> None:
    p = rsim.NO_MASS
    _, second, _ = _two_windows(no_mass_data, p)
    dec = second["decision"]
    assert dec["struct"] == "reduced"
    assert dec["wall"] == gates.WALL_SMALL
    assert dec["candidate"]["a"] == pytest.approx(p["a"], rel=0.02)
    assert dec["candidate"]["alpha"] == pytest.approx(p["alpha"], rel=0.02)
    # A full fit, if it passes its checks, finds no wall share
    full = second["full"]
    assert not full["ok"] or full["params"]["c"] / full["params"]["alpha"] < 0.1


def test_coarse_sensor_gives_no_wall() -> None:
    p = rsim.NO_MASS
    data = rsim.simulate(p, 8.5, q=0.1)
    _, second, _ = _two_windows(data, p)
    assert second["precise"] is False and "full" not in second
    assert second["decision"]["struct"] == "reduced"
    assert second["decision"]["wall"] == gates.WALL_COARSE
    assert second["decision"]["candidate"]["alpha"] == pytest.approx(p["alpha"], rel=0.03)


def test_refusals_before_the_fit(mass_data) -> None:
    p = rsim.MASS
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=None, L=None)
    assert jobs.identify_job(snap)["decision"]["why"] == gates.WHY_EMITTER
    # The recorded data no longer cover the window
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=p["tau_e"], L=p["L"])
    late = dict(snap, first_t=snap["window"]["intervals"][0][0] + H)
    assert jobs.identify_job(late)["decision"]["why"] == gates.WHY_HISTORY
    keys = snap["window"]["nights"]
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=p["tau_e"], L=p["L"], excluded=keys + ["q1:0.0:0.0"])
    rec = jobs.identify_job(snap)
    assert rec["decision"]["why"] == gates.WHY_DISTURBED
    assert rec["excluded_nights"] == keys
    # One disturbed period is left out, the others are fitted
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=p["tau_e"], L=p["L"], excluded=[keys[1]])
    rec = jobs.identify_job(snap)
    assert rec["fitted"] == [keys[0], keys[2]] and rec["nights"] == keys


def test_no_heating_is_refused(mass_data) -> None:
    data = dict(mass_data, on=[], off=[])
    p = rsim.MASS
    rec = jobs.identify_job(rsim.snapshot(data, D0 + 4, tau_e=p["tau_e"], L=p["L"]))
    assert rec["decision"]["why"] == gates.WHY_HEAT


# ---------------------------------------------------------------------------
# Publication rules
# ---------------------------------------------------------------------------
_INFO = {"sd": {"a": 0.01, "b_eq_a": 0.01, "alpha": 0.01, "c": 0.01}, "tau": 1.0}
_GOOD = {"n": 200, "mean": 0.001, "rms": 0.02, "runs_z": -1.0, "ac1h": 0.1, "corr_u": 0.05, "corr_sp": 0.0,
         "corr_te": 0.1}


def test_residual_checks() -> None:
    assert gates.residuals_ok(_GOOD, "precise")
    assert not gates.residuals_ok(dict(_GOOD, mean=0.05), "precise")
    assert not gates.residuals_ok(dict(_GOOD, rms=0.2), "precise")
    assert not gates.residuals_ok(dict(_GOOD, runs_z=-6.0), "precise")
    assert not gates.residuals_ok(dict(_GOOD, ac1h=0.7), "precise")
    assert not gates.residuals_ok(dict(_GOOD, corr_u=0.5), "precise")
    # A structure below the sensor resolution is not a model error
    assert gates.residuals_ok(dict(_GOOD, rms=0.01, runs_z=-8.0, ac1h=0.6), "precise", q=0.01)
    assert not gates.residuals_ok(dict(_GOOD, rms=0.03, runs_z=-8.0), "precise", q=0.01)
    assert gates.residuals_ok(dict(_GOOD, rms=0.04, runs_z=-8.0), "coarse", q=0.1)
    # Held-out data get wider bounds on bias and spread
    assert not gates.residuals_ok(dict(_GOOD, rms=0.1), "precise")
    assert gates.residuals_ok(dict(_GOOD, rms=0.1, runs_z=-1.0), "precise", held_out=True)
    assert not gates.residuals_ok(None, "precise")


def test_runs_test_is_taken_on_hourly_means() -> None:
    """Ripple shorter than an hour makes long runs of 10 min residuals but not of their hourly means; a slow
    structured misfit makes long runs of both."""
    rng = np.random.default_rng(3)
    n = 6 * 14
    segs_fast, segs_slow = [], []
    for _ in range(3):
        e = rng.normal(0.0, 0.02, n + 3)
        segs_fast.append(np.convolve(e, np.ones(4) / 4.0, mode="valid")[:n])  # correlated over 30 min
        k = np.arange(n)
        segs_slow.append(0.03 * np.sin(2.0 * np.pi * k / 112.0) + rng.normal(0.0, 0.005, n))  # 19 h wave
    lag = 6
    zeros = [np.zeros(n)] * 3
    fast = identification.residual_checks(segs_fast, zeros, zeros, zeros, lag)
    slow = identification.residual_checks(segs_slow, zeros, zeros, zeros, lag)
    assert fast["runs_z_10min"] < -3.5 and fast["runs_z"] > -3.5 and fast["runs_blocks"] == 3 * 14
    assert slow["runs_z"] < -3.5
    # Too few hourly means: no runs test
    one = identification.residual_checks([segs_fast[0][:48]], [zeros[0][:48]], [zeros[0][:48]], [zeros[0][:48]],
                                         lag)
    assert one["runs_blocks"] == 8 and one["runs_z"] is None


def test_effective_count_of_correlated_residuals() -> None:
    rng = np.random.default_rng(5)
    white = [rng.normal(0.0, 1.0, 400) for _ in range(3)]
    assert identification.effective_count(white) == pytest.approx(1200, rel=0.15)
    ar = []
    for w in white:
        x = np.zeros_like(w)
        for i in range(1, len(w)):
            x[i] = 0.8 * x[i - 1] + w[i]
        ar.append(x)
    # AR(1) with rho 0.8: integrated time (1 + rho) / (1 - rho) = 9
    assert identification.effective_count(ar) == pytest.approx(1200 / 9.0, rel=0.35)


def _rec(struct, a=1e-3, alpha=1e-4, beta=5e-6, c=6e-5, ok=True):
    params = {"struct": struct, "a": a, "alpha": alpha, "beta": beta, "c": c if struct == "full" else 0.0,
              "tau_e": 600.0, "L": 60.0}
    return {"params": params, "ok": ok, "why": None if ok else gates.WHY_RESIDUALS}


def test_decide_paths() -> None:
    cur = {"q": 0.01, "full": _rec("full"), "reduced": _rec("reduced", alpha=4e-5, ok=False)}
    prev = {"q": 0.01, "full": _rec("full", a=1.05e-3), "reduced": _rec("reduced", alpha=4e-5, ok=False)}
    held = {"full": _GOOD}
    dec = gates.decide(cur, prev, held, precise=True)
    assert dec["struct"] == "full" and dec["candidate"]["a"] == pytest.approx((1e-3 * 1.05e-3) ** 0.5)
    # Disagreeing windows
    prev_far = dict(prev, full=_rec("full", a=2e-3))
    assert gates.decide(cur, prev_far, held, True) == {"why": gates.WHY_DISAGREE, "wall": gates.WALL_NOT_CONFIRMED}
    # Bad held-out residuals
    assert gates.decide(cur, prev, {"full": dict(_GOOD, mean=0.2)}, True)["why"] == gates.WHY_HELDOUT
    # Full model no better than the reduced one on held-out data: reduced if it passes
    cur2 = dict(cur, reduced=_rec("reduced", alpha=4e-5))
    prev2 = dict(prev, reduced=_rec("reduced", alpha=4.2e-5))
    held2 = {"full": _GOOD, "reduced": dict(_GOOD, rms=0.02)}
    dec = gates.decide(cur2, prev2, held2, True)
    assert dec["struct"] == "reduced" and dec["wall"] == gates.WALL_NOT_BETTER
    # Small wall share: reduced model
    cur3 = dict(cur2, full=_rec("full", c=5e-6))
    assert gates.decide(cur3, prev2, held2, True)["wall"] == gates.WALL_SMALL
    # Coarse sensor: never the full model
    assert gates.decide(cur2, prev2, held2, False)["wall"] == gates.WALL_COARSE
    # A refusal before the fit passes through
    assert gates.decide({"why": gates.WHY_HEAT}, prev, {}, True) == {"why": gates.WHY_HEAT, "wall": None}


def test_published_is_the_median_of_the_last_candidates() -> None:
    base = {"tau_e": 600.0, "L": 60.0}
    cands = [dict(base, struct="reduced", a=1e-3, alpha=4e-5, beta=1 / 3600, c=0.0)]
    cands += [dict(base, struct="full", a=a, alpha=1e-4, beta=5e-6, c=6e-5) for a in (1.0e-3, 1.2e-3, 1.1e-3, 0.9e-3)]
    pub = gates.published(cands)
    assert pub["struct"] == "full" and pub["a"] == pytest.approx(1.1e-3)
    assert gates.published(cands[:2])["a"] == 1.0e-3  # a change of structure starts over
    assert gates.published([]) is None


# ---------------------------------------------------------------------------
# Energy balance and fit periods
# ---------------------------------------------------------------------------
def _signals(data: dict) -> dict:
    ht, hv, init = heater_timeline(data["on"], data["off"])
    return {"heat_t": ht, "heat_v": hv, "heat_init": init, "temp_t": data["pub_t"], "temp_v": data["pub_v"],
            "ext_t": data["ext_t"], "ext_v": data["ext_v"], "first_t": data["first_t"], "last_t": data["last_t"],
            "invalid": []}


def _points(slope: float, gain: float, deltas, noise=(), valid=1.0) -> list[dict]:
    noise = list(noise) + [0.0] * (len(deltas) - len(noise))
    return [{"day": D0 + i, "valid": valid, "duty": slope * d - gain + e, "delta_c": d, "u": slope * d - gain + e}
            for i, (d, e) in enumerate(zip(deltas, noise))]


def test_energy_balance_regression_and_its_qualification() -> None:
    """u = s D - g by weighted least squares; the slope with its gain when it is determined, else the slope alone
    when the days show no gain, else a refusal."""
    noise = (0.002, -0.003, 0.001, 0.0025, -0.002, 0.0)
    wide = (8.0, 14.0, 11.0, 17.0, 9.5, 15.0)
    # Enough spread of the indoor-outdoor difference: slope and gain, within their uncertainty
    rec = energy_balance.qualify(_points(0.03, 0.08, wide, noise), 0.05)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_GAIN and rec["n"] == 6
    assert rec["slope"] == pytest.approx(0.03, rel=0.05) and rec["gain"] == pytest.approx(0.08, abs=0.02)
    assert rec["fit"]["half_width"] <= energy_balance.EB_MAX_REL_HALF_WIDTH
    # A negative gain (heat lost to a colder neighbour) is a gain too
    rec = energy_balance.qualify(_points(0.03, -0.05, wide, noise), 0.05)
    assert rec["reason"] is None and rec["gain"] == pytest.approx(-0.05, abs=0.02)
    # Little spread, no gain: the slope alone (the ratio of the means)
    flat = (12.0, 12.1, 11.9, 12.05, 11.95, 12.0)
    rec = energy_balance.qualify(_points(0.03, 0.0, flat, noise), 0.05)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_SLOPE and rec["gain"] == 0.0
    assert rec["slope"] == pytest.approx(0.03, rel=0.02)
    # Moderate spread and a large gain: the gain is seen, the slope is not determined well enough
    scatter = (0.01, -0.012, 0.008, -0.01, 0.012, -0.008)
    mid = (9.0, 12.0, 8.5, 12.5, 10.0, 11.5)
    rec = energy_balance.qualify(_points(0.04, 0.15, mid, scatter), 0.05)
    assert rec["reason"] == energy_balance.REASON_GAIN and rec["slope"] is not None
    # Enough spread, no gain seen, but the line is not determined: refused, no ratio of the means as a fallback
    rec = energy_balance.qualify(_points(0.03, 0.0, mid, (0.03, -0.03, 0.02, -0.025, 0.03, -0.02)), 0.05)
    assert rec["reason"] == energy_balance.REASON_SCATTER and rec["form"] == energy_balance.FORM_GAIN
    # Below 1 C of spread the gain cannot be seen: the slope alone, the ratio of the means
    narrow = (10.0, 11.5, 9.0, 12.0, 10.5, 11.0)
    assert energy_balance.fit(_points(0.04, 0.15, narrow))["sd_delta_c"] < energy_balance.EB_MIN_SPREAD_C
    rec = energy_balance.qualify(_points(0.04, 0.15, narrow, scatter), 0.05)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_SLOPE
    assert rec["slope"] == pytest.approx(0.04 - 0.15 / 10.67, rel=0.03)
    # No gain seen but too much scatter from day to day
    rec = energy_balance.qualify(_points(0.03, 0.0, flat, (0.03, -0.03, 0.02, -0.025, 0.03, -0.02)), 0.05)
    assert rec["reason"] == energy_balance.REASON_SCATTER
    # Fewer than four usable days: the days with missing data or without heating do not count
    pts = _points(0.03, 0.0, flat, noise)
    pts[0]["valid"] = 0.7
    pts[1]["duty"] = 0.01
    pts[2]["delta_c"] = -1.0
    assert energy_balance.qualify(pts, 0.05)["reason"] == energy_balance.REASON_DAYS
    assert energy_balance.qualify(pts[:3], 0.05)["reason"] == energy_balance.REASON_HISTORY
    assert energy_balance.qualify(_points(0.03, 0.0, flat), None)["reason"] == energy_balance.REASON_NO_A
    # Too little heating on average, too small a difference, out of range
    assert energy_balance.qualify(_points(0.003, 0.0, flat), 0.05)["reason"] == energy_balance.REASON_DUTY
    assert energy_balance.qualify(_points(0.03, 0.0, (5.0,) * 6), 0.05)["reason"] == energy_balance.REASON_DELTA
    big = [dict(p, u=4.0 * p["delta_c"], duty=0.9) for p in _points(0.03, 0.0, flat)]
    assert energy_balance.qualify(big, 0.05)["reason"] == energy_balance.REASON_RANGE
    # On a tiny spread the intercept is a long extrapolation: never read as a gain, the slope alone qualifies
    tiny = (12.0, 12.002, 11.998, 12.001, 11.999, 12.0)
    rec = energy_balance.qualify(_points(0.03, 0.0, tiny, (0.0003, -0.0002, 0.0001, 0.0002, -0.0003, 0.0)), 0.05)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_SLOPE


def test_energy_balance_weights_the_days_by_their_valid_share() -> None:
    pts = _points(0.03, 0.05, (8.0, 14.0, 11.0, 17.0, 9.5, 15.0))
    pts[3] = dict(pts[3], valid=0.8, u=pts[3]["u"] + 0.05)
    lighter = energy_balance.fit(pts)
    pts[3]["valid"] = 1.0
    even = energy_balance.fit(pts)
    assert abs(lighter["slope"] - 0.03) < abs(even["slope"] - 0.03)


def test_energy_balance_on_a_simulated_room(no_mass_data) -> None:
    p = rsim.NO_MASS
    clock = LocalClock("UTC")
    sig = _signals(no_mass_data)
    rec = energy_balance.regression(sig, clock, D0 + 7, p["a"] * 60.0)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_SLOPE
    assert [q["day"] for q in rec["points"]] == list(range(D0, D0 + 7))
    assert rec["slope"] == pytest.approx(p["alpha"] / p["a"], rel=0.03)
    assert energy_balance.regression(sig, clock, D0 + 3, p["a"] * 60.0)["reason"] == "history"
    assert energy_balance.regression(sig, clock, D0 + 7, None)["reason"] == "no a"
    cold = _signals(dict(no_mass_data, on=[], off=[]))
    assert energy_balance.regression(cold, clock, D0 + 7, p["a"] * 60.0)["reason"] == "days"


def test_energy_balance_separates_a_constant_gain_from_the_losses(monkeypatch) -> None:
    """Outdoor temperature changing from day to day and a constant free gain of 6 % of full power: the
    regression gives the true static loss and the gain."""
    base = rsim.outdoor
    monkeypatch.setattr(rsim, "outdoor",
                        lambda t: base(t) + 4.0 * math.sin(2.0 * math.pi * (t - rsim.T0) / (5.3 * DAY)))
    p = rsim.NO_MASS
    g = 0.06
    data = rsim.simulate(p, 8.5, gain=lambda t: g * p["a"])
    rec = energy_balance.regression(_signals(data), LocalClock("UTC"), D0 + 8, p["a"] * 60.0)
    assert rec["reason"] is None and rec["form"] == energy_balance.FORM_GAIN
    assert rec["slope"] == pytest.approx(p["alpha"] / p["a"], rel=0.05)
    assert rec["gain"] == pytest.approx(g, abs=0.015)
    # The ratio of the means is off by g / mean difference
    assert rec["fit"]["slope_only"] < 0.9 * p["alpha"] / p["a"]


def test_daily_energy_balance_covers_the_whole_days_before_the_day(no_mass_data) -> None:
    p = rsim.NO_MASS
    clock = LocalClock("UTC")
    snap = rsim.snapshot(no_mass_data, D0 + 6, a_per_min=p["a"] * 60.0)
    rec = energy_balance.daily(snap, clock, D0 + 6)
    ref = energy_balance.regression(_signals(no_mass_data), clock, D0 + 6, p["a"] * 60.0)
    assert rec == {"value": ref["slope"], "gain": 0.0, "reason": None, "slope": ref["slope"],
                   "form": energy_balance.FORM_SLOPE, "days": 6}
    # A refused day gives the reason, no value
    rec = energy_balance.daily(dict(snap, on=[], off=[]), clock, D0 + 6)
    assert rec["value"] is None and rec["gain"] is None and rec["reason"] == "days"
    # Its own job, whatever the quiet periods: a record without any period gives the same balance
    flat = dict(snap, sp=[[no_mass_data["first_t"], 20.0]], window=None)
    assert jobs.balance_job(flat) == {"day": D0 + 6, "eb": energy_balance.daily(snap, clock, D0 + 6)}
    # The identification only reads the slope in force
    job = jobs.identify_job(dict(snap, tau_e=None, L=None, eb=0.0321))
    assert job["decision"]["why"] == gates.WHY_EMITTER and job["eb"] == 0.0321


def test_job_checks_the_candidate_against_the_energy_balance_in_force(mass_data) -> None:
    """The slope in force for the decision is the one of the snapshot (None before a first qualified value)."""
    p = rsim.MASS
    first, second, _ = _two_windows(mass_data, p)
    snap = rsim.snapshot(mass_data, D0 + 7, prev=_slim(first), tau_e=p["tau_e"], L=p["L"],
                         a_per_min=p["a"] * 60.0)
    assert jobs.eb_in_force(dict(snap, eb=0.021)) == 0.021
    assert jobs.eb_in_force(snap) is None and second["eb"] is None
    gain = gates._coords(second["decision"]["candidate"])[1]
    far = jobs._done(dict(second), dict(snap, eb=gain * 1.10))
    assert far["decision"] == {"why": gates.WHY_EB, "wall": gates.WALL_NOT_CONFIRMED}
    near = jobs._done(dict(second), dict(snap, eb=gain * 1.03))
    assert near["decision"]["struct"] == "full"


def _period(k: int, a: float, b: float) -> dict:
    return {"id": f"q1:{a:.1f}:{a:.1f}", "quiet": [a, b - 2 * H], "ident": [a, b]}


def test_windows_are_three_closed_periods_compared_with_the_three_before() -> None:
    pers = [_period(k, k * DAY + 22 * H, k * DAY + 32 * H) for k in range(8)]
    ws = windows(pers)
    assert [w["id"] for w in ws] == [p["id"] for p in pers[2:]]
    for k, w in enumerate(ws, start=2):
        assert w["nights"] == [p["id"] for p in pers[k - 2: k + 1]]
        assert w["intervals"] == [p["ident"] for p in pers[k - 2: k + 1]] and w["t_end"] == pers[k]["ident"][1]
        if k < 5:
            assert w["prev"] is None
            continue
        # The window compared with it is the one of the three periods before: disjoint
        prev = next(x for x in ws if x["id"] == w["prev"])
        assert prev["nights"] == [p["id"] for p in pers[k - 5: k - 2]]
        assert not set(prev["nights"]) & set(w["nights"])
    # A period without its recorded identification interval falls back on its quiet part
    assert windows([dict(p, ident=None) for p in pers[:3]])[0]["intervals"][0] == pers[0]["quiet"]


def test_fit_periods_are_the_valid_identification_intervals() -> None:
    pers = [_period(k, k * DAY + 22 * H, k * DAY + 32 * H) for k in range(3)]
    w = windows(pers)[0]
    first, last = -DAY, 10 * DAY
    per = fit_periods(w, first, last, [])
    assert [n for _, _, n in per] == w["nights"]
    assert all(b - a == pytest.approx(10 * H) for a, b, _ in per)
    # Cut by an invalid interval: two pieces with their own initial state; a disturbed period is left out
    gap = (DAY + 25 * H, DAY + 28 * H)
    per = fit_periods(w, first, last, [gap], excluded=[w["nights"][0]])
    assert [(round((b - a) / H, 3), n) for a, b, n in per] == [(3.0, w["nights"][1]), (4.0, w["nights"][1]),
                                                               (10.0, w["nights"][2])]
    # Pieces shorter than the minimum are dropped; the recorded data bound the intervals
    gap2 = (2 * DAY + 22.5 * H, 2 * DAY + 31.5 * H)
    assert w["nights"][2] not in [n for _, _, n in fit_periods(w, first, last, [gap2])]
    per = fit_periods(w, 22 * H + 5 * H, 2 * DAY + 26 * H, [])
    assert [(round((b - a) / H, 3), n) for a, b, n in per] == [(5.0, w["nights"][0]), (10.0, w["nights"][1]),
                                                               (4.0, w["nights"][2])]


def test_periods_come_from_the_shared_catalogue(mass_data) -> None:
    """The fit periods of a window are the identification intervals of the catalogue's quiet periods: the
    setback 22:00-06:00 and the 2 h response to the rise while it is dark."""
    pers = rsim.quiet_periods(mass_data)
    assert len(pers) == 8
    for p in pers:
        a, b = p["ident"]
        assert p["kind"] == "setback" and p["quiet"][1] == p["rise_t"]
        assert (a - rsim.T0) % DAY == pytest.approx(22 * H, abs=1.0) and b - a == pytest.approx(10 * H, abs=1.0)
    w = rsim.window(mass_data, D0 + 4)
    assert w["nights"] == [p["id"] for p in pers[1:4]] and w["prev"] is None
    assert rsim.window(mass_data, D0 + 7)["prev"] == w["id"]
    # A gap splits a night: the longest piece is the period, and only it is fitted
    g = (pers[2]["quiet"][0] + 2 * H, pers[2]["quiet"][0] + 3 * H)
    cut = rsim.window(mass_data, D0 + 4, gaps=[g])
    k = cut["nights"].index(next(n for n in cut["nights"] if n not in w["nights"]))
    assert k == 1 and cut["intervals"][1][0] == pytest.approx(g[1], abs=0.1)


def _fit_on(data: dict, span_h: float, emitter: bool = False) -> dict:
    """Model with walls fitted on the first span_h hours of each period of the window closed on day 4."""
    p = rsim.MASS
    snap = rsim.snapshot(data, D0 + 4, tau_e=p["tau_e"], L=p["L"], a_per_min=p["a"] * 60.0)
    w = snap["window"]
    per = [(a, min(b, a + span_h * H), n) for (a, b), n in zip(w["intervals"], w["nights"])]
    d = identification.WindowData(snap, per, p["tau_e"], p["L"])
    pb = identification.Problem(d, True)
    f = identification.fit(pb)
    em = {"source": "grid", "first_moment": p["tau_e"] + p["L"]} if emitter else {}
    f["info"] = identification.precision(pb, f, jobs._emitter_problems(d, em, p["tau_e"], 0.0, True))
    return dict(gates.fit_record(f, "full", "precise", p["tau_e"], p["L"], 0.01), hours=d.hours)


def test_information_of_a_fit_is_its_precision_not_its_duration() -> None:
    """The precision of the identifiable coordinates, from the Jacobian of the fit, decides; the same 12 hours
    determine the walls with a clean sensor and not with a noisy one, and 30 hours of the noisy one do."""
    clean = rsim.simulate(rsim.MASS, 4.5)
    noisy = rsim.simulate(rsim.MASS, 4.5, noise=0.02)
    short_clean = _fit_on(clean, 4.0)
    assert short_clean["hours"] == pytest.approx(12.0, abs=0.1) and short_clean["ok"] is True
    short_noisy = _fit_on(noisy, 4.0)
    assert short_noisy["ok"] is False and short_noisy["why"] == gates.WHY_INFO
    sd = short_noisy["info"]["sd"]
    assert RM_INFO_Z * sd["alpha"] > RM_AGREE_TOL["alpha"]
    long_noisy = _fit_on(noisy, 10.0)
    assert long_noisy["hours"] == pytest.approx(30.0, abs=0.1) and long_noisy["ok"] is True
    # A window that estimated its emitter carries the uncertainty of that estimate too
    with_em = _fit_on(noisy, 10.0, emitter=True)["info"]["sd"]
    assert with_em["b_eq_a"] > long_noisy["info"]["sd"]["b_eq_a"] and with_em["a"] > long_noisy["info"]["sd"]["a"]
    # The refusal shows with its plain reason and keeps the walls unconfirmed
    cur = {"q": 0.01, "full": short_noisy, "reduced": {"ok": False, "why": gates.WHY_INFO}}
    assert gates.decide(cur, None, {}, True) == {"why": gates.WHY_INFO, "wall": gates.WALL_NOT_CONFIRMED}
    assert public.reason(gates.WHY_INFO) == "the quiet periods do not determine the model precisely enough yet"
    # Without a precision, or with one not determined, a fit is not kept; a model without a real wall share
    # is never used, so its precision is not required
    params = long_noisy["params"]
    assert not gates.info_ok(None, "full", params)
    assert not gates.info_ok({"sd": dict(long_noisy["info"]["sd"], c=float("nan"))}, "full", params)
    assert gates.info_ok(None, "full", dict(params, c=0.05 * params["alpha"]))


# ---------------------------------------------------------------------------
# Heating response against the online estimate
# ---------------------------------------------------------------------------
def test_a_lumped_simple_model_is_refused() -> None:
    # A simple model with the walls lumped into the air: right static ratio, a 14 times too small
    a_online = 7.5e-4
    lumped = dict(_rec("reduced", a=a_online / 14.0, alpha=1.0e-6), ok=True)
    lumped_prev = dict(_rec("reduced", a=a_online / 14.5, alpha=1.04e-6), ok=True)
    cur = {"q": 0.01, "full": _rec("full", c=1e-6), "reduced": lumped}
    prev = {"q": 0.01, "full": _rec("full", c=1e-6), "reduced": lumped_prev}
    held = {"reduced": _GOOD}
    # Without the online estimate the windows agree and the simple model would be published
    assert gates.decide(cur, prev, held, True)["struct"] == "reduced"
    dec = gates.decide(cur, prev, held, True, a_online=a_online)
    assert dec == {"why": gates.WHY_A_ONLINE, "wall": gates.WALL_SMALL}
    # Refused already on the first window, before waiting for a second one
    assert gates.decide(cur, None, {}, True, a_online=a_online)["why"] == gates.WHY_A_ONLINE
    # Within a factor 2 of the online estimate: published
    near = dict(cur, reduced=dict(_rec("reduced", a=a_online / 1.8, alpha=4e-5), ok=True))
    near_prev = dict(prev, reduced=dict(_rec("reduced", a=a_online / 1.75, alpha=4.1e-5), ok=True))
    assert gates.decide(near, near_prev, held, True, a_online=a_online)["struct"] == "reduced"
    assert public.reason(gates.WHY_A_ONLINE) == (
        "the heating response differs too much from the one measured by the regulation")


def test_a_lumped_wall_model_falls_back_to_the_simple_model() -> None:
    cur = {"q": 0.01, "full": _rec("full", a=3e-4), "reduced": _rec("reduced", alpha=4e-5)}
    prev = {"q": 0.01, "full": _rec("full", a=3.1e-4), "reduced": _rec("reduced", alpha=4.1e-5)}
    held = {"full": _GOOD, "reduced": dict(_GOOD, rms=0.05)}
    assert gates.decide(cur, prev, held, True)["struct"] == "full"
    dec = gates.decide(cur, prev, held, True, a_online=1e-3)
    assert dec["struct"] == "reduced" and dec["wall"] == gates.WALL_NOT_CONFIRMED
    cur_bad = dict(cur, reduced=_rec("reduced", alpha=4e-5, ok=False))
    assert gates.decide(cur_bad, prev, held, True, a_online=1e-3) == {"why": gates.WHY_A_ONLINE,
                                                                         "wall": gates.WALL_NOT_CONFIRMED}


# ---------------------------------------------------------------------------
# Emitter refinement
# ---------------------------------------------------------------------------
LAG_ONLY = dict(rsim.MASS, tau_e=600.0, L=0.0)


@pytest.fixture(scope="module")
def lag_data():
    return rsim.simulate(LAG_ONLY, 4.5)


def _job(data: dict, plant: dict, first_moment: float, day: int = 4, prev=None, **kw) -> dict:
    return jobs.identify_job(dict(rsim.snapshot(data, D0 + day, prev=prev, tau_e=first_moment, L=0.0,
                                                a_per_min=plant["a"] * 60.0), **kw))


def test_emitter_grid_finds_the_true_time_constant(lag_data) -> None:
    rec = _job(lag_data, LAG_ONLY, 150.0)
    em = rec["emitter"]
    assert em["source"] == "grid" and em["flat"] is False and em["significance"] > RM_EMITTER_CHI2
    assert em["first_moment"] == pytest.approx(600.0, rel=0.1) and em["L"] == 0.0
    assert rec["full"]["ok"] and rec["full"]["params"]["tau_e"] == pytest.approx(em["tau_e"], abs=0.1)
    costs = em["costs"]
    assert len(costs) == len(RM_EMITTER_GRID_S) == 13
    assert RM_EMITTER_GRID_S[0] == 60.0 and RM_EMITTER_GRID_S[-1] == 3600.0
    assert min(costs) in (costs[6], costs[7])


def test_emitter_grid_does_not_depend_on_the_dead_time_in_force(lag_data) -> None:
    a = _job(lag_data, LAG_ONLY, 150.0)["emitter"]
    b = _job(lag_data, LAG_ONLY, 1400.0)["emitter"]
    assert a["costs"] == b["costs"] and a["first_moment"] == b["first_moment"]


def test_emitter_optimum_at_the_edge_with_a_marked_profile_is_refused() -> None:
    plant = dict(rsim.NO_MASS, tau_e=9000.0, L=0.0)
    data = rsim.simulate(plant, 4.5)
    rec = _job(data, plant, 720.0)
    assert rec["emitter"]["first_moment"] == 3600.0 and rec["emitter"]["significance"] > RM_EMITTER_CHI2
    assert rec["decision"] == {"why": gates.WHY_EMITTER_EDGE, "wall": None}
    assert "full" not in rec and "reduced" not in rec
    assert public.reason(gates.WHY_EMITTER_EDGE) == (
        "the heating response delay does not match the measured temperature")


def _fake_profile(monkeypatch, costs, n_eff: float) -> list:
    """jobs._full_fit replaced by a cost profile (a function of the first moment)."""
    calls = []

    def fake(snap, periods, fm, te_pub, gain):
        calls.append(fm)
        return fm, 0.0, f"data@{fm:.1f}", {"cost": costs(fm), "check": {"n_eff": n_eff}}

    monkeypatch.setattr(jobs, "_full_fit", fake)
    return calls


def test_emitter_profile_significance_against_the_residual_variance(monkeypatch) -> None:
    """The rise of the cost profile is judged in units of the residual variance per effective independent
    residual (chi-square, one degree of freedom, 95 %)."""
    grid = RM_EMITTER_GRID_S
    # Optimum at the low edge, 4 % rise at the far end: with 40 effective residuals 1.6 < 3.84, not significant.
    # Not refused; without a refined T_ar value the grid optimum is used, with one the emitter in force.
    calls = _fake_profile(monkeypatch, lambda fm: 1.0 + 0.04 * (fm - 60.0) / 3540.0, 40.0)
    em = jobs.refine_emitter({}, [], "in force", 300.0, 0.0)
    assert em["edge"] is False and em["flat"] is True and em["significance"] == pytest.approx(1.6)
    assert em["source"] == "grid" and em["first_moment"] == 60.0 and em["data"] == "data@60.0"
    assert calls == list(grid)
    em = jobs.refine_emitter({"te_pub": 280.0}, [], "in force", 280.0, 20.0)
    assert em["flat"] is True and em["source"] == "in force"
    assert (em["tau_e"], em["L"], em["data"]) == (280.0, 20.0, "in force")
    # The same rise with 200 effective residuals is significant: an edge optimum is refused
    _fake_profile(monkeypatch, lambda fm: 1.0 + 0.04 * (fm - 60.0) / 3540.0, 200.0)
    em = jobs.refine_emitter({}, [], "in force", 300.0, 0.0)
    assert em["edge"] is True and em["flat"] is False and em["significance"] == pytest.approx(8.0)
    _fake_profile(monkeypatch, lambda fm: 1.0 + 0.04 * (3600.0 - fm) / 3540.0, 200.0)
    assert jobs.refine_emitter({}, [], "in force", 300.0, 0.0)["edge"] is True
    # An interior optimum is refined between its grid neighbours (at most 10 more fits) and never refused
    calls = _fake_profile(monkeypatch, lambda fm: 1.0 + 0.5 * math.log(fm / 400.0) ** 2, 50.0)
    em = jobs.refine_emitter({}, [], "in force", 300.0, 0.0)
    assert em["edge"] is False and em["flat"] is False and em["source"] == "grid"
    assert len(grid) < len(calls) <= len(grid) + 10 and em["first_moment"] == pytest.approx(400.0, rel=0.01)


def test_emitter_split_keeps_the_refined_lag() -> None:
    from custom_components.vtherm_smartpi.smartpi.room_model.emitter import split_first_moment

    assert jobs._te_pub({}, 600.0, 120.0) == 600.0
    assert jobs._te_pub({}, 720.0, 0.0) is None
    assert jobs._te_pub({"te_pub": 900.0}, 720.0, 0.0) == 900.0
    assert split_first_moment(720.0 * 1.41, 600.0) == pytest.approx((600.0, 720.0 * 1.41 - 600.0))
    assert split_first_moment(720.0 * 0.5, 600.0) == (360.0, 0.0)
    assert split_first_moment(720.0 * 2.0, None) == (1440.0, 0.0)


def test_wall_rate_above_the_cap_is_refused() -> None:
    from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams

    fit = {"a": 1e-3, "alpha": 1e-4, "beta": 1.0 / (2.0 * H), "c": 5e-5, "check": dict(_GOOD, rms=0.01),
           "info": _INFO}
    assert gates.fit_record(fit, "full", "precise", 600.0, 0.0, 0.01)["why"] == gates.WHY_RANGE
    ok = gates.fit_record(dict(fit, beta=1.0 / (4.0 * H)), "full", "precise", 600.0, 0.0, 0.01)
    assert ok["ok"] is True
    assert not RoomParams("full", 1e-3, 1e-4, 1.0 / (2.0 * H), 5e-5, 600.0, 0.0).plausible()


def test_room_without_mass_and_a_slow_emitter_gets_no_walls() -> None:
    # True emitter 3600 s, the end of the grid: no window publishes walls for a room without mass
    plant = dict(rsim.NO_MASS, tau_e=3600.0, L=0.0)
    data = rsim.simulate(plant, 7.5)
    first = _job(data, plant, 720.0, day=4)
    second = _job(data, plant, 720.0, day=7, prev=_slim(first))
    for rec in (first, second):
        assert rec["decision"].get("struct") != "full"
        assert rec["emitter"]["first_moment"] >= 1800.0


def test_fit_with_the_free_gain_in_force_matches_the_energy_balance() -> None:
    """A constant free gain of 8 % of full power: the fits that take the gain of the static model in force
    find the true b_eq / a and agree with the slope; without it the gain reads as a lower loss and the
    candidate is refused against the same slope."""
    p = rsim.NO_MASS
    g = 0.08
    data = rsim.simulate(p, 7.5, gain=lambda t: g * p["a"])
    slope = p["alpha"] / p["a"]

    def two(gain):
        prev = None
        for d in (4, 7):
            snap = dict(rsim.snapshot(data, D0 + d, prev=prev, tau_e=p["tau_e"], L=p["L"], a_per_min=None),
                        eb=slope, eb_gain=gain)
            rec = jobs.identify_job(snap)
            prev = _slim(rec)
        return rec

    with_gain = two(g)
    assert with_gain["gain"] == g
    assert with_gain["reduced"]["b_eq"] / with_gain["reduced"]["params"]["a"] == pytest.approx(slope, rel=0.03)
    assert with_gain["decision"]["struct"] == "reduced"
    without = two(0.0)
    assert without["reduced"]["b_eq"] / without["reduced"]["params"]["a"] < 0.9 * slope
    assert without["decision"].get("struct") is None
    assert without["decision"]["why"] in (gates.WHY_EB, gates.WHY_RESIDUALS)
    # The gain in force: the one of the static model in force, 0 without one
    assert jobs.eb_gain_in_force({"eb": 0.02, "eb_gain": 0.1}) == 0.1
    assert jobs.eb_gain_in_force({"eb": None, "eb_gain": 0.1}) == 0.0


# ---------------------------------------------------------------------------
# Combination in the identifiable coordinates
# ---------------------------------------------------------------------------
def test_combine_and_median_in_identifiable_coordinates() -> None:
    base = {"struct": "full", "tau_e": 600.0, "L": 0.0}
    # Same static ratio b_eq / a = 0.04 on a ridge of (alpha, c)
    p = dict(base, a=1e-3, alpha=1.0e-4, beta=4e-6, c=6.0e-5)
    q = dict(base, a=1e-3, alpha=1.5e-4, beta=9e-6, c=1.1e-4)
    out = gates.combine(p, q)
    assert (out["alpha"] - out["c"]) / out["a"] == pytest.approx(0.04, rel=1e-12)
    assert out["c"] / out["alpha"] == pytest.approx((0.6 * 1.1 / 1.5) ** 0.5, rel=1e-12)
    assert out["beta"] == pytest.approx(6e-6, rel=1e-12)
    assert out["a"] == pytest.approx(1e-3, rel=1e-12)
    # The geometric mean of alpha and c taken apart would give b_eq / a = 0.0412 (+3 %)
    old = ((1.0e-4 * 1.5e-4) ** 0.5 - (6.0e-5 * 1.1e-4) ** 0.5) / 1e-3
    assert old == pytest.approx(0.04124, abs=1e-5)
    # Reduced model: a and b_eq / a
    r = gates.combine(dict(base, struct="reduced", a=1e-3, alpha=4e-5, beta=1 / 3600, c=0.0),
                      dict(base, struct="reduced", a=4e-3, alpha=4e-5, beta=1 / 3600, c=0.0))
    assert r["a"] == pytest.approx(2e-3) and r["alpha"] == pytest.approx(4e-5) and r["c"] == 0.0
    # Published: median of each coordinate over the last candidates
    cands = [dict(p, a=1.0e-3), dict(q, a=1.2e-3, alpha=1.2 * q["alpha"], c=1.2 * q["c"]),
             dict(p, a=1.1e-3, alpha=1.1 * 1.2e-4, c=1.1 * 0.7e-4, beta=5e-6)]
    pub = gates.published(cands)
    ratios = sorted((c["alpha"] - c["c"]) / c["a"] for c in cands)
    shares = sorted(c["c"] / c["alpha"] for c in cands)
    assert pub["a"] == pytest.approx(1.1e-3)
    assert (pub["alpha"] - pub["c"]) / pub["a"] == pytest.approx(ratios[1], rel=1e-12)
    assert pub["c"] / pub["alpha"] == pytest.approx(shares[1], rel=1e-12)
    assert pub["beta"] == pytest.approx(5e-6)


def test_candidate_must_agree_with_the_energy_balance() -> None:
    cur = {"q": 0.01, "full": _rec("full"), "reduced": _rec("reduced", alpha=4e-5, ok=False)}
    prev = {"q": 0.01, "full": _rec("full", a=1.05e-3), "reduced": _rec("reduced", alpha=4e-5, ok=False)}
    held = {"full": _GOOD}
    dec = gates.decide(cur, prev, held, precise=True)
    assert dec["struct"] == "full"
    gain = gates._coords(dec["candidate"])[1]
    # No qualified ratio, or one within 5 %: unchanged
    assert gates.decide(cur, prev, held, True, eb=None) == dec
    for f in (1.03, 1 / 1.03, 1.049, 1 / 1.049):
        assert gates.decide(cur, prev, held, True, eb=gain * f) == dec
    # 10 % off either way: refused with the plain reason
    for f in (1.10, 1 / 1.10, 1.051):
        assert gates.decide(cur, prev, held, True, eb=gain * f) == {"why": gates.WHY_EB,
                                                                     "wall": gates.WALL_NOT_CONFIRMED}
    assert public.reason(gates.WHY_EB) == "the model's heat loss disagrees with the measured heat balance"
    # The simple model is checked the same way
    cur2 = dict(cur, full=_rec("full", ok=False), reduced=_rec("reduced", alpha=4e-5))
    prev2 = dict(prev, reduced=_rec("reduced", alpha=4.2e-5))
    dec2 = gates.decide(cur2, prev2, {"reduced": _GOOD}, True)
    assert dec2["struct"] == "reduced"
    g2 = gates._coords(dec2["candidate"])[1]
    assert gates.decide(cur2, prev2, {"reduced": _GOOD}, True, eb=g2 * 1.03) == dec2
    assert gates.decide(cur2, prev2, {"reduced": _GOOD}, True, eb=g2 * 1.10)["why"] == gates.WHY_EB
