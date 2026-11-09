"""Unit tests of the T_ar learning modules: clock, history, policy, gates, identification rules."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tar_learning import gates, identification, policy
from custom_components.vtherm_smartpi.smartpi.tar_learning.clock import LocalClock, OffsetClock
from custom_components.vtherm_smartpi.smartpi.tar_learning.const import (
    TAR_APPLY_BOUND_HIGH,
    TAR_APPLY_BOUND_LOW,
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


def test_applied_value_prefers_refined_and_respects_bound() -> None:
    ap = policy.applied_value(400.0, True, 300.0, None)
    assert (ap.value_s, ap.source, ap.out_of_bound) == (300.0, "fast", False)
    ap = policy.applied_value(400.0, True, 300.0, 350.0)
    assert (ap.value_s, ap.source) == (350.0, "refined")
    ap = policy.applied_value(400.0, True, 400.0 * TAR_APPLY_BOUND_HIGH * 1.01, None)
    assert (ap.value_s, ap.source, ap.out_of_bound) == (400.0, "bootstrap", True)
    ap = policy.applied_value(400.0, True, 400.0 * TAR_APPLY_BOUND_LOW * 0.99, None)
    assert ap.out_of_bound and ap.value_s == 400.0
    ap = policy.applied_value(400.0, True, 400.0 * TAR_APPLY_BOUND_LOW, None)
    assert ap.source == "fast"
    # Without a reliable bootstrap value nothing is applied
    ap = policy.applied_value(None, False, 300.0, None)
    assert ap.source == "bootstrap" and ap.value_s is None and not ap.out_of_bound


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
    c = OffsetClock(6.0)
    t_sp = 2 * 86400.0
    t23 = c.at(1, 23.0)
    regulated = [t23 + 1200.0 * i for i in range(22)]
    obs = gates.gate_observables(c, regulated, t_sp, t_sp + 60.0)
    assert obs["max_idle_sleep_h"] < 1.0 and not obs["passive_2h"]
    assert gates.unconfirmed("G1", obs, 300.0, None) is None
    passive = [t23 + 600.0, t_sp + 60.0]
    obs = gates.gate_observables(c, passive, t_sp, t_sp + 60.0)
    assert obs["max_idle_sleep_h"] > 1.0 and obs["passive_2h"]
    assert gates.unconfirmed("G1", obs, 1200.0, None) is not None
    assert gates.unconfirmed("GT4", obs, 1200.0, 0.9) is None
    assert gates.unconfirmed("GT4", obs, 600.0, 0.9) is not None
    assert gates.unconfirmed("GT4", obs, 1200.0, 0.5) is not None
    with pytest.raises(ValueError):
        gates.unconfirmed("other", obs, 1.0, None)


def test_quarters_merge_short_tail() -> None:
    assert gates.quarters(0.0, 3600.0 + 600.0) == [(0.0, 1800.0), (1800.0, 4200.0)]
    assert gates.quarters(0.0, 3600.0 + 1200.0)[-1] == (3600.0, 4800.0)


def test_window_regulated_needs_every_night() -> None:
    nights = {1: {"ok": True}, 2: {"ok": True}, 3: {"ok": True}, 4: {"ok": False}}
    assert gates.window_regulated(nights, 4, 3)
    assert not gates.window_regulated(nights, 5, 3)


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
    evs = [{"day": 6, "cls": "mass", "te_ok": True, "te": 300.0},
           {"day": 7, "cls": "unknown", "te_ok": False, "te": None}]
    assert identification.te_in_force(evs, 6) is None
    assert identification.te_in_force(evs, 7)["value"] == 300.0
    assert identification.te_in_force(evs, 8)["value"] == 300.0
    silenced = identification.silence({"cls": "mass", "te": 300.0, "te_ok": True, "day": 6}, "not regulated")
    assert silenced["cls"] == "unknown" and silenced["te_raw"] == 300.0
    assert identification.te_in_force([silenced], 7) is None
