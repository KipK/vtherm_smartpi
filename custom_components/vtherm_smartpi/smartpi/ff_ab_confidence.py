"""
Feed-Forward A,B Confidence Policy for Smart-PI.

Evaluates the reliability of the thermal model parameters a,b and determines:
  - Whether u_ff_ab should be used normally (AB_OK).
  - Whether u_ff_trim should be slowed / frozen (AB_DEGRADED).
  - Whether the controller should fall back to a conservative pure-PI mode (AB_BAD).

This class consumes existing reliability signals — it does not recompute them.
"""
from __future__ import annotations

import logging

from .const import (
    ABConfidenceState,
    AB_BAD_PERSIST_CYCLES,
    AB_CONFIDENCE_MIN_SAMPLES_A,
    AB_CONFIDENCE_MIN_SAMPLES_B,
)

_LOGGER = logging.getLogger(__name__)


class ABConfidence:
    """Tracks a,b model confidence and manages fallback policy."""

    def __init__(self) -> None:
        self.state: ABConfidenceState = ABConfidenceState.AB_OK
        self._bad_cycle_count: int = 0

    def evaluate(
        self,
        *,
        tau_reliable: bool,
        learn_ok_count_a: int,
        learn_ok_count_b: int,
    ) -> ABConfidenceState:
        """Evaluate and update confidence state.

        Args:
            tau_reliable: Output of ABEstimator.tau_reliability().reliable.
            learn_ok_count_a: Number of accepted a-samples.
            learn_ok_count_b: Number of accepted b-samples.

        Returns:
            New ABConfidenceState.
        """
        enough_a = learn_ok_count_a >= AB_CONFIDENCE_MIN_SAMPLES_A
        enough_b = learn_ok_count_b >= AB_CONFIDENCE_MIN_SAMPLES_B

        if tau_reliable and enough_a and enough_b:
            new_state = ABConfidenceState.AB_OK
        elif tau_reliable:
            # Tau is reliable but sample counts are borderline
            new_state = ABConfidenceState.AB_DEGRADED
        else:
            new_state = ABConfidenceState.AB_BAD

        if new_state == ABConfidenceState.AB_BAD:
            self._bad_cycle_count += 1
        else:
            self._bad_cycle_count = 0

        if self.state != new_state:
            _LOGGER.debug(
                "ABConfidence: %s → %s (tau_reliable=%s, ok_a=%d, ok_b=%d)",
                self.state.value,
                new_state.value,
                tau_reliable,
                learn_ok_count_a,
                learn_ok_count_b,
            )

        self.state = new_state
        return self.state

    def get_ff_fallback(self) -> float | None:
        """Return fallback FF value when a,b are unreliable, or None if no fallback.

        In the refactored architecture there is no empirical hold fallback anymore:
        after sustained AB_BAD we simply fall back to a conservative zero-FF mode
        and let the PI absorb the residual error.
        """
        if self.state != ABConfidenceState.AB_BAD:
            return None

        if self._bad_cycle_count < AB_BAD_PERSIST_CYCLES:
            return None

        return 0.0

    def reset(self) -> None:
        self.state = ABConfidenceState.AB_OK
        self._bad_cycle_count = 0
