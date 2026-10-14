"""Pure falsification evidence for a persistent reference-governor cap.

A constraining cap carries a promise: the capped command reaches the
predicted terminal temperature once the stop dead time has elapsed.  This
module keeps each promise until its horizon matures, then compares it with the
temperature measured at that time.  When the room repeatedly stays short of
the promise by more than the accepted tolerance while it is not rising in the
demand direction, the nominal model is biased in the conservative direction and
the cap can no longer protect against overshoot.  The owner may then retire the
episode.  Evidence is transient and never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..hvac_mode import VThermHvacMode
from .const import (
    REFERENCE_GOVERNOR_FALSIFICATION_COUNT,
    REFERENCE_GOVERNOR_FALSIFICATION_MAX_SLOPE_H,
)
from .reference_governor import ReferenceGovernorDecision, _direction

_SECONDS_PER_MINUTE = 60.0
_MAX_PENDING_RECORDS = 256
RETIRE_REASON = "prediction_falsified_below_target"


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """One cap promise awaiting its measurement horizon."""

    evaluated_at: float
    matures_at: float
    predicted_terminal_temp: float


@dataclass(frozen=True, slots=True)
class PredictionFalsificationDecision:
    """Immutable evidence published for one observation."""

    retire: bool
    reason: str
    consecutive_shortfalls: int = 0
    shortfall_span_s: float = 0.0
    last_shortfall_c: float | None = None
    tolerance_c: float | None = None


def _finite(value: object) -> float | None:
    """Return a finite number, excluding booleans."""
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if isfinite(numeric) else None


class GovernorPredictionFalsification:
    """Track cap promises and count consecutive matured shortfalls."""

    def __init__(self) -> None:
        self._pending: tuple[PredictionRecord, ...] = ()
        self._count = 0
        self._first_shortfall_at: float | None = None
        self._last_shortfall_at: float | None = None
        self._last_shortfall_c: float | None = None

    @property
    def consecutive_shortfalls(self) -> int:
        """Return the current run of matured shortfalls."""
        return self._count

    @property
    def pending_records(self) -> tuple[PredictionRecord, ...]:
        """Return promises whose horizon has not matured yet."""
        return self._pending

    def reset(self) -> None:
        """Drop all promises and shortfall evidence."""
        self._pending = ()
        self._count = 0
        self._first_shortfall_at = None
        self._last_shortfall_at = None
        self._last_shortfall_c = None

    def observe(
        self,
        *,
        decision: ReferenceGovernorDecision,
        hvac_mode: VThermHvacMode | None,
        target_temp: object,
        current_temp: object,
        measured_slope_h: object | None,
        remaining_cycle_min: object,
        stop_deadtime_s: object,
        now_monotonic: object,
        cycle_min: object,
        tolerance_c: object,
    ) -> PredictionFalsificationDecision:
        """Consume one kernel decision with the observation it was made from.

        Only a valid constraining ``cap`` decision keeps or extends evidence;
        any other decision clears it, so a cap that is lifted by itself never
        inherits stale shortfalls.  At most one matured promise is judged per
        observation, the freshest one, so one measurement cannot count twice.
        """
        now = _finite(now_monotonic)
        tolerance = _finite(tolerance_c)
        direction = _direction(hvac_mode)
        if now is None or tolerance is None or tolerance < 0.0 or direction is None:
            self.reset()
            return PredictionFalsificationDecision(False, "invalid_evidence")

        predicted = _finite(decision.predicted_terminal_temp)
        constraining = (
            decision.reason == "cap"
            and decision.active is True
            and decision.constraint_active is True
            and decision.coast_required is False
            and predicted is not None
        )
        if not constraining:
            self.reset()
            return PredictionFalsificationDecision(False, "not_constraining")

        current = _finite(current_temp)
        target = _finite(target_temp)
        deadtime = _finite(stop_deadtime_s)
        remaining = _finite(remaining_cycle_min)
        cycle = _finite(cycle_min)
        if (
            current is None
            or target is None
            or deadtime is None
            or deadtime <= 0.0
            or remaining is None
            or remaining < 0.0
            or cycle is None
            or cycle <= 0.0
        ):
            self.reset()
            return PredictionFalsificationDecision(False, "invalid_evidence")

        matured = tuple(record for record in self._pending if record.matures_at <= now)
        self._pending = tuple(record for record in self._pending if record.matures_at > now)
        if matured:
            freshest = max(matured, key=lambda record: record.evaluated_at)
            shortfall = direction * (freshest.predicted_terminal_temp - current)
            self._last_shortfall_c = shortfall
            if shortfall > tolerance:
                self._count += 1
                if self._first_shortfall_at is None:
                    self._first_shortfall_at = now
                self._last_shortfall_at = now
            else:
                self._count = 0
                self._first_shortfall_at = None
                self._last_shortfall_at = None

        if not self._pending or now > self._pending[-1].evaluated_at:
            horizon_s = remaining * _SECONDS_PER_MINUTE + deadtime
            record = PredictionRecord(now, now + horizon_s, predicted)
            self._pending = (self._pending + (record,))[-_MAX_PENDING_RECORDS:]

        span = (
            self._last_shortfall_at - self._first_shortfall_at
            if self._first_shortfall_at is not None and self._last_shortfall_at is not None
            else 0.0
        )
        required_span = deadtime + cycle * _SECONDS_PER_MINUTE
        slope = None if measured_slope_h is None else _finite(measured_slope_h)
        slope_allows = (
            slope is not None
            and direction * slope <= REFERENCE_GOVERNOR_FALSIFICATION_MAX_SLOPE_H
        )
        demand_positive = direction * (target - current) > 0.0

        if self._count < REFERENCE_GOVERNOR_FALSIFICATION_COUNT:
            reason = "shortfall_count_insufficient"
        elif span < required_span:
            reason = "shortfall_span_insufficient"
        elif not slope_allows:
            reason = "slope_blocks_retirement"
        elif not demand_positive:
            reason = "no_positive_demand"
        else:
            reason = RETIRE_REASON
        return PredictionFalsificationDecision(
            retire=reason == RETIRE_REASON,
            reason=reason,
            consecutive_shortfalls=self._count,
            shortfall_span_s=span,
            last_shortfall_c=self._last_shortfall_c,
            tolerance_c=tolerance,
        )
