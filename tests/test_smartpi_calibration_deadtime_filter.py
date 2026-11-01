"""Dead time detection on the filtered temperature during a forced calibration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.calibration import CalibrationManager
from custom_components.vtherm_smartpi.smartpi.const import SmartPICalibrationPhase
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter

_MONOTONIC = "custom_components.vtherm_smartpi.smartpi.calibration.time.monotonic"


def _manager(phase: SmartPICalibrationPhase) -> CalibrationManager:
    mgr = CalibrationManager("t")
    mgr.start_calibration(1.0)
    mgr._calibration_state = phase
    return mgr


def test_calibration_feeds_the_filtered_temperature_to_the_detection():
    mgr = _manager(SmartPICalibrationPhase.HEAT_UP)
    dt_est = MagicMock()

    mgr.calculate(
        target_temp=20.0,
        current_temp=19.9,
        hvac_mode=VThermHvacMode_HEAT,
        dt_est=dt_est,
        max_on_percent=1.0,
        filtered_temp=19.84,
    )

    kwargs = dt_est.update.call_args.kwargs
    assert kwargs["tin"] == 19.84
    assert kwargs["tin_raw"] == 19.9


def test_calibration_without_filtered_temperature_uses_the_raw_reading():
    mgr = _manager(SmartPICalibrationPhase.HEAT_UP)
    dt_est = MagicMock()

    mgr.calculate(
        target_temp=20.0,
        current_temp=19.9,
        hvac_mode=VThermHvacMode_HEAT,
        dt_est=dt_est,
        max_on_percent=1.0,
    )

    kwargs = dt_est.update.call_args.kwargs
    assert kwargs["tin"] == 19.9
    assert kwargs["tin_raw"] == 19.9


def _heat_deadtime(*, filtered: bool) -> float | None:
    """Calibration heat-up with a 0.1 °C sensor flickering by one step.

    The reading flickers between 19.8 and 19.9 °C before the power step and
    for the first 20 minutes of the heat-up, then rises by one step every
    two minutes: the true dead time is 20 minutes.
    """
    mgr = _manager(SmartPICalibrationPhase.COOL_DOWN)
    dt_est = DeadTimeEstimator()
    tint = AdaptiveTintFilter("t")
    now = 0.0

    def tick(raw: float) -> None:
        lp, _ = tint.update(raw, now)
        with patch(_MONOTONIC, return_value=now):
            mgr.calculate(
                target_temp=20.0,
                current_temp=raw,
                hvac_mode=VThermHvacMode_HEAT,
                dt_est=dt_est,
                max_on_percent=1.0,
                filtered_temp=lp if filtered else None,
            )

    for i in range(40):
        tick(19.8 if i % 2 == 0 else 19.9)
        now += 60.0
    mgr._calibration_state = SmartPICalibrationPhase.HEAT_UP
    for i in range(20):
        tick(19.8 if i % 2 == 0 else 19.9)
        now += 60.0
    for i in range(10):
        tick(round(19.9 + 0.1 * (i // 2 + 1), 1))
        now += 60.0
    return dt_est.deadtime_heat_s


def test_flickering_sensor_gives_a_zero_dead_time_on_the_raw_reading():
    dead_time = _heat_deadtime(filtered=False)

    assert dead_time is not None
    assert dead_time <= 120.0


def test_flickering_sensor_keeps_the_true_dead_time_on_the_filtered_reading():
    dead_time = _heat_deadtime(filtered=True)

    assert dead_time is not None
    assert 15 * 60.0 <= dead_time <= 25 * 60.0
