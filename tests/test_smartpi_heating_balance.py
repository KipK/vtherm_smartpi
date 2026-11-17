"""Heating balance: heating rate and emitter lag measured on heating phases."""

from __future__ import annotations

import math
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.diagnostics import _heating_balance_diag
from custom_components.vtherm_smartpi.smartpi.heating_balance import HeatingBalance
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager

A_PER_MIN = 0.05  # full-power heating rate (C/min)
B_PER_MIN = 0.001
T_EXT = 5.0
TAU_S = 300.0
STEP_S = 60.0
WALL_OFFSET = 1_700_000_000.0
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


class _Rig:
    """Room with a first-order emitter, observed every minute like the dead time history."""

    def __init__(self, tau_s: float = TAU_S, a_per_min: float = A_PER_MIN) -> None:
        self.tau_s = tau_s
        self.a_per_min = a_per_min
        self.temp = 19.0
        self.heat = 0.0
        self.now = 1000.0
        self.history: deque = deque()
        self.balance = HeatingBalance("test")
        self.clock = _Clock()

    def advance(self, u: float) -> None:
        """Advance the plant over one step under the power u."""
        for _ in range(int(STEP_S / 10.0)):
            self.heat += (u - self.heat) * (1.0 - math.exp(-10.0 / self.tau_s))
            self.temp += (self.a_per_min * self.heat - B_PER_MIN * (self.temp - T_EXT)) * 10.0 / 60.0
        self.now += STEP_S
        self.clock.mono = self.now
        self.history.append((self.now + self.clock.shift, round(self.temp, 2)))

    def run(self, u: float, minutes: float, *, mode=VThermHvacMode_HEAT, b: float | None = B_PER_MIN,
            context: str = "regulation") -> None:
        for _ in range(int(round(minutes * 60.0 / STEP_S))):
            self.advance(u)
            self.balance.observe(
                now=self.now + self.clock.shift, dt_s=STEP_S, u=u, hvac_mode=mode, b=b,
                tin_history=self.history, context=context,
            )

    def pulse(self, rest_min: float = 60.0, heat_min: float = 40.0, wait_min: float = 300.0) -> None:
        self.run(0.0, rest_min)
        self.run(1.0, heat_min)
        self.run(0.0, wait_min)


def test_pulse_after_a_rest_gives_the_heating_rate_and_the_lag():
    rig = _Rig()
    rig.pulse()
    balance = rig.balance

    assert balance.count == 1
    assert balance.last_reason == "measured"
    assert balance.state == "resting"
    result = balance.results[0]
    assert balance.a_per_min == pytest.approx(A_PER_MIN, rel=0.12)
    assert balance.lag_s == pytest.approx(TAU_S, rel=0.30)
    assert result["constant_power"] is True
    assert result["context"] == "regulation"
    assert result["on_s"] == pytest.approx(40 * 60.0)
    assert result["duration_s"] == pytest.approx(40 * 60.0)
    assert result["rest_s"] == pytest.approx(60 * 60.0)
    assert result["peak_delay_s"] > 0.0
    assert result["a_no_loss"] < result["a"]
    assert result["end_wall"] > 1e9


def test_context_of_the_phase_start_is_kept():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 5.0, context="calibration")
    rig.run(1.0, 35.0, context="regulation")
    rig.run(0.0, 300.0, context="regulation")
    assert rig.balance.results[0]["context"] == "calibration"


def test_rest_before_heating_too_short_gives_no_result():
    rig = _Rig()
    rig.pulse(rest_min=20.0)
    assert rig.balance.count == 0
    assert rig.balance.last_reason == "rest before heating too short"
    assert rig.balance.state == "resting"


def test_baseline_with_too_few_samples_gives_no_result():
    rig = _Rig()
    rig.run(0.0, 30.0)
    rig.history.clear()
    rig.history.extend([(rig.now - 40.0, 19.0), (rig.now - 20.0, 19.0)])
    rig.run(1.0, 40.0)
    rig.run(0.0, 300.0)
    assert rig.balance.count == 0
    assert rig.balance.last_reason == "baseline samples insufficient"


def test_short_phase_and_small_rise_give_no_result():
    rig = _Rig()
    rig.pulse(heat_min=5.0)
    assert rig.balance.count == 0
    assert rig.balance.last_reason in ("heating too short", "temperature rise too small")

    rig = _Rig(a_per_min=0.02)
    rig.run(0.0, 60.0)
    rig.run(1.0, 12.0)
    rig.run(0.0, 300.0)
    assert rig.balance.count == 0
    assert rig.balance.last_reason == "temperature rise too small"


def test_stop_shorter_than_ten_minutes_continues_the_phase():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 20.0)
    rig.run(0.0, 5.0)
    assert rig.balance.state == "waiting for the peak"
    rig.run(1.0, 20.0)
    assert rig.balance.state == "heating"
    rig.run(0.0, 300.0)

    assert rig.balance.count == 1
    result = rig.balance.results[0]
    assert result["constant_power"] is False
    assert result["lag_s"] is None
    assert result["on_s"] == pytest.approx(40 * 60.0)
    assert rig.balance.a_per_min == pytest.approx(A_PER_MIN, rel=0.15)


def test_varying_power_gives_the_rate_without_a_lag():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 20.0)
    rig.run(0.5, 30.0)
    rig.run(0.0, 300.0)
    assert rig.balance.count == 1
    assert rig.balance.results[0]["constant_power"] is False
    assert rig.balance.lag_s is None
    assert rig.balance.a_per_min == pytest.approx(A_PER_MIN, rel=0.15)


def test_peak_not_reached_before_the_next_heating_gives_no_result():
    rig = _Rig(tau_s=1200.0)
    rig.run(0.0, 60.0)
    rig.run(1.0, 40.0)
    rig.run(0.0, 12.0)
    rig.run(1.0, 5.0)
    assert rig.balance.count == 0
    assert rig.balance.last_reason == "still rising at the next heating"
    assert rig.balance.state == "heating"


def test_peak_found_before_the_next_heating_gives_a_result():
    rig = _Rig(tau_s=60.0)
    rig.run(0.0, 60.0)
    rig.run(1.0, 40.0)
    rig.run(0.0, 20.0)
    rig.run(1.0, 5.0)
    assert rig.balance.count == 1
    assert rig.balance.last_reason == "measured"
    assert rig.balance.state == "heating"


def test_temperature_rising_for_the_whole_wait_gives_no_result():
    balance = HeatingBalance("t")
    history: list[tuple[float, float]] = []
    now = 1000.0
    for k in range(60 + 40 + 260):
        now += STEP_S
        u = 1.0 if 60 <= k < 100 else 0.0
        history.append((now, 19.0 + 0.001 * max(0, k - 60)))
        balance.observe(now=now, dt_s=STEP_S, u=u, hvac_mode=VThermHvacMode_HEAT, b=None,
                        tin_history=history, context="regulation")
    assert balance.count == 0
    assert balance.last_reason == "no peak within the wait"
    assert balance.state == "resting"


def test_observation_gap_drops_the_phase():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 15.0)
    assert rig.balance.state == "heating"
    rig.now += 1800.0
    rig.run(0.0, 1.0)
    assert rig.balance.state == "resting"
    assert rig.balance.last_reason == "observation gap"
    rig.run(1.0, 40.0)
    rig.run(0.0, 300.0)
    # The rest start was forgotten with the gap: the next phase is not measurable.
    assert rig.balance.count == 0
    assert rig.balance.last_reason == "rest before heating too short"


def test_cool_mode_is_ignored_and_clears_the_phase():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 15.0)
    rig.run(1.0, 10.0, mode=VThermHvacMode_COOL)
    assert rig.balance.state == "resting"
    rig.run(0.0, 400.0, mode=VThermHvacMode_COOL)
    assert rig.balance.count == 0
    assert rig.balance.state == "resting"


def test_ignores_non_positive_interval_and_non_increasing_time():
    balance = HeatingBalance("t")
    kwargs = dict(u=1.0, hvac_mode=VThermHvacMode_HEAT, b=None, tin_history=[], context="regulation")
    balance.observe(now=100.0, dt_s=0.0, **kwargs)
    assert balance.state == "resting"
    balance.observe(now=100.0, dt_s=60.0, **kwargs)
    assert balance.state == "heating"
    balance.observe(now=100.0, dt_s=60.0, u=0.0, hvac_mode=VThermHvacMode_HEAT, b=None,
                    tin_history=[], context="regulation")
    assert balance.state == "heating"


# ----------------------------------------------------------------------
# Restart
# ----------------------------------------------------------------------


def _restarted(bridged: bool | None, stop_after_cut_min: float = 25.0) -> _Rig:
    """Rig whose balance is saved in the middle of the wait for the peak and reloaded."""
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 40.0)
    rig.run(0.0, stop_after_cut_min)
    assert rig.balance.state == "waiting for the peak"
    with patch("custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", rig.clock):
        saved = rig.balance.save_state()
    # The monotonic clock of the new session starts elsewhere; the stop spans one step.
    rig.clock.shift = MONO_SHIFT
    rig.history = deque((t + MONO_SHIFT, v) for t, v in rig.history)
    rig.balance = HeatingBalance("test")
    with patch("custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", rig.clock):
        rig.balance.load_state(saved)
    assert rig.balance.state == "resting"
    if bridged is not None:
        rig.balance.resolve_restart(bridged)
    rig.now += STEP_S
    rig.advance(0.0)
    rig.balance.observe(
        now=rig.now + rig.clock.shift, dt_s=2 * STEP_S, u=0.0, hvac_mode=VThermHvacMode_HEAT,
        b=B_PER_MIN, tin_history=rig.history, context="regulation",
    )
    return rig


def test_bridged_restart_in_the_wait_gives_the_uninterrupted_result():
    reference = _Rig()
    reference.pulse()
    restarted = _restarted(True)
    assert restarted.balance.state == "waiting for the peak"
    restarted.run(0.0, 300.0 - 27.0)

    assert restarted.balance.count == 1
    ref, res = reference.balance.results[0], restarted.balance.results[0]
    assert res["a"] == pytest.approx(ref["a"], rel=0.02)
    assert res["lag_s"] == pytest.approx(ref["lag_s"], rel=0.02)
    assert res["peak_delay_s"] == pytest.approx(ref["peak_delay_s"], abs=STEP_S)


def test_unbridged_restart_drops_the_phase():
    rig = _restarted(False)
    assert rig.balance.state == "resting"
    assert rig.balance.last_reason == "interrupted by restart"
    rig.run(0.0, 300.0)
    assert rig.balance.count == 0


def test_restart_without_decision_drops_the_phase_at_the_first_observation():
    rig = _restarted(None)
    assert rig.balance.state == "resting"
    assert rig.balance.last_reason == "interrupted by restart"


def test_results_survive_a_restart():
    rig = _Rig()
    rig.pulse()
    with patch("custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", rig.clock):
        saved = rig.balance.save_state()
    restored = HeatingBalance("t")
    restored.load_state(saved)
    assert restored.results == rig.balance.results
    assert restored.a_per_min == rig.balance.a_per_min


def test_clear_empties_everything():
    rig = _Rig()
    rig.pulse()
    rig.run(1.0, 5.0)
    rig.balance.clear()
    assert rig.balance.count == 0
    assert rig.balance.state == "resting"
    assert rig.balance.last_reason == ""
    assert rig.balance.a_per_min is None
    assert rig.balance.lag_s is None


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        [],
        "x",
        {"results": "x"},
        {"results": [None, 3, {"a": 1.0}, {"a": "x"}]},
        {"restart": "x"},
        {"restart": {"phase": "x", "rest_start_wall": 1e9}},
        {"restart": {"phase": {"stage": "cut"}, "rest_start_wall": None}},
        {"restart": {"phase": None, "rest_start_wall": "x"}},
    ],
)
def test_malformed_saved_state_starts_empty(state):
    balance = HeatingBalance("t")
    balance.load_state(state)
    assert balance.count == 0
    assert balance.state == "resting"
    balance.resolve_restart(True)
    assert balance.state == "resting"
    balance.observe(now=100.0, dt_s=60.0, u=1.0, hvac_mode=VThermHvacMode_HEAT, b=None,
                    tin_history=[], context="regulation")
    assert balance.state == "heating"


def test_saved_measurement_with_a_missing_lag_is_kept():
    rig = _Rig()
    rig.run(0.0, 60.0)
    rig.run(1.0, 20.0)
    rig.run(0.0, 5.0)
    rig.run(1.0, 20.0)
    rig.run(0.0, 300.0)
    with patch("custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", rig.clock):
        saved = rig.balance.save_state()
    restored = HeatingBalance("t")
    restored.load_state(saved)
    assert restored.count == 1
    assert restored.results[0]["lag_s"] is None


def test_history_is_bounded():
    rig = _Rig()
    for _ in range(9):
        rig.pulse(rest_min=60.0, heat_min=30.0, wait_min=240.0)
    assert rig.balance.count == 7


# ----------------------------------------------------------------------
# Wiring and diagnostics
# ----------------------------------------------------------------------


def test_learning_window_saves_and_loads_the_heating_balance():
    rig = _Rig()
    rig.pulse()
    with patch("custom_components.vtherm_smartpi.smartpi.timestamp_utils.time", rig.clock):
        saved = rig.balance.save_state()
    window = LearningWindowManager("t")
    window.load_state({"heating_balance": saved})
    assert window.heating_balance.count == 1
    assert window.save_state()["heating_balance"]["results"] == saved["results"]
    window.reset_all()
    assert window.heating_balance.count == 0


def test_learning_window_feeds_the_heating_balance():
    from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
    from custom_components.vtherm_smartpi.smartpi.governance import FreezeReason, GovernanceDecision

    window = LearningWindowManager("t")
    est = ABEstimator()
    dt = MagicMock()
    dt.deadtime_heat_reliable = False
    dt.deadtime_heat_s = None
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = deque()
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    for k, (u, kwargs, context) in enumerate(
        [(1.0, {}, "regulation"), (1.0, {"is_hysteresis": True}, "bootstrap"),
         (1.0, {"is_calibrating": True}, "calibration")]
    ):
        window.heating_balance.clear()
        now = 1000.0 + 600.0 * k
        window.update(
            dt_min=1.0, current_temp=19.0, ext_temp=T_EXT, u_active=u, estimator=est,
            dt_est=dt, governance=gov, now=now, in_deadband=False, in_near_band=False,
            t_heat_episode_start=None, t_cool_episode_start=None,
            hvac_mode=VThermHvacMode_HEAT, target_temp=21.0, raw_temp=19.0, **kwargs,
        )
        assert window.heating_balance.state == "heating"
        assert window.heating_balance._phase["context"] == context


def test_diagnostics_publish_the_heating_balance():
    empty = _heating_balance_diag(HeatingBalance("t"))
    assert empty == {
        "state": "resting", "measurements": 0, "a": None, "lag_s": None,
        "last_reason": "", "last": None,
    }
    rig = _Rig()
    rig.pulse()
    diag = _heating_balance_diag(rig.balance)
    assert set(diag) == {"state", "measurements", "a", "lag_s", "last_reason", "last"}
    assert diag["measurements"] == 1
    assert diag["last_reason"] == "measured"
    assert diag["a"] == pytest.approx(A_PER_MIN, rel=0.12)
    assert set(diag["last"]) == {
        "heating_min", "rise_c", "peak_delay_min", "a", "lag_s", "constant_power", "context",
    }
    assert diag["last"]["heating_min"] == pytest.approx(40.0)
