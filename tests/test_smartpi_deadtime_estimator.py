from unittest.mock import MagicMock

import pytest
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi import deadtime_estimator, timestamp_utils
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from helpers import force_smartpi_stable_mode

class TestDeadTimeEstimatorNew:
    def setup_method(self):
        self.est = DeadTimeEstimator()

    def test_nominal_heat_detection(self):
        now = 1000.0
        # Condition initial: OFF
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # Start Heat (Power 0 -> 1)
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        assert self.est.heat_start_time == now
        assert self.est.heat_start_temp == 20.0
        
        # Wait, temp constant
        now += 300
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # Temp rises 0.05
        now += 60
        self.est.update(now, 20.06, 21.0, 1.0)
        # 20.06 - 20.0 = 0.06 >= 0.05
        assert self.est.state == "HEATING"
        # start at 1060. Inflection at 1360. Diff = 300.
        assert self.est.deadtime_heat_s == 300.0
        assert self.est.deadtime_heat_reliable is True

    def test_nominal_cool_detection(self):
        now = 1000.0
        # Initial: Heating
        self.est.update(now, 20.0, 21.0, 0.0) # OFF first
        now += 1000
        self.est.update(now, 20.0, 21.0, 1.0) # ON
        # Wait enough for min power/state
        
        # Force state to HEATING (detected or not)
        self.est.state = "HEATING" 
        self.est.last_power = 1.0 # Simulate prev power
        
        # Stop Heat (Power 1 -> 0)
        now += 60
        stop_time = now
        # Prev power 1.0 > 0.8
        self.est.update(now, 22.0, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.cool_start_time == now
        assert self.est.cool_peak_temp == 22.0
        
        # Inertia: Temp rises a bit
        now += 60
        self.est.update(now, 22.1, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.cool_peak_temp == 22.1
        
        # Temp Drops 0.05 from peak (22.1 - 0.05 = 22.05)
        now += 300
        self.est.update(now, 22.04, 21.0, 0.0)
        assert self.est.state == "COOLING"
        # start at 2060. Peak/inflection at 2120. Diff = 60.
        assert self.est.deadtime_cool_s == 60.0
        assert self.est.deadtime_cool_reliable is True

    def test_active_cool_power_learns_falling_response_deadtime(self):
        """In HVAC COOL, power ON must populate the physical cool dead time."""
        now = 1000.0
        self.est.update(
            now,
            24.0,
            20.0,
            0.0,
            hvac_mode=VThermHvacMode_COOL,
        )
        now += 600.0
        self.est.update(
            now,
            24.0,
            20.0,
            1.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.cool_start_time == now

        now += 180.0
        self.est.update(
            now,
            24.0,
            20.0,
            1.0,
            hvac_mode=VThermHvacMode_COOL,
        )
        now += 60.0
        self.est.update(
            now,
            23.94,
            20.0,
            1.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert self.est.deadtime_cool_s == 180.0
        assert self.est.deadtime_cool_reliable is True
        assert self.est.deadtime_heat_s is None

    def test_cool_power_stop_learns_passive_rising_deadtime(self):
        """In HVAC COOL, power OFF must populate the physical heat dead time."""
        now = 1000.0
        self.est.model_hvac_mode = "cool"
        self.est.last_power = 1.0
        self.est.state = "COOLING"
        self.est.update(
            now,
            20.0,
            20.0,
            0.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert self.est.state == "WAITING_HEAT_RESPONSE"

        now += 120.0
        self.est.update(
            now,
            20.0,
            20.0,
            0.0,
            hvac_mode=VThermHvacMode_COOL,
        )
        now += 60.0
        self.est.update(
            now,
            20.06,
            20.0,
            0.0,
            hvac_mode=VThermHvacMode_COOL,
        )

        assert self.est.deadtime_heat_s == 120.0
        assert self.est.deadtime_heat_reliable is True

    def test_legacy_cool_deadtime_state_is_reset_on_first_use(self):
        """Legacy COOL transition labels are not silently reinterpreted."""
        self.est.deadtime_heat_s = 120.0
        self.est.deadtime_heat_reliable = True
        self.est._history_heat.append(120.0)

        reset = self.est.ensure_hvac_mode(VThermHvacMode_COOL)

        assert reset is True
        assert self.est.deadtime_heat_s is None
        assert self.est.deadtime_cool_s is None
        assert self.est.model_hvac_mode == "cool"

    def test_cooling_persistence(self):
        """Test that COOLING state persists even with subsequent updates at 0 power"""
        now = 1000.0
        self.est.state = "COOLING"
        self.est.last_power = 0.0
        
        # Update with 0 power
        now += 60
        self.est.update(now, 20.0, 21.0, 0.0)
        assert self.est.state == "COOLING"
        
        # Update again
        now += 60
        self.est.update(now, 19.9, 21.0, 0.0)
        assert self.est.state == "COOLING"

    def test_power_threshold_check(self):
        now = 1000.0
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # Weak Start (Power 0 -> 0.5)
        now += 60
        self.est.update(now, 20.0, 21.0, 0.5)
        assert self.est.state == "HEATING" # Active but ignored
        # Should not be WAITING_HEAT_RESPONSE
        assert self.est.heat_start_time is None

    def test_min_off_time_check(self):
        now = 1000.0
        # OFF
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # ON
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        # Should accept (first run, last_stop_time is None)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # OFF (shortly after)
        now += 60
        self.est.update(now, 20.0, 21.0, 0.0)
        assert self.est.state == "WAITING_COOL_RESPONSE"
        assert self.est.last_stop_time == now
        stop_time = now
        
        # ON again (too soon, 1 min later)
        now += 60
        self.est.update(now, 20.0, 21.0, 1.0)
        # Should Ignore Heat Start because (now - stop_time) < 600
        assert self.est.state == "HEATING" # Ignored
        assert self.est.heat_start_time < stop_time 

    def test_timeout_heat(self):
        now = 0.0
        self.est.update(now, 20.0, 21.0, 0.0)
        
        # ON
        now += 60
        start_time = now
        self.est.update(now, 20.0, 21.0, 1.0)
        assert self.est.state == "WAITING_HEAT_RESPONSE"
        
        # Forward > 4h (14400s) - timeout_seconds is 14400.0
        now += 14401
        self.est.update(now, 20.04, 21.0, 1.0) # Temp didn't rise enough (0.04 < 0.05)
        
        assert self.est.state == "HEATING"
        assert self.est.deadtime_heat_s is None


# ---------------------------------------------------------------------------
# Raw / filtered temperature history routing (learning slopes vs detection)
# ---------------------------------------------------------------------------

SENSOR_QUANTUM_C = 0.1
TICK_S = 60.0


def _quantise(value: float) -> float:
    """Return the value as reported by a sensor with a 0.1 degC resolution."""
    return round(round(value / SENSOR_QUANTUM_C) * SENSOR_QUANTUM_C, 1)


def _feed(est: DeadTimeEstimator, filt: AdaptiveTintFilter, now: float, raw: float) -> None:
    """Feed one tick through the filter and the estimator as SmartPI does."""
    t_int_lp, _ = filt.update(raw, now)
    est.update(now, t_int_lp, 21.0, 0.0, tin_raw=raw)


@pytest.mark.parametrize("rate_c_per_h", [0.1, 0.3])
@pytest.mark.parametrize("phase_c", [0.01, 0.03, 0.06, 0.09])
def test_quantised_slow_ramp_slope_uses_raw_history(rate_c_per_h: float, phase_c: float) -> None:
    """A quantised slow ramp yields slopes within 20 % of the truth once accepted."""
    est = DeadTimeEstimator()
    filt = AdaptiveTintFilter("ramp")
    t0 = 1000.0
    true_slope_per_min = rate_c_per_h / 60.0
    ratios: list[float] = []

    for k in range(6 * 60 + 1):
        now = t0 + k * TICK_S
        raw = _quantise(20.0 + phase_c + rate_c_per_h * (now - t0) / 3600.0)
        _feed(est, filt, now, raw)

        assert est.tin_history[-1] == (now, raw)
        slope, _, _ = ABEstimator.robust_dTdt_per_min(list(est.tin_history))
        if slope is not None:
            ratios.append(slope / true_slope_per_min)

    assert ratios, "slope never accepted over 6 h"
    assert min(ratios) >= 0.8
    assert max(ratios) <= 1.2


def test_single_quantum_step_is_rejected_on_raw_history() -> None:
    """One sensor quantum must not produce a slope (too few level changes)."""
    est = DeadTimeEstimator()
    filt = AdaptiveTintFilter("step")

    for k in range(12):
        raw = 20.0 if k < 2 else 20.1
        _feed(est, filt, 1000.0 + k * TICK_S, raw)

    slope, method, jumps = ABEstimator.robust_dTdt_per_min(list(est.tin_history))
    assert slope is None
    assert method == "too_few_jumps"
    assert jumps == 1

    # The filtered history turns the same single step into many distinct
    # levels, which is why it must not be used for learning slopes.
    lp_slope, _, _ = ABEstimator.robust_dTdt_per_min(list(est._tin_lp_history))
    assert lp_slope is not None


def test_raw_history_and_filtered_lookback_routing() -> None:
    """Raw values go to tin_history; the inflection lookback uses filtered values."""
    est = DeadTimeEstimator()
    now = 1000.0
    # The raw sequence never reaches the trough band, so a lookback on it
    # would place the inflection at detection time (360 s instead of 300 s).
    est.update(now, 20.0, 21.0, 0.0, tin_raw=30.0)
    now += 60
    start = now
    est.update(now, 20.0, 21.0, 1.0, tin_raw=30.5)
    now += 300
    est.update(now, 20.0, 21.0, 1.0, tin_raw=31.0)
    now += 60
    est.update(now, 20.06, 21.0, 1.0, tin_raw=31.5)

    assert est.state == "HEATING"
    assert est.deadtime_heat_s == 300.0
    assert [v for _, v in est.tin_history] == [30.0, 30.5, 31.0, 31.5]
    assert [v for _, v in est._tin_lp_history] == [20.0, 20.0, 20.0, 20.06]
    assert [t for t, _ in est.tin_history] == [1000.0, start, start + 300, start + 360]


def test_tin_raw_defaults_to_tin() -> None:
    """Without tin_raw both histories hold the same samples."""
    est = DeadTimeEstimator()
    est.update(1000.0, 20.0, 21.0, 0.0)
    est.update(1060.0, 20.1, 21.0, 0.0)

    assert list(est.tin_history) == [(1000.0, 20.0), (1060.0, 20.1)]
    assert list(est._tin_lp_history) == list(est.tin_history)


def test_reset_observation_clears_both_histories() -> None:
    """Both temperature histories are discarded on observation reset and full reset."""
    est = DeadTimeEstimator()
    est.update(1000.0, 20.0, 21.0, 0.0, tin_raw=20.1)
    est.reset_observation()
    assert not est.tin_history
    assert not est._tin_lp_history

    est.update(2000.0, 20.0, 21.0, 0.0, tin_raw=20.1)
    est.reset()
    assert not est.tin_history
    assert not est._tin_lp_history


class _Clock:
    """Controllable wall and monotonic clocks for persistence conversions."""

    def __init__(self, wall: float, mono: float) -> None:
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Patch the clocks used by persistence conversions and state restore."""
    fake = _Clock(wall=1_700_000_000.0, mono=50_000.0)
    monkeypatch.setattr(timestamp_utils, "time", fake)
    monkeypatch.setattr(deadtime_estimator, "time", fake)
    return fake


def _restart(clock: _Clock, downtime_s: float, mono_after_boot: float) -> None:
    """Simulate a host restart: wall time advances, monotonic origin resets."""
    clock.wall += downtime_s
    clock.mono = mono_after_boot


def test_histories_persistence_round_trip(clock: _Clock) -> None:
    """Both histories survive save/load with past samples; their samples stay distinct."""
    est = DeadTimeEstimator()
    base = clock.mono - 120.0
    est.update(base, 20.0, 21.0, 0.0, tin_raw=20.1)
    est.update(base + 60.0, 20.02, 21.0, 0.0, tin_raw=20.2)

    state = est.save_state()
    assert [t for t, _ in state["tin_history"]] == [
        pytest.approx(clock.wall - 120.0),
        pytest.approx(clock.wall - 60.0),
    ]
    assert [v for _, v in state["tin_history"]] == [20.1, 20.2]
    assert [v for _, v in state["tin_lp_history"]] == [20.0, 20.02]

    _restart(clock, downtime_s=300.0, mono_after_boot=10.0)
    restored = DeadTimeEstimator()
    restored.load_state(state)
    assert [v for _, v in restored.tin_history] == [20.1, 20.2]
    assert [v for _, v in restored._tin_lp_history] == [20.0, 20.02]
    # Samples keep their age relative to the restarted monotonic clock.
    expected_ts = [10.0 - 300.0 - 120.0, 10.0 - 300.0 - 60.0]
    assert [t for t, _ in restored.tin_history] == pytest.approx(expected_ts)
    assert [t for t, _ in restored._tin_lp_history] == pytest.approx(expected_ts)


def _learned_estimator_waiting(clock: _Clock, *, active: bool) -> DeadTimeEstimator:
    """Estimator with learned dead times and a detection in progress."""
    est = DeadTimeEstimator()
    est.deadtime_heat_s = 300.0
    est.deadtime_cool_s = 900.0
    est.deadtime_heat_reliable = True
    est.deadtime_cool_reliable = True
    est._history_heat.append(300.0)
    est._history_cool.append(900.0)
    est.update(clock.mono - 120.0, 20.0, 21.0, 0.0, tin_raw=20.05)
    # Old stop: well beyond the minimum OFF time.
    est.last_stop_time = clock.mono - 7200.0
    if active:
        est.state = "WAITING_HEAT_RESPONSE"
        est.heat_start_time = clock.mono - 600.0
        est.heat_start_temp = 20.0
        est.heat_trough_temp = 20.0
        est._waiting_power_on = True
    else:
        est.state = "WAITING_COOL_RESPONSE"
        est.cool_start_time = clock.mono - 600.0
        est.cool_peak_temp = 20.0
        est._waiting_power_on = False
    return est


@pytest.mark.parametrize("active", [True, False])
def test_in_progress_detection_is_not_restored(clock: _Clock, active: bool) -> None:
    """A detection spanning a restart is dropped; learned dead times and histories survive."""
    est = _learned_estimator_waiting(clock, active=active)
    state = est.save_state()
    # The persisted format still carries the detection keys as wall-clock values.
    assert state["last_stop_time"] == pytest.approx(clock.wall - 7200.0)
    start_key = "heat_start_time" if active else "cool_start_time"
    assert state[start_key] == pytest.approx(clock.wall - 600.0)

    _restart(clock, downtime_s=1800.0, mono_after_boot=5.0)
    restored = DeadTimeEstimator()
    restored.load_state(state)

    assert restored.state == "OFF"
    assert restored.heat_start_time is None
    assert restored.heat_start_temp is None
    assert restored.heat_trough_temp is None
    assert restored.cool_start_time is None
    assert restored.cool_peak_temp is None
    assert restored._waiting_power_on is None
    assert restored.last_stop_time == pytest.approx(5.0)
    assert restored.deadtime_heat_s == 300.0
    assert restored.deadtime_cool_s == 900.0
    assert restored.deadtime_heat_reliable
    assert restored.deadtime_cool_reliable
    assert list(restored._history_heat) == [300.0]
    assert list(restored._history_cool) == [900.0]
    assert [v for _, v in restored.tin_history] == [20.05]
    assert [v for _, v in restored._tin_lp_history] == [20.0]
    assert restored.tin_history[0][0] == pytest.approx(5.0 - 1800.0 - 120.0)

    # Immediately after the restart the room already moves in the waited direction.
    now = clock.mono + 10.0
    u = 1.0 if active else 0.0
    step = 0.2 if active else -0.2
    for i in range(1, 4):
        restored.update(now + 60.0 * i, 20.0 + step * i, 21.0, u)

    assert list(restored._history_heat) == [300.0]
    assert list(restored._history_cool) == [900.0]
    assert restored.deadtime_heat_s == 300.0
    assert restored.deadtime_cool_s == 900.0
    assert restored.state not in {"WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE"}


def test_detection_arms_after_in_session_minimum_off(clock: _Clock) -> None:
    """Once the minimum OFF time elapses within the session, detection arms again."""
    est = _learned_estimator_waiting(clock, active=True)
    state = est.save_state()
    _restart(clock, downtime_s=1800.0, mono_after_boot=5.0)
    restored = DeadTimeEstimator()
    restored.load_state(state)

    restored.update(10.0, 20.0, 21.0, 0.0)
    restored.update(5.0 + restored.min_off_time_seconds + 1.0, 20.0, 21.0, 1.0)
    assert restored.state == "WAITING_HEAT_RESPONSE"


def test_legacy_state_without_filtered_history_loads(clock: _Clock) -> None:
    """A persisted state without the filtered history key loads with it empty."""
    est = DeadTimeEstimator()
    est.update(clock.mono - 60.0, 20.0, 21.0, 0.0, tin_raw=20.1)
    state = est.save_state()
    del state["tin_lp_history"]

    _restart(clock, downtime_s=60.0, mono_after_boot=1.0)
    restored = DeadTimeEstimator()
    restored._tin_lp_history.append((1.0, 19.0))
    restored.load_state(state)

    assert [v for _, v in restored.tin_history] == [20.1]
    assert not restored._tin_lp_history


def test_legacy_null_sample_timestamps_are_dropped(clock: _Clock) -> None:
    """Samples persisted without a timestamp are skipped on load."""
    restored = DeadTimeEstimator()
    restored.load_state(
        {"tin_history": [(None, 19.0), (clock.wall - 60.0, 20.0)]}
    )
    assert [v for _, v in restored.tin_history] == [20.0]


@pytest.mark.parametrize("stable", [False, True])
def test_smartpi_routes_raw_temperature_to_tin_history(stable: bool) -> None:
    """SmartPI feeds the raw measurement to tin_history and t_int_lp to the lookback history."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="Raw history routing test",
    )
    if stable:
        force_smartpi_stable_mode(smartpi)
    expected_phase = SmartPIPhase.STABLE if stable else SmartPIPhase.HYSTERESIS
    assert smartpi.phase == expected_phase

    smartpi.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=10.0, hvac_mode=VThermHvacMode_HEAT)
    smartpi.calculate(target_temp=20.0, current_temp=19.5, ext_current_temp=10.0, hvac_mode=VThermHvacMode_HEAT)

    t_int_lp = smartpi.tint_filter.t_int_lp
    assert t_int_lp != pytest.approx(19.5)
    assert smartpi.dt_est.tin_history[-1][1] == 19.5
    assert smartpi.dt_est._tin_lp_history[-1][1] == pytest.approx(t_int_lp)
