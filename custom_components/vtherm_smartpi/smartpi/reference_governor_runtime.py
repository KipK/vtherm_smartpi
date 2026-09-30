"""Authoritative runtime state machine for the SmartPI reference governor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .reference_governor import ReferenceGovernorDecision

_HANDOFF_EVALUATION_COUNT = 3
_SECONDS_PER_MINUTE = 60.0
_UNCONSTRAINED_REASON = "unconstrained"
_NO_POSITIVE_DEMAND_REASON = "no_positive_demand"


class ReferenceGovernorPhase(str, Enum):
    """Runtime phases of the authoritative reference governor."""

    IDLE = "idle"
    GOVERNED = "governed"
    HANDOFF = "handoff"


# The longer name is convenient for callers that keep phase types explicit.
ReferenceGovernorRuntimePhase = ReferenceGovernorPhase


@dataclass(frozen=True, slots=True)
class ReferenceGovernorRuntimeDecision:
    """Immutable runtime output and the kernel evidence behind it."""

    phase: ReferenceGovernorPhase
    effective_reference: float
    command_cap: float | None
    shaping_active: bool
    handoff_ready: bool
    reason: str
    kernel_decision: ReferenceGovernorDecision

    @property
    def kernel_evidence(self) -> ReferenceGovernorDecision:
        """Return the pure kernel decision carried by this runtime output."""
        return self.kernel_decision


class ReferenceGovernorRuntime:
    """Own transient activation and handoff state for one governor instance.

    ``now_monotonic`` is in seconds, as returned by ``time.monotonic()`` in
    this repository.  ``cycle_min`` is in minutes and is converted to seconds
    for the handoff duration test.

    The accepted kernel policy is deliberately narrow.  ``cap`` and ``coast``
    are valid constraining reasons.  ``unconstrained`` is the only ordinary
    release reason.  ``no_positive_demand`` is accepted only when the kernel
    also reports no active/coast constraint, no cap, and no prediction or
    target bound; this is the physically meaningful no-demand bypass.  Every
    other reason, including shadow bypass reasons and invalid kernel reasons,
    aborts and disarms the runtime.
    """

    def __init__(self) -> None:
        self._phase = ReferenceGovernorPhase.IDLE
        self._armed = False
        self._target_temp: float | None = None
        self._last_monotonic: float | None = None
        self._handoff_times: tuple[float, ...] = ()

    @property
    def phase(self) -> ReferenceGovernorPhase:
        """Return the current transient phase."""
        return self._phase

    @property
    def armed(self) -> bool:
        """Return whether a setpoint change has armed this runtime."""
        return self._armed

    @property
    def target_temp(self) -> float | None:
        """Return the target associated with the current arm request."""
        return self._target_temp

    def arm(
        self,
        target_temp: float,
        now_monotonic: float,
        *,
        force: bool = False,
    ) -> None:
        """Arm a target and clear prior runtime evidence.

        A changed target always starts a fresh runtime episode.  Repeating an
        identical target is idempotent so an incidental duplicate callback
        cannot erase governed or handoff state.  Callers may explicitly use
        ``force=True`` when they need to restart the same target.
        """
        target = self._require_finite(target_temp, "target_temp")
        now = self._require_finite(now_monotonic, "now_monotonic")
        if self._target_temp == target and not force:
            return
        self._target_temp = target
        self._armed = True
        self._phase = ReferenceGovernorPhase.IDLE
        self._last_monotonic = now
        self._handoff_times = ()

    def reset(self) -> None:
        """Clear all transient state without creating persisted state."""
        self._phase = ReferenceGovernorPhase.IDLE
        self._armed = False
        self._target_temp = None
        self._last_monotonic = None
        self._handoff_times = ()

    def load_state(self, state: object | None = None) -> None:
        """Clear transient state at a load boundary; no runtime state loads."""
        del state
        self.reset()

    def consume(
        self,
        kernel_decision: ReferenceGovernorDecision,
        now_monotonic: float,
        cycle_min: float,
    ) -> ReferenceGovernorRuntimeDecision:
        """Consume one kernel decision and publish the authoritative output."""
        if not isinstance(kernel_decision, ReferenceGovernorDecision):
            raise TypeError("kernel_decision must be ReferenceGovernorDecision")

        now = self._finite(now_monotonic)
        if now is None:
            return self._abort(kernel_decision, "invalid_timestamp")

        if self._is_constraining(kernel_decision):
            self._advance_clock(now)
            if not self._armed:
                return self._disarmed(kernel_decision)
            self._phase = ReferenceGovernorPhase.GOVERNED
            self._handoff_times = ()
            return self._output(
                kernel_decision,
                phase=ReferenceGovernorPhase.GOVERNED,
                effective_reference=kernel_decision.admissible_reference,
                command_cap=kernel_decision.command_cap,
                shaping_active=True,
                handoff_ready=False,
                reason=kernel_decision.reason,
            )

        if not self._is_valid_unconstrained(kernel_decision):
            return self._abort(kernel_decision, kernel_decision.reason)

        if not self._armed:
            return self._disarmed(kernel_decision)

        if self._phase is ReferenceGovernorPhase.IDLE:
            if kernel_decision.reason == _NO_POSITIVE_DEMAND_REASON:
                cycle_seconds = self._finite(cycle_min)
                if cycle_seconds is None or cycle_seconds <= 0.0:
                    return self._abort(kernel_decision, "invalid_cycle")
                cycle_seconds *= _SECONDS_PER_MINUTE
                if not isfinite(cycle_seconds):
                    return self._abort(kernel_decision, "invalid_cycle")
                self._phase = ReferenceGovernorPhase.HANDOFF
                self._handoff_times = self._append_increasing_timestamp(now)
                return self._handoff_output(kernel_decision, cycle_seconds)
            self._advance_clock(now)
            return self._output(
                kernel_decision,
                phase=ReferenceGovernorPhase.IDLE,
                effective_reference=kernel_decision.nominal_reference,
                command_cap=None,
                shaping_active=False,
                handoff_ready=False,
                reason=kernel_decision.reason,
            )

        cycle_seconds = self._finite(cycle_min)
        if cycle_seconds is None or cycle_seconds <= 0.0:
            return self._abort(kernel_decision, "invalid_cycle")
        cycle_seconds *= _SECONDS_PER_MINUTE
        if not isfinite(cycle_seconds):
            return self._abort(kernel_decision, "invalid_cycle")

        if self._phase is ReferenceGovernorPhase.GOVERNED:
            self._phase = ReferenceGovernorPhase.HANDOFF
            self._handoff_times = self._append_increasing_timestamp(now)
            return self._handoff_output(kernel_decision, cycle_seconds)

        handoff_times = self._append_increasing_timestamp(now)
        self._handoff_times = handoff_times
        return self._handoff_output(kernel_decision, cycle_seconds)

    @staticmethod
    def _finite(value: object) -> float | None:
        """Return a finite number, excluding booleans."""
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric if isfinite(numeric) else None

    @classmethod
    def _require_finite(cls, value: object, name: str) -> float:
        """Validate an explicit arm argument."""
        numeric = cls._finite(value)
        if numeric is None:
            raise ValueError(f"{name} must be finite")
        return numeric

    @classmethod
    def _is_constraining(cls, decision: ReferenceGovernorDecision) -> bool:
        """Return whether a decision is a valid constraining kernel result."""
        cap = cls._finite(decision.command_cap)
        admissible = cls._finite(decision.admissible_reference)
        nominal = cls._finite(decision.nominal_reference)
        return (
            isinstance(decision.reason, str)
            and (
                (
                    decision.reason == "cap"
                    and decision.active is True
                    and decision.constraint_active is True
                    and decision.coast_required is False
                )
                or (
                    decision.reason == "coast"
                    and decision.active is True
                    and decision.constraint_active is True
                    and decision.coast_required is True
                )
            )
            and cap is not None
            and 0.0 <= cap <= 1.0
            and admissible is not None
            and nominal is not None
        )

    @classmethod
    def _is_valid_unconstrained(cls, decision: ReferenceGovernorDecision) -> bool:
        """Apply the narrow release policy documented on the runtime class."""
        nominal = cls._finite(decision.nominal_reference)
        admissible = cls._finite(decision.admissible_reference)
        if (
            nominal is None
            or admissible is None
            or admissible != nominal
            or decision.active
            or decision.constraint_active
            or decision.coast_required
        ):
            return False
        if not isinstance(decision.reason, str):
            return False
        if decision.reason == _UNCONSTRAINED_REASON:
            cap = cls._finite(decision.command_cap)
            return cap is None or 0.0 <= cap <= 1.0
        if decision.reason != _NO_POSITIVE_DEMAND_REASON:
            return False
        return (
            decision.command_cap is None
            and decision.predicted_terminal_temp is None
            and decision.target_bound is None
            and decision.dynamic_reserve_c == 0.0
        )

    def _advance_clock(self, now: float) -> None:
        """Retain the greatest observed timestamp for evidence ordering."""
        if self._last_monotonic is None or now > self._last_monotonic:
            self._last_monotonic = now

    def _append_increasing_timestamp(self, now: float) -> tuple[float, ...]:
        """Add only a timestamp strictly newer than all prior evaluations."""
        if self._last_monotonic is None or now <= self._last_monotonic:
            return self._handoff_times
        self._last_monotonic = now
        return self._handoff_times + (now,)

    def _handoff_output(
        self,
        decision: ReferenceGovernorDecision,
        cycle_seconds: float,
    ) -> ReferenceGovernorRuntimeDecision:
        """Publish handoff output or complete after sufficient evidence."""
        complete = (
            len(self._handoff_times) >= _HANDOFF_EVALUATION_COUNT
            and self._handoff_times[-1] - self._handoff_times[0] >= cycle_seconds
        )
        if complete:
            self._phase = ReferenceGovernorPhase.IDLE
            self._armed = False
            self._handoff_times = ()
            return self._output(
                decision,
                phase=ReferenceGovernorPhase.IDLE,
                effective_reference=decision.nominal_reference,
                command_cap=None,
                shaping_active=False,
                handoff_ready=True,
                reason="handoff_complete",
            )
        return self._output(
            decision,
            phase=ReferenceGovernorPhase.HANDOFF,
            effective_reference=decision.nominal_reference,
            command_cap=None,
            shaping_active=False,
            handoff_ready=False,
            reason="handoff_waiting",
        )

    def _abort(
        self,
        decision: ReferenceGovernorDecision,
        reason: str,
    ) -> ReferenceGovernorRuntimeDecision:
        """Abort activation and remove any previously published cap."""
        prior_target = self._target_temp if self._armed else None
        fallback = self._raw_reference(decision, prior_target=prior_target)
        self.reset()
        return self._output(
            decision,
            phase=ReferenceGovernorPhase.IDLE,
            effective_reference=fallback,
            command_cap=None,
            shaping_active=False,
            handoff_ready=False,
            reason=reason,
        )

    def _disarmed(
        self, decision: ReferenceGovernorDecision
    ) -> ReferenceGovernorRuntimeDecision:
        """Publish raw output after completion or before a new arm."""
        return self._output(
            decision,
            phase=ReferenceGovernorPhase.IDLE,
            effective_reference=self._raw_reference(decision),
            command_cap=None,
            shaping_active=False,
            handoff_ready=False,
            reason="disarmed",
        )

    @classmethod
    def _raw_reference(
        cls,
        decision: ReferenceGovernorDecision,
        *,
        prior_target: float | None = None,
    ) -> float:
        """Return nominal, admissible, or prior-target fallback output."""
        nominal = cls._finite(decision.nominal_reference)
        if nominal is not None:
            return nominal
        admissible = cls._finite(decision.admissible_reference)
        if admissible is not None:
            return admissible
        target = cls._finite(prior_target)
        return target if target is not None else 0.0

    @staticmethod
    def _output(
        decision: ReferenceGovernorDecision,
        *,
        phase: ReferenceGovernorPhase,
        effective_reference: float,
        command_cap: float | None,
        shaping_active: bool,
        handoff_ready: bool,
        reason: str,
    ) -> ReferenceGovernorRuntimeDecision:
        """Construct one immutable runtime output."""
        return ReferenceGovernorRuntimeDecision(
            phase=phase,
            effective_reference=effective_reference,
            command_cap=command_cap,
            shaping_active=shaping_active,
            handoff_ready=handoff_ready,
            reason=reason,
            kernel_decision=decision,
        )
