from __future__ import annotations

import logging
from typing import Set

from .const import (
    SmartPIPhase,
    GovernanceRegime,
    FreezeReason,
    GovernanceDecision,
    GOVERNANCE_MATRIX,
)

_LOGGER = logging.getLogger(__name__)

class SmartPIGovernance:
    """
    Governance System for Smart-PI.
    
    Responsible for:
    1. Determining the current operating regime (WARMUP, STABLE, PERTURBED, etc.)
    2. Deciding whether adaptation (learning, gains) is allowed in the current regime.
    3. Providing detailed reasons for freezing adaptation.
    """

    def __init__(self, name: str):
        self._name = name
        self._cycle_regimes: Set[GovernanceRegime] = set()
        self._current_regime: GovernanceRegime = GovernanceRegime.WARMUP
        
        # Diagnostics
        self.last_decision_thermal: GovernanceDecision = GovernanceDecision.ADAPT_ON
        self.last_freeze_reason_thermal: FreezeReason = FreezeReason.NONE
        self.last_decision_gains: GovernanceDecision = GovernanceDecision.ADAPT_ON
        self.last_freeze_reason_gains: FreezeReason = FreezeReason.NONE

    @property
    def regime(self) -> GovernanceRegime:
        """Expose current regime."""
        return self._current_regime

    @regime.setter
    def regime(self, value: GovernanceRegime) -> None:
        self._current_regime = value

    @property
    def cycle_regimes(self) -> Set[GovernanceRegime]:
        """Expose cycle regimes set."""
        return self._cycle_regimes

    def reset(self):
        """Reset internal state."""
        self._cycle_regimes.clear()
        self._current_regime = GovernanceRegime.WARMUP
        self.last_decision_thermal = GovernanceDecision.ADAPT_ON
        self.last_freeze_reason_thermal = FreezeReason.NONE
        self.last_decision_gains = GovernanceDecision.ADAPT_ON
        self.last_freeze_reason_gains = FreezeReason.NONE

    def load_state(self, state: dict):
        """Load state from persistence. Nothing to restore: all governance state is recomputed."""

    def save_state(self) -> dict:
        """Save state for persistence. Governance state is not persisted:
        it is fully recomputed on the first calculate() call after reboot."""
        return {}

    def on_cycle_start(self):
        """Called at the start of a new cycle."""
        self._cycle_regimes.clear()

    def determine_regime(
        self,
        phase: SmartPIPhase,
        ext_temp: float | None,
        integrator_hold: bool,
        power_shedding: bool,
        output_initialized: bool,
        u_cmd: float,
        in_deadband: bool,
        in_near_band: bool
    ) -> GovernanceRegime:
        """Determines the current governance regime based on system state."""

        # Phase-based
        if phase == SmartPIPhase.HYSTERESIS:
            return GovernanceRegime.WARMUP

        # Degraded: sensor issues
        if ext_temp is None:
            return GovernanceRegime.DEGRADED

        # Perturbed: active perturbation
        if power_shedding:
            return GovernanceRegime.PERTURBED

        # Hold
        if integrator_hold:
            return GovernanceRegime.HOLD

        # Saturation (command at limits) - only meaningful after first output computed
        if output_initialized and (u_cmd <= 0.001 or u_cmd >= 0.999):
            return GovernanceRegime.SATURATED

        # Dead band (checked before near-band because it's a stricter zone)
        if in_deadband:
            return GovernanceRegime.DEAD_BAND

        # Near band
        if in_near_band:
            return GovernanceRegime.NEAR_BAND

        # Default: normal regulation
        return GovernanceRegime.EXCITED_STABLE

    def update_regime(self, regime: GovernanceRegime):
        """Update the current regime and track it for cycle homogeneity."""
        self._current_regime = regime
        self._cycle_regimes.add(self._current_regime)

    def _store_last_decision(
        self,
        domain: str,
        decision: GovernanceDecision,
        reason: FreezeReason,
    ) -> tuple[GovernanceDecision, FreezeReason]:
        """Persist the last governance decision for diagnostics."""
        if domain == "thermal":
            self.last_decision_thermal = decision
            self.last_freeze_reason_thermal = reason
        elif domain == "gains":
            self.last_decision_gains = decision
            self.last_freeze_reason_gains = reason

        return decision, reason

    def decide_update(self, domain: str, learning_resume_ts: float | None = None, now: float = 0.0) -> tuple[GovernanceDecision, FreezeReason]:
        """Central governance decision for a given domain.
        
        Args:
            domain: 'thermal' (a/b learning) or 'gains' (Kp/Ki adaptation)
            learning_resume_ts: Timestamp until which learning is paused (optional)
            now: Current monotonic time (required if learning_resume_ts provided)

        Returns:
            (GovernanceDecision, FreezeReason)
        """
        # Priority 1: Critical errors
        if learning_resume_ts is not None:
            if now < learning_resume_ts:
                return self._store_last_decision(
                    domain,
                    GovernanceDecision.HARD_FREEZE,
                    FreezeReason.PERTURBED,
                )

        # Priority 2: Regime transition (cycle homogeneity)
        if len(self._cycle_regimes) > 1:
            return self._store_last_decision(
                domain,
                GovernanceDecision.HARD_FREEZE,
                FreezeReason.REGIME_TRANSITION,
            )

        # Priority 3: Regime-specific matrix
        regime = self._current_regime
        if regime in GOVERNANCE_MATRIX:
            decision, reason = GOVERNANCE_MATRIX[regime][domain]

            return self._store_last_decision(domain, decision, reason)

        return self._store_last_decision(
            domain,
            GovernanceDecision.HARD_FREEZE,
            FreezeReason.SYSTEM_INEFFICIENT,
        )
