"""Data-quality rules of the T_ar learning history: every rule marks an invalid interval."""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.vtherm_smartpi.smartpi.tar_learning import jobs
from custom_components.vtherm_smartpi.smartpi.tar_learning.const import TAR_GAP_S
from custom_components.vtherm_smartpi.smartpi.tar_learning.history import TarHistory
from custom_components.vtherm_smartpi.smartpi.tar_learning.manager import TarLearningManager


def _obs(h: TarHistory, t: float, temp=20.0, ext=5.0, **kw):
    return h.record(t, temp=temp, ext=ext, setpoint=kw.pop("sp", 20.0), phase="STABLE", b=0.002, dtc=None,
                    lag=None, **kw)


def _run(h: TarHistory, t0: float, t1: float, step: float = 60.0, **kw) -> None:
    t = t0
    while t <= t1:
        _obs(h, t, **kw)
        t += step


def _invalid(h: TarHistory, a: float, b: float) -> bool:
    return any(g0 < b and g1 > a for g0, g1 in h.invalid_intervals())


def _clean(h: TarHistory, t0: float, t1: float, start: float = 20.0) -> None:
    """Valid samples: a precise sensor rising by 0.01 C per minute, measured each minute."""
    t, v = t0, start
    while t <= t1:
        _obs(h, t, temp=v)
        t += 60.0
        v += 0.01


def test_clean_data_holds_no_invalid_interval() -> None:
    h = TarHistory()
    _clean(h, 0.0, 6 * 3600.0)
    assert h.invalid_intervals() == []


# 1. gaps and unavailable values -------------------------------------------------
def test_observation_gap_is_invalid_and_never_bridged() -> None:
    h = TarHistory()
    _clean(h, 0.0, 3600.0)
    _clean(h, 3600.0 + TAR_GAP_S + 60.0, 3 * 3600.0, start=21.0)
    assert h.gaps == [(3600.0, 3600.0 + TAR_GAP_S + 60.0)]
    assert h.gap_overlaps(3000.0, 4000.0)
    assert not h.gap_overlaps(600.0, 3000.0)


@pytest.mark.parametrize("field", ["temp", "ext"])
def test_unavailable_temperature_is_invalid(field: str) -> None:
    h = TarHistory()
    _clean(h, 0.0, 3600.0)
    missing = {"temp": None} if field == "temp" else {"temp": 20.6, "ext": None}
    _obs(h, 3660.0, **missing)
    _obs(h, 3720.0, **missing)
    assert h.invalid_reason == ("indoor" if field == "temp" else "outdoor") + " temperature unavailable"
    _obs(h, 3780.0, temp=20.7)
    assert h.gaps == [(3660.0, 3780.0)]
    assert h.invalid_since is None


def test_unknown_command_is_invalid() -> None:
    h = TarHistory()
    _clean(h, 0.0, 600.0)
    _obs(h, 660.0, excluded="command unknown")
    _obs(h, 720.0, temp=20.2)
    assert h.gaps == [(660.0, 720.0)]


def test_identification_bins_and_regulated_nights_respect_invalid_intervals() -> None:
    gaps = [(6000.0, 7000.0)]
    mask = jobs._valid_mask(gaps, 0.0)(np.arange(0.0, 9000.0, 600.0), 600.0)
    assert list(np.nonzero(~mask)[0]) == [10, 11]  # bins [6000, 6600) and [6600, 7200)
    ok = jobs._valid_fn(gaps, 0.0, 20000.0)
    assert not ok(5000.0, 8000.0) and ok(7000.0, 9000.0)
    assert jobs._valid_until(gaps)(1000.0, 9000.0) == 6000.0


# 2. restarts and downtime ----------------------------------------------------------
def test_restart_not_bridged_marks_only_its_downtime_invalid() -> None:
    h = TarHistory()
    _clean(h, 0.0, 3600.0)
    restored = TarHistory.from_dict(h.to_dict())
    _obs(restored, 3900.0, temp=20.62)  # 5 min later, no restart decision: not bridged
    assert restored.gaps == [(3600.0, 3900.0)]
    assert restored.gap_overlaps(3700.0, 3800.0)
    assert not restored.gap_overlaps(3000.0, 3600.0)
    assert not restored.gap_overlaps(3900.0, 4500.0)
    assert restored.restarts == [(3600.0, 3900.0, False)]


def test_invalid_period_open_at_save_is_kept_after_restart() -> None:
    h = TarHistory()
    _clean(h, 0.0, 600.0)
    _obs(h, 660.0, excluded="window open")
    restored = TarHistory.from_dict(h.to_dict())
    assert (660.0, 660.0) not in restored.gaps
    assert any(a == 660.0 for a, _ in restored.gaps)


# 3. physically impossible spike ----------------------------------------------------------
def test_spike_is_dropped_and_marked_invalid() -> None:
    h = TarHistory()
    _clean(h, 0.0, 1200.0)
    n = len(h.temp_v)
    _obs(h, 1230.0, temp=35.0)
    assert len(h.temp_v) == n and h.invalid_reason == "temperature jump"
    _obs(h, 1260.0, temp=20.22)
    assert 35.0 not in h.temp_v
    assert h.gap_overlaps(1230.0, 1260.0)


def test_confirmed_new_level_is_kept() -> None:
    h = TarHistory()
    _clean(h, 0.0, 600.0)
    for k, t in enumerate((630.0, 640.0, 650.0)):
        _obs(h, t, temp=23.0 + 0.01 * k)
    assert h.temp_v[-1] == 23.02
    assert h.gap_overlaps(630.0, 650.0)


@pytest.mark.parametrize("step_c, rate_c_per_h, period_s", [(0.1, 6.0, 30.0), (0.01, 6.0, 10.0), (0.1, 12.0, 10.0)])
def test_a_fast_real_morning_step_is_never_cut(step_c: float, rate_c_per_h: float, period_s: float) -> None:
    """A strong heating raising the room by up to 12 C/h, seen by a 0.1 C or a 0.01 C sensor."""
    h = TarHistory()
    _clean(h, 0.0, 3600.0)
    base = h.temp_v[-1]
    t = 3600.0
    while t < 3600.0 + 7200.0:
        t += period_s
        true = base + rate_c_per_h * (t - 3600.0) / 3600.0
        _obs(h, t, temp=round(round(true / step_c) * step_c, 3))
    assert h.invalid_intervals() == []
    assert h.temp_v[-1] > base + rate_c_per_h * 1.9


# 4. periods when SmartPI is not in control ------------------------------------------------
@pytest.mark.parametrize("reason", ["window open", "power shedding", "safety mode", "command overridden"])
def test_excluded_period_is_invalid(reason: str) -> None:
    h = TarHistory()
    _clean(h, 0.0, 3600.0)
    _run(h, 3660.0, 5400.0, excluded=reason, temp=20.6)
    assert h.invalid_reason == reason
    _obs(h, 5460.0, temp=20.61)
    assert h.gaps == [(3660.0, 5460.0)]


def test_manager_records_hvac_off_as_invalid_and_rejects_the_morning() -> None:
    m = TarLearningManager("dq", "UTC")
    m.set_dependencies(True)
    t0 = 1_767_225_600.0  # 2026-01-01 00:00 UTC

    def tick(t, mode=VThermHvacMode_HEAT, sp=18.0, excluded=None):
        m.observe(t, hvac_mode=mode, temp=19.0 + (t - t0) / 360000.0, ext=3.0, setpoint=sp, phase="Stable",
                  b=0.002, dtc=900.0, dtc_reliable=True, lag=300.0, cycle_min=10.0, excluded=excluded)

    t = t0
    while t < t0 + 5 * 3600.0:
        tick(t, mode=VThermHvacMode_OFF if t0 + 4 * 3600.0 <= t < t0 + 4.5 * 3600.0 else VThermHvacMode_HEAT)
        t += 60.0
    while t < t0 + 10 * 3600.0:
        tick(t, sp=20.0)  # comfort step at 05:00 local
        t += 60.0
    assert m.history.gaps[0][0] == t0 + 4 * 3600.0
    assert len(m.pending) == 1
    assert m._next_job(t) is None or m._next_job(t)[0] != "morning"
    assert m.mornings[-1]["why"] == "data gap around the step"


def test_clock_going_back_starts_a_new_origin_without_data_on_the_interval() -> None:
    h = TarHistory()
    _clean(h, 3600.0, 7200.0)
    # The clock goes back by one hour: the observations are not frozen until it catches up.
    _clean(h, 5400.0, 5400.0 + 1800.0, start=21.0)
    assert h.last_t == 5400.0 + 1800.0
    assert h.temp_t == sorted(h.temp_t)
    assert max(h.temp_t) <= h.last_t
    assert h.temp_v[-1] > 21.2
    # Back before the first observation: nothing of the old origin is kept.
    _clean(h, 600.0, 1200.0, start=19.0)
    assert h.first_t == 600.0 and h.last_t == 1200.0
    assert min(h.temp_t) >= 600.0 and max(h.temp_t) <= 1200.0
    assert h.invalid_intervals() == []


def test_clock_going_back_closes_the_heater_state() -> None:
    h = TarHistory()
    _clean(h, 0.0, 600.0)
    h.applied_cycle(600.0, 1.0, 600.0, True)
    _clean(h, 660.0, 1200.0, start=20.7)
    _obs(h, 300.0, temp=21.0)
    assert h.on == [] and h.off == []
    assert h._heater_on is False
