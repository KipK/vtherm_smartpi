"""
Learning Window Manager for Smart-PI.

Manages multi-cycle learning window state and accumulation for the a/b estimator.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .const import (
    AB_SETTLE_ACTIVE_CAP_FRAC,
    AB_SETTLE_K_PASSIVE_DEADTIME,
    AB_B_MIN_PASSIVE_GRADIENT_C,
    AB_B_OUTDOOR_TARGET_MARGIN_C,
    DELTA_MIN,
    DT_MAX_MIN,
    RESPONSE_LAG_MODEL_CHANGE_REL,
    RESPONSE_LAG_U_TOL,
    U_CV_MAX,
    U_CV_MIN_MEAN,
    U_OFF_MAX,
    U_ON_MIN,
    clamp,
)
from .ab_window_balance import ABWindowBalance
from .learning_outcomes import LearningOutcomes
from .response_lag import ResponseLagEstimator

# Two observed intervals are contiguous within this tolerance (s).
_CONTIGUITY_TOL_S = 1.0

if TYPE_CHECKING:
    from .ab_estimator import ABEstimator
    from .deadtime_estimator import DeadTimeEstimator
    from .governance import SmartPIGovernance
    from ..hvac_mode import VThermHvacMode

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
        # Delivered power integral (applied power filtered by the response lag)
        self._uf_int: float = 0.0
        self._t_int_s: float = 0.0
        self._u_first: float | None = None

        # Time spent sliding the window start, summed over the window life:
        # ticks in the expected direction do not clear it.
        self._sliding_elapsed_s: float = 0.0
        # End of the observed data of a window interrupted by an observation
        # gap: the window closes on its data up to this time.
        self._close_at: float | None = None

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

        # Integral balance pools fed by windows closed without a robust slope.
        # A window reset during update() closes its segment into the pools;
        # an external reset discards it.
        self._balance = ABWindowBalance(name)
        self._updating: bool = False
        # A window timeout only bounds the OLS slope: the balance segment
        # carries over into the next window until a gate closes it.
        self._balance_carry: bool = False
        # Sliding-period counts of window outcomes for diagnostics.
        self._outcomes = LearningOutcomes()
        # Response lag after power steps: delivered power and settling gates.
        self._response_lag = ResponseLagEstimator(name)
        # Lag of the power model of the collected a measurements.
        self._lag_model: float | None = None
        # End of the last observed interval (contiguity of the observations).
        self._last_update_now: float | None = None

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
    def balance(self) -> ABWindowBalance:
        """Return the integral balance pools (diagnostics)."""
        return self._balance

    @property
    def response_lag(self) -> ResponseLagEstimator:
        """Return the response lag estimator (diagnostics)."""
        return self._response_lag

    def outcomes_summary(self, now: float) -> dict[str, list[list]]:
        """Return the most frequent window outcomes of the sliding period."""
        return self._outcomes.summary(now)

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
        self._uf_int = 0.0
        self._t_int_s = 0.0
        self._u_first = None
        self._u_count = 0
        self._u_mean = 0.0
        self._u_m2 = 0.0
        self._last_extend_method = ""
        self._sliding_elapsed_s = 0.0
        self._close_at = None
        if not self._updating:
            self._balance.discard()

    def reset_all(self) -> None:
        """Reset all learning window state including timestamps."""
        self.reset()
        self._balance.clear()
        self._outcomes.clear()
        self._response_lag.clear()
        self._lag_model = None
        self._last_update_now = None
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
        """Return coefficient of variation of u in the window.

        It is 0 while the window mean power is below U_CV_MIN_MEAN, so power
        returning in an OFF window is not seen until the mean reaches it.
        """
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
        self._response_lag.load_state(state.get("response_lag"))
        self._lag_model = self._response_lag.lag_s

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
            "response_lag": self._response_lag.save_state(),
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
        estimator: "ABEstimator",
        dt_est: "DeadTimeEstimator",
        governance: "SmartPIGovernance",
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
        hvac_mode: "VThermHvacMode | None" = None,
        target_temp: float | None = None,
        a_learning_allowed: bool = True,
        a_learning_block_reason: str | None = None,
        raw_temp: float | None = None,
        min_active_window_min: float = 0.0,
    ) -> tuple[int, int]:
        """Update the learning window, then pool a window closed by this update."""
        if dt_min > 0:
            self._close_window_at_gap(now, dt_min * 60.0)
            self._last_update_now = now
            self._response_lag.observe(
                now=now,
                dt_s=dt_min * 60.0,
                u=clamp(u_active, 0.0, 1.0),
                t_ext=ext_temp,
                hvac_mode=hvac_mode,
                b=_published_b(estimator),
                tin_history=dt_est.tin_history,
                deadtime_s=_active_deadtime_s(dt_est, hvac_mode),
            )
            self._follow_power_model(
                estimator, _active_deadtime_s(dt_est, hvac_mode), hvac_mode
            )
        estimator.a_model_ready = self._response_lag.resolved
        publish = getattr(estimator, "publish_collected_a", None)
        if callable(publish):
            from ..hvac_mode import VThermHvacMode_COOL

            publish(-1.0 if hvac_mode == VThermHvacMode_COOL else 1.0)
        was_active = self._active
        window_param = (
            "b" if self._u_first is not None and self._u_first < U_OFF_MAX else "a"
        )
        self._updating = True
        try:
            result = self._update_window(
                dt_min,
                current_temp,
                ext_temp,
                u_active,
                estimator,
                dt_est,
                governance,
                now,
                in_deadband,
                in_near_band,
                t_heat_episode_start,
                t_cool_episode_start,
                ff3_active=ff3_active,
                deadtime_skip_count_a=deadtime_skip_count_a,
                deadtime_skip_count_b=deadtime_skip_count_b,
                is_calibrating=is_calibrating,
                is_hysteresis=is_hysteresis,
                hvac_mode=hvac_mode,
                target_temp=target_temp,
                a_learning_allowed=a_learning_allowed,
                a_learning_block_reason=a_learning_block_reason,
                raw_temp=raw_temp,
                min_active_window_min=self._bootstrap_active_window_min(
                    min_active_window_min, is_hysteresis
                ),
            )
        finally:
            self._updating = False
        carry, self._balance_carry = self._balance_carry, False
        if was_active and not self._active:
            self._outcomes.record(now, window_param, estimator.learn_last_reason)
        if self._balance.is_open and not self._active and not carry:
            pooled = self._balance.close(
                now=now,
                hvac_mode=hvac_mode,
                target_temp=target_temp,
                estimator=estimator,
            )
            if pooled is not None:
                param, submitted = pooled
                self._outcomes.record(
                    now,
                    param,
                    f"pool: {estimator.learn_last_reason}"
                    if submitted
                    else "pool: segment added",
                )
        return result

    def _bootstrap_active_window_min(self, min_active_window_min: float, is_hysteresis: bool) -> float:
        """Minimum active window duration (min), capped by the bootstrap heating phases.

        In the bootstrap the heater runs at full power between the hysteresis
        thresholds, so a heating phase may be shorter than one PWM cycle. The
        minimum then falls to half the median heating phase, measured on the
        previous phases (fixed before the data of the window, as the cycle).
        """
        if not is_hysteresis:
            return min_active_window_min
        episode_s = self._response_lag.episode_median_s
        if episode_s is None:
            return min_active_window_min
        return min(min_active_window_min, AB_SETTLE_ACTIVE_CAP_FRAC * episode_s / 60.0)

    def _close_window_at_gap(self, now: float, dt_s: float) -> None:
        """Close a window at its last observation when the intervals are not contiguous.

        calculate() does not feed the learning during a heating guard cut and
        an interval is capped at one cycle: across such a gap the power and the
        temperature are unobserved. The window is submitted on its data up to
        the last observation when its slope is robust there (as on a power
        change), else abandoned; it never extends across the gap.
        """
        if (
            self._last_update_now is None
            or now - dt_s <= self._last_update_now + _CONTIGUITY_TOL_S
        ):
            return
        if self._active:
            self._close_at = self._last_update_now
            return
        # A balance segment carried over a window timeout spans the gap.
        self._balance_carry = False
        self._balance.discard()

    def _follow_power_model(
        self,
        estimator: "ABEstimator",
        deadtime_s: float | None,
        hvac_mode: "VThermHvacMode | None",
    ) -> None:
        """Recompute the a collection when the bootstrap power model changes."""
        lag = self._response_lag.lag_s
        previous = self._lag_model
        changed = (previous is None) != (lag is None) or (
            previous is not None
            and lag is not None
            and abs(lag - previous) > RESPONSE_LAG_MODEL_CHANGE_REL * previous
        )
        if not changed:
            return
        self._lag_model = lag
        replay = getattr(estimator, "replay_a_power", None)
        if not callable(replay):
            return
        from ..hvac_mode import VThermHvacMode_COOL

        settle = self._response_lag.active_settle_s

        def power_of(geometry: tuple[float, float, float]) -> float | None:
            # Only windows that the settling gate would have admitted are
            # recomputed: their correction is small whatever the shape of the
            # response, unlike the one of a window within the transient.
            u_applied, start_s, duration_s = geometry
            if settle is not None and start_s < settle:
                return None
            return u_applied * self._response_lag.delivered_fraction(
                start_s, duration_s, deadtime_s
            )

        mode_sign = -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0
        if replay(power_of, mode_sign, "replay: response lag model changed") is True:
            _LOGGER.debug(
                "%s - a collection recomputed: response lag %s -> %s",
                self._name, previous, lag,
            )

    def _power_geometry(self, u_applied: float) -> tuple[float, float, float] | None:
        """Window power geometry within a constant-power episode, or None."""
        ep_start = self._response_lag.episode_start
        ep_power = self._response_lag.episode_power
        if (
            ep_start is None
            or ep_power is None
            or self._start_ts is None
            or self._start_ts < ep_start
            or abs(u_applied - ep_power) > RESPONSE_LAG_U_TOL
        ):
            return None
        return (u_applied, self._start_ts - ep_start, self._t_int_s)

    def _update_window(
        self,
        dt_min: float,
        current_temp: float,
        ext_temp: float,
        u_active: float,
        estimator: "ABEstimator",
        dt_est: "DeadTimeEstimator",
        governance: "SmartPIGovernance",
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
        hvac_mode: "VThermHvacMode | None" = None,
        target_temp: float | None = None,
        a_learning_allowed: bool = True,
        a_learning_block_reason: str | None = None,
        raw_temp: float | None = None,
        min_active_window_min: float = 0.0,
    ) -> tuple[int, int]:
        """
        Update learning window and submit to estimator if conditions met.
        
        Args:
            dt_min: Elapsed time in minutes since last update.
            current_temp: Current indoor temperature.
            ext_temp: Current outdoor temperature.
            u_active: Power applied during this interval (0..1).
            estimator: The ABEstimator instance for learning submission.
            dt_est: The DeadTimeEstimator instance for deadtime checks.
            governance: The SmartPIGovernance instance for regime decisions.
            now: Current monotonic time.
            in_deadband: Whether the system is in deadband.
            in_near_band: Whether the system is in near-band.
            t_heat_episode_start: Monotonic timestamp of heating episode start.
            t_cool_episode_start: Monotonic timestamp of cooling episode start.
            ff3_active: True when FF3 was active for the current cycle.
            deadtime_skip_count_a: Counter for heating deadtime skips (returned).
            deadtime_skip_count_b: Counter for cooling deadtime skips (returned).
            hvac_mode: Current HVAC mode.
            target_temp: Current target temperature.
            a_learning_allowed: Whether ON/A learning is eligible.
            a_learning_block_reason: Diagnostic reason when ON/A learning is blocked.
            raw_temp: Raw measured indoor temperature for the integral balance
                (defaults to current_temp).
            
        Returns:
            Tuple of (deadtime_skip_count_a, deadtime_skip_count_b) for tracking.
        """
        # Import here to avoid circular imports at module level
        from .governance import GovernanceDecision
        from .ab_estimator import ABEstimator
        from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
        
        if dt_min <= 0:
            return deadtime_skip_count_a, deadtime_skip_count_b

        dt_s = dt_min * 60.0

        if ff3_active:
            estimator.learn_skip_count += 1
            estimator.learn_last_reason = "skip: ff3_active"
            if self._active:
                self.reset()
            return deadtime_skip_count_a, deadtime_skip_count_b

        # Setpoint changes do not reset an active window by themselves: the
        # ON/A gate below closes a window that started ON (u_first > U_ON_MIN)
        # on the observation where the setpoint changes.
        # A power change closes a window through the power CV check (which
        # acts only once the window mean power reaches U_CV_MIN_MEAN) or
        # through a governance regime transition within the cycle.

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

        # This gate only protects ON/A learning from contaminated setpoint
        # transients. OFF/B learning must continue through the normal guards.
        blocked_a_window = (
            self._active and self._u_first is not None and self._u_first > U_ON_MIN
        )
        blocked_a_start = not self._active and u_active > U_ON_MIN
        if (
            not is_calibrating
            and not a_learning_allowed
            and (blocked_a_window or blocked_a_start)
        ):
            estimator.learn_skip_count += 1
            estimator.learn_last_reason = (
                a_learning_block_reason or "skip: setpoint transient"
            )
            self.reset()
            return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Bootstrap: require deadtime before A/B collection ---
        # During hysteresis, A uses the active response dead time and B uses
        # the passive response dead time. The mapping is reversed in HVAC COOL.
        if is_hysteresis:
            active_deadtime_reliable = (
                dt_est.deadtime_cool_reliable
                if hvac_mode == VThermHvacMode_COOL
                else dt_est.deadtime_heat_reliable
            )
            passive_deadtime_reliable = (
                dt_est.deadtime_heat_reliable
                if hvac_mode == VThermHvacMode_COOL
                else dt_est.deadtime_cool_reliable
            )
            if u_active > U_ON_MIN and not active_deadtime_reliable:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: bootstrap - active deadtime not yet learned"
                if self._active:
                    self.reset()
                return deadtime_skip_count_a, deadtime_skip_count_b
            if u_active < U_OFF_MAX and not passive_deadtime_reliable:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = "skip: bootstrap - passive deadtime not yet learned"
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

            # The slope settles after a power step: the window starts once the
            # response transient has decayed (gate of the published lag).
            settle_end = None
            settle_param = ""
            if u_active > U_ON_MIN:
                settle_end, settle_param = self._response_lag.active_gate_end(), "a"
            elif u_active < U_OFF_MAX:
                # The emitter keeps heating after a cut: the passive dead time
                # gives a floor of this residual power decay, used with or
                # without a published lag. The bootstrap keeps its own guards
                # and calibration is exempt.
                min_settle_s = (
                    None
                    if is_hysteresis or is_calibrating
                    else _passive_settle_s(dt_est, hvac_mode)
                )
                settle_end, settle_param = (
                    self._response_lag.passive_gate_end(min_settle_s),
                    "b",
                )
            in_transient = settle_end is not None and settle_end >= now
            if settle_end is not None and proposed_start_ts < settle_end:
                proposed_start_ts = settle_end

            # If the anchored start is still in the future, the deadtime or the
            # response transient has not expired yet — emit a single skip and wait.
            if proposed_start_ts >= now:
                estimator.learn_skip_count += 1
                estimator.learn_last_reason = (
                    f"skip: response transient ({settle_param})"
                    if in_transient
                    else "skip: window start still in deadtime"
                )
                return deadtime_skip_count_a, deadtime_skip_count_b

            # OK to start window (possibly with anchored start)
            self._active = True
            self._start_ts = proposed_start_ts
            self._T_int_start = current_temp
            self._T_ext_start = ext_temp
            self._u_int = 0.0
            self._uf_int = 0.0
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
            close_at = self._close_at
            if close_at is not None or cv > U_CV_MAX:
                # Regime change or observation gap: window must close now.
                # Try OLS slope on collected samples before deciding to submit or abandon.
                u_eff_win = self._u_int / max(self._t_int_s, 1e-9)
                trim_frac = 0.10 if u_eff_win < U_OFF_MAX else 0.0
                early_samples = [
                    p for p in dt_est.tin_history
                    if p[0] >= self._start_ts and (close_at is None or p[0] <= close_at)
                ]
                slope_early, method_early, _ = ABEstimator.robust_dTdt_per_min(
                    early_samples, trim_start_frac=trim_frac
                )
                delta_T_early = abs(self._T_int_start - self._T_ext_start)
                # The duration of an active window is set before its data: a
                # stop as soon as a short window looks significant favours
                # the steepest noisy slopes (optional stopping).
                if (
                    slope_early is not None
                    and u_eff_win > U_ON_MIN
                    and self._t_int_s < min_active_window_min * 60.0
                ):
                    slope_early, method_early = None, "active_window_shorter_than_cycle"
                if slope_early is not None and delta_T_early >= DELTA_MIN:
                    _LOGGER.debug(
                        "%s - power CV %.2f > %.2f: early submit via slope (%.0fs)",
                        self._name, cv, U_CV_MAX, self._t_int_s,
                    )
                    early_submit = True
                    slope_val, method = slope_early, method_early
                else:
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        f"skip: observation gap ({method_early})"
                        if close_at is not None
                        else f"skip: power instability (CV={cv:.2f}, {method_early})"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b
            else:
                early_submit = False

        # An early submit uses the already-accumulated data and does not add the
        # transition tick.
        if not early_submit:
            # Accumulate current sample into power stats and energy integral
            self._update_u_stats(u_active)
            self._u_int += clamp(u_active, 0.0, 1.0) * dt_s
            delivered = clamp(self._response_lag.interval_mean_power, 0.0, 1.0)
            self._uf_int += delivered * dt_s
            # Only a is referred to the delivered power: passive windows keep
            # the applied power so that their classification is unchanged.
            pool_u = (
                delivered
                if self._u_first is not None and self._u_first > U_ON_MIN
                else clamp(u_active, 0.0, 1.0)
            )
            self._t_int_s += dt_s

            # Current Window Stats
            window_dt_min = self._t_int_s / 60.0

            dT = current_temp - self._T_int_start
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
            # Work in an oriented temperature space where positive means that
            # active HVAC power moves the room in the requested direction.
            oriented_dT = -dT if hvac_mode == VThermHvacMode_COOL else dT

            # B (OFF): temperature still follows the previous active response.
            # A (ON): temperature still follows the passive response after start.
            # Slide the window anchor forward instead of resetting: this keeps the window
            # open while ensuring tin_history[start_ts:] only captures post-anomaly data.
            b_wrong_dir = (u_eff_pre < U_OFF_MAX and oriented_dT > 0)
            a_wrong_dir = (u_eff_pre > U_ON_MIN and oriented_dT < 0)
            if b_wrong_dir or a_wrong_dir:
                # Accumulate the sliding duration over the window life (ticks
                # in the expected direction do not clear it). Using a per-tick
                # window_dt_min would reset the timer every slide, making
                # DT_MAX_MIN unreachable when dt_s is small.
                self._sliding_elapsed_s += dt_s
                if self._sliding_elapsed_s < DT_MAX_MIN * 60:
                    self._balance.discard()
                    self._start_ts = now
                    self._T_int_start = current_temp
                    self._T_ext_start = ext_temp
                    self._t_int_s = 0.0
                    self._u_int = 0.0
                    self._uf_int = 0.0
                    label = (
                        f"B flywheel ({dT:+.2f}°C)"
                        if b_wrong_dir
                        else f"A deadtime ({dT:+.2f}°C)"
                    )
                    estimator.learn_last_reason = f"skip: {label}, sliding start"
                    return deadtime_skip_count_a, deadtime_skip_count_b
                else:
                    # Cumulative DT_MAX_MIN reached with slope still wrong → abandon
                    self.reset()
                    estimator.learn_last_reason = (
                        "skip: B flywheel timeout" if b_wrong_dir else "skip: A deadtime timeout"
                    )
                    return deadtime_skip_count_a, deadtime_skip_count_b

            self._balance.observe(
                now,
                dt_s,
                pool_u,
                current_temp if raw_temp is None else raw_temp,
                ext_temp,
            )

            # Try slope quality: submit if robust, extend if not, timeout if limit reached.
            # robust_dTdt_per_min enforces its own internal guards (jumps, amplitude, t-test).
            trim_frac = 0.10 if u_eff_pre < U_OFF_MAX else 0.0
            relevant_samples = [p for p in dt_est.tin_history if p[0] >= self._start_ts]
            slope_val, method, n_samples = ABEstimator.robust_dTdt_per_min(
                relevant_samples, trim_start_frac=trim_frac
            )
            if (
                slope_val is not None
                and u_eff_pre > U_ON_MIN
                and window_dt_min < min_active_window_min
            ):
                slope_val, method = None, "active_window_shorter_than_cycle"
            if slope_val is not None:
                # Slope is robust: proceed to submission with this slope.
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
                self._balance_carry = True
                self.reset()
                estimator.learn_last_reason = f"skip: window timeout ({method})"
                return deadtime_skip_count_a, deadtime_skip_count_b

        if self._t_int_s <= 0.0:
            self.reset()
            estimator.learn_last_reason = "skip: window duty invalid"
            return deadtime_skip_count_a, deadtime_skip_count_b

        # --- Learning Submission ---
        u_eff = self._u_int / self._t_int_s

        # A window can start as passive and become active after power changes.
        # Recheck the effective duty before submission so such a mixed window
        # cannot bypass the transient gate.
        if (
            not is_calibrating
            and not a_learning_allowed
            and u_eff > U_ON_MIN
        ):
            estimator.learn_skip_count += 1
            estimator.learn_last_reason = (
                a_learning_block_reason or "skip: setpoint transient"
            )
            self.reset()
            return deadtime_skip_count_a, deadtime_skip_count_b

        if u_eff < U_OFF_MAX:
            # OFF Learning
            if hvac_mode == VThermHvacMode_HEAT:
                loss_gradient = self._T_int_start - self._T_ext_start
                if loss_gradient < AB_B_MIN_PASSIVE_GRADIENT_C:
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        "skip: b heat mode - insufficient heat-loss gradient"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b

                if (
                    target_temp is not None
                    and self._T_ext_start
                    > target_temp - AB_B_OUTDOOR_TARGET_MARGIN_C
                ):
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        "skip: b heat mode - outdoor too close to target"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b
            elif hvac_mode == VThermHvacMode_COOL:
                gain_gradient = self._T_ext_start - self._T_int_start
                if gain_gradient < AB_B_MIN_PASSIVE_GRADIENT_C:
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        "skip: b cool mode - insufficient heat-gain gradient"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b

                if (
                    target_temp is not None
                    and self._T_ext_start
                    < target_temp + AB_B_OUTDOOR_TARGET_MARGIN_C
                ):
                    estimator.learn_skip_count += 1
                    estimator.learn_last_reason = (
                        "skip: b cool mode - outdoor too close to target"
                    )
                    self.reset()
                    return deadtime_skip_count_a, deadtime_skip_count_b

            self._balance.discard()
            estimator.diag_dTdt_method = method
            estimator.learn(
                dT_int_per_min=slope_val,
                u=0.0,
                t_int=self._T_int_start,
                t_ext=self._T_ext_start,
                hvac_mode=(
                    VThermHvacMode_COOL
                    if hvac_mode == VThermHvacMode_COOL
                    else VThermHvacMode_HEAT
                ),
            )
        elif u_eff > U_ON_MIN:
            # ON phase: a is referred to the delivered power, which equals the
            # applied power while the response lag is unknown.
            self._balance.discard()
            estimator.diag_dTdt_method = method
            estimator.learn(
                dT_int_per_min=slope_val,
                u=self._uf_int / self._t_int_s,
                power_geometry=self._power_geometry(u_eff),
                t_int=self._T_int_start,
                t_ext=self._T_ext_start,
                hvac_mode=(
                    VThermHvacMode_COOL
                    if hvac_mode == VThermHvacMode_COOL
                    else VThermHvacMode_HEAT
                ),
            )
        else:
            estimator.learn_last_reason = "skip: low excitation (u mid)"

        self.reset()
        return deadtime_skip_count_a, deadtime_skip_count_b


def _published_b(estimator: "ABEstimator") -> float | None:
    """Return the published b, or None before its first publication."""
    # Attributes are read defensively: tests drive the window with mocks.
    count = getattr(estimator, "learn_ok_count_b", 0)
    b = getattr(estimator, "b", None)
    if isinstance(count, int) and count > 0 and isinstance(b, (int, float)):
        return float(b)
    return None


def _passive_settle_s(
    dt_est: "DeadTimeEstimator", hvac_mode: "VThermHvacMode | None"
) -> float | None:
    """Return the passive settling floor, or None.

    It is AB_SETTLE_K_PASSIVE_DEADTIME times the reliable dead time of the
    passive response (fall in HEAT, rise in COOL).
    """
    from ..hvac_mode import VThermHvacMode_COOL

    if hvac_mode == VThermHvacMode_COOL:
        value, reliable = dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable
    else:
        value, reliable = dt_est.deadtime_cool_s, dt_est.deadtime_cool_reliable
    if reliable is True and isinstance(value, (int, float)) and value > 0.0:
        return AB_SETTLE_K_PASSIVE_DEADTIME * float(value)
    return None


def _active_deadtime_s(
    dt_est: "DeadTimeEstimator", hvac_mode: "VThermHvacMode | None"
) -> float | None:
    """Return the reliable dead time of the active response, or None."""
    from ..hvac_mode import VThermHvacMode_COOL

    if hvac_mode == VThermHvacMode_COOL:
        value, reliable = dt_est.deadtime_cool_s, dt_est.deadtime_cool_reliable
    else:
        value, reliable = dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable
    if reliable is True and isinstance(value, (int, float)):
        return float(value)
    return None
