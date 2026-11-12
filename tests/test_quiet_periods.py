"""Quiet periods: darkness from the solar position, plateaus, episodes, keys and regulated start."""

from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from custom_components.vtherm_smartpi.smartpi import quiet_periods as qp
from custom_components.vtherm_smartpi.smartpi.quiet_periods import solar
from custom_components.vtherm_smartpi.smartpi.quiet_periods.const import (
    DARK_ELEVATION_DEG,
    DARK_TIME_MARGIN_S,
    REGULATED_MIN_S,
    SUN_STANDARD_HORIZON_DEG,
)
from custom_components.vtherm_smartpi.smartpi.tar_learning import gates

H = 3600.0
PARIS = qp.Position(48.85, 2.35)
TZ = ZoneInfo("Europe/Paris")


def _utc(y, mo, d, h=0, mi=0) -> float:
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def _local(y, mo, d, h=0.0) -> float:
    return (dt.datetime(y, mo, d, tzinfo=TZ) + dt.timedelta(hours=h)).timestamp()


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------
def test_position_validation() -> None:
    assert qp.validate_position(0, 0) == qp.Position(0.0, 0.0)
    assert qp.validate_position(-90.0, 180.0) is not None
    for lat, lon in ((None, 2.0), (48.0, None), ("48", 2.0), (91.0, 0.0), (0.0, -181.0), (float("inf"), 0.0),
                     (float("nan"), 0.0), (True, 0.0), (object(), 0.0)):
        assert qp.validate_position(lat, lon) is None


# ---------------------------------------------------------------------------
# Darkness
# ---------------------------------------------------------------------------
# Published sunrise and sunset (UTC, to the minute) of almanacs; the darkness must end before the sunrise and
# start after the sunset, by the refraction margin plus the time margin (a few minutes, at most 12).
ALMANAC = [
    # London, summer solstice: sunrise 04:43 BST, sunset 21:21 BST
    (qp.Position(51.5074, -0.1278), dt.date(2023, 6, 21), (3, 43), (20, 21)),
    # Sydney, winter solstice (southern hemisphere): sunrise 07:00 AEST, sunset 16:53 AEST
    (qp.Position(-33.8688, 151.2093), dt.date(2023, 6, 21), (21, 0), (6, 53)),
]


@pytest.mark.parametrize("pos, day, rise, sset", ALMANAC)
def test_darkness_against_almanac(pos, day, rise, sset) -> None:
    t0 = _utc(day.year, day.month, day.day) - 86400.0
    darks = qp.dark_intervals(pos, t0, t0 + 3 * 86400.0)
    ends = [b for _, b in darks]
    starts = [a for a, _ in darks]

    def near(ts, h, m):
        ref = [_utc(day.year, day.month, day.day, h, m) + k * 86400.0 for k in (-1, 0, 1)]
        return min((t - r for t in ts for r in ref), key=abs)

    assert -12 * 60.0 <= near(ends, *rise) <= -DARK_TIME_MARGIN_S  # ends before the sunrise
    assert DARK_TIME_MARGIN_S <= near(starts, *sset) <= 12 * 60.0  # starts after the sunset


@pytest.mark.parametrize("lat, lon", [(48.85, 2.35), (-33.9, 151.2), (0.0, -78.5), (64.1, -21.9), (37.8, -122.4)])
@pytest.mark.parametrize("month", [3, 6, 9, 12])
def test_no_instant_is_called_dark_with_the_sun_up(lat, lon, month) -> None:
    """Every instant of a darkness has the centre of the sun below the standard horizon (whole disc down)."""
    from custom_components.vtherm_smartpi.smartpi.quiet_periods.solar import sun_elevation

    pos = qp.Position(lat, lon)
    t0 = _utc(2028, month, 20)  # 2028 is a leap year
    rnd = random.Random(month)
    for a, b in qp.dark_intervals(pos, t0, t0 + 2 * 86400.0):
        for t in [a, b - 0.1] + [rnd.uniform(a, b) for _ in range(30)]:
            assert sun_elevation(pos, t) < SUN_STANDARD_HORIZON_DEG
        # the margin is real at both crossings of the horizon
        assert sun_elevation(pos, a) < DARK_ELEVATION_DEG + 0.05
        assert sun_elevation(pos, b) < DARK_ELEVATION_DEG + 0.05


def test_darkness_does_not_depend_on_the_time_zone_or_the_call_range() -> None:
    t0 = _utc(2026, 3, 28)
    full = qp.dark_intervals(PARIS, t0, t0 + 4 * 86400.0)  # across the change to summer time
    part = qp.dark_intervals(PARIS, t0 + 1.5 * 86400.0, t0 + 2.5 * 86400.0)
    assert set(part) <= set(full)
    assert len(full) == 5


def test_polar_day_has_no_darkness_and_polar_night_is_cut_in_blocks() -> None:
    tromso = qp.Position(69.65, 18.96)
    assert qp.dark_intervals(tromso, _utc(2026, 6, 21), _utc(2026, 6, 22)) == []
    blocks = qp.dark_intervals(tromso, _utc(2026, 12, 20), _utc(2026, 12, 23))
    assert len(blocks) >= 3
    assert all(b - a <= 24 * H for a, b in blocks)
    # the blocks are anchored on the start of the polar night, whatever the requested range
    later = qp.dark_intervals(tromso, _utc(2026, 12, 21, 12), _utc(2026, 12, 22))
    assert set(later) <= set(blocks)


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------
def _history(days, sp_fn, step_s=60.0, start=None, gaps=(), temp_fn=None, starts_fn=None):
    """Setpoints, valid spans, heater starts and temperatures of a synthetic history (local schedule)."""
    t0 = start if start is not None else _local(2026, 11, 2)
    t1 = t0 + days * 86400.0
    sps, on, tt, tv = [], [], [], []
    t = t0
    while t < t1:
        h = (dt.datetime.fromtimestamp(t, TZ) - dt.datetime.fromtimestamp(t, TZ).replace(
            hour=0, minute=0, second=0)).total_seconds() / H
        sp = sp_fn(t, h)
        if not sps or sps[-1][1] != sp:
            sps.append((round(t, 1), sp))
        temp = temp_fn(t, sp) if temp_fn else sp
        if not tv or tv[-1] != temp:
            tt.append(t)
            tv.append(temp)
        if starts_fn and starts_fn(t, sp, temp):
            on.append(t)
        t += step_s
    invalid = list(gaps)
    valid = qp.valid_spans(t0, t1, invalid)
    return {"setpoints": sps, "valid": valid, "first_t": t0, "last_t": t1, "on": on, "temps": (tt, tv)}


def _episodes(hist, pos=PARIS, restarts=()):
    return qp.build_episodes(pos, hist["setpoints"], hist["valid"], hist["first_t"], hist["last_t"], hist["on"],
                             hist["temps"], list(restarts))


def _eco(at_h: float, comfort_h: float = 7.5):
    def sp(t, h):
        if at_h < comfort_h:
            return 16.0 if at_h <= h < comfort_h else 18.0
        return 16.0 if (h >= at_h or h < comfort_h) else 18.0
    return sp


@pytest.mark.parametrize("eco_h", [0.0, 22.0, 2.0])
def test_setback_at_any_hour_gives_one_period_from_the_setback_to_the_step(eco_h: float) -> None:
    hist = _history(4, _eco(eco_h))
    eps = [e for e in _episodes(hist) if e["closed"] and e["complete"]]
    assert len(eps) >= 3
    for e in eps:
        start = dt.datetime.fromtimestamp(e["quiet"][0], TZ)
        end = dt.datetime.fromtimestamp(e["quiet"][1], TZ)
        assert (start.hour, start.minute) == (int(eco_h), 0)  # the period starts at the setback
        assert (end.hour, end.minute) == (7, 30)  # and ends at the comfort step, before sunrise (about 07:40)
        assert e["rise_t"] == e["end"] and e["kind"] == "setback"
        assert e["id"] == qp.night_id(e["origin"], e["quiet"][0])


def test_step_after_sunrise_ends_the_period_at_sunrise() -> None:
    """A comfort step at 09:00 is in daylight: the period stops at the end of the darkness, without a rise."""
    hist = _history(3, _eco(0.0, comfort_h=9.0))
    for e in [e for e in _episodes(hist) if e["closed"]]:
        assert e["rise_t"] is None and e["end"] == e["dark_end"]
        assert dt.datetime.fromtimestamp(e["end"], TZ).hour == 7
        assert qp.episode_of_rise([e], e["end"]) is None


def test_early_riser_in_summer_at_high_latitude() -> None:
    """Sunrise near 03:00 at 60 N in June: a 04:00 step is in daylight, the period ends at the dawn."""
    pos = qp.Position(60.0, 10.0)
    hist = _history(3, _eco(0.0, comfort_h=4.0), start=_local(2026, 6, 20))
    eps = [e for e in _episodes(hist, pos) if e["closed"]]
    assert eps and all(e["rise_t"] is None and e["end"] == e["dark_end"] for e in eps)
    assert all(dt.datetime.fromtimestamp(e["end"], TZ).hour == 3 for e in eps)


def _events_sp(eco_intervals):
    """Setpoint 16 C inside the given local intervals, 18 C outside."""
    ivs = [(_local(*d0), _local(*d1)) for d0, d1 in eco_intervals]

    def sp(t, h):
        return 16.0 if any(a <= t < b for a, b in ivs) else 18.0

    return sp


def test_variable_schedule_is_read_day_by_day() -> None:
    sp = _events_sp([((2026, 11, 2, 23.0), (2026, 11, 3, 7.5)), ((2026, 11, 4, 0.5), (2026, 11, 4, 6.0)),
                     ((2026, 11, 5, 1.0), (2026, 11, 5, 7.0))])
    eps = [e for e in _episodes(_history(4, sp)) if e["closed"] and e["complete"]]
    got = [(dt.datetime.fromtimestamp(e["quiet"][0], TZ).strftime("%d %H:%M"),
            dt.datetime.fromtimestamp(e["quiet"][1], TZ).strftime("%d %H:%M"), e["kind"]) for e in eps]
    assert got == [("02 23:00", "03 07:30", "setback"), ("04 00:30", "04 06:00", "setback"),
                   ("05 01:00", "05 07:00", "setback")]


def test_night_work_daytime_eco_is_silent_and_overlap_keeps_the_dark_part() -> None:
    # eco only between 09:00 and 16:00, in daylight in November: no period at eco; the nights at a held
    # comfort setpoint are quiet periods of their own, without any comfort step
    eps = _episodes(_history(3, lambda t, h: 16.0 if 9.0 <= h < 16.0 else 18.0))
    assert eps and all(e["kind"] == "held" and e["sp"] == 18.0 and e["rise_t"] is None for e in eps)
    # eco from 09:00 to 19:00: the setback run reaches the darkness (about 17:15), only that part is kept
    eps = _episodes(_history(3, lambda t, h: 16.0 if 9.0 <= h < 19.0 else 18.0))
    assert eps
    for e in eps:
        assert e["start"] == e["dark_start"] and dt.datetime.fromtimestamp(e["end"], TZ).hour == 19


def test_held_setpoint_gives_one_period_per_darkness_and_no_step() -> None:
    eps = [e for e in _episodes(_history(4, lambda t, h: 19.0)) if e["closed"] and e["complete"]]
    assert len(eps) == 3
    for e in eps:
        assert e["kind"] == "held" and e["rise_t"] is None
        assert e["quiet"] == [e["dark_start"], e["dark_end"]]
    assert len({e["id"] for e in eps}) == 3


def test_data_gaps_cut_a_period_and_are_never_filled() -> None:
    t_gap = _local(2026, 11, 3, 3.0)
    hist = _history(3, _eco(0.0), gaps=[(t_gap, t_gap + 1800.0)])
    e = next(e for e in _episodes(hist) if e["start"] <= t_gap < e["end"])
    assert len(e["comps"]) == 2
    assert e["quiet"] == [round(t_gap + 1800.0, 1), e["end"]]  # the longest piece, 03:30 to 07:30
    assert not any(c[0] < t_gap + 1800.0 and c[1] > t_gap for c in e["comps"])
    # the regulated-night span needs the selected piece to reach the step: here it does
    # a gap that leaves the earlier piece the longest cuts the regulated night
    t_gap2 = _local(2026, 11, 3, 6.0)
    hist2 = _history(3, _eco(0.0), gaps=[(t_gap2, t_gap2 + 600.0)])
    e2 = next(e for e in _episodes(hist2) if e["start"] <= t_gap2 < e["end"])
    assert qp.regulated_span(e2)[2] == qp.WHY_GAP


def test_a_bridged_restart_does_not_cut_a_period() -> None:
    """A bridged restart leaves no invalid interval: the period stays whole; only its marker remains."""
    hist = _history(3, _eco(0.0))
    t_r = _local(2026, 11, 3, 4.0)
    eps = _episodes(hist, restarts=[(t_r - 60.0, t_r + 60.0, True)])
    e = next(e for e in eps if e["start"] <= t_r < e["end"])
    assert len(e["comps"]) == 1


def test_open_period_is_not_closed_and_keys_do_not_change_when_it_closes() -> None:
    hist = _history(3, _eco(0.0))
    full = _episodes(hist)
    t_cut = _local(2026, 11, 4, 3.0)  # inside the last night
    part = qp.build_episodes(PARIS, [x for x in hist["setpoints"] if x[0] <= t_cut],
                             qp.valid_spans(hist["first_t"], t_cut, []), hist["first_t"], t_cut,
                             hist["on"], hist["temps"], [])
    assert not part[-1]["closed"]
    closed_then = {e["id"] for e in part if e["closed"]}
    assert closed_then <= {e["id"] for e in full}
    assert all(e["end"] <= t_cut for e in part if e["closed"])  # nothing decided from the future


def test_two_drops_in_one_setback_keep_the_longest_plateau() -> None:
    def sp(t, h):
        if 22.0 <= h or h < 1.0:
            return 17.0
        if 1.0 <= h < 7.5:
            return 16.0
        return 19.0

    eps = [e for e in _episodes(_history(3, sp)) if e["closed"] and e["complete"]]
    for e in eps[1:]:  # the first one starts with the history, after its first drop
        assert len(e["pieces"]) == 2 and e["sp"] == 16.0
        assert dt.datetime.fromtimestamp(e["quiet"][0], TZ).hour == 1
        assert dt.datetime.fromtimestamp(e["origin"], TZ).hour == 22


# ---------------------------------------------------------------------------
# Regulated start, G1 and the regulated night on whole episodes
# ---------------------------------------------------------------------------
def _room(regulates: bool, reach_h: float):
    """Room cooling from 18 to 16 C after a midnight setback, regulating at 16 C from reach_h (or never)."""
    def temp(t, sp):
        h = (dt.datetime.fromtimestamp(t, TZ).hour + dt.datetime.fromtimestamp(t, TZ).minute / 60.0)
        if sp == 18.0:
            return 18.0
        if h >= 12.0:
            return 18.0
        if h < reach_h:
            return round(18.0 - 2.0 * h / reach_h, 2)
        return 16.0 if regulates else 16.4

    def starts(t, sp, temp_now):
        h = (dt.datetime.fromtimestamp(t, TZ).hour + dt.datetime.fromtimestamp(t, TZ).minute / 60.0)
        if sp == 18.0:
            return int(t) % 1200 == 0
        return regulates and reach_h <= h < 12.0 and int(t) % 600 == 0

    return temp, starts


def _decide(regulates: bool, reach_h: float):
    temp, starts = _room(regulates, reach_h)
    hist = _history(3, _eco(0.0), temp_fn=temp, starts_fn=starts)
    e = [e for e in _episodes(hist) if e["closed"] and e["complete"]][0]
    c = qp.step_component(e, e["rise_t"])
    obs = gates.gate_observables(hist["on"], e["rise_t"], None, c[4])
    a, b, why = qp.regulated_span(e)
    rn = None
    if why is None:
        tt, tv = hist["temps"]
        rn = gates.regulated_span(a, b, e["sp"], hist["on"], [x + 300.0 for x in hist["on"]], tt, tv, 600.0)
    return e, c, obs, why, rn


def test_eco_at_midnight_without_sleepers_is_confirmed_and_regulated() -> None:
    e, c, obs, why, rn = _decide(True, 1.5)
    assert dt.datetime.fromtimestamp(c[4], TZ).strftime("%H:%M") == "01:30"
    assert gates.unconfirmed("G1", obs, 400.0, None) is None
    assert why is None and rn["ok"], rn


def test_room_that_never_reaches_eco_stays_unconfirmed() -> None:
    e, c, obs, why, rn = _decide(False, 1.5)
    assert c[4] is None and why == qp.WHY_NO_REGULATED_START
    assert gates.unconfirmed("G1", obs, 400.0, None) == "no regulated start"


def test_late_regulation_is_too_short() -> None:
    e, c, obs, why, rn = _decide(True, 5.0)
    assert (e["end"] - c[4]) < REGULATED_MIN_S
    assert why == qp.WHY_REGULATED_SHORT
    assert gates.unconfirmed("G1", obs, 400.0, None) == "regulated part too short"


def test_room_already_at_eco_when_the_darkness_starts() -> None:
    """Setback at 15:00 in daylight, eco reached at 16:00: the regulated part starts with the darkness."""
    def sp(t, h):
        return 16.0 if (h >= 15.0 or h < 7.5) else 18.0

    def temp(t, s):
        h = dt.datetime.fromtimestamp(t, TZ).hour
        return 16.0 if (s == 16.0 and (h >= 16 or h < 8)) else 18.0

    hist = _history(3, sp, temp_fn=temp, starts_fn=lambda t, s, v: v == 16.0 and int(t) % 1200 == 0)
    e = [e for e in _episodes(hist) if e["closed"] and e["complete"]][0]
    sel = e["comps"][e["sel"]]
    assert sel[3] < sel[0] and sel[4] == sel[0] == e["dark_start"]


def test_a_heater_start_right_after_a_restart_is_not_a_regulated_start() -> None:
    temp, starts = _room(False, 1.5)
    hist = _history(3, _eco(0.0), temp_fn=temp, starts_fn=starts)
    t_r = _local(2026, 11, 3, 3.0)
    hist["on"] = sorted(hist["on"] + [t_r + 10.0])
    e = [e for e in _episodes(hist, restarts=[(t_r - 30.0, t_r, False)]) if e["closed"] and e["complete"]][0]
    assert e["comps"][e["sel"]][4] is None


def test_identification_keeps_the_dark_response_to_the_rise() -> None:
    """The identification interval continues after the rise until sunrise (2 h at most), never after it."""
    hist = _history(3, _eco(0.0, comfort_h=6.0))
    for e in [e for e in _episodes(hist) if e["settled"]]:
        assert e["ident"][0] == e["quiet"][0] and e["quiet"][1] == e["rise_t"]
        assert e["ident"][1] == min(e["rise_t"] + 2 * H, e["dark_end"])
        assert e["ident"][1] <= e["dark_end"]
    # a new setpoint change inside the response stops it
    sp = _events_sp([((2026, 11, 3, 0.0), (2026, 11, 3, 6.0))])

    def two_rises(t, h):
        return 19.0 if _local(2026, 11, 3, 6.5) <= t < _local(2026, 11, 3, 12.0) else sp(t, h)

    e = next(e for e in _episodes(_history(2, two_rises)) if e["rise_t"])
    assert e["ident"][1] == round(_local(2026, 11, 3, 6.5), 1)
    # the end of the recorded data is no end: not settled yet
    cut = _local(2026, 11, 3, 7.0)
    hist = _history(2, _eco(0.0, comfort_h=6.0))
    part = qp.build_episodes(PARIS, [x for x in hist["setpoints"] if x[0] <= cut],
                             qp.valid_spans(hist["first_t"], cut, []), hist["first_t"], cut,
                             hist["on"], hist["temps"], [])
    last = [e for e in part if e["rise_t"]][-1]
    assert last["closed"] and not last["settled"]


def test_polar_night_search_is_bounded_and_not_repeated(monkeypatch) -> None:
    real_h = solar._h
    calls = []

    def counting(pos, t):
        calls.append(t)
        return real_h(pos, t)

    solar._day_samples.cache_clear()
    solar._dark_days.cache_clear()
    monkeypatch.setattr(solar, "_h", counting)
    svalbard = qp.Position(78.0, 15.0)
    t0 = _utc(2026, 12, 10)
    qp.dark_intervals(svalbard, t0, t0 + 86400.0)
    cold = len(calls)
    assert cold < 60000
    # The next day finds the days already sampled: only the new day and the crossings are computed
    calls.clear()
    qp.dark_intervals(svalbard, t0 + 86400.0, t0 + 2 * 86400.0)
    assert len(calls) < cold / 20
