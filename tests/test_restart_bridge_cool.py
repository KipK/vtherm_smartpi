"""Restart bridge in COOL: the bootstrap output and the response lag resume; a mode change is not bridged."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.restart_bridge import REASON_MODE_CHANGED, RestartDecision
from custom_components.vtherm_smartpi.smartpi.tar_learning.history import TarHistory

A_PER_S = 0.025 / 60.0  # full-power cooling rate (C/s)
B_PER_S = 1.0e-3 / 60.0  # gain coefficient (1/s)
T_EXT = 32.0
LAG_S = 300.0
TICK_S = 60.0
SETPOINT = 24.0
WALL_OFFSET = 1_700_000_000.0
MONO_SHIFT = -80_000.0


class _Clock:
    def __init__(self) -> None:
        self.t = 100_000.0
        self.shift = 0.0

    def monotonic(self) -> float:
        return self.t + self.shift

    def time(self) -> float:
        return self.t + WALL_OFFSET


@contextmanager
def _clocks(clock: _Clock):
    with patch("time.monotonic", clock.monotonic), patch("time.time", clock.time):
        yield


class _Room:
    """Room warmed by the outdoor air, cooled behind a first-order lag, 0.01 C sensor."""

    def __init__(self, temp: float) -> None:
        self.temp = temp
        self.cold = 0.0

    def step(self, dt_s: float, u: float) -> None:
        for _ in range(int(dt_s)):
            self.cold += (u - self.cold) / LAG_S
            self.temp += -A_PER_S * self.cold - B_PER_S * (self.temp - T_EXT)

    @property
    def reading(self) -> float:
        return round(self.temp, 2)


def _smartpi(saved: dict | None = None) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="cool", saved_state=saved)
    if saved is None:
        algo.dt_est.deadtime_cool_s = 120.0
        algo.dt_est.deadtime_cool_reliable = True
        algo.dt_est.deadtime_heat_s = 300.0
        algo.dt_est.deadtime_heat_reliable = True
    return algo


class _Run:
    """A room in COOL driven by the bootstrap hysteresis, with stops and restarts."""

    def __init__(self) -> None:
        self.clock = _Clock()
        self.room = _Room(SETPOINT + 0.2)
        with _clocks(self.clock):
            self.algo: SmartPI | None = _smartpi()
        self.u = 0.0
        self.saved: dict | None = None

    def tick(self, mode=VThermHvacMode_COOL) -> None:
        self.room.step(TICK_S, self.u)
        self.clock.t += TICK_S
        if self.algo is None:
            return
        with _clocks(self.clock):
            self.algo.calculate(target_temp=SETPOINT, current_temp=self.room.reading, ext_current_temp=T_EXT,
                                slope=0, hvac_mode=mode)
        self.u = self.algo.on_percent

    def until(self, predicate, limit: int = 1000) -> None:
        for _ in range(limit):
            self.tick()
            if predicate():
                return
        raise AssertionError("condition not reached")

    def stop(self) -> None:
        with _clocks(self.clock):
            self.saved = self.algo.save_state()
        self.algo = None

    def start(self) -> None:
        self.clock.shift += MONO_SHIFT
        with _clocks(self.clock):
            self.algo = _smartpi(self.saved)

    @property
    def lag(self):
        return self.algo.learn_win.response_lag


def _to_cooling(run: _Run, minutes: int) -> None:
    """A passive period, then minutes into the second cooling phase, inside the band."""
    run.until(lambda: run.u == 1.0)
    run.until(lambda: run.u == 0.0)
    run.until(lambda: run.u == 1.0)
    for _ in range(minutes):
        run.tick()
    assert run.u == 1.0
    assert run.algo.phase == SmartPIPhase.HYSTERESIS
    assert run.lag._ep_start is not None


def test_short_restart_in_cool_resumes_the_output_and_the_response_lag() -> None:
    reference = _Run()
    _to_cooling(reference, 10)
    reference.until(lambda: reference.u == 0.0)
    reference.tick()

    run = _Run()
    _to_cooling(run, 10)
    episodes = run.lag.episodes_count
    run.stop()
    run.tick()
    run.tick()
    run.start()
    run.tick()

    decision = run.algo.restart.decision
    assert decision.bridged and decision.hvac_mode == "cool"
    assert run.algo.ctl.hysteresis_state == "band"
    assert run.u == 1.0  # the cooling phase goes on
    assert run.lag.episodes_count == episodes
    assert run.lag.last_reason != "episode interrupted by restart"
    run.until(lambda: run.u == 0.0)
    run.tick()
    assert run.lag.episodes_count == reference.lag.episodes_count
    assert run.lag.estimates_count == reference.lag.estimates_count
    assert run.lag.last_reason == reference.lag.last_reason


def test_heat_to_cool_change_is_not_bridged() -> None:
    run = _Run()
    _to_cooling(run, 10)
    run.stop()
    run.saved["restart"]["hvac_mode"] = str(VThermHvacMode_HEAT)  # heating before the stop
    run.tick()
    run.start()
    run.tick()

    decision = run.algo.restart.decision
    assert not decision.bridged and decision.reason == REASON_MODE_CHANGED
    assert run.u == 0.0  # the saved output is not resumed
    assert run.lag.last_reason == "episode interrupted by restart"


def test_learning_histories_keep_their_non_heating_handling_in_cool() -> None:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="cool", tar_learning=True, room_model=True)
    h = TarHistory()
    h.record(0.0, temp=24.0, ext=30.0, setpoint=24.0, phase="HYSTERESIS", b=None, dtc=None, lag=None)
    algo.tar_learning.history = TarHistory.from_dict(json.loads(json.dumps(h.to_dict(10.0))))
    cool = RestartDecision(True, 0.0, 60.0, 60.0, None, "cool")
    algo._apply_restart_decision(cool, None)
    assert algo.tar_learning.history._restart_bridged is False
    heat = RestartDecision(True, 0.0, 60.0, 60.0, None, "heat")
    assert heat.heat_bridged and not cool.heat_bridged


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_hvac_mode_of_the_last_tick_is_saved(mode) -> None:
    run = _Run()
    run.tick(mode=mode)
    run.stop()
    assert run.saved["restart"]["hvac_mode"] == str(mode)
    assert run.saved["restart"]["committed"] is not None
