"""The published dead time is the median of the last detections."""

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator


def _detect_heat_response(est: DeadTimeEstimator, start: float, deadtime_s: float) -> float:
    """Drive one OFF -> ON heat response whose temperature starts rising after deadtime_s."""
    now = start
    est.update(now, 20.0, 0.0, hvac_mode=VThermHvacMode_HEAT)
    now += 700.0
    est.update(now, 20.0, 0.0, hvac_mode=VThermHvacMode_HEAT)
    now += 60.0
    heat_start = now
    est.update(now, 20.0, 1.0, hvac_mode=VThermHvacMode_HEAT)
    assert est.state == "WAITING_HEAT_RESPONSE"
    now = heat_start + deadtime_s
    est.update(now, 20.0, 1.0, hvac_mode=VThermHvacMode_HEAT)
    now += 60.0
    est.update(now, 20.06, 1.0, hvac_mode=VThermHvacMode_HEAT)
    assert est.state == "HEATING"
    # Stop the heating and let the room cool back to the start level.
    now += 60.0
    est.update(now, 20.0, 0.0, hvac_mode=VThermHvacMode_HEAT)
    return now


def test_single_aberrant_detection_does_not_move_heat_deadtime() -> None:
    """Two consistent detections and one aberrant one: the median ignores the outlier."""
    est = DeadTimeEstimator()
    now = 1000.0
    for deadtime_s in (300.0, 360.0, 6000.0):
        now = _detect_heat_response(est, now + 60.0, deadtime_s)

    assert list(est._history_heat) == [300.0, 360.0, 6000.0]
    assert est.deadtime_heat_s == 360.0
    assert est.deadtime_heat_reliable is True


def test_cool_deadtime_is_median_of_history() -> None:
    """The passive cool channel publishes the median of its detections."""
    est = DeadTimeEstimator()
    for dt in (0.0, 600.0, 660.0, 720.0):
        est._add_sample_cool(dt)

    # Even count: mean of the two central detections.
    assert est.deadtime_cool_s == 630.0


@pytest.mark.parametrize(
    ("history", "expected"),
    [([300.0], 300.0), ([300.0, 420.0], 360.0)],
)
def test_one_or_two_detections_give_their_mean(history: list[float], expected: float) -> None:
    """With one or two detections the median equals their mean."""
    est = DeadTimeEstimator()
    for dt in history:
        est._add_sample_heat(dt)

    assert est.deadtime_heat_s == expected


def test_restored_deadtime_is_median_of_restored_history() -> None:
    """A persisted value computed as a mean is replaced by the median of the history."""
    est = DeadTimeEstimator()
    est.load_state(
        {
            "deadtime_heat_s": (300.0 + 330.0 + 5400.0) / 3.0,
            "deadtime_cool_s": (900.0 + 60.0 + 960.0) / 3.0,
            "deadtime_heat_reliable": True,
            "deadtime_cool_reliable": True,
            "history_heat": [300.0, 330.0, 5400.0],
            "history_cool": [900.0, 60.0, 960.0],
        }
    )

    assert est.deadtime_heat_s == 330.0
    assert est.deadtime_cool_s == 900.0
    assert est.deadtime_heat_reliable is True
    assert est.deadtime_cool_reliable is True


def test_restored_value_without_history_is_kept() -> None:
    """A persisted dead time without detection history is restored as is."""
    est = DeadTimeEstimator()
    est.load_state(
        {
            "deadtime_heat_s": 420.0,
            "deadtime_heat_reliable": True,
        }
    )

    assert est.deadtime_heat_s == 420.0
    assert est.deadtime_cool_s is None
