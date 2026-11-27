"""States saved by the released versions load and regulate in this version.

The fixtures in ``tests/fixtures`` are the JSON of ``save_state()`` of SmartPI 0.6.7 and 0.7, taken from a real
simulated run of each version (room with a 4 min heater lag, setpoint 19 C by day and 16 C by night, outdoor 5 +- 3 C):

- ``saved_state_0_6_7_stable``: learned room (a 0.0295, b 0.000975, 25 / 33 accepted), dead time 780 s, integral 13.7;
- ``saved_state_0_6_7_bootstrap``: bootstrap (hysteresis) with 3 a and 8 b measurements, a not published, dead time 840 s;
- ``saved_state_0_6_7_bootstrap_late``: still bootstrap, 16 a and 15 b measurements, a not published (published at the load);
- ``saved_state_0_7_stable``: learned room (a 0.0294, b 0.000982, 21 / 33 accepted), dead time 780 s, integral 10.8,
  with the restart context of 0.7.

Each test restarts the thermostat 2 min after the last active tick of the saved state, as a short upgrade restart.
"""

from __future__ import annotations

import json
import logging
import math
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.gains import start_dead_time

FIXTURES = Path(__file__).parent / "fixtures"
STABLE = ["saved_state_0_6_7_stable", "saved_state_0_7_stable"]
BOOTSTRAP = ["saved_state_0_6_7_bootstrap", "saved_state_0_6_7_bootstrap_late"]
ALL = STABLE + BOOTSTRAP

RESTART_GAP_S = 120.0
TICK_S = 60.0
# Command of the generating run at the moment of the save (its log); the state does not carry the applied command.
LAST_COMMAND = {"saved_state_0_6_7_stable": 0.51, "saved_state_0_7_stable": 0.51}
# Room at the moment of the save: indoor 19.10 C for the setpoint 19.0 C (stable states).
SETPOINT, ROOM = 19.0, 19.10


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class _Clock:
    def __init__(self, wall: float) -> None:
        self.mono = 500.0
        self._wall0 = wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds

    def wall(self) -> float:
        return self._wall0 + RESTART_GAP_S + (self.mono - 500.0)


@contextmanager
def _clock(state: dict):
    clock = _Clock(state["last_active_wall_ts"])
    with patch("time.monotonic", lambda: clock.mono), patch("time.time", clock.wall):
        yield clock


def _build(state: dict | None) -> SmartPI:
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="upgrade",
        saved_state=state,
    )


def _tick(algo: SmartPI, clock: _Clock, temp: float, target: float = SETPOINT, boundary: bool = False) -> float:
    clock.advance(TICK_S)
    algo.calculate(
        target_temp=target,
        current_temp=temp,
        ext_current_temp=5.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
        cycle_boundary=boundary,
    )
    return algo.on_percent


@pytest.mark.parametrize("name", ALL)
def test_fixture_is_plain_json(name):
    """A fixture is what the Store keeps: JSON without non-finite numbers."""
    state = _fixture(name)
    assert json.loads(json.dumps(state, allow_nan=False)) == state


@pytest.mark.parametrize("name", ALL)
def test_loads_without_warning(name, caplog):
    """The state loads, and saves again as JSON, without a warning or an error in the log."""
    state = _fixture(name)
    with _clock(state), caplog.at_level(logging.DEBUG):
        algo = _build(state)
        resaved = algo.save_state()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    json.dumps(resaved, allow_nan=False)


@pytest.mark.parametrize("name", ALL)
def test_production_load_path_gives_the_same_model(name):
    """The handler loads into an existing algorithm (``load_state``): same result as the constructor."""
    state = _fixture(name)
    with _clock(state):
        built = _build(state)
        loaded = _build(None)
        loaded.load_state(state)
    for algo in (built, loaded):
        assert (algo.est.a, algo.est.b, algo.est.learn_ok_count_a, algo.est.learn_ok_count_b) == (
            built.est.a, built.est.b, built.est.learn_ok_count_a, built.est.learn_ok_count_b)
    assert loaded.phase == built.phase
    assert loaded.integral == built.integral


@pytest.mark.parametrize("name", ALL)
def test_learned_model_and_counters_are_kept(name):
    """a, b, their counters and their measurement history are those of the saved state."""
    state = _fixture(name)
    saved = state["est_state"]
    with _clock(state):
        est = _build(state).est
    if name == "saved_state_0_6_7_bootstrap_late":   # a is published at the load (see the late bootstrap test)
        assert est.learn_ok_count_a == saved["learn_ok_count_a"] + 1
        assert est.learn_ok_count == saved["learn_ok_count"] + 1
    else:
        assert est.a == saved["a"]
        assert est.learn_ok_count == saved["learn_ok_count"]
        assert est.learn_ok_count_a == saved["learn_ok_count_a"]
    assert est.b == saved["b"]
    assert est.learn_ok_count_b == saved["learn_ok_count_b"]
    assert list(est.a_meas_hist) == saved["a_meas_hist"]
    assert list(est.b_meas_hist) == saved["b_meas_hist"]


@pytest.mark.parametrize("name", ALL)
def test_integral_is_kept(name):
    """The integral of the controller is restored (zero in a bootstrap state, learned otherwise)."""
    state = _fixture(name)
    with _clock(state):
        algo = _build(state)
    assert algo.integral == state["ctl_state"]["integral"]
    assert (algo.integral != 0.0) == (name in STABLE)


@pytest.mark.parametrize("name", ALL)
def test_heat_dead_time_after_a_start_is_kept(name):
    """The reliable HEAT dead time of the saved state stands after the load."""
    state = _fixture(name)
    saved = state["dt_est_state"]
    assert saved["deadtime_heat_reliable"] is True
    with _clock(state):
        algo = _build(state)
    assert algo.dt_est.active_deadtime(VThermHvacMode_HEAT) == (saved["deadtime_heat_s"], True)
    delay = start_dead_time(algo.dt_est, VThermHvacMode_HEAT)
    assert delay is not None and delay.seconds == saved["deadtime_heat_s"]
    # The COOL slot is not the active one of the state: it is dropped at the load.
    assert algo.dt_est.deadtime_cool_s is None


@pytest.mark.parametrize("name", ALL)
def test_response_model_is_seeded_as_previous_version(name):
    """No saved response model: it is seeded from the dead time, published as 'previous version', provisional.

    The seed keeps the effective dead time (theta + tau_e / 2) equal to the saved HEAT dead time (the active slot of
    these states; the COOL slot is not active, so COOL has no seed) and has no event yet.
    """
    state = _fixture(name)
    assert "response_state" not in state
    saved = state["dt_est_state"]
    with _clock(state):
        algo = _build(state)
    model = algo._start.model
    values = model.values("heat")
    assert values is not None
    assert values["source"] == "previous"
    assert values["confidence"] == "provisional"
    assert model.effective_dead_time_s("heat") == pytest.approx(saved["deadtime_heat_s"])
    assert model.event_count("heat") == 0
    assert model.values("cool") is None
    public = model.public_model("heat")
    assert public["response_source"] == "previous version"
    assert public["response_confidence"] == "provisional"
    assert algo.hold_dead_time_s(VThermHvacMode_HEAT) == pytest.approx(saved["deadtime_heat_s"])


@pytest.mark.parametrize("name", ["saved_state_0_6_7_stable", "saved_state_0_6_7_bootstrap", "saved_state_0_6_7_bootstrap_late"])
def test_restart_without_context_is_not_bridged(name):
    """0.6.7 saves no restart context: the first tick is a plain start, not a bridged restart."""
    state = _fixture(name)
    assert "restart" not in state
    with _clock(state) as clock:
        algo = _build(state)
        _tick(algo, clock, ROOM)
    assert algo.restart.bridged is False
    assert algo.restart.decision is not None
    assert algo.restart.decision.reason == "no saved state of the last tick"


def test_restart_context_of_0_7_is_bridged_after_a_short_stop():
    """0.7 saves its restart context: a 2 min stop is bridged (downtime from the last saved tick)."""
    state = _fixture("saved_state_0_7_stable")
    assert state["restart"]["hvac_mode"] == "heat"
    with _clock(state) as clock:
        algo = _build(state)
        _tick(algo, clock, ROOM)
    assert algo.restart.bridged is True
    assert algo.restart.decision.downtime_s == pytest.approx(RESTART_GAP_S + TICK_S, abs=2)


@pytest.mark.parametrize("name", STABLE)
def test_first_ticks_of_a_learned_room_stay_close_to_the_last_command(name):
    """Learned states regulate at once: finite command in [0, 1], no jump from the command before the restart.

    Observed with the room at 19.10 C for 19.0 C: the first command is 0.462 (0.6.7 and 0.7) for 0.51 before the
    save, a step of -0.05; the next ticks move by 0.0002 each. The bound is 0.10, twice the observed step (10 % of
    full power): a larger step would be visible in the room within one cycle.
    """
    state = _fixture(name)
    with _clock(state) as clock:
        algo = _build(state)
        commands = [_tick(algo, clock, ROOM, boundary=(k == 0)) for k in range(6)]
    assert all(math.isfinite(u) and 0.0 <= u <= 1.0 for u in commands)
    assert abs(commands[0] - LAST_COMMAND[name]) <= 0.10
    assert all(abs(b - a) <= 0.02 for a, b in zip(commands, commands[1:]))
    assert algo.phase == SmartPIPhase.STABLE


def test_bootstrap_state_resumes_in_the_bootstrap_with_its_measurements():
    """A bootstrap with too few a measurements stays in the bootstrap (hysteresis on/off) and keeps what it collected."""
    name = "saved_state_0_6_7_bootstrap"
    state = _fixture(name)
    with _clock(state) as clock:
        algo = _build(state)
        assert algo.phase == SmartPIPhase.HYSTERESIS
        commands = [_tick(algo, clock, 16.7, target=SETPOINT, boundary=(k == 0)) for k in range(6)]
    assert all(u in (0.0, 1.0) for u in commands)
    assert commands[0] == 1.0  # well below the band
    assert algo.phase == SmartPIPhase.HYSTERESIS
    saved = state["est_state"]
    assert len(algo.est.a_meas_hist) == len(saved["a_meas_hist"]) == 3
    assert len(algo.est.b_meas_hist) == len(saved["b_meas_hist"]) == 8
    assert algo.est.learn_ok_count_b == saved["learn_ok_count_b"]
    assert algo.est.b == saved["b"]
    assert algo.est.learn_ok_count_a == 0


def test_saved_bootstrap_output_of_0_6_7_is_not_resumed_without_a_bridged_restart():
    """The saved on/off output only applies after a bridged restart; 0.6.7 saves no restart context.

    Observed: the state was saved with the heater on, the room is inside the band (19.0 C for 19.0 C) and the first
    command is off (0.0), where an uninterrupted hysteresis would stay on until the upper edge. One cycle of the
    hysteresis once, at the upgrade.
    """
    state = _fixture("saved_state_0_6_7_bootstrap")
    assert state["hysteresis_output"]["on"] is True
    with _clock(state) as clock:
        algo = _build(state)
        first = _tick(algo, clock, SETPOINT)
    assert algo.restart.bridged is False
    assert first == 0.0


def test_late_bootstrap_state_publishes_a_from_its_collected_measurements():
    """With 5 or more a measurements and a reliable dead time the bootstrap ends at the load with a published.

    The seeded response model (previous version) settles the delivered power model, so a is published from the
    collected measurements at the load, as at a learning update, before the first regulated tick (0.0298 for 0.03 in
    the simulated room). Observed: 0.19 at the setpoint (the holding power), 0.38 easing to 0.32 at 0.2 C under it.
    Without the publication at the load the first tick ran with the initial a (0.0005) and reached full power.
    """
    name = "saved_state_0_6_7_bootstrap_late"
    state = _fixture(name)
    saved = state["est_state"]
    assert len(saved["a_meas_hist"]) >= 5 and saved["learn_ok_count_a"] == 0
    for temp, bound in ((SETPOINT, 0.25), (SETPOINT - 0.2, 0.45)):
        with _clock(state) as clock:
            algo = _build(state)
            assert algo.phase == SmartPIPhase.STABLE
            assert algo.est.learn_ok_count_a == 1
            assert min(saved["a_meas_hist"]) <= algo.est.a <= max(saved["a_meas_hist"])
            commands = [_tick(algo, clock, temp, boundary=(k == 0)) for k in range(6)]
        assert all(math.isfinite(u) and 0.0 <= u <= bound for u in commands)
        assert list(algo.est.a_meas_hist) == saved["a_meas_hist"]
        assert list(algo.est.b_meas_hist) == saved["b_meas_hist"]
        assert algo.est.learn_ok_count_b == saved["learn_ok_count_b"]


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="est_state.a = None is not guarded: float(None) raises out of SmartPI(saved_state=...). In production "
    "the handler catches it and logs an error, but the thermostat then starts with an unloaded model.",
)
def test_malformed_estimator_value_does_not_raise_out_of_the_constructor():
    """A saved a = None must not make the whole load raise."""
    state = _fixture("saved_state_0_6_7_stable")
    state["est_state"]["a"] = None
    with _clock(state):
        algo = _build(state)
    assert algo.est.b == state["est_state"]["b"]
