"""Position of the sun and the intervals of darkness (pure Python, astral).

The elevation of the centre of the sun comes from the ``astral`` package (the
one Home Assistant uses for its sun helpers), without refraction: the
threshold DARK_ELEVATION_DEG carries the refraction and its margin. Darkness is
decided conservatively: a sampled elevation counts as dark only clearly below
the threshold, every boundary is refined by bisection, then moved inwards by
DARK_TIME_MARGIN_S. Times are epoch seconds (UTC); the local time zone plays no
part.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from functools import lru_cache

from .const import (
    DARK_BLOCK_S,
    DARK_ELEVATION_DEG,
    DARK_SEARCH_MAX_S,
    DARK_TIME_MARGIN_S,
    KEY_DIGITS,
    SUN_REFINE_S,
    SUN_SAMPLE_GUARD_DEG,
    SUN_SAMPLE_S,
)


@dataclass(frozen=True)
class Position:
    """Latitude and longitude of the home, in degrees."""

    latitude: float
    longitude: float

    def to_list(self) -> list[float]:
        return [self.latitude, self.longitude]


def validate_position(latitude, longitude) -> Position | None:
    """A Position from finite numbers within [-90, 90] and [-180, 180], None otherwise."""
    vals = []
    for v, lim in ((latitude, 90.0), (longitude, 180.0)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        v = float(v)
        if not math.isfinite(v) or abs(v) > lim:
            return None
        vals.append(v)
    return Position(vals[0], vals[1])


def astral_available() -> bool:
    try:
        import astral.sun  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        return False
    return True


@lru_cache(maxsize=8192)
def _elev(lat: float, lon: float, t: float) -> float:
    from astral import Observer  # pylint: disable=import-outside-toplevel
    from astral.sun import elevation  # pylint: disable=import-outside-toplevel

    when = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return float(elevation(Observer(lat, lon), when, with_refraction=False))


def sun_elevation(pos: Position, t: float) -> float:
    """Geometric elevation of the centre of the sun at t, in degrees."""
    return _elev(pos.latitude, pos.longitude, float(t))


def _h(pos: Position, t: float) -> float:
    """Negative when dark (the guard included)."""
    return sun_elevation(pos, t) - DARK_ELEVATION_DEG + SUN_SAMPLE_GUARD_DEG


def _refine(pos: Position, a: float, b: float) -> float:
    """Crossing of _h between a and b (opposite signs), at SUN_REFINE_S."""
    ha = _h(pos, a) < 0.0
    while b - a > SUN_REFINE_S:
        m = 0.5 * (a + b)
        if (_h(pos, m) < 0.0) == ha:
            a = m
        else:
            b = m
    return 0.5 * (a + b)


def _scan(pos: Position, t0: float, t1: float) -> list[list]:
    """Dark runs [start, end, start_is_crossing, end_is_crossing] on the sample grid covering [t0, t1]."""
    g0 = math.floor(t0 / SUN_SAMPLE_S) * SUN_SAMPLE_S
    n = int(math.ceil((t1 - g0) / SUN_SAMPLE_S)) + 1
    ts = [g0 + k * SUN_SAMPLE_S for k in range(n)]
    dark = [_h(pos, t) < 0.0 for t in ts]
    runs: list[list] = []
    cur = None
    for k, d in enumerate(dark):
        if d and cur is None:
            if k == 0:
                cur = [ts[0], None, False, False]
            else:
                cur = [_refine(pos, ts[k - 1], ts[k]), None, True, False]
        elif not d and cur is not None:
            cur[1] = _refine(pos, ts[k - 1], ts[k])
            cur[3] = True
            runs.append(cur)
            cur = None
    if cur is not None:
        cur[1] = ts[-1]
        runs.append(cur)
    return runs


def _extend(pos: Position, run: list) -> list:
    """Find the true start and end of a dark run cut by the scanned range (polar night)."""
    step = DARK_BLOCK_S
    a, b = run[0], run[1]
    if not run[2]:
        lo = a
        while not run[2] and a - lo < DARK_SEARCH_MAX_S:
            lo -= step
            back = _scan(pos, lo, run[0])
            if back and back[-1][1] >= run[0] - SUN_SAMPLE_S:
                run[0], run[2] = back[-1][0], back[-1][2]
            else:
                break
    if not run[3]:
        hi = b
        while not run[3] and hi - b < DARK_SEARCH_MAX_S:
            hi += step
            fwd = _scan(pos, run[1], hi)
            if fwd and fwd[0][0] <= run[1] + SUN_SAMPLE_S:
                run[1], run[3] = fwd[0][1], fwd[0][3]
            else:
                break
    return run


def dark_intervals(pos: Position, t0: float, t1: float) -> list[tuple[float, float]]:
    """Intervals of certified darkness [start, end) that overlap [t0, t1), with their full extent.

    A darkness longer than DARK_BLOCK_S (polar night) is cut into blocks of that
    length anchored on its start, so that it still ends somewhere. The scan
    covers whole UTC days around [t0, t1) and is cached.
    """
    if t1 <= t0:
        return []
    d0 = math.floor(t0 / 86400.0) * 86400.0
    d1 = math.ceil(t1 / 86400.0) * 86400.0
    return [x for x in _dark_days(pos, d0, d1) if x[1] > t0 and x[0] < t1]


@lru_cache(maxsize=64)
def _dark_days(pos: Position, t0: float, t1: float) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for run in _scan(pos, t0, t1):
        run = _extend(pos, run)
        a = run[0] + (DARK_TIME_MARGIN_S if run[2] else 0.0)
        b = run[1] - (DARK_TIME_MARGIN_S if run[3] else 0.0)
        if b <= a:
            continue
        k = a
        while k < b:
            e = min(k + DARK_BLOCK_S, b)
            if e > t0 and k < t1:
                out.append((round(k, KEY_DIGITS), round(e, KEY_DIGITS)))
            k = e
    return tuple(out)
