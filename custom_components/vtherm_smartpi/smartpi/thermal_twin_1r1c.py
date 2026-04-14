"""
Thermal Twin 1R1C — Digital twin based on (a, b) model + deadtime.
Spec v4.5 compliant.

Diagnostics-only: does NOT modify the control command.

Model: dT/dt = a·u(t-L) - b·(T - Text) + d(t)
  - a: heating efficacy (°C·min⁻¹ per unit of u)
  - b: loss coefficient (min⁻¹), tau = 1/b
  - L: dead time (seconds)
  - d(t): external perturbation (solar, occupants, ...), estimated online

Architecture v4 — two distinct predictions:
  - T_pred_pure (pure model, no d_hat): drives T_hat state and d_raw estimation
  - T_pred (corrected model, includes d_hat_ema): exposed diagnostics, RMSE, CUSUM

v4.1 fixes:
  - CUSUM reset after detection (no simultaneous gain+loss)
  - d_hat_ema physical clamp (|d_hat| ≤ d_hat_max)
  - d_hat slow relaxation toward 0 when u < u_min (avoids stale solar bias)
  - rmse_pure exposed (uncompensated model quality indicator)
  - u_buffer without maxlen (fixes infinite loop on deadtime increase)
  - Input validation on dt_s, gamma
  - Buffer normalization after load_state()

v4.2 robustness improvements:
  - NaN/Inf guard on tin_meas input and computed state (prevents permanent corruption)
  - NaN/Inf validation in load_state() (protects against corrupted persistence)
  - Default gamma=0.1 (observer enabled by default)
  - Luenberger nudge clamped to MAX_NUDGE_C per step (prevents wild T_hat jumps)
  - d_raw clamped to D_RAW_MAX before EMA (prevents spike contamination of d_hat)
  - Auto-reset on sustained divergence (RMSE > threshold for DIVERGE_RESET_THRESHOLD steps)
  - Incremental RMSE computation O(1) per step instead of O(n)
  - Innovation bias EMA tracking for slow drift detection
  - T_steady_max (at u=1) and setpoint_reachable_max diagnostics
  - Fix: update_with_eta uses correct deadtime based on power direction (heat/cool)

v4.3 improvements:
  - NaN/Inf guard on text_meas in _resolve_text (prevents NaN propagation to model)
  - NaN/Inf validation in reset() (prevents corrupted initialization)
  - last_tin_meas reset to None in load_state() (prevents stale dTin_dt after HA restart)
  - NaN guard on tin0/target in eta_best_case() (prevents indeterminate results)
  - Periodic RMSE recalibration every 1000 steps (prevents float drift accumulation)
  - Exponential backoff on auto-reset cooldown (prevents reset loops on structural divergence)
  - T_hat_error diagnostic (absolute gap between twin and measurement)
  - warming_up flag (indicates unreliable diagnostics during buffer fill)
  - reset_count diagnostic (tracks cumulative auto-resets for consumer awareness)

v4.4 T_steady audit & reliability:
  - Fix: T_steady_max_valid now returned in diagnostics (was computed but missing from dict)
  - Add u_eff to diagnostics (deadtime-delayed command that actually drives the plant now)
  - Add T_steady_immediate = Text + (a·u_eff + d)/b (physical equilibrium at current heating)
  - Add d_hat_fresh flag (time-based, True when u_eff >= u_min for >= 3 nominal steps)
  - Add T_steady_passive = Text + d/b (equilibrium at u=0, natural floor temperature)
  - Add T_steady_reliable composite flag (T_steady_valid AND model_reliable AND d_hat_fresh)

v4.5 post-reboot reliability:
  - save_state() now saves a Unix timestamp ("saved_at": time.time())
  - load_state() computes downtime and restores state adaptively in 3 levels:
      < 5 min  (REBOOT_SHORT_S):  full restore, unchanged behaviour
      5–60 min (REBOOT_MEDIUM_S): clear u_buffer (fill 0s), clear innovation buffers,
                                   reset CUSUM and confidence counters, decay d_hat_ema
                                   proportionally to downtime
      > 1 h:                       all of the above + stronger d_hat decay +
                                   _needs_resync=True, divergence state reset
  - First step() after a long restart: if T_hat is > REBOOT_RESYNC_THRESHOLD from
    tin_meas, immediately re-synchronise T_hat and clear d_hat_ema
  - step() exposes "cold_start" (bool) and "downtime_s" (float|None) diagnostics

Discretisation: exact exponential (ZOH, unconditionally stable).
Observer: simplified Luenberger (post-prediction nudging on pure model).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from math import exp, isfinite, log, sqrt

from .const import clamp

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(val, default: float = 0.0) -> float:
    """Convert to float, returning default if NaN/Inf/None."""
    if val is None:
        return default
    try:
        f = float(val)
        return f if isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _resolve_text(
    text_meas: float | None,
    policy: str,
    last_text: float | None,
) -> float | None:
    """Resolve Text according to policy. Returns None if unavailable.

    v4.3: NaN/Inf text_meas is treated as missing (falls back to hold_last).
    """
    if text_meas is not None and isfinite(text_meas):
        return float(text_meas)
    if policy == "hold_last" and last_text is not None:
        return last_text
    return None


# ---------------------------------------------------------------------------
# Physical bounds for T_steady guard (§15.4.2)
# ---------------------------------------------------------------------------
T_STEADY_MIN = -50.0
T_STEADY_MAX = 80.0

# ---------------------------------------------------------------------------
# Robustness constants (v4.2)
# ---------------------------------------------------------------------------
MAX_NUDGE_C = 2.0              # °C max Luenberger nudge per step
D_RAW_MAX = 0.5                # °C/min max plausible perturbation rate for d_raw
DIVERGE_RESET_THRESHOLD = 60   # steps before auto-reset (~1h @ 60s/step)
DIVERGE_RMSE_THRESHOLD = 1.5   # °C RMSE above which divergence is counted
BIAS_EMA_ALPHA = 0.02          # Innovation bias EMA learning rate
BIAS_WARN_THRESHOLD = 0.3      # °C bias threshold for warning

# ---------------------------------------------------------------------------
# Post-reboot reliability constants (v4.5)
# ---------------------------------------------------------------------------
REBOOT_SHORT_S  = 5 * 60       # < 5 min  : full restore (current behaviour)
REBOOT_MEDIUM_S = 60 * 60      # 5–60 min : partial clean (buffers, CUSUM, d_hat decay)
# > REBOOT_MEDIUM_S            : full resync (also _diverge_count reset, _needs_resync)
REBOOT_RESYNC_THRESHOLD = 2.0  # °C : T_hat vs tin_meas gap triggering immediate resync


# ---------------------------------------------------------------------------
# ThermalTwin1R1C
# ---------------------------------------------------------------------------


class ThermalTwin1R1C:
    """Digital twin 1R1C with dead-time buffer and Luenberger nudging.

    Spec v4.5 compliant — dual prediction architecture:
      - Pure model for state estimation (T_hat) and d_raw extraction
      - Corrected model (with d_hat_ema) for exposed diagnostics

    v4.4 T_steady: NaN-proof inputs/state/text, clamped nudge & d_raw,
    auto-reset with backoff, incremental RMSE with recalibration, bias tracking,
    T_steady_max_valid returned, T_steady_immediate (at u_eff), T_steady_passive
    (at u=0), d_hat_fresh flag, T_steady_reliable composite.

    v4.5 post-reboot: save_state() stores a Unix timestamp; load_state()
    computes downtime and applies 3-level adaptive restoration so that stale
    signals (u_buffer, CUSUM, innovation buffers, d_hat, T_hat) do not
    corrupt the twin after a long HA shutdown.

    The u_buffer stores timestamped (sim_time_s, u_value) tuples. Size is
    controlled by a purge horizon in step() based on max_deadtime_s.
    """

    def __init__(
        self,
        dt_s: int = 60,
        gamma: float = 0.1,
        text_policy: str = "hold_last",
        u_clip: tuple[float, float] = (0.0, 1.0),
        max_deadtime_s: int = 4 * 3600,
        # --- Advanced diagnostics (§15) ---
        rmse_window: int = 30,
        rmse_reliable_threshold: float = 0.5,
        cusum_delta: float = 0.15,
        cusum_threshold: float = 2.0,
        N_sat: int = 20,
        dT_sat_threshold: float = 0.005,
        eps_sat: float = 0.3,
        d_hat_alpha: float = 0.05,
        d_hat_u_min: float = 0.05,  # v4 §15.6.4: freeze d_hat when u < this
        d_hat_max: float = 0.1,     # v4.1 §15.6.5: clamp |d_hat_ema| (°C/min)
        d_hat_relax: float = 0.005, # v4.1: slow relaxation rate toward 0 when frozen
    ) -> None:
        # ---- Input validation ----
        if dt_s <= 0:
            raise ValueError(f"dt_s must be > 0, got {dt_s}")
        if not 0.0 <= gamma <= 1.0:
            raise ValueError(
                f"gamma must be in [0, 1] for observer stability, got {gamma}"
            )

        self.dt_s: int = int(dt_s)
        self.dt_min: float = self.dt_s / 60.0
        self.gamma: float = float(gamma)
        self.text_policy: str = text_policy
        self.u_min: float = float(u_clip[0])
        self.u_max: float = float(u_clip[1])
        self.max_deadtime_s: int = int(max_deadtime_s)

        # Advanced diagnostics config
        self.rmse_window: int = rmse_window
        self.rmse_reliable_threshold: float = rmse_reliable_threshold
        self.cusum_delta: float = cusum_delta
        self.cusum_threshold: float = cusum_threshold
        self.N_sat: int = N_sat
        self.dT_sat_threshold: float = dT_sat_threshold
        self.eps_sat: float = eps_sat
        self.d_hat_alpha: float = d_hat_alpha
        self.d_hat_u_min: float = d_hat_u_min
        self.d_hat_max: float = d_hat_max
        self.d_hat_relax: float = d_hat_relax

        # Core state
        self.T_hat: float | None = None
        self.T_pred: float | None = None
        self.last_text: float | None = None
        self.dead_steps: int = 0
        self.u_buffer: deque[tuple[float, float]] = deque()  # (sim_time_s, u_value)
        self._sim_time_s: float = 0.0

        # Advanced diagnostics state
        self.cusum_pos: float = 0.0
        self.cusum_neg: float = 0.0
        self.last_tin_meas: float | None = None
        self.d_hat_ema: float = 0.0

        # EMA RMSE state
        self._ema_sq_innovation: float = 0.0
        self._ema_sq_innovation_pure: float = 0.0

        # Robustness state
        self._innovation_bias_ema: float = 0.0
        self._step_count: int = 0
        self._reset_count: int = 0  # for auto-reset cooldown backoff

        # Time-based confidence state
        self._sat_active_time_s: float = 0.0
        self._d_hat_active_time_s: float = 0.0
        self._diverge_high_rmse_s: float = 0.0
        self._diverge_cooldown_s: float = 0.0
        self._rmse_age_s: float = 0.0

        # v4.5 post-reboot state
        self._downtime_s: float = 0.0    # downtime detected at load_state(); reset after 1st step
        self._needs_resync: bool = False  # True → re-sync T_hat at next step() if far from meas

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(
        self,
        tin_init: float,
        text_init: float | None = None,
        u_init: float = 0.0,
    ) -> None:
        """Initialise or warm-restart the twin.

        v4.3: validates tin_init and text_init against NaN/Inf.
        """
        if not isfinite(tin_init):
            raise ValueError(f"reset: tin_init must be finite, got {tin_init}")
        self.T_hat = float(tin_init)
        self.T_pred = None
        if text_init is not None:
            if not isfinite(text_init):
                _LOGGER.warning("ThermalTwin reset: text_init NaN/Inf, ignored")
            else:
                self.last_text = float(text_init)
        u_val = clamp(float(u_init), self.u_min, self.u_max)
        self._sim_time_s = 0.0
        self.u_buffer = deque([(0.0, u_val)])
        # Reset advanced diagnostics state
        self.cusum_pos = 0.0
        self.cusum_neg = 0.0
        self.last_tin_meas = None
        self.d_hat_ema = 0.0
        self._ema_sq_innovation = 0.0
        self._ema_sq_innovation_pure = 0.0
        self._innovation_bias_ema = 0.0
        # Reset step count but NOT _reset_count (persists across resets)
        self._step_count = 0
        # Time-based confidence counters
        self._sat_active_time_s = 0.0
        self._d_hat_active_time_s = 0.0
        self._diverge_high_rmse_s = 0.0
        self._diverge_cooldown_s = 0.0
        self._rmse_age_s = 0.0
        # v4.5: reset reboot flags
        self._downtime_s = 0.0
        self._needs_resync = False

    # ------------------------------------------------------------------
    # Step (one tick of simulation) — v4.1 dual prediction architecture
    # ------------------------------------------------------------------

    def step(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        tin_meas: float,
        text_meas: float | None,
        a: float,
        b: float,
        u_now: float,
        deadtime_s: float,
        sp: float | None = None,
        dt_s: float | None = None,
        mode: str = "heat",
    ) -> dict:
        """Simulate one time-step and return diagnostics dict."""
        if self.T_hat is None:
            return {"status": "not_initialized"}

        # ---- v4.2: NaN/Inf guard on tin_meas ----
        if not isfinite(tin_meas):
            _LOGGER.warning("ThermalTwin: tin_meas is NaN/Inf, skipping step")
            return {"status": "invalid_tin_meas", "T_hat_prev": self.T_hat}

        # ---- v4.5: first-step resync after long reboot ----
        if self._needs_resync:
            if abs(float(tin_meas) - self.T_hat) > REBOOT_RESYNC_THRESHOLD:
                _LOGGER.info(
                    "ThermalTwin v4.5: post-reboot resync T_hat %.2f → %.2f "
                    "(downtime %.0f s)",
                    self.T_hat, float(tin_meas), self._downtime_s,
                )
                self.T_hat = float(tin_meas)
                self.d_hat_ema = 0.0
            self._needs_resync = False

        # ---- Text policy ----
        text_used = _resolve_text(text_meas, self.text_policy, self.last_text)
        if text_meas is not None and isfinite(text_meas):
            self.last_text = float(text_meas)

        if text_used is None:
            return {"status": "missing_text", "T_hat_prev": self.T_hat}

        # ---- Compute actual time step ----
        actual_dt_s = max(0.0, dt_s if dt_s is not None else self.dt_s)
        actual_dt_min = actual_dt_s / 60.0

        if actual_dt_s == 0.0:
            return {
                "status": "zero_dt",
                "T_hat_prev": self.T_hat,
                "T_hat_next": self.T_hat,
                "T_pred": self.T_pred,
                "Tin_meas": float(tin_meas),
                "d_hat_ema": round(self.d_hat_ema, 6),
                "dead_steps": self.dead_steps,
                "u_eff": 0.0,
                "u_now": float(u_now),
            }

        # ---- Mode validation ----
        if mode not in ("heat", "cool"):
            return {"status": "invalid_mode"}

        # ---- Params validation ----
        if not isfinite(a) or not isfinite(b) or b <= 0:
            return {
                "status": "invalid_params",
                "T_hat_prev": self.T_hat,
                "Text_used": text_used,
            }
        if mode == "heat" and a < 0:
            return {
                "status": "invalid_params",
                "T_hat_prev": self.T_hat,
                "Text_used": text_used,
            }
        # ---- Advance simulation clock ----
        self._sim_time_s += actual_dt_s

        # ---- Push command with timestamp ----
        u = clamp(float(u_now), self.u_min, self.u_max)
        self.u_buffer.append((self._sim_time_s, u))

        # ---- u_eff: last command with timestamp <= (now - deadtime) ----
        clamped_deadtime = clamp(deadtime_s, 0, self.max_deadtime_s)
        target_time = self._sim_time_s - clamped_deadtime

        u_eff = self.u_buffer[0][1]  # fallback: oldest
        for ts, uv in self.u_buffer:
            if ts <= target_time:
                u_eff = uv
            else:
                break

        # Purge entries older than max_deadtime + one nominal step margin
        purge_horizon = self._sim_time_s - self.max_deadtime_s - self.dt_s
        while len(self.u_buffer) > 1 and self.u_buffer[0][0] < purge_horizon:
            self.u_buffer.popleft()

        # dead_steps: diagnostic only
        dead_steps = int(round(clamped_deadtime / self.dt_s)) if self.dt_s > 0 else 0
        self.dead_steps = dead_steps

        # ---- Exact discretisation (ZOH, unconditionally stable) ----
        t_prev = self.T_hat
        alpha = exp(-b * actual_dt_min)
        one_minus_alpha = 1.0 - alpha

        # ── PURE prediction (drives T_hat and d_raw) ──
        t_eq_raw = text_used + (a / b) * u_eff
        t_pred_pure = t_eq_raw + (t_prev - t_eq_raw) * alpha

        # ── Innovation on pure model ──
        innovation_pure = float(tin_meas) - t_pred_pure

        # ── d_hat estimation (§15.6) with v4.2 d_raw clamping ──
        if u_eff >= self.d_hat_u_min and one_minus_alpha > 1e-12:
            # Active heating: estimate perturbation from innovation
            # Clamp d_raw to prevent spike contamination
            d_raw = clamp(
                b * innovation_pure / one_minus_alpha,
                -D_RAW_MAX, D_RAW_MAX,
            )
            # Temporal EMA: convert fixed alpha to time-proportional beta
            d_hat_tau_s = self.dt_s / self.d_hat_alpha
            if actual_dt_s > 0 and d_hat_tau_s > 0:
                d_hat_beta = 1.0 - exp(-actual_dt_s / d_hat_tau_s)
            else:
                d_hat_beta = 0.0
            self.d_hat_ema = (
                d_hat_beta * d_raw
                + (1.0 - d_hat_beta) * self.d_hat_ema
            )
            # Physical clamp (§15.6.5)
            self.d_hat_ema = clamp(
                self.d_hat_ema, -self.d_hat_max, self.d_hat_max
            )
        else:
            # u too low: slow relaxation toward 0 instead of hard freeze.
            # Avoids stale solar bias when heating OFF at night.
            # Time-proportional decay: equivalent to per-step relax at nominal dt_s.
            self.d_hat_ema *= (1.0 - self.d_hat_relax) ** (actual_dt_s / self.dt_s)

        # ── CORRECTED prediction (diagnostics only) ──
        t_eq_corr = text_used + (a * u_eff + self.d_hat_ema) / b
        t_pred = t_eq_corr + (t_prev - t_eq_corr) * alpha

        # ── Innovation on corrected model (for RMSE, CUSUM) ──
        innovation = float(tin_meas) - t_pred

        # ---- Nudge (Luenberger, on PURE model — §4.2) ----
        # v4.2: clamp innovation to prevent wild T_hat jumps on sensor glitches
        if self.gamma > 0:
            max_inno = MAX_NUDGE_C / self.gamma
            clamped_inno_pure = clamp(innovation_pure, -max_inno, max_inno)
            t_next = t_pred_pure + self.gamma * clamped_inno_pure
        else:
            t_next = t_pred_pure

        # ---- v4.2: NaN/Inf guard on computed state ----
        if not isfinite(t_next):
            _LOGGER.warning(
                "ThermalTwin: t_next is NaN/Inf, resyncing to tin_meas=%.2f",
                tin_meas,
            )
            t_next = float(tin_meas)
            self.d_hat_ema = 0.0

        # ---- Update state ----
        self.T_hat = t_next
        self.T_pred = t_pred if isfinite(t_pred) else t_next

        # ---- Advanced diagnostics (§15) ----
        adv = self._compute_advanced_diagnostics(
            innovation=innovation,
            innovation_pure=innovation_pure,
            a=a, b=b,
            u_now=u,
            u_eff=u_eff,
            tin_meas=float(tin_meas),
            text_used=text_used,
            sp=sp,
            actual_dt_s=actual_dt_s,
            mode=mode,
        )

        # ---- v4.2/v4.3: auto-reset on sustained divergence with backoff ----
        auto_reset_triggered = False
        if adv.get("_auto_reset_needed"):
            self._reset_count += 1
            _LOGGER.warning(
                "ThermalTwin: auto-reset #%d after %.0f s high RMSE (RMSE=%.3f)",
                self._reset_count,
                self._diverge_high_rmse_s,
                adv.get("rmse_30", 0),
            )
            self.T_hat = float(tin_meas)
            self.T_pred = float(tin_meas)
            self.d_hat_ema = 0.0
            self._ema_sq_innovation = 0.0
            self._ema_sq_innovation_pure = 0.0
            self._rmse_age_s = 0.0
            self.cusum_pos = 0.0
            self.cusum_neg = 0.0
            self._innovation_bias_ema = 0.0
            self._step_count = 0
            self._d_hat_active_time_s = 0.0
            self.last_tin_meas = None
            # Exponential backoff cooldown (seconds)
            cooldown_table = [3600, 7200, 14400, 21600]
            idx = min(self._reset_count - 1, len(cooldown_table) - 1)
            self._diverge_cooldown_s = cooldown_table[idx]
            self._diverge_high_rmse_s = 0.0
            auto_reset_triggered = True
            t_next = float(tin_meas)

        # ---- v4.5: cold_start diagnostics (ephemeral — reset after first step) ----
        cold_start = self._downtime_s > REBOOT_SHORT_S
        downtime_diag = (
            round(self._downtime_s, 0)
            if self._downtime_s < float("inf")
            else None
        )
        self._downtime_s = 0.0  # consumed — subsequent steps report 0

        return {
            "status": "ok",
            "T_hat_prev": t_prev,
            "T_hat_next": t_next,
            "T_pred": self.T_pred,
            "Tin_meas": float(tin_meas),
            "Text_used": text_used,
            "a": float(a),
            "b": float(b),
            "tau_min": 1.0 / float(b),
            "deadtime_s": float(deadtime_s),
            "dead_steps": int(self.dead_steps),
            "u_now": u,
            "u_eff": u_eff,   # v4.4: deadtime-delayed command actually driving the plant
            "gamma": float(self.gamma),
            "innovation": innovation,
            "d_hat_ema": round(self.d_hat_ema, 6),
            "T_hat_error": round(abs(t_next - float(tin_meas)), 4),
            "auto_reset_triggered": auto_reset_triggered,
            # v4.5 reboot diagnostics
            "cold_start": cold_start,
            "downtime_s": downtime_diag,
            **adv,
        }

    # ------------------------------------------------------------------
    # Advanced diagnostics (§15) — v4.1
    # ------------------------------------------------------------------

    def _compute_advanced_diagnostics(  # pylint: disable=too-many-locals
        self,
        innovation: float,
        innovation_pure: float,
        a: float,
        b: float,
        u_now: float,
        u_eff: float,
        tin_meas: float,
        text_used: float,
        sp: float | None = None,
        actual_dt_s: float = 0.0,
        mode: str = "heat",
    ) -> dict:
        """Compute advanced diagnostics from innovation signals.

        v4.4 T_steady note:
          T_steady uses u_now (current command) — answers "where will we converge
          at the CURRENT COMMAND?" This is the correct quantity for setpoint planning.
          Deadtime shifts the transient, not the equilibrium.

          T_steady_immediate uses u_eff (deadtime-delayed) — answers "where is the
          CURRENT HEATING EFFECT driving us?" Useful to detect optimism/pessimism
          during command transitions.

          T_steady_passive uses u=0 — the natural floor temperature (no heating).
        """

        # ---- warming_up flag (time-based) ----
        self._rmse_age_s += actual_dt_s
        warming_up = self._rmse_age_s < self.rmse_window * self.dt_s

        # ---- §15.1 RMSE via temporal EMA ----
        rmse_tau_s = self.rmse_window * self.dt_s
        if actual_dt_s > 0 and rmse_tau_s > 0:
            beta = 1.0 - exp(-actual_dt_s / rmse_tau_s)
        else:
            beta = 0.0

        self._ema_sq_innovation = (
            beta * (innovation ** 2) + (1.0 - beta) * self._ema_sq_innovation
        )
        self._ema_sq_innovation_pure = (
            beta * (innovation_pure ** 2) + (1.0 - beta) * self._ema_sq_innovation_pure
        )

        rmse = sqrt(self._ema_sq_innovation) if not warming_up else None
        rmse_pure = sqrt(self._ema_sq_innovation_pure) if not warming_up else None
        model_reliable = rmse is not None and rmse < self.rmse_reliable_threshold

        # ---- §15.2 CUSUM with reset (v4.1) ----
        self.cusum_pos = max(0.0, self.cusum_pos + innovation - self.cusum_delta)
        self.cusum_neg = max(0.0, self.cusum_neg - innovation - self.cusum_delta)
        external_gain_detected = self.cusum_pos > self.cusum_threshold
        external_loss_detected = self.cusum_neg > self.cusum_threshold
        if external_gain_detected:
            self.cusum_pos = 0.0
        if external_loss_detected:
            self.cusum_neg = 0.0

        # ---- §15.3 Perturbation ----
        perturbation_dTdt = b * innovation

        cooling_model_available = (a <= 0) if mode == "cool" else None

        # ---- §15.4 T_steady family (v4.4 audit) ----
        # T_steady: equilibrium at current COMMAND u_now (planning / setpoint reachability)
        # Deadtime only shifts the transient, not the steady-state → u_now is correct here.
        T_steady = text_used + (a * u_now + self.d_hat_ema) / b
        T_steady_valid = (T_STEADY_MIN <= T_steady <= T_STEADY_MAX)
        if not T_steady_valid:
            model_reliable = False

        if sp is not None and T_steady_valid:
            setpoint_reachable = (T_steady >= sp) if mode == "heat" else (T_steady <= sp)
        else:
            setpoint_reachable = None

        # T_steady_max: equilibrium at max power u=1 — "can the system ever reach sp?"
        T_steady_max = text_used + (a * 1.0 + self.d_hat_ema) / b
        T_steady_max_valid = (T_STEADY_MIN <= T_steady_max <= T_STEADY_MAX)
        if sp is not None and T_steady_max_valid:
            setpoint_reachable_max = (T_steady_max >= sp) if mode == "heat" else (T_steady_max <= sp)
        else:
            setpoint_reachable_max = None

        # v4.4: T_steady_immediate — equilibrium at u_eff (deadtime-delayed command).
        # Shows where the current HEATING EFFECT is driving the temperature.
        # Differs from T_steady during command transitions (e.g. u went 0→1 just now).
        T_steady_immediate = text_used + (a * u_eff + self.d_hat_ema) / b
        T_steady_immediate_valid = (T_STEADY_MIN <= T_steady_immediate <= T_STEADY_MAX)

        # v4.4: T_steady_passive — equilibrium with no heating (u=0).
        # Natural floor temperature: where the room converges without any control action.
        T_steady_passive = text_used + self.d_hat_ema / b
        T_steady_passive_valid = (T_STEADY_MIN <= T_steady_passive <= T_STEADY_MAX)

        # d_hat freshness tracking (time-based)
        if u_eff >= self.d_hat_u_min:
            self._d_hat_active_time_s += actual_dt_s
        else:
            self._d_hat_active_time_s = 0.0
        d_hat_fresh = self._d_hat_active_time_s >= 3 * self.dt_s

        # ---- §15.5 Emitter saturation (time-based) ----
        if u_now >= 0.95:
            self._sat_active_time_s += actual_dt_s
        else:
            self._sat_active_time_s = 0.0

        dTin_dt = None
        actual_dt_min = actual_dt_s / 60.0
        if self.last_tin_meas is not None and actual_dt_min > 0:
            dTin_dt = (tin_meas - self.last_tin_meas) / actual_dt_min
        self.last_tin_meas = tin_meas

        if sp is not None:
            if mode == "heat":
                temp_beyond_sat = tin_meas < sp - self.eps_sat
            else:
                temp_beyond_sat = tin_meas > sp + self.eps_sat
        else:
            temp_beyond_sat = False

        emitter_saturated = (
            sp is not None
            and self._sat_active_time_s >= self.N_sat * self.dt_s
            and dTin_dt is not None
            and abs(dTin_dt) < self.dT_sat_threshold
            and temp_beyond_sat
        )

        self._step_count += 1

        # ---- T_steady_reliable composite flag ----
        # True only when all conditions are met:
        #   - T_steady is within physical bounds
        #   - Overall model RMSE is low (reliable parameters)
        #   - d_hat_ema is fresh (recently estimated under active heating)
        #   - Innovation buffer is full (no warm-up artefacts)
        T_steady_reliable = (
            T_steady_valid
            and model_reliable
            and d_hat_fresh
            and not warming_up
        )

        # ---- Innovation bias tracking (temporal EMA) ----
        bias_tau_s = self.dt_s / BIAS_EMA_ALPHA
        if actual_dt_s > 0 and bias_tau_s > 0:
            bias_beta = 1.0 - exp(-actual_dt_s / bias_tau_s)
        else:
            bias_beta = 0.0
        self._innovation_bias_ema = (
            bias_beta * innovation
            + (1.0 - bias_beta) * self._innovation_bias_ema
        )
        bias_warning = abs(self._innovation_bias_ema) > BIAS_WARN_THRESHOLD

        # ---- Divergence detection (time-based) with cooldown ----
        if self._diverge_cooldown_s > 0:
            self._diverge_cooldown_s = max(0.0, self._diverge_cooldown_s - actual_dt_s)
            auto_reset_needed = False
        elif rmse is not None and rmse > DIVERGE_RMSE_THRESHOLD:
            self._diverge_high_rmse_s += actual_dt_s
            auto_reset_needed = self._diverge_high_rmse_s >= DIVERGE_RESET_THRESHOLD * self.dt_s
        else:
            self._diverge_high_rmse_s = 0.0
            auto_reset_needed = False

        return {
            "rmse_30": round(rmse, 4) if rmse is not None else None,
            "rmse_pure": round(rmse_pure, 4) if rmse_pure is not None else None,
            "model_reliable": model_reliable,
            "perturbation_dTdt": round(perturbation_dTdt, 6),
            "cusum_pos": round(self.cusum_pos, 4),
            "cusum_neg": round(self.cusum_neg, 4),
            "external_gain_detected": external_gain_detected,
            "external_loss_detected": external_loss_detected,
            # ---- T_steady family (v4.4) ----
            "T_steady": round(T_steady, 2),
            "T_steady_valid": T_steady_valid,
            "T_steady_reliable": T_steady_reliable,
            "setpoint_reachable": setpoint_reachable,
            "T_steady_max": round(T_steady_max, 2),
            "T_steady_max_valid": T_steady_max_valid,     # v4.4: was missing
            "setpoint_reachable_max": setpoint_reachable_max,
            "T_steady_immediate": round(T_steady_immediate, 2),   # v4.4: at u_eff
            "T_steady_immediate_valid": T_steady_immediate_valid,
            "T_steady_passive": round(T_steady_passive, 2),       # v4.4: at u=0
            "T_steady_passive_valid": T_steady_passive_valid,
            "d_hat_fresh": d_hat_fresh,                            # v4.4
            # ---- rest ----
            "emitter_saturated": emitter_saturated,
            "cooling_model_available": cooling_model_available,
            "innovation_bias": round(self._innovation_bias_ema, 4),
            "bias_warning": bias_warning,
            "warming_up": warming_up,
            "reset_count": self._reset_count,
            "_auto_reset_needed": auto_reset_needed,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_state(self) -> dict:
        """Save twin state for persistence across restarts."""
        return {
            "T_hat": self.T_hat,
            "T_pred": self.T_pred,
            "last_text": self.last_text,
            "dead_steps": self.dead_steps,
            "u_buffer": [(ts, uv) for ts, uv in self.u_buffer],
            "sim_time_s": self._sim_time_s,
            "cusum_pos": self.cusum_pos,
            "cusum_neg": self.cusum_neg,
            "last_tin_meas": self.last_tin_meas,
            "d_hat_ema": self.d_hat_ema,
            # EMA RMSE
            "ema_sq_innovation": self._ema_sq_innovation,
            "ema_sq_innovation_pure": self._ema_sq_innovation_pure,
            # Robustness state
            "innovation_bias_ema": self._innovation_bias_ema,
            "step_count": self._step_count,
            "reset_count": self._reset_count,
            # Time-based confidence
            "sat_active_time_s": self._sat_active_time_s,
            "d_hat_active_time_s": self._d_hat_active_time_s,
            "diverge_high_rmse_s": self._diverge_high_rmse_s,
            "diverge_cooldown_s": self._diverge_cooldown_s,
            "rmse_age_s": self._rmse_age_s,
            # Timestamp for downtime computation on next load
            "saved_at": time.time(),
        }

    def load_state(self, state: dict) -> None:
        """Restore twin state from persisted data.

        Handles migration from old format (flat u_buffer, step-based counters)
        to new format (timestamped u_buffer, time-based confidence state).
        Validates all loaded floats against NaN/Inf to prevent corruption.
        """
        if not state:
            return

        # v4.2: validate core floats — reset to safe defaults if corrupted
        t_hat_raw = state.get("T_hat")
        self.T_hat = float(t_hat_raw) if (t_hat_raw is not None and isfinite(float(t_hat_raw))) else None

        t_pred_raw = state.get("T_pred")
        self.T_pred = float(t_pred_raw) if (t_pred_raw is not None and isfinite(float(t_pred_raw))) else None

        lt_raw = state.get("last_text")
        self.last_text = float(lt_raw) if (lt_raw is not None and isfinite(float(lt_raw))) else None

        ds = state.get("dead_steps", 0)
        self.dead_steps = int(ds)
        buf = state.get("u_buffer", [])
        # Migration: handle both old (flat float) and new (timestamped tuple) formats
        if buf and isinstance(buf[0], (list, tuple)) and len(buf[0]) == 2:
            # New format: (sim_time_s, u_value) tuples
            clean = [(float(ts), float(uv)) for ts, uv in buf
                     if isinstance(ts, (int, float)) and isfinite(float(ts))
                     and isinstance(uv, (int, float)) and isfinite(float(uv))]
            self.u_buffer = deque(clean) if clean else deque([(0.0, 0.0)])
            self._sim_time_s = _safe_float(state.get("sim_time_s"), 0.0)
        else:
            # Old format: plain floats — reconstruct with single timestamp
            clean = [v for v in buf if isinstance(v, (int, float)) and isfinite(v)]
            last_val = clean[-1] if clean else 0.0
            self.u_buffer = deque([(0.0, last_val)])
            self._sim_time_s = 0.0

        self.cusum_pos = _safe_float(state.get("cusum_pos"), 0.0)
        self.cusum_neg = _safe_float(state.get("cusum_neg"), 0.0)

        # Always reset last_tin_meas after load to avoid stale dTin_dt
        self.last_tin_meas = None

        self.d_hat_ema = _safe_float(state.get("d_hat_ema"), 0.0)

        # EMA RMSE state
        self._ema_sq_innovation = _safe_float(state.get("ema_sq_innovation"), 0.0)
        self._ema_sq_innovation_pure = _safe_float(state.get("ema_sq_innovation_pure"), 0.0)

        # Robustness state
        self._innovation_bias_ema = _safe_float(state.get("innovation_bias_ema"), 0.0)
        self._step_count = int(state.get("step_count", 0))
        self._reset_count = int(state.get("reset_count", 0))

        # Time-based confidence state
        self._sat_active_time_s = _safe_float(state.get("sat_active_time_s"), 0.0)
        self._d_hat_active_time_s = _safe_float(state.get("d_hat_active_time_s"), 0.0)
        self._diverge_high_rmse_s = _safe_float(state.get("diverge_high_rmse_s"), 0.0)
        self._diverge_cooldown_s = _safe_float(state.get("diverge_cooldown_s"), 0.0)
        self._rmse_age_s = _safe_float(state.get("rmse_age_s"), 0.0)

        # Migration from old step-based counters
        if "sat_active_time_s" not in state and "sat_count" in state:
            self._sat_active_time_s = int(state["sat_count"]) * self.dt_s
        if "d_hat_active_time_s" not in state and "d_hat_active_steps" in state:
            self._d_hat_active_time_s = int(state["d_hat_active_steps"]) * self.dt_s
        if "diverge_high_rmse_s" not in state and "diverge_count" in state:
            dc = int(state["diverge_count"])
            if dc > 0:
                self._diverge_high_rmse_s = dc * self.dt_s
            elif dc < 0:
                self._diverge_cooldown_s = abs(dc) * self.dt_s

        # ---- Adaptive restoration based on downtime ----
        saved_at = _safe_float(state.get("saved_at"), 0.0)
        downtime_s = time.time() - saved_at if saved_at > 0.0 else float("inf")
        self._downtime_s = downtime_s
        self._needs_resync = False  # may be set below

        if downtime_s >= REBOOT_SHORT_S:
            # Level 2 (5 min – 1h) and Level 3 (> 1h):
            # The heating state during the downtime is unknown — fill u_buffer
            # with zeros (most conservative: assume heating was off).
            self.u_buffer = deque([(self._sim_time_s, 0.0)])
            # EMA RMSE state contains stale residuals.
            self._ema_sq_innovation = 0.0
            self._ema_sq_innovation_pure = 0.0
            self._rmse_age_s = 0.0
            # CUSUM accumulators based on old residuals are meaningless.
            self.cusum_pos = 0.0
            self.cusum_neg = 0.0
            # d_hat freshness: must be re-earned from scratch.
            self._d_hat_active_time_s = 0.0
            self._sat_active_time_s = 0.0
            # Proportional d_hat decay: the longer the stop, the more d_hat
            # represents stale external conditions (solar, occupants).
            # Using the same per-step relax rate as in the frozen-u path.
            if self.dt_s > 0:
                steps_off = downtime_s / self.dt_s
                decay = (1.0 - self.d_hat_relax) ** steps_off
                self.d_hat_ema *= decay

        if downtime_s > REBOOT_MEDIUM_S:
            # Level 3 only (> 1h):
            # Divergence state from a previous session is no longer relevant.
            self._diverge_high_rmse_s = 0.0
            self._diverge_cooldown_s = 0.0
            # T_hat may be significantly off — flag for resync at first step().
            self._needs_resync = True
            _LOGGER.info(
                "ThermalTwin v4.5: long downtime %.0f s detected, "
                "_needs_resync=True, d_hat_ema decayed to %.4f",
                downtime_s, self.d_hat_ema,
            )

    # ------------------------------------------------------------------
    # Convenience: update twin + compute ETA in one call
    # ------------------------------------------------------------------

    def update_with_eta(
        self,
        tin: float,
        text: float | None,
        target: float,
        on_percent: float,
        tau_reliable: bool,
        a: float,
        b: float,
        mode: str,
        deadtime_heat_s: float | None,
        deadtime_cool_s: float | None,
        deadtime_heat_reliable: bool,
        deadtime_cool_reliable: bool,
        dt_s: float | None = None,
    ) -> dict:
        """Update thermal twin and compute ETA best-case (diagnostics-only)."""
        if self.T_hat is None:
            if not tau_reliable:
                return {"status": "not_reliable"}
            self.reset(tin, text, u_init=on_percent)

        # v4.2: use correct deadtime based on power direction
        if on_percent > 0.01:
            deadtime_s = deadtime_heat_s or 0.0
        else:
            deadtime_s = deadtime_cool_s or 0.0
        twin_result = self.step(
            tin_meas=tin,
            text_meas=text,
            a=a, b=b,
            u_now=on_percent,
            deadtime_s=deadtime_s,
            sp=target,
            dt_s=dt_s,
            mode=mode,
        )

        cooling_active = mode == "cool" and a <= 0
        if tau_reliable:
            eta_result = eta_best_case(
                tin0=tin, text=text, target=target,
                a=a, b=b, mode=mode,
                deadtime_heat_s=deadtime_heat_s,
                deadtime_cool_s=deadtime_cool_s,
                deadtime_heat_ok=deadtime_heat_reliable,
                deadtime_cool_ok=deadtime_cool_reliable,
                last_text=self.last_text,
                d_hat_ema=self.d_hat_ema,
                cooling_active_available=cooling_active,
            )
        else:
            eta_result = {"eta_s": None, "reason": "not_reliable"}

        return {
            **twin_result,
            **{f"eta_{k}": v for k, v in eta_result.items()},
        }


# ---------------------------------------------------------------------------
# ETA best-case (standalone function)
# ---------------------------------------------------------------------------

EPS_DENOM = 1e-6
MAX_ETA_S = 48 * 3600


def eta_best_case(  # pylint: disable=too-many-arguments,too-many-return-statements
    tin0: float,
    text: float | None,
    target: float,
    a: float,
    b: float,
    mode: str,
    deadtime_heat_s: float | None,
    deadtime_cool_s: float | None,
    deadtime_heat_ok: bool,
    deadtime_cool_ok: bool,
    eps: float = 0.1,
    eps_denom: float = EPS_DENOM,
    max_eta_s: float = MAX_ETA_S,
    text_policy: str = "hold_last",
    last_text: float | None = None,
    d_hat_ema: float = 0.0,
    cooling_active_available: bool = False,
) -> dict:
    """Compute best-case ETA to reach *target* from *tin0*."""
    # v4.3: NaN guard on inputs
    if not isfinite(tin0) or not isfinite(target):
        return {"eta_s": None, "reason": "invalid_input"}

    if abs(tin0 - target) <= eps:
        return {"eta_s": 0.0, "reason": "already_reached"}

    text_used = _resolve_text(text, text_policy, last_text)
    if text_used is None:
        return {"eta_s": None, "reason": "missing_text"}

    if not isfinite(a) or not isfinite(b) or b <= 0:
        return {"eta_s": None, "reason": "invalid_params"}
    if mode == "heat" and a < 0:
        return {"eta_s": None, "reason": "invalid_params"}

    tau_min = 1.0 / b

    if mode == "heat":
        u = 1.0
        l_s = (deadtime_heat_s
               if (deadtime_heat_ok and deadtime_heat_s is not None)
               else 0.0)
    elif cooling_active_available and a < 0:
        u = 1.0
        l_s = (deadtime_cool_s
               if (deadtime_cool_ok and deadtime_cool_s is not None)
               else 0.0)
    else:
        # Passive cooling only
        u = 0.0
        l_s = 0.0

    t_inf = text_used + (a * u + d_hat_ema) / b

    denom = tin0 - t_inf
    if abs(denom) < eps_denom:
        return {"eta_s": None, "reason": "unreachable", "T_inf": t_inf}

    rho = (target - t_inf) / denom
    if rho <= 0 or rho >= 1:
        return {"eta_s": None, "reason": "unreachable",
                "T_inf": t_inf, "rho": rho}

    t_reach_min = (l_s / 60.0) - tau_min * log(rho)
    eta_s = max(0.0, t_reach_min * 60.0)

    if eta_s > max_eta_s:
        return {"eta_s": eta_s, "reason": "too_far", "T_inf": t_inf,
                "tau_min": tau_min, "L_s": l_s, "u": u, "rho": rho}

    return {"eta_s": eta_s, "reason": "ok", "T_inf": t_inf,
            "tau_min": tau_min, "L_s": l_s, "u": u, "rho": rho}
