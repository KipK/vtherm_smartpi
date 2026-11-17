"""The FF trim learns from no window in which the central boiler withheld the applied power.

While Versatile Thermostat's central boiler, which heats the thermostat, is off and SmartPI's
applied power is above 0, the room cools although the trace holds that power. The trim's
observer reads that as a room needing more power for good. SmartPI marks that time in the causal
power trace (``central_boiler_off``), so a window overlapping it is rejected as one with
unobserved power (technical_doc.md 3.8).
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.boiler_call import BOILER_PAUSE_CONFIRM_S
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime
from custom_components.vtherm_smartpi.smartpi.ff_trim import FFTrimThermalSample

T0 = 1000.0
CYCLE_S = 1800.0  # one committed interval spanning the author's 30-minute trim window
DEADTIME_S = 120.0
# The author's cooling drift under heat (test_smartpi_ff_trim_observer.py): -0.2 degC/h.
COOLING = (20.0, 19.9667, 19.9333, 19.9)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def _algo() -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=5.0, minimal_activation_delay=0, minimal_deactivation_delay=0, name="trim-boiler")


def _sample(at: float, temperature: float) -> FFTrimThermalSample:
    return FFTrimThermalSample(
        observed_monotonic=at,
        measurement_id=str(at),
        temperature=temperature,
        target=20.0,
        ff1=0.5,
        regime=GovernanceRegime.DEAD_BAND,
        i_mode="I:FREEZE(deadband)",
        u_pi=0.1,
        hvac_mode="heat",
        saturated=False,
        trajectory_active=False,
        ff3_active=False,
        setpoint_changed=False,
        outside_temperature_available=True,
        model_reliable=True,
        outside_temperature=10.0,
    )


def _window(algo: SmartPI, cycle_start: float):
    """The trim's stationary window over one cycle's power, the room cooling slowly."""
    observer = algo._fftrim_observer
    for index, temperature in enumerate(COOLING):
        observer.record_thermal_sample(
            _sample(cycle_start + DEADTIME_S + index * CYCLE_S / 3, temperature),
            deadtime_s=DEADTIME_S,
            deadtime_reliable=True,
        )
    return observer.try_complete_window(a=0.1, b=0.005, deadtime_s=DEADTIME_S, deadtime_reliable=True, current_trim=0.0)


async def _cycle(algo: SmartPI, clock: Clock, power: float) -> float:
    """One cycle at ``power``, committed as Versatile Thermostat starts and ends it."""
    start = clock.now
    await algo.on_cycle_started(power * CYCLE_S, (1 - power) * CYCLE_S, power, VThermHvacMode_HEAT)
    clock.now = start + CYCLE_S
    await algo.on_cycle_completed(e_eff=power)
    return start


@pytest.fixture
def clock():
    c = Clock()
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", c):
        yield c


@pytest.mark.parametrize("boiler_on", [True, None])
async def test_a_window_with_the_boiler_heating_or_unknown_is_the_author_s(clock, boiler_on) -> None:
    """With the boiler on, or a state that cannot be read, the trim learns from the window as
    without a boiler: a slow cooling under heat asks for more power."""
    algo = _algo()
    algo.set_boiler_state(boiler_on, current_temp=20.0)
    start = await _cycle(algo, clock, 0.5)

    result = _window(algo, start)

    assert result is not None and result.admissible is True
    assert result.correction == pytest.approx(0.0358333, abs=1e-5)


async def test_a_window_while_the_boiler_withholds_the_power_is_rejected(clock) -> None:
    """The boiler off while the room gets 50 % of its power: without the mark the window is
    admissible and raises the trim by 0.036; with it, it is rejected."""
    algo = _algo()
    algo.set_boiler_state(True, current_temp=20.0)
    await algo.on_cycle_started(0.5 * CYCLE_S, 0.5 * CYCLE_S, 0.5, VThermHvacMode_HEAT)
    start = clock.now
    clock.now += 60.0
    algo.set_boiler_state(False, current_temp=20.0)
    clock.now = start + CYCLE_S
    await algo.on_cycle_completed(e_eff=0.5)

    result = _window(algo, start)

    assert result is not None and result.admissible is False
    assert result.reason == "causal_power_not_covered"


async def test_the_mark_holds_while_the_boiler_stays_off_over_several_cycles(clock) -> None:
    """The mark ends at each committed power and is set again: a window over a later cycle,
    the boiler still off, is rejected too."""
    algo = _algo()
    algo.set_boiler_state(False, current_temp=20.0)
    await _cycle(algo, clock, 0.5)
    second = await _cycle(algo, clock, 0.5)
    algo._fftrim_observer.reset_runtime(reset_physical_trace=False)

    result = _window(algo, second)

    assert result is not None and result.admissible is False
    assert result.reason == "causal_power_not_covered"


async def test_no_mark_while_the_room_does_not_call(clock) -> None:
    """At no power the room gets what the trace holds: the boiler off withholds nothing."""
    algo = _algo()
    algo.set_boiler_state(False, current_temp=20.0)
    start = await _cycle(algo, clock, 0.0)

    assert algo._causal_power_trace._unobserved_since is None
    result = _window(algo, start)
    assert result is None or result.reason != "causal_power_not_covered"


async def test_the_mark_ends_with_the_boiler_s_return(clock) -> None:
    """Once the boiler heats again, the next committed power ends the mark and sets none: a
    window over the following cycle is the author's."""
    algo = _algo()
    algo.set_boiler_state(False, current_temp=20.0)
    await _cycle(algo, clock, 0.5)
    algo.set_boiler_state(True, current_temp=20.0)
    start = await _cycle(algo, clock, 0.5)
    algo._fftrim_observer.reset_runtime(reset_physical_trace=False)

    result = _window(algo, start)

    assert result is not None and result.admissible is True


async def test_a_cut_found_late_marks_from_the_room_s_last_heat(clock) -> None:
    """A room on a relay whose cut is found at its next on part: the heat ended earlier
    (``ended_at``), and the mark starts there."""
    algo = _algo()
    algo.set_boiler_state(True, current_temp=20.0)
    await algo.on_cycle_started(0.5 * CYCLE_S, 0.5 * CYCLE_S, 0.5, VThermHvacMode_HEAT)
    ended_at = clock.now + 300.0
    clock.now += 900.0
    algo.set_boiler_state(False, current_temp=20.0, ended_at=ended_at)

    assert algo._causal_power_trace._unobserved_since == (ended_at, "central_boiler_off")


@pytest.mark.parametrize(
    ("off_s", "kept"),
    [(0.03, False), (8.0, False), (BOILER_PAUSE_CONFIRM_S - 0.5, False), (BOILER_PAUSE_CONFIRM_S + 0.5, True)],
    ids=["a blink", "an own start waiting for the boiler", "just under the pause's confirmation", "just over it"],
)
async def test_a_mark_shorter_than_the_pause_s_confirmation_is_dropped(clock, off_s, kept) -> None:
    """As the learning pause, the trim ignores the boiler off for less than one calculation
    period (59 s): a blink of the boiler (milliseconds), or a room's own start waiting seconds
    for it. A longer time without heat rejects the window."""
    algo = _algo()
    algo.set_boiler_state(True, current_temp=20.0)
    await algo.on_cycle_started(0.5 * CYCLE_S, 0.5 * CYCLE_S, 0.5, VThermHvacMode_HEAT)
    start = clock.now
    clock.now += 600.0
    algo.set_boiler_state(False, current_temp=20.0)
    clock.now += off_s
    algo.set_boiler_state(True, current_temp=20.0, since=clock.now)
    clock.now = start + CYCLE_S
    await algo.on_cycle_completed(e_eff=0.5)

    result = _window(algo, start)

    assert result is not None and result.admissible is (not kept)


@pytest.mark.parametrize(("off_s", "kept"), [(40.0, False), (70.0, True)], ids=["under the pause's confirmation", "over it"])
async def test_a_time_without_heat_split_by_a_cycle_s_end_counts_as_a_whole(clock, off_s, kept) -> None:
    """The boiler goes off half of ``off_s`` before a cycle ends and comes back as long after the
    next one started: the cycle's end records the mark's first piece. A time without heat shorter
    than 59 s is dropped as a whole, both pieces; a longer one rejects the window over the first
    cycle. Before, the piece recorded at the cycle's end stayed (Bohac 2019, Table 6, p. 77: a
    panel radiator gives about 95 % of its heat after a stop of 59 s)."""
    algo = _algo()
    algo.set_boiler_state(True, current_temp=20.0)
    await algo.on_cycle_started(0.5 * CYCLE_S, 0.5 * CYCLE_S, 0.5, VThermHvacMode_HEAT)
    start = clock.now
    clock.now = start + CYCLE_S - off_s / 2
    algo.set_boiler_state(False, current_temp=20.0)
    clock.now = start + CYCLE_S
    await algo.on_cycle_completed(e_eff=0.5)
    await algo.on_cycle_started(0.5 * CYCLE_S, 0.5 * CYCLE_S, 0.5, VThermHvacMode_HEAT)
    clock.now += off_s / 2
    algo.set_boiler_state(True, current_temp=20.0, since=clock.now)

    reasons = {d.reason for d in algo._causal_power_trace._discontinuities}
    assert ("central_boiler_off" in reasons) is kept
    assert algo._causal_power_trace._unobserved_since is None
    result = _window(algo, start)
    assert result is not None and result.admissible is (not kept)
