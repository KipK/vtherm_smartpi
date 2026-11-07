"""Applied command timeline and emitter response (pure Python).

The applied heat-equivalent power (0..1) is kept as a zero-order-hold
timeline: a PWM cycle is on from its start for power x cycle, a valve level is
held until the next one. The emitter output is that command delayed by L and
filtered by a first order of time constant tau_e; over a step the bin mean of
the output is exact for a command held over the step.
"""
from __future__ import annotations

import bisect
import math
from typing import Any

_KEEP_S = 4.0 * 3600.0
_MAX_POINTS = 2000


class CommandTimeline:
    """Applied power (0..1) over time, zero before the first sample."""

    def __init__(self) -> None:
        self.t: list[float] = []
        self.v: list[float] = []
        self._cycle_start: float | None = None
        self.power = 0.0

    def plan(self, t: float, steps: list[tuple[float, float]]) -> None:
        """Replace the timeline from t on by the given (time, level) steps."""
        i = bisect.bisect_left(self.t, t)
        del self.t[i:]
        del self.v[i:]
        for ts, vs in steps:
            vs = min(max(float(vs), 0.0), 1.0)
            if self.v and self.v[-1] == vs:
                continue
            if self.t and ts <= self.t[-1]:
                self.v[-1] = vs
                continue
            self.t.append(float(ts))
            self.v.append(vs)
        if len(self.t) > _MAX_POINTS:
            del self.t[: len(self.t) - _MAX_POINTS]
            del self.v[: len(self.v) - _MAX_POINTS]

    def applied_cycle(self, t: float, power: float, cycle_s: float, cycle_start: bool) -> None:
        """A PWM command: on from the cycle start for power x cycle."""
        power = min(max(float(power), 0.0), 1.0)
        self.power = power
        if cycle_start or self._cycle_start is None:
            self._cycle_start = t
        start = self._cycle_start
        if power <= 0.0:
            self.plan(t, [(t, 0.0)])
        elif power >= 1.0:
            self.plan(t, [(t, 1.0), (start + cycle_s + 1.0, 0.0)])
        else:
            off_at = start + power * cycle_s
            self.plan(t, [(t, 1.0 if off_at > t else 0.0), (max(off_at, t), 0.0)])

    def applied_level(self, t: float, power: float) -> None:
        """A valve command: the level is held until the next one."""
        self.power = min(max(float(power), 0.0), 1.0)
        self._cycle_start = t
        self.plan(t, [(t, self.power)])

    def stop(self, t: float) -> None:
        """No heating from t on (HVAC not heating, restart)."""
        self.power = 0.0
        self._cycle_start = None
        self.plan(t, [(t, 0.0)])

    def mean(self, a: float, b: float) -> float:
        """Mean level over [a, b)."""
        if b <= a:
            return self.at(a)
        i = bisect.bisect_right(self.t, a)
        cur = self.v[i - 1] if i > 0 else 0.0
        acc = 0.0
        t_cur = a
        while i < len(self.t) and self.t[i] < b:
            acc += cur * (self.t[i] - t_cur)
            t_cur = self.t[i]
            cur = self.v[i]
            i += 1
        acc += cur * (b - t_cur)
        return acc / (b - a)

    def at(self, t: float) -> float:
        i = bisect.bisect_right(self.t, t)
        return self.v[i - 1] if i > 0 else 0.0

    def prune(self, t: float) -> None:
        """Forget the steps older than the delay buffer needs (keeps the level at the cutoff)."""
        cutoff = t - _KEEP_S
        i = max(0, bisect.bisect_left(self.t, cutoff) - 1)
        del self.t[:i]
        del self.v[:i]

    def to_dict(self) -> dict[str, Any]:
        return {"t": [round(x, 1) for x in self.t], "v": [round(x, 4) for x in self.v], "power": self.power}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CommandTimeline":
        tl = cls()
        if not isinstance(data, dict):
            return tl
        try:
            t = [float(x) for x in data.get("t", [])]
            v = [float(x) for x in data.get("v", [])]
            if len(t) == len(v) and all(b >= a for a, b in zip(t, t[1:])):
                tl.t, tl.v = t, v
            tl.power = float(data.get("power") or 0.0)
        except (TypeError, ValueError):
            return cls()
        return tl


def emitter_step(x: float, v: float, h: float, tau_e: float) -> tuple[float, float]:
    """First-order emitter over one step with the input v held: (bin mean of the output, output at the end)."""
    if tau_e < 1.0:
        return v, v
    e = math.exp(-h / tau_e)
    mean = v + (x - v) * (tau_e / h) * (1.0 - e)
    return mean, v + (x - v) * e
