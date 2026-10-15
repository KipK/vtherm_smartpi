"""Elapsed time between two calculate() ticks: continuous one-cycle cap."""

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI

CYCLE_MIN = 10.0


def _elapsed_min(gap_min: float) -> float:
    """Return the dt_min seen by a tick that follows another one after `gap_min` minutes."""
    pi = SmartPI(
        hass=MagicMock(),
        cycle_min=CYCLE_MIN,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestDtCap",
    )
    pi._last_calculate_time = 1000.0
    dt_min, _resumed, _startup, _source = pi._update_time_tracking(1000.0 + gap_min * 60.0)
    return dt_min


@pytest.mark.parametrize("gap_min", [0.0, 0.5, 1.0, 3.0, 9.999])
def test_gap_shorter_than_one_cycle_is_used_as_is(gap_min: float) -> None:
    assert _elapsed_min(gap_min) == pytest.approx(gap_min)


@pytest.mark.parametrize("gap_min", [10.001, 30.0, 99.0, 101.0, 600.0])
def test_gap_longer_than_one_cycle_counts_for_one_cycle(gap_min: float) -> None:
    assert _elapsed_min(gap_min) == pytest.approx(CYCLE_MIN)


def test_cap_is_continuous_at_one_cycle() -> None:
    eps = 1e-6
    below, at, above = _elapsed_min(CYCLE_MIN - eps), _elapsed_min(CYCLE_MIN), _elapsed_min(CYCLE_MIN + eps)
    assert at == pytest.approx(CYCLE_MIN)
    assert abs(at - below) <= 2 * eps
    assert abs(above - at) <= 2 * eps


def test_cap_has_no_step_around_ten_cycles() -> None:
    """9.9 and 10.1 cycles of gap give the same elapsed time."""
    assert _elapsed_min(9.9 * CYCLE_MIN) == pytest.approx(_elapsed_min(10.1 * CYCLE_MIN))


def test_elapsed_time_is_monotonic_in_the_gap() -> None:
    gaps = [0.0, 1.0, 5.0, 9.0, 10.0, 11.0, 50.0, 99.0, 100.0, 101.0, 1000.0]
    values = [_elapsed_min(g) for g in gaps]
    assert values == sorted(values)


def test_first_tick_has_zero_elapsed_time() -> None:
    pi = SmartPI(
        hass=MagicMock(),
        cycle_min=CYCLE_MIN,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestDtCapFirst",
    )
    dt_min, _resumed, _startup, _source = pi._update_time_tracking(5000.0)
    assert dt_min == 0.0
    assert pi._last_calculate_time == 5000.0


def _integral_step_after_gap(gap_min: float) -> tuple[float, str]:
    """Run two STABLE ticks `gap_min` apart; return the integral change and the I mode of the second."""
    from unittest.mock import PropertyMock, patch

    from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
    from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase

    pi = SmartPI(
        hass=MagicMock(),
        cycle_min=CYCLE_MIN,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestDtCapIntegral",
        deadband_c=0.0,
    )
    with (
        patch("custom_components.vtherm_smartpi.algo.time.monotonic") as monotonic,
        patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
    ):
        before = None
        for t in (1000.0, 1000.0 + gap_min * 60.0):
            monotonic.return_value = t
            if before is None and t > 1000.0:
                before = pi.ctl.integral
            pi.calculate(
                target_temp=20.0,
                current_temp=19.5,
                ext_current_temp=5.0,
                slope=0.0,
                hvac_mode=VThermHvacMode_HEAT,
            )
    return pi.ctl.integral - before, pi.ctl.last_i_mode


def test_integral_step_is_continuous_across_the_former_ten_cycle_threshold() -> None:
    one_cycle, mode = _integral_step_after_gap(CYCLE_MIN)
    assert mode == "I:RUN"
    assert one_cycle > 0.0
    assert _integral_step_after_gap(9.9 * CYCLE_MIN)[0] == pytest.approx(one_cycle)
    assert _integral_step_after_gap(10.1 * CYCLE_MIN)[0] == pytest.approx(one_cycle)


def test_integral_step_below_one_cycle_scales_with_the_gap() -> None:
    one_cycle, _ = _integral_step_after_gap(CYCLE_MIN)
    half_cycle, mode = _integral_step_after_gap(CYCLE_MIN / 2.0)
    assert mode == "I:RUN"
    assert 0.0 < half_cycle < one_cycle
