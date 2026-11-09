"""Restart bridge in the regulation core: work in progress across a short restart."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import RESTART_BRIDGE_MAX_GAP_S, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.integral_guard import IntegralGuardSource

A_PER_S = 0.03 / 60.0  # full-power heating rate (C/s)
B_PER_S = 1.0e-3 / 60.0  # loss coefficient (1/s)
T_EXT = 5.0
LAG_S = 240.0
TICK_S = 60.0
CYCLE_TICKS = 10
WALL_OFFSET = 1_700_000_000.0
MONO_SHIFT = -80_000.0  # monotonic clock of the new session (host reboot)


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


def _seed(algo: SmartPI) -> None:
    """A learned model (stable phase), reliable dead times, learning on."""
    est = algo.est
    a_min, b_min = A_PER_S * 60.0, B_PER_S * 60.0
    for _ in range(40):
        est.a_meas_hist.append(a_min)
        est.b_meas_hist.append(b_min)
    est.a, est.b = a_min, b_min
    est.learn_ok_count_a = est.learn_ok_count_b = 40
    est.learn_ok_count = 80
    for i in range(10):
        est._b_hat_hist.append(b_min * (1.0 + 0.01 * (i - 5)))
    algo.dt_est.deadtime_heat_s = 180.0
    algo.dt_est.deadtime_heat_reliable = True
    algo.dt_est.deadtime_cool_s = 300.0
    algo.dt_est.deadtime_cool_reliable = True
    algo._output_initialized = True
    algo._cycles_since_reset = algo.ff_warmup_cycles  # as restored with a learned model


def _smartpi(saved: dict | None = None) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=CYCLE_TICKS * TICK_S / 60.0, minimal_activation_delay=0,
                   minimal_deactivation_delay=0, name="bridge", saved_state=saved)
    if saved is None:
        _seed(algo)
    return algo


class _Run:
    """A room regulated by SmartPI, with cycles committed like the scheduler does, and restarts.

    The heater holds the committed power; during a stop it keeps it (the
    actuator keeps its last state). After a restart the thermostat calculates
    twice (recalculate, then the cycle control) and starts a new cycle.
    """

    def __init__(self, setpoint: float = 19.0, temp: float = 18.0) -> None:
        self.clock = _Clock()
        self.room = _Room(temp)
        self.setpoint = setpoint
        with _clocks(self.clock):
            self.algo: SmartPI | None = _smartpi()
        self.u = 0.0
        self.k = 0  # tick index within the cycle
        self.saved: dict | None = None
        self.loop = asyncio.new_event_loop()

    def _calc(self, boundary: bool = False) -> None:
        self.algo.calculate(target_temp=self.setpoint, current_temp=self.room.reading, ext_current_temp=T_EXT,
                            slope=0, hvac_mode=VThermHvacMode_HEAT, cycle_boundary=boundary)

    def _commit(self) -> None:
        self.algo._commit_applied_command(self.algo.on_percent, VThermHvacMode_HEAT, None, cycle_start=True)
        self.u = self.algo.committed_on_percent

    def tick(self) -> None:
        self.room.step(TICK_S, self.u)
        self.clock.t += TICK_S
        if self.algo is None:
            self.k = (self.k + 1) % CYCLE_TICKS
            return
        with _clocks(self.clock):
            if self.k == 0:
                self.loop.run_until_complete(self.algo.on_cycle_completed(e_eff=self.u, elapsed_ratio=1.0))
                self._calc(boundary=True)
                self._commit()
            else:
                self._calc()
        self.k = (self.k + 1) % CYCLE_TICKS

    def ticks(self, n: int) -> None:
        for _ in range(n):
            self.tick()

    def stop(self) -> None:
        with _clocks(self.clock):
            self.saved = self.algo.save_state()
        self.algo = None

    def start(self, saved: dict | None = None) -> None:
        """Restart and run the first tick as the thermostat does (calculations, then a forced cycle)."""
        self.clock.shift += MONO_SHIFT
        self.room.step(TICK_S, self.u)
        self.clock.t += TICK_S
        with _clocks(self.clock):
            self.algo = _smartpi(self.saved if saved is None else saved)
            self._calc()  # recalculate()
            self.clock.t += 0.05
            self._calc()  # then the cycle control, a few milliseconds later
            self._commit()
        self.k = 1

    @property
    def lag(self):
        return self.algo.learn_win.response_lag


def _to_heating_episode() -> _Run:
    """Warm room (passive period), then a setpoint step: an episode of the response lag starts."""
    run = _Run(setpoint=17.0, temp=19.0)
    run.ticks(60)
    run.setpoint = 20.0
    run.ticks(15)
    assert run.lag._ep_start is not None
    return run


# ---------------------------------------------------------------------------
# Response lag: an episode in progress across a short restart
# ---------------------------------------------------------------------------
def test_response_lag_episode_resumes_across_a_short_restart() -> None:
    """The first observation after a load used to see a zero power: the episode was counted interrupted."""
    run = _to_heating_episode()
    episodes = run.lag.episodes_count
    with _clocks(run.clock):
        start_wall = run.clock.time() - (run.clock.monotonic() - run.lag._ep_start)
    run.stop()
    run.tick()
    run.start()
    run.tick()

    assert run.lag.last_reason != "episode interrupted by restart"
    assert run.lag.episodes_count == episodes
    assert run.lag._ep_start is not None
    with _clocks(run.clock):
        assert run.clock.time() - (run.clock.monotonic() - run.lag._ep_start) == pytest.approx(start_wall, abs=1.0)


def test_response_lag_episode_is_interrupted_by_a_long_restart() -> None:
    run = _to_heating_episode()
    episodes = run.lag.episodes_count
    run.stop()
    run.ticks(int(RESTART_BRIDGE_MAX_GAP_S / TICK_S) + 1)
    run.start()
    run.tick()

    assert run.algo.restart.decision.bridged is False
    assert run.lag.episodes_count == episodes + 1
    assert run.lag.last_reason == "episode interrupted by restart"


# ---------------------------------------------------------------------------
# Committed power, integral guard, learning freeze
# ---------------------------------------------------------------------------
def test_bridged_restart_restores_the_committed_power_and_the_integral_guard() -> None:
    run = _to_heating_episode()
    algo = run.algo
    algo.integral_guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=True)
    algo.integral_guard._stall_min = 7.0
    committed = algo.committed_on_percent
    assert committed > 0.0
    run.stop()
    run.tick()
    with _clocks(run.clock):
        restored = _smartpi(run.saved)
        assert restored.committed_on_percent == 0.0  # nothing resumes before the decision
        decision = restored.resolve_restart(VThermHvacMode_HEAT, run.room.reading)
    assert decision.bridged
    assert decision.downtime_s == pytest.approx(TICK_S, abs=1.0)
    assert restored.committed_on_percent == pytest.approx(committed)
    assert restored.u_prev == pytest.approx(committed)
    assert restored.integral_guard.source == IntegralGuardSource.SETPOINT_CHANGE
    assert restored.integral_guard._stall_min == 7.0
    diag = restored.get_published_diagnostics()["control"]["last_restart"]
    assert diag == {"bridged": True, "downtime_s": 60, "reason": None}


def test_bridged_restart_has_no_learning_freeze() -> None:
    run = _to_heating_episode()
    run.stop()
    run.tick()
    run.start()
    assert run.algo.learn_win.learning_resume_ts is None
    assert run.algo.est.learn_last_reason != "skip: governance (perturbed)"


@pytest.mark.parametrize("gap_ticks, legacy", [(int(RESTART_BRIDGE_MAX_GAP_S / TICK_S) + 1, False), (1, True)])
def test_long_or_unknown_restart_behaves_as_before(gap_ticks: int, legacy: bool) -> None:
    run = _to_heating_episode()
    run.algo.integral_guard.arm(IntegralGuardSource.SETPOINT_CHANGE, block_positive=True)
    run.stop()
    if legacy:
        run.saved.pop("restart")  # a state saved without the heater state
    run.ticks(gap_ticks)
    with _clocks(run.clock):
        restored = _smartpi(run.saved)
        restored.calculate(target_temp=run.setpoint, current_temp=run.room.reading, ext_current_temp=T_EXT,
                           slope=0, hvac_mode=VThermHvacMode_HEAT)
    assert restored.restart.decision.bridged is False
    assert restored.committed_on_percent == 0.0
    assert restored.integral_guard.source == IntegralGuardSource.NONE
    assert restored.learn_win.learning_resume_ts is not None  # learning frozen for one cycle
    assert not restored.learn_win.active
    assert restored.dt_est.state == "OFF"


# ---------------------------------------------------------------------------
# a/b learning window across a bridged restart
# ---------------------------------------------------------------------------
def _window_run() -> tuple[_Run, int]:
    """Run until an a/b window has been open for 5 minutes, 3 ticks before a cycle boundary."""
    run = _Run(setpoint=19.0, temp=18.95)
    for i in range(3000):
        run.tick()
        win = run.algo.learn_win
        if win.active and win.t_int_s >= 300.0 and run.k == CYCLE_TICKS - 3:
            return run, i + 1
    raise AssertionError("no learning window")


def test_learning_window_spanning_a_bridged_restart_completes_as_without_it() -> None:
    ref, n = _window_run()
    ok_before = ref.algo.est.learn_ok_count
    start_wall = ref.algo.learn_win.start_ts - ref.clock.monotonic() + ref.clock.time()
    ref.ticks(120)

    run, _ = _window_run()
    assert run.algo.est.learn_ok_count == ok_before
    run.stop()
    run.ticks(2)
    run.start()  # at the cycle boundary of the reference
    win = run.algo.learn_win
    assert run.algo.restart.decision.bridged
    assert win.active
    assert win.start_ts - run.clock.monotonic() + run.clock.time() == pytest.approx(start_wall, abs=1.0)
    run.ticks(119)

    assert run.algo.est.learn_ok_count == ref.algo.est.learn_ok_count > ok_before
    assert run.algo.est.a == pytest.approx(ref.algo.est.a, rel=0.02)
    assert run.algo.est.b == pytest.approx(ref.algo.est.b, rel=0.02)


def test_learning_window_is_dropped_by_a_restart_that_is_not_bridged() -> None:
    run, _ = _window_run()
    run.stop()
    run.ticks(int(RESTART_BRIDGE_MAX_GAP_S / TICK_S) + 1)
    run.start()
    assert run.algo.restart.decision.bridged is False
    assert not run.algo.learn_win.active


# ---------------------------------------------------------------------------
# Dead time detection in progress
# ---------------------------------------------------------------------------
def _passive_response(est: DeadTimeEstimator, clock: _Clock, t_from: float, t_to: float) -> None:
    """Room at 20 C after a full-power cut at t = 0: flat for 1200 s, then cooling 1 C/h."""
    t = t_from
    while t <= t_to:
        clock.t = t
        temp = 20.0 if t < 1200.0 else 20.0 - (t - 1200.0) / 3600.0
        with _clocks(clock):
            est.update(now=clock.monotonic(), tin=temp, u_applied=0.0 if t >= 0.0 else 1.0,
                       hvac_mode=VThermHvacMode_HEAT)
        t += 60.0


@pytest.mark.parametrize("bridged", [True, False])
def test_dead_time_detection_resumes_across_a_bridged_restart(bridged: bool) -> None:
    clock = _Clock()
    ref = DeadTimeEstimator()
    _passive_response(ref, clock, -1200.0, 3600.0)
    assert ref.last_outcome["cool"] == "detected"

    clock = _Clock()
    est = DeadTimeEstimator()
    _passive_response(est, clock, -1200.0, 600.0)
    assert est.state == "WAITING_COOL_RESPONSE"
    with _clocks(clock):
        saved = est.save_state()
    clock.shift = MONO_SHIFT
    clock.t = 720.0
    with _clocks(clock):
        back = DeadTimeEstimator()
        back.load_state(saved)
        assert back.state == "OFF"
        back.resume_after_restart(bridged)
    _passive_response(back, clock, 720.0, 3600.0)
    if bridged:
        assert back.last_outcome["cool"] == "detected"
        assert back.deadtime_cool_s == pytest.approx(ref.deadtime_cool_s, abs=1.0)
    else:
        assert back.last_outcome["cool"] is None
        assert back.deadtime_cool_s is None


# ---------------------------------------------------------------------------
# Handler: one decision before the first observations, no cycle reset
# ---------------------------------------------------------------------------
def test_first_timer_start_after_a_bridged_restart_keeps_the_cycle_state() -> None:
    algo = MagicMock(spec=SmartPI)
    algo.restart_bridged = True
    t = SimpleNamespace(vtherm_hvac_mode=VThermHvacMode_HEAT, prop_algorithm=algo, name="t")
    handler = SmartPIHandler(t)
    with patch.object(handler, "_start_recalc_timer"):
        asyncio.run(handler.on_state_changed(True))
        algo.reset_cycle_state.assert_not_called()
        handler._recalc_timer_remove = None  # stopped by an OFF period
        asyncio.run(handler.on_state_changed(True))
        algo.reset_cycle_state.assert_called_once()


def test_restart_exclusion_seen_by_the_handler() -> None:
    t = SimpleNamespace(window_manager=SimpleNamespace(is_window_detected=False), hvac_off_reason=None,
                        is_overpowering_detected=False, safety_manager=SimpleNamespace(is_safety_detected=True))
    handler = SmartPIHandler(t)
    assert handler._restart_exclusion() == "safety mode"
    t.is_overpowering_detected = True
    assert handler._restart_exclusion() == "power shedding"
    t.window_manager.is_window_detected = True
    assert handler._restart_exclusion() == "window open"
    t.window_manager.is_window_detected = False
    t.is_overpowering_detected = False
    t.safety_manager.is_safety_detected = False
    assert handler._restart_exclusion() is None


def test_phase_is_stable_in_the_harness() -> None:
    run = _Run()
    run.ticks(2)
    assert run.algo.phase == SmartPIPhase.STABLE
