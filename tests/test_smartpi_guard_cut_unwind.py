"""The guard cut lets a positive integral track the forced zero output."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import NEAR_BAND_HYSTERESIS_C


def _algo(integral: float, u_ff: float = 0.18, kp: float = 0.5, ki: float = 0.05) -> SmartPI:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="test_cut_unwind",
        max_on_percent=1.0,
        deadband_c=0.0,
    )
    algo.Kp = kp
    algo.Ki = ki
    algo.ctl.integral = integral
    algo.ctl.u_ff = u_ff
    algo.guards._guard_cut_active = True
    return algo


def _ticks(algo: SmartPI, times: list[float], current_temp: float, target_temp: float = 19.0) -> None:
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic") as mono:
        for t in times:
            mono.return_value = t
            algo.calculate(
                target_temp=target_temp,
                current_temp=current_temp,
                ext_current_temp=6.0,
                hvac_mode=VThermHvacMode_HEAT,
            )
            assert algo.on_percent == 0.0
            assert algo.ctl.last_i_mode == "I:OVERRIDE(guard_cut)"


def _integral_floor(algo: SmartPI) -> float:
    """Integral that holds the room at the cut threshold minus its hysteresis."""
    return algo.Kp * (algo.deadband_mgr.near_band_above_deg - NEAR_BAND_HYSTERESIS_C) / algo.Ki


def _tracked(algo: SmartPI, error: float, dt_min: float) -> float:
    """Integral after one back-calculation step toward the applied 0 % (Ti = Kp / Ki)."""
    request = algo.ctl.u_ff + algo.Kp * error + algo.Ki * algo.ctl.integral
    return algo.ctl.integral - request * dt_min / algo.Kp


def test_positive_integral_tracks_the_cut_output() -> None:
    """d(Ki * I)/dt = -v / Ti per minute, a tick gap counting for one cycle at most."""
    algo = _algo(integral=60.0, ki=0.005)
    expected = _tracked(algo, -0.2, 1.0)
    _ticks(algo, [1000.0, 1060.0], current_temp=19.2)
    assert algo.ctl.integral == pytest.approx(expected)
    expected = _tracked(algo, -0.2, 10.0)
    assert expected > _integral_floor(algo)
    _ticks(algo, [1060.0 + 3600.0], current_temp=19.2)
    assert algo.ctl.integral == pytest.approx(expected)
    assert algo.ctl.u_i == pytest.approx(algo.Ki * algo.ctl.integral)


def test_unwinding_follows_the_request_not_the_overshoot() -> None:
    """A surplus integral comes down even when the room barely exceeds the setpoint."""
    algo = _algo(integral=60.0, ki=0.005)
    expected = _tracked(algo, -0.01, 1.0)
    _ticks(algo, [1000.0, 1060.0], current_temp=19.01)
    assert algo.ctl.integral == pytest.approx(expected)
    # Integrating the error alone would only have removed 0.01 in this minute
    assert algo.ctl.integral < 60.0 - 0.5


def test_unwinding_stops_at_the_release_point() -> None:
    """The integral alone no longer holds the room above the cut threshold: the rest is kept."""
    algo = _algo(integral=6.0)
    floor = _integral_floor(algo)
    assert 0.0 < floor < 6.0
    _ticks(algo, [1000.0, 1060.0, 4660.0, 8260.0, 11860.0], current_temp=19.2)
    assert algo.ctl.integral == pytest.approx(floor)
    assert algo.Ki * algo.ctl.integral / algo.Kp == pytest.approx(
        algo.deadband_mgr.near_band_above_deg - NEAR_BAND_HYSTERESIS_C
    )


def test_unwinding_stops_at_zero_without_a_release_margin() -> None:
    algo = _algo(integral=0.1)
    algo.deadband_mgr._near_band_above_deg = NEAR_BAND_HYSTERESIS_C
    _ticks(algo, [1000.0, 1060.0], current_temp=19.2)
    assert algo.ctl.integral == 0.0


def test_the_heat_need_is_kept_after_a_setpoint_decrease() -> None:
    """A cut that follows a setpoint decrease sees the step, not an integral bias."""
    algo = _algo(integral=4.0, u_ff=0.36, kp=0.9, ki=0.01)
    assert algo.ctl.integral < _integral_floor(algo)
    _ticks(algo, [1000.0, 1060.0, 1120.0, 1180.0], current_temp=20.0, target_temp=19.0)
    assert algo.ctl.integral == 4.0


@pytest.mark.parametrize(
    "integral, u_ff, kp, current_temp",
    [
        (6.0, 0.0, 2.0, 19.2),  # the PI no longer requests power: no integration into SAT_LO
        (-50.0, 0.49, 0.5, 19.37),  # a negative integral is never pushed further
        (6.0, 0.18, 0.5, 18.9),  # below the setpoint: nothing to unwind
        (1.0, 0.18, 0.5, 19.2),  # already under the release point
    ],
)
def test_integral_is_kept_outside_the_unwinding_conditions(integral, u_ff, kp, current_temp) -> None:
    algo = _algo(integral=integral, u_ff=u_ff, kp=kp)
    _ticks(algo, [1000.0, 1060.0, 1120.0], current_temp=current_temp)
    assert algo.ctl.integral == integral


def test_unwinding_restarts_after_the_cut_ends() -> None:
    """A new cut starts its own integration clock instead of spanning the regulation in between."""
    algo = _algo(integral=6.0)
    _ticks(algo, [1000.0, 1060.0], current_temp=19.2)
    assert algo._guard_cut_integral_ts == 1060.0
    algo.guards._guard_cut_active = False
    with patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=1120.0):
        algo.calculate(target_temp=19.0, current_temp=19.0, ext_current_temp=6.0, hvac_mode=VThermHvacMode_HEAT)
    assert algo._guard_cut_integral_ts is None
