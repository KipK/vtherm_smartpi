"""Room model: matrices, parameters, command timeline, observer and forecast scoring."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from scipy.linalg import expm as sp_expm

import room_model_sim as rsim
from custom_components.vtherm_smartpi.smartpi.room_model import linalg as la
from custom_components.vtherm_smartpi.smartpi.room_model import model as rm
from custom_components.vtherm_smartpi.smartpi.room_model.const import RM_HORIZONS_S, RM_SCORE_KEEP, RM_STEPS_KEEP_S
from custom_components.vtherm_smartpi.smartpi.room_model.emitter import CommandTimeline, emitter_step
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams
from custom_components.vtherm_smartpi.smartpi.room_model.observer import (
    HEALTH_OK,
    HEALTH_PAUSED,
    HEALTH_SETTLING,
    HEALTH_WAITING,
    RoomObserver,
)
from custom_components.vtherm_smartpi.smartpi.room_model.scoring import ForecastScore

H = 3600.0


def _params(p: dict) -> RoomParams:
    if p["c"] > 0.0:
        return RoomParams("full", p["a"], p["alpha"], p["beta"], p["c"], p["tau_e"], p["L"])
    return RoomParams.reduced(p["a"], p["alpha"], p["tau_e"], p["L"])


# ---------------------------------------------------------------------------
# Matrices and parameters
# ---------------------------------------------------------------------------
def test_expm_matches_scipy() -> None:
    rng = np.random.default_rng(3)
    for scale in (1e-3, 0.3, 4.0):
        m = rng.normal(size=(5, 5)) * scale
        assert np.allclose(la.expm(m.tolist()), sp_expm(m), rtol=1e-10, atol=1e-12)


@pytest.mark.parametrize("plant", [rsim.MASS, rsim.NO_MASS])
def test_discretisation_is_exact(plant) -> None:
    p = _params(plant)
    h = 60.0
    d = rm.discretize(p, h)
    A, B = (np.array(x) for x in rm.continuous(p))
    M = np.zeros((6, 6))
    M[:3, :3], M[:3, 3:] = A * h, B * h
    E = sp_expm(M)
    assert np.allclose(d.phi, E[:3, :3], atol=1e-14)
    assert np.allclose(d.gam, E[:3, 3:], atol=1e-14)
    # Van Loan against a fine quadrature of Phi(s) Qc Phi(s)^T
    Qc = np.array(rm.process_noise(p))
    s = np.linspace(0.0, h, 2001)
    integrand = [sp_expm(A * x) @ Qc @ sp_expm(A * x).T for x in s]
    qd = np.trapezoid(integrand, s, axis=0)
    assert np.allclose(d.qd, qd, rtol=1e-6, atol=1e-20)


def test_params_derived_values_and_round_trip() -> None:
    p = _params(rsim.MASS)
    assert p.full and p.plausible()
    assert p.b_eq == pytest.approx(p.alpha - p.c)
    assert p.gamma == pytest.approx(p.c * p.beta)
    assert p.c <= p.k_te <= p.alpha and p.k_te == pytest.approx(0.5 * (p.c + p.alpha))
    assert RoomParams.from_dict(p.to_dict()) == p
    r = _params(rsim.NO_MASS)
    assert not r.full and r.b_eq == r.alpha and r.gamma == 0.0 and r.k_te == 0.0
    assert RoomParams.from_dict(r.to_dict()) == r
    assert RoomParams.from_dict({"struct": "full", "a": "x"}) is None
    assert RoomParams.from_dict(dict(p.to_dict(), a=1.0)) is None  # out of range


def test_wall_extra_power_is_zero_at_equilibrium() -> None:
    p = _params(rsim.MASS)
    w_eq = rm.equilibrium_wall(p, 20.0, 5.0)
    assert rm.wall_extra_power(p, 20.0, w_eq, 5.0) == pytest.approx(0.0, abs=1e-12)
    # Walls colder than their equilibrium absorb heat: positive extra power
    assert rm.wall_extra_power(p, 20.0, 0.5 * w_eq, 5.0) > 0.0
    assert rm.wall_extra_power(_params(rsim.NO_MASS), 20.0, 0.0, 5.0) is None


# ---------------------------------------------------------------------------
# Command timeline and emitter
# ---------------------------------------------------------------------------
def test_pwm_cycles_and_valve_levels() -> None:
    tl = CommandTimeline()
    tl.applied_cycle(0.0, 0.25, 600.0, True)
    assert tl.mean(0.0, 600.0) == pytest.approx(0.25)
    assert tl.at(100.0) == 1.0 and tl.at(200.0) == 0.0
    # A mid-cycle update keeps the cycle start
    tl.applied_cycle(100.0, 0.5, 600.0, False)
    assert tl.mean(0.0, 600.0) == pytest.approx(0.5)
    # Full power runs through the cycle and is replaced by the next cycle
    tl.applied_cycle(600.0, 1.0, 600.0, True)
    tl.applied_cycle(1200.0, 1.0, 600.0, True)
    assert tl.mean(600.0, 1800.0) == pytest.approx(1.0)
    tl.applied_level(1800.0, 0.4)
    assert tl.mean(1800.0, 5000.0) == pytest.approx(0.4)
    tl.stop(2000.0)
    assert tl.mean(2000.0, 3000.0) == 0.0
    back = CommandTimeline.from_dict(tl.to_dict())
    assert back.mean(0.0, 3000.0) == pytest.approx(tl.mean(0.0, 3000.0), abs=1e-3)
    tl.prune(1e6)
    assert len(tl.t) <= 1


def test_emitter_step_is_exact_for_a_held_input() -> None:
    tau, h = 600.0, 60.0
    mean, end = emitter_step(0.0, 1.0, h, tau)
    assert end == pytest.approx(1.0 - math.exp(-h / tau))
    assert mean == pytest.approx(1.0 - tau / h * (1.0 - math.exp(-h / tau)))
    assert emitter_step(0.3, 1.0, h, 0.0) == (1.0, 1.0)


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mass_run():
    return rsim.simulate(rsim.MASS, 4.0)


def _observer(plant, q=0.01) -> RoomObserver:
    ob = RoomObserver()
    ob.configure(_params(plant), q)
    return ob


def _replay(ob: RoomObserver, data: dict, t_from: float, t_to: float, active=lambda t: True) -> None:
    def feed(t, kind, pl):
        if kind == "cycle":
            ob.timeline.applied_cycle(t, pl, 600.0, True)
            return
        y, yt, te = pl
        ob.observe(t, temp=y, meas_key=yt, meas_t=yt, ext=te, active=active(t))

    rsim.replay(data, feed, t_from, t_to)


def _truth_at(data: dict, t: float) -> tuple[float, float]:
    tt, ta, tw = data["truth"]
    j = int(np.argmin(np.abs(tt - t)))
    return float(ta[j]), float(tw[j])


def test_observer_tracks_a_mass_room_at_true_parameters(mass_run) -> None:
    ob = _observer(rsim.MASS)
    _replay(ob, mass_run, rsim.T0 + 600.0, rsim.T0 + 4 * 86400.0)
    assert ob.health() == HEALTH_OK
    s = ob.score.summary()
    assert all(n > 50 for n in s["count"])
    assert max(s["rmse"]) < 0.03
    assert all(m < u / 10.0 for m, u in zip(s["rmse"], s["unchanged_rmse"]))
    ta, w = _truth_at(mass_run, ob.t)
    assert ob.x[1] == pytest.approx(w, rel=0.05)
    p = rsim.MASS
    truth = (p["c"] * (ta - rsim.outdoor(ob.t)) - w) / p["a"]
    assert ob.wall_extra() == pytest.approx(truth, abs=0.01)
    fc = ob.forecast()
    assert len(fc) == len(RM_HORIZONS_S) and all(math.isfinite(x) for x in fc)


def test_repeated_values_are_not_new_information() -> None:
    ob = _observer(rsim.NO_MASS)
    ob.observe(0.0, temp=19.0, meas_key="m0", meas_t=0.0, ext=5.0, active=True)
    ob.observe(60.0, temp=19.05, meas_key="m1", meas_t=50.0, ext=5.0, active=True)
    assert ob.innov_n == 1
    # A heartbeat repeating the value (new time stamp) and a tick without a new measurement
    ob.observe(120.0, temp=19.05, meas_key="m2", meas_t=110.0, ext=5.0, active=True)
    ob.observe(180.0, temp=19.05, meas_key="m2", meas_t=110.0, ext=5.0, active=True)
    assert ob.innov_n == 1
    ob.observe(240.0, temp=19.07, meas_key="m3", meas_t=230.0, ext=5.0, active=True)
    assert ob.innov_n == 2


def test_coarse_change_is_read_as_a_boundary_crossing() -> None:
    ob = _observer(rsim.NO_MASS, q=0.1)
    ob.observe(0.0, temp=19.0, meas_key="a", meas_t=0.0, ext=5.0, active=True)
    ob.observe(60.0, temp=19.1, meas_key="b", meas_t=90.0, ext=5.0, active=True)
    assert ob._pending == (90.0, pytest.approx(19.05))
    assert ob.r == pytest.approx(0.01 / 12.0)
    assert ob.wall_extra() is None  # no wall without a precise sensor


def test_restart_resumes_a_recent_state_and_restarts_after_a_long_one(mass_run) -> None:
    ob = _observer(rsim.MASS)
    t_cut = rsim.T0 + 2 * 86400.0
    _replay(ob, mass_run, rsim.T0 + 600.0, t_cut)
    saved = ob.to_dict()
    w_before = ob.x[1]
    restarts = ob.restarts
    # Short downtime: the wall state is kept, the air uncertainty reopened
    short = _observer(rsim.MASS)
    short.load(saved)
    _replay(short, mass_run, t_cut + 1800.0, t_cut + 1860.0)
    assert short.restarts == restarts
    assert short.x[1] == pytest.approx(w_before, rel=0.05)
    assert short.health() == HEALTH_OK
    # Long downtime: restart at the wall equilibrium, settling, then recovered
    long = _observer(rsim.MASS)
    long.load(saved)
    t_back = t_cut + 6 * H
    _replay(long, mass_run, t_back, t_back + 120.0)
    assert long.restarts == restarts + 1
    assert long.health() == HEALTH_SETTLING
    _replay(long, mass_run, t_back + 120.0, t_back + 4 * H)
    ta, w = _truth_at(mass_run, long.t)
    assert long.health() == HEALTH_OK
    assert long.x[1] == pytest.approx(w, rel=0.15)


def test_pause_keeps_the_state_and_drops_pending_forecasts(mass_run) -> None:
    ob = _observer(rsim.MASS)
    t1 = rsim.T0 + 2 * 86400.0
    _replay(ob, mass_run, rsim.T0 + 600.0, t1)
    assert ob.score.origins
    _replay(ob, mass_run, t1, t1 + 1800.0, active=lambda t: False)
    assert ob.health() == HEALTH_PAUSED
    assert not ob.score.origins
    restarts = ob.restarts
    _replay(ob, mass_run, t1 + 1800.0, t1 + 3600.0)
    assert ob.restarts == restarts
    assert ob.health() == HEALTH_OK


def test_inconsistent_measurements_are_flagged() -> None:
    ob = _observer(rsim.NO_MASS)
    for k in range(400):
        # The room warms by 2 C/h without heating: far from the model, beyond the disturbance bound
        ob.observe(60.0 * k, temp=19.0 + 2.0 * k / 60.0, meas_key=k, meas_t=60.0 * k, ext=5.0, active=True)
    assert ob.health() not in (HEALTH_OK, HEALTH_SETTLING)


def test_disturbed_period_is_detected() -> None:
    p = rsim.NO_MASS
    night = rsim.T0 + 3 * 86400.0  # local midnight of the fourth day

    def gain(t):  # 0.2 C/h of unmeasured gain in the fourth night only
        return 0.2 / H if night - 4 * H <= t < night + 6 * H else 0.0

    data = rsim.simulate(p, 6.0, gain=gain)
    ob = _observer(p)
    pers = [(q["id"], *q["ident"]) for q in rsim.quiet_periods(data)]
    # The manager gives the observer the closed periods at each run (here every hour)
    for h in range(144):
        t = rsim.T0 + h * H
        _replay(ob, data, max(t, rsim.T0 + 600.0), t + H)
        ob.attach_periods([x for x in pers if x[2] <= t + H], t + H)
    hit = [k for k, a, b in pers if a <= night < b]
    assert len(hit) == 1 and ob.disturbed_nights() == hit
    # Each period counts once: a second call changes nothing, and the steps already used are forgotten
    assert all(v[1] == pytest.approx(10 * H, abs=120.0) for v in ob.nights.values())
    nights = {k: list(v) for k, v in ob.nights.items()}
    ob.attach_periods(pers, rsim.T0 + 6 * 86400.0)
    assert ob.nights == nights
    assert not ob.steps or ob.steps[0][0] >= pers[-1][2] - 60.0


def _tracked(ob: RoomObserver, t0: float, ds: list[float]) -> None:
    for i, d in enumerate(ds):
        ob._track(t0 + 60.0 * (i + 1), d)


def test_period_disturbance_weights_each_step_by_its_overlap() -> None:
    """A sub-step crossing the bound between two periods counts for its part inside each: the integral and the
    duration are assigned exactly once."""
    ob = RoomObserver()
    t0 = 1_000_000.0
    _tracked(ob, t0, [float(i) for i in range(60)])
    cut = t0 + 30 * 60.0 + 15.0  # 15 s into the 31st step
    ob.attach_periods([("q1:a", t0 - 600.0, cut), ("q1:b", cut, t0 + 3600.0)], t0 + 4000.0)
    a, b = ob.nights["q1:a"], ob.nights["q1:b"]
    assert a[1] == pytest.approx(30 * 60.0 + 15.0) and b[1] == pytest.approx(29 * 60.0 + 45.0)
    total = sum(60.0 * i for i in range(60))
    assert a[0] == pytest.approx(sum(60.0 * i for i in range(30)) + 15.0 * 30)
    assert a[0] + b[0] == pytest.approx(total) and a[1] + b[1] == pytest.approx(3600.0)
    # A period that ends less than a step before now waits; the steps used are forgotten
    ob2 = RoomObserver()
    _tracked(ob2, t0, [1.0] * 60)
    ob2.attach_periods([("q1:c", t0, t0 + 3600.0)], t0 + 3630.0)
    assert ob2.nights == {}
    ob2.attach_periods([("q1:c", t0, t0 + 3600.0)], t0 + 3700.0)
    assert ob2.nights["q1:c"] == pytest.approx([3600.0, 3600.0, t0]) and ob2.steps == []


def test_late_selection_takes_the_steps_of_the_final_period() -> None:
    """The selection of a period is final only once it is closed: the steps wait in the buffer, and only the
    final period (here the longest piece after a gap, chosen at the close) gets them; nothing was summed under
    another key before."""
    ob = RoomObserver()
    t0 = 2_000_000.0
    _tracked(ob, t0, [0.0] * 120 + [2.0] * 300)
    assert ob.nights == {} and sum(len(ds) for _, ds in ob.steps) == 420
    # The period finally selected starts after a gap at 2 h
    final = ("q1:x", t0 + 7200.0, t0 + 420 * 60.0)
    ob.attach_periods([final], t0 + 420 * 60.0 + 60.0)
    assert ob.nights["q1:x"][:2] == pytest.approx([2.0 * 300 * 60.0, 300 * 60.0])
    # Saved and restored with the steps still pending
    ob3 = RoomObserver()
    _tracked(ob3, t0, [0.5] * 90)
    back = RoomObserver()
    back.load(json.loads(json.dumps(ob3.to_dict())))
    assert back.steps == ob3.steps and back.nights == {}
    # A gap in the tracking starts a new run of steps; the buffer is bounded
    _tracked(ob3, t0 + 90 * 60.0 + 600.0, [0.5] * 3)
    assert len(ob3.steps) == 2
    _tracked(ob3, t0 + 40 * 3600.0, [0.5])
    assert ob3.steps[0][0] >= t0 + 40 * 3600.0 - RM_STEPS_KEEP_S - 60.0


# ---------------------------------------------------------------------------
# Forecast scoring
# ---------------------------------------------------------------------------
def test_scoring_records_one_error_per_horizon_and_round_trips() -> None:
    s = ForecastScore()
    phi = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    gam = [[0.0] * 3 for _ in range(3)]
    t = 0.0
    for _ in range(int(13 * H / 60.0)):
        s.maybe_start(t, [20.0, 0.0, 0.0], 20.0)
        t += 60.0
        s.step(phi, gam, [0.0, 0.0, 0.0], 1.0)
        s.score(t, 20.5)
    sm = s.summary()
    assert sm["count"][0] == 13 and sm["count"][-1] == 2
    assert sm["rmse"][0] == pytest.approx(0.5)
    assert sm["unchanged_rmse"][0] == pytest.approx(0.5)
    back = ForecastScore.from_dict(s.to_dict())
    assert back.summary() == sm
    for _ in range(RM_SCORE_KEEP + 10):
        s._push(s.errors[0], 0.1)
    assert len(s.errors[0]) == RM_SCORE_KEEP


def test_observer_restarts_tracking_when_the_clock_goes_back(mass_run) -> None:
    ob = _observer(rsim.MASS)
    _replay(ob, mass_run, rsim.T0 + 600.0, rsim.T0 + 2 * 86400.0)
    assert ob.health() == HEALTH_OK
    t_old = ob.t
    back = t_old - 3600.0
    ob.observe(back, temp=ob.y_last, meas_key=None, meas_t=None, ext=ob.te, active=False)
    assert ob.x is None
    assert ob.health() == HEALTH_WAITING
    assert ob.wall_extra() is None
    ob.observe(back + 60.0, temp=ob.y_last, meas_key=None, meas_t=None, ext=ob.te, active=True)
    assert ob.t == back + 60.0
    assert ob.health() == HEALTH_SETTLING
    ob.observe(back + 660.0, temp=ob.y_last, meas_key=None, meas_t=None, ext=ob.te, active=True)
    assert ob.t > back + 600.0
