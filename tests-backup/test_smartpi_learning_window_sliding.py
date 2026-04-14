"""Tests for LearningWindowManager sliding-start and deadtime-anchor fixes."""
import pytest
from unittest.mock import MagicMock

from custom_components.versatile_thermostat.smartpi.learning_window import LearningWindowManager
from custom_components.versatile_thermostat.smartpi.const import (
    DT_MAX_MIN,
    GovernanceDecision,
    FreezeReason,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_estimator():
    est = MagicMock()
    est.learn_skip_count = 0
    est.learn_last_reason = ""
    est.learn_ok_count = 0
    est.learn_ok_count_b = 0
    return est


def _make_dt_est(heat_reliable=True, cool_reliable=False,
                 heat_s=50.0, cool_s=None):
    dt = MagicMock()
    dt.deadtime_heat_reliable = heat_reliable
    dt.deadtime_heat_s = heat_s
    dt.deadtime_cool_reliable = cool_reliable
    dt.deadtime_cool_s = cool_s
    dt.tin_history = []
    return dt


def _make_governance():
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    return gov


# ---------------------------------------------------------------------------
# Bug 1 — Cumulative sliding start timeout
# ---------------------------------------------------------------------------

def test_sliding_start_a_cumulative_timeout():
    """
    When u=1.0 and temperature keeps descending (a_wrong_dir), sliding start
    must time out after DT_MAX_MIN minutes total, not per-tick.

    With the old code, window_dt_min was reset to ~dt_s/60 every tick, making
    DT_MAX_MIN unreachable. The fix accumulates elapsed time in _sliding_elapsed_s.
    """
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est(heat_reliable=False, cool_reliable=False)
    gov = _make_governance()

    dt_s = 20.0          # 20-second tick
    dt_min = dt_s / 60.0
    # Drive enough ticks to exceed DT_MAX_MIN (240 min = 14400 s)
    n_ticks = int(DT_MAX_MIN * 60 / dt_s) + 2

    base_now = 1_000_000.0
    base_temp = 20.5
    ext_temp = 10.0

    abandon_reason = None
    for i in range(n_ticks):
        now = base_now + i * dt_s
        # Slowly descending temperature with u=1.0 → a_wrong_dir
        temp = base_temp - i * 0.01
        win.update(
            dt_min=dt_min,
            current_temp=temp,
            ext_temp=ext_temp,
            u_active=1.0,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=now,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )
        if "timeout" in est.learn_last_reason:
            abandon_reason = est.learn_last_reason
            break

    assert abandon_reason is not None, (
        f"Window never timed out after {n_ticks} ticks "
        f"(last reason: {est.learn_last_reason!r})"
    )
    assert "A deadtime timeout" in abandon_reason
    assert not win.active


def test_sliding_start_b_cumulative_timeout():
    """
    When u=0.0 and temperature keeps rising (b_wrong_dir / B flywheel),
    sliding start must time out cumulatively.
    """
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est(heat_reliable=False, cool_reliable=False)
    gov = _make_governance()

    dt_s = 20.0
    dt_min = dt_s / 60.0
    n_ticks = int(DT_MAX_MIN * 60 / dt_s) + 2

    base_now = 1_000_000.0
    base_temp = 19.0
    ext_temp = 10.0

    abandon_reason = None
    for i in range(n_ticks):
        now = base_now + i * dt_s
        # Rising temperature with u=0.0 → b_wrong_dir
        temp = base_temp + i * 0.01
        win.update(
            dt_min=dt_min,
            current_temp=temp,
            ext_temp=ext_temp,
            u_active=0.0,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=now,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )
        if "timeout" in est.learn_last_reason:
            abandon_reason = est.learn_last_reason
            break

    assert abandon_reason is not None, (
        f"Window never timed out after {n_ticks} ticks "
        f"(last reason: {est.learn_last_reason!r})"
    )
    assert "B flywheel timeout" in abandon_reason
    assert not win.active


def test_sliding_elapsed_resets_on_new_window():
    """_sliding_elapsed_s must be zeroed when a new window opens."""
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est(heat_reliable=False, cool_reliable=False)
    gov = _make_governance()

    dt_s = 20.0
    dt_min = dt_s / 60.0
    base_now = 1_000_000.0

    # Trigger a few sliding ticks (a_wrong_dir: u=1.0, dT < 0)
    for i in range(5):
        win.update(
            dt_min=dt_min,
            current_temp=20.5 - i * 0.01,
            ext_temp=10.0,
            u_active=1.0,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=base_now + i * dt_s,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )

    accumulated = win._sliding_elapsed_s
    assert accumulated > 0.0, "Expected sliding elapsed to accumulate"

    # Reset and open a new valid window (temperature rising with u=1.0)
    win.reset()
    assert win._sliding_elapsed_s == 0.0, "Expected _sliding_elapsed_s reset to 0"


# ---------------------------------------------------------------------------
# Bug 2 — Window anchored to deadtime end instead of perpetual skip
# ---------------------------------------------------------------------------

def test_window_anchored_past_heating_deadtime():
    """
    When the backdated start (now - dt_s) falls inside the heating deadtime,
    the window must open with _start_ts anchored to deadtime_end_ts,
    not be rejected with an infinite skip loop.
    """
    win = LearningWindowManager("test")
    est = _make_estimator()
    gov = _make_governance()

    deadtime_s = 50.0
    dt_s = 20.0              # shorter than deadtime → old code looped forever
    dt_min = dt_s / 60.0

    heat_episode_start = 1_000_000.0
    # Call at a moment well after deadtime has expired
    now = heat_episode_start + deadtime_s + 5.0   # 5s after deadtime end

    dt_est = _make_dt_est(
        heat_reliable=True, heat_s=deadtime_s,
        cool_reliable=False, cool_s=None,
    )
    dt_est.tin_history = []

    win.update(
        dt_min=dt_min,
        current_temp=19.0,
        ext_temp=10.0,
        u_active=1.0,
        setpoint_changed=False,
        estimator=est,
        dt_est=dt_est,
        governance=gov,
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=heat_episode_start,
        t_cool_episode_start=None,
    )

    # Window should have opened with start anchored to deadtime end
    assert win.active, (
        f"Window should be active. last_reason={est.learn_last_reason!r}"
    )
    expected_start = heat_episode_start + deadtime_s
    assert win._start_ts == pytest.approx(expected_start, abs=1e-6), (
        f"Expected _start_ts={expected_start}, got {win._start_ts}"
    )


def test_window_skip_once_when_deadtime_not_expired():
    """
    When the deadtime has NOT yet expired (anchored start >= now),
    exactly one skip should be emitted per call, not an infinite loop.
    """
    win = LearningWindowManager("test")
    est = _make_estimator()
    gov = _make_governance()

    deadtime_s = 50.0
    dt_s = 20.0
    dt_min = dt_s / 60.0

    heat_episode_start = 1_000_000.0
    # Call BEFORE deadtime expires
    now = heat_episode_start + 30.0   # 30s elapsed, deadtime ends at +50s

    dt_est = _make_dt_est(
        heat_reliable=True, heat_s=deadtime_s,
        cool_reliable=False, cool_s=None,
    )

    win.update(
        dt_min=dt_min,
        current_temp=19.0,
        ext_temp=10.0,
        u_active=1.0,
        setpoint_changed=False,
        estimator=est,
        dt_est=dt_est,
        governance=gov,
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=heat_episode_start,
        t_cool_episode_start=None,
    )

    assert not win.active, "Window must not open while deadtime has not expired"
    assert est.learn_skip_count == 1
    # The early deadtime guard (elapsed_episode < deadtime_heat_s) fires first
    # and emits "skip: deadtime window" before reaching the anchor logic.
    assert "deadtime" in est.learn_last_reason


def test_window_anchored_past_cooling_deadtime():
    """
    Same anchor logic applies to cooling deadtime (symmetric case).
    """
    win = LearningWindowManager("test")
    est = _make_estimator()
    gov = _make_governance()

    deadtime_s = 160.0
    dt_s = 20.0
    dt_min = dt_s / 60.0

    cool_episode_start = 2_000_000.0
    now = cool_episode_start + deadtime_s + 10.0   # after cooling deadtime

    dt_est = _make_dt_est(
        heat_reliable=False, heat_s=None,
        cool_reliable=True, cool_s=deadtime_s,
    )
    dt_est.tin_history = []

    win.update(
        dt_min=dt_min,
        current_temp=21.0,
        ext_temp=10.0,
        u_active=0.0,      # OFF → B learning
        setpoint_changed=False,
        estimator=est,
        dt_est=dt_est,
        governance=gov,
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=None,
        t_cool_episode_start=cool_episode_start,
    )

    assert win.active, (
        f"Window should be active after cooling deadtime. reason={est.learn_last_reason!r}"
    )
    expected_start = cool_episode_start + deadtime_s
    assert win._start_ts == pytest.approx(expected_start, abs=1e-6)
