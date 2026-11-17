"""A one-step toggle of the sensor does not shorten a dead time.

A 0.1-degC sensor that reports on change can step one step up and back for a minute (a room near a
quantisation boundary, the sensor's noise). The filtered temperature rises and falls with it; the
estimator took that rise for the peak (or a fall for the trough) and its fall for the response,
and timed the dead time from the toggle. In a synthetic test, a 50-s toggle about 500 s after a
relay room's stop gave a passive dead time of about 500 s, where the room's is about 955-1020 s
(the weak-evidence rule kept it out); replayed through the estimator, that trace gives 535 s, and
1192 s with the rule below. A step of the raw reading now counts only once
confirmed: held two of SmartPI's calculations, or followed by a second step the same way. A
response must stand above the noise level (Astrom and Hagglund 1995, 6.4, p. 237), and a
commercial adaptive controller, the Foxboro EXACT, adapts only once the error exceeds two times its
noise band, the measurement's peak-to-peak amplitude (6.6, pp. 245-246; Hagglund and Astrom 2000,
4, p. 1174): for a sensor that toggles by one step,
one step may be a toggle, two steps is a real move, as for the trend before a start.

The room as SmartPI sees it: Versatile Thermostat calculates twice at each change of the reading,
and SmartPI every 60 s.
"""

from __future__ import annotations

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import deadtime_estimator
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter

DEAD_TRUE = 960.0
LEVEL = 19.24  # the room, read 19.2
FALL_C_PER_H = 0.6


def _passive(t: float) -> float:
    """After a settled heating stopped at 0 s: flat, then falling from DEAD_TRUE."""
    return LEVEL - max(0.0, t - DEAD_TRUE) * FALL_C_PER_H / 3600.0


def _active(t: float) -> float:
    """After a start at 0 s from a flat room: flat, then rising from DEAD_TRUE."""
    return LEVEL - 0.08 + max(0.0, t - DEAD_TRUE) * 3.0 / 3600.0


def _measure(room, *, power: float, toggle=None, phase: float = 0.0) -> float | None:
    """The dead time the estimator gives for ``room`` after a power step at 0 s; ``toggle``:
    (from, to, step) seconds and direction of a one-step toggle of the reading."""
    filt = AdaptiveTintFilter("t")
    est = DeadTimeEstimator()
    est.deadtime_heat_s = est.deadtime_cool_s = 780.0
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    before = 1.0 - power
    est.last_power, est.state = before, ("HEATING" if before else "OFF")
    est.last_start_time = -5000.0 if before else None
    est.last_stop_time = None if before else -5000.0

    def reading(t: float) -> float:
        value = round(round(room(t) / 0.1) * 0.1, 2)
        if toggle is not None and toggle[0] <= t < toggle[1]:
            value = round(value + 0.1 * toggle[2], 2)
        return value

    def feed(t: float, raw: float, u: float) -> None:
        lp, _ = filt.update(raw, t)
        est.update(now=t, tin=lp, tin_raw=raw, u_applied=u, hvac_mode=VThermHvacMode_HEAT)

    for t in range(-1800, 0, 60):
        feed(float(t) + phase, reading(t), before)
    feed(0.0, reading(0.0), power)
    last, t, periodic = reading(0.0), 0.0, 60.0 + phase
    while t < 4 * 3600.0 and est.state in ("WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE"):
        t += 1.0
        raw = reading(t)
        if raw != last:
            feed(t, raw, power)
            feed(t, raw, power)  # Versatile Thermostat's recalculate, then its control
            last = raw
        if t >= periodic:
            feed(t, raw, power)
            periodic += 60.0
    return est.deadtime_heat_s if power else est.deadtime_cool_s


@pytest.mark.parametrize("toggle_s", [20.0, 50.0, 100.0])
@pytest.mark.parametrize("at", [200.0, 600.0])
@pytest.mark.parametrize("direction", [1, -1], ids=["up", "down"])
def test_a_toggle_does_not_shorten_a_passive_dead_time(at, toggle_s, direction) -> None:
    """Waiting for the room to cool after a stop: a toggle up (taken for the peak) or down
    (taken for the drop) leaves the dead time as without it."""
    reference = _measure(_passive, power=0.0)
    assert _measure(_passive, power=0.0, toggle=(at, at + toggle_s, direction)) == reference


@pytest.mark.parametrize("toggle_s", [20.0, 50.0, 100.0])
@pytest.mark.parametrize("at", [200.0, 600.0])
@pytest.mark.parametrize("direction", [1, -1], ids=["up", "down"])
def test_a_toggle_does_not_shorten_a_heating_dead_time(at, toggle_s, direction) -> None:
    """Waiting for the room to warm after a start: a toggle down (taken for the trough) or up
    (taken for the rise) leaves the dead time as without it."""
    reference = _measure(_active, power=1.0)
    assert _measure(_active, power=1.0, toggle=(at, at + toggle_s, direction)) == reference


def test_a_step_held_longer_is_the_room_s_move(monkeypatch) -> None:
    """A step held longer than two calculations, as the peak of a room still warming after its
    stop (about 230 s in a synthetic test), is the room's own: the estimator times it as before."""
    toggle = (500.0, 730.0, 1)
    now = _measure(_passive, power=0.0, toggle=toggle)
    monkeypatch.setattr(deadtime_estimator, "DEADTIME_RAW_CONFIRM_S", 0.0)
    assert now == _measure(_passive, power=0.0, toggle=toggle)


@pytest.mark.parametrize("direction", [1, -1], ids=["up", "down"])
@pytest.mark.parametrize("room,power", [(_passive, 0.0), (_active, 1.0)], ids=["passive", "heating"])
def test_a_toggle_in_progress_at_the_power_step_does_not_give_0_s(room, power, direction) -> None:
    """A toggle that began 30 s before the stop (or the start) and ends 30 s after: the peak (or
    trough) the detection starts from is the room's, not the toggle's, so no detection of 0 s at
    once (before this was handled: a passive dead time of 0.0 s)."""
    reference = _measure(room, power=power)
    assert _measure(room, power=power, toggle=(-30.0, 30.0, direction)) == reference


@pytest.mark.parametrize("direction", [1, -1], ids=["up", "down"])
@pytest.mark.parametrize("room,power", [(_passive, 0.0), (_active, 1.0)], ids=["passive", "heating"])
def test_a_step_held_from_before_the_power_step_is_measured_as_before(monkeypatch, room, power, direction) -> None:
    """A step 30 s before the stop (or the start) that stays: the room moved, it was no toggle.
    Armed while the step was not confirmed, the wait starts from the temperature before it; once
    the step is confirmed, the trough (or peak) is taken again from the temperature since the
    power step, so the move before it is not the response and the result is the one before this
    rule (a rise 50 s before a start then gave 45 s in place of no measurement)."""
    toggle = (-30.0, 10 * 3600.0, direction)
    now = _measure(room, power=power, toggle=toggle)
    monkeypatch.setattr(deadtime_estimator, "DEADTIME_RAW_CONFIRM_S", 0.0)
    assert now == _measure(room, power=power, toggle=toggle)
