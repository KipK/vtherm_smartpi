"""A reset of the dead-time estimator starts its minimum time without heat, as a restart does
(documentation/en/technical_doc.md, 3.6): the room's past is unknown, and a room still warming from
its last heating would give a dead time too short."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import deadtime_estimator
from custom_components.vtherm_smartpi.smartpi.deadtime_estimator import DeadTimeEstimator
from custom_components.vtherm_smartpi.smartpi.tint_filter import AdaptiveTintFilter

RESET_AT = 10_000.0
TRUE_DEADTIME_S = 780.0


def _reset_at(est: DeadTimeEstimator, monkeypatch) -> None:
    monkeypatch.setattr(deadtime_estimator.time, "monotonic", lambda: RESET_AT)
    est.reset()


def _start_after_reset(est: DeadTimeEstimator, *, start_s: float, warming_c_per_h: float) -> None:
    """From the reset, the room drifts by ``warming_c_per_h`` (still warming from its last
    heating, or settled at 0); the heating starts ``start_s`` later at full power and the room
    responds ``TRUE_DEADTIME_S`` after it at 3 degC/h. Fed every 60 s and at the start, read to
    0.01 degC, through the regulation's filter, for two hours after the start or until a
    detection ends."""
    filt = AdaptiveTintFilter("test")
    for t in sorted({60.0 * n for n in range(int((start_s + 7200.0) // 60.0) + 1)} | {start_s}):
        temp = 20.0 + warming_c_per_h * t / 3600.0 + 3.0 * max(0.0, t - start_s - TRUE_DEADTIME_S) / 3600.0
        raw = round(temp, 2)
        lp, _ = filt.update(raw, RESET_AT + t)
        est.update(RESET_AT + t, lp, 1.0 if t >= start_s else 0.0, hvac_mode=VThermHvacMode_HEAT, tin_raw=raw)
        if t >= start_s and est.state != "WAITING_HEAT_RESPONSE":
            return


def test_a_reset_starts_the_minimum_time_without_heat(monkeypatch) -> None:
    est = DeadTimeEstimator()

    _reset_at(est, monkeypatch)

    assert est.last_stop_time == RESET_AT
    assert est.last_stop_time + est.min_off_time_seconds == RESET_AT + 600.0


def test_a_start_85_s_after_a_reset_in_a_room_still_warming_is_not_measured(monkeypatch) -> None:
    """The room still rises from its last heating: a start 85 s after the reset arms no
    detection (HEATING), where an estimator without a last stop measured a dead time of about
    0 s."""
    est = DeadTimeEstimator()
    _reset_at(est, monkeypatch)

    _start_after_reset(est, start_s=85.0, warming_c_per_h=1.0)

    assert est.state == "HEATING"
    assert est.last_outcome["heat"] is None and not est._history_heat
    assert est.deadtime_heat_s is None and est.deadtime_heat_reliable is False

    without_a_stop = DeadTimeEstimator()
    _start_after_reset(without_a_stop, start_s=85.0, warming_c_per_h=1.0)
    assert without_a_stop.last_outcome["heat"] == "detected"
    assert without_a_stop.deadtime_heat_s < 60.0


def test_a_start_700_s_after_a_reset_in_a_settled_room_is_measured_as_before(monkeypatch) -> None:
    """600 s after the reset the start is measured as by an estimator whose last stop was at the
    reset: the room's true dead time."""
    est = DeadTimeEstimator()
    _reset_at(est, monkeypatch)
    _start_after_reset(est, start_s=700.0, warming_c_per_h=0.0)

    author = DeadTimeEstimator()
    author.last_stop_time = RESET_AT
    _start_after_reset(author, start_s=700.0, warming_c_per_h=0.0)

    assert est.last_outcome["heat"] == author.last_outcome["heat"] == "detected"
    assert est._history_heat == author._history_heat
    assert est.deadtime_heat_s == pytest.approx(TRUE_DEADTIME_S, abs=60.0)


def test_a_change_between_heating_and_cooling_starts_the_minimum_time_without_heat(monkeypatch) -> None:
    """The change of mode resets the estimator: a cooling start at that moment arms no
    detection; 600 s later one arms."""
    est = DeadTimeEstimator()
    est.update(1000.0, 24.0, 0.0, hvac_mode=VThermHvacMode_HEAT)
    est.update(1060.0, 24.0, 1.0, hvac_mode=VThermHvacMode_HEAT)
    est.update(1120.0, 24.1, 0.0, hvac_mode=VThermHvacMode_HEAT)
    monkeypatch.setattr(deadtime_estimator.time, "monotonic", lambda: 5000.0)

    est.update(5000.0, 24.0, 1.0, hvac_mode=VThermHvacMode_COOL)

    assert est.model_hvac_mode == str(VThermHvacMode_COOL)
    assert est.last_stop_time == 5000.0
    assert est.state == "COOLING"

    est.update(5300.0, 24.0, 0.0, hvac_mode=VThermHvacMode_COOL)
    est.update(5900.0, 24.0, 1.0, hvac_mode=VThermHvacMode_COOL)
    assert est.state == "WAITING_COOL_RESPONSE"


def test_smartpi_s_learning_reset_starts_the_minimum_time_without_heat(monkeypatch) -> None:
    """The service's reset (SmartPI.reset_learning) resets the estimator with it."""
    smartpi = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0, minimal_deactivation_delay=0, name="Reset")
    smartpi.dt_est.last_stop_time = None
    monkeypatch.setattr(deadtime_estimator.time, "monotonic", lambda: RESET_AT)

    smartpi.reset_learning()

    assert smartpi.dt_est.last_stop_time == RESET_AT
