"""Persistence conversions between monotonic and wall-clock timestamps."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi import timestamp_utils
from custom_components.vtherm_smartpi.smartpi.calibration import CalibrationManager
from custom_components.vtherm_smartpi.smartpi.timestamp_utils import (
    convert_monotonic_to_wall_ts,
    convert_wall_to_monotonic_ts,
    monotonic_to_wall_ts,
    wall_to_monotonic_ts,
)


class _Clock:
    """Controllable wall and monotonic clocks."""

    def __init__(self, wall: float, mono: float) -> None:
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock(wall=1_700_000_000.0, mono=50_000.0)
    monkeypatch.setattr(timestamp_utils, "time", fake)
    return fake


def _restart(clock: _Clock, downtime_s: float, mono_after_boot: float) -> None:
    clock.wall += downtime_s
    clock.mono = mono_after_boot


def test_past_event_round_trip_across_restart(clock: _Clock) -> None:
    wall = monotonic_to_wall_ts(clock.mono - 600.0)
    assert wall == pytest.approx(clock.wall - 600.0)

    _restart(clock, downtime_s=3600.0, mono_after_boot=20.0)
    restored = wall_to_monotonic_ts(wall)
    # 600 s before save plus 3600 s of downtime, relative to the fresh clock.
    assert restored == pytest.approx(20.0 - 4200.0)
    assert clock.monotonic() - restored == pytest.approx(4200.0)


def test_past_event_helpers_handle_missing_and_invalid_values(clock: _Clock) -> None:
    assert monotonic_to_wall_ts(None) is None
    assert wall_to_monotonic_ts(None) is None
    assert wall_to_monotonic_ts("not-a-number") is None
    assert wall_to_monotonic_ts(float("nan")) is None
    assert wall_to_monotonic_ts(0.0) is None


def test_deadline_helpers_still_expire(clock: _Clock) -> None:
    assert convert_monotonic_to_wall_ts(clock.mono - 1.0) is None
    future_wall = convert_monotonic_to_wall_ts(clock.mono + 600.0)
    assert future_wall == pytest.approx(clock.wall + 600.0)

    _restart(clock, downtime_s=60.0, mono_after_boot=10.0)
    assert convert_wall_to_monotonic_ts(future_wall) == pytest.approx(10.0 + 540.0)

    _restart(clock, downtime_s=600.0, mono_after_boot=10.0)
    assert convert_wall_to_monotonic_ts(future_wall) is None


def test_calibration_start_time_round_trip_across_restart(clock: _Clock) -> None:
    mgr = CalibrationManager("test")
    mgr.start_calibration(now=clock.mono - 300.0)
    state = mgr.save_state()
    assert state["calibration_start_time"] == pytest.approx(clock.wall - 300.0)

    _restart(clock, downtime_s=120.0, mono_after_boot=3.0)
    restored = CalibrationManager("test")
    restored.load_state(state)
    assert restored.is_calibrating
    assert restored.calibration_start_time == pytest.approx(3.0 - 420.0)


def test_learning_resume_deadline_is_not_restored(clock: _Clock) -> None:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="resume deadline",
    )
    algo.learn_win.set_learning_resume_ts(clock.mono + 600.0)
    state = algo.save_state()
    assert state["lw_state"]["learning_resume_ts"] == pytest.approx(clock.wall + 600.0)

    _restart(clock, downtime_s=60.0, mono_after_boot=1.0)
    restored = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="resume deadline",
    )
    restored.load_state(state)
    assert restored.learn_win.learning_resume_ts is None
