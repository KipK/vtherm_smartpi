########################################################################
#                                                                      #
#                      Smart-PI Algorithm                              #
#                      ------------------                              #
#  Auto-adaptive PI controller for Versatile Thermostat (VTH).         #
#                                                                      #
########################################################################

"""
SmartPI Algorithm (v2) - Auto-adaptive PI controller for Versatile Thermostat.

This module implements a duty-cycle PI controller for slow mono-mode thermal
systems (heating or cooling), with online identification of a first-order
signed actuation/loss model plus dead time.

Key features (v2)
-----------------
1. Hybrid Learning Phases:
   - Hysteresis: Initial ON/OFF control to generate strong signal for learning A/B.
   - Stable: Adaptive PI control once model is reliable (31+ samples).

2. Measurement & Modeling:
   - Online learning of signed actuation efficacy (a) and passive exchange (b)
     via robust Median + MAD estimation.
   - Automatic Dead Time (L) estimation using Takeoff (Primary) and SK (Backup).

3. Adaptive Control:
   - Feed-forward compensation based on outdoor temperature.
   - PI gains automatically tuned based on inertia (Tau) and Dead Time (IMC-like).
   - Auto-adaptive Near-Band: Reduces gains near setpoint to prevent overshoot,
     sized dynamically based on Dead Time.

4. Comfort & Protections:
   - Setpoint Boost: Faster reaction to manual setpoint increases (>0.3°C).
   - Integral hold during the dead time after a window or power-shedding resume.
   - Anticipation: The P and I errors read the measured temperature plus its expected travel over the dead time.
   - Anti-windup: Conditional integration + Tracking anti-windup.

The output is a power command between 0 and 1 (0-100%).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from math import exp
from typing import Any, Dict, Optional

from .hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
)
from homeassistant.core import HomeAssistant
from vtherm_api.command_receipt import AppliedCommandReceipt, CommandReceiptKind

from .smartpi.const import (
    SmartPIPhase,
    GovernanceRegime,
    GovernanceDecision,
    MAX_STEP_PER_MINUTE,
    SETPOINT_BOOST_RATE,
    SETPOINT_MODE_DELTA_C,
    LEARNING_PAUSE_RESUME_MIN,
    PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN,
    HYST_UPPER_C,
    HYST_LOWER_C,
    DEFAULT_DEADBAND_C,
    INTEGRAL_HOLD_SOURCE_DEADBAND_SHELL,
    DEFAULT_NEAR_BAND_DEG,
    ERROR_FILTER_TAU,
    NEAR_SETPOINT_UNWIND_STEPS,
    NEAR_BAND_HYSTERESIS_C,
    RESTART_BRIDGE_MAX_GAP_S,
    clamp,
)
from .smartpi.anticipation import AnticipationHorizon, DeadbandGate, anticipated_error, correction_c, select_horizon
from .smartpi.learning_start import LearningStart
from .smartpi.deadtime_estimator import (
    DeadTimeEstimator,
    deadtime_window_active,
    update_episode_starts,
)
from .smartpi.ab_estimator import ABEstimator
from .smartpi.diagnostics import build_published_diagnostics
from .smartpi.governance import SmartPIGovernance
from .smartpi.setpoint import SmartPISetpointManager
from .smartpi.guards import GUARD_CUT_I_MODE, SmartPIGuards
from .smartpi.controller import SmartPIController
from .smartpi.learning_window import LearningWindowManager
from .smartpi.deadband import DeadbandManager
from .smartpi.gains import GainScheduler
from .smartpi.feedforward import (
    FFResult,
    add_wall_part,
    compute_ff,
    compute_ff_gain,
    compute_ff_warmup_scale,
    compute_room_ff,
)
from .smartpi.ff_ab_confidence import ABConfidence
from .smartpi.tint_filter import AdaptiveTintFilter
from .smartpi.resume_hold import ResumeDeadtimeHold, ResumeHoldSource
from .smartpi.valve_curve import ValveCurveParams, build_valve_curve
from .smartpi.tar_learning.manager import TarLearningManager
from .smartpi.room_model.feedforward import HoldingTransfer, select_source
from .smartpi.room_model.manager import RoomModelManager
from .smartpi.room_model.const import RM_EXCLUDED_SHEDDING
from .smartpi.restart_bridge import (
    RestartAfter,
    RestartBridge,
    RestartDecision,
    command_holdable,
    decide,
)
from .smartpi.const import ENABLE_ADAPTIVE_TINT_FILTER
from .smartpi.temperature_history import TemperatureHistory
from .smartpi.timestamp_utils import (
    elapsed_wall_minutes,
    monotonic_to_wall_ts,
    normalize_wall_timestamp,
    wall_to_monotonic_ts,
)
from .const import HVAC_OFF_REASON_WINDOW_DETECTION

_LOGGER = logging.getLogger(__name__)


class SmartPI:
    """
    SmartPI Algorithm - Auto-adaptive PI controller for Versatile Thermostat (VTH).

    Public API
    ----------
    - calculate(...): compute the next duty-cycle command in [0,1]
    - get_published_diagnostics(): return the nested live diagnostics tree
    - update_learning(...): feed learning data (slope and previously applied u)

    Design intent
    -------------
    - Primary objective: minimize overshoot on setpoint changes and recoveries,
      accepting a slower approach to target.
    - Keep computational footprint small; no external libs.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        # Original integration arguments (positional)
        cycle_min: float,
        minimal_activation_delay: int,
        minimal_deactivation_delay: int,
        name: str,
        max_on_percent: Optional[float] = None,
        # Tuning knobs (keyword)
        deadband_c: float = DEFAULT_DEADBAND_C,
        saved_state: Optional[Dict[str, Any]] = None,
        # --- Feed-forward (FF) progressive enablement ("smart warm-up") ---
        ff_warmup_ok_count: int = 30,
        ff_warmup_cycles: int = 6,
        near_band_deg: float = DEFAULT_NEAR_BAND_DEG,
        hysteresis_on: float = HYST_LOWER_C,
        hysteresis_off: float = HYST_UPPER_C,
        deadband_allow_p: bool = False,
        enable_valve_linearization: bool = False,
        valve_curve_params: ValveCurveParams | None = None,
        tar_learning: bool = False,
        room_model: bool = False,
        learning_start: str = "bootstrap",
    ) -> None:
        self._name = name
        self._cycle_min = cycle_min
        self._hyst_on = hysteresis_on
        self._hyst_off = hysteresis_off
        self._deadband_allow_p = bool(deadband_allow_p)
        self.valve_curve = build_valve_curve(
            enable_valve_linearization,
            valve_curve_params,
        )
        self._valve_mode_enabled = bool(enable_valve_linearization)
        # VT valve settings seen by SmartPI; set by the handler in valve mode.
        self.valve_config_diag: dict[str, Any] | None = None
        self.vt_safety_override = False  # set by the handler during VT safety mode

        self.deadband_c = float(deadband_c)

        self._minimal_activation_delay = int(minimal_activation_delay)
        self._minimal_deactivation_delay = int(minimal_deactivation_delay)
        self._max_on_percent = max_on_percent

        # FF progressive enablement
        self.ff_warmup_ok_count = max(int(ff_warmup_ok_count), 1)
        self.ff_warmup_cycles = max(int(ff_warmup_cycles), 1)
        self._cycles_since_reset: int = 0

        # 2DOF / scheduling parameters
        self.near_band_deg = max(float(near_band_deg), 0.0)

        # --- Sub-Systems ---
        self.gov = SmartPIGovernance(name)
        self.sp_mgr = SmartPISetpointManager(name)
        self.ctl = SmartPIController(name)

        # Model estimator
        self.est = ABEstimator()

        self.learn_win = LearningWindowManager(name, TemperatureHistory())
        self.deadband_mgr = DeadbandManager(name, near_band_deg)
        self.gain_scheduler = GainScheduler(name)
        self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)

        self._e_filt: Optional[float] = None

        # Outputs (duty-cycle only, timing calculated by handler)
        self._on_percent: float = 0.0
        self._committed_on_percent: float = 0.0
        self._actuator_on_percent: float = 0.0
        self._actuator_committed_on_percent: float = 0.0

        # Diagnostics / status
        self._last_error: float = 0.0
        self._last_error_p: float = 0.0
        self._anticipation = DeadbandGate()
        self._last_temperature_slope_h: float | None = None
        self._last_u_ff: float = 0.0
        self._last_u_pi: float = 0.0
        self._last_ff_reason: str = "ff_none"
        # Holding power source in force (learned b / a or room model) and the
        # bumpless transfer of its discrete changes
        self._ff_holding = HoldingTransfer()

        self._tau_reliable: bool = False

        # Track last time calculate() was executed for dt-based integration
        self._last_calculate_time: Optional[float] = None
        # Last learning tick while the guard cut holds the output at zero
        self._guard_cut_learning_ts: Optional[float] = None
        # Last integral tick while the guard cut holds the output at zero
        self._guard_cut_integral_ts: Optional[float] = None
        # Wall-clock time of the last valid active regulation tick. OFF saves
        # preserve this value so prolonged inactivity remains measurable.
        self._last_active_wall_ts: float | None = None
        # Track last target temp for learning invalidation
        self._last_target_temp = None

        # Track last sensor temperature (unrounded) used in calculations
        self._last_current_temp: float | None = None
        self._last_ext_temp: float | None = None
        self._ext_control: float | None = None  # last valid outdoor reading, control path only

        # Track last HVAC mode for integral reset on HEAT/COOL transitions
        self._last_hvac_mode: VThermHvacMode | None = None

        # Learning start timestamp
        self._learning_enabled: bool = True
        self._learning_start_date: Optional[datetime] = datetime.now()
        self._session_learn_ok_count_base: int = 0
        self._session_learn_skip_count_base: int = 0

        # Helper to distinguish Startup (Init) from Resume (OFF->ON)
        # We want to pause learning on Resume, but NOT on Startup/Reboot
        self._startup_grace_period: bool = True

        # Output chain diagnostics
        self._last_u_cmd: float = 0.0       # command after [0,1] clamp
        self._last_u_limited: float = 0.0   # after rate-limit and max_on_percent
        self._last_u_applied: float = 0.0   # candidate output after timing constraints
        self._last_forced_by_timing: bool = False  # True when timing forced 0%/100%
        self._output_initialized: bool = False # True once calculate() runs successfully

        # --- Dead Time (L) Support ---
        self.learning_start = learning_start  # "bootstrap" or "direct" (smartpi/learning_start.py)
        self._start = LearningStart(learning_start, self, getattr(hass, "config", None))
        self.dt_est = DeadTimeEstimator(self._start.direct, self._start.measures_start, self.learn_win.temperatures)
        # Learning of the HEAT active dead time (None when the algorithm runs without it)
        self.tar_learning: TarLearningManager | None = None
        if tar_learning:
            time_zone = getattr(getattr(hass, "config", None), "time_zone", None)
            self.tar_learning = TarLearningManager(
                name, time_zone if isinstance(time_zone, str) else None
            )
        # Room model, on the history of the T_ar learning (None when off); once
        # published it gives the feed-forward holding power in heating.
        self.room_model: RoomModelManager | None = None
        if room_model and self.tar_learning is not None:
            self.room_model = RoomModelManager(
                name, time_zone if isinstance(time_zone, str) else None, self.tar_learning
            )
        self._t_heat_episode_start: float | None = None
        self._t_cool_episode_start: float | None = None
        self._deadtime_skip_count_a: int = 0
        self._deadtime_skip_count_b: int = 0

        # --- Guard Manager (Phase 2.5 refactoring) ---
        self.guards = SmartPIGuards()

        # Cycle tracking
        self._setpoint_changed_in_cycle: bool = False

        # --- AB confidence ---
        self._ab_confidence = ABConfidence()
        self._last_restart_reason: str = "none"
        self.resume_hold = ResumeDeadtimeHold()

        # Last FFResult (used by diagnostics)
        self._last_ff_result: FFResult | None = None

        # Bootstrap on/off output saved before a restart (monotonic time),
        # applied by the first calculation after a bridged restart.
        self._hysteresis_restore: dict | None = None

        # Restart bridge: context of the last tick, and the decision on the
        # restart of this session, consulted by every component.
        self.restart = RestartBridge()
        # Set by the handler: why SmartPI is not in control at the first tick
        # after a restart, as seen by the thermostat (window, shedding, safety).
        self.restart_exclusion_probe = None
        # The next time tracking spans the downtime of a bridged restart.
        self._bridge_first_tick = False
        self._clear_restart_wait()

        # --- Safety-First Governance (Delegated to self.gov) ---
        if saved_state:
            self.load_state(saved_state)

        _LOGGER.debug("%s - SmartPI initialized", self._name)

    # ------------------------------
    # Persistence
    # ------------------------------

    ########################################################################
    #                                                                      #
    #                      PERSISTENCE & STATE                             #
    #                                                                      #
    ########################################################################

    def reset_learning(self) -> None:
        """Reset all learned parameters to defaults."""
        self.est.reset()
        self._session_learn_ok_count_base = self.est.learn_ok_count
        self._session_learn_skip_count_base = self.est.learn_skip_count
        self.ctl.reset()
        self.sp_mgr.reset()
        self.gov.reset()

        self._last_error = 0.0
        self._last_error_p = 0.0
        self._anticipation.reset()
        self._set_linear_output(0.0)
        self._output_initialized = False
        self._last_u_ff = 0.0
        self._last_u_pi = 0.0
        self._last_ff_reason = "ff_none"
        self._last_u_cmd = 0.0
        self._last_u_limited = 0.0
        self._last_u_applied = 0.0
        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self._ff_holding.reset()
        self._e_filt = None
        self._cycles_since_reset = 0

        # Reset learning states
        self._last_calculate_time = None
        self._last_active_wall_ts = None
        self._last_target_temp = None
        self._last_current_temp = None
        self._last_ext_temp = None
        self._last_hvac_mode = None
        self._learning_start_date = datetime.now()

        # Learning window state is managed by learn_win component
        self.tint_filter.reset()

        # Reset Dead Time Estimator
        self.dt_est.reset()
        if self.tar_learning is not None:
            self.tar_learning.reset()
        if self.room_model is not None:
            self.room_model.reset()
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        self._deadtime_skip_count_a = 0
        self._deadtime_skip_count_b = 0

        # Reset guard manager
        self.guards.reset(keep_counts=True)

        # Reset new component managers (Phase 2.5 refactoring)
        self.learn_win.reset_all()
        self.est.a_model_ready = self.learn_win.response_lag.resolved
        self._hysteresis_restore = None
        self._start.reset()
        self.deadband_mgr.reset()
        self.gain_scheduler.reset()
        self._ab_confidence.reset()
        self._last_restart_reason = "none"
        self.resume_hold.clear()

        _LOGGER.info("%s - SmartPI learning and history reset", self._name)

    @property
    def Kp(self) -> float:
        return self.gain_scheduler.kp

    @Kp.setter
    def Kp(self, value: float):
        self.gain_scheduler.kp = value

    @property
    def Ki(self) -> float:
        return self.gain_scheduler.ki

    @Ki.setter
    def Ki(self, value: float):
        self.gain_scheduler.ki = value

    @property
    def phase(self) -> SmartPIPhase:
        return self._start.phase

    @property
    def command_is_hysteresis(self) -> bool:
        return self._start.command_is_hysteresis

    @property
    def command_is_on_off(self) -> bool:
        return self._start.command_is_on_off

    @property
    def meas_count_a(self) -> int:
        return len(self.est.a_meas_hist)

    @property
    def meas_count_b(self) -> int:
        return len(self.est.b_meas_hist)

    @property
    def _cycle_regimes(self):
        return self.gov.cycle_regimes

    def force_calibration(self) -> bool:
        """Request a calibration (measurement pulses, smartpi/calibration.py); False when it does not start."""
        return self._start.request_calibration(self.phase, self._learning_enabled)

    # ------------------------------
    # Learning entry point
    # ------------------------------

    def _reset_learning_window(self) -> None:
        """Reset the multi-cycle learning window state.

        Delegates to the LearningWindowManager component.
        """
        self.learn_win.reset()

    def reset_cycle_state(self) -> None:
        """Reset cycle tracking state (called when resuming from OFF).

        Clears learning window so the next cycle starts fresh.
        The CycleScheduler will re-drive cycle start/end via its own timer.
        """
        self._reset_learning_window()
        _LOGGER.debug("%s - SmartPI: cycle state reset (learning window cleared)", self._name)

    @property
    def learning_enabled(self) -> bool:
        """Return whether thermal model learning is enabled."""
        return self._learning_enabled

    def set_learning_enabled(self, enabled: bool) -> None:
        """Enable or pause learning and invalidate in-flight observations."""
        enabled = bool(enabled)
        if enabled == self._learning_enabled:
            return

        self._learning_enabled = enabled
        if not enabled:
            self._start.cancel_calibration()  # pulses would run without measuring
        self._reset_learning_window()
        self.dt_est.reset_observation(self._committed_on_percent)
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        _LOGGER.info(
            "%s - SmartPI learning %s",
            self._name,
            "enabled" if enabled else "paused",
        )

    def update_learning(
        self,
        dt_min: float,
        current_temp: float,
        ext_temp: float,
        u_active: float,
        setpoint_changed: bool = False,
        hvac_mode: VThermHvacMode | None = None,
        target_temp: float | None = None,
        raw_temp: float | None = None,
        a_learning_block_reason: str | None = None,
    ) -> None:
        """
        Feed learning data from the heartbeat tick.

        Delegates to the LearningWindowManager component.

        Args:
            dt_min: Elapsed time in minutes since last update
            current_temp: Current indoor temperature
            ext_temp: Current outdoor temperature
            u_active: Power applied during this interval (0..1)
            setpoint_changed: True if setpoint changed during this interval
            hvac_mode: Current HVAC mode.
            target_temp: Current target temperature.
            raw_temp: Raw measured indoor temperature (defaults to current_temp).
            a_learning_block_reason: Reason blocking ON/A learning, if any
                (a setpoint change blocks it with its own reason).
        """
        now = time.monotonic()

        if not self._learning_enabled:
            return

        if a_learning_block_reason is None and setpoint_changed:
            a_learning_block_reason = "skip: setpoint transition"

        # Delegate to LearningWindowManager
        self._deadtime_skip_count_a, self._deadtime_skip_count_b = self.learn_win.update(
            dt_min=dt_min,
            current_temp=current_temp,
            ext_temp=ext_temp,
            u_active=u_active,
            estimator=self.est,
            dt_est=self.dt_est,
            governance=self.gov,
            now=now,
            in_deadband=self.deadband_mgr.in_deadband,
            in_near_band=self.deadband_mgr.in_near_band,
            t_heat_episode_start=self._t_heat_episode_start,
            t_cool_episode_start=self._t_cool_episode_start,
            deadtime_skip_count_a=self._deadtime_skip_count_a,
            deadtime_skip_count_b=self._deadtime_skip_count_b,
            is_calibrating=self.phase == SmartPIPhase.CALIBRATION,
            is_hysteresis=(self.phase == SmartPIPhase.HYSTERESIS),
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            a_learning_allowed=a_learning_block_reason is None,
            a_learning_block_reason=a_learning_block_reason,
            raw_temp=raw_temp,
            min_active_window_min=self._cycle_min,
        )
        self._start.zero_integral()

    def _unwind_integral_during_guard_cut(
        self,
        now: float,
        target_temp: float | None,
        current_temp: float | None,
    ) -> None:
        """Track the forced zero output with the integral, down to the release point.

        The cut overrides the PI with 0 % while the PI still requests power: a
        positive integral that keeps the room above the cut threshold must
        follow the applied output, or the release restores the same power and
        the cut repeats. The integral tracks the applied 0 % by
        back-calculation, d(Ki * I)/dt = -v / Ti with v the PI request and
        Ti = Kp / Ki, while the PI request stays positive (never into SAT_LO).
        The unwinding rate scales with the request, not with the overshoot:
        an integral that carries more than the heat need comes down within a
        few cuts even when the room barely exceeds the threshold.

        Once the feed-forward balances the losses, the integral alone holds
        the room Ki * I / Kp above the setpoint. The unwinding stops when this
        offset is back at the cut threshold minus its hysteresis: a smaller
        integral cannot hold the room above the threshold and is not what
        keeps the cut (the heat need left after a setpoint decrease, the
        overshoot of a rise), so it is kept.
        """
        last = self._guard_cut_integral_ts
        if last is None:
            last = self._last_calculate_time
        self._guard_cut_integral_ts = now
        if last is None or target_temp is None or current_temp is None:
            return
        dt_min = min(max(now - last, 0.0) / 60.0, self._cycle_min)
        error = float(target_temp) - float(current_temp)  # the cut is HEAT only
        if dt_min <= 0.0 or error >= 0.0 or self.Ki <= 0.0:
            return
        release_offset = max(self.deadband_mgr.near_band_above_deg - NEAR_BAND_HYSTERESIS_C, 0.0)
        integral_floor = self.Kp * release_offset / self.Ki
        if self.ctl.integral <= integral_floor:
            return
        request = self.ctl.u_ff + self.Kp * error + self.Ki * self.ctl.integral
        if request <= 0.0:
            return
        if self.Kp > 0.0:
            # Back-calculation with Ti = Kp / Ki: delta(Ki * I) = -v * dt / Ti
            self.ctl.integral = max(self.ctl.integral - request * dt_min / self.Kp, integral_floor)
        else:
            self.ctl.integral = max(self.ctl.integral + error * dt_min, integral_floor)
        self.ctl.u_i = self.Ki * self.ctl.integral

    def _update_learning_during_guard_cut(
        self,
        *,
        now: float,
        current_temp: float,
        raw_temp: float,
        ext_temp: float | None,
        target_temp: float | None,
        hvac_mode: VThermHvacMode | None,
    ) -> None:
        """Feed the learning while the guard cut holds the output at zero.

        The room cools without heating during the cut (for instance during the
        whole fall after a setpoint decrease): it is a passive window for b and
        for the response lag baseline. The governance sees the forced zero
        command outside any band, and ON/A windows stay blocked.
        """
        last = self._guard_cut_learning_ts
        if last is None:
            last = self._last_calculate_time
        self._guard_cut_learning_ts = now
        if last is None:
            return
        # A gap between two ticks never counts for more than one cycle
        dt_min = min(max(now - last, 0.0) / 60.0, self._cycle_min)
        if dt_min <= 0.0:
            return
        self.deadband_mgr.in_deadband = False
        self.deadband_mgr.in_near_band = False
        self.gov.update_regime(
            self.gov.determine_regime(
                self.phase, ext_temp, False, False, True, 0.0, False, False
            )
        )
        self.update_learning(
            dt_min=dt_min,
            current_temp=current_temp,
            ext_temp=ext_temp,
            u_active=self._committed_on_percent,
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            raw_temp=raw_temp,
            a_learning_block_reason="skip: guard cut",
        )

    async def on_command_receipt(self, receipt: AppliedCommandReceipt) -> None:
        """Commit one command applied by the scheduler."""
        self._start.note_receipt(receipt, self._valve_mode_enabled)
        self._commit_applied_command(receipt.applied_on_percent, receipt.hvac_mode,
                                     cycle_start=receipt.kind == CommandReceiptKind.CYCLE_START)

    async def on_cycle_started(
        self,
        on_time_sec: float,
        off_time_sec: float,
        on_percent: float,
        hvac_mode: str,
        physical_on_percent: float | None = None,
    ) -> None:
        """Commit a cycle start reported by a scheduler without receipts."""
        del on_time_sec, off_time_sec
        self._commit_applied_command(
            physical_on_percent
            if self._valve_mode_enabled and physical_on_percent is not None
            else on_percent,
            hvac_mode,
            cycle_start=True,
        )

    def on_applied_power_updated(
        self,
        on_percent: float | None,
        hvac_mode: VThermHvacMode,
    ) -> None:
        """Commit a mid-cycle valve position read without receipts."""
        if on_percent is None:
            return
        self._commit_applied_command(on_percent, hvac_mode, cycle_start=False)

    def _commit_applied_command(
        self,
        actuator_power: float,
        hvac_mode: object,
        *,
        cycle_start: bool,
    ) -> None:
        """Commit the applied actuator power to the deadtime and learning components."""
        now = time.monotonic()
        linear_power = self._set_committed_actuator_output(actuator_power)
        self._update_deadtime_episode_status(linear_power, hvac_mode, now)
        # The learning components resume their histories once the restart is decided.
        if self.tar_learning is not None and not self.restart.pending:
            # linear_power is the heat-equivalent power of the applied command
            # (valve curve inverted), the input unit of the model a and b.
            self.tar_learning.on_applied(
                time.time(), linear_power, cycle_start, self._cycle_min, hvac_mode,
                valve=self._valve_mode_enabled,
            )
        if self.room_model is not None and not self.restart.pending:
            try:
                self.room_model.on_applied(
                    time.time(), linear_power, cycle_start, self._cycle_min, hvac_mode,
                    valve=self._valve_mode_enabled,
                )
            except Exception:  # noqa: BLE001 - the room model must never disturb the regulation
                _LOGGER.exception("%s - room model: applied command not recorded", self._name)
        if cycle_start:
            self._setpoint_changed_in_cycle = False
            # Reset governance regime tracking for new cycle
            self.gov.on_cycle_start()

    async def on_cycle_completed(self, e_eff: float = None, elapsed_ratio: float = 1.0, **_kw) -> None:
        """Handle end of cycle (learning)."""
        del e_eff, elapsed_ratio

        # Cycle accepted -> Count it
        self._cycles_since_reset += 1

    @property
    def a(self) -> float:
        return self.est.a

    @property
    def b(self) -> float:
        return self.est.b

    def _to_actuator_power(self, linear_power: float) -> float:
        """Convert internal linear demand to actuator command."""
        return self.valve_curve.apply(linear_power)

    def _to_linear_power(self, actuator_power: float) -> float:
        """Convert actuator feedback to internal linear demand."""
        return self.valve_curve.invert(actuator_power)

    def _linear_max_on(self) -> float | None:
        """Return max_on_percent, an actuator limit in VT, in linear demand space."""
        cap = self._max_on_percent
        return self.valve_curve.invert(cap) if cap is not None and self.valve_curve.params else cap

    def configure_valve_linearization(
        self,
        enabled: bool,
        params: ValveCurveParams | None = None,
        valve_mode: bool | None = None,
    ) -> None:
        """Configure the actuator linearization curve."""
        self.valve_curve = build_valve_curve(enabled, params)
        self._valve_mode_enabled = (
            bool(valve_mode) if valve_mode is not None else bool(enabled)
        )
        self._sync_actuator_on_percent()
        self._sync_actuator_committed_on_percent()

    def _sync_actuator_on_percent(self) -> None:
        """Refresh the actuator command from the current linear command."""
        self._actuator_on_percent = self._to_actuator_power(self._on_percent)

    def _sync_actuator_committed_on_percent(self) -> None:
        """Refresh the committed actuator command from the linear committed value."""
        self._actuator_committed_on_percent = self._to_actuator_power(
            self._committed_on_percent
        )

    def _set_linear_output(self, linear_power: float) -> None:
        """Set the candidate output in the internal linear command space."""
        self._on_percent = clamp(linear_power, 0.0, 1.0)
        self._sync_actuator_on_percent()

    def _set_committed_actuator_output(self, actuator_power: float) -> float:
        """Commit an actuator command and return its linear equivalent."""
        self._actuator_committed_on_percent = clamp(actuator_power, 0.0, 1.0)
        linear_power = self._to_linear_power(self._actuator_committed_on_percent)
        self._committed_on_percent = linear_power
        self.u_prev = linear_power
        self._last_u_applied = linear_power
        return linear_power

    def _commit_current_linear_output(self) -> None:
        """Commit the current linear candidate output and refresh actuator state."""
        self._committed_on_percent = self._on_percent
        self.u_prev = self._on_percent
        self._last_u_applied = self._on_percent
        self._sync_actuator_on_percent()
        self._sync_actuator_committed_on_percent()

    @property
    def on_percent(self) -> float:
        return self._to_actuator_power(self._on_percent)

    @property
    def calculated_on_percent(self) -> float:
        return self._to_actuator_power(self._on_percent)

    @property
    def committed_on_percent(self) -> float:
        return self._to_actuator_power(self._committed_on_percent)

    @property
    def linear_on_percent(self) -> float:
        """Return the current internal linear demand."""
        return self._on_percent

    @property
    def linear_committed_on_percent(self) -> float:
        """Return the current committed internal linear demand."""
        return self._committed_on_percent

    @property
    def integral_error(self) -> float:
        """Current integral accumulator value."""
        return self.integral

    @property
    def kp(self) -> float:
        return self.gain_scheduler.kp

    @property
    def ki(self) -> float:
        return self.gain_scheduler.ki

    @property
    def u_ff(self) -> float:
        """Last feed-forward value."""
        return self._last_u_ff

    @property
    def u_pi(self) -> float:
        return self._last_u_pi

    @property
    def tau_min(self) -> float:
        return self.est.tau_reliability().tau_min

    @property
    def tau_reliable(self) -> bool:
        return self._tau_reliable

    @property
    def learn_ok_count(self) -> int:
        return self.est.learn_ok_count

    @property
    def learn_ok_count_a(self) -> int:
        return self.est.learn_ok_count_a

    @property
    def learn_ok_count_b(self) -> int:
        return self.est.learn_ok_count_b

    @property
    def learn_skip_count(self) -> int:
        return self.est.learn_skip_count

    @property
    def learn_last_reason(self) -> str:
        return self.est.learn_last_reason

    @property
    def last_decision_thermal(self) -> str:
        return self.gov.last_decision_thermal.value

    @property
    def last_decision_gains(self) -> str:
        return self.gov.last_decision_gains.value

    @property
    def last_i_mode(self) -> str:
        return self.ctl.last_i_mode

    @property
    def integral(self) -> float:
        """Current integral accumulator value (delegated to controller)."""
        return self.ctl.integral

    @integral.setter
    def integral(self, value: float) -> None:
        self.ctl.integral = value

    @property
    def u_prev(self) -> float:
        """Previously applied power (last_on_percent)."""
        return self.ctl.u_prev

    @u_prev.setter
    def u_prev(self, value: float) -> None:
        self.ctl.u_prev = value

    @property
    def last_sat(self) -> str:
        return self.ctl.last_sat

    @last_sat.setter
    def last_sat(self, value: str):
        self.ctl.last_sat = value

    @property
    def in_deadband(self) -> bool:
        return self.deadband_mgr.in_deadband

    @in_deadband.setter
    def in_deadband(self, value: bool):
        self.deadband_mgr.in_deadband = value

    @property
    def in_near_band(self) -> bool:
        return self.deadband_mgr.in_near_band

    @in_near_band.setter
    def in_near_band(self, value: bool):
        self.deadband_mgr.in_near_band = value

    @property
    def error(self) -> float:
        return self._last_error

    @property
    def error_p(self) -> float:
        return self._last_error_p

    @property
    def anticipation_c(self) -> float:
        """Correction (°C) added to the measured temperature for the P and I errors, 0 when off."""
        return self._anticipation.value

    def anticipation_horizon(self, hvac_mode: VThermHvacMode | None = None) -> AnticipationHorizon:
        """Horizon (s) of the anticipation read now and its source (see smartpi/anticipation.py)."""
        mode = hvac_mode if hvac_mode is not None else self._last_hvac_mode
        return select_horizon(
            self.learn_win.heating_balance.kept_lag_s, lambda: self.gain_dead_time(mode), mode,
            self._start.kept_lag_s(mode),
        )

    @property
    def anticipation_horizon_in_use_s(self) -> float:
        """Horizon (s) of the anticipation read now; 0 = no anticipation."""
        return self.anticipation_horizon().seconds

    @property
    def temperature_slope_h(self) -> float | None:
        return self._last_temperature_slope_h

    @property
    def error_filtered(self) -> float:
        return self._e_filt if self._e_filt is not None else 0.0

    @property
    def u_cmd(self) -> float:
        return self._last_u_cmd

    @property
    def u_limited(self) -> float:
        return self._last_u_limited

    @property
    def u_applied(self) -> float:
        return self._to_actuator_power(self._last_u_applied)

    @property
    def aw_du(self) -> float:
        return self.ctl.last_aw_du

    @property
    def forced_by_timing(self) -> bool:
        return self._last_forced_by_timing

    def update_timing_constraints(self, u_prev: float, u_cmd: float) -> float:
        """Apply min on/off delays and update forced_by_timing status."""
        self._last_forced_by_timing = False

        # Valve opening does not depend on scheduler ON time: min ON/OFF delays
        # only constrain switch-like outputs.
        if self._valve_mode_enabled:
            return u_cmd

        u_final = u_cmd
        cycle_sec = self._cycle_min * 60
        on_time_sec = u_cmd * cycle_sec

        # Case: Switching ON
        if u_prev <= 0.001 and u_cmd > 0.001:
            if 0.001 < on_time_sec < self._minimal_activation_delay:
                u_final = 0.0
                self._last_forced_by_timing = True

        # Case: Switching OFF (simplified, usually handled by handler)
        # but if we force it, we should report it.

        return u_final

    @property
    def cycle_min(self) -> float:
        """Return the cycle duration in minutes."""
        return self._cycle_min

    # Learning window properties - delegate to learn_win component
    @property
    def learn_win_active(self) -> bool:
        """Return True if a learning window is currently active."""
        return self.learn_win.active

    @property
    def learn_u_int(self) -> float:
        """Return the accumulated power integral (u * dt)."""
        return self.learn_win.u_int

    @property
    def learn_t_int_s(self) -> float:
        """Return the accumulated time integral in seconds."""
        return self.learn_win.t_int_s

    def regulation_dead_time(self, hvac_mode: VThermHvacMode | None = None):
        """Dead-time source of the regulation.

        It is the dead-time estimator itself, unless the gains of the T_ar
        learning use a learned HEAT active dead time; the estimator values
        are never modified.
        """
        if self.tar_learning is None:
            return self.dt_est
        mode = hvac_mode if hvac_mode is not None else self._last_hvac_mode
        return self.tar_learning.regulation_dead_time(self.dt_est, mode)

    def gain_dead_time(self, hvac_mode: VThermHvacMode | None = None):
        """Dead-time source of the gain rule.

        The rule expects an effective dead time: with the T_ar learning it is
        the one of its apply rule (tar_learning/policy.py), the other consumers
        use regulation_dead_time.
        """
        if self.tar_learning is None:
            return self.dt_est
        mode = hvac_mode if hvac_mode is not None else self._last_hvac_mode
        return self.tar_learning.gain_dead_time(self.dt_est, mode)

    def observe_tar_learning(
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode | None,
        excluded: str | None = None,
    ) -> None:
        """Record the signals of the T_ar learning (no-op when the learning is disabled).

        excluded: why the applied power is not SmartPI's command (None when it is).
        """
        if self.tar_learning is None or self.restart.pending:
            return
        if excluded is None and self.vt_safety_override:
            excluded = "safety mode"
        try:
            self.tar_learning.observe(
                time.time(),
                hvac_mode=hvac_mode,
                temp=current_temp,
                ext=ext_current_temp,
                setpoint=target_temp,
                phase=str(self.phase.value) if isinstance(self.phase, SmartPIPhase) else str(self.phase),
                b=self.est.b,
                dtc=self.dt_est.deadtime_cool_s,
                dtc_reliable=self.dt_est.deadtime_cool_reliable,
                lag=self.learn_win.response_lag.lag_s,
                cycle_min=self._cycle_min,
                valve=self._valve_mode_enabled,
                excluded=excluded,
            )
        except Exception:  # noqa: BLE001 - the learning must never disturb the regulation
            _LOGGER.exception("%s - T_ar learning: observation failed", self._name)

    def observe_room_model(
        self,
        current_temp: float | None,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode | None,
        excluded: str | None = None,
        measure_id: Any | None = None,
    ) -> None:
        """Feed the room model (no-op when it is off); the next cycle reads its tracking.

        excluded: why the applied power is not SmartPI's command (None when it is).
        measure_id: identity of the indoor temperature measurement (its time stamp).
        """
        if self.room_model is None or self.restart.pending:
            return
        if excluded is None and self.vt_safety_override:
            excluded = "safety mode"
        try:
            self.room_model.observe(
                time.time(),
                hvac_mode=hvac_mode,
                temp=current_temp,
                measure_id=measure_id,
                ext=ext_current_temp,
                excluded=excluded,
                a_per_min=self.est.a,
                first_moment_s=self.regulation_dead_time(VThermHvacMode_HEAT).deadtime_heat_s,
            )
        except Exception:  # noqa: BLE001 - the room model must never disturb the regulation
            _LOGGER.exception("%s - room model: observation failed", self._name)

    @property
    def in_deadtime_window(self) -> bool:
        """Check if we are currently inside the estimated dead time window."""
        return deadtime_window_active(
            self.regulation_dead_time(),
            self._t_heat_episode_start,
            self._t_cool_episode_start,
            time.monotonic(),
        )

    # Phase 2: Helper for Near-Band Auto-Calculation
    def update_near_band_auto(self, hvac_mode: VThermHvacMode, current_temp: float, ext_temp: Optional[float]) -> None:
        """
        Calculate Near-Band thresholds based on Dead Time and Model Slopes (Phase 2).

        Delegates to DeadbandManager component.
        """
        # Delegate to DeadbandManager component
        self.deadband_mgr.update_near_band_auto(
            hvac_mode=hvac_mode,
            current_temp=current_temp,
            ext_temp=ext_temp,
            dt_est=self.regulation_dead_time(hvac_mode),
            estimator=self.est,
            cycle_min=self.cycle_min,
            deadband_c=self.deadband_c,
        )
        # Near-band state is now managed by DeadbandManager component

    # ------------------------------
    # Main control law
    # ------------------------------

    ########################################################################
    #                                                                      #
    #                      CONTROL LAW                                     #
    #                                                                      #
    ########################################################################

    def save_state(self) -> dict:
        """Save algorithm state for persistence."""
        state = {
            "version": 2,
            "learning_enabled": self._learning_enabled,
            "last_active_wall_ts": self._last_active_wall_ts,
            "deadtime_skip_count_a": self._deadtime_skip_count_a,
            "deadtime_skip_count_b": self._deadtime_skip_count_b,
            "cycles_since_reset": min(self._cycles_since_reset, self.ff_warmup_cycles),
            "est_state": self.est.save_state(),
            "dt_est_state": self.dt_est.save_state(),
            "gov_state": self.gov.save_state(),
            "ctl_state": self.ctl.save_state(),
            "sp_mgr_state": self.sp_mgr.save_state(),
            # Component states
            "lw_state": self.learn_win.save_state(),
            "db_state": self.deadband_mgr.save_state(),
            "gs_state": self.gain_scheduler.save_state(),
            "guards_state": self.guards.save_state(),
            # Holding power source the restored integral complements
            "ff_source": self._ff_holding.save(),
            "ff_wall": self._ff_holding.save_wall(),
            "tint_filter_state": self.tint_filter.save_state(),
        }
        hysteresis = self._hysteresis_output_context()
        if hysteresis is not None:
            state["hysteresis_output"] = {
                "on": hysteresis["on"],
                "wall_ts": monotonic_to_wall_ts(hysteresis["ts"]),
                "hvac_mode": hysteresis["hvac_mode"],
            }
        state.update(self._start.save(time.monotonic()))
        restart = self._restart_context()
        if restart is not None:
            state["restart"] = restart
        return state

    def _restart_context(self) -> dict | None:
        """Last tick, heater state and transient regulation state, for a bridged restart."""
        context = self.restart.save()
        if context is None or self.restart.pending:
            # Saved again before the restart was decided: the first stop's context.
            return context
        context.update(
            committed=[self._committed_on_percent, self._actuator_committed_on_percent],
            calc_wall=monotonic_to_wall_ts(self._last_calculate_time),
            heat_episode_wall=monotonic_to_wall_ts(self._t_heat_episode_start),
            cool_episode_wall=monotonic_to_wall_ts(self._t_cool_episode_start),
        )
        return context

    def _restart_cycle(self) -> tuple:
        """Cycle configuration that a bridged restart must keep."""
        return (
            float(self._cycle_min),
            int(self._minimal_activation_delay),
            int(self._minimal_deactivation_delay),
            bool(self._valve_mode_enabled),
        )

    def _calc_exclusion(self, power_shedding: bool) -> str | None:
        """Why SmartPI is not in control, from the inputs of a calculation."""
        if power_shedding:
            return RM_EXCLUDED_SHEDDING
        if self.vt_safety_override:
            return "safety mode"
        return None

    def note_control_exclusion(self, excluded: str | None) -> None:
        """Why SmartPI's command is not the applied one at this tick, as seen by the thermostat."""
        self.restart.note_exclusion(excluded)

    @property
    def restart_bridged(self) -> bool:
        """The restart of this session was bridged."""
        return self.restart.bridged

    def resolve_restart(
        self,
        hvac_mode: VThermHvacMode | None,
        current_temp: float | None,
        power_shedding: bool = False,
    ) -> RestartDecision | None:
        """Decide the restart once, at the first tick after a load that has the indoor temperature.

        The restart stays pending, and no component is fed, until that tick.
        """
        if not self.restart.pending:
            return self.restart.decision
        if current_temp is None:
            return None
        # A heater cut during the wait did not keep its state over the stop:
        # the decision is then taken without an indoor temperature.
        decision = decide(
            self.restart.before(self._restart_stores()),
            self._restart_after(
                time.time(), hvac_mode, power_shedding,
                None if self._restart_wait_cut else current_temp,
            ),
        )
        self._clear_restart_wait()
        saved = self.restart.saved
        self.restart.resolve(decision)
        self._apply_restart_decision(decision, saved)
        if decision.bridged:
            _LOGGER.info("%s - Restart bridged (stop of %.0f s)", self._name, decision.downtime_s)
        else:
            _LOGGER.info("%s - Restart not bridged: %s", self._name, decision.reason)
        return decision

    def _clear_restart_wait(self) -> None:
        """Forget the wait for the indoor temperature of a pending restart."""
        # The output was forced to 0 during the wait
        self._restart_wait_cut = False

    def _restart_stores(self) -> list:
        """Last tick and heater state of the learning stores that hold data."""
        stores = []
        for manager in (self.tar_learning, self.room_model):
            context = manager.restart_context() if manager is not None else None
            if context is not None:
                stores.append(context)
        return stores

    def _restart_after(
        self,
        now_wall: float,
        hvac_mode: VThermHvacMode | None,
        power_shedding: bool,
        temp: float | None,
    ) -> RestartAfter:
        """What is seen after the restart, for its decision."""
        excluded = self._calc_exclusion(power_shedding)
        probe = self.restart_exclusion_probe
        if excluded is None and callable(probe):
            excluded = probe()
        return RestartAfter(
            now=now_wall,
            hvac_mode=None if hvac_mode is None else str(hvac_mode),
            cycle=self._restart_cycle(),
            excluded=excluded,
            temp=temp,
        )

    def _restart_held_command(
        self, hvac_mode: VThermHvacMode | None, power_shedding: bool
    ) -> float | None:
        """Command of before the stop, kept while a restart waits for the indoor temperature.

        Over a short stop the heater keeps its last commanded state. While the
        restart is pending for lack of an indoor temperature, the calculation
        keeps that command instead of cutting the heater, as long as the
        restart is bridgeable as far as known without the temperature: the
        stop and the wait together within RESTART_BRIDGE_MAX_GAP_S, same mode
        and cycle, SmartPI in control. None otherwise, and once the output was
        forced to 0: the output is 0.
        """
        if not self.restart.pending or self._restart_wait_cut:
            return None
        saved = self.restart.saved
        try:
            linear = clamp(float(saved["committed"][0]), 0.0, 1.0)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if not command_holdable(
            self.restart.before(self._restart_stores()),
            self._restart_after(time.time(), hvac_mode, power_shedding, None),
        ):
            return None
        return linear

    def _apply_restart_decision(self, decision: RestartDecision, saved: dict | None) -> None:
        """Resume the work in progress of every component after a bridged restart, or drop it."""
        bridged = decision.bridged
        self.dt_est.resume_after_restart(bridged)
        self.learn_win.resume_after_restart(bridged)
        # The learning histories are HEAT-only: in COOL they keep their handling outside heating.
        if self.tar_learning is not None:
            self.tar_learning.on_restart(decision.heat_bridged, decision.t_resume)
        if self.room_model is not None:
            self.room_model.on_restart(decision.heat_bridged, decision.t_resume)
        if not bridged or saved is None:
            return
        try:
            linear, actuator = (clamp(float(v), 0.0, 1.0) for v in saved["committed"])
        except (KeyError, TypeError, ValueError):
            linear = actuator = 0.0
        # The heater kept the committed command over the stop.
        self._committed_on_percent = linear
        self._actuator_committed_on_percent = actuator
        self._last_u_applied = linear
        self.u_prev = linear
        self._t_heat_episode_start = wall_to_monotonic_ts(saved.get("heat_episode_wall"))
        self._t_cool_episode_start = wall_to_monotonic_ts(saved.get("cool_episode_wall"))
        # The first tick spans the stop, without the learning freeze of a startup.
        self._last_calculate_time = wall_to_monotonic_ts(saved.get("calc_wall"))
        self._startup_grace_period = False
        self._bridge_first_tick = True
        # The setpoint before the stop: a change during the stop is then detected
        # at the first tick like any other setpoint change.
        target = saved.get("target")
        if isinstance(target, (int, float)) and not isinstance(target, bool) and math.isfinite(target):
            self._last_target_temp = float(target)

    def _hysteresis_output_context(self) -> dict | None:
        """Return the bootstrap on/off output to resume after a restart."""
        if self._hysteresis_restore is not None:
            # Not yet applied: no calculation since the previous restart.
            return self._hysteresis_restore
        if (
            not self.command_is_hysteresis
            or self._last_calculate_time is None
            or self._last_hvac_mode not in (VThermHvacMode_HEAT, VThermHvacMode_COOL)
            or self._committed_on_percent not in (0.0, 1.0)
        ):
            return None
        return {
            "on": self._committed_on_percent == 1.0,
            "ts": self._last_calculate_time,
            "hvac_mode": str(self._last_hvac_mode),
        }

    @staticmethod
    def _load_hysteresis_output(value: object) -> dict | None:
        """Validate a saved bootstrap on/off output and convert its time."""
        if not isinstance(value, dict) or not isinstance(value.get("on"), bool):
            return None
        mode = value.get("hvac_mode")
        if mode not in (str(VThermHvacMode_HEAT), str(VThermHvacMode_COOL)):
            return None
        ts = wall_to_monotonic_ts(value.get("wall_ts"))
        if ts is None:
            return None
        return {"on": value["on"], "ts": ts, "hvac_mode": mode}

    def _take_hysteresis_restore(self, hvac_mode: VThermHvacMode | None) -> float | None:
        """Consume the saved bootstrap output once the restart is decided; return it when it applies.

        It applies after a bridged restart in the same hvac mode: over such a
        stop the actuator keeps its last state, as an uninterrupted hysteresis
        would have.
        """
        if self.restart.pending:
            return None
        restore = self._hysteresis_restore
        self._hysteresis_restore = None
        if restore is None or hvac_mode is None or str(hvac_mode) != restore["hvac_mode"]:
            return None
        if not self.restart.bridged:
            return None
        return 1.0 if restore["on"] else 0.0

    def load_state(self, state: dict) -> None:
        """Restore algorithm state from the current nested persistence format."""
        if not state:
            return

        if "est_state" not in state:
            _LOGGER.warning(
                "%s - Ignoring unsupported SmartPI persisted state format",
                self._name,
            )
            return

        # Load component states
        self.est.load_state(state.get("est_state", {}))
        self._session_learn_ok_count_base = self.est.learn_ok_count
        self._session_learn_skip_count_base = self.est.learn_skip_count
        self.dt_est.load_state(state.get("dt_est_state", {}))
        self.gov.load_state(state.get("gov_state", {}))
        self.ctl.load_state(state.get("ctl_state", {}))
        self.sp_mgr.load_state(state.get("sp_mgr_state", {}))
        lw_state = state.get("lw_state", {})
        self.learn_win.load_state(lw_state)
        if isinstance(lw_state, dict) and "response_lag" in lw_state:
            # The flag is otherwise refreshed by the first learning update: a
            # restored unresolved model must keep the bootstrap from the first
            # calculation. States without the block keep the default.
            self.est.a_model_ready = self.learn_win.response_lag.resolved
        self._learning_start_date = self.learn_win.learning_start_date
        self.deadband_mgr.load_state(state.get("db_state", {}))
        self.gain_scheduler.load_state(state.get("gs_state", {}))
        self.ctl.clear_integral_hold()

        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self._last_u_applied = 0.0
        self._last_restart_reason = "none"
        self.resume_hold.clear()
        self._hysteresis_restore = self._load_hysteresis_output(state.get("hysteresis_output"))
        self._start.load(state)
        # The restart is decided at the first tick (resolve_restart); until then
        # the transient state above stays as after a cold start.
        self.restart.load(state.get("restart"))
        self._bridge_first_tick = False
        self._clear_restart_wait()

        # Load main algorithm scalars
        self._deadtime_skip_count_a = int(state.get("deadtime_skip_count_a", 0))
        self._deadtime_skip_count_b = int(state.get("deadtime_skip_count_b", 0))
        self._last_active_wall_ts = normalize_wall_timestamp(
            state.get("last_active_wall_ts")
        )
        # A learning resume deadline from the previous session must NOT be carried over:
        # _startup_grace_period handles the post-restart freeze for exactly one cycle.
        # Keeping a stale deadline (e.g., the 20-min OFF-resume value) would overwrite
        # the learn_win ts on the second tick via update_learning() and block learning
        # for the full 20 minutes after every restart.
        self.learn_win.set_learning_resume_ts(None)

        # Load Guard State
        self.guards.load_state(state.get("guards_state", {}))

        # The restored integral complements this holding power source: a different
        # source at the first cycle is transferred like any change of source.
        self._ff_holding.load(state.get("ff_source"), state.get("ff_wall"))
        self.tint_filter.load_state(state.get("tint_filter_state", {}))

        self.set_learning_enabled(bool(state.get("learning_enabled", True)))

        # The restored integral holds the share of the load that the
        # feed-forward did not cover before the restart: resume the time_scale
        # ramp where it stood, so that the feed-forward comes back at the same
        # level and the restart stays bumpless.
        try:
            restored_cycles = int(state.get("cycles_since_reset", 0))
        except (TypeError, ValueError):
            restored_cycles = 0
        self._cycles_since_reset = max(0, min(restored_cycles, self.ff_warmup_cycles))

        # Instant FF on reboot when model is well-learned:
        # skip the time_scale ramp by pre-setting _cycles_since_reset.
        tau_info = self.est.tau_reliability()
        self._ab_confidence.evaluate(
            tau_reliable=tau_info.reliable,
            learn_ok_count_a=self.est.learn_ok_count_a,
            learn_ok_count_b=self.est.learn_ok_count_b,
        )
        if self.est.learn_ok_count_a >= 10 and tau_info.reliable:
            self._cycles_since_reset = self.ff_warmup_cycles
            _LOGGER.info(
                "%s - Reboot: model learned (ok_a=%d, tau_reliable) -> FF warmup bypassed",
                self._name, self.est.learn_ok_count_a,
            )
        else:
            _LOGGER.debug(
                "%s - Reboot: model not ready (ok_a=%d, tau_reliable=%s) -> FF ramp resumes at %d/%d",
                self._name, self.est.learn_ok_count_a, tau_info.reliable,
                self._cycles_since_reset, self.ff_warmup_cycles,
            )

    def _force_off_output(self) -> None:
        """Zero the output and committed command; drop the episode context."""
        self._set_linear_output(0.0)
        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self._last_u_applied = 0.0
        self.ctl.u_prev = 0.0
        self._last_u_cmd = 0.0
        self._last_u_pi = 0.0
        self._last_u_limited = 0.0
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        self._output_initialized = True
        self._last_calculate_time = None
        self.resume_hold.clear()

    def _validate_and_handle_off(
        self,
        target_temp: float | None,
        current_temp: float | None,
        hvac_mode: VThermHvacMode,
        power_shedding: bool,
        off_reason: str | None = None,
    ) -> bool:
        """Input validation and OFF/Shedding handling.

        Returns:
            True if calculation should STOP (OFF or invalid).
        """
        if target_temp is None or current_temp is None:
            held = (
                None if target_temp is None
                else self._restart_held_command(hvac_mode, power_shedding)
            )
            if held is None:
                _LOGGER.warning("%s - Missing target or current temp, force 0", self._name)
                if self.restart.pending:
                    self._restart_wait_cut = True
            else:
                _LOGGER.info(
                    "%s - No indoor temperature yet after the restart: command of before the stop kept (%.0f %%)",
                    self._name, 100.0 * held,
                )
            self._set_linear_output(0.0 if held is None else held)
            self._last_u_cmd = 0.0
            self._last_u_pi = 0.0
            self._last_u_limited = 0.0
            self.ctl.last_i_mode = "I:FREEZE(missing_sensor)"
            self._last_calculate_time = None
            return True

        if hvac_mode == VThermHvacMode_OFF:
            self._start.interrupt(time.monotonic(), "thermostat stopped")
            self._force_off_output()
            self.ctl.last_i_mode = "I:FREEZE(hvac_off)"
            self.deadband_mgr.in_deadband = False
            self.deadband_mgr.in_near_band = False
            self._last_restart_reason = (
                "window"
                if off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
                else "off"
            )
            return True

        # Handle explicit force off (shedding, windows)
        if power_shedding:
            self._start.interrupt(time.monotonic(), "power shedding")
            self._force_off_output()
            # We update regime to PERTURBED but SKIP PID calculation
            self.gov.on_cycle_start()
            self.gov.update_regime(GovernanceRegime.PERTURBED)
            _, reason = self.gov.decide_update("thermal")
            self.ctl.last_i_mode = f"I:RESET({reason.value})"
            self._last_restart_reason = "power_shedding"
            return True

        return False

    def _update_time_tracking(self, now: float) -> tuple[float, bool, bool, ResumeHoldSource]:
        """Update dt_min and handles first-run logic.

        Returns:
            Tuple of (dt_min, resumed_from_off, startup_first_run, resume_hold_source).
        """
        dt_min = 0.0
        resumed_from_off = False
        startup_first_run = False
        resume_hold_source = ResumeHoldSource.NONE
        bridge_tick, self._bridge_first_tick = self._bridge_first_tick, False
        if self._last_calculate_time is None:
            first_run = True
        else:
            first_run = False
            dt_min = (now - self._last_calculate_time) / 60.0
        self._last_calculate_time = now

        # Resume from OFF/Shedding/Startup (Only on first run after OFF)
        if first_run:
            if self._startup_grace_period:
                startup_first_run = True
                # After reboot: freeze learning for exactly 1 cycle to let
                # the system reach a coherent state before collecting samples.
                self._startup_grace_period = False
                resume_ts = now + (self._cycle_min * 60.0)
                self.learn_win.set_learning_resume_ts(resume_ts)
                _LOGGER.info(
                    "%s - Reboot: state restored, learning frozen for %.0f min (1 cycle)",
                    self._name, self._cycle_min
                )
            elif self._last_restart_reason in {"off", "window", "power_shedding"}:
                resumed_from_off = True
                if self._last_restart_reason == "window":
                    resume_hold_source = ResumeHoldSource.WINDOW_RESUME
                elif self._last_restart_reason == "power_shedding":
                    resume_hold_source = ResumeHoldSource.POWER_SHEDDING_RESUME
                self._last_restart_reason = "none"
                # Resume from window/OFF -> Pause learning to let system stabilize
                self.learn_win.set_learning_resume_ts(now + (LEARNING_PAUSE_RESUME_MIN * 60.0))
                _LOGGER.debug("%s - Resume from OFF: Learning paused for %d min", self._name, LEARNING_PAUSE_RESUME_MIN)

        # A gap between two ticks never counts for more than one cycle, except
        # the downtime of a bridged restart, over which the heater kept the
        # committed command. Its first cycle still starts from the raw setpoint,
        # as after any restart.
        max_dt_min = self._cycle_min
        if bridge_tick:
            startup_first_run = True
            max_dt_min = max(max_dt_min, RESTART_BRIDGE_MAX_GAP_S / 60.0)
        dt_min = min(dt_min, max_dt_min)

        return dt_min, resumed_from_off, startup_first_run, resume_hold_source

    def _refresh_active_timestamp_and_expire_controller_memory(self) -> bool:
        """Refresh active wall time and purge PI memory after prolonged inactivity."""
        now_wall = time.time()
        inactive_minutes = elapsed_wall_minutes(self._last_active_wall_ts, now_wall)
        expired = (
            inactive_minutes is not None
            and inactive_minutes > PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN
        )
        if expired:
            _LOGGER.info(
                "%s - Prolonged pause (%.1f min) detected, purging PI state and integral memory",
                self._name,
                inactive_minutes,
            )
            self.ctl.reset()
            self._ff_holding.reset()
        self._last_active_wall_ts = now_wall
        return expired

    def _manage_setpoint(
        self,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode,
        anticipation_c: float,
    ) -> tuple[bool, float, float, float | None]:
        """Setpoint change detection, error terms and boost logic.

        Returns:
            Tuple of (setpoint_changed, error_i, error_p, old_target_temp).
            error_i: raw setpoint error (SP - y), signed by hvac_mode — deadband and stored state.
            error_p: error on the anticipated temperature (SP - (y + anticipation_c)), signed by hvac_mode — P term.
        """
        setpoint_changed = False
        old_target_temp = self._last_target_temp  # Save before update
        if self._last_target_temp is not None:
            if abs(target_temp - self._last_target_temp) > 0.01:
                setpoint_changed = True
                self._setpoint_changed_in_cycle = True
                _LOGGER.info(
                    "%s - Target change detected (%.2f -> %.2f), invalidating learning window",
                    self._name, self._last_target_temp, target_temp
                )
        self._last_target_temp = target_temp

        error_i = target_temp - current_temp
        error_p = target_temp - (current_temp + anticipation_c)
        if hvac_mode == VThermHvacMode_COOL:
            error_i = -error_i
            error_p = -error_p

        self.sp_mgr.update_boost_state(target_temp, error_i)

        return setpoint_changed, error_i, error_p, old_target_temp

    def _update_control_context(
        self,
        error_i: float,
        hvac_mode: VThermHvacMode,
        current_temp: float,
        ext_current_temp: float | None,
        error_p: float,
        dt_min: float = 0.0,
    ) -> float:
        """Update tau reliability, error weighting, and deadband state.

        Args:
            error_i: Raw setpoint error (SP_brut - y) — used for deadband and stored state.
            error_p: Error on the anticipated temperature — returned as e_p for P term.

        Returns:
            e_p, the error used by the P term.
        """
        tau_info = self.est.tau_reliability()
        # Until the first model is published (PI-driven learning stage) no reader takes it as reliable.
        self._tau_reliable = tau_info.reliable and self.phase != SmartPIPhase.HYSTERESIS

        e_p = error_p
        alpha_err = 1.0 - exp(-max(dt_min, 0.001) / ERROR_FILTER_TAU)
        if self._e_filt is None:
            self._e_filt = error_i
        else:
            self._e_filt = (1.0 - alpha_err) * self._e_filt + alpha_err * error_i

        self._last_error = error_i
        self._last_error_p = e_p

        # Deadband update uses raw setpoint error (physical distance from target)
        self.deadband_mgr.update(
            error=error_i,
            near_band_error=self._e_filt if self._e_filt is not None else error_i,
            hvac_mode=hvac_mode,
            tau_reliable=self._tau_reliable,
            dt_est=self.regulation_dead_time(hvac_mode),
            estimator=self.est,
            current_temp=current_temp,
            ext_temp=ext_current_temp,
            cycle_min=self.cycle_min,
            deadband_c=self.deadband_c,
        )

        return e_p

    def _apply_gains_and_ff(
        self,
        gov_decision_g: GovernanceDecision,
        target_temp_ff: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode,
        error: float,
        e_p: float,
    ) -> float:
        """Calculate gains and feedforward and return the runtime feedforward."""
        old_ki = self.gain_scheduler.ki   # for the bumpless transfer below
        near_band_ratio = self._near_band_gain_ratio(error, hvac_mode)

        # Delegate gain calculation to GainScheduler component
        tau_info = self.est.tau_reliability()
        self.gain_scheduler.calculate(
            tau_reliable=self._tau_reliable,
            tau_min=tau_info.tau_min,
            estimator=self.est,
            dt_est=self.gain_dead_time(hvac_mode),
            in_near_band=self.deadband_mgr.in_near_band,
            governance_decision=gov_decision_g,
            near_band_ratio=near_band_ratio,
            cycle_min=self._cycle_min,
            valve_mode_enabled=self._valve_mode_enabled,
            hvac_mode=hvac_mode,
            pre_model_a=self._start.pre_model_a(hvac_mode),
        )
        # Bumpless gain change: the integral follows a change of Ki
        self.ctl.transfer_gains(old_ki, self.gain_scheduler.ki, self._name)

        # --- AB confidence ---
        self._ab_confidence.evaluate(
            tau_reliable=self._tau_reliable,
            learn_ok_count_a=self.est.learn_ok_count_a,
            learn_ok_count_b=self.est.learn_ok_count_b,
        )

        # --- Signed k_ff from the active mono-mode model ---
        k_ff = compute_ff_gain(self.est, hvac_mode, self._tau_reliable)
        warmup_scale = compute_ff_warmup_scale(
            learn_ok_count=self.est.ramp_count,
            ff_warmup_ok_count=self.ff_warmup_ok_count,
            cycles_since_reset=self._cycles_since_reset,
            ff_warmup_cycles=self.ff_warmup_cycles,
        )

        ff_result = compute_ff(
            k_ff=k_ff,
            target_temp_ff=target_temp_ff,
            ext_temp=ext_current_temp,
            warmup_scale=warmup_scale,
        )
        ff_result = self._select_holding_power(
            ff_result, target_temp_ff, ext_current_temp, hvac_mode
        )
        self._last_ff_result = ff_result
        self._last_ff_reason = ff_result.ff_reason
        return ff_result.u_ff1

    def _select_holding_power(
        self,
        learned_ff_result: FFResult,
        target_temp_ff: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode,
        now: float | None = None,
    ) -> FFResult:
        """Return the FF1 result of the holding power source in force.

        A published room model with a qualified energy-balance ratio replaces
        the learned branch in heating with PWM heaters. A discrete change of the
        static part (source, model structure, published parameters, new
        energy-balance ratio) is moved into the integral, so that
        the command does not jump. The wall part is never moved: it
        is blended over a fixed time when it is switched on or off or its
        parameters change (room_model/feedforward.py).
        """
        source = select_source(self.room_model, hvac_mode, self._valve_mode_enabled)
        holding = self._ff_holding
        delta = holding.update(
            source,
            now=time.time() if now is None else now,
            learned_power=learned_ff_result.u_ff1, learned_warmup=learned_ff_result.warmup_scale,
            target_temp=target_temp_ff,
            ext_temp=ext_current_temp,
            heat=hvac_mode == VThermHvacMode_HEAT,
        )
        if source.room:
            ff_result = compute_room_ff(
                source=source,
                target_temp_ff=target_temp_ff,
                ext_temp=ext_current_temp,
                wall=holding.wall_applied,
            )
        else:
            ff_result = add_wall_part(learned_ff_result, holding.wall_applied, ext_current_temp)
        if delta != 0.0:
            moved = self.ctl.absorb_feedforward_step(ki=self.Ki, delta_ff=delta)
            _LOGGER.info(
                "%s - Feed-forward source %s: static holding power step %+.4f moved into the integral (%+.4f applied)",
                self._name, source.plain, delta, moved,
            )
        return ff_result

    def _near_band_gain_ratio(
        self,
        error: float,
        hvac_mode: VThermHvacMode,
    ) -> float:
        """Return |error| scaled to the active near-band width."""
        threshold = self.deadband_mgr.near_band_entry_threshold(error, hvac_mode)
        if threshold <= 1e-9:
            return 1.0
        return clamp(abs(error) / threshold, 0.0, 1.0)

    def _apply_soft_constraints(
        self,
        u_cmd: float,
        dt_min: float,
        setpoint_changed: bool
    ) -> float:
        """Apply rate limiting and clamping to the output.

        Returns:
            The limited output.
        """
        # Rate Limit
        rate_limit = SETPOINT_BOOST_RATE if self.sp_mgr.boost_active else MAX_STEP_PER_MINUTE
        # Bypass rate limit on first run, setpoint change, or when dt_min is 0
        if setpoint_changed or not self._output_initialized or dt_min <= 0.0:
            u_limited = u_cmd
        else:
            max_step = rate_limit * dt_min
            prev_limited = self._last_u_limited
            u_limited = clamp(u_cmd, prev_limited - max_step, prev_limited + max_step)

        # SATURATION & FINAL OUTPUT
        max_on = self._linear_max_on()
        self._set_linear_output(clamp(u_limited, 0.0, 1.0 if max_on is None else max_on))
        return self._on_percent

    def calculate(
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None = None,
        slope: float | None = None,
        hvac_mode: VThermHvacMode | None = None,
        power_shedding: bool = False,
        cycle_boundary: bool = False,
        off_reason: str | None = None,
        temperature_measure_id: Any | None = None,
    ) -> float:
        """
        Compute the next duty-cycle command.

        cycle_boundary and temperature_measure_id are part of the calling
        contract of Versatile Thermostat; the regulation does not use them.
        """
        self.ctl.last_integral_hold_source = "none"
        self.ctl.last_aw_du = 0.0

        self._last_temperature_slope_h = float(slope) if isinstance(slope, (int, float)) else None

        now = time.monotonic()
        self._ext_control = self._ext_control if ext_current_temp is None else ext_current_temp
        self._start.begin(hvac_mode)

        # --- 0. HVAC mode transition (HEAT↔COOL) → reset integral ---
        # Track active modes BEFORE validation so that missing presets in COOL
        # don't intercept the cycle and hide the transition.
        if hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            # The persisted model mode covers the first tick after a restart.
            previous_mode = self._last_hvac_mode or self.est.model_hvac_mode
            mode_changed = previous_mode is not None and str(hvac_mode) != str(previous_mode)
            if mode_changed:
                self.ctl.reset()
                self._anticipation.reset()
                self._ff_holding.reset()
                _LOGGER.info(
                    "%s - HVAC mode changed (%s → %s): PI state reset",
                    self._name, previous_mode, hvac_mode
                )
            self._last_hvac_mode = hvac_mode
            model_reset = self.est.ensure_hvac_mode(hvac_mode)
            self.dt_est.ensure_hvac_mode(hvac_mode)
            if model_reset is True:
                self._ab_confidence.reset()
                self._cycles_since_reset = 0
                self._session_learn_ok_count_base = self.est.learn_ok_count
                self._session_learn_skip_count_base = self.est.learn_skip_count
                _LOGGER.info(
                    "%s - HVAC signed-model migration: reset mode-dependent A",
                    self._name,
                )

        # The restart of this session is decided at the first tick of a ready
        # thermostat (a target and an indoor temperature are available), then
        # the tick context is kept. It stays pending while a sensor is missing.
        if target_temp is not None and current_temp is not None:
            if self.restart.pending:
                self.resolve_restart(hvac_mode, current_temp, power_shedding)
            self.restart.note_tick(
                time.time(),
                hvac_mode=hvac_mode,
                temp=current_temp,
                cycle=self._restart_cycle(),
                excluded=self._calc_exclusion(power_shedding),
                target=target_temp,
            )

        # The saved bootstrap output only applies to the first calculation that
        # decides the restart; it stays pending while a sensor is missing.
        hysteresis_restore = self._take_hysteresis_restore(hvac_mode)

        # --- 1. Validation & Handle OFF ---
        if self._validate_and_handle_off(target_temp, current_temp, hvac_mode, power_shedding, off_reason):
            return

        self.guards.sensor_step_c = self.learn_win.balance.resolution_c

        # Guard Cut: force 0% if active
        if self.guards.guard_cut_active:
            # The output is forced but the room is still observed: the dead time
            # detection, the learning and the published temperature follow the cut.
            if current_temp is not None:
                t_int_lp, t_int_clean = self.tint_filter.update(current_temp, now)
                if not self.command_is_on_off:
                    self.dt_est.update(
                        now=now,
                        tin=t_int_lp,
                        tin_raw=current_temp,
                        u_applied=self._committed_on_percent,
                        hvac_mode=hvac_mode,
                    )
                self._last_current_temp = current_temp
                self._update_learning_during_guard_cut(
                    now=now,
                    current_temp=t_int_clean,
                    raw_temp=current_temp,
                    ext_temp=ext_current_temp,
                    target_temp=target_temp,
                    hvac_mode=hvac_mode,
                )
            self.ctl.last_i_mode = GUARD_CUT_I_MODE
            self.guards.note_cut(target_temp, current_temp, hvac_mode)   # published error of the cut
            self._start.follow_calibration(now, hvac_mode, current_temp)  # the end of a calibration is checked too
            self._unwind_integral_during_guard_cut(now, target_temp, current_temp)
            self._set_linear_output(0.0)
            self._last_calculate_time = None
            return
        # The learning stays contiguous across the end of a guard cut.
        guard_cut_learning_ts, self._guard_cut_learning_ts = self._guard_cut_learning_ts, None
        self._guard_cut_integral_ts = None

        self._refresh_active_timestamp_and_expire_controller_memory()

        # --- 1a. Adaptive T_int Filter ---
        if current_temp is not None:
            t_int_lp, t_int_clean = self.tint_filter.update(current_temp, now)
        else:
            t_int_lp = current_temp
            t_int_clean = current_temp

        # --- 2. Update Time Tracking ---
        dt_min, _resumed_from_off, _startup_first_run, resume_hold_source = self._update_time_tracking(now)

        # --- 3. Setpoint Management ---
        # Measurement pulses (direct start or calibration): the command of a pulse replaces the PI.
        self._start.advance(now, target_temp, current_temp, hvac_mode, self._on_percent)
        # The anticipated temperature feeds the P and I errors only; the deadband,
        # guards, feed-forward and learning keep the measured temperature.
        anticipating = not self.command_is_on_off
        anticipation_c = self._anticipation.apply(correction_c(
            self.anticipation_horizon(hvac_mode).seconds if anticipating else 0.0,
            now, current_temp, target_temp, self.learn_win.temperatures.raw, anticipating,
        ), self.deadband_mgr.in_deadband)
        setpoint_changed, error_i, error_p, old_target_temp = self._manage_setpoint(
            target_temp, current_temp, hvac_mode, anticipation_c,
        )
        error_i_ctl = anticipated_error(error_i, anticipation_c, hvac_mode)  # integral error on the anticipated temperature

        # Clear cycle regimes on setpoint change to prevent REGIME_TRANSITION freeze
        if setpoint_changed:
            self.gov.on_cycle_start()
            self.resume_hold.clear()
            # Large changes restart the error history; the integral is preserved.
            if abs(target_temp - old_target_temp) >= SETPOINT_MODE_DELTA_C:
                new_error = float(target_temp - current_temp)
                if hvac_mode == VThermHvacMode_COOL:
                    new_error = -new_error
                if new_error != 0.0:
                    self._e_filt = new_error
                    self._last_error = new_error
                    self._last_error_p = new_error
        elif resume_hold_source != ResumeHoldSource.NONE:
            self.resume_hold.start(resume_hold_source)

        # --- 4. Learning ---
        if self._learning_enabled and not self.command_is_on_off:
            self.dt_est.update(
                now=now,
                tin=t_int_lp,
                tin_raw=current_temp,
                u_applied=self._committed_on_percent,
                hvac_mode=hvac_mode,
            )

        # Heartbeat learning update
        learning_dt_min = dt_min
        if learning_dt_min <= 0 and guard_cut_learning_ts is not None:
            learning_dt_min = min(max(now - guard_cut_learning_ts, 0.0) / 60.0, self._cycle_min)
        if learning_dt_min > 0:
            self.update_learning(
                dt_min=learning_dt_min,
                current_temp=t_int_clean,
                ext_temp=ext_current_temp,
                u_active=self._committed_on_percent,
                setpoint_changed=setpoint_changed,
                hvac_mode=hvac_mode,
                target_temp=target_temp,
                raw_temp=current_temp,
            )

        # --- 5. Hysteresis command (or a measurement pulse, of the start or of a calibration) ---
        if self.command_is_on_off:
            out = self._start.output
            if out is None:
                out = self.ctl.calculate_hysteresis(target_temp, current_temp, hvac_mode, self._hyst_off, self._hyst_on)
            if out is None and hysteresis_restore is not None:
                # Inside the band after a short restart: resume the saved
                # on/off state instead of starting from OFF.
                out = hysteresis_restore
            if out is None and self._on_percent not in (0.0, 1.0):
                # The band keeps an on/off output. A modulated output left by
                # an earlier phase is not a hysteresis state: start from OFF.
                out = 0.0
            if out is not None:
                self._set_linear_output(out)

            # Hysteresis transitions are applied immediately by the handler,
            # so commit the bang-bang output now for deadtime tracking.
            self._commit_current_linear_output()
            self._update_deadtime_episode_status(self._committed_on_percent, hvac_mode, now)
            if self._learning_enabled:
                self.dt_est.update(
                    now=now,
                    tin=t_int_lp,
                    tin_raw=current_temp,
                    u_applied=self._committed_on_percent,
                    hvac_mode=hvac_mode,
                )

            self._last_current_temp = current_temp
            self._last_ext_temp = ext_current_temp
            return

        # --- 6. Control Context & Deadband ---
        e_p = self._update_control_context(
            error_i,
            hvac_mode,
            current_temp,
            self._ext_control,
            error_p,
            dt_min,
        )
        in_deadband_now = self.deadband_mgr.in_deadband
        in_core_deadband = self._tau_reliable and abs(error_i) < max(self.deadband_c, 0.0)
        self.resume_hold.update(
            error_i=error_i,
            in_core_deadband=in_core_deadband,
            deadband_c=self.deadband_c,
            hvac_mode=hvac_mode,
            dt_est=self.regulation_dead_time(hvac_mode),
            ctl=self.ctl,
            in_deadtime_window=lambda: self.in_deadtime_window,
        )

        # --- 6b. Integral freeze during deadtime window ---
        integrator_hold = self.in_deadtime_window
        integrator_hold_source = "deadtime" if integrator_hold else "none"

        # --- 7. Governance Decision ---
        regime = self.gov.determine_regime(
            self.phase,
            ext_current_temp,
            integrator_hold,
            power_shedding,
            self._output_initialized,
            self.ctl.u_cmd,
            self.deadband_mgr.in_deadband,
            self.deadband_mgr.in_near_band
        )
        self.gov.update_regime(regime)
        gov_decision_g, _ = self.gov.decide_update('gains')
        self.gov.decide_update('thermal', self.learn_win.learning_resume_ts, now)

        # --- 8. Gains & FF ---
        u_ff = self._apply_gains_and_ff(
            gov_decision_g,
            target_temp,
            self._ext_control,
            hvac_mode,
            error_i,
            e_p,
        )
        # After the deadtime hold: deadband shell, then no outdoor value yet; a dropout freezes gains/learning only
        if not integrator_hold and self.deadband_mgr.in_deadband and not in_core_deadband:
            integrator_hold, integrator_hold_source = True, INTEGRAL_HOLD_SOURCE_DEADBAND_SHELL
        elif not integrator_hold and self._ext_control is None:
            integrator_hold, integrator_hold_source = True, "governance_sensor_invalid"
        elif not integrator_hold and self._start.recovering:  # back to the setpoint after a calibration pulse
            integrator_hold, integrator_hold_source = True, "calibration_recovery"
        if self.vt_safety_override:  # the PI does not drive the actuator
            integrator_hold, integrator_hold_source = True, "vt_safety"

        # Deadband ENTRY: no bumpless transfer. The integral keeps its
        # naturally accumulated value and is simply frozen by compute_pwm.
        # On deadband exit the PI resumes from this authentic state and the
        # integrator fills the gap to hold power within a few cycles — the
        # building's thermal inertia absorbs the brief transient.

        # --- 11. PID Compute ---
        # Capture saturation state before compute_pwm updates it (used for SAT_HI exit detection)
        prev_sat_before_compute = self.ctl.last_sat
        u_max = self._linear_max_on()
        u_max = 1.0 if u_max is None else min(1.0, u_max)

        u_cmd = self.ctl.compute_pwm(
            error_i_ctl,
            e_p,
            self.Kp,
            self.Ki,
            u_ff,
            dt_min,
            in_deadband_now,
            integrator_hold,
            deadband_c=self.deadband_c,
            core_deadband=in_core_deadband,
            deadband_allow_p=self._deadband_allow_p,
            integrator_hold_source=integrator_hold_source,
            u_max=u_max,
            unwind_band_c=NEAR_SETPOINT_UNWIND_STEPS * self.learn_win.balance.resolution_c,
        )

        # Anti-windup on the limit known at this tick (max_on_percent).
        # Rate limit and timing quantization are not saturations.
        self.ctl.track_output_limit(u_max, self.Kp, self.Ki, dt_min)

        # --- 12. Soft Constraints ---
        u_limited = self._apply_soft_constraints(u_cmd, dt_min, setpoint_changed)
        self._last_u_limited = u_limited

        # --- 13. Timing Constraints ---
        u_final = self.update_timing_constraints(self.u_prev, u_limited)
        self._set_linear_output(u_final)
        self._last_u_applied = u_final

        # --- 13. Store the candidate output only ---
        self.ctl.finalize_cycle(u_limited, u_final)

        # --- 14. Update State & Diagnostics ---
        self._output_initialized = True
        # Note: self.integral and self.u_prev are already updated in ctl and managed via properties
        self._last_u_pi = self.ctl.u_pi
        self._last_u_ff = self.ctl.u_ff
        self._last_u_cmd = u_cmd
        self._last_current_temp = current_temp
        self._last_ext_temp = ext_current_temp
        self._last_target_temp = target_temp

        # --- 14b. SAT_HI exit tracking ---
        if self.ctl.last_sat == "NO_SAT" and prev_sat_before_compute == "SAT_HI":
            _LOGGER.debug(
                "%s - SAT_HI exit: integral stays at %.2f (natural post-conditional-integration)",
                self._name, self.ctl.integral
            )

    def _update_deadtime_episode_status(self, u_applied: float, hvac_mode: VThermHvacMode, now: float) -> None:
        """Update the heating/cooling episode starts used by in_deadtime_window."""
        self._t_heat_episode_start, self._t_cool_episode_start = update_episode_starts(
            u_applied,
            hvac_mode,
            now,
            self._t_heat_episode_start,
            self._t_cool_episode_start,
            self._name,
        )

    def get_published_diagnostics(self) -> Dict[str, Any]:
        """Return the canonical SmartPI live diagnostics."""
        return build_published_diagnostics(self)
