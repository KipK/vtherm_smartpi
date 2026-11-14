"""Quiet periods from the recorded data (pure Python).

Objects, all on the epoch axis in seconds, as half-open intervals [start, end):

- plateau: a maximal interval of constant setpoint;
- setback run: from a setpoint drop to the next rise (further drops stay in the
  run). A rise ends it, except a rise that stays below the setpoint before the
  drop while the room is still above the new setpoint: it calls for no heat, so
  the room goes on cooling freely and the run goes on (its plateau is a further
  piece of the run);
- darkness: an interval of certified darkness (solar.py);
- episode: at most one per darkness, the part of a setback run inside it, or
  the whole darkness when the setpoint does not change inside it at all (held
  setpoint). It ends at the rise that ends the run, or at the end of the
  darkness. A darkness without a setback run and with setpoint changes inside
  has no episode;
- quiet components: the plateau pieces of an episode intersected with the
  valid data. The selected component of an episode is its longest one
  (the earliest on a tie); an episode whose selected component is shorter than
  PERIOD_MIN_S is no night;
- regulated start of a component: the first heater start, at or after the
  start of its plateau, with the room within REGULATED_ECO_TOL_C of the
  setpoint; when that start is before the component, the component start if the
  data between them are valid. None when the room never regulates there. A
  heater start just after a restart is not counted.

An episode keeps its components as [start, end, setpoint, first heater start
at the setpoint, regulated start] and the index ``sel`` of the selected one.

An episode is closed once its end is known (end <= last observation) and
complete when the data cover it from its start. Its identification interval is
its selected component, continued after the rise that ends it while it is still
dark, the new setpoint constant and the data valid (IDENT_TAIL_S at most): the
response to the rise excites the emitter. The episode is settled once that
interval is known too. Its key is
``q<version>:<origin>:<start of the selected component>``, the origin being the
start of the episode (the setback, or the start of the darkness for a run that
began before it or a held setpoint), both at 0.1 s: events and the position
fix it, not the local time, and later data never change the key of a closed
episode.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from typing import Any

from .const import (
    BASELINE_HOLD_S,
    IDENT_TAIL_S,
    KEY_DIGITS,
    PERIOD_MIN_S,
    REGULATED_ECO_TOL_C,
    REGULATED_MIN_S,
    RESTART_START_GUARD_S,
    SELECTION_VERSION,
    SP_EPS,
)
from .solar import Position, dark_intervals

INF = math.inf

# Why a darkness gives no episode, or why a period does not serve a rule (codes;
# the diagnostics translate them)
WHY_NO_POSITION = "home position unknown"
WHY_SHORT = "period too short"
WHY_NO_REGULATED_START = "room never regulated at the low setpoint"
WHY_REGULATED_SHORT = "regulated part too short"
WHY_GAP = "data gap"
WHY_SETPOINT = "setpoint change in the regulated part"


def _r(t: float) -> float:
    return round(float(t), KEY_DIGITS)


def night_id(origin: float, quiet_start: float) -> str:
    """Key of an episode: selection version, origin and start of its selected component."""
    return f"q{SELECTION_VERSION}:{_r(origin):.{KEY_DIGITS}f}:{_r(quiet_start):.{KEY_DIGITS}f}"


# ---------------------------------------------------------------------------
# Plateaus and setback runs
# ---------------------------------------------------------------------------
def plateaus(setpoints: list) -> list[dict]:
    """Plateaus of a zero-order-hold setpoint timeline [(t, sp), ...]: start, end, sp, prev (None: unknown)."""
    out: list[dict] = []
    for t, v in setpoints:
        if v is None:
            continue
        v = float(v)
        if out and abs(v - out[-1]["sp"]) <= SP_EPS:
            continue
        if out:
            out[-1]["end"] = float(t)
        out.append({"start": float(t), "end": INF, "sp": v, "prev": out[-1]["sp"] if out else None})
    return out


def setback_runs(pls: list[dict], temps=None) -> list[dict]:
    """Setback runs: from a drop to the next rise that calls for heat.

    temps: (times, values) of the indoor temperature. A rise to a setpoint still
    below the one before the drop, with the room strictly above the new setpoint
    at the rise, does not end the run. Without temperatures every rise ends it.
    """
    runs: list[dict] = []
    cur = None
    for p in pls:
        if p["prev"] is None:
            continue
        if p["sp"] < p["prev"] - SP_EPS:
            if cur is None:
                cur = {"start": p["start"], "end": INF, "high": p["prev"]}
                runs.append(cur)
        elif p["sp"] > p["prev"] + SP_EPS and cur is not None:
            if temps is not None and p["sp"] < cur["high"] - SP_EPS:
                temp = _temp_at(temps, p["start"])
                if temp is not None and temp > p["sp"]:
                    continue
            cur["end"] = p["start"]
            cur = None
    return [{"start": r["start"], "end": r["end"]} for r in runs]


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------
def intersect(a: float, b: float, spans: list) -> list[tuple[float, float]]:
    """Parts of [a, b) inside the sorted disjoint spans."""
    out = []
    for s0, s1 in spans:
        if s1 <= a:
            continue
        if s0 >= b:
            break
        lo, hi = max(a, s0), min(b, s1)
        if hi > lo:
            out.append((lo, hi))
    return out


def covered(a: float, b: float, spans: list) -> bool:
    """[a, b) lies inside one span."""
    return any(s0 <= a and b <= s1 for s0, s1 in spans)


def valid_spans(first_t: float | None, last_t: float | None, invalid: list) -> list[tuple[float, float]]:
    """Complement of the invalid intervals within [first_t, last_t]."""
    if first_t is None or last_t is None or last_t <= first_t:
        return []
    out, t = [], float(first_t)
    for g0, g1 in sorted((float(a), float(b)) for a, b in invalid):
        if g1 <= t:
            continue
        if g0 > t:
            out.append((t, min(g0, last_t)))
        t = max(t, g1)
        if t >= last_t:
            break
    if t < last_t:
        out.append((t, float(last_t)))
    return [(a, b) for a, b in out if b > a]


# ---------------------------------------------------------------------------
# Regulated start
# ---------------------------------------------------------------------------
def _temp_at(temps, t: float) -> float | None:
    tt, tv = temps
    i = bisect.bisect_right(tt, t) - 1
    return float(tv[i]) if i >= 0 else None


def regulated_start(comp_start: float, comp_end: float, plateau_start: float, sp: float, on: list, temps,
                    valid: list, restarts: list) -> tuple[float | None, float | None]:
    """(first heater start at the low setpoint, regulated start) of one quiet component."""
    resumes = [float(r[1]) for r in restarts]

    def eco_starts(t_from: float):
        i = bisect.bisect_left(on, t_from)
        for e in on[i:]:
            if e >= comp_end:
                return
            if any(0.0 <= e - r <= RESTART_START_GUARD_S for r in resumes):
                continue
            temp = _temp_at(temps, e)
            if temp is not None and temp <= sp + REGULATED_ECO_TOL_C:
                yield float(e)

    first = next(eco_starts(plateau_start), None)
    if first is None:
        return None, None
    if first >= comp_start:
        return first, first
    if covered(first, comp_start, valid):
        return first, comp_start
    later = next(eco_starts(comp_start), None)
    return first, later


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------
def _pieces(a: float, b: float, pls: list[dict]) -> list[tuple[float, float, float, float]]:
    """Plateau pieces (start, end, sp, plateau start) of [a, b)."""
    out = []
    for p in pls:
        lo, hi = max(a, p["start"]), min(b, p["end"])
        if hi > lo:
            out.append((lo, hi, p["sp"], p["start"]))
    return out


def _candidate(kind: str, a: float, b: float, d0: float, d1: float, rise_t: float | None,
               setback_start: float | None, pls: list[dict], valid: list) -> dict:
    pieces = _pieces(a, b, pls)
    comps = []
    for p0, p1, sp, ps in pieces:
        for c0, c1 in intersect(p0, p1, valid):
            comps.append([_r(c0), _r(c1), sp, ps])
    sel = None
    for k, c in enumerate(comps):
        if sel is None or c[1] - c[0] > comps[sel][1] - comps[sel][0] + 1e-9:
            sel = k
    return {"kind": kind, "origin": _r(a), "start": _r(a), "end": _r(b), "dark_start": d0, "dark_end": d1,
            "rise_t": None if rise_t is None else _r(rise_t),
            "setback_start": None if setback_start is None else _r(setback_start),
            "pieces": [[_r(p[0]), _r(p[1]), p[2]] for p in pieces], "comps": comps, "sel": sel}


def _ident_end(ep: dict, sel: list, pls: list[dict], valid: list, d1: float, last_t: float) -> float:
    """End of the identification interval: the selected component, continued after the rise that ends the
    episode while the data stay valid, the new setpoint constant and the sun down, for at most IDENT_TAIL_S.

    The end of the recorded data is no end: the interval is then not known yet (it ends after last_t)."""
    r = ep.get("rise_t")
    if r is None or abs(sel[1] - r) > 0.05:
        return sel[1]
    if r >= last_t:
        return INF
    nxt = next((p["end"] for p in pls if p["start"] <= r < p["end"]), r)
    span = next((b for a, b in valid if a <= r < b), r)
    if span >= last_t - 0.05:
        span = INF
    end = min(nxt, span, d1, r + IDENT_TAIL_S)
    return end if end == INF else _r(max(r, end))


def build_episodes(pos: Position | None, setpoints: list, valid: list, first_t: float | None,
                   last_t: float | None, on: list, temps, restarts: list) -> list[dict]:
    """Episodes of the history, oldest first (see the module docstring); [] without a position.

    valid: the spans of valid data of the history (TarHistory.valid_spans); on: the heater starts;
    temps: (times, values) of the indoor temperature; restarts: the restart markers of the history.
    """
    if pos is None or first_t is None or last_t is None or last_t <= first_t:
        return []
    valid = [(float(a), float(b)) for a, b in valid]
    pls = plateaus(setpoints)
    if not pls:
        return []
    runs = setback_runs(pls, temps)
    out = []
    for d0, d1 in dark_intervals(pos, float(first_t), float(last_t)):
        cands = []
        for r in runs:
            if r["start"] >= d1 or r["end"] <= d0:
                continue
            a, b = max(r["start"], d0), min(r["end"], d1)
            cands.append(_candidate("setback", a, b, d0, d1, r["end"] if r["end"] <= d1 else None, r["start"],
                                    pls, valid))
        if not cands:
            inside = [p for p in pls if d0 < p["start"] < d1]
            if not inside and any(p["start"] <= d0 < p["end"] for p in pls):
                cands.append(_candidate("held", d0, d1, d0, d1, None, None, pls, valid))
        best = None
        for c in cands:
            if c["sel"] is None:
                continue
            s = c["comps"][c["sel"]]
            if s[1] - s[0] < PERIOD_MIN_S:
                continue
            if best is None or s[1] - s[0] > best["comps"][best["sel"]][1] - best["comps"][best["sel"]][0] + 1e-9:
                best = c
        if best is None:
            continue
        for c in best["comps"]:
            first, reg = regulated_start(c[0], c[1], c[3], c[2], on, temps, valid, restarts)
            c[3] = None if first is None else _r(first)
            c.append(None if reg is None else _r(reg))
        s = best["comps"][best["sel"]]
        best["quiet"] = [s[0], s[1]]
        best["sp"] = s[2]
        best["id"] = night_id(best["origin"], s[0])
        best["ident"] = [s[0], _ident_end(best, s, pls, valid, d1, float(last_t))]
        best["closed"] = best["end"] <= float(last_t)
        best["settled"] = best["closed"] and best["ident"][1] <= float(last_t)
        best["complete"] = best["start"] >= float(first_t)
        out.append(best)
    return out


# ---------------------------------------------------------------------------
# Uses of an episode
# ---------------------------------------------------------------------------
def episode_of_rise(episodes: list[dict], t_sp: float) -> dict | None:
    """The episode that a setpoint rise at t_sp ends."""
    for e in episodes:
        if e.get("rise_t") is not None and abs(e["rise_t"] - t_sp) <= 1.0:
            return e
    return None


def step_component(ep: dict, t_sp: float) -> list | None:
    """Quiet component that ends at the rise and holds the 2 h baseline, None otherwise."""
    for c in ep["comps"]:
        if abs(c[1] - t_sp) <= 1.0 and c[0] <= t_sp - BASELINE_HOLD_S + 1e-6:
            return c
    return None


def quiet_intervals(ep: dict) -> list[tuple[float, float]]:
    """All quiet components of an episode, as intervals."""
    return [(c[0], c[1]) for c in ep["comps"]]


def regulated_span(ep: dict) -> tuple[float | None, float | None, str | None]:
    """[regulated start, end of the episode) of the regulated-night rule, or the reason it has none."""
    s = ep["comps"][ep["sel"]]
    reg = s[4]
    end = ep["end"]
    if s[1] < end - 0.05:
        if any(abs(p[1] - s[1]) <= 0.05 for p in ep["pieces"]):
            return None, None, WHY_SETPOINT
        return None, None, WHY_GAP
    if reg is None:
        return None, None, WHY_NO_REGULATED_START
    if end - reg < REGULATED_MIN_S:
        return reg, end, WHY_REGULATED_SHORT
    return reg, end, None


def local_iso(t: float | None, tz) -> str | None:
    """Local time of t for the diagnostics (minutes)."""
    if t is None:
        return None
    return dt.datetime.fromtimestamp(float(t), tz).isoformat(timespec="minutes")


def summary(ep: dict) -> dict[str, Any]:
    """Plain data of an episode kept as evidence (persisted)."""
    keys = ("id", "kind", "origin", "start", "end", "dark_start", "dark_end", "rise_t", "setback_start", "pieces",
            "comps", "sel", "quiet", "sp", "ident")
    return {k: ep.get(k) for k in keys}
