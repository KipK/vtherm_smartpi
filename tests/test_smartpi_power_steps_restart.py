"""Power steps across a restart: passive period and episode resume."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.const import RESPONSE_LAG_RESTART_MAX_GAP_S
from custom_components.vtherm_smartpi.smartpi.power_steps import PowerSteps

DT_S = 10.0
WALL_OFFSET = 1_700_000_000.0
# The monotonic clock of the new session starts elsewhere (host reboot).
MONO_SHIFT = -50_000.0


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
    """Drive a reference tracker and one restarted at a chosen time.

    The actuator holds its last state during the stop; the restarted tracker
    sees no observation over the stop.
    """

    def __init__(self) -> None:
        self.clock = _Clock()
        self.now = 1000.0
        self.ref = PowerSteps("ref")
        self.est: PowerSteps | None = PowerSteps("est")
        self.saved: dict | None = None
        self.stopped = False

    def run(self, u: float, minutes: float, *, est_u: float | None = None, mode=VThermHvacMode_HEAT):
        for _ in range(int(minutes * 60 / DT_S)):
            self.now += DT_S
            self.clock.mono = self.now
            self.ref.observe(
                now=self.now, dt_s=DT_S, u=u, hvac_mode=VThermHvacMode_HEAT, model=None,
            )
            if self.stopped:
                continue
            self.est.observe(
                now=self.now + self.clock.shift, dt_s=DT_S, u=u if est_u is None else est_u,
                hvac_mode=mode, model=None,
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
            self.est = PowerSteps("est")
            self.est.load_state(self.saved if saved is None else saved)
        self.stopped = False


def _cycles(run: _Run, count: int) -> None:
    for _ in range(count):
        run.run(0.0, 60.0)
        run.run(1.0, 25.0)


def test_episode_resumed_after_a_short_stop_gives_the_uninterrupted_episode():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    run.run(1.0, 17.0)
    run.run(0.0, 10.0)

    assert run.est.episodes_count == run.ref.episodes_count == 3
    assert run.est._durations[-1] == pytest.approx(run.ref._durations[-1], abs=1e-3)


def test_passive_period_resumed_after_a_short_stop():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 40.0)
    run.stop()
    run.run(0.0, 3.0)
    run.start()
    run.run(0.0, 17.0)
    assert run.est._off_start == pytest.approx(run.ref._off_start + MONO_SHIFT, abs=1e-3)


def test_long_stop_does_not_resume_the_passive_period():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 40.0)
    run.stop()
    run.run(0.0, RESPONSE_LAG_RESTART_MAX_GAP_S / 60.0 + 2.0)
    run.start()
    run.run(0.0, 5.0)
    # The passive period starts at the first observation after the restart.
    assert run.est._off_start == pytest.approx(run.now + MONO_SHIFT - 5 * 60.0, abs=1e-3)


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
    # The power after the restart starts a new episode.
    run.run(1.0, 10.0)
    run.run(0.0, 10.0)
    assert run.est.episodes_count == 4


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

    # A mode change clears the tracker, the saved context included.
    assert run.est.episodes_count == 0


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

    # Without the context the episode in progress is not resumed: the run after the restart is a new episode of the
    # same phase.
    assert run.est.episodes_count == 3


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
    est = PowerSteps("t")
    est.load_state({"episodes": 2, "hvac_mode": "heat", "restart": restart})
    assert est._restore is None
    assert est.episodes_count == 2


def test_interrupted_episode_and_the_run_after_a_short_stop_count_once():
    run = _Run()
    _cycles(run, 2)
    run.run(0.0, 60.0)
    run.run(1.0, 6.0)
    run.stop()
    run.run(1.0, 2.0)
    run.start()
    # The power differs after the restart: the episode is counted as ended.
    run.run(1.0, 1.0, est_u=0.5)
    assert run.est.episodes_count == 3
    # The run started by the restart ends: same heating phase.
    run.run(0.0, 10.0)
    assert run.est.episodes_count == 3
