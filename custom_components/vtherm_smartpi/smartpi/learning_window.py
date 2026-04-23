"""
Learning Window Manager for Smart-PI.

Manages multi-cycle learning window state and accumulation for the a/b estimator.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .timestamp_utils import convert_monotonic_to_wall_ts

from .const import (
    DELTA_MIN,
    DT_MAX_MIN,
    U_CV_MAX,
    U_CV_MIN_MEAN,
    U_OFF_MAX,
    U_ON_MIN,
    clamp,
)

if TYPE_CHECKING:
    from .ab_estimator import ABEstimator
    from .deadtime_estimator import DeadTimeEstimator
    from .governance import SmartPIGovernance

_LOGGER = logging.getLogger(__name__)


class LearningWindowManager:
    """
    Manages multi-cycle learning window state and accumulation.
    
    The learning window accumulates temperature and power data over multiple
    cycles to provide robust estimates for the a/b thermal model parameters.
    """

    def __init__(self, name: str):
        """Initialize the learning window manager.
        
        Args:
            name: Entity name for logging purposes.
        """
        self._name = name
        
        # Learning window state (multi-cycle learning)
        self._active: bool = False
        self._start_ts: float | None = None
        self._T_int_start: float = 0.0
        self._T_ext_start: float = 0.0
        self._u_int: float = 0.0
        self._t_int_s: float = 0.0
        self._u_first: float | None = None

        # Cumulative time spent in sliding-start state (across consecutive ticks)
        self._sliding_elapsed_s: float = 0.0

        # Power variance tracking (Welford online algorithm)
        self._u_count: int = 0
        self._u_mean: float = 0.0
        self._u_m2: float = 0.0      # Sum of squared differences from the mean

        # Learning start timestamp
        self._learning_start_date: Optional[datetime] = datetime.now()

        # Skip learning cycles after resume from interruption
        self._learning_resume_ts: Optional[float] = None

        # Track last published extending method to suppress duplicate messages
        self._last_extend_method: str = ""

    # --------------------------------------------------------------------------
    # Properties for diagnostic access
    # --------------------------------------------------------------------------
    
    @property
    def active(self) -> bool:
        """Return True if a learning window is currently active."""
        return self._active

    @property
    def start_ts(self) -> float | None:
        """Return the monotonic timestamp when the current window started."""
        return self._start_ts

    @property
    def T_int_start(self) -> float:
        """Return the indoor temperature at window start."""
        return self._T_int_start

    @property
    def T_ext_start(self) -> float:
        """Return the outdoor temperature at window start."""
        return self._T_ext_start

    @property
    def u_int(self) -> float:
        """Return the accumulated power integral (u * dt)."""
        return self._u_int

    @property
    def t_int_s(self) -> float:
        """Return the accumulated time integral in seconds."""
        return self._t_int_s

    @property
    def u_first(self) -> float | None:
        """Return the first power value in the window (for consistency check)."""
        return self._u_first

    @property
    def learning_start_date(self) -> Optional[datetime]:
        """Return the wall-clock date when learning started."""
        return self._learning_start_date

    @property
    def learning_resume_ts(self) -> Optional[float]:
        """Return the monotonic timestamp for learning resume cooldown."""
        return self._learning_resume_ts

    # --------------------------------------------------------------------------
    # State management methods
    # --------------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the multi-cycle learning window state."""
        self._active = False
        self._start_ts = None
        self._u_int = 0.0
        self._t_int_s = 0.0
        self._u_first = None
        self._u_count = 0
        self._u_mean = 0.0
        self._u_m2 = 0.0
        self._last_extend_method = ""
        self._sliding_elapsed_s = 0.0

    def reset_all(self) -> None:
        """Reset all learning window state including timestamps."""
        self.reset()
        self._T_int_start = 0.0
        self._T_ext_start = 0.0
        self._learning_start_date = datetime.now()
        self._learning_resume_ts = None

    def set_learning_resume_ts(self, ts: Optional[float]) -> None:
        """Set the learning resume timestamp for cooldown after interruption.
        
        Args:
            ts: Monotonic timestamp until which learning should be paused.
        """
        self._learning_resume_ts = ts

    # --------------------------------------------------------------------------
    # Power variance tracking (Welford online algorithm)
    # --------------------------------------------------------------------------

    def _update_u_stats(self, u: float) -> None:
        """Update running mean/variance of u using Welford's online algorithm."""
        self._u_count += 1
        delta = u - self._u_mean
        self._u_mean += delta / self._u_count
        delta2 = u - self._u_mean
        self._u_m2 += delta * delta2

    @property
    def _u_std(self) -> float:
        """Return current standard deviation of u in the window."""
        if self._u_count < 2:
            return 0.0
        return (self._u_m2 / (self._u_count - 1)) ** 0.5

    @property
    def _u_cv(self) -> float:
        """Return coefficient of variation of u in the window."""
        if self._u_mean < U_CV_MIN_MEAN:
            return 0.0  # Mean too low, CV not meaningful
        return self._u_std / self._u_mean

    # --------------------------------------------------------------------------
    # Persistence methods
    # --------------------------------------------------------------------------

    def load_state(self, state: dict) -> None:
        """Load state from persistence dict.
        
        Args:
            state: Dictionary containing persisted state.
        """
        if not state:
            return

        # Parse learning start date from ISO string
        start_date_str = state.get("learning_start_date")
        if start_date_str:
            try:
                self._learning_start_date = datetime.fromisoformat(start_date_str)
            except (ValueError, TypeError):
                self._learning_start_date = datetime.now()
        else:
            self._learning_start_date = datetime.now()
            
        # The learning_resume_ts must NOT survive a restart: _startup_grace_period in
        # prop_algo_smartpi handles the post-boot freeze for one cycle. Restoring a stale
        # value (e.g., 20-min OFF-resume) would block learning for the full duration.
        self._learning_resume_ts = None

    def save_state(self) -> dict:
        """Save state to persistence dict.
        
        Returns:
            Dictionary containing the current state.
        """
        return {
            "learning_start_date": (
                self._learning_start_date.isoformat() 
                if self._learning_start_date else None
            ),
            "learning_resume_ts": convert_monotonic_to_wall_ts(self._learning_resume_ts),
        }

    # --------------------------------------------------------------------------
    # Learning window update method
    # --------------------------------------------------------------------------

    def update(
        self,
        dt_min: float,
        current_temp: float,
        ext_temp: float,
        u_active: float,
        setpoint_changed: bool,
        estimator: "ABEstimator",
        dt_est: "DeadTimeEstimator",
        governance: "SmartPIGovernance",
        learning_resume_ts: float | None,
        now: float,
        in_deadband: bool,
        in_near_band: bool,
        t_heat_episode_start: float | None,
        t_cool_episode_start: float | None,
        ff3_active: bool = False,
        deadtime_skip_count_a: int = 0,
        deadtime_skip_count_b: int = 0,
        is_calibrating: bool = False,
        is_hysteresis: bool = False,
    ) -> tuple[int, int]:
        """
        Update learning window and submit to estimator if conditions met.
        
        Args:
            dt_min: Elapsed time in minutes since last update.
            current_temp: Current indoor temperature.
            ext_temp: Current outdoor temperature.
            u_active: Power applied during this interval (0..1).
            setpoint_changed: True if setpoint changed during this interval.
            estimator: The ABEstimator instance for learning submission.
            dt_est: The DeadTimeEstimator instance for deadtime checks.
            governance: The SmartPIGovernance instance for regime decisions.
            learning_resume_ts: Timestamp for learning resume cooldown.
            now: Current monotonic time.
            in_deadband: Whether the system is in deadband.
            in_near_band: Whether the system is in near-band.
            t_heat_episode_start: Monotonic timestamp of heating episode start.
            t_cool_episode_start: Monotonic timestamp of cooling episode start.
            ff3_active: True when FF3 was active for the current cycle.
            deadtime_skip_count_a: Counter for heating deadtime skips (returned).
            deadtime_skip_count_b: Counter for cooling deadtime skips (returned).
            
        Returns:
            Tuple of (deadtime_skip_count_a, deadtime_skip_count_b) for tracking.
        """
        # Import here to avoid circular imports at module level
        from .governance import GovernanceDecision
        from .ab_estimator import ABEstimator
        
        if dt_min <= 0:
            return deadtime_skip_count_a, deadtime_skip_count_b

        dt_s = dt_min * 60.0

        # Update learning resume timestamp if provided
        if learning_resume_ts is not None:
            self._learning_resume_ts = learning_resume_ts

        if ff3_active:
            estimator.learn_skip_count += 1
            estimator.learn_last_reason = "skip: ff3_active"
            if self._active:
                self.reset()
            return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Setpoint change ---
        # Active windows are allowed to continue:
        # - B (OFF): cooling is purely physical, independent of setpoint.
        # - A (ON):  power transition detection closes the window if u changes.
        # In both cases the power-transition guard (u_active ≠ u_first) is the
        # real safety net; a blind reset here would discard valid in-flight data.
        # If no window is active there is nothing to do either way.
        if setpoint_changed and self._active:
            _LOGGER.debug(
                "%s - setpoint changed, window continues (power transition guards)",
                self._name
            )

        # --- Governance gate (thermal domain: a/b learning) ---
        # During calibration, bypass governance to allow A/B learning
        # (the system traverses deadband/nearband at 100%/0%, governance
        # restrictions are not relevant).
        if not is_calibrating:
            gov_decision, gov_reason = governance.decide_update(
                'thermal', self._learning_resume_ts, now
            )
            if gov_decision in (GovernanceDecision.HARD_FREEZE, GovernanceDecision.FREEZE):
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = f"skip: governance ({gov_reason.value})"
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Bootstrap: require deadtime before A/B collection ---
        # During hysteresis phase, A collection is gated on heat deadtime availability,
        # and B collection is gated on cool deadtime availability.
        if is_hysteresis:
            if u_active > U_ON_MIN and not dt_est.deadtime_heat_reliable:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: bootstrap - heat deadtime not yet learned"
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b
            if u_active < U_OFF_MAX and not dt_est.deadtime_cool_reliable:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: bootstrap - cool deadtime not yet learned"
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Interruption / Resume Check ---
        if self._learning_resume_ts:
            if now < self._learning_resume_ts:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: resume cool-down"
                self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b
            else:
                self._learning_resume_ts = None

        # --- Validation: external temperature required ---
        if ext_temp is None:
            estimator.learn_skip_count += 1
            estimator.learn_last_reason = "skip: no external temp"
            self.reset()
            return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Dead Time Gating ---
        # If in deadband or near-band (Stable PI), ignore deadtime skipping
        # because we are "safely landed" and small fluctuations should be 
        # treated as normal learning/skipping rather than blocking.
        ignore_deadtime_skip = in_deadband or in_near_band

        # Heating deadtime check
        if (
            not ignore_deadtime_skip 
            and dt_est.deadtime_heat_reliable 
            and t_heat_episode_start is not None 
            and dt_est.deadtime_heat_s is not None
        ):
            elapsed_episode = now - t_heat_episode_start
            if elapsed_episode < dt_est.deadtime_heat_s:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: deadtime window"
                deadtime_skip_count_a += 1
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b

        # Cooling deadtime check
        if (
            not ignore_deadtime_skip 
            and dt_est.deadtime_cool_reliable 
            and t_cool_episode_start is not None 
            and dt_est.deadtime_cool_s is not None
        ):
            elapsed_episode = now - t_cool_episode_start
            if elapsed_episode < dt_est.deadtime_cool_s:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: deadtime window (cool)"
                deadtime_skip_count_b += 1
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Learning Window Accumulation ---
        early_submit = False
        if not self._active:
            # Before starting window, check if backdated start would be in deadtime.
            # If so, anchor the window start to the deadtime end rather than rejecting:
            # this avoids an infinite skip loop when dt_s < deadtime duration.
            proposed_start_ts = now - dt_s

            # Check heating deadtime overlap
            if (
                not ignore_deadtime_skip
                and dt_est.deadtime_heat_reliable
                and t_heat_episode_start is not None
                and dt_est.deadtime_heat_s is not None
            ):
                deadtime_end_ts = t_heat_episode_start + dt_est.deadtime_heat_s
                if proposed_start_ts < deadtime_end_ts:
                    proposed_start_ts = deadtime_end_ts

            # Check cooling deadtime overlap
            if (
                not ignore_deadtime_skip
                and dt_est.deadtime_cool_reliable
                and t_cool_episode_start is not None
                and dt_est.deadtime_cool_s is not None
            ):
                deadtime_end_ts = t_cool_episode_start + dt_est.deadtime_cool_s
                if proposed_start_ts < deadtime_end_ts:
                    proposed_start_ts = deadtime_end_ts

            # If the anchored start is still in the future, the deadtime has not
            # expired yet — emit a single skip and wait.
            if proposed_start_ts >= now:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: window start still in deadtime"
                return deadtime_skip_count_a, deadtime_skip_count_b

            # OK to start window (possibly with anchored start)
            self._active = True
            self._start_ts = proposed_start_ts
            self._T_int_start = current_temp
            self._T_ext_start = ext_temp
            self._u_int = 0.0
            self._t_int_s = 0.0
            self._u_first = u_active
            self._sliding_elapsed_s = 0.0
            # Initialize power variance tracking and record first sample
            self._u_count = 0
            self._u_mean = 0.0
            self._u_m2 = 0.0
            self._update_u_stats(u_active)
        else:
            # Check power consistency via coefficient of variation (Welford)
            cv = self._u_cv
            if cv > U_CV_MAX:
                # Regime change detected: window must close now.
                # Try OLS slope on collected samples before deciding to submit or abandon.
                u_eff_win = self._u_int / max(self._t_int_s, 1e-9)
                trim_frac = 0.10 if u_eff_win < U_OFF_MAX else 0.0
                early_samples = [p for p in dt_est.tin_history if p[0] >= self._start_ts]
                slope_early, method_early, _ = ABEstimator.robust_dTdt_per_min(
                    early_samples, trim_start_frac=trim_frac
                )
                delta_T_early = abs(self._T_int_start - self._T_ext_start)
                if slope_early is not None and delta_T_early >= DELTA_MIN:
                    _LOGGER.debug(
                        "%s - power CV %.2f > %.2f: early submit via slope (%.0fs)",
                        self._name, cv, U_CV_MAX, self._t_int_s,
                    )
                    early_submit = True
                else:
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        f"skip: power instability (CV={cv:.2f}, {method_early})"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b
            else:
                early_submit = False

        if early_submit:
            # Use already-accumulated data (do not add the transition tick)
            window_dt_min = self._t_int_s / 60.0
            dT = current_temp - self._T_int_start
            abs_dT = abs(dT)
            delta_T = self._T_int_start - self._T_ext_start
        else:
            # Accumulate current sample into power stats and energy integral
            self._update_u_stats(u_active)
            self._u_int += clamp(u_active, 0.0, 1.0) * dt_s
            self._t_int_s += dt_s

            # Current Window Stats
            window_dt_min = self._t_int_s / 60.0

            dT = current_temp - self._T_int_start
            abs_dT = abs(dT)
            delta_T = self._T_int_start - self._T_ext_start

            # Calculate preliminary u_eff for direction check
            if self._t_int_s > 0.0:
                u_eff_pre = self._u_int / self._t_int_s
            else:
                u_eff_pre = 0.0

            # --- Extension Checks ---
            if abs(delta_T) < DELTA_MIN:
                self.reset()
                estimator.learn_last_reason = "skip: delta too small"
                return deadtime_skip_count_a, deadtime_skip_count_b

            # --- Sliding start: discard data while slope direction is wrong ---
            # B (OFF): temperature still rising after heater off (thermal flywheel) → dT > 0
            # A (ON):  temperature still falling after heater on  (heat deadtime)   → dT < 0
            # Slide the window anchor forward instead of resetting: this keeps the window
            # open while ensuring tin_history[start_ts:] only captures post-anomaly data.
            b_wrong_dir = (u_eff_pre < U_OFF_MAX and dT > 0)
            a_wrong_dir = (u_eff_pre > U_ON_MIN  and dT < 0)
            if b_wrong_dir or a_wrong_dir:
                # Accumulate sliding duration cumulatively across consecutive ticks.
                # Using a per-tick window_dt_min would reset the timer every slide,
                # making DT_MAX_MIN unreachable when dt_s is small.
                self._sliding_elapsed_s += dt_s
                if self._sliding_elapsed_s < DT_MAX_MIN * 60:
                    self._start_ts = now
                    self._T_int_start = current_temp
                    self._T_ext_start = ext_temp
                    self._t_int_s = 0.0
                    self._u_int = 0.0
                    label = f"B flywheel (+{dT:.2f}°C)" if b_wrong_dir else f"A deadtime ({dT:.2f}°C)"
                    estimator.learn_last_reason = f"skip: {label}, sliding start"
                    return deadtime_skip_count_a, deadtime_skip_count_b
                else:
                    # Cumulative DT_MAX_MIN reached with slope still wrong → abandon
                    self.reset()
                    estimator.learn_last_reason = (
                        "skip: B flywheel timeout" if b_wrong_dir else "skip: A deadtime timeout"
                    )
                    return deadtime_skip_count_a, deadtime_skip_count_b

            # Try slope quality: submit if robust, extend if not, timeout if limit reached.
            # robust_dTdt_per_min enforces its own internal guards (jumps, amplitude, t-test).
            trim_frac = 0.10 if u_eff_pre < U_OFF_MAX else 0.0
            relevant_samples = [p for p in dt_est.tin_history if p[0] >= self._start_ts]
            slope_val, method, n_samples = ABEstimator.robust_dTdt_per_min(
                relevant_samples, trim_start_frac=trim_frac
            )

            if slope_val is not None:
                # Slope is robust: proceed to submission.
                pass
            elif window_dt_min < DT_MAX_MIN:
                # Signal not yet robust: extend window.
                # Only publish on method transition to keep rejection reasons visible.
                if method != self._last_extend_method:
                    estimator.learn_last_reason = (
                        f"extending: slope not robust ({method}, n={n_samples})"
                    )
                    self._last_extend_method = method
                return deadtime_skip_count_a, deadtime_skip_count_b
            else:
                # Absolute timeout reached with no valid slope: abandon.
                self.reset()
                estimator.learn_last_reason = f"skip: window timeout ({method})"
                return deadtime_skip_count_a, deadtime_skip_count_b

        if self._t_int_s <= 0.0:
            self.reset()
            estimator.learn_last_reason = "skip: window duty invalid"
            return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Learning Submission ---
        u_eff = self._u_int / self._t_int_s
        dT_dt = dT / window_dt_min

        if u_eff < U_OFF_MAX:
            # OFF Learning
            relevant_samples = [
                p for p in dt_est.tin_history 
                if p[0] >= self._start_ts
            ]

            slope_val, method, _ = ABEstimator.robust_dTdt_per_min(
                relevant_samples,
                trim_start_frac=0.10,
                # trim_end omitted: the useful cooling signal arrives late in the
                # window. Power-transition detection already closes the window before
                # any heater-ON data can contaminate the tail.
            )

            if slope_val is not None:
                final_slope = slope_val
                estimator.diag_dTdt_method = method
            else:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = f"skip: OFF slope not robust ({method})"
                self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b

            estimator.learn(
                dT_int_per_min=final_slope,
                u=0.0,
                t_int=self._T_int_start,
                t_ext=self._T_ext_start,
            )
        elif u_eff > U_ON_MIN:
            # ON phase
            relevant_samples = [
                p for p in dt_est.tin_history 
                if p[0] >= self._start_ts
            ]

            slope_val, method, _ = ABEstimator.robust_dTdt_per_min(relevant_samples)
            if slope_val is not None:
                final_slope = slope_val
                estimator.diag_dTdt_method = method
            else:
                final_slope = dT_dt
                estimator.diag_dTdt_method = "fallback_simple"

            estimator.learn(
                dT_int_per_min=final_slope,
                u=u_eff,
                t_int=self._T_int_start,
                t_ext=self._T_ext_start,
            )
        else:
            estimator.learn_last_reason = "skip: low excitation (u mid)"

        self.reset()
        return deadtime_skip_count_a, deadtime_skip_count_b
