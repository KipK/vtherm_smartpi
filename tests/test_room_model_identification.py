"""Room model identification: fits on synthetic rooms, publication rules, energy-balance prior, fit periods."""

from __future__ import annotations

import time

import pytest

import room_model_sim as rsim
from custom_components.vtherm_smartpi.smartpi.room_model import gates, jobs
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
    assert first["prior"]["reason"] is None  # the energy balance qualifies and is used as prior
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
    assert worst < 10.0


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
# Energy balance prior and fit periods
# ---------------------------------------------------------------------------
def _signals(data: dict) -> dict:
    ht, hv, init = heater_timeline(data["on"], data["off"])
    return {"heat_t": ht, "heat_v": hv, "heat_init": init, "temp_t": data["pub_t"], "temp_v": data["pub_v"],
            "ext_t": data["ext_t"], "ext_v": data["ext_v"], "first_t": data["first_t"], "last_t": data["last_t"],
            "invalid": []}


def test_energy_balance_prior(no_mass_data) -> None:
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
