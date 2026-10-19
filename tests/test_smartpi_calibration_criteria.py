"""AutoCalib exit criteria, retry delay and unreachable calibration thresholds."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.autocalib import AutoCalibTrigger
from custom_components.vtherm_smartpi.smartpi.calibration import CalibrationManager
from custom_components.vtherm_smartpi.smartpi.const import (
    AUTOCALIB_COOLDOWN_H,
    AUTOCALIB_RETRY_DELAY_H,
    AUTOCALIB_SNAPSHOT_PERIOD_H,
    CALIBRATION_REACHABILITY_MARGIN_C,
    HYST_LOWER_C,
    HYST_UPPER_C,
    AutoCalibState,
    SmartPICalibrationPhase,
    SmartPICalibrationResult,
    SmartPIPhase,
)
from test_smartpi_handler_safety import _control, _make_handler

NOW = 1_800_000_000.0
HOUR = 3600.0


def _algo(count_a=10, count_b=10, dt_heat=True, dt_cool=True, mode=VThermHvacMode_HEAT):
    """Mock algorithm with estimates that show stagnation on `a` and `b`."""
    algo = MagicMock()
    algo.phase = SmartPIPhase.STABLE
    algo.calibration_mgr.is_calibrating = False
    algo.calibration_mgr.last_calibration_time = None
    algo.gov.regime = MagicMock()
    algo.est.learn_ok_count_a = count_a
    algo.est.learn_ok_count_b = count_b
    algo.est.diag_a_mad_over_med = 0.4
    algo.est.diag_b_mad_over_med = 0.4
    algo.est.tau_reliability.return_value = MagicMock(reliable=False)
    algo.dt_est.deadtime_heat_reliable = dt_heat
    algo.dt_est.deadtime_cool_reliable = dt_cool
    algo._last_hvac_mode = mode
    algo._last_target_temp = 21.0
    return algo


def _triggered(params, snap_a=10, snap_b=10):
    ac = AutoCalibTrigger("t")
    ac._state = AutoCalibState.TRIGGERED
    ac._post_calib_snap_a = snap_a
    ac._post_calib_snap_b = snap_b
    ac._triggered_params = list(params)
    ac._snap_dt_cool_ok = True
    return ac


# --- Exit criteria follow the triggering parameters ---------------------------


def test_exit_requires_only_the_triggering_parameter():
    """A calibration triggered on `a` succeeds when `a` progresses, whatever `b` does."""
    ac = _triggered(["a"])
    event = ac.on_calibration_complete(NOW, _algo(count_a=12, count_b=10))

    assert event.event_type == "smartpi_autocalib_success"
    assert event.payload["improved_params"] == ["a"]
    assert ac.retry_count == 0


def test_exit_retries_when_the_triggering_parameter_does_not_progress():
    """Progress on `b` does not satisfy a calibration triggered on `a`."""
    ac = _triggered(["a"])
    event = ac.on_calibration_complete(NOW, _algo(count_a=10, count_b=12))

    assert event.event_type == "smartpi_autocalib_retry"
    assert event.payload["missing_criteria"] == ["a(new=0<1)"]


def test_exit_requires_every_triggering_parameter():
    """With `a` and `b` triggering, both must progress."""
    ac = _triggered(["a", "b"])
    event = ac.on_calibration_complete(NOW, _algo(count_a=12, count_b=10))

    assert event.event_type == "smartpi_autocalib_retry"
    assert event.payload["missing_criteria"] == ["b(new=0<1)"]


def test_manual_request_is_judged_on_the_dead_times():
    """A manual calibration needs reliable dead times, not new `a`/`b` observations."""
    ac = _triggered(["manual_request"])
    event = ac.on_calibration_complete(NOW, _algo(count_a=10, count_b=10))
    assert event.event_type == "smartpi_autocalib_success"

    ac = _triggered(["manual_request"])
    event = ac.on_calibration_complete(NOW, _algo(dt_heat=False))
    assert event.event_type == "smartpi_autocalib_retry"
    assert event.payload["missing_criteria"] == ["deadtime_heat"]


# --- `b` is neither triggered nor required without a passive gradient --------


def test_mild_season_does_not_trigger_b():
    """`b` cannot be learned in HEAT when the outdoor temperature is close to the setpoint."""
    ac = AutoCalibTrigger("t")
    ac._snap_ok_count_a = ac._snap_ok_count_b = 10
    ac._snap_dt_heat_ok = ac._snap_dt_cool_ok = True

    cold = ac._evaluate_stagnation(_algo(), ext_temp=5.0, current_temp=20.0)
    mild_gradient = ac._evaluate_stagnation(_algo(), ext_temp=19.0, current_temp=20.0)
    mild_target = ac._evaluate_stagnation(_algo(), ext_temp=19.5, current_temp=22.0)
    unknown = ac._evaluate_stagnation(_algo(), ext_temp=None, current_temp=20.0)

    assert cold == ["a", "b"]
    assert mild_gradient == ["a"]
    assert mild_target == ["a"]
    assert unknown == ["a"]


def test_mild_season_cool_mode_mirrors_the_heat_limits():
    """In COOL, `b` needs a warm outdoor side: ext >= setpoint + margin and a heat-gain gradient."""
    ac = AutoCalibTrigger("t")
    ac._snap_ok_count_a = ac._snap_ok_count_b = 10
    ac._snap_dt_heat_ok = ac._snap_dt_cool_ok = True
    algo = _algo(mode=VThermHvacMode_COOL)

    assert ac._evaluate_stagnation(algo, ext_temp=30.0, current_temp=24.0) == ["a", "b"]
    assert ac._evaluate_stagnation(algo, ext_temp=22.0, current_temp=21.0) == ["a"]


def test_mild_season_calibration_on_a_succeeds_without_b():
    """The mild-season scenario: `a` progresses, `b` cannot; no retry cycle is spent."""
    ac = AutoCalibTrigger("t")
    ac._snap_ok_count_a = ac._snap_ok_count_b = 10
    ac._snap_dt_heat_ok = ac._snap_dt_cool_ok = True
    algo = _algo()
    triggered = ac._evaluate_stagnation(algo, ext_temp=19.5, current_temp=20.0)
    ac._do_trigger(NOW, algo, triggered)

    event = ac.on_calibration_complete(NOW + HOUR, _algo(count_a=12, count_b=10))

    assert event.event_type == "smartpi_autocalib_success"


# --- Retry delay -------------------------------------------------------------


def _failed_attempt_at(now):
    """Autocalib after a calibration on `a` that ended without progress at `now`."""
    ac = _triggered(["a"])
    ac._snap_ok_count_a = ac._snap_ok_count_b = 10
    ac._snap_dt_heat_ok = ac._snap_dt_cool_ok = True
    ac._snapshot_ts = now - 130 * HOUR
    ac._last_trigger_ts = now - HOUR
    algo = _algo()
    # CalibrationManager sets this when the cycle completes
    algo.calibration_mgr.last_calibration_time = now
    event = ac.on_calibration_complete(now, algo)
    assert event.event_type == "smartpi_autocalib_retry"
    return ac, algo


def test_retry_fires_after_the_retry_delay_not_the_cooldown():
    """The retry runs AUTOCALIB_RETRY_DELAY_H after the failed attempt, not 24 h after it."""
    ac, algo = _failed_attempt_at(NOW)
    assert ac._next_check_ts == NOW + AUTOCALIB_RETRY_DELAY_H * HOUR

    assert ac.check_hourly(NOW + (AUTOCALIB_RETRY_DELAY_H - 1) * HOUR, algo, 5.0, 20.0) is None
    assert AUTOCALIB_RETRY_DELAY_H < AUTOCALIB_COOLDOWN_H

    event = ac.check_hourly(NOW + (AUTOCALIB_RETRY_DELAY_H + 1) * HOUR, algo, 5.0, 20.0)
    assert event is not None
    assert event.event_type == "smartpi_autocalib_triggered"
    assert event.payload["retry_count"] == 1
    assert ac.next_check_ts_iso is None


def test_retry_does_not_rewrite_the_trigger_time_or_the_snapshot():
    """The published trigger time and snapshot age stay the real ones during a retry."""
    snapshot = NOW - 130 * HOUR
    ac = _triggered(["a"])
    ac._snapshot_ts = snapshot
    ac._last_trigger_ts = NOW - HOUR
    ac.on_calibration_complete(NOW, _algo())

    assert ac._last_trigger_ts == NOW - HOUR
    assert ac._snapshot_ts == snapshot


def test_retry_waits_for_the_snapshot_period_only_when_none_is_scheduled():
    """A scheduled retry bypasses the 5-day snapshot age, a plain check does not."""
    ac = AutoCalibTrigger("t")
    ac._state = AutoCalibState.MONITORING
    ac._snapshot_ts = NOW - (AUTOCALIB_SNAPSHOT_PERIOD_H - 10) * HOUR
    ac._snap_ok_count_a = ac._snap_ok_count_b = 10
    ac._snap_dt_heat_ok = ac._snap_dt_cool_ok = True
    algo = _algo()
    assert ac.check_hourly(NOW, algo, 5.0, 20.0) is None

    ac._next_check_ts = NOW - 1.0
    ac._last_hourly_check_ts = None
    event = ac.check_hourly(NOW, algo, 5.0, 20.0)
    assert event.event_type == "smartpi_autocalib_triggered"


def test_scheduled_retry_ends_when_the_stagnation_is_gone():
    """If the estimates recovered during the delay, the retry is dropped without a calibration."""
    ac, _ = _failed_attempt_at(NOW)
    ac._snapshot_ts = NOW - 10 * HOUR
    recovered = _algo(count_a=20, count_b=20)
    recovered.est.diag_a_mad_over_med = 0.1
    recovered.est.diag_b_mad_over_med = 0.1
    recovered.est.tau_reliability.return_value = MagicMock(reliable=True)

    event = ac.check_hourly(NOW + 7 * HOUR, recovered, 5.0, 20.0)

    assert event is None
    assert ac.retry_count == 0
    assert ac.next_check_ts_iso is None


def test_snapshot_clears_the_scheduled_check():
    """A snapshot closes any pending re-trigger."""
    ac, algo = _failed_attempt_at(NOW)
    ac._take_snapshot(NOW, algo, reason="rolling")
    assert ac._next_check_ts is None


# --- Unreachable calibration ---------------------------------------------------


def test_unreachable_result_consumes_no_retry_and_waits_for_the_cooldown():
    """An unreachable cycle is not a model failure: no retry, next attempt after the cooldown."""
    ac = _triggered(["a"])
    ac._retry_count = 1

    event = ac.on_calibration_complete(NOW, _algo(), SmartPICalibrationResult.UNREACHABLE)

    assert event.event_type == "smartpi_autocalib_unreachable"
    assert event.payload == {"name": "t", "triggered_params": ["a"], "retry_count": 1}
    assert ac.state == AutoCalibState.MONITORING
    assert ac.retry_count == 1
    assert ac._next_check_ts == NOW + AUTOCALIB_COOLDOWN_H * HOUR
    guard_ok, reason = ac._check_guards(NOW + HOUR, _algo())
    assert guard_ok is False
    assert reason.startswith("retry_delay")
    assert ac._check_guards(NOW + (AUTOCALIB_COOLDOWN_H + 1) * HOUR, _algo())[0] is True


def _twin(**overrides):
    diag = {
        "status": "ok",
        "model_reliable": True,
        "warming_up": False,
        "cooling_model_available": None,
        "T_steady_max": 25.0,
        "T_steady_max_valid": True,
        "T_steady_passive": 15.0,
        "T_steady_passive_valid": True,
    }
    diag.update(overrides)
    return diag


def _manager(phase):
    mgr = CalibrationManager("t")
    mgr.start_calibration(1.0)
    mgr._calibration_state = phase
    return mgr


def _calc(mgr, twin, mode=VThermHvacMode_HEAT, current=20.2, target=20.0, max_on=1.0):
    """Default current temperature lies short of every phase threshold (19.7, 20.5, 20.0)."""
    return mgr.calculate(
        target_temp=target,
        current_temp=current,
        hvac_mode=mode,
        dt_est=MagicMock(),
        max_on_percent=max_on,
        twin_diag=twin,
    )


@pytest.mark.parametrize(
    "phase, mode, twin",
    [
        # HEAT: passive cool-down to target - 0.3 and back to target, full-power heat-up
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=19.6)),
        (SmartPICalibrationPhase.COOL_DOWN_FINAL, VThermHvacMode_HEAT, _twin(T_steady_passive=20.5)),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_HEAT, _twin(T_steady_max=20.6)),
        # COOL: active cool-down, passive warm-up
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_max=19.6)),
        (SmartPICalibrationPhase.COOL_DOWN_FINAL, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_max=20.5)),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_passive=20.6)),
    ],
)
def test_unreachable_threshold_ends_the_cycle(phase, mode, twin):
    """The cycle ends with UNREACHABLE when the reliable twin equilibrium misses the threshold."""
    mgr = _manager(phase)

    result = _calc(mgr, twin, mode)

    assert result.result == SmartPICalibrationResult.UNREACHABLE
    assert result.on_percent == 0.0
    assert result.message == f"unreachable_{phase.value}"
    assert mgr.state == SmartPICalibrationPhase.IDLE
    assert mgr.calibration_result == SmartPICalibrationResult.UNREACHABLE
    assert mgr.take_completed_result() == SmartPICalibrationResult.UNREACHABLE
    assert mgr.take_completed_result() is None
    assert mgr.last_calibration_time is None


@pytest.mark.parametrize(
    "phase, mode, twin",
    [
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin()),
        (SmartPICalibrationPhase.COOL_DOWN_FINAL, VThermHvacMode_HEAT, _twin()),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_HEAT, _twin()),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_passive=25.0)),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_max=15.0)),
        # Twin not usable: the cycle runs as before
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=30.0, model_reliable=False)),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=30.0, warming_up=True)),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=30.0, status="zero_dt")),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=30.0, T_steady_passive_valid=False)),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_COOL, _twin(T_steady_max=30.0)),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, None),
    ],
)
def test_reachable_or_unknown_threshold_keeps_the_cycle(phase, mode, twin):
    """A reachable threshold, or a twin that is not reliable, does not end the cycle."""
    mgr = _manager(phase)

    result = _calc(mgr, twin, mode)

    assert result.result == SmartPICalibrationResult.PENDING
    assert mgr.is_calibrating
    assert mgr.take_completed_result() is None


def test_reachability_margin_is_applied_beyond_the_threshold():
    """The equilibrium must clear the threshold by CALIBRATION_REACHABILITY_MARGIN_C."""
    low = 20.0 - HYST_LOWER_C
    just_short = _twin(T_steady_passive=low - CALIBRATION_REACHABILITY_MARGIN_C + 0.05)
    enough = _twin(T_steady_passive=low - CALIBRATION_REACHABILITY_MARGIN_C - 0.05)
    assert _calc(_manager(SmartPICalibrationPhase.COOL_DOWN), just_short).result == SmartPICalibrationResult.UNREACHABLE
    assert _calc(_manager(SmartPICalibrationPhase.COOL_DOWN), enough).result == SmartPICalibrationResult.PENDING

    high = 20.0 + HYST_UPPER_C
    just_short = _twin(T_steady_max=high + CALIBRATION_REACHABILITY_MARGIN_C - 0.05)
    enough = _twin(T_steady_max=high + CALIBRATION_REACHABILITY_MARGIN_C + 0.05)
    assert _calc(_manager(SmartPICalibrationPhase.HEAT_UP), just_short).result == SmartPICalibrationResult.UNREACHABLE
    assert _calc(_manager(SmartPICalibrationPhase.HEAT_UP), enough).result == SmartPICalibrationResult.PENDING


@pytest.mark.parametrize(
    "phase, mode, twin, current, next_phase, expected",
    [
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_HEAT, _twin(T_steady_passive=19.6),
         19.7, SmartPICalibrationPhase.HEAT_UP, SmartPICalibrationResult.PENDING),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_HEAT, _twin(T_steady_max=20.6),
         20.5, SmartPICalibrationPhase.COOL_DOWN_FINAL, SmartPICalibrationResult.PENDING),
        (SmartPICalibrationPhase.COOL_DOWN_FINAL, VThermHvacMode_HEAT, _twin(T_steady_passive=20.5),
         20.0, SmartPICalibrationPhase.IDLE, SmartPICalibrationResult.SUCCESS),
        (SmartPICalibrationPhase.COOL_DOWN, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_max=19.6),
         19.6, SmartPICalibrationPhase.HEAT_UP, SmartPICalibrationResult.PENDING),
        (SmartPICalibrationPhase.HEAT_UP, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_passive=20.6),
         20.6, SmartPICalibrationPhase.COOL_DOWN_FINAL, SmartPICalibrationResult.PENDING),
        (SmartPICalibrationPhase.COOL_DOWN_FINAL, VThermHvacMode_COOL,
         _twin(cooling_model_available=True, T_steady_max=20.5),
         19.9, SmartPICalibrationPhase.IDLE, SmartPICalibrationResult.SUCCESS),
    ],
)
def test_measured_threshold_takes_priority_over_reachability(phase, mode, twin, current, next_phase, expected):
    """A threshold the measured temperature already reached moves the cycle on, whatever the twin predicts."""
    mgr = _manager(phase)

    result = _calc(mgr, twin, mode, current=current)

    assert result.result == expected
    assert mgr.state == next_phase
    assert not result.message.startswith("unreachable")


def test_active_reachability_uses_the_allowed_power():
    """An active phase is judged on the equilibrium at max_on_percent, linear between passive and full power."""
    # Passive 10 C, full power 30 C: u_max 0.2 settles at 14 C, short of 20.5 + margin.
    twin = _twin(T_steady_max=30.0, T_steady_passive=10.0)
    assert _calc(_manager(SmartPICalibrationPhase.HEAT_UP), twin).result == SmartPICalibrationResult.PENDING
    limited = _calc(_manager(SmartPICalibrationPhase.HEAT_UP), twin, max_on=0.2)
    assert limited.result == SmartPICalibrationResult.UNREACHABLE
    assert limited.message == f"unreachable_{SmartPICalibrationPhase.HEAT_UP.value}"
    # u_max 0.6 settles at 22 C, which clears 20.5 + margin.
    assert _calc(_manager(SmartPICalibrationPhase.HEAT_UP), twin, max_on=0.6).result == SmartPICalibrationResult.PENDING
    # The passive phases ignore the power limit.
    passive = _twin(T_steady_passive=18.0)
    assert _calc(_manager(SmartPICalibrationPhase.COOL_DOWN), passive, max_on=0.2).result == SmartPICalibrationResult.PENDING


def test_completed_result_reports_every_way_a_run_ends():
    """Success, HVAC OFF, timeout and reset each leave one result for the handler."""
    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_OFF

    mgr = _manager(SmartPICalibrationPhase.COOL_DOWN_FINAL)
    _calc(mgr, None, current=19.0)
    assert mgr.take_completed_result() == SmartPICalibrationResult.SUCCESS

    mgr = _manager(SmartPICalibrationPhase.COOL_DOWN)
    _calc(mgr, None, VThermHvacMode_OFF)
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED

    mgr = _manager(SmartPICalibrationPhase.COOL_DOWN)
    mgr.handle_timeout()
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED

    mgr = _manager(SmartPICalibrationPhase.HEAT_UP)
    mgr.reset()
    assert mgr.take_completed_result() == SmartPICalibrationResult.CANCELLED

    mgr = CalibrationManager("t")
    mgr.reset()
    assert mgr.take_completed_result() is None


def test_algo_ends_a_calibration_requested_on_an_unreachable_threshold():
    """A requested calibration whose first threshold is unreachable ends in the same tick."""
    from test_smartpi_calibration import create_smartpi

    algo = create_smartpi()
    algo._last_twin_diag = _twin(T_steady_passive=22.0)
    algo.force_calibration()

    algo.calculate(
        target_temp=20.0,
        current_temp=20.0,
        ext_current_temp=22.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert algo.calibration_state == SmartPICalibrationPhase.IDLE
    assert algo.calibration_mgr.calibration_result == SmartPICalibrationResult.UNREACHABLE
    assert algo.on_percent == 0.0
    assert algo.autocalib.state == AutoCalibState.TRIGGERED


@pytest.mark.asyncio
async def test_handler_reports_a_run_that_ended_within_one_calculation(fake_handler_runtime):
    """The handler hands the result to AutoCalib even if the run never spanned two passes."""
    handler, algo = _make_handler(fake_handler_runtime)
    fake_handler_runtime.hass = MagicMock()
    fake_handler_runtime.entity_id = "climate.salon"
    algo.autocalib._state = AutoCalibState.TRIGGERED
    algo.autocalib._triggered_params = ["a"]
    algo.calibration_mgr._completed_result = SmartPICalibrationResult.UNREACHABLE

    await _control(handler, algo)

    fired = fake_handler_runtime.hass.bus.async_fire.call_args_list
    assert [c.args[0] for c in fired] == ["smartpi_autocalib_unreachable"]
    assert fired[0].args[1]["entity_id"] == "climate.salon"
    assert fired[0].args[1]["name"] == algo.autocalib._name
    assert algo.autocalib.state == AutoCalibState.MONITORING
    assert algo.calibration_mgr.take_completed_result() is None
