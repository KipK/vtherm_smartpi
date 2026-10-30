"""Response lag estimator across a restart: passive period and episode resume."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import RESPONSE_LAG_RESTART_MAX_GAP_S
from custom_components.vtherm_smartpi.smartpi.response_lag import ResponseLagEstimator

A_PER_S = 0.025 / 60.0  # full-power heating rate (C/s)
B_PER_MIN = 3.0e-4
T_EXT = 5.0
LAG_S = 300.0
DT_S = 10.0
WALL_OFFSET = 1_700_000_000.0
# The monotonic clock of the new session starts elsewhere (host reboot).
MONO_SHIFT = -50_000.0


class _Plant:
    """Room integrator behind a first-order heater lag, 0.01 C sensor."""

    def __init__(self) -> None:
        self.temp = 19.0
        self.heat = 0.0

    def step(self, dt_s: float, u: float) -> None:
        for _ in range(int(dt_s)):
            self.heat += (u - self.heat) / LAG_S
            self.temp += A_PER_S * self.heat - B_PER_MIN / 60.0 * (self.temp - T_EXT)

    @property
    def reading(self) -> float:
        return round(self.temp, 2)


class _Clock:
    """Monotonic and wall clocks of the timestamp helpers."""

    def __init__(self) -> None:
        self.mono = 0.0
        self.shift = 0.0

    def monotonic(self) -> float:
        return self.mono + self.shift

    def time(self) -> float:
        return self.mono + WALL_OFFSET


class _Run:
    """Drive a reference estimator and one restarted at a chosen time.

    The plant keeps its power during the stop (the actuator holds its last
    state); the restarted estimator sees neither observations nor samples
    over the stop, and its history is converted to the new monotonic frame
    like the restored dead time history.
    """

    def __init__(self) -> None:
        self.plant = _Plant()
        self.clock = _Clock()
        self.now = 1000.0
        self.ref = ResponseLagEstimator("ref")
        self.est: ResponseLagEstimator | None = ResponseLagEstimator("est")
        self.ref_hist: list[tuple[float, float]] = []
        self.est_hist: list[tuple[float, float]] = []
        self.saved: dict | None = None
        self.stopped = False

    def run(self, u: float, minutes: float, *, est_u: float | None = None, mode=VThermHvacMode_HEAT):
        for _ in range(int(minutes * 60 / DT_S)):
            self.plant.step(DT_S, u)
            self.now += DT_S
            self.clock.mono = self.now
            sample = self.plant.reading
            self.ref_hist.append((self.now, sample))
            self.ref.observe(
                now=self.now, dt_s=DT_S, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=None, tin_history=self.ref_hist,
            )
            if self.stopped:
                continue
            local = self.now + self.clock.shift
            self.est_hist.append((local, sample))
            self.est.observe(
                now=local, dt_s=DT_S, u=u if est_u is None else est_u, t_ext=T_EXT,
                hvac_mode=mode, b=None, tin_history=self.est_hist,
            )

    def stop(self) -> None:
        with patch(
            "custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", self.clock
        ):
            self.saved = self.est.save_state()
        self.est = None
        self.stopped = True

    def start(self, saved: dict | None = None) -> None:
        self.clock.shift = MONO_SHIFT
        with patch(
            "custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", self.clock
        ):
            self.est = ResponseLagEstimator("est")
            self.est.load_state(self.saved if saved is None else saved)
        self.est_hist = [(t + MONO_SHIFT, v) for t, v in self.est_hist]
        self.stopped = False


def _cycles(run: _Run, count: int) -> None:
    for _ in range(count):
        run.run(0.0, 60.0)
        run.run(1.0, 25.0)


def test_episode_resumed_after_a_short_stop_gives_the_uninterrupted_estimate():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    run.run(1.0, 17.0)
    run.run(0.0, 10.0)

    assert run.ref.estimates_count == 3
    assert run.est.estimates_count == 3
    assert run.est.last_reason == "estimated"
    assert run.est.episodes_count == run.ref.episodes_count == 3
    assert run.est._estimates[-1][0] == pytest.approx(run.ref._estimates[-1][0], rel=0.02)
    assert run.est.lag_s == pytest.approx(run.ref.lag_s, rel=0.02)


def test_passive_period_resumed_after_a_short_stop():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 40.0)
    run.stop()
    run.run(0.0, 3.0)
    run.start()
    run.run(0.0, 17.0)
    run.run(1.0, 25.0)
    run.run(0.0, 10.0)

    assert run.est.estimates_count == 3
    assert run.est.last_reason == "estimated"
    assert run.est._estimates[-1][0] == pytest.approx(run.ref._estimates[-1][0], rel=0.05)


def test_passive_period_without_samples_before_the_restart_has_no_baseline():
    """Only samples after the restart: no crossing in the tail, no estimate."""
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 40.0)
    run.stop()
    run.run(0.0, 3.0)
    run.start()
    run.est_hist.clear()
    run.run(1.0, 25.0)
    run.run(0.0, 10.0)

    assert run.est.estimates_count == 2
    assert run.est.last_reason == "no baseline crossing"


def test_long_stop_does_not_resume_the_passive_period():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 40.0)
    run.stop()
    run.run(0.0, RESPONSE_LAG_RESTART_MAX_GAP_S / 60.0 + 2.0)
    run.start()
    run.run(0.0, 5.0)
    run.run(1.0, 25.0)
    run.run(0.0, 10.0)

    assert run.est.estimates_count == 2
    assert run.est.last_reason == "passive baseline not settled"


def test_long_stop_counts_the_interrupted_episode():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, RESPONSE_LAG_RESTART_MAX_GAP_S / 60.0 + 2.0)
    run.start()
    run.run(1.0, 1.0)

    assert run.est.episodes_count == 3
    assert run.est.estimates_count == 2
    assert run.est.last_reason == "episode interrupted by restart"
    # The power after the restart starts a new episode without a baseline.
    run.run(1.0, 10.0)
    run.run(0.0, 10.0)
    assert run.est.episodes_count == 4
    assert run.est.last_reason == "passive baseline not settled"


def test_power_change_counts_the_interrupted_episode():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    run.run(1.0, 1.0, est_u=0.5)

    assert run.est.episodes_count == 3
    assert run.est.estimates_count == 2
    assert run.est.last_reason == "episode interrupted by restart"
    assert run.est.episode_start == pytest.approx(run.now + MONO_SHIFT - 60.0)


def test_power_off_after_the_restart_counts_the_episode_without_passive_resume():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    run.run(0.0, 1.0, est_u=0.0)

    assert run.est.episodes_count == 3
    assert run.est.last_reason == "episode interrupted by restart"
    assert run.est.episode_start is None
    # The passive period starts at the restart, not before the episode.
    assert run.est._off_start == pytest.approx(run.now + MONO_SHIFT - 60.0)


def test_mode_change_does_not_resume():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    run.run(1.0, 1.0, mode=VThermHvacMode_COOL)

    # A mode change clears the estimator, the saved context included.
    assert run.est.episodes_count == 0
    assert run.est.estimates_count == 0
    assert run.est.last_reason == ""


def test_legacy_state_without_restart_context():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    legacy = {k: v for k, v in run.saved.items() if k != "restart"}
    run.run(1.0, 2.0)
    run.start(legacy)
    run.run(1.0, 17.0)
    run.run(0.0, 10.0)

    assert run.est.episodes_count == 3
    assert run.est.estimates_count == 2
    assert run.est.last_reason == "passive baseline not settled"


def test_context_is_saved_again_before_the_first_observation():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    first = run.saved
    run.run(1.0, 1.0)
    run.start()
    # Second stop before any observation: the same context is saved.
    run.stop()
    assert run.saved["restart"]["episode"]["u0"] == first["restart"]["episode"]["u0"]
    assert run.saved["restart"]["last_obs_wall"] == pytest.approx(
        first["restart"]["last_obs_wall"], abs=1e-3
    )
    assert run.saved["restart"]["episode"]["start_wall"] == pytest.approx(
        first["restart"]["episode"]["start_wall"], abs=1e-3
    )


def test_saved_context_round_trip():
    run = _Run()
    _cycles(run, 1)
    run.run(0.0, 30.0)
    run.stop()
    restart = run.saved["restart"]
    assert restart["episode"] is None
    assert restart["last_obs_wall"] == pytest.approx(run.now + WALL_OFFSET)
    assert restart["off_start_wall"] == pytest.approx(run.now - 30 * 60.0 + WALL_OFFSET)

    run.start()
    assert run.est._restore["off_start"] == pytest.approx(run.now - 30 * 60.0 + MONO_SHIFT)
    assert run.est._restore["last_obs"] == pytest.approx(run.now + MONO_SHIFT)


@pytest.mark.parametrize(
    "restart",
    [
        "x",
        {"last_obs_wall": None, "off_start_wall": 1.0, "episode": None},
        {"last_obs_wall": WALL_OFFSET, "off_start_wall": WALL_OFFSET + 10.0, "episode": None},
        {"last_obs_wall": WALL_OFFSET, "episode": {"start_wall": WALL_OFFSET, "u0": "1",
                                                   "const": True, "off_before_s": None,
                                                   "t_ext": None}},
        {"last_obs_wall": WALL_OFFSET, "episode": {"start_wall": WALL_OFFSET, "u0": 1.0,
                                                   "const": 1, "off_before_s": None,
                                                   "t_ext": None}},
    ],
)
def test_invalid_restart_context_is_ignored(restart):
    est = ResponseLagEstimator("t")
    est.load_state({"estimates": [], "episodes": 2, "hvac_mode": "heat", "restart": restart})
    assert est._restore is None
    assert est.episodes_count == 2
