"""Delays of the regulation: the gain rule reads the reliable active dead time after a start; the integral holds
read the response model's effective dead time theta + tau_e / 2 (3 S / 4 when the split is assumed), else the same
dead time. The room model reads (theta, tau_e) of the HEAT model."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceDecision
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.gains import (
    SOURCE_DEAD_TIME,
    SOURCE_RESPONSE_MODEL,
    GainScheduler,
    effective_dead_time,
    imc_simc_gains,
    start_dead_time,
)
from custom_components.vtherm_smartpi.smartpi.measure_event import MODE_COOL, MODE_HEAT, SOURCE_PULSE
from custom_components.vtherm_smartpi.smartpi.response_model import ResponseModel
from custom_components.vtherm_smartpi.smartpi.resume_hold import ResumeDeadtimeHold, ResumeHoldSource
from custom_components.vtherm_smartpi.smartpi.room_model.manager import RoomModelManager


def _dt_est(seconds: float | None = 480.0, reliable: bool = True):
    return SimpleNamespace(active_deadtime=lambda mode: (seconds, reliable))


def _fit(theta: float, tau: float, split_ok: bool = True) -> dict:
    return {"theta_s": theta, "tau_e_s": tau, "s_s": theta + tau, "split_ok": split_ok, "rms_c": 0.01}


def _model(theta: float, tau: float, split_ok: bool = True) -> dict:
    model = ResponseModel()
    model.add(MODE_HEAT, SOURCE_PULSE, _fit(theta, tau, split_ok), 100.0)
    return model.values(MODE_HEAT)


# -- delays -----------------------------------------------------------------------------------------------------
def test_the_gains_read_the_dead_time_after_a_start_and_the_holds_the_model():
    model = _model(300.0, 1200.0)
    assert start_dead_time(_dt_est(480.0), VThermHvacMode_HEAT) == (480.0, SOURCE_DEAD_TIME)
    assert effective_dead_time(model, _dt_est(480.0), VThermHvacMode_HEAT) == (900.0, SOURCE_RESPONSE_MODEL)


def test_an_assumed_split_gives_three_quarters_of_the_emitter_lag_to_the_holds():
    model = _model(300.0, 1200.0, split_ok=False)          # S 1500, theta assumed S / 2
    assert model["confidence"] == "dead time assumed"
    assert effective_dead_time(model, _dt_est(), VThermHvacMode_HEAT).seconds == 0.75 * 1500.0


def test_without_a_reliable_dead_time_there_is_no_gain_delay():
    assert start_dead_time(_dt_est(480.0, reliable=False), VThermHvacMode_HEAT) is None
    assert start_dead_time(_dt_est(None), VThermHvacMode_HEAT) is None
    assert start_dead_time(_dt_est(0.0), VThermHvacMode_HEAT) is None
    # without a model the holds keep the same dead time
    assert effective_dead_time(None, _dt_est(480.0), VThermHvacMode_HEAT) == (480.0, SOURCE_DEAD_TIME)
    assert effective_dead_time(None, _dt_est(480.0, reliable=False), VThermHvacMode_HEAT) is None


def _calculate(delay, *, a=0.02, tau_min=1000.0, valve=False, cycle_min=10.0, pre_model_a=None, tau_reliable=True,
               scheduler=None):
    estimator = MagicMock()
    estimator.a = a
    scheduler = scheduler or GainScheduler("test")
    result = scheduler.calculate(
        tau_reliable=tau_reliable, tau_min=tau_min, estimator=estimator, delay=delay, in_near_band=False,
        governance_decision=GovernanceDecision.ADAPT_ON, cycle_min=cycle_min, valve_mode_enabled=valve,
        hvac_mode=VThermHvacMode_HEAT, pre_model_a=pre_model_a,
    )
    return scheduler, result


def test_the_rule_reads_the_dead_time_and_publishes_it_with_its_closed_loop_time():
    delay = start_dead_time(_dt_est(2100.0), VThermHvacMode_HEAT)                     # L = 35 min
    scheduler, result = _calculate(delay, a=0.02)
    L_min, lam_min = 35.0, 105.0                                                      # lambda = 3 L
    assert result.kp == pytest.approx(1.0 / (0.02 * (lam_min + L_min)))
    assert result.kp_source == "imc_simc"
    assert scheduler.delay == delay and scheduler.closed_loop_time_s == pytest.approx(60.0 * lam_min)


def test_valve_mode_uses_four_delays():
    delay = start_dead_time(_dt_est(2100.0), VThermHvacMode_HEAT)
    scheduler, result = _calculate(delay, a=0.02, valve=True)
    assert scheduler.closed_loop_time_s == pytest.approx(4.0 * 2100.0)
    assert result.kp == pytest.approx(1.0 / (0.02 * (140.0 + 35.0)))
    assert result.kp_source == "imc_simc_valve"


def test_the_cycle_floor_binds_for_a_fast_emitter():
    scheduler, _ = _calculate(start_dead_time(_dt_est(270.0), VThermHvacMode_HEAT), cycle_min=4.0)
    assert scheduler.closed_loop_time_s == pytest.approx(3.0 * 270.0)
    scheduler, _ = _calculate(start_dead_time(_dt_est(360.0), VThermHvacMode_HEAT))
    assert scheduler.closed_loop_time_s == pytest.approx(1200.0)                       # 2 cycles over 3 x 360 s


def test_without_any_delay_the_gains_are_the_safe_ones_and_nothing_is_published():
    scheduler, result = _calculate(None)
    assert result.kp_source == "safe" and scheduler.delay is None and scheduler.closed_loop_time_s is None


def test_pre_model_gains_read_the_dead_time_after_a_start():
    _, result = _calculate(start_dead_time(_dt_est(700.0), VThermHvacMode_HEAT), tau_reliable=False,
                           pre_model_a=0.02)
    assert result.kp_source == "measurement_pulses"
    kp, _ = imc_simc_gains(0.02, 700.0, float("inf"), 10.0)
    assert result.kp == pytest.approx(min(kp, 0.55))
    _, result = _calculate(start_dead_time(_dt_est(None), VThermHvacMode_HEAT), tau_reliable=False,
                           pre_model_a=0.02)
    assert result.kp_source == "safe"


def test_smartpi_gives_the_gains_the_dead_time_even_with_a_model():
    pi = _smartpi()
    pi.dt_est.deadtime_heat_s, pi.dt_est.deadtime_heat_reliable = 480.0, True
    pi._start.model.seed(MODE_HEAT, 300.0, 600.0)                                      # theta_eff 600 s
    pi.gain_scheduler.calculate = MagicMock(wraps=pi.gain_scheduler.calculate)
    pi._apply_gains_and_ff(GovernanceDecision.ADAPT_ON, 20.0, 5.0, VThermHvacMode_HEAT, 2.0, 2.0)
    delay = pi.gain_scheduler.calculate.call_args.kwargs["delay"]
    assert delay == (480.0, SOURCE_DEAD_TIME)
    assert pi.hold_dead_time_s(VThermHvacMode_HEAT) == 600.0


# -- integral holds --------------------------------------------------------------------------------------------
def _smartpi() -> SmartPI:
    return SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="GainDelay")


def test_the_hold_after_a_start_lasts_the_effective_dead_time_of_the_model():
    pi = _smartpi()
    pi.dt_est.deadtime_heat_s, pi.dt_est.deadtime_heat_reliable = 100.0, True
    pi._start.model.seed(MODE_HEAT, 300.0, 600.0)                                      # theta_eff 600 s
    assert pi.hold_dead_time_s(VThermHvacMode_HEAT) == 600.0
    pi._t_heat_episode_start = time.monotonic() - 590.0
    assert pi.in_deadtime_window is True
    pi._t_heat_episode_start = time.monotonic() - 610.0
    assert pi.in_deadtime_window is False


def test_the_hold_reads_the_model_of_the_mode_of_the_episode():
    pi = _smartpi()
    pi._start.model.seed(MODE_HEAT, 300.0, 600.0)
    pi._start.model.seed(MODE_COOL, 60.0, 120.0)                                       # theta_eff 120 s
    pi._t_cool_episode_start = time.monotonic() - 200.0
    assert pi.hold_dead_time_s(VThermHvacMode_COOL) == 120.0
    assert pi.in_deadtime_window is False


def test_without_a_model_the_hold_keeps_the_reliable_dead_time():
    pi = _smartpi()
    pi.dt_est.deadtime_heat_s, pi.dt_est.deadtime_heat_reliable = 300.0, True
    pi._t_heat_episode_start = time.monotonic() - 290.0
    assert pi.hold_dead_time_s(VThermHvacMode_HEAT) == 300.0 and pi.in_deadtime_window is True
    pi.dt_est.deadtime_heat_reliable = False
    assert pi.hold_dead_time_s(VThermHvacMode_HEAT) is None and pi.in_deadtime_window is False


@pytest.mark.parametrize("source", [ResumeHoldSource.WINDOW_RESUME, ResumeHoldSource.HEATING_UNAVAILABLE_RESUME])
def test_a_resume_hold_needs_an_effective_dead_time(source):
    ctl = SmartPIController("test")
    hold = ResumeDeadtimeHold()
    hold.start(source)
    hold.update(error_i=0.6, in_core_deadband=False, deadband_c=0.05, hold_s=None, ctl=ctl,
                in_deadtime_window=lambda: True)
    assert hold.active is False
    hold.start(source)
    hold.update(error_i=0.6, in_core_deadband=False, deadband_c=0.05, hold_s=600.0, ctl=ctl,
                in_deadtime_window=lambda: True)
    assert hold.started is True and ctl.integral_hold_mode == source.value


def test_heating_back_holds_the_integral_for_the_effective_dead_time():
    pi = _smartpi()
    pi._start.model.seed(MODE_HEAT, 300.0, 600.0)
    now = time.monotonic()
    pi._t_heat_episode_start = now - 5000.0
    pi.learning_block._heating_back(now)
    assert pi.resume_hold.source == ResumeHoldSource.HEATING_UNAVAILABLE_RESUME
    assert pi.in_deadtime_window is True
    pi._t_heat_episode_start = time.monotonic() - 601.0
    assert pi.in_deadtime_window is False


# -- diagnostics -----------------------------------------------------------------------------------------------
def test_the_gain_inputs_are_published_in_plain_words():
    pi = _smartpi()
    control = pi.get_published_diagnostics()["analysis"]["control"]
    assert (control["gain_delay_s"], control["gain_delay_source"], control["gain_closed_loop_time_s"]) == (
        None, None, None)
    pi.gain_scheduler.calculate(
        tau_reliable=True, tau_min=1000.0, estimator=SimpleNamespace(a=0.02),
        delay=start_dead_time(_dt_est(1200.0), VThermHvacMode_HEAT), in_near_band=False,
        governance_decision=GovernanceDecision.ADAPT_ON,
    )
    control = pi.get_published_diagnostics()["analysis"]["control"]
    assert (control["gain_delay_s"], control["gain_delay_source"], control["gain_closed_loop_time_s"]) == (
        1200, "dead time", 3600)


# -- room model ------------------------------------------------------------------------------------------------
def test_the_room_model_reads_theta_and_tau_e_of_the_heat_model():
    manager = RoomModelManager("unit", "UTC", None)
    kwargs = dict(hvac_mode=VThermHvacMode_HEAT, temp=19.0, measure_id=1.0, ext=5.0, excluded=None, a_per_min=0.07)
    manager.observe(1000.0, first_moment_s=720.0, response={"theta_s": 300.0, "tau_e_s": 900.0}, **kwargs)
    assert manager._emitter == (900.0, 300.0)
    manager.observe(1060.0, first_moment_s=720.0, response=None, **kwargs)
    assert manager._emitter == (720.0, 0.0)                                           # no model: the dead time


def test_smartpi_gives_the_room_model_the_heat_model():
    pi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0,
                 name="RoomInput", room_model=True)
    pi._start.model.seed(MODE_HEAT, 300.0, 900.0)
    pi._start.model.seed(MODE_COOL, 60.0, 120.0)
    pi.room_model.observe = MagicMock()
    pi.observe_room_model(19.0, 5.0, VThermHvacMode_COOL)
    response = pi.room_model.observe.call_args.kwargs["response"]
    assert (response["theta_s"], response["tau_e_s"]) == (300.0, 900.0)
