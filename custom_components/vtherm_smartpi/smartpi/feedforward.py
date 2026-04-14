"""
Feed-Forward Orchestrator for Smart-PI Algorithm.

Computes the full FF signal chain:
  ff_raw -> u_ff1 -> u_ff2 -> u_ff_final -> u_ff3 -> u_ff_eff

Compatibility aliases kept for the current codebase:
  u_ff_ab == u_ff1
  u_ff_trim == u_ff2
  u_db_nominal == u_ff_final in nominal operation

Source of truth for runtime FF is u_ff_eff, not ff_raw.

Chain:
  u_ff_eff + u_pi -> u_cmd -> u_limited -> u_applied -> e_eff
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .const import (
    GovernanceRegime,
    clamp,
)
from .ff_trim import FFTrim

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FFResult:
    """Complete FF computation result for one cycle."""

    ff_raw: float          # Raw FF signal before trim (diagnostic)
    u_ff1: float           # Structural FF branch derived from a,b (or fallback value)
    u_ff2: float           # Slow corrective bias branch
    u_ff_final: float      # Final FF command: clamp(u_ff1 + u_ff2, 0, 1)
    u_ff3: float           # Predictive FF3 correction, applied after FF1 + FF2
    u_db_nominal: float    # Nominal deadband FF command
    u_ff_ab: float         # FF principal derived from a,b (or fallback value)
    u_ff_trim: float       # Slow trim correction applied (additive)
    u_ff_base: float       # u_ff_ab (before trim)
    u_ff_eff: float        # FF effectively injected after FF3: clamp(u_ff_final + u_ff3, 0, 1)
    ff_reason: str         # Diagnostic reason string for the current FF state


def compute_ff(
    *,
    k_ff: float,
    target_temp_ff: float,
    ext_temp: float | None,
    warmup_scale: float,
    trim: FFTrim,
    regime: GovernanceRegime,
    error: float,
    near_band_below_deg: float,
    near_band_above_deg: float,
    ab_fallback: float | None,
    u_ff3: float = 0.0,
) -> FFResult:
    """Compute the full FF signal for one cycle.

    Args:
        k_ff: FF gain = b / a (loss-to-heating ratio).
        target_temp_ff: Feedforward reference setpoint (°C).
        ext_temp: Outdoor temperature (°C), or None if unavailable.
        warmup_scale: Combined warmup scaling factor (learn_scale * time_scale * reliable_cap).
        trim: FFTrim instance (provides additive u_ff2 correction).
        regime: Current governance regime.
        error: SP - T_in (positive = below setpoint, negative = above setpoint).
        near_band_below_deg: Near-band width below setpoint (°C).
        near_band_above_deg: Near-band width above setpoint (°C).
        ab_fallback: Fallback FF value from ABConfidence (or None = use u_ff_ab normally).

    Returns:
        FFResult with all FF signal components.
    """
    # --- Step 1: Compute u_ff1 (structural branch) ---
    if ab_fallback is not None:
        # AB_BAD fallback: use empirical hold or 0
        u_ff1 = ab_fallback
        ff_reason_prefix = "ff_ab_fallback"
    elif ext_temp is not None:
        u_ff1 = clamp(k_ff * (target_temp_ff - ext_temp), 0.0, 1.0) * warmup_scale
        ff_reason_prefix = "ff_none"
    else:
        u_ff1 = 0.0
        ff_reason_prefix = "ff_no_ext_temp"

    ff_raw = u_ff1  # Raw FF before bias
    u_ff2 = trim.u_ff_trim

    # --- Step 2: compatibility aliases kept for the current pipeline ---
    u_ff_ab = u_ff1
    u_ff_trim = u_ff2
    u_ff_base = u_ff1

    # --- Step 3: final FF and deadband nominal command ---
    u_ff_final = clamp(u_ff1 + u_ff2, 0.0, 1.0)
    u_db_nominal = u_ff_final
    u_ff_eff = clamp(u_ff_final + u_ff3, 0.0, 1.0)
    ff_reason = ff_reason_prefix

    return FFResult(
        ff_raw=ff_raw,
        u_ff1=u_ff1,
        u_ff2=u_ff2,
        u_ff_final=u_ff_final,
        u_ff3=u_ff3,
        u_db_nominal=u_db_nominal,
        u_ff_ab=u_ff_ab,
        u_ff_trim=u_ff_trim,
        u_ff_base=u_ff_base,
        u_ff_eff=u_ff_eff,
        ff_reason=ff_reason,
    )
