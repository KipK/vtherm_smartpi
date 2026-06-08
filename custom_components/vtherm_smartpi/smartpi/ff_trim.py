"""
Feed-Forward Slow Trim for Smart-PI.

The trim corrects a slow, persistent bias in u_ff_ab without replacing the FF principal.
It is applied additively AFTER the taper, so regime-based modulation does not attenuate
the empirical correction.

Signal chain:
  u_ff_eff = clamp(alpha * u_ff_ab + u_ff_trim, 0, 1)

Authority:
  |u_ff_trim| <= rho_trim * max(u_ff_ab, FF_TRIM_EPSILON)

The trim is frozen under several conditions (see freeze()).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Deque

from .const import (
    GovernanceRegime,
    clamp,
    FF_TRIM_RHO,
    FF_TRIM_LAMBDA,
    FF_TRIM_EPSILON,
    FF_TRIM_PERSISTENCE,
    FF_TRIM_BUFFER_SIZE,
    FF_TRIM_DELTA_EPSILON,
    FF_TRIM_PI_STABILITY_EPSILON,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FFTrimUpdateResult:
    """Result of one persistent trim update attempt."""

    updated: bool
    reason: str
    applied_delta: float
    pending_count: int


@dataclass(frozen=True)
class FFTrimPIEligibility:
    """PI-state eligibility for trim learning."""

    admissible: bool
    reason: str


def evaluate_pi_eligibility_for_trim(
    regime: GovernanceRegime | str | None,
    i_mode: str | None,
    u_pi: float | None,
    previous_u_pi: float | None,
) -> FFTrimPIEligibility:
    """Validate that PI is neutral enough for trim learning."""
    if regime == GovernanceRegime.DEAD_BAND:
        if i_mode == "I:FREEZE(deadband)":
            return FFTrimPIEligibility(True, "pi_deadband_freeze")
        return FFTrimPIEligibility(False, f"pi_mode_{i_mode}")

    if regime != GovernanceRegime.NEAR_BAND:
        regime_value = regime.value if isinstance(regime, GovernanceRegime) else regime
        return FFTrimPIEligibility(False, f"regime_{regime_value}")

    if i_mode is None:
        return FFTrimPIEligibility(False, "pi_missing_mode")

    blocked_prefixes = (
        "I:GUARD",
        "I:CLAMP",
        "I:SKIP",
        "I:RESET",
        "I:BLEED",
        "I:HOLD",
    )
    if i_mode.startswith(blocked_prefixes):
        return FFTrimPIEligibility(False, f"pi_mode_{i_mode}")

    if u_pi is None or previous_u_pi is None:
        return FFTrimPIEligibility(False, "pi_pending_stability")

    if abs(u_pi - previous_u_pi) > FF_TRIM_PI_STABILITY_EPSILON:
        return FFTrimPIEligibility(False, "pi_unstable")

    return FFTrimPIEligibility(True, "pi_near_band_stable")


class FFTrim:
    """Slow trim correction on the FF principal u_ff_ab."""

    u_ff_trim: float
    frozen: bool
    freeze_reason: str

    def __init__(self) -> None:
        self.u_ff_trim: float = 0.0
        self.frozen: bool = False
        self.freeze_reason: str = "none"
        self._pending_deltas: Deque[float] = deque(maxlen=FF_TRIM_BUFFER_SIZE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, delta_power: float, u_ff_ab: float) -> None:
        """Update the trim using the observed power deficit.

        The signal should come from thermal physics (slope-based), not from
        u_applied, to avoid self-referential learning when the deadband command
        already contains u_ff_trim.

        Args:
            delta_power: incremental power correction needed
                (positive = need more power).
            u_ff_ab: Current FF principal value (used to compute authority budget).
        """
        if self.frozen:
            return

        authority = FF_TRIM_RHO * max(u_ff_ab, FF_TRIM_EPSILON)
        # delta_power is an incremental correction derived from thermal drift.
        # The trim must therefore converge toward the current trim plus this
        # correction, not toward delta_power as an absolute target.
        target_trim = self.u_ff_trim + delta_power
        new_trim = (1.0 - FF_TRIM_LAMBDA) * self.u_ff_trim + FF_TRIM_LAMBDA * target_trim
        self.u_ff_trim = clamp(new_trim, -authority, authority)

        _LOGGER.debug(
            "FFTrim: updated u_ff_trim=%.4f (delta_power=%.4f, authority=%.4f)",
            self.u_ff_trim,
            delta_power,
            authority,
        )

    def update_persistent(
        self,
        delta_power: float,
        u_ff_ab: float,
    ) -> FFTrimUpdateResult:
        """Update trim only after same-direction thermal corrections persist."""
        if self.frozen:
            self.clear_pending()
            return FFTrimUpdateResult(
                False,
                f"frozen_{self.freeze_reason}",
                0.0,
                0,
            )

        if abs(delta_power) <= FF_TRIM_DELTA_EPSILON:
            self.clear_pending()
            return FFTrimUpdateResult(False, "quiet_delta", 0.0, 0)

        direction = 1.0 if delta_power > 0.0 else -1.0
        previous_direction = self._pending_direction()
        if previous_direction is not None and previous_direction != direction:
            self.clear_pending()

        self._pending_deltas.append(delta_power)
        pending_count = len(self._pending_deltas)
        if pending_count < FF_TRIM_PERSISTENCE:
            return FFTrimUpdateResult(
                False,
                f"pending_{pending_count}/{FF_TRIM_PERSISTENCE}",
                0.0,
                pending_count,
            )

        applied_delta = float(median(self._pending_deltas))
        self.update(applied_delta, u_ff_ab)
        return FFTrimUpdateResult(
            True,
            "updated_persistent",
            applied_delta,
            pending_count,
        )

    def clear_pending(self) -> None:
        """Discard pending trim samples that belong to an invalid context."""
        self._pending_deltas.clear()

    def _pending_direction(self) -> float | None:
        """Return the direction shared by pending deltas, if any."""
        for delta in reversed(self._pending_deltas):
            if abs(delta) > FF_TRIM_DELTA_EPSILON:
                return 1.0 if delta > 0.0 else -1.0
        return None

    def compute_ff_base(self, u_ff_ab: float) -> float:
        """Return u_ff_base = clamp(u_ff_ab + u_ff_trim, 0, 1)."""
        return clamp(u_ff_ab + self.u_ff_trim, 0.0, 1.0)

    def freeze(self, reason: str) -> None:
        """Freeze trim updates."""
        if not self.frozen:
            _LOGGER.debug("FFTrim: frozen (%s)", reason)
        self.frozen = True
        self.freeze_reason = reason
        self.clear_pending()

    def unfreeze(self) -> None:
        """Unfreeze trim updates."""
        if self.frozen:
            _LOGGER.debug("FFTrim: unfrozen")
        self.frozen = False
        self.freeze_reason = "none"

    def reset(self) -> None:
        """Full reset of trim state."""
        self.u_ff_trim = 0.0
        self.frozen = False
        self.freeze_reason = "none"
        self.clear_pending()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_state(self) -> dict:
        return {"u_ff_trim": self.u_ff_trim}

    def load_state(self, state: dict) -> None:
        self.u_ff_trim = float(state.get("u_ff_trim", 0.0))
        self.clear_pending()
