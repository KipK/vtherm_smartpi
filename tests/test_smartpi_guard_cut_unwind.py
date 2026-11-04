"""The guard cut lets a positive integral see the overshoot that holds it."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


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


def test_positive_integral_unwinds_during_the_cut() -> None:
    """The overshoot is integrated per minute, a tick gap counting for one cycle at most."""
    algo = _algo(integral=6.0)
    _ticks(algo, [1000.0, 1060.0], current_temp=19.2)
    assert algo.ctl.integral == pytest.approx(6.0 - 0.2 * 1.0)
    _ticks(algo, [1060.0 + 3600.0], current_temp=19.2)
    assert algo.ctl.integral == pytest.approx(5.8 - 0.2 * 10.0)
    assert algo.ctl.u_i == pytest.approx(algo.Ki * algo.ctl.integral)


def test_unwinding_stops_at_zero() -> None:
    algo = _algo(integral=0.1)
    _ticks(algo, [1000.0, 1060.0], current_temp=19.2)
    assert algo.ctl.integral == 0.0


@pytest.mark.parametrize(
    "integral, u_ff, kp, current_temp",
    [
        (6.0, 0.0, 5.0, 19.2),  # the PI no longer requests power: no integration into SAT_LO
        (-50.0, 0.49, 0.5, 19.37),  # a negative integral is never pushed further
        (6.0, 0.18, 0.5, 18.9),  # below the setpoint: nothing to unwind
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
