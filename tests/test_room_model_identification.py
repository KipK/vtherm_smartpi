"""Room model identification: fits on synthetic rooms, publication rules, energy balance, fit periods."""

from __future__ import annotations

import time

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.smartpi.room_model import gates, jobs, public
from custom_components.vtherm_smartpi.smartpi.room_model import energy_balance
from custom_components.vtherm_smartpi.smartpi.room_model.energy_balance import heater_timeline, static_gain
from custom_components.vtherm_smartpi.smartpi.room_model.periods import fit_periods
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock

D0 = rsim.day_of(rsim.T0)
H = 3600.0


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
    # The energy balance of the 3 whole days before the day (and 22 h more for its shifted windows) qualifies;
    # the candidate agrees with it
    assert first["eb"]["reason"] is None and second["eb"]["reason"] is None
    assert second["eb"]["value"] == pytest.approx((p["alpha"] - p["c"]) / p["a"], rel=0.03)
    early = jobs.identify_job(rsim.snapshot(mass_data, D0 + 3, tau_e=p["tau_e"], L=p["L"],
                                            a_per_min=p["a"] * 60.0))
    assert early["eb"] == {"value": None, "reason": "history", "ratio": None}
    dec = second["decision"]
    assert dec["struct"] == "full"
    c = dec["candidate"]
    for k in ("a", "alpha", "c"):
        assert c[k] == pytest.approx(p[k], rel=0.03)
    assert c["alpha"] - c["c"] == pytest.approx(p["alpha"] - p["c"], rel=0.05)
    assert c["beta"] == pytest.approx(p["beta"], rel=0.2)
    assert c["tau_e"] == p["tau_e"] and c["L"] == p["L"]
    assert gates.residuals_ok(second["held"]["full"], "precise", held_out=True, q=0.01)
    # The reduced model cannot follow a mass room
    assert second["reduced"]["ok"] is False
    assert worst < 30.0  # five extra full fits refine the emitter


def test_reduced_model_on_a_room_without_mass(no_mass_data) -> None:
    p = rsim.NO_MASS
    _, second, _ = _two_windows(no_mass_data, p)
    dec = second["decision"]
    assert dec["struct"] == "reduced"
    assert dec["wall"] == gates.WALL_SMALL
    assert dec["candidate"]["a"] == pytest.approx(p["a"], rel=0.02)
    assert dec["candidate"]["alpha"] == pytest.approx(p["alpha"], rel=0.02)
    assert second["full"]["ok"] is False


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
    snap = rsim.snapshot(mass_data, D0 + 1, tau_e=p["tau_e"], L=p["L"])
    assert jobs.identify_job(snap)["decision"]["why"] == gates.WHY_HISTORY
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=p["tau_e"], L=p["L"], excluded=[D0 + 1, D0 + 2, D0 + 3])
    rec = jobs.identify_job(snap)
    assert rec["decision"]["why"] == gates.WHY_DISTURBED
    assert rec["excluded_nights"] == [D0 + 1, D0 + 2, D0 + 3]
    # One disturbed night is left out, the others are fitted
    snap = rsim.snapshot(mass_data, D0 + 4, tau_e=p["tau_e"], L=p["L"], excluded=[D0 + 2])
    rec = jobs.identify_job(snap)
    assert rec["nights"] == [D0 + 1, D0 + 3]


def test_no_heating_is_refused(mass_data) -> None:
    data = dict(mass_data, on=[], off=[])
    p = rsim.MASS
    rec = jobs.identify_job(rsim.snapshot(data, D0 + 4, tau_e=p["tau_e"], L=p["L"]))
    assert rec["decision"]["why"] == gates.WHY_HEAT


# ---------------------------------------------------------------------------
# Publication rules
# ---------------------------------------------------------------------------
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


def test_energy_balance(no_mass_data) -> None:
    p = rsim.NO_MASS
    clock = LocalClock("UTC")
    end = clock.at(D0 + 6, 8.0)
    rec = static_gain(_signals(no_mass_data), end, p["a"] * 60.0)
    assert rec["reason"] is None
    assert rec["value"] == pytest.approx(p["alpha"] / p["a"], rel=0.03)
    assert static_gain(_signals(no_mass_data), clock.at(D0 + 2, 8.0), p["a"] * 60.0)["reason"] == "history"
    assert static_gain(_signals(no_mass_data), end, None)["reason"] is not None
    cold = dict(no_mass_data, on=[], off=[])
    assert static_gain(_signals(cold), end, p["a"] * 60.0)["reason"] in ("duty", "delta")


def test_daily_energy_balance_covers_the_whole_days_before_the_day(no_mass_data) -> None:
    p = rsim.NO_MASS
    clock = LocalClock("UTC")
    snap = rsim.snapshot(no_mass_data, D0 + 6, a_per_min=p["a"] * 60.0)
    rec = energy_balance.daily(snap, clock, D0 + 6)
    ref = static_gain(_signals(no_mass_data), clock.at(D0 + 6, 0.0), p["a"] * 60.0)
    assert rec == {"value": ref["value"], "reason": None, "ratio": ref["value"]}
    assert rec["value"] == pytest.approx(p["alpha"] / p["a"], rel=0.03)
    # A refused day gives the computed ratio and the reason, no value
    rec = energy_balance.daily(dict(snap, on=[], off=[]), clock, D0 + 6)
    assert rec["value"] is None and rec["reason"] == "duty" and rec["ratio"] is not None
    # The job computes it first, whatever the outcome of the fits
    job = jobs.identify_job(dict(snap, tau_e=None, L=None))
    assert job["decision"]["why"] == gates.WHY_EMITTER
    assert job["eb"] == energy_balance.daily(snap, clock, D0 + 6)


def test_job_checks_the_candidate_against_the_energy_balance_in_force(mass_data) -> None:
    """The ratio in force for the decision is the job's own when it qualifies, else the earlier one."""
    p = rsim.MASS
    first, second, _ = _two_windows(mass_data, p)
    snap = rsim.snapshot(mass_data, D0 + 7, prev=_slim(first), tau_e=p["tau_e"], L=p["L"],
                         a_per_min=p["a"] * 60.0)
    assert jobs.eb_in_force(second, snap) == second["eb"]["value"]
    refused = dict(second, eb={"value": None, "reason": "spread", "ratio": 0.04})
    assert jobs.eb_in_force(refused, dict(snap, eb=0.021)) == 0.021
    assert jobs.eb_in_force(refused, snap) is None
    gain = gates._coords(second["decision"]["candidate"])[1]
    far = jobs._done(dict(refused), dict(snap, eb=gain * 1.10))
    assert far["decision"] == {"why": gates.WHY_EB, "wall": gates.WALL_NOT_CONFIRMED}
    near = jobs._done(dict(refused), dict(snap, eb=gain * 1.03))
    assert near["decision"]["struct"] == "full"


def test_fit_periods_are_the_valid_nights() -> None:
    clock = LocalClock("UTC")
    first = clock.at(D0 - 10, 0.0)
    last = clock.at(D0 + 1, 0.0)
    per = fit_periods(clock, D0, first, last, [])
    assert [n for _, _, n in per] == [D0 - 3, D0 - 2, D0 - 1]
    assert all(b - a == pytest.approx(14 * H) for a, b, _ in per)
    gap = (clock.at(D0 - 2, 23.0), clock.at(D0 - 1, 2.0))
    per = fit_periods(clock, D0, first, last, [gap], excluded_nights=[D0 - 3])
    assert [(round((b - a) / H, 3), n) for a, b, n in per] == [(5.0, D0 - 2), (6.0, D0 - 2), (14.0, D0 - 1)]
    # Pieces shorter than the minimum are dropped
    gap2 = (clock.at(D0 - 1, 18.5), clock.at(D0, 7.5))
    per = fit_periods(clock, D0, first, last, [gap2])
    assert D0 - 1 not in [n for _, _, n in per]


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


def _job(data: dict, plant: dict, first_moment: float, day: int = 4, prev=None) -> dict:
    return jobs.identify_job(rsim.snapshot(data, D0 + day, prev=prev, tau_e=first_moment, L=0.0,
                                           a_per_min=plant["a"] * 60.0))


def test_emitter_grid_finds_the_true_time_constant(lag_data) -> None:
    rec = _job(lag_data, LAG_ONLY, 600.0 / 1.41)
    assert rec["emitter"]["mult"] == 1.41
    assert rec["emitter"]["tau_e"] == pytest.approx(600.0, abs=1.0) and rec["emitter"]["L"] == 0.0
    assert rec["full"]["ok"] and rec["full"]["params"]["tau_e"] == pytest.approx(600.0, abs=1.0)
    costs = rec["emitter"]["costs"]
    assert min(costs) == costs[3] and costs[2] > 2.0 * costs[3]


def test_emitter_optimum_at_the_edge_is_refused(lag_data) -> None:
    rec = _job(lag_data, LAG_ONLY, 600.0 / 4.0)
    assert rec["emitter"]["mult"] == 2.0
    assert rec["decision"] == {"why": gates.WHY_EMITTER_EDGE, "wall": None}
    assert "full" not in rec and "reduced" not in rec
    assert public.reason(gates.WHY_EMITTER_EDGE) == (
        "the heating response delay does not match the measured temperature")


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

    fit = {"a": 1e-3, "alpha": 1e-4, "beta": 1.0 / (2.0 * H), "c": 5e-5, "check": dict(_GOOD, rms=0.01)}
    assert gates.fit_record(fit, "full", "precise", 600.0, 0.0, 0.01)["why"] == gates.WHY_RANGE
    ok = gates.fit_record(dict(fit, beta=1.0 / (4.0 * H)), "full", "precise", 600.0, 0.0, 0.01)
    assert ok["ok"] is True
    assert not RoomParams("full", 1e-3, 1e-4, 1.0 / (2.0 * H), 5e-5, 600.0, 0.0).plausible()


def test_room_without_mass_and_a_slow_emitter_gets_no_walls() -> None:
    # True emitter 3600 s, first moment in force 720 s: the grid stops at 1440 s
    plant = dict(rsim.NO_MASS, tau_e=3600.0, L=0.0)
    data = rsim.simulate(plant, 7.5)
    first = _job(data, plant, 720.0, day=4)
    second = _job(data, plant, 720.0, day=7, prev=_slim(first))
    for rec in (first, second):
        assert rec["emitter"]["mult"] == 2.0
        assert rec["decision"]["why"] == gates.WHY_EMITTER_EDGE
        assert rec["decision"].get("struct") is None


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
