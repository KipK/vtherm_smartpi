"""Neutral acquisition of distinct VT thermal measurements."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class ThermalMeasurement:
    """One distinct sensor observation at its first runtime evaluation."""

    observed_monotonic: float
    measurement_id: str
    indoor_temperature: float
    outside_temperature: float | None
    epoch: int


class ThermalMeasurementSource:
    """Recognize VT measurement changes independently from consumer gates."""

    def __init__(self) -> None:
        self._last_measurement_id: str | None = None
        self._epoch = 0

    @property
    def epoch(self) -> int:
        """Return the structural reset epoch of measurement acquisition."""
        return self._epoch

    @property
    def last_measurement_id(self) -> str | None:
        """Return the last recognized VT measurement identity."""
        return self._last_measurement_id

    def observe(
        self,
        *,
        now_monotonic: float,
        measurement_id: Any | None,
        indoor_temperature: float,
        outside_temperature: float | None,
    ) -> ThermalMeasurement | None:
        """Return a new immutable observation, or None for no new measurement."""
        if measurement_id is None:
            return None
        normalized_id = (
            measurement_id.isoformat()
            if hasattr(measurement_id, "isoformat")
            else str(measurement_id)
        )
        if normalized_id == self._last_measurement_id:
            return None

        observed = float(now_monotonic)
        indoor = float(indoor_temperature)
        outside = (
            float(outside_temperature)
            if outside_temperature is not None
            else None
        )
        self._last_measurement_id = normalized_id
        if not isfinite(observed) or not isfinite(indoor):
            return None
        if outside is not None and not isfinite(outside):
            outside = None

        return ThermalMeasurement(
            observed_monotonic=observed,
            measurement_id=normalized_id,
            indoor_temperature=indoor,
            outside_temperature=outside,
            epoch=self._epoch,
        )

    def reset(self) -> None:
        """Forget acquisition identity at a structural runtime boundary."""
        self._last_measurement_id = None
        self._epoch += 1
