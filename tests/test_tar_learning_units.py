"""Unit tests of the T_ar learning modules: clock, history, policy, gates, identification rules."""

from __future__ import annotations

import datetime as dt
import json
import math

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tar_learning import gates, identification, policy, public
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock, OffsetClock
from custom_components.vtherm_smartpi.smartpi.tar_learning.const import (
    TAR_GAP_S,
    TAR_HISTORY_RETENTION_S,
)
from custom_components.vtherm_smartpi.smartpi.tar_learning.history import TarHistory

PARIS = "Europe/Paris"


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------
def test_offset_clock_maps_days_and_hours() -> None:
    c = OffsetClock(6.0)
    assert c.at(3, 6.0) == 3 * 86400.0
    assert c.at(3, 23.0) == 3 * 86400.0 + 17 * 3600.0
    assert c.at(4, -1.0) == c.at(3, 23.0)
    assert c.day_of(3 * 86400.0 + 17 * 3600.0) == 3
    assert c.tod_h(0.0) == 6.0


def test_local_clock_follows_daylight_saving() -> None:
    c = LocalClock(PARIS)
    day = dt.date(2026, 3, 29).toordinal()  # spring forward at 02:00
    t8 = c.at(day, 8.0)
    assert dt.datetime.fromtimestamp(t8, dt.timezone.utc).hour == 6  # 08:00 CEST
    assert c.tod_h(t8) == pytest.approx(8.0)
    assert c.day_of(t8) == day
    # 23:00 of the previous evening is 8 hours (not 9) before 08:00 on that night
    assert t8 - c.at(day - 1, 23.0) == 8 * 3600.0


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def _rec(h: TarHistory, t: float, temp=20.0, sp=19.0, **kw):
    return h.record(t, temp=temp, ext=kw.get("ext", 5.0), setpoint=sp, phase=kw.get("phase", "STABLE"),
                    b=kw.get("b", 0.002), dtc=kw.get("dtc"), lag=kw.get("lag"))


def test_history_keeps_changes_only_and_reports_setpoint_changes() -> None:
    h = TarHistory()
    assert _rec(h, 0.0, 20.0, 19.0) is None
    _rec(h, 60.0, 20.0, 19.0)
    _rec(h, 120.0, 20.1, 19.0)
    assert h.temp_t == [0.0, 120.0]
    assert _rec(h, 180.0, 20.1, 20.0) == 19.0
    assert h.setpoint_at(170.0) == 19.0 and h.setpoint_at(200.0) == 20.0
    assert len(h.params) == 1 and len(h.ext_t) == 1


def test_history_rebuilds_heater_edges_from_cycles() -> None:
    h = TarHistory()
    _rec(h, 0.0)
    h.applied_cycle(0.0, 0.25, 600.0, True)       # on 0 -> 150
    _rec(h, 300.0)
    h.applied_cycle(600.0, 1.0, 600.0, True)      # on 600 ...
    h.applied_cycle(1200.0, 0.5, 600.0, True)     # ... stays on -> 1500
    h.applied_cycle(1800.0, 0.0, 600.0, True)
    h.flush(2400.0)
    assert h.on == [0.0, 600.0]
    assert h.off == [150.0, 1500.0]
    # An early restart of the cycle while on keeps the heater on, a zero power cuts it
    h.applied_cycle(2400.0, 0.5, 600.0, True)
    h.applied_cycle(2500.0, 0.0, 600.0, True)
    assert h.on[-1] == 2400.0 and h.off[-1] == 2500.0


def test_history_gaps_prune_and_persistence() -> None:
    h = TarHistory()
    _rec(h, 0.0)
    _rec(h, 100.0, 20.2)
    _rec(h, 100.0 + TAR_GAP_S + 10.0, 20.3)
    assert h.gaps == [(100.0, 100.0 + TAR_GAP_S + 10.0)]
    assert h.gap_overlaps(50.0, 500.0)
    assert not h.gap_overlaps(0.0, 90.0)
    h.applied_cycle(5000.0, 0.5, 600.0, True)
    _rec(h, 5100.0, 20.4)
    restored = TarHistory.from_dict(h.to_dict())
    assert restored.temp_v == h.temp_v and restored.gaps == h.gaps
    # A heater on at save time is closed at the last observation when the restart is not
    # bridged (no open on period across it)
    restored.resolve_restart(False, 5200.0)
    assert len(restored.on) == len(restored.off) and restored.off[-1] == 5100.0
    later = TAR_HISTORY_RETENTION_S + 6000.0
    _rec(restored, later, 21.0)
    restored.prune(later)
    assert restored.temp_t == [later]
    assert len(restored.setpoints) == 1  # last setpoint before the cutoff is kept
    assert TarHistory.from_dict({"temp_t": [1.0], "temp_v": []}).is_empty()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_fast_publication_rule() -> None:
    assert policy.fast_publication([300.0, 310.0]) is None
    assert policy.fast_publication([300.0, 310.0, 320.0]) == 310.0
    assert policy.fast_publication([100.0, 300.0, 600.0]) is None  # MAD / median > 25 %
    assert policy.fast_publication([900.0, 900.0, 300.0, 310.0, 320.0, 330.0, 340.0]) == 320.0


def test_apply_rule_in_gain_units() -> None:
    """L_T = T / 2 against the reference L_b: never below L_b, alone up to 2 L_b, above only when confirmed."""
    # L_b <= L_T <= 2 L_b: the learned value is used (regulation T, gains T / 2)
    ap = policy.applied_value(400.0, True, 1000.0, None)
    assert (ap.value_s, ap.source, ap.gains_s, ap.reference_s, ap.out_of_bound, ap.limit) == (
        1000.0, "fast", 500.0, 400.0, False, None)
    ap = policy.applied_value(400.0, True, 800.0, None)
    assert (ap.source, ap.gains_s) == ("fast", 400.0)
    ap = policy.applied_value(400.0, True, 1600.0, None)
    assert (ap.source, ap.gains_s) == ("fast", 800.0)
    # the refined value is preferred
    ap = policy.applied_value(400.0, True, 1000.0, 1200.0)
    assert (ap.value_s, ap.source, ap.gains_s) == (1200.0, "refined", 600.0)
    # short side: the gains keep the reference, the regulation keeps the bootstrap
    ap = policy.applied_value(400.0, True, 700.0, None)
    assert (ap.value_s, ap.source, ap.gains_s, ap.out_of_bound, ap.limit) == (400.0, "bootstrap", 400.0, True, "short")
    assert (ap.in_force_s, ap.in_force_source) == (700.0, "fast")
    # long side without a second method: twice the reference
    ap = policy.applied_value(400.0, True, 2000.0, None)
    assert (ap.value_s, ap.source, ap.gains_s, ap.out_of_bound, ap.limit) == (400.0, "bootstrap", 800.0, True, "long")
    # long side confirmed by the window value within x1.25
    ap = policy.applied_value(400.0, True, 2000.0, None, window_s=1700.0)
    assert (ap.value_s, ap.source, ap.gains_s, ap.limit) == (2000.0, "fast", 1000.0, None)
    ap = policy.applied_value(400.0, True, 2000.0, None, window_s=1500.0)
    assert ap.limit == "long" and ap.gains_s == 800.0
    # the refined value above 2 L_b needs the fast value too
    ap = policy.applied_value(400.0, True, None, 2000.0, window_s=2000.0)
    assert ap.limit == "long"
    ap = policy.applied_value(400.0, True, 1800.0, 2000.0, window_s=2000.0)
    assert (ap.source, ap.gains_s) == ("refined", 1000.0)
    # Without a reliable bootstrap value nothing is applied
    ap = policy.applied_value(None, False, 300.0, None)
    assert ap.source == "bootstrap" and ap.value_s is None and not ap.out_of_bound and ap.gains_s is None


def test_apply_rule_reference() -> None:
    """The reference is the one given (the frozen bootstrap), not the current bootstrap dead time."""
    # A bootstrap fallen to 106 s under occupants: the frozen reference 230 s keeps the gains at 230 s
    ap = policy.applied_value(106.0, True, 300.0, None, reference_s=230.0)
    assert (ap.source, ap.gains_s, ap.value_s, ap.limit) == ("bootstrap", 230.0, 106.0, "short")
    # 500 s is within [230, 460] in gain units: used
    ap = policy.applied_value(106.0, True, 500.0, None, reference_s=230.0)
    assert (ap.source, ap.gains_s, ap.value_s) == ("fast", 250.0, 500.0)
    # slow emitter: bootstrap 450 s, morning 1950 s and window 1893 s agree
    ap = policy.applied_value(450.0, True, 1950.0, None, reference_s=450.0, window_s=1893.0)
    assert (ap.source, ap.gains_s) == ("fast", 975.0)
    ap = policy.applied_value(450.0, True, 1950.0, None, reference_s=450.0)
    assert (ap.source, ap.gains_s, ap.limit) == ("bootstrap", 900.0, "long")


def test_lower_bound_raises_but_is_never_confirmed() -> None:
    # A bound above the fast value raises it (veto of the shorter value)
    ap = policy.applied_value(400.0, True, 900.0, None, bound_s=1200.0)
    assert (ap.value_s, ap.source, ap.gains_s) == (1200.0, "lower_bound", 600.0)
    assert (ap.in_force_s, ap.in_force_source) == (900.0, "fast")
    # A bound alone stands for a missing value
    ap = policy.applied_value(400.0, True, None, None, bound_s=1000.0)
    assert (ap.source, ap.gains_s) == ("lower_bound", 500.0)
    # A bound below the value in force changes nothing
    ap = policy.applied_value(400.0, True, 1000.0, None, bound_s=900.0)
    assert (ap.source, ap.gains_s) == ("fast", 500.0)
    # A bound above 2 L_b is capped even when the fast value and the window agree
    ap = policy.applied_value(400.0, True, 1700.0, None, window_s=1700.0, bound_s=3000.0)
    assert (ap.source, ap.gains_s, ap.limit) == ("bootstrap", 800.0, "long")
    # A short bound alone keeps the reference
    ap = policy.applied_value(400.0, True, None, None, bound_s=500.0)
    assert (ap.gains_s, ap.limit) == (400.0, "short")
    assert policy.bound_publication([]) is None
    assert policy.bound_publication([600.0, 900.0, 700.0]) == 700.0
    assert policy.bound_publication([100.0, 600.0, 900.0, 700.0, 800.0, 750.0]) == 750.0


def test_methods_agree_tolerance() -> None:
    assert policy.methods_agree(1950.0, 1893.0)
    assert policy.methods_agree(2310.0, 1893.0)
    assert policy.methods_agree(1000.0, 1240.0)
    assert not policy.methods_agree(1000.0, 1260.0)
    assert not policy.methods_agree(500.0, 190.0)
    assert not policy.methods_agree(None, 1000.0) and not policy.methods_agree(1000.0, None)
    assert public.limit_reason(None) is None
    assert "twice the reference" in public.limit_reason("long")
    assert "keep the reference" in public.limit_reason("short")


def test_regulation_view_never_touches_the_estimator() -> None:
    dt_est = DeadTimeEstimator()
    dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable = 480.0, True
    dt_est.deadtime_cool_s, dt_est.deadtime_cool_reliable = 900.0, True
    view = policy.RegulationDeadTime(dt_est, 300.0)
    assert view.deadtime_heat_s == 300.0
    assert view.active_deadtime(VThermHvacMode_HEAT) == (300.0, True)
    assert view.passive_deadtime(VThermHvacMode_HEAT) == (900.0, True)
    assert view.active_deadtime(VThermHvacMode_COOL) == (900.0, True)
    assert view.deadtime_cool_s == 900.0 and view.deadtime_heat_reliable is True
    assert dt_est.deadtime_heat_s == 480.0


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def test_g1_and_gt4() -> None:
    t_sp = 2 * 86400.0
    reg = t_sp - 7 * 3600.0  # regulated start 7 h before the step
    regulated = [reg + 1200.0 * i for i in range(22)]
    obs = gates.gate_observables(regulated, t_sp, t_sp + 60.0, reg)
    assert obs["max_idle_h"] < 1.0 and not obs["passive_2h"]
    assert gates.unconfirmed("G1", obs, 300.0, None) is None
    passive = [reg, reg + 600.0, t_sp + 60.0]
    obs = gates.gate_observables(passive, t_sp, t_sp + 60.0, reg)
    assert obs["max_idle_h"] > 1.0 and obs["passive_2h"]
    assert gates.unconfirmed("G1", obs, 1200.0, None) == "idle in the regulated part"
    assert gates.unconfirmed("GT4", obs, 1200.0, 0.9) is None
    assert gates.unconfirmed("GT4", obs, 600.0, 0.9) is not None
    assert gates.unconfirmed("GT4", obs, 1200.0, 0.5) is not None
    with pytest.raises(ValueError):
        gates.unconfirmed("other", obs, 1.0, None)


def test_g1_never_takes_an_unknown_as_a_favourable_zero() -> None:
    """No regulated start (room never at its low setpoint), or a regulated part under 4 h: unconfirmed."""
    t_sp = 2 * 86400.0
    starts = [t_sp - 3 * 3600.0 + 600.0 * i for i in range(18)]
    obs = gates.gate_observables(starts, t_sp, None, None)
    assert obs["max_idle_h"] is None and obs["regulated_h"] is None
    assert gates.unconfirmed("G1", obs, 300.0, None) == "no regulated start"
    obs = gates.gate_observables(starts, t_sp, None, t_sp - 3 * 3600.0)
    assert obs["max_idle_h"] is None and obs["regulated_h"] == pytest.approx(3.0)
    assert gates.unconfirmed("G1", obs, 300.0, None) == "regulated part too short"
    # An observable missing from a record is unknown, never zero
    assert gates.unconfirmed("G1", {}, 300.0, None) is not None
    assert gates.unconfirmed("G1", {"regulated_start": 0.0, "regulated_h": 5.0}, 300.0, None) is not None


def test_quarters_merge_short_tail() -> None:
    assert gates.quarters(0.0, 3600.0 + 600.0) == [(0.0, 1800.0), (1800.0, 4200.0)]
    assert gates.quarters(0.0, 3600.0 + 1200.0)[-1] == (3600.0, 4800.0)


def test_window_regulated_needs_every_night() -> None:
    nights = {"a": {"ok": True}, "b": {"ok": True}, "c": {"ok": True}, "d": {"ok": False}}
    assert gates.window_regulated(nights, ["a", "b", "c"])
    assert not gates.window_regulated(nights, ["b", "c", "d"])
    assert not gates.window_regulated(nights, ["a", "b", "x"])  # a night never gated is not regulated
    assert not gates.window_regulated(nights, [])


# ---------------------------------------------------------------------------
# Identification rules
# ---------------------------------------------------------------------------
def _mass(k, tw, te):
    return {"ok": True, "cls": "mass", "p2": {"k": k, "tau_w": tw, "b": 1e-5, "a": 1e-3, "tau_e": te},
            "p1": {"b": 1e-5, "a": 1e-3, "tau_e": te}, "dbic": 50.0, "rmse1": 0.05, "q_obs": 0.01}


def test_evaluate_day_mass_and_tau_e() -> None:
    e = identification.evaluate_day(_mass(7e-5, 1.2e5, 290.0), _mass(7.2e-5, 1.3e5, 300.0), 1e-5)
    assert e["cls"] == "mass" and e["te_ok"]
    assert e["te"] == pytest.approx(math.sqrt(290.0 * 300.0))
    e = identification.evaluate_day(_mass(7e-5, 1.2e5, 290.0), _mass(1e-4, 1.3e5, 300.0), 1e-5)
    assert e["cls"] == "unknown" and e["why_mass"] == "k unstable"
    e = identification.evaluate_day(_mass(7e-5, 1.2e5, 200.0), _mass(7e-5, 1.2e5, 400.0), 1e-5)
    assert e["cls"] == "mass" and not e["te_ok"] and e["why_te"] == "tau_e unstable"


def test_class_in_force_resets_on_disagreement() -> None:
    f = identification.class_in_force
    assert f([], 5) == "unknown"
    assert f([(6, "mass")], 7) == "mass"
    assert f([(6, "mass")], 6) == "unknown"
    assert f([(6, "mass"), (7, "one")], 8) == "unknown"
    assert f([(6, "mass"), (7, "one"), (8, "one")], 9) == "one"
    assert f([(6, "mass"), (7, "unknown"), (8, "mass")], 9) == "mass"


def test_te_in_force_keeps_latest_publication() -> None:
    evs = [{"day": 6, "t": 6, "cls": "mass", "te_ok": True, "te": 300.0},
           {"day": 7, "t": 7, "cls": "unknown", "te_ok": False, "te": None}]
    assert identification.te_in_force(evs, 6) is None
    assert identification.te_in_force(evs, 7)["value"] == 300.0
    assert identification.te_in_force(evs, 8)["value"] == 300.0
    silenced = identification.silence({"cls": "mass", "te": 300.0, "te_ok": True, "day": 6, "t": 6},
                                      "not regulated")
    assert silenced["cls"] == "unknown" and silenced["te_raw"] == 300.0
    assert identification.te_in_force([silenced], 7) is None


# ---------------------------------------------------------------------------
# Comfort step cut by sunrise
# ---------------------------------------------------------------------------
def _row(T: float, win: float, cut) -> dict:
    """A passive-night comfort step that passes every rule but the window ones."""
    from custom_components.vtherm_smartpi.smartpi.tar_learning.morning import FW

    return {"night": "passive", "rms_base": 0.005, "q": 0.01, "sensor_period_s": 60.0, "cut": cut,
            "win_s": {"Wsp": win}, "energy_s": {"Wsp": 3 * win},
            "fit": {FW: {"T": T, "a_per_min": 0.1, "rms": 0.01, "edge": False}, "rise": {"Wsp": 1.0}}}


def test_a_slow_emitter_cut_by_sunrise_gives_a_lower_bound() -> None:
    """A 2300 s emitter cut 1600 s after its start fits a short delay (900 s) that passes its own window rule;
    its bootstrap dead time (1150 s, first moment about 2300 s) makes it a lower bound, never a value."""
    from custom_components.vtherm_smartpi.smartpi.tar_learning import jobs, morning

    assert morning.gate_night_class(_row(900.0, 1600.0, None)) == (True, "accepted")  # not cut
    floor = jobs.delay_floor(None, None, 1150.0)
    assert floor == pytest.approx(2300.0)
    assert morning.gate_night_class(_row(900.0, 1600.0, {"floor": floor})) == (False, morning.CUT_BOUND_REASON)
    dec = morning.decide(_row(900.0, 1600.0, {"floor": floor}))
    assert dec["acc"] is False and dec["T"] is None
    assert dec["cut_T"] == 900.0 and dec["bound"] == pytest.approx(0.6 * 900.0)
    # the refined value, the published fast value and the lower bound in force raise the floor too
    assert jobs.delay_floor({"value": 2500.0}, 1800.0, 400.0) == 2500.0
    assert jobs.delay_floor(None, 1800.0, 400.0) == 1800.0
    assert jobs.delay_floor(None, 1800.0, 400.0, 2000.0) == 2000.0
    # no reference: the window must hold three times the fitted value
    assert morning.gate_night_class(_row(900.0, 1600.0, {"floor": None}))[1] == morning.CUT_BOUND_REASON
    assert morning.gate_night_class(_row(500.0, 1600.0, {"floor": None})) == (True, "accepted")
    assert public.morning_reason("excitation: cut by sunrise") == (
        "comfort step cut by sunrise before its response could be measured")
    assert "lower bound" in public.morning_reason(morning.CUT_BOUND_REASON)


def test_a_censored_step_that_fails_another_gate_is_refused_as_cut() -> None:
    from custom_components.vtherm_smartpi.smartpi.tar_learning import morning

    row = _row(900.0, 1600.0, {"floor": 2300.0})
    row["fit"]["rise"]["Wsp"] = 0.05  # rise too small
    assert morning.gate_night_class(row) == (False, morning.CUT_REASON)
    assert "bound" not in morning.decide(row)
    # its own window rule (1.5 T) still applies
    assert morning.gate_night_class(_row(1200.0, 1600.0, {"floor": 2300.0})) == (False, morning.CUT_REASON)


def test_a_fast_emitter_cut_late_is_accepted() -> None:
    from custom_components.vtherm_smartpi.smartpi.tar_learning import jobs, morning

    floor = jobs.delay_floor(None, None, 312.0)
    assert morning.gate_night_class(_row(300.0, 5400.0, {"floor": floor})) == (True, "accepted")
    # the window must hold three times the largest of the fit and the floor for an estimate
    assert morning.gate_night_class(_row(300.0, 1800.0, {"floor": floor})) == (False, morning.CUT_BOUND_REASON)
    assert morning.gate_night_class(_row(300.0, 1900.0, {"floor": floor})) == (True, "accepted")
    # the regulated branch applies the same rule
    from custom_components.vtherm_smartpi.smartpi.tar_learning.morning import FW

    x = {"T": 300.0, "a_per_min": 0.1, "rms": 0.01, "edge": False}
    assert morning._gate_fit(x, 0.01, 5400.0, 9000.0, 1.0, 60.0, {"floor": floor}) == (True, "accepted")
    assert morning._gate_fit(x, 0.01, 1600.0, 9000.0, 1.0, 60.0, {"floor": 2300.0})[1] == morning.CUT_BOUND_REASON
    assert FW


@pytest.mark.parametrize("T, win, estimate", [(690.0, 3185.0, True), (1560.0, 3274.0, False)])
def test_salon_steps_cut_by_sunrise(T: float, win: float, estimate: bool) -> None:
    """The two salon steps of the field week cut by sunrise (fitted 690 s and 1560 s, windows of 53 and 55 min).

    With the bootstrap dead time only (458 s, first moment about 916 s), the 690 s fit holds three times that
    reference and stays an estimate; the 1560 s fit does not and is a lower bound. Once a value near the uncut
    fit (2310 s) is in force, both are lower bounds."""
    from custom_components.vtherm_smartpi.smartpi.tar_learning import jobs, morning

    floor = jobs.delay_floor(None, None, 458.0)
    if estimate:
        assert morning.gate_night_class(_row(T, win, {"floor": floor})) == (True, "accepted")
    else:
        assert morning.gate_night_class(_row(T, win, {"floor": floor})) == (False, morning.CUT_BOUND_REASON)
        assert morning.decide(_row(T, win, {"floor": floor}))["bound"] == pytest.approx(0.6 * T)
    assert morning.gate_night_class(_row(T, win, {"floor": jobs.delay_floor(None, 2310.0, 458.0)})) == (
        False, morning.CUT_BOUND_REASON)

# ---------------------------------------------------------------------------
# Reference of the apply rule, lower bounds and the stored state
# ---------------------------------------------------------------------------
def _manager():
    from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager

    m = TarLearningManager("unit", "UTC")
    m.set_dependencies(True)
    return m


_CONFIRMED = {"regulated_start": 0.0, "regulated_h": 6.0, "max_idle_h": 0.3, "passive_2h": False}


def _censored(nid: str, T: float, confirmed: bool = True) -> dict:
    obs = _CONFIRMED if confirmed else dict(_CONFIRMED, max_idle_h=2.0)
    return {"t_sp": 1000.0, "t_s": 1010.0, "accepted": False, "T": None,
            "why": "excitation: cut by sunrise, lower bound", "cut_T": T, "bound_s": 0.6 * T, "night_id": nid,
            "gate_obs": obs, "night_summary": {"night_id": nid}}


def test_reference_is_frozen_at_the_first_reliable_bootstrap() -> None:
    from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager

    off = TarLearningManager("unit", "UTC")  # dependencies not checked yet: nothing is frozen
    off.applied(300.0, True)
    assert off.reference_heat_s is None
    m = _manager()
    m.applied(300.0, False)
    assert m.reference_heat_s is None
    m.applied(300.0, True)
    assert m.reference_heat_s == 300.0 and m.dirty
    m.fast_pub = {"value_s": 500.0, "day": 0}
    # A bootstrap that falls (sleepers warming the room before the heater starts) never lowers the reference
    ap = m.applied(106.0, True)
    assert m.reference_heat_s == 300.0 and m.bootstrap_heat_s == 106.0
    assert (ap.reference_s, ap.gains_s, ap.source, ap.limit) == (300.0, 300.0, "bootstrap", "short")
    # A larger current bootstrap is the reference
    ap = m.applied(400.0, True)
    assert m.reference_heat_s == 300.0 and ap.reference_s == 400.0
    diag = m.diagnostics(400.0, True)
    assert diag["reference_s"] == 400.0 and diag["gains_s"] == 400.0 and diag["out_of_bound"] is True
    m.reset()
    assert m.reference_heat_s is None


def test_window_value_for_the_agreement() -> None:
    m = _manager()
    assert m.window_value() is None
    m.evals = [{"te_ok": True, "te": 1700.0}, {"te_ok": False, "te": None, "te_ok_raw": True, "te_raw": 1893.0},
               {"te_ok": False, "te": None}]
    assert m.window_value() == 1893.0
    m.te_pub = {"value": 2000.0}
    assert m.window_value() == 2000.0
    m.te_pub = None
    m.fast_pub = {"value_s": 1950.0, "day": 0}
    m.applied(450.0, True)
    ap = m.applied(450.0, True)
    assert (ap.source, ap.gains_s, ap.limit) == ("fast", 975.0, None)


def test_confirmed_steps_cut_by_sunrise_give_lower_bounds_only() -> None:
    m = _manager()
    m._apply_morning({"t_sp": 1000.0}, _censored("q1:a:1", 2000.0))
    m._apply_morning({"t_sp": 1000.0}, _censored("q1:a:2", 1500.0, confirmed=False))
    m._apply_morning({"t_sp": 1000.0}, _censored("q1:a:3", 2500.0))
    # the same period evaluated again replaces its bound
    m._apply_morning({"t_sp": 1000.0}, _censored("q1:a:3", 3000.0))
    assert m.fast_values == [] and m.fast_pub is None and m.fast_count == 0
    assert m.bound_values == [["q1:a:1", 1200.0], ["q1:a:3", 1800.0]] and m.bound_count == 2
    assert m.bound_value() == 1500.0
    assert m.mornings[1]["confirmed"] is False and m.mornings[0]["bound_s"] == 1200.0
    diag = m.diagnostics(500.0, True)
    assert diag["fast"]["lower_bound_s"] == 1500.0 and diag["fast"]["lower_bound_mornings"] == 2
    assert diag["fast"]["last_morning"]["reason"] == (
        "comfort step cut by sunrise: kept as a lower bound of the delay, not as a value")
    # 1500 s raises the gains to 750 s, within twice the 500 s reference
    assert (diag["source"], diag["gains_s"], diag["applied_s"]) == ("lower_bound", 750.0, 1500.0)


def test_state_round_trip_and_migration_of_the_earlier_format() -> None:
    m = _manager()
    m.applied(300.0, True)
    m.applied(250.0, True)
    m._apply_morning({"t_sp": 1000.0}, _censored("q1:a:1", 2000.0))
    state = json.loads(json.dumps(m.save_state()))
    assert state["version"] == 3
    back = _manager()
    back.load_state(state)
    assert (back.reference_heat_s, back.bootstrap_heat_s) == (300.0, 250.0)
    assert back.bound_values == m.bound_values and back.bound_count == 1 and back.bound_ids == ["q1:a:1"]
    # Format 2: no frozen reference and no bounds; the last reliable bootstrap becomes the reference
    v2 = {k: v for k, v in state.items() if k not in ("reference_heat_s", "bound_values", "bound_ids", "bound_count")}
    v2["version"] = 2
    v2["fast_pub"] = {"value_s": 640.0, "day": 3}
    old = _manager()
    old.load_state(v2)
    assert old.reference_heat_s == 250.0 and old.bound_values == [] and old.bound_count == 0
    assert old.fast_pub == {"value_s": 640.0, "day": 3} and old.dirty
    assert old.save_state()["version"] == 3
    # Corrupt bounds are dropped, the rest is kept; a later format is ignored
    bad = dict(state, bound_values=[["q1:a:1", "x"]])
    b2 = _manager()
    b2.load_state(bad)
    assert b2.bound_values == [] and b2.reference_heat_s == 300.0
    later = _manager()
    later.load_state(dict(state, version=4))
    assert later.reference_heat_s is None and later.bound_values == []
