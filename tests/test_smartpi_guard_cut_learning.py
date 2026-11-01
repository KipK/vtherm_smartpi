"""Learning while the guard cut holds the output at zero."""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import (
    RESPONSE_LAG_RESOLVE_EPISODES,
    GovernanceRegime,
    SmartPIPhase,
)

from .helpers import force_smartpi_stable_mode

_MONOTONIC = "custom_components.vtherm_smartpi.algo.time.monotonic"


def _stable_smartpi() -> SmartPI:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="guard_cut_learning",
    )
    force_smartpi_stable_mode(algo)
    # A model past the bootstrap has a resolved response lag.
    algo.learn_win.response_lag._episodes = RESPONSE_LAG_RESOLVE_EPISODES
    assert algo.phase == SmartPIPhase.STABLE
    return algo


def _calculate(algo: SmartPI, now: float, temp: float, target: float = 16.0) -> None:
    with patch(_MONOTONIC, return_value=now):
        algo.calculate(
            target_temp=target,
            current_temp=temp,
            ext_current_temp=8.0,
            hvac_mode=VThermHvacMode_HEAT,
        )


def _start_cut_after_setpoint_drop(algo: SmartPI, now: float) -> None:
    """A setpoint drop to 16 °C while heating at 19.6 °C triggers the cut."""
    _calculate(algo, now, 19.6, target=20.0)
    action = algo.guards.check_guard_cut(
        current_temp=19.6,
        target_temp=16.0,
        near_band_above=algo.deadband_mgr.near_band_above_deg,
        is_device_active=True,
        hvac_mode=VThermHvacMode_HEAT,
        is_calibration=False,
    )
    assert algo.guards.guard_cut_active, action
    algo._committed_on_percent = 0.0


def test_guard_cut_feeds_the_passive_learning():
    algo = _stable_smartpi()
    _start_cut_after_setpoint_drop(algo, 1000.0)
    algo.deadband_mgr.in_deadband = True

    with patch.object(algo.learn_win, "update", return_value=(0, 0)) as update:
        for i in range(1, 6):
            _calculate(algo, 1000.0 + 60.0 * i, 19.6 - 0.02 * i)

    assert update.call_count == 5
    kwargs = update.call_args.kwargs
    assert math.isclose(kwargs["dt_min"], 1.0)
    assert kwargs["u_active"] == 0.0
    assert kwargs["in_deadband"] is False
    assert kwargs["in_near_band"] is False
    assert kwargs["a_learning_allowed"] is False
    assert kwargs["a_learning_block_reason"] == "skip: guard cut"
    assert algo.gov.regime == GovernanceRegime.SATURATED
    assert algo.ctl.last_i_mode == "I:OVERRIDE(guard_cut)"
    assert algo.on_percent == 0.0


def test_guard_cut_learning_caps_a_tick_gap_at_one_cycle():
    algo = _stable_smartpi()
    _start_cut_after_setpoint_drop(algo, 1000.0)

    with patch.object(algo.learn_win, "update", return_value=(0, 0)) as update:
        _calculate(algo, 1060.0, 19.5)
        _calculate(algo, 1060.0 + 3600.0, 19.0)

    assert math.isclose(update.call_args_list[-1].kwargs["dt_min"], algo._cycle_min)


def test_learning_resumes_normally_after_the_guard_cut():
    algo = _stable_smartpi()
    _start_cut_after_setpoint_drop(algo, 1000.0)
    _calculate(algo, 1060.0, 19.5)
    assert algo._guard_cut_learning_ts == 1060.0

    algo.guards.reset()
    with patch.object(algo.learn_win, "update", return_value=(0, 0)) as update:
        # The first tick after the cut continues from the last cut tick.
        _calculate(algo, 1120.0, 15.9)
        assert algo._guard_cut_learning_ts is None
        _calculate(algo, 1180.0, 15.9)

    assert update.call_count == 2
    for call in update.call_args_list:
        assert math.isclose(call.kwargs["dt_min"], 1.0)
        assert call.kwargs["a_learning_block_reason"] != "skip: guard cut"


def test_b_is_learned_during_the_fall_after_a_setpoint_drop():
    """A guard cut covering the fall to the eco setpoint gives b windows."""
    algo = _stable_smartpi()
    _start_cut_after_setpoint_drop(algo, 1000.0)
    learned_before = algo.est.learn_ok_count_b

    # Passive first-order fall towards the outdoor temperature (b = 0.002 / min).
    for i in range(1, 121):
        temp = round(8.0 + (19.6 - 8.0) * math.exp(-0.002 * i), 2)
        _calculate(algo, 1000.0 + 60.0 * i, temp)

    assert algo.guards.guard_cut_active
    assert algo.phase == SmartPIPhase.STABLE
    assert algo.est.learn_ok_count_b > learned_before
