"""Neutral causal trace of physically committed SmartPI power."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from math import isfinite
from typing import Deque

from .const import GovernanceRegime, clamp


@dataclass(frozen=True)
class AppliedPowerSegment:
    """Linear power applied over one committed monotonic interval."""

    start_monotonic: float
    end_monotonic: float
    linear_power: float
    quality: str = "unspecified"


@dataclass(frozen=True)
class ControlOwnershipSnapshot:
    """Control terms that own one physically committed command."""

    u_ff1: float
    trim_stored: float
    u_ff_visible: float
    u_ff3: float
    u_p: float
    u_i: float
    ki: float
    gain_generation: int
    u_cmd: float
    u_limited: float
    linear_committed_power: float
    regime: GovernanceRegime | str | None
    i_mode: str | None
    quality: str = "causal_full"
    constraint_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlOwnershipSegment:
    """One ownership snapshot over a committed monotonic interval."""

    start_monotonic: float
    end_monotonic: float
    ownership: ControlOwnershipSnapshot


@dataclass(frozen=True)
class TraceDiscontinuity:
    """Explicit uncertainty or invalidation boundary in the physical trace."""

    start_monotonic: float
    end_monotonic: float
    reason: str


@dataclass(frozen=True)
class PhysicalTraceWindow:
    """Immutable view of one requested committed-power interval."""

    start_monotonic: float
    end_monotonic: float
    power_segments: tuple[AppliedPowerSegment, ...]
    ownership_segments: tuple[ControlOwnershipSegment, ...]
    discontinuities: tuple[TraceDiscontinuity, ...]
    mean_linear_power: float | None
    power_coverage_ratio: float
    ownership_coverage_ratio: float
    max_power_gap_s: float
    last_committed_end_monotonic: float | None
    age_s: float | None
    is_stale: bool
    epoch: int
    status: str


class CausalPowerTrace:
    """Own the single bounded trace of realized switch or valve power."""

    _POWER_HISTORY_MAX_S = 24.0 * 60.0 * 60.0
    _MAX_CONTINUITY_GAP_S = 5.0

    def __init__(self) -> None:
        self._power_segments: Deque[AppliedPowerSegment] = deque()
        self._ownership_segments: Deque[ControlOwnershipSegment] = deque()
        self._discontinuities: Deque[TraceDiscontinuity] = deque()
        self._active_cycle_segments: list[AppliedPowerSegment] = []
        self._active_ownership_segments: list[ControlOwnershipSegment] = []
        self._active_power_start: float | None = None
        self._active_linear_power: float | None = None
        self._active_power_quality: str = "unspecified"
        self._active_ownership_start: float | None = None
        self._active_ownership: ControlOwnershipSnapshot | None = None
        self._epoch = 0

    @property
    def epoch(self) -> int:
        """Return the structural reset epoch of this transient trace."""
        return self._epoch

    @property
    def earliest_power_start(self) -> float | None:
        """Return the oldest committed power timestamp still retained."""
        if not self._power_segments:
            return None
        return self._power_segments[0].start_monotonic

    @property
    def last_committed_end(self) -> float | None:
        """Return the end of the newest committed power segment."""
        if not self._power_segments:
            return None
        return self._power_segments[-1].end_monotonic

    @property
    def power_segments(self) -> tuple[AppliedPowerSegment, ...]:
        """Return an immutable snapshot of all retained power segments."""
        return tuple(self._power_segments)

    @property
    def ownership_segments(self) -> tuple[ControlOwnershipSegment, ...]:
        """Return an immutable snapshot of all retained ownership segments."""
        return tuple(self._ownership_segments)

    @property
    def active_ownership_segments(self) -> tuple[ControlOwnershipSegment, ...]:
        """Return an immutable snapshot of provisional ownership segments."""
        return tuple(self._active_ownership_segments)

    def record_applied_power(self, segment: AppliedPowerSegment) -> None:
        """Append one non-overlapping committed segment in linear model space."""
        start = float(segment.start_monotonic)
        end = float(segment.end_monotonic)
        power = clamp(float(segment.linear_power), 0.0, 1.0)
        quality = str(segment.quality)
        if not all(isfinite(value) for value in (start, end, power)) or end <= start:
            return

        if self._power_segments:
            previous = self._power_segments[-1]
            gap_s = start - previous.end_monotonic
            if 0.0 < gap_s <= self._MAX_CONTINUITY_GAP_S:
                self._discontinuities.append(
                    TraceDiscontinuity(
                        previous.end_monotonic,
                        start,
                        "legacy_gap_fill",
                    )
                )
                previous = replace(previous, end_monotonic=start)
                self._power_segments[-1] = previous
            start = max(start, previous.end_monotonic)
            if end <= start:
                return
            if (
                abs(start - previous.end_monotonic) <= 1e-6
                and abs(power - previous.linear_power) <= 1e-9
                and quality == previous.quality
            ):
                self._power_segments[-1] = AppliedPowerSegment(
                    previous.start_monotonic,
                    end,
                    power,
                    quality,
                )
                self._prune_power_history(end)
                return

        self._power_segments.append(
            AppliedPowerSegment(start, end, power, quality)
        )
        self._prune_power_history(end)

    def start_applied_cycle(
        self,
        *,
        now_monotonic: float,
        linear_power: float,
        ownership: ControlOwnershipSnapshot | None = None,
        quality: str = "unspecified",
    ) -> None:
        """Start the provisional trace for one physical scheduler cycle."""
        self._active_cycle_segments.clear()
        self._active_ownership_segments.clear()
        self._active_power_start = float(now_monotonic)
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)
        self._active_power_quality = str(quality)
        self._active_ownership_start = float(now_monotonic)
        self._active_ownership = ownership

    def update_applied_power(
        self,
        *,
        now_monotonic: float,
        linear_power: float,
        ownership: ControlOwnershipSnapshot | None = None,
        quality: str = "valve_segmented_linear",
    ) -> None:
        """Record a provisional valve-power change inside the active cycle."""
        now = float(now_monotonic)
        if (
            self._active_power_start is not None
            and self._active_linear_power is not None
            and now > self._active_power_start
        ):
            self._active_cycle_segments.append(
                AppliedPowerSegment(
                    self._active_power_start,
                    now,
                    self._active_linear_power,
                    self._active_power_quality,
                )
            )
        if (
            self._active_ownership_start is not None
            and self._active_ownership is not None
            and now > self._active_ownership_start
        ):
            self._active_ownership_segments.append(
                ControlOwnershipSegment(
                    self._active_ownership_start,
                    now,
                    self._active_ownership,
                )
            )
        self._active_power_start = now
        self._active_linear_power = clamp(float(linear_power), 0.0, 1.0)
        self._active_power_quality = str(quality)
        self._active_ownership_start = now
        self._active_ownership = ownership

    def complete_applied_cycle(
        self,
        *,
        now_monotonic: float,
        realized_linear_power: float | None,
        use_valve_trace: bool,
    ) -> None:
        """Commit either the valve trace or the realized switch-cycle duty."""
        cycle_end = float(now_monotonic)
        cycle_start = self._active_power_start
        cycle_power = self._active_linear_power
        if cycle_start is None or cycle_end <= cycle_start:
            self._clear_active_cycle()
            return

        if use_valve_trace:
            if cycle_power is not None:
                self._active_cycle_segments.append(
                    AppliedPowerSegment(
                        cycle_start,
                        cycle_end,
                        cycle_power,
                        self._active_power_quality,
                    )
                )
            for segment in self._active_cycle_segments:
                self.record_applied_power(segment)
        elif realized_linear_power is not None:
            first_start = (
                self._active_cycle_segments[0].start_monotonic
                if self._active_cycle_segments
                else cycle_start
            )
            self.record_applied_power(
                AppliedPowerSegment(
                    first_start,
                    cycle_end,
                    clamp(float(realized_linear_power), 0.0, 1.0),
                    "switch_cycle_average",
                )
            )

        if (
            self._active_ownership_start is not None
            and self._active_ownership is not None
            and cycle_end > self._active_ownership_start
        ):
            self._active_ownership_segments.append(
                ControlOwnershipSegment(
                    self._active_ownership_start,
                    cycle_end,
                    self._active_ownership,
                )
            )
        for segment in self._active_ownership_segments:
            self._record_ownership_segment(segment)

        self._clear_active_cycle()

    def mark_discontinuity(self, now_monotonic: float, reason: str) -> None:
        """Record a zero-duration provenance boundary without altering power."""
        now = float(now_monotonic)
        if not isfinite(now) or not str(reason):
            return
        self._discontinuities.append(TraceDiscontinuity(now, now, str(reason)))
        self._prune_power_history(now)

    def read_window(
        self,
        start_monotonic: float,
        end_monotonic: float,
        *,
        now_monotonic: float | None = None,
        max_age_s: float | None = None,
    ) -> PhysicalTraceWindow:
        """Return a clipped immutable view without mutating retention state."""
        start = float(start_monotonic)
        end = float(end_monotonic)
        last_end = self.last_committed_end
        if not isfinite(start) or not isfinite(end) or end <= start:
            return PhysicalTraceWindow(
                start,
                end,
                (),
                (),
                (),
                None,
                0.0,
                0.0,
                0.0,
                last_end,
                None,
                False,
                self._epoch,
                "invalid_interval",
            )

        power_segments = tuple(
            AppliedPowerSegment(
                max(start, segment.start_monotonic),
                min(end, segment.end_monotonic),
                segment.linear_power,
                segment.quality,
            )
            for segment in self._power_segments
            if min(end, segment.end_monotonic)
            > max(start, segment.start_monotonic)
        )
        ownership_segments = tuple(
            ControlOwnershipSegment(
                max(start, segment.start_monotonic),
                min(end, segment.end_monotonic),
                segment.ownership,
            )
            for segment in self._ownership_segments
            if min(end, segment.end_monotonic)
            > max(start, segment.start_monotonic)
        )
        discontinuities = tuple(
            TraceDiscontinuity(
                max(start, item.start_monotonic),
                min(end, item.end_monotonic),
                item.reason,
            )
            for item in self._discontinuities
            if (
                (item.start_monotonic < end and item.end_monotonic > start)
                or (
                    item.start_monotonic == item.end_monotonic
                    and start < item.start_monotonic <= end
                )
            )
        )
        duration = end - start
        power_covered = sum(
            segment.end_monotonic - segment.start_monotonic
            for segment in power_segments
        )
        ownership_covered = sum(
            segment.end_monotonic - segment.start_monotonic
            for segment in ownership_segments
        )
        mean_power = (
            sum(
                (segment.end_monotonic - segment.start_monotonic)
                * segment.linear_power
                for segment in power_segments
            )
            / power_covered
            if power_covered > 0.0
            else None
        )
        power_coverage = clamp(power_covered / duration, 0.0, 1.0)
        ownership_coverage = clamp(ownership_covered / duration, 0.0, 1.0)
        max_gap = self._max_gap(start, end, power_segments)
        age = None
        if now_monotonic is not None and power_segments:
            age = max(
                float(now_monotonic) - power_segments[-1].end_monotonic,
                0.0,
            )
        is_stale = bool(
            max_age_s is not None
            and age is not None
            and age > max(float(max_age_s), 0.0)
        )

        if last_end is None or last_end < end:
            status = "pending"
        elif power_coverage < 1.0 - 1e-9:
            status = "gap"
        elif any(
            item.reason != "legacy_gap_fill" for item in discontinuities
        ):
            status = "discontinuity"
        elif discontinuities:
            status = "imputed"
        elif is_stale:
            status = "stale"
        else:
            status = "complete"
        return PhysicalTraceWindow(
            start,
            end,
            power_segments,
            ownership_segments,
            discontinuities,
            mean_power,
            power_coverage,
            ownership_coverage,
            max_gap,
            last_end,
            age,
            is_stale,
            self._epoch,
            status,
        )

    def reset(self) -> None:
        """Clear committed and provisional physical evidence."""
        self._power_segments.clear()
        self._ownership_segments.clear()
        self._discontinuities.clear()
        self._clear_active_cycle()
        self._epoch += 1

    @staticmethod
    def _max_gap(
        start: float,
        end: float,
        segments: tuple[AppliedPowerSegment, ...],
    ) -> float:
        cursor = start
        largest = 0.0
        for segment in segments:
            largest = max(largest, segment.start_monotonic - cursor)
            cursor = max(cursor, segment.end_monotonic)
        return max(largest, end - cursor)

    def _prune_power_history(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self._POWER_HISTORY_MAX_S
        self._discard_power_before(cutoff)

    def _discard_power_before(self, cutoff: float) -> None:
        while (
            self._power_segments
            and self._power_segments[0].end_monotonic <= cutoff
        ):
            self._power_segments.popleft()
        if (
            self._power_segments
            and self._power_segments[0].start_monotonic < cutoff
            < self._power_segments[0].end_monotonic
        ):
            first = self._power_segments[0]
            self._power_segments[0] = replace(first, start_monotonic=cutoff)
        while (
            self._ownership_segments
            and self._ownership_segments[0].end_monotonic <= cutoff
        ):
            self._ownership_segments.popleft()
        if (
            self._ownership_segments
            and self._ownership_segments[0].start_monotonic < cutoff
            < self._ownership_segments[0].end_monotonic
        ):
            first_ownership = self._ownership_segments[0]
            self._ownership_segments[0] = replace(
                first_ownership,
                start_monotonic=cutoff,
            )
        while (
            self._discontinuities
            and self._discontinuities[0].end_monotonic <= cutoff
        ):
            self._discontinuities.popleft()

    def _record_ownership_segment(self, segment: ControlOwnershipSegment) -> None:
        start = float(segment.start_monotonic)
        end = float(segment.end_monotonic)
        if not isfinite(start) or not isfinite(end) or end <= start:
            return
        if self._ownership_segments:
            previous = self._ownership_segments[-1]
            start = max(start, previous.end_monotonic)
            if end <= start:
                return
            if (
                abs(start - previous.end_monotonic) <= 1e-6
                and previous.ownership == segment.ownership
            ):
                self._ownership_segments[-1] = ControlOwnershipSegment(
                    previous.start_monotonic,
                    end,
                    segment.ownership,
                )
                return
        self._ownership_segments.append(
            ControlOwnershipSegment(start, end, segment.ownership)
        )

    def _clear_active_cycle(self) -> None:
        self._active_cycle_segments.clear()
        self._active_ownership_segments.clear()
        self._active_power_start = None
        self._active_linear_power = None
        self._active_power_quality = "unspecified"
        self._active_ownership_start = None
        self._active_ownership = None
