"""
Adaptive statistical filter on indoor temperature (T_int).

Produces two output signals from raw sensor readings:
- t_int_lp : low-pass (EMA) smoothed value, used for dead time estimation
- t_int_clean : adaptively gated value, used for learning / slopes / a,b estimation

The PI control loop, deadband, twin and governance are NOT affected.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque

from .const import (
    TINT_LP_ALPHA,
    TINT_SIGMA_WINDOW,
    TINT_SIGMA_MIN,
    TINT_ADAPTIVE_K,
)

_LOGGER = logging.getLogger(__name__)


class AdaptiveTintFilter:
    """EMA low-pass + rolling-sigma adaptive gate on T_int."""

    def __init__(self, name: str, enabled: bool = True):
        self._name = name
        self._enabled = enabled

        self._t_int_lp: float | None = None
        self._t_int_clean: float | None = None
        self._last_published: float | None = None
        self._sigma_buffer: deque[float] = deque(maxlen=TINT_SIGMA_WINDOW)
        self._sigma: float = TINT_SIGMA_MIN
        self._last_update_was_publish: bool = True
        self._hold_start_ts: float | None = None

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    def update(self, t_int_raw: float, now: float) -> tuple[float, float]:
        """Process one raw T_int sample.

        Returns:
            (t_int_lp, t_int_clean)
        """
        if not self._enabled:
            self._t_int_lp = t_int_raw
            self._t_int_clean = t_int_raw
            return (t_int_raw, t_int_raw)

        # Cold start
        if self._t_int_lp is None:
            self._t_int_lp = t_int_raw
            self._t_int_clean = t_int_raw
            self._last_published = t_int_raw
            self._sigma_buffer.append(t_int_raw)
            self._sigma = TINT_SIGMA_MIN
            self._last_update_was_publish = True
            self._hold_start_ts = None
            return (t_int_raw, t_int_raw)

        # Stage 1: EMA low-pass
        self._t_int_lp = (
            TINT_LP_ALPHA * t_int_raw
            + (1.0 - TINT_LP_ALPHA) * self._t_int_lp
        )

        # Stage 2: rolling sigma
        self._sigma_buffer.append(self._t_int_lp)
        if len(self._sigma_buffer) >= 2:
            self._sigma = max(
                statistics.pstdev(self._sigma_buffer), TINT_SIGMA_MIN
            )
        else:
            self._sigma = TINT_SIGMA_MIN

        # Stage 3: adaptive threshold gate
        delta = abs(self._t_int_lp - self._last_published)
        if delta > TINT_ADAPTIVE_K * self._sigma:
            self._t_int_clean = self._t_int_lp
            self._last_published = self._t_int_lp
            self._last_update_was_publish = True
            self._hold_start_ts = None
        else:
            self._t_int_clean = self._last_published
            self._last_update_was_publish = False
            if self._hold_start_ts is None:
                self._hold_start_ts = now

        return (self._t_int_lp, self._t_int_clean)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self):
        """Reset internal state."""
        self._t_int_lp = None
        self._t_int_clean = None
        self._last_published = None
        self._sigma_buffer.clear()
        self._sigma = TINT_SIGMA_MIN
        self._last_update_was_publish = True
        self._hold_start_ts = None

    def save_state(self) -> dict:
        """Save state for persistence."""
        return {
            "t_int_lp": self._t_int_lp,
            "t_int_clean": self._t_int_clean,
            "last_published": self._last_published,
            "sigma_buffer": list(self._sigma_buffer),
            "sigma": self._sigma,
        }

    def load_state(self, state: dict):
        """Load state from persistence."""
        if not state:
            return
        lp = state.get("t_int_lp")
        if lp is not None:
            self._t_int_lp = float(lp)
        cl = state.get("t_int_clean")
        if cl is not None:
            self._t_int_clean = float(cl)
        pub = state.get("last_published")
        if pub is not None:
            self._last_published = float(pub)
        buf = state.get("sigma_buffer", [])
        self._sigma_buffer.clear()
        for v in buf:
            self._sigma_buffer.append(float(v))
        s = state.get("sigma")
        if s is not None:
            self._sigma = max(float(s), TINT_SIGMA_MIN)

    # ------------------------------------------------------------------
    # Diagnostic properties
    # ------------------------------------------------------------------

    @property
    def t_int_lp(self) -> float | None:
        """Low-pass filtered T_int."""
        return self._t_int_lp

    @property
    def t_int_clean(self) -> float | None:
        """Adaptively gated T_int (for learning)."""
        return self._t_int_clean

    @property
    def sigma(self) -> float:
        """Current rolling sigma estimate."""
        return self._sigma

    @property
    def last_update_was_publish(self) -> bool:
        """True if the last update published a value (gate opened)."""
        return self._last_update_was_publish

    @property
    def hold_duration_s(self) -> float:
        """Seconds since last publish (0.0 if not holding)."""
        if self._hold_start_ts is None:
            return 0.0
        return time.monotonic() - self._hold_start_ts
