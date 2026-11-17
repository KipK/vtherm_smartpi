"""A thermostat on a relay that alone keeps the central boiler on (documentation/en/technical_doc.md,
3.8): Versatile Thermostat counts a relay for its boiler only while it is on, so the boiler goes
off in each off part of SmartPI's cycle. That off part is the cycle's own, not a lack of heat: the
boiler cuts the room's heat only once the relay has been on for 5 s with the boiler off, and gives
it back only while both are on. The real handler and SmartPI's real calculation, driven by a small
event loop: the relay's switching in its cycle, the boiler's changes (seen by the handler's
listeners), SmartPI's calculations every 60 s, and the checks the handler schedules."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakes.fake_thermostat_runtime import FakeThermostatRuntime
from helpers import force_smartpi_stable_mode

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.boiler_call import (
    BOILER_BLINK_S,
    BOILER_PAUSE_CONFIRM_S,
    BOILER_VALVE_HOLD_S,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler

DEAD = 780.0
RECALC = 60.0
FOLLOW_S = 0.01  # Versatile Thermostat switches its boiler this long after the relay


class _Loop:
    """The relay's cycle, the boiler, SmartPI's calculations and the handler's scheduled checks, in
    time order, on one monotonic clock."""

    def __init__(self, handler: SmartPIHandler, temp: Callable[[float], float] = lambda _at: 19.0) -> None:
        self.handler = handler
        self.temp = temp
        self.now = 0.0
        self.boiler = False
        self.timers: list[list] = []
        handler._boiler_listener_remove = MagicMock()  # the boiler's changes are followed
        handler._thermostat_listener_remove = MagicMock()  # and the thermostat's

    def later(self, _hass, delay: float, action) -> Callable[[], None]:
        timer = [self.now + delay, action, True]
        self.timers.append(timer)
        return lambda: timer.__setitem__(2, False)

    def _patched(self):
        return (
            patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=self.now),
            patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=self.now),
            patch("custom_components.vtherm_smartpi.smartpi.deadtime_estimator.time.monotonic", return_value=self.now),
            patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=self.boiler),
            patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
            patch("custom_components.vtherm_smartpi.handler.async_call_later", side_effect=self.later),
            patch.object(self.handler, "_async_save", new_callable=AsyncMock),
        )

    async def _at(self, at: float, act) -> None:
        while True:
            due = sorted((t for t in self.timers if t[2] and t[0] <= at), key=lambda t: t[0])
            if not due:
                break
            timer = due[0]
            timer[2] = False
            self.now = timer[0]
            p = self._patched()
            with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
                timer[1](None)
        self.now = at
        self.handler._thermostat.current_temperature = self.temp(at)
        p = self._patched()
        with p[0], p[1], p[2], p[3] as boiler_patch, p[4], p[5], p[6]:
            self._boiler_patch = boiler_patch
            result = act()
            if result is not None:
                await result

    async def run(self, events: Iterable[tuple[float, str, object]], watch: Callable[[float, str], None] | None = None) -> None:
        """Each event in time order: ("calc", command), ("cycle", (command, cycle in s): Versatile
        Thermostat's receipt of a cycle's start), ("relay", on) or ("boiler", on)."""
        algo = self.handler._thermostat.prop_algorithm
        for at, kind, value in sorted(events, key=lambda e: (e[0], {"calc": 0, "cycle": 1, "relay": 2, "boiler": 3}[e[1]])):
            # The checks due before the event run first, on the state before it.
            if kind == "calc":

                def act(v=value):
                    algo._committed_on_percent = v
                    return self.handler.control_heating(force=False)

            elif kind == "cycle":

                def act(v=value):
                    algo._committed_on_percent, algo._cycle_min = v[0], v[1] / 60.0
                    algo._current_cycle_start_monotonic = self.now

            elif kind == "relay":

                def act(v=value):
                    self.handler._thermostat.is_device_active = v
                    self.handler._on_thermostat_change(None)

            else:

                def act(v=value):
                    self.boiler = v
                    self._boiler_patch.return_value = v
                    self.handler._on_boiler_change(None)

            await self._at(at, act)
            if watch is not None:
                watch(at, kind)


def _relay_handler(scheduler: bool = True) -> SmartPIHandler:
    """A learned room behind a relay (dead times 780 s, reliable, six detections each), at rest;
    without ``scheduler``, a thermostat without a cycle scheduler (a climate underlying)."""
    thermostat = FakeThermostatRuntime()
    thermostat.is_used_by_central_boiler = True
    if scheduler:
        thermostat.cycle_scheduler.is_valve_mode = False
    else:
        thermostat.cycle_scheduler = None
    handler = SmartPIHandler(thermostat)
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        handler.init_algorithm()
    algo = thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    est = algo.dt_est
    est.deadtime_heat_s = est.deadtime_cool_s = DEAD
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    est._history_heat.extend([DEAD] * 6)
    est._history_cool.extend([DEAD] * 6)
    est.state, est.last_power, est.last_start_time, est.last_stop_time = "OFF", 0.0, None, -10_000.0
    return handler


def _cycles(cycle_s: float, duty: float, start: float, end: float) -> list[tuple[float, str, object]]:
    """SmartPI's relay in cycles of ``cycle_s`` at ``duty`` from ``start`` to ``end``: on at each
    cycle's start (just after Versatile Thermostat's receipt of it), off after ``duty`` of it; a
    calculation every minute at that command."""
    events: list[tuple[float, str, object]] = []
    at = start
    while at < end:
        events.append((at, "cycle", (duty, cycle_s)))
        events.append((at, "relay", True))
        if duty < 1.0:
            events.append((at + duty * cycle_s, "relay", False))
        at += cycle_s
    calc = start
    while calc < end:
        events.append((calc, "calc", duty))
        calc += RECALC
    return events


def _following(events: list[tuple[float, str, object]]) -> list[tuple[float, str, object]]:
    """The boiler follows the relay, kept on by it alone, a moment after each change."""
    return [(at + FOLLOW_S, "boiler", value) for at, kind, value in events if kind == "relay"]


@pytest.mark.parametrize(
    ("cycle_s", "duty"),
    [(300.0, 0.6), (3600.0, 0.58), (300.0, 0.99)],
    ids=["5-minute cycle", "60-minute cycle (an underfloor loop)", "off part of 3 s"],
)
async def test_a_relay_cycling_alone_with_the_boiler_is_its_own_cycle(cycle_s, duty, caplog) -> None:
    """The relay keeps the boiler on alone, which follows it: the room's heat starts once, with
    the boiler, and the relay's own off parts (25 minutes in an hour's cycle, or only 3 s) are no
    stop: the estimator sees SmartPI's command throughout, learning never pauses, and SmartPI logs
    no stop of heat and no blink."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    relay = _cycles(cycle_s, duty, 0.0, 3 * cycle_s)
    paused: list[float] = []
    powers: list[float] = []

    def watch(at: float, _kind: str) -> None:
        if algo.boiler_learning_paused:
            paused.append(at)
        if at > BOILER_BLINK_S + 1.0:
            powers.append(algo._dead_time_power())

    await loop.run(relay + _following(relay), watch)

    assert est.last_start_time == pytest.approx(FOLLOW_S) and est.last_stop_time == -10_000.0
    assert not paused
    assert set(powers) == {duty}
    assert "heat stopped" not in caplog.text and "blink" not in caplog.text
    # The FF trim's trace keeps no time without heat from the boiler (the start's 0.01 s is dropped).
    trace = algo._causal_power_trace
    assert trace._unobserved_since is None
    assert not [d for d in trace._discontinuities if d.reason == "central_boiler_off"]


async def test_a_cut_from_outside_with_a_cycling_relay_holds(caplog) -> None:
    """The boiler does not start for the relay (another room's demand is missing): the room gets no
    heat, in the relay's on parts as in its off parts. The estimator sees no power throughout, and
    learning pauses a calculation period after the call and stays paused."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    loop = _Loop(handler)
    paused: list[tuple[float, bool]] = []
    powers: list[float] = []

    def watch(at: float, _kind: str) -> None:
        paused.append((at, algo.boiler_learning_paused))
        powers.append(algo._dead_time_power())

    await loop.run(_cycles(300.0, 0.6, 0.0, 900.0), watch)

    assert set(powers) == {0.0} and algo.dt_est.last_power == 0.0
    assert all(p for at, p in paused if at >= BOILER_PAUSE_CONFIRM_S + 1.0)
    assert not any(p for at, p in paused if at < BOILER_PAUSE_CONFIRM_S)
    assert caplog.text.count("SmartPI learning paused") == 1 and "learning resumed" not in caplog.text
    # The FF trim's trace marks the time without heat from the boiler.
    assert algo._causal_power_trace._unobserved_since[1] == "central_boiler_off"


async def test_after_a_cut_the_heat_starts_with_the_relay_not_with_the_boiler() -> None:
    """An hour's cycle at 50 %: the boiler does not start for the relay's first on part (a cut);
    another room starts it at 2400 s, in the relay's off part. The room gets heat only when the
    relay turns on again, at 3600 s: the cut holds until then, and the heat's start counts from it,
    not 20 minutes too early. (At 50 % the estimator times no response: its 80 % floor.)"""
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    events = _cycles(3600.0, 0.5, 0.0, 4800.0) + [(2400.0, "boiler", True)]
    seen: dict[float, float] = {}

    def watch(at: float, _kind: str) -> None:
        seen[at] = algo._dead_time_power()

    await loop.run(events, watch)

    assert all(power == 0.0 for at, power in seen.items() if at < 3600.0)
    assert seen[3600.0 + RECALC] == 0.5
    assert est.last_start_time == 3600.0


async def test_a_relay_whose_boiler_waits_for_its_activation_delay_starts_from_the_boiler() -> None:
    """The relay turns on at the call; the boiler starts 120 s later (Versatile Thermostat's
    activation delay) and follows the relay after that. The room waits for the boiler: learning
    pauses meanwhile and resumes at the boiler's start, from which the heat's start counts."""
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    relay = _cycles(300.0, 1.0, 0.0, 600.0)
    events = relay + [(120.0, "boiler", True)]
    paused_at: list[float] = []

    def watch(at: float, _kind: str) -> None:
        if algo.boiler_learning_paused:
            paused_at.append(at)

    await loop.run(events, watch)

    assert paused_at and max(paused_at) <= 120.0 and not algo.boiler_learning_paused
    assert est.last_start_time == 120.0 and est.heat_start_time == 120.0


async def test_a_relay_that_the_boiler_stops_following_is_cut_5_s_after_it_turns_on(caplog) -> None:
    """The relay heats the room, the boiler following it, for 480 s; then the boiler stays off as
    the relay turns on again at 600 s (the threshold is no longer reached): the heat is cut 5 s
    after the relay's start, once Versatile Thermostat has had the time to start the boiler, and
    the stop counts from the end of the room's last heat, the relay's off at 480 s."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    est = handler._thermostat.prop_algorithm.dt_est
    loop = _Loop(handler)
    relay = _cycles(300.0, 0.6, 0.0, 900.0)
    boiler = [e for e in _following(relay) if e[0] < 600.0]
    await loop.run(relay + boiler)

    assert est.last_stop_time == 480.0 and est.last_power == 0.0
    assert caplog.text.count("heat stopped with the central boiler") == 1


async def test_a_relay_s_own_stop_after_a_settled_heating_times_its_passive_response() -> None:
    """The relay heats the room for 1200 s in cycles at 90 % (off 30 s in each), the boiler
    following it, then SmartPI stops calling: the heating counts from the first start, not from
    the relay's last on part, so it has settled (longer than the room's 780 s) and the stop times
    the room's passive response."""
    handler = _relay_handler()
    est = handler._thermostat.prop_algorithm.dt_est
    loop = _Loop(handler)
    relay = _cycles(300.0, 0.9, 0.0, 1200.0)
    await loop.run(relay + _following(relay) + [(1200.0, "calc", 0.0)])

    assert est.last_start_time == pytest.approx(FOLLOW_S)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", 1200.0, 1200.0)


async def test_a_thermostat_without_a_cycle_scheduler_follows_the_boiler_as_read() -> None:
    """A climate underlying has no cycle scheduler: the relay's rule does not apply, and the boiler
    going off while SmartPI calls stops the room's heat as for a valve, once it has stayed off
    120 s, dated from its stop, its device off or not (SmartPI's calculation is not run here: the
    test thermostat needs a scheduler for it)."""
    handler = _relay_handler(scheduler=False)
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    algo._committed_on_percent = 1.0
    est.last_power = 1.0
    loop = _Loop(handler)
    await loop.run([(0.0, "boiler", True), (100.0, "boiler", False)])
    assert algo.boiler_on is True  # held as heat
    await loop.run([(100.0 + BOILER_VALVE_HOLD_S, "boiler", False)])

    assert not handler._drives_a_relay()
    assert est.last_stop_time == 100.0 and algo.boiler_on is False and algo._dead_time_power() == 0.0


def test_only_a_relay_s_cycle_scheduler_out_of_valve_mode_is_a_relay() -> None:
    """A switch's cycle scheduler is a relay; a valve's is not, nor a thermostat without one."""
    handler = _relay_handler()
    assert handler._drives_a_relay()
    handler._thermostat.cycle_scheduler.is_valve_mode = True
    assert not handler._drives_a_relay()
    handler._thermostat.cycle_scheduler = None
    assert not handler._drives_a_relay()
    assert isinstance(handler._thermostat.prop_algorithm, SmartPI)


# --- the relay's applied cycle, a cut found late, an outage of the boiler's state


def _cooling_after(end: float) -> Callable[[float], float]:
    """The room flat until its passive response, the room's dead time after the heat's real end,
    then cooling."""
    return lambda at: 20.0 if at < end + DEAD else 20.0 - (at - end - DEAD) * 0.0005


@pytest.mark.parametrize(("cycle_s", "duty", "followed"), [(300.0, 0.9, 4), (3600.0, 0.85, 2)], ids=["5-minute cycle", "60-minute cycle"])
async def test_a_cut_found_after_the_relay_s_own_off_part_is_timed_late(cycle_s, duty, followed, caplog) -> None:
    """The boiler follows the relay, then stops
    following it during an off part (another room stopped): the cut shows only at the relay's next
    on part, 5 s in. The stop counts from the room's last heat, the relay's off; the passive
    response armed then starts late, so it can only come out shorter: shorter than the published
    780 s, it is ignored (0.6.4's rule), where it gave 295 s at an hour's cycle."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    est = handler._thermostat.prop_algorithm.dt_est
    real_end = (followed - 1) * cycle_s + duty * cycle_s
    loop = _Loop(handler, _cooling_after(real_end))
    relay = _cycles(cycle_s, duty, 0.0, real_end + 6000.0)
    boiler = [e for e in _following(relay) if e[0] < real_end + 1.0]
    await loop.run(relay + boiler)

    assert est.last_stop_time == real_end
    assert est.last_outcome["cool"] == "ignored" and list(est._history_cool) == [DEAD] * 6
    assert "Cool Deadtime detected" not in caplog.text


async def test_a_cut_found_late_without_a_published_passive_dead_time_times_none() -> None:
    """As above, at an hour's cycle, but the room has no published passive dead time: a late
    measurement has nothing to be judged against, so none is timed; the stop is recorded."""
    handler = _relay_handler()
    est = handler._thermostat.prop_algorithm.dt_est
    est.deadtime_cool_s, est.deadtime_cool_reliable = None, False
    est._history_cool.clear()
    real_end = 3600.0 + 0.85 * 3600.0
    seen: list[str] = []
    loop = _Loop(handler, _cooling_after(real_end))
    relay = _cycles(3600.0, 0.85, 0.0, real_end + 6000.0)
    boiler = [e for e in _following(relay) if e[0] < real_end + 1.0]
    await loop.run(relay + boiler, lambda _at, _kind: seen.append(est.state))

    assert est.last_stop_time == real_end
    assert "WAITING_COOL_RESPONSE" not in seen and not est._history_cool


async def test_a_relay_off_where_its_cycle_has_it_on_follows_the_boiler(caplog) -> None:
    """The relay at 100 %, the boiler following it, becomes
    unavailable at a cycle's start (Versatile Thermostat no longer counts it, the boiler goes
    off). Its applied cycle has it on: 5 s into the on part the relay's off is no own off part, so
    the boiler's state counts as read: the heat stops, from its last flow, and learning pauses."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    relay = [e for e in _cycles(300.0, 1.0, 0.0, 1500.0) if not (e[1] == "relay" and e[0] >= 600.0)]
    boiler = [e for e in _following(relay) if e[0] < 600.0]
    await loop.run(relay + boiler + [(600.0, "relay", False), (600.01, "boiler", False)])

    assert est.last_stop_time == 600.0 and algo._dead_time_power() == 0.0
    assert algo.boiler_learning_paused and "heat stopped with the central boiler" in caplog.text


async def test_a_relay_that_switches_on_just_after_its_cycle_s_start_is_no_cut(caplog) -> None:
    """Versatile Thermostat reports a cycle's start just before it switches the relay: a relay that
    turns on 2 s into its on part, the boiler following it, is still the relay's own cycle."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    est = handler._thermostat.prop_algorithm.dt_est
    loop = _Loop(handler)
    relay = [(at + 2.0, kind, value) if kind == "relay" and value else (at, kind, value) for at, kind, value in _cycles(300.0, 0.6, 0.0, 1500.0)]
    await loop.run(relay + _following(relay))

    assert est.last_stop_time == -10_000.0 and "heat stopped" not in caplog.text


async def test_an_outage_of_the_boiler_s_state_restarts_the_estimator_s_observation(caplog) -> None:
    """A room with a valve: 300 s of heat with the boiler on; the
    boiler's state unknown from 300 to 1500 s (the real boiler off meanwhile); back on at 1500 s;
    the room's own stop at 1560 s. A response spanning the outage is not attributable: the
    observation restarts as after a restart, so the stop arms no passive measurement (it gave
    0.0 s), and the minimum time without heat counts from the boiler's return."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    handler._thermostat.cycle_scheduler.is_valve_mode = True
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler, lambda at: 20.0 if at < 1080 else 20.0 - (at - 1080) * 0.0005)
    events = [(0.0, "boiler", True)] + [(t, "calc", 1.0) for t in range(0, 1560, 60)]
    events += [(300.0, "boiler", None), (1500.0, "boiler", True), (1560.0, "calc", 0.0)]
    events += [(t, "calc", 0.0) for t in range(1620, 4000, 60)]
    states: list[str] = []
    await loop.run(events, lambda at, _kind: states.append(est.state) if at >= 1560.0 else None)

    assert "WAITING_COOL_RESPONSE" not in states and list(est._history_cool) == [DEAD] * 6
    assert est.last_stop_time == 1560.0 and "dead-time observation restarted" in caplog.text


def test_the_age_of_the_active_period_is_published() -> None:
    """The diagnostics publish how long the active period in progress has lasted, from the last
    power start the estimator saw (``analysis.deadtime.active_elapsed_s``)."""
    from custom_components.vtherm_smartpi.smartpi.diagnostics import (
        build_published_diagnostics,  # noqa: PLC0415
    )

    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    assert est.active_elapsed_s is None
    est.update(100.0, 19.0, 1.0)
    est.update(400.0, 19.0, 1.0)
    assert est.active_elapsed_s == 300.0
    est.update(500.0, 19.0, 0.0)
    assert est.active_elapsed_s is None
    assert "active_elapsed_s" in str(build_published_diagnostics(algo))



async def test_a_relay_room_that_starts_calling_in_a_blink_of_the_boiler_records_its_start(caplog) -> None:
    """The boiler, heating another room, goes off for 8 ms while the relay room starts calling: its
    heat had not flowed, so the boiler's return starts it, and its start is recorded (no "after a
    blink" line for it)."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    events = [(0.0, "boiler", True), (60.0, "calc", 0.0), (100.0, "boiler", False)]
    events += [(100.002, "cycle", (1.0, 300.0)), (100.002, "relay", True), (100.002, "calc", 1.0), (100.008, "boiler", True), (160.0, "calc", 1.0)]

    await loop.run(events)

    assert est.state == "WAITING_HEAT_RESPONSE" and est.last_start_time == pytest.approx(100.008)
    assert "after a blink" not in caplog.text


async def test_a_relay_room_heating_through_a_blink_of_the_boiler_goes_on_heating(caplog) -> None:
    """The relay room heats, in the on part of its cycle, and the boiler goes off for 8 ms (a
    threshold raised): its heat flowed, so the boiler's stop is held as
    heat (3.8.2) and its return is neither a stop nor a blink; the heating period goes on."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    est = algo.dt_est
    loop = _Loop(handler)
    relay = _cycles(600.0, 1.0, 0.0, 600.0)

    await loop.run(relay + _following(relay) + [(300.0, "boiler", False), (300.008, "boiler", True), (360.0, "calc", 1.0)])

    assert est.last_start_time == pytest.approx(FOLLOW_S) and algo._dead_time_power() == 1.0
    assert "heat stopped with the central boiler" not in caplog.text and "after a blink" not in caplog.text


# --- a boiler's stop while a relay's heat flows is held as heat, as for a valve


async def _relay_heating(events: list[tuple[float, str, object]], temp: Callable[[float], float] | None = None) -> tuple[SmartPIHandler, list[tuple[float, bool, float]]]:
    """The relay at 100 % in 5-minute cycles from 0 s, plus ``events``; each event's (time, learning
    paused, power the estimator sees)."""
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    loop = _Loop(handler) if temp is None else _Loop(handler, temp)
    seen: list[tuple[float, bool, float]] = []

    def watch(at: float, _kind: str) -> None:
        seen.append((at, algo.boiler_learning_paused, algo._dead_time_power()))

    await loop.run(_cycles(300.0, 1.0, 0.0, 1200.0) + [(FOLLOW_S, "boiler", True)] + events, watch)
    return handler, seen


async def test_a_short_stop_of_the_boiler_while_the_relay_heats_is_held_as_heat(caplog) -> None:
    """The relay heats the room, the boiler with it; the boiler goes off for 100 s (another room's
    off part, a threshold of devices) while the relay stays on. The water in the radiator goes on
    heating (Bohac 2019, Table 6, p. 77): the stop is held as heat, as for a valve. No stop, no
    pause, no new start; the estimator sees SmartPI's command throughout. Before the hold, the
    heat was cut 5 s into the stop."""
    caplog.set_level(logging.INFO)
    handler, seen = await _relay_heating([(400.0, "boiler", False), (500.0, "boiler", True)])
    est = handler._thermostat.prop_algorithm.dt_est

    assert (est.last_start_time, est.last_stop_time) == (pytest.approx(FOLLOW_S), -10_000.0)
    assert not [at for at, paused, _ in seen if paused]
    assert {power for at, _, power in seen if at > BOILER_BLINK_S + 1.0} == {1.0}
    assert "heat stopped" not in caplog.text and "blink" not in caplog.text


async def test_a_stop_of_the_boiler_longer_than_the_hold_cuts_the_relay_s_heat_from_its_stop(caplog) -> None:
    """The boiler goes off at 400 s while the relay heats and stays off: 120 s later the cut is
    confirmed, dated from the boiler's stop (a dead time counts from the onset of the step,
    Astrom and Hagglund 1995, 2.4, p. 16), and learning pauses at once."""
    caplog.set_level(logging.INFO)
    handler, seen = await _relay_heating([(400.0, "boiler", False)])
    est = handler._thermostat.prop_algorithm.dt_est

    assert est.last_stop_time == 400.0 and est.last_power == 0.0
    assert caplog.text.count("heat stopped with the central boiler") == 1
    paused = [at for at, p, _ in seen if p]
    assert paused and min(paused) >= 400.0 + BOILER_VALVE_HOLD_S - 1.0 and not [at for at, p, _ in seen if 400.0 < at < 400.0 + BOILER_VALVE_HOLD_S - 1.0 and p]


async def test_a_relay_room_that_stops_calling_in_the_hold_stops_from_the_boiler_s_stop() -> None:
    """The boiler goes off at 1000 s while the relay heats; SmartPI stops calling 50 s later,
    inside the hold: the room's heat had stopped with the boiler, so its stop, and the passive
    response armed by it after a settled heating, count from the boiler's stop."""
    handler, _ = await _relay_heating([(1000.0, "boiler", False), (1050.0, "calc", 0.0)])
    est = handler._thermostat.prop_algorithm.dt_est

    assert est.last_stop_time == 1000.0
    assert (est.state, est.cool_start_time) == ("WAITING_COOL_RESPONSE", 1000.0)


async def test_a_held_stop_runs_on_through_the_relay_s_own_off_part() -> None:
    """Cycles of 300 s at 60 %: the boiler goes off at 150 s while the relay heats, the relay's own
    off part starts at 180 s, and the boiler stays off: the hold runs out at 270 s, in the off part,
    and the cut counts from the boiler's stop at 150 s, not from the relay's next on part."""
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    loop = _Loop(handler)
    relay = _cycles(300.0, 0.6, 0.0, 900.0)
    boiler = [e for e in _following(relay) if e[0] < 150.0] + [(150.0, "boiler", False)]
    await loop.run(relay + boiler)

    assert algo.dt_est.last_stop_time == 150.0 and algo.boiler_on is False


async def test_a_second_stop_of_the_boiler_gets_its_own_hold(caplog) -> None:
    """Two stops while the relay heats: 60 s from 400 s, then from 700 s for good. The first is
    held and ends with the boiler's return; the second gets its own hold, and its cut counts from
    its own stop at 700 s, 120 s later, not from the first one."""
    caplog.set_level(logging.INFO)
    handler, seen = await _relay_heating([(400.0, "boiler", False), (460.0, "boiler", True), (700.0, "boiler", False)])
    est = handler._thermostat.prop_algorithm.dt_est

    assert est.last_stop_time == 700.0
    assert caplog.text.count("heat stopped with the central boiler") == 1
    assert not [at for at, paused, _ in seen if paused and at < 700.0 + BOILER_VALVE_HOLD_S - 1.0]


# --- a run of the boiler shorter than its count, while a relay's heat is cut, is no start


async def _cut_relay_with_a_run(run_s: float) -> tuple[SmartPIHandler, list[tuple[float, bool, float]]]:
    """The relay at 100 % in 5-minute cycles from 0 s with the boiler off (the room's heat cut,
    learning paused); at 400 s the boiler runs ``run_s`` and goes off for good. Each event's (time,
    learning paused, power the estimator sees)."""
    handler = _relay_handler()
    algo = handler._thermostat.prop_algorithm
    loop = _Loop(handler)
    seen: list[tuple[float, bool, float]] = []

    def watch(at: float, _kind: str) -> None:
        seen.append((at, algo.boiler_learning_paused, algo._dead_time_power()))

    await loop.run(_cycles(300.0, 1.0, 0.0, 900.0) + [(400.0, "boiler", True), (400.0 + run_s, "boiler", False)], watch)
    return handler, seen


@pytest.mark.parametrize("run_s", [0.03, 2.0, BOILER_BLINK_S - 0.1])
async def test_a_short_run_of_the_boiler_while_a_relay_s_heat_is_cut_starts_nothing(run_s, caplog) -> None:
    """The boiler runs less than 5 s while the relay's heat is cut (a threshold's blink, another
    room's short pulse): it gave the room no heat. No start, no hold, learning stays paused, as
    for a valve: a change counts once it has held for a set time (Adnan 2013, 3.3, p. 30). Before,
    the relay's heat came back at once, its stop was held 120 s, and learning resumed for that
    time."""
    caplog.set_level(logging.INFO)
    handler, seen = await _cut_relay_with_a_run(run_s)
    est = handler._thermostat.prop_algorithm.dt_est

    assert est.last_start_time is None and est.state == "OFF"
    assert not [at for at, paused, _ in seen if at >= 2 * RECALC and not paused]
    assert {power for _, _, power in seen} == {0.0}
    assert "heat stopped" not in caplog.text and "learning resumed" not in caplog.text


async def test_a_run_of_the_boiler_that_counts_is_held_on_a_relay_as_on_a_valve(caplog) -> None:
    """The boiler runs 6 s while the relay's heat is cut: its start counts after 5 s, learning
    resumes then, and its stop is held as heat for 120 s, as for a valve; the boiler still off,
    the cut counts from its stop, and the heating detection armed at the start ends aborted."""
    caplog.set_level(logging.INFO)
    handler, seen = await _cut_relay_with_a_run(6.0)
    est = handler._thermostat.prop_algorithm.dt_est

    assert (est.last_start_time, est.last_stop_time) == (400.0, 406.0)
    assert est.last_outcome["heat"] == "aborted"
    resumed = [at for at, paused, _ in seen if at >= 400.0 and not paused]
    assert resumed and min(resumed) >= 400.0 + BOILER_BLINK_S - 0.01
    assert caplog.text.count("heat stopped with the central boiler") == 1


# --- a relay held off by Versatile Thermostat's minimal deactivation delay is in its own off part


async def _restarted_cycle(protection_s: float, relay_on_at: float, caplog) -> list[tuple[float, bool, float]]:
    """Cycles of 300 s at 60 %: the relay on at 0 s and off at 180 s; a cycle started again at
    200 s (a new command), its on part planned from 200 s to 380 s; the relay turns on at
    ``relay_on_at`` and off 180 s later, the boiler following the relay alone; a calculation every
    minute; Versatile Thermostat's minimal deactivation delay ``protection_s``. Each event's (time,
    learning paused, power the estimator sees)."""
    caplog.set_level(logging.INFO)
    handler = _relay_handler()
    handler._thermostat.minimal_deactivation_delay = protection_s
    algo = handler._thermostat.prop_algorithm
    loop = _Loop(handler)
    relay = [
        (0.0, "cycle", (0.6, 300.0)),
        (0.0, "relay", True),
        (180.0, "relay", False),
        (200.0, "cycle", (0.6, 300.0)),
        (relay_on_at, "relay", True),
        (relay_on_at + 180.0, "relay", False),
    ]
    seen: list[tuple[float, bool, float]] = []

    def watch(at: float, _kind: str) -> None:
        seen.append((at, algo.boiler_learning_paused, algo._dead_time_power()))

    await loop.run(relay + _following(relay) + [(float(at), "calc", 0.6) for at in range(0, 600, 60)], watch)
    return seen


async def test_a_relay_held_off_by_its_minimal_deactivation_delay_is_no_cut(caplog) -> None:
    """Versatile Thermostat keeps the relay off until it has been off for its minimal deactivation
    delay (120 s), so the restarted cycle's on part starts at 300 s, not at 200 s (its cycle
    scheduler documentation, 8.2). The relay is held off by its own protection, not failing to
    switch: no cut, no pause, the estimator sees SmartPI's command throughout (a cause to find
    before acting, Astrom and Hagglund 1995, 6.7, p. 262). Before, the heat was cut 5 s into the
    planned on part."""
    seen = await _restarted_cycle(120.0, 300.0, caplog)

    assert not [at for at, paused, _ in seen if paused]
    assert not [at for at, _, power in seen if at > BOILER_BLINK_S + 1.0 and power == 0.0]
    assert "heat stopped" not in caplog.text


@pytest.mark.parametrize(("protection_s", "cut_from"), [(0.0, 200.0), (60.0, 240.0)])
async def test_a_relay_off_beyond_its_protection_is_a_cut(protection_s, cut_from, caplog) -> None:
    """The same relay turned on only at 300 s, though its protection (none, or 60 s) let it turn on
    at ``cut_from``: it failed to switch, so the boiler is followed as read and the room's heat is
    cut 5 s after that moment, until the relay turns on."""
    seen = await _restarted_cycle(protection_s, 300.0, caplog)

    unpowered = [at for at, _, power in seen if power == 0.0 and at > BOILER_BLINK_S + 1.0]
    assert unpowered and min(unpowered) >= cut_from + BOILER_BLINK_S - 0.01 and max(unpowered) < 300.0 + BOILER_BLINK_S + 0.01
