"""Bootstrap on/off output and response lag episode across a short restart."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    RESPONSE_LAG_RESTART_MAX_GAP_S,
    SmartPIPhase,
)

A_PER_S = 0.025 / 60.0  # full-power heating rate (C/s)
B_PER_S = 1.0e-3 / 60.0  # loss coefficient (1/s)
T_EXT = 5.0
LAG_S = 300.0
TICK_S = 60.0
SETPOINT = 18.0
WALL_OFFSET = 1_700_000_000.0
# Monotonic clock of the new session (host reboot).
MONO_SHIFT = -80_000.0


class _Clock:
    def __init__(self) -> None:
        self.t = 100_000.0
        self.shift = 0.0

    def monotonic(self) -> float:
        return self.t + self.shift

    def time(self) -> float:
        return self.t + WALL_OFFSET


class _Room:
    """Room integrator behind a first-order heater lag, 0.01 C sensor."""

    def __init__(self, temp: float) -> None:
        self.temp = temp
        self.heat = 0.0

    def step(self, dt_s: float, u: float) -> None:
        for _ in range(int(dt_s)):
            self.heat += (u - self.heat) / LAG_S
            self.temp += A_PER_S * self.heat - B_PER_S * (self.temp - T_EXT)

    @property
    def reading(self) -> float:
        return round(self.temp, 2)


@contextmanager
def _clocks(clock: _Clock):
    with patch("time.monotonic", clock.monotonic), patch("time.time", clock.time):
        yield


def _smartpi(name: str, saved_state: dict | None = None) -> SmartPI:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name=name,
        saved_state=saved_state,
    )
    if saved_state is None:
        algo.dt_est.deadtime_heat_s = 120.0
        algo.dt_est.deadtime_heat_reliable = True
        algo.dt_est.deadtime_cool_s = 300.0
        algo.dt_est.deadtime_cool_reliable = True
    return algo


class _Run:
    """One room driven by a SmartPI that can be stopped and restarted."""

    def __init__(self) -> None:
        self.clock = _Clock()
        self.room = _Room(18.7)
        with _clocks(self.clock):
            self.algo: SmartPI | None = _smartpi("run")
        self.u = 0.0
        self.saved: dict | None = None

    def tick(self, mode=VThermHvacMode_HEAT) -> None:
        self.room.step(TICK_S, self.u)
        self.clock.t += TICK_S
        if self.algo is None:
            return
        with _clocks(self.clock):
            self.algo.calculate(
                target_temp=SETPOINT,
                current_temp=self.room.reading,
                ext_current_temp=T_EXT,
                slope=0,
                hvac_mode=mode,
            )
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

    def start(self, saved: dict | None = None) -> None:
        self.clock.shift += MONO_SHIFT
        with _clocks(self.clock):
            self.algo = _smartpi("run", self.saved if saved is None else saved)

    @property
    def lag(self):
        return self.algo.learn_win.response_lag


def _to_heating(run: _Run, minutes: int) -> None:
    """Passive period, then minutes into the second heating phase."""
    run.until(lambda: run.u == 1.0)
    run.until(lambda: run.u == 0.0)
    run.until(lambda: run.u == 1.0)
    for _ in range(minutes):
        run.tick()
    assert run.u == 1.0
    assert SETPOINT - 0.3 < run.room.reading < SETPOINT + 0.5
    assert run.algo.phase == SmartPIPhase.HYSTERESIS


def _finish_heating(run: _Run) -> None:
    run.until(lambda: run.u == 0.0)
    run.tick()


def _reference() -> _Run:
    run = _Run()
    _to_heating(run, 20)
    _finish_heating(run)
    return run


def test_short_restart_in_the_band_keeps_heating_and_resumes_the_episode():
    reference = _reference()
    assert reference.lag.last_reason == "estimated"
    expected = reference.lag._estimates[-1][0]

    run = _Run()
    _to_heating(run, 20)
    episodes = run.lag.episodes_count
    estimates = run.lag.estimates_count
    run.stop()
    run.tick()
    run.tick()
    run.start()
    run.tick()

    assert run.algo.ctl.hysteresis_state == "band"
    assert run.u == 1.0
    _finish_heating(run)

    assert run.lag.episodes_count == episodes + 1
    assert run.lag.estimates_count == estimates + 1
    assert run.lag.last_reason == "estimated"
    assert run.lag._estimates[-1][0] == pytest.approx(expected, rel=0.1)


def test_long_restart_starts_off():
    run = _Run()
    _to_heating(run, 20)
    episodes = run.lag.episodes_count
    run.stop()
    for _ in range(int(RESPONSE_LAG_RESTART_MAX_GAP_S / TICK_S) + 1):
        run.tick()
    run.start()
    run.tick()

    assert SETPOINT - 0.3 < run.room.reading < SETPOINT + 0.5
    assert run.u == 0.0
    run.tick()
    assert run.lag.episodes_count == episodes + 1
    assert run.lag.last_reason == "episode interrupted by restart"


def test_other_hvac_mode_does_not_resume_the_output():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    run.tick()
    run.start()
    run.tick(mode=VThermHvacMode_COOL)

    assert run.u == 0.0


def test_outside_the_band_the_thresholds_decide():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    saved = run.saved
    saved["hysteresis_output"]["on"] = False
    run.tick()
    run.room.temp = SETPOINT - 0.5
    run.start(saved)
    run.tick()

    assert run.algo.ctl.hysteresis_state == "on"
    assert run.u == 1.0


def test_saved_off_state_is_resumed_in_the_band():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    saved = run.saved
    saved["hysteresis_output"]["on"] = False
    run.tick()
    run.start(saved)
    run.tick()

    assert run.algo.ctl.hysteresis_state == "band"
    assert run.u == 0.0


def test_stable_phase_does_not_resume_the_output():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    saved = run.saved
    est = saved["est_state"]
    est["a"] = 0.025
    est["b"] = 0.001
    est["learn_ok_count_a"] = 6
    est["learn_ok_count_b"] = 6
    est["a_meas_hist"] = [0.025] * 6
    est["a_obs_hist"] = [None] * 6
    est["b_meas_hist"] = [0.001] * 6
    run.tick()
    run.start(saved)
    assert run.algo.phase == SmartPIPhase.STABLE
    run.tick()

    assert run.algo._hysteresis_restore is None
    assert run.u < 1.0


def test_stable_phase_saves_no_output():
    run = _Run()
    _to_heating(run, 20)
    run.algo.est.learn_ok_count_a = 6
    run.algo.est.a_meas_hist.extend([0.025] * 6)
    run.algo.est.b_meas_hist.extend([0.001] * 6)
    assert run.algo.phase == SmartPIPhase.STABLE
    run.stop()

    assert "hysteresis_output" not in run.saved


def test_legacy_state_starts_off():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    saved = run.saved
    saved.pop("hysteresis_output")
    run.tick()
    run.start(saved)
    run.tick()

    assert run.u == 0.0


def test_output_is_saved_again_before_the_first_calculation():
    run = _Run()
    _to_heating(run, 20)
    run.stop()
    first = run.saved["hysteresis_output"]
    run.start()
    run.stop()

    assert run.saved["hysteresis_output"]["on"] is True
    assert run.saved["hysteresis_output"]["hvac_mode"] == first["hvac_mode"]
    assert run.saved["hysteresis_output"]["wall_ts"] == pytest.approx(first["wall_ts"])
