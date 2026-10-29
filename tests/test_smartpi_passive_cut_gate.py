"""Passive window gate after a power cut: longer of the published lag and the dead time floor."""
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.ab_estimator import ABEstimator
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_SETTLE_K_PASSIVE,
    AB_SETTLE_K_PASSIVE_DEADTIME,
    FreezeReason,
    GovernanceDecision,
    U_OFF_MAX,
)
from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.response_lag import ResponseLagEstimator

B_PER_MIN = 3.0e-4
T_EXT = 5.0
LAG_S = 300.0
PASSIVE_DT_S = 1800.0
TICK_S = 60.0
ON_TICKS = 30


def _observe(est: ResponseLagEstimator, now: float, u: float) -> None:
    est.observe(now=now, dt_s=TICK_S, u=u, t_ext=T_EXT, hvac_mode=VThermHvacMode_HEAT,
                b=B_PER_MIN, tin_history=[])


def _published(est: ResponseLagEstimator) -> None:
    est.load_state({"estimates": [[LAG_S, 20.0]] * 3, "durations": [],
                    "hvac_mode": str(VThermHvacMode_HEAT)})


def test_gate_falls_back_to_the_given_settling_time_without_a_lag():
    est = ResponseLagEstimator("t")
    now = 1000.0
    for k in range(10):
        now += TICK_S
        _observe(est, now, 1.0 if k < 5 else 0.0)
    off_start = 1000.0 + 5 * TICK_S
    assert est.lag_s is None
    assert est.passive_gate_end() is None
    assert est.passive_gate_end(None) is None
    assert est.passive_gate_end(0.0) is None
    assert est.passive_gate_end(PASSIVE_DT_S) == pytest.approx(off_start + PASSIVE_DT_S)
    now += TICK_S
    _observe(est, now, 1.0)
    assert est.passive_gate_end(PASSIVE_DT_S) is None


def test_gate_is_the_longer_of_the_published_lag_and_the_floor():
    est = ResponseLagEstimator("t")
    _published(est)
    now = 1000.0
    for k in range(10):
        now += TICK_S
        _observe(est, now, 1.0 if k < 5 else 0.0)
    off_start = 1000.0 + 5 * TICK_S
    lag_settle = AB_SETTLE_K_PASSIVE * LAG_S
    assert est.passive_gate_end() == pytest.approx(off_start + lag_settle)
    assert est.passive_gate_end(None) == pytest.approx(off_start + lag_settle)
    assert est.passive_gate_end(0.0) == pytest.approx(off_start + lag_settle)
    assert est.passive_gate_end(10.0) == pytest.approx(off_start + lag_settle)
    assert est.passive_gate_end(PASSIVE_DT_S) == pytest.approx(off_start + PASSIVE_DT_S)


def _dt_est(cool_s=PASSIVE_DT_S, cool_reliable=True, heat_s=60.0, heat_reliable=True):
    dt = MagicMock()
    dt.deadtime_heat_reliable = heat_reliable
    dt.deadtime_heat_s = heat_s
    dt.deadtime_cool_reliable = cool_reliable
    dt.deadtime_cool_s = cool_s
    dt.tin_history = []
    return dt


def _run_cut(
    *,
    dt=None,
    lag_published=False,
    in_near_band=False,
    in_deadband=False,
    is_hysteresis=False,
    is_calibrating=False,
    hvac_mode=VThermHvacMode_HEAT,
    off_ticks=60,
):
    """Heat for ON_TICKS ticks, then cut; return (cut time, window starts, reasons)."""
    est = ABEstimator()
    est.b = B_PER_MIN
    est.b_meas_hist.extend([B_PER_MIN] * AB_A_SOFT_GATE_MIN_B)
    dt = dt or _dt_est()
    gov = MagicMock()
    gov.decide_update.return_value = (GovernanceDecision.ADAPT_ON, FreezeReason.NONE)
    win = LearningWindowManager("t")
    if lag_published:
        win.load_state({"response_lag": {"estimates": [[LAG_S, 20.0]] * 3, "durations": [],
                                         "hvac_mode": str(hvac_mode)}})
    sign = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
    t_ext = 35.0 if hvac_mode == VThermHvacMode_COOL else T_EXT
    target = 24.0 if hvac_mode == VThermHvacMode_COOL else 21.0
    now, temp = 1000.0, 20.0
    cut_ts = 1000.0 + ON_TICKS * TICK_S
    starts, reasons = [], []
    ep_start = None
    for k in range(ON_TICKS + off_ticks):
        now += TICK_S
        u = 1.0 if k < ON_TICKS else 0.0
        if u > 0 and ep_start is None:
            ep_start = now - TICK_S
        if u == 0:
            ep_start = None
        temp += sign * (0.01 if u > 0 else -0.005)
        reading = round(temp, 2)
        dt.tin_history.append((now, reading))
        win.update(
            dt_min=TICK_S / 60.0, current_temp=reading, ext_temp=t_ext, u_active=u,
            estimator=est, dt_est=dt, governance=gov, now=now,
            in_deadband=in_deadband, in_near_band=in_near_band,
            t_heat_episode_start=ep_start if hvac_mode == VThermHvacMode_HEAT else None,
            t_cool_episode_start=ep_start if hvac_mode == VThermHvacMode_COOL else None,
            is_hysteresis=is_hysteresis, is_calibrating=is_calibrating,
            hvac_mode=hvac_mode, target_temp=target, raw_temp=reading,
        )
        reasons.append(est.learn_last_reason)
        # Passive windows only: an active window may still be open on the cut tick.
        if win.active and win._u_first is not None and win._u_first < U_OFF_MAX:
            starts.append(win.start_ts)
    return cut_ts, starts, reasons


@pytest.mark.parametrize(
    "in_near_band,in_deadband", [(False, False), (True, False), (False, True)]
)
def test_passive_window_waits_for_the_passive_dead_time_without_a_lag(in_near_band, in_deadband):
    cut_ts, starts, reasons = _run_cut(in_near_band=in_near_band, in_deadband=in_deadband)
    assert "skip: response transient (b)" in reasons
    assert starts
    assert min(starts) >= cut_ts + AB_SETTLE_K_PASSIVE_DEADTIME * PASSIVE_DT_S


@pytest.mark.parametrize(
    "dt,expected",
    [
        # Floor longer than the lag settling time.
        (_dt_est(), AB_SETTLE_K_PASSIVE_DEADTIME * PASSIVE_DT_S),
        # Lag settling time longer than the floor.
        (_dt_est(cool_s=60.0), AB_SETTLE_K_PASSIVE * LAG_S),
        # No reliable passive dead time: the published lag alone.
        (_dt_est(cool_reliable=False), AB_SETTLE_K_PASSIVE * LAG_S),
    ],
)
def test_published_lag_gate_is_raised_to_the_dead_time_floor(dt, expected):
    cut_ts, starts, reasons = _run_cut(dt=dt, lag_published=True)
    assert "skip: response transient (b)" in reasons
    assert min(starts) == pytest.approx(cut_ts + expected)


def test_cooling_mode_uses_the_heating_dead_time_as_passive_dead_time():
    dt = _dt_est(cool_s=60.0, heat_s=PASSIVE_DT_S)
    cut_ts, starts, reasons = _run_cut(dt=dt, hvac_mode=VThermHvacMode_COOL)
    assert "skip: response transient (b)" in reasons
    assert min(starts) >= cut_ts + AB_SETTLE_K_PASSIVE_DEADTIME * PASSIVE_DT_S


@pytest.mark.parametrize(
    "dt",
    [_dt_est(cool_s=None, cool_reliable=False), _dt_est(cool_reliable=False), _dt_est(cool_s=0.0)],
)
def test_no_gate_without_a_reliable_passive_dead_time(dt):
    cut_ts, starts, reasons = _run_cut(dt=dt)
    assert "skip: response transient (b)" not in reasons
    # Opens within a few ticks once the active window has closed.
    assert min(starts) < cut_ts + 5 * TICK_S


@pytest.mark.parametrize("lag_published", [False, True])
@pytest.mark.parametrize("phase", ["hysteresis", "calibration"])
def test_bootstrap_and_calibration_windows_do_not_depend_on_the_passive_dead_time(
    phase, lag_published
):
    """The dead time floor never acts in the bootstrap or a calibration."""
    flags = {
        "is_hysteresis": phase == "hysteresis",
        "is_calibrating": phase == "calibration",
        "lag_published": lag_published,
    }
    runs = [
        _run_cut(dt=_dt_est(cool_s=value), **flags)
        for value in (PASSIVE_DT_S, 10.0 * PASSIVE_DT_S, 60.0)
    ]
    assert runs[0] == runs[1] == runs[2]
    cut_ts, starts, _ = runs[0]
    if lag_published:
        # Only the published lag gate, as before the floor existed.
        assert min(starts) == pytest.approx(cut_ts + AB_SETTLE_K_PASSIVE * LAG_S)
    else:
        # Opens within a few ticks once the active window has closed.
        assert min(starts) < cut_ts + 5 * TICK_S
