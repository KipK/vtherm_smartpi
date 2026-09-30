"""Tests for neutral acquisition of distinct thermal measurements."""

from datetime import datetime, timezone

from custom_components.vtherm_smartpi.smartpi.thermal_measurement import (
    ThermalMeasurementSource,
)


def test_source_suppresses_repeated_current_measurement() -> None:
    """Repeated runtime evaluations cannot manufacture sensor observations."""
    source = ThermalMeasurementSource()
    measurement_id = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    first = source.observe(
        now_monotonic=100.0,
        measurement_id=measurement_id,
        indoor_temperature=20.1,
        outside_temperature=8.0,
    )
    repeated = source.observe(
        now_monotonic=160.0,
        measurement_id=measurement_id,
        indoor_temperature=20.2,
        outside_temperature=8.1,
    )

    assert first is not None
    assert first.measurement_id == measurement_id.isoformat()
    assert first.observed_monotonic == 100.0
    assert first.indoor_temperature == 20.1
    assert first.outside_temperature == 8.0
    assert first.epoch == 0
    assert repeated is None


def test_missing_identity_is_not_a_thermal_observation() -> None:
    """Generic recalculations do not consume or create measurement identity."""
    source = ThermalMeasurementSource()

    assert source.observe(
        now_monotonic=100.0,
        measurement_id=None,
        indoor_temperature=20.0,
        outside_temperature=None,
    ) is None
    assert source.last_measurement_id is None


def test_invalid_outdoor_value_is_acquired_as_missing() -> None:
    """A bad optional outdoor value cannot contaminate physical consumers."""
    source = ThermalMeasurementSource()

    measurement = source.observe(
        now_monotonic=100.0,
        measurement_id="sensor-1",
        indoor_temperature=20.0,
        outside_temperature=float("nan"),
    )

    assert measurement is not None
    assert measurement.outside_temperature is None


def test_reset_starts_a_new_acquisition_epoch() -> None:
    """A structural reset may reacquire the current sensor observation."""
    source = ThermalMeasurementSource()
    first = source.observe(
        now_monotonic=100.0,
        measurement_id="sensor-1",
        indoor_temperature=20.0,
        outside_temperature=None,
    )

    source.reset()
    reacquired = source.observe(
        now_monotonic=200.0,
        measurement_id="sensor-1",
        indoor_temperature=20.1,
        outside_temperature=None,
    )

    assert first is not None
    assert reacquired is not None
    assert source.epoch == 1
    assert reacquired.epoch == 1
