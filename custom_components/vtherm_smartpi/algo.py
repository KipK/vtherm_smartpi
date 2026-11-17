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
   - Analytical Setpoint Trajectory: Shapes the proportional reference toward the target.
   - Anti-windup: Conditional integration + Tracking anti-windup.

The output is a power command between 0 and 1 (0-100%).
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
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
    SmartPICalibrationPhase,
    MAX_STEP_PER_MINUTE,
    SETPOINT_BOOST_RATE,
    SETPOINT_MODE_DELTA_C,
    SMARTPI_RECALC_INTERVAL_SEC,
    LEARNING_PAUSE_RESUME_MIN,
    PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN,
    HYST_UPPER_C,
    HYST_LOWER_C,
    DEFAULT_DEADBAND_C,
    INTEGRAL_HOLD_SOURCE_DEADBAND_SHELL,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_B,
    DEFAULT_NEAR_BAND_DEG,
    CALIBRATION_TIMEOUT_MIN,
    ERROR_FILTER_TAU,
    TRAJECTORY_ENABLE_ERROR_THRESHOLD,
    NEAR_SETPOINT_UNWIND_STEPS,
    NEAR_BAND_HYSTERESIS_C,
    RESPONSE_LAG_RESTART_MAX_GAP_S,
    clamp,
)
from .smartpi.autocalib import AutoCalibTrigger, AutoCalibEvent
from .smartpi.deadtime_estimator import (
    DeadTimeEstimator,
    deadtime_window_active,
    update_episode_starts,
)
from .smartpi.ab_estimator import ABEstimator
from .smartpi.thermal_twin_1r1c import ThermalTwin1R1C
from .smartpi.diagnostics import build_published_diagnostics
from .smartpi.governance import SmartPIGovernance
from .smartpi.setpoint import SmartPISetpointManager
from .smartpi.guards import SmartPIGuards
from .smartpi.controller import SmartPIController
from .smartpi.learning_window import LearningWindowManager
from .smartpi.deadband import DeadbandManager
from .smartpi.calibration import CalibrationManager
from .smartpi.gains import GainScheduler
from .smartpi.feedforward import (
    FFResult,
    compute_ff,
    compute_ff_gain,
    compute_ff_warmup_scale,
)
from .smartpi.ff3 import FF3Result, FF3RuntimeState, compute_ff3
from .smartpi.ff3_eligibility import (
    build_ff3_disturbance_context,
)
from .smartpi.causal_power_trace import CausalPowerTrace
from .smartpi.thermal_measurement import ThermalMeasurementSource
from .smartpi.ff_trim import CausalFFTrimObserver, FFTrim
from .smartpi.ff_trim_periodic import PeriodicFFTrimObserver
from .smartpi.ff_trim_runtime import FFTrimCoordinator
from .smartpi.ff_ab_confidence import ABConfidence
from .smartpi.command_ownership import (
    CommandOwnershipSnapshot,
    CommandOwnershipTracker,
)
from .smartpi.tint_filter import AdaptiveTintFilter
from .smartpi.resume_hold import ResumeDeadtimeHold, ResumeHoldSource
from .smartpi.valve_curve import ValveCurveParams, build_valve_curve
from .smartpi.const import ENABLE_ADAPTIVE_TINT_FILTER
from .smartpi.timestamp_utils import (
    elapsed_wall_minutes,
    monotonic_to_wall_ts,
    normalize_wall_timestamp,
    wall_to_monotonic_ts,
)
from .const import HVAC_OFF_REASON_WINDOW_DETECTION
from .boiler_call import BOILER_PAUSE_CONFIRM_S

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
        # --- Setpoint trajectory shaping knobs ---
        near_band_deg: float = DEFAULT_NEAR_BAND_DEG,
        use_setpoint_filter: bool = True,
        use_ff3: bool = False,
        hysteresis_on: float = HYST_LOWER_C,
        hysteresis_off: float = HYST_UPPER_C,
        release_tau_factor: float = 0.5,
        deadband_allow_p: bool = False,
        enable_valve_linearization: bool = False,
        valve_curve_params: ValveCurveParams | None = None,
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
        self._use_ff3 = bool(use_ff3)

        # --- Sub-Systems ---
        self.gov = SmartPIGovernance(name)
        self.sp_mgr = SmartPISetpointManager(
            name, enabled=use_setpoint_filter,
            release_tau_factor=release_tau_factor,
        )
        self.ctl = SmartPIController(name)

        # Model estimator
        self.est = ABEstimator()

        # --- New Component Managers (Phase 2.5 refactoring) ---
        self.learn_win = LearningWindowManager(name)
        self.deadband_mgr = DeadbandManager(name, near_band_deg)
        self.calibration_mgr = CalibrationManager(name)
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
        self._last_temperature_slope_h: float | None = None
        self._last_u_ff: float = 0.0
        self._last_u_pi: float = 0.0
        self._last_ff_reason: str = "ff_none"
        self._last_u_ff3: float = 0.0
        self.ff3_state = FF3RuntimeState()

        self._cycle_boundary_pending: bool = True  # Force boundary on first run
        self._current_cycle_start_monotonic: float | None = None

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
        # Learning also pauses while SmartPI calls for heat and Versatile Thermostat's central
        # boiler, which heats this thermostat, is off (set by the handler).
        self._boiler_learning_paused: bool = False
        # Versatile Thermostat's central boiler state as the dead-time estimator follows it, for a
        # thermostat the boiler counts (set by the handler, which filters the boiler's blinks); None
        # when the boiler does not count this thermostat or its state cannot be read.
        self._boiler_on: bool | None = None
        # Whether the boiler's state has been known since this SmartPI started (an outage of it
        # is then told from a first reading).
        self._boiler_was_known: bool = False
        # Since when the causal power trace marks the boiler's heat as not delivered
        # (``_mark_heat_not_delivered``), None while it does not.
        self._heat_not_delivered_since: float | None = None
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
        self._command_ownership = CommandOwnershipTracker()
        self.fftrim_coord = FFTrimCoordinator(self)
        # True/False once the scheduler is known to emit receipts (or not).
        self.receipts_supported: bool | None = None
        self._last_forced_by_timing: bool = False  # True when timing forced 0%/100%
        self._output_initialized: bool = False # True once calculate() runs successfully

        # --- Dead Time (L) Support ---
        self.dt_est = DeadTimeEstimator()
        # --- Thermal Twin 1R1C (diagnostics-only) ---
        self.twin = self._new_thermal_twin()
        self._last_twin_diag: dict = {}
        self._t_heat_episode_start: float | None = None
        self._t_cool_episode_start: float | None = None
        # When the boiler pause last changed (monotonic time): learning's episodes start after it,
        # the regulation's above do not (``_learning_episode_start``).
        self._learning_restart_ts: float | None = None
        self._deadtime_skip_count_a: int = 0
        self._deadtime_skip_count_b: int = 0

        # --- Guard Manager (Phase 2.5 refactoring) ---
        self.guards = SmartPIGuards()

        # Cycle tracking
        self._setpoint_changed_in_cycle: bool = False

        # --- AutoCalibTrigger (supervision) ---
        self.autocalib = AutoCalibTrigger(name)

        # --- FFv2: trim bias + AB confidence ---
        self._ff_trim: FFTrim = FFTrim()
        self._causal_power_trace = CausalPowerTrace()
        self._thermal_measurements = ThermalMeasurementSource()
        self._fftrim_observer = CausalFFTrimObserver(
            cycle_min,
            self._causal_power_trace,
        )
        self._fftrim_periodic_observer = PeriodicFFTrimObserver(cycle_min)
        self._ab_confidence = ABConfidence()
        self._last_restart_reason: str = "none"
        self.resume_hold = ResumeDeadtimeHold()

        # Last complete FFResult (used by diagnostics and on_cycle_completed)
        self._last_ff_result: FFResult | None = None

        # Bootstrap on/off output saved before a restart (monotonic time),
        # applied by the first calculation.
        self._hysteresis_restore: dict | None = None

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

    @staticmethod
    def _new_thermal_twin() -> ThermalTwin1R1C:
        """Create the diagnostics-only thermal twin with its fixed settings."""
        return ThermalTwin1R1C(dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1)

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
        self._set_linear_output(0.0)
        self._output_initialized = False
        self._last_u_ff = 0.0
        self._last_u_pi = 0.0
        self._last_ff_reason = "ff_none"
        self._last_u_ff3 = 0.0
        self.ff3_state.reset("reset")
        self.ff3_state.clear_disturbance_context()
        self._last_u_cmd = 0.0
        self._last_u_limited = 0.0
        self._last_u_applied = 0.0
        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self.fftrim_coord.reset()
        self._command_ownership.reset()
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
        self._current_cycle_start_monotonic = None

        # Learning window state is managed by learn_win component
        self.tint_filter.reset()

        # Reset Thermal Twin
        self.twin = self._new_thermal_twin()
        self._last_twin_diag = {}

        # Reset Dead Time Estimator
        self.dt_est.reset()
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        self._learning_restart_ts = None
        self._deadtime_skip_count_a = 0
        self._deadtime_skip_count_b = 0

        # Reset guard manager
        self.guards.reset(keep_counts=True)

        # Reset new component managers (Phase 2.5 refactoring)
        self.learn_win.reset_all()
        self.est.a_model_ready = self.learn_win.response_lag.resolved
        self._hysteresis_restore = None
        self.deadband_mgr.reset()
        self.calibration_mgr.reset()
        self.gain_scheduler.reset()
        self._ff_trim.reset()
        self.fftrim_coord.reset_observation_runtime()
        self._ab_confidence.reset()
        self._last_restart_reason = "none"
        self.resume_hold.clear()

        _LOGGER.info("%s - SmartPI learning and history reset", self._name)

    @property
    def calibration_state(self) -> SmartPICalibrationPhase:
        return self.calibration_mgr.state

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
        if self.calibration_mgr.state != SmartPICalibrationPhase.IDLE:
            return SmartPIPhase.CALIBRATION
        if (
            (len(self.est.a_meas_hist) < AB_MIN_SAMPLES_A and self.est.learn_ok_count_a == 0)
            or len(self.est.b_meas_hist) < AB_MIN_SAMPLES_B
            # The first a publication waits for the delivered power model.
            or (not self.est.a_model_ready and self.est.learn_ok_count_a == 0)
        ):
            return SmartPIPhase.HYSTERESIS
        return SmartPIPhase.STABLE

    @property
    def meas_count_a(self) -> int:
        return len(self.est.a_meas_hist)

    @property
    def meas_count_b(self) -> int:
        return len(self.est.b_meas_hist)

    @property
    def _cycle_regimes(self):
        return self.gov.cycle_regimes

    def force_calibration(self) -> "AutoCalibEvent" | None:
        """Force a calibration cycle to refresh Dead Time estimation."""
        self.calibration_mgr.request_calibration(phase=self.phase)
        if self.phase != SmartPIPhase.HYSTERESIS:
            return self.autocalib.force_manual_trigger(time.time(), self)
        return None

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
        self._ff_trim.clear_pending()
        self._ff_trim.clear_last_transaction()
        self.fftrim_coord.reset_observation_runtime()
        self.reset_command_ownership("ownership_discontinuity")
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
        self._reset_learning_window()
        self.dt_est.reset_observation(self._committed_on_percent)
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        _LOGGER.info(
            "%s - SmartPI learning %s",
            self._name,
            "enabled" if enabled else "paused",
        )

    @property
    def boiler_learning_paused(self) -> bool:
        """Return whether learning pauses because the central boiler is off."""
        return self._boiler_learning_paused

    @property
    def _learning_active(self) -> bool:
        """Learning runs unless the user paused it or the central boiler does not heat."""
        return self._learning_enabled and not self._boiler_learning_paused

    def set_boiler_learning_paused(self, paused: bool) -> None:
        """Pause or resume learning while SmartPI calls for heat and the central boiler is off.

        The room then cools although the command says it heats, and learning from that time
        would be wrong. The pause only pauses learning: the learning window and the detection of
        the response to SmartPI's call in flight are dropped (a passive detection goes on), and learning's next episode starts now at the earliest;
        the dead-time estimator keeps its last stop and its temperature history, which it keeps
        recording, since the wait for the boiler is time without heat. The regulation, its
        dead-time window included, is not touched. The estimator sees the boiler's state
        itself (``set_boiler_state``), also during a calibration: nothing arms while the boiler
        is off, and dropping a heating detection at the pause's start is only a safeguard.
        """
        paused = bool(paused)
        if paused == self._boiler_learning_paused:
            return

        self._boiler_learning_paused = paused
        self._reset_learning_window()
        self._learning_restart_ts = time.monotonic()
        if paused:
            self._drop_dead_time_detection()
        _LOGGER.info(
            "%s - SmartPI learning %s (central boiler off)",
            self._name,
            "paused" if paused else "resumed",
        )

    def _drop_dead_time_detection(self) -> None:
        """Drop the detection of the response to SmartPI's call still in flight (heating, in
        COOL cooling), as the estimator does when a new power transition replaces it (outcome
        ``aborted``); its learned dead times, its last stop and its temperature history stay. A
        detection of the passive response to the boiler's stop stays: the room's heat did stop."""
        est = self.dt_est
        if self._last_hvac_mode == VThermHvacMode_COOL:
            if est.state == "WAITING_COOL_RESPONSE":
                est.last_outcome["cool"] = "aborted"
                est.state = "COOLING"
        elif est.state == "WAITING_HEAT_RESPONSE":
            est.last_outcome["heat"] = "aborted"
            est.state = "HEATING"

    @property
    def boiler_on(self) -> bool | None:
        """Versatile Thermostat's central boiler state as the dead-time estimator follows it, None
        when the boiler does not count this thermostat or its state cannot be read."""
        return self._boiler_on

    def _dead_time_command(self) -> float:
        """SmartPI's command as the dead-time estimator gets it: the committed one, or during a
        calibration the calibration's own output, which it feeds the estimator itself."""
        return self._on_percent if self.calibration_mgr.is_calibrating else self._committed_on_percent

    def _dead_time_power(self) -> float:
        """The power the dead-time estimator sees: SmartPI's command (``_dead_time_command``), or
        none while Versatile Thermostat's central boiler, which heats this thermostat, is off."""
        return 0.0 if self._boiler_on is False else self._dead_time_command()

    def _mark_heat_not_delivered(self, since: float | None = None) -> None:
        """While Versatile Thermostat's central boiler, which heats this thermostat, is off and
        SmartPI's applied power is above 0, the room does not get that power: the causal power
        trace marks the time (``central_boiler_off``), so the FF trim learns from no window that
        overlaps it. The mark ends at the next committed power and is set again while this holds;
        it starts at ``since`` when the heat is known to have ended earlier (a stop held as heat,
        or found late on a relay)."""
        if self._boiler_on is False and self._committed_on_percent > 0.0:
            now = time.monotonic()
            start = since if since is not None and since < now else now
            if self._heat_not_delivered_since is None:
                self._heat_not_delivered_since = start
            self._causal_power_trace.mark_unobserved_power(start, "central_boiler_off")

    def _end_heat_not_delivered(self, since: float | None = None) -> None:
        """The boiler heats again, from ``since`` (its start) or now: the mark of the time
        without its heat ends then, not at the next committed power, up to a cycle later. A time
        without heat shorter than the learning pause's confirmation (``BOILER_PAUSE_CONFIRM_S``)
        is dropped as a whole, also when committed powers split it into pieces: a blink of the
        boiler, or the seconds a room's own start waits for it, as for the pause."""
        now = time.monotonic()
        end = since if since is not None and since < now else now
        started, self._heat_not_delivered_since = self._heat_not_delivered_since, None
        if started is not None and end - started < BOILER_PAUSE_CONFIRM_S:
            self._causal_power_trace.discard_unobserved_power(started, "central_boiler_off")
        else:
            self._causal_power_trace.end_unobserved_power(end, "central_boiler_off")

    def relay_plan(self) -> tuple[float, float] | None:
        """The applied cycle in progress, as Versatile Thermostat's command receipts commit it:
        its start (monotonic time) and how long the device is on from it, the applied command
        times the cycle; None before a cycle is known."""
        start = self._current_cycle_start_monotonic
        if start is None:
            return None
        return start, max(0.0, self._committed_on_percent) * self._cycle_min * 60.0

    def set_boiler_state(
        self,
        on: bool | None,
        current_temp: float | None = None,
        since: float | None = None,
        blink: bool = False,
        ended_at: float | None = None,
        stopped_at: float | None = None,
    ) -> bool:
        """Versatile Thermostat's central boiler state for a thermostat it counts (None when it
        does not count it, or the state cannot be read), as the handler follows it; whether the
        power the dead-time estimator sees changed with it.

        The estimator sees no power while the boiler is off (``_dead_time_power``), and every path
        that feeds it does so. A change of the boiler that changes that power is fed at once, at
        the moment the handler gives it, so its own rules (3.6) judge it: the room's heat stops
        (``_heat_stops_with_the_boiler``) or starts (``_heat_starts_with_the_boiler``, from the
        boiler's start ``since``). A boiler back right after its stop blinked (``blink``): the
        room's heat did not stop (``_heat_back_after_a_blink``). A stop found after the room's
        heat had already ended, at ``ended_at`` (a room on a relay), is timed late
        (``_heat_stopped_earlier``). This holds during a calibration too, with the calibration's
        output as the command. A state that becomes known, or unknown, is neither a stop nor a
        start: the estimator sees the power at its next feed, by its own rules. But a state known
        again after an outage restarts the estimator's observation, as a restart does
        (``_boiler_state_known_again``), and a first reading off, while the estimator saw power,
        records the stop (``_heat_stopped_at_an_unknown_moment``). A boiler's stop the handler
        held as heat (a valve, or a relay whose heat flowed), at ``stopped_at``
        (``SmartPIHandler._valve_heat``, ``_relay_stop_held``), is dated from then
        (``_heat_stopped_at``), also when the room's own stop came during the hold.
        """
        if on is False and stopped_at is not None and self._boiler_on is True and self._dead_time_power() <= 0.01:
            # The room stopped calling during the hold: its heat stopped with the boiler.
            self._boiler_on = False
            if self.dt_est.last_power > 0.01:
                self._heat_stops_with_the_boiler(current_temp, stopped_at=stopped_at)
            else:
                self._heat_stopped_at(stopped_at)
            return True
        if on == self._boiler_on:
            return False
        known = on is not None and self._boiler_on is not None
        before = self._dead_time_power()
        self._boiler_on = on
        if on is False:
            self._mark_heat_not_delivered(ended_at if ended_at is not None else stopped_at)
        else:
            self._end_heat_not_delivered(since)
        after = self._dead_time_power()
        if not known:
            if on is not None and self._boiler_was_known:
                self._boiler_state_known_again()
            elif after <= 0.01 < self.dt_est.last_power:
                self._heat_stopped_at_an_unknown_moment()
            self._boiler_was_known = self._boiler_was_known or on is not None
            return False
        if (before > 0.01) == (after > 0.01):
            return False
        if after <= 0.01:
            self._heat_stops_with_the_boiler(current_temp, ended_at, stopped_at)
        elif blink:
            self._heat_back_after_a_blink(current_temp)
        else:
            self._heat_starts_with_the_boiler(current_temp, since)
        return True

    def _feed_dead_time_now(self, power: float, current_temp: float | None, at: float | None = None) -> bool:
        """One update of the dead-time estimator now, or at the earlier moment ``at`` but not
        before its last temperature sample, at ``power``, on SmartPI's last filtered temperature;
        whether it was fed. Not while the user's learning is off, unless a calibration runs: a
        calibration feeds the estimator in any case."""
        tin = self.tint_filter.t_int_lp
        if tin is None or not (self._learning_enabled or self.calibration_mgr.is_calibrating):
            return False
        now = time.monotonic()
        if at is not None:
            history = self.dt_est.tin_history
            now = max(min(at, now), history[-1][0]) if history else min(at, now)
        self.dt_est.update(
            now=now,
            tin=tin,
            tin_raw=current_temp,
            u_applied=power,
            hvac_mode=self._last_hvac_mode or VThermHvacMode_HEAT,
        )
        return True

    def _heat_stops_with_the_boiler(self, current_temp: float | None, ended_at: float | None = None, stopped_at: float | None = None) -> None:
        """Versatile Thermostat's central boiler went off while SmartPI calls: the room's heat
        stops now, though SmartPI's command does not. The dead-time estimator sees the power fall
        to 0 now and its own rules decide: it records the stop, and times the room's passive
        response from now if the heating had settled (3.6); a stop known to have come earlier is
        then dated back (``ended_at``: ``_heat_stopped_earlier``; ``stopped_at``:
        ``_heat_stopped_at``). A heating detection still waiting is
        dropped first: a later rise would not answer this heat."""
        now = time.monotonic()
        dropped = self.dt_est.state == "WAITING_HEAT_RESPONSE"
        self._drop_dead_time_detection()
        if not self._feed_dead_time_now(0.0, current_temp):
            # Not fed (the user's learning off, no temperature yet): the stop is recorded all the same.
            self.dt_est.last_stop_time = now
            self.dt_est.last_power = 0.0
        if ended_at is not None and ended_at < now:
            self._heat_stopped_earlier(ended_at)
        if stopped_at is not None and stopped_at < now:
            self._heat_stopped_at(stopped_at)
        _LOGGER.info(
            "%s - SmartPI heat stopped with the central boiler%s",
            self._name,
            " (heating dead-time detection aborted)" if dropped else "",
        )

    def _heat_starts_with_the_boiler(self, current_temp: float | None, since: float | None) -> None:
        """The central boiler heats the calling room from ``since``: the dead-time estimator sees
        the power step from 0 then, on SmartPI's last filtered temperature, and its own rules
        decide whether a detection arms: its power floor, its minimum time without heat since the
        last real stop (the wait for the boiler counts as time without heat) and the trend before
        the start. A detection counts from the boiler's start, not from the call that waited for
        it, also a calibration's."""
        power = self._dead_time_command()
        if not self._feed_dead_time_now(power, current_temp, at=since):
            # Not fed (the user's learning off, no temperature yet): the start is recorded all the same.
            self.dt_est.last_start_time = time.monotonic() if since is None else since
            self.dt_est.last_power = power

    def _heat_back_after_a_blink(self, current_temp: float | None) -> None:
        """The central boiler came back right after its stop: the room's heat did not stop. The
        dead-time estimator sees SmartPI's command again without a new start, so the heating
        period goes on; the detection of the passive response armed at the stop ends ``aborted``,
        as when a power change replaces a wait (3.B)."""
        power = self._dead_time_command()
        self.dt_est.last_power = power
        self._feed_dead_time_now(power, current_temp)
        _LOGGER.info(
            "%s - SmartPI heat back with the central boiler after a blink (no new start)",
            self._name,
        )

    def _heat_stopped_earlier(self, ended_at: float) -> None:
        """The stop was found after the room's heat had ended, at ``ended_at``: a room on a relay
        whose boiler did not start again at its relay's next on part. The minimum time without
        heat counts from the real end of the heat. A passive response armed now starts late, so it
        can only come out shorter than it is: as for a start on weak evidence (the estimator's
        ``_weak_evidence``), it is kept only when it does not lower the published passive dead time;
        without a published value there is nothing to judge it against, and none is timed."""
        est = self.dt_est
        est.last_stop_time = ended_at
        cool = self._last_hvac_mode != VThermHvacMode_COOL
        if est.state != ("WAITING_COOL_RESPONSE" if cool else "WAITING_HEAT_RESPONSE"):
            return
        published, _ = est.passive_deadtime(self._last_hvac_mode or VThermHvacMode_HEAT)
        if published is None:
            est.state = "COOLING" if cool else "HEATING"
            est._waiting_power_on = None
        else:
            est._weak_evidence = True

    def _heat_stopped_at(self, at: float) -> None:
        """The room's heat stopped at ``at``, the boiler's stop that the handler held as heat (a
        valve, or a relay whose heat flowed), and the estimator recorded it later: the stop, and the passive response armed by
        it, count from ``at``. A dead time counts from the onset of the step (reference 5, §2.4,
        p. 16). The response is armed only if the heating had settled by ``at``; its peak (in
        COOL, its trough) and the last moment near it come from the level the estimator's tracking
        followed since (``tin_track_history``), so that a step of the sensor not confirmed yet
        counts here as it does in the tracking (3.6)."""
        est = self.dt_est
        if est.last_stop_time is None or est.last_stop_time < at:
            return
        est.last_stop_time = at
        cool = self._last_hvac_mode != VThermHvacMode_COOL
        if est.state != ("WAITING_COOL_RESPONSE" if cool else "WAITING_HEAT_RESPONSE"):
            return
        if not est._active_period_settled(at, self._last_hvac_mode or VThermHvacMode_HEAT):
            est.state = "COOLING" if cool else "HEATING"
            est._waiting_power_on = None
            return
        before = [v for t, v in est.tin_track_history if t <= at]
        since = [(t, v) for t, v in est.tin_track_history if t > at]
        extreme = before[-1] if before else (since[0][1] if since else (est.cool_peak_temp if cool else est.heat_trough_temp))
        near = at
        for t, v in since:
            if (v > extreme) if cool else (v < extreme):
                extreme = v
            if (v >= extreme - 0.01) if cool else (v <= extreme + 0.01):
                near = t
        if cool:
            est.cool_start_time, est.cool_peak_temp = at, extreme
        else:
            est.heat_start_time, est.heat_trough_temp = at, extreme
        est._near_extreme_time = near

    def _boiler_state_known_again(self) -> None:
        """The boiler's state can be read again after it could not: whether the room got heat
        meanwhile is not known. As after a restart (``DeadTimeEstimator.load_state``), a response
        spanning the outage is not attributable: the estimator's observation restarts at the power
        it sees now, and its minimum time without heat counts from now. Unlike a restart, its
        temperature history is cleared too, which the learning windows and the trend before the
        next start read."""
        est = self.dt_est
        est.reset_observation(self._dead_time_power())
        est.last_stop_time = time.monotonic()
        _LOGGER.info("%s - SmartPI central boiler state known again: dead-time observation restarted", self._name)

    def _heat_stopped_at_an_unknown_moment(self) -> None:
        """The boiler's state becomes known, off, while the dead-time estimator saw power (fed
        SmartPI's command while the state could not be read): the room's heat stopped at a moment
        not known. The stop is recorded now, so the minimum time without heat counts from it, and
        a heating detection still waiting is dropped; no passive response is timed, as the room
        may have been cooling for a while."""
        est = self.dt_est
        self._drop_dead_time_detection()
        est.last_power = 0.0
        est.last_stop_time = time.monotonic()

    def _learning_episode_start(self, regulation_start: float | None) -> float | None:
        """Learning's episode start: the regulation's, but not before the last change of the
        boiler pause."""
        if regulation_start is None or self._learning_restart_ts is None:
            return regulation_start
        return max(regulation_start, self._learning_restart_ts)
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

        if not self._learning_active:
            return

        if a_learning_block_reason is None and setpoint_changed:
            a_learning_block_reason = "skip: setpoint transition"

        # Delegate to LearningWindowManager
        self._deadtime_skip_count_a, self._deadtime_skip_count_b = self.learn_win.update(
            dt_min=dt_min,
            current_temp=current_temp,
            ext_temp=ext_temp,
            u_active=u_active,
            ff3_active=self.ff3_state.active_cycle,
            estimator=self.est,
            dt_est=self.dt_est,
            governance=self.gov,
            now=now,
            in_deadband=self.deadband_mgr.in_deadband,
            in_near_band=self.deadband_mgr.in_near_band,
            t_heat_episode_start=self._learning_episode_start(self._t_heat_episode_start),
            t_cool_episode_start=self._learning_episode_start(self._t_cool_episode_start),
            deadtime_skip_count_a=self._deadtime_skip_count_a,
            deadtime_skip_count_b=self._deadtime_skip_count_b,
            is_calibrating=self.calibration_mgr.is_calibrating,
            is_hysteresis=(self.phase == SmartPIPhase.HYSTERESIS),
            hvac_mode=hvac_mode,
            target_temp=target_temp,
            a_learning_allowed=a_learning_block_reason is None,
            a_learning_block_reason=a_learning_block_reason,
            raw_temp=raw_temp,
            min_active_window_min=self._cycle_min,
        )

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

    def stage_command(self, hvac_mode: VThermHvacMode) -> int:
        """Freeze the control context of the next scheduler request and return its id."""
        return self._command_ownership.stage(
            self.fftrim_coord.build_command_snapshot(hvac_mode),
            self.on_percent,
        )

    def reset_command_ownership(self, reason: str = "ownership_discontinuity") -> None:
        """Discard all transient ownership requests and bindings."""
        self._command_ownership.reset(reason)
        self.fftrim_coord.transfer_pending_request_id = None

    async def on_command_receipt(self, receipt: AppliedCommandReceipt) -> None:
        """Commit one command applied by the scheduler and bind its frozen request."""
        binding = self._command_ownership.bind_receipt(receipt)
        snapshot = binding.owned_snapshot
        coord = self.fftrim_coord
        if (
            snapshot is not None
            and coord.transfer_pending_request_id is not None
            and binding.request_id > coord.transfer_pending_request_id
        ):
            coord.transfer_pending_request_id = None
        self._commit_applied_command(
            receipt.applied_on_percent,
            receipt.hvac_mode,
            snapshot,
            cycle_start=receipt.kind == CommandReceiptKind.CYCLE_START,
        )

    async def on_cycle_started(
        self,
        on_time_sec: float,
        off_time_sec: float,
        on_percent: float,
        hvac_mode: str,
        physical_on_percent: float | None = None,
    ) -> None:
        """Commit a cycle start reported by a scheduler without receipts (no ownership)."""
        del on_time_sec, off_time_sec
        self._commit_applied_command(
            physical_on_percent
            if self._valve_mode_enabled and physical_on_percent is not None
            else on_percent,
            hvac_mode,
            None,
            cycle_start=True,
        )

    def on_applied_power_updated(
        self,
        on_percent: float | None,
        hvac_mode: VThermHvacMode,
    ) -> None:
        """Commit a mid-cycle valve position read without receipts (no ownership)."""
        if on_percent is None:
            self._causal_power_trace.mark_unobserved_power(
                time.monotonic(), "missing_valve_position"
            )
            return
        self._commit_applied_command(on_percent, hvac_mode, None, cycle_start=False)

    def _feed_dead_time_at_commit(self) -> None:
        """The command Versatile Thermostat has just applied starts or stops the power the
        dead-time estimator sees: it is fed now, at the moment of the application, not at the next
        calculation, up to a calculation period later. A dead time counts from the onset of the
        step (reference 5, §2.4, p. 16). Not during a calibration, which feeds the estimator its
        own command; a power that stays on, or off, feeds nothing."""
        if self.calibration_mgr.is_calibrating:
            return
        power = self._dead_time_power()
        if (power > 0.01) != (self.dt_est.last_power > 0.01):
            # The last raw reading SmartPI got: the raw history holds only the sensor's readings.
            self._feed_dead_time_now(power, self._last_current_temp)

    def _commit_applied_command(
        self,
        actuator_power: float,
        hvac_mode: object,
        snapshot: CommandOwnershipSnapshot | None,
        *,
        cycle_start: bool,
    ) -> None:
        """Commit applied actuator power and its ownership to the causal trace."""
        now = time.monotonic()
        if cycle_start:
            self.ff3_state.start_cycle()
        linear_power = self._set_committed_actuator_output(actuator_power)
        valve = self._valve_mode_enabled
        ownership = FFTrimCoordinator.to_control_ownership_snapshot(
            snapshot,
            linear_power,
            quality="valve_segmented_linear" if valve else "switch_cycle_equivalent",
        )
        trace_quality = "valve_segmented_linear" if valve else "switch_cycle_average"
        if cycle_start:
            self._causal_power_trace.start_applied_cycle(
                now_monotonic=now,
                linear_power=linear_power,
                ownership=ownership,
                quality=trace_quality,
            )
            self._current_cycle_start_monotonic = now
        else:
            self._causal_power_trace.update_applied_power(
                now_monotonic=now,
                linear_power=linear_power,
                ownership=ownership,
                quality=trace_quality,
            )
        self._mark_heat_not_delivered()
        self._feed_dead_time_at_commit()
        self._update_deadtime_episode_status(linear_power, hvac_mode, now)
        if cycle_start:
            self._setpoint_changed_in_cycle = False
            # Reset governance regime tracking for new cycle
            self.gov.on_cycle_start()

    async def on_cycle_completed(self, e_eff: float = None, elapsed_ratio: float = 1.0, **_kw) -> None:
        """Handle end of cycle (learning)."""
        self.fftrim_coord.on_cycle_completed(
            cycle_end_now=time.monotonic(),
            e_eff=e_eff,
            elapsed_ratio=elapsed_ratio,
        )
        self._mark_heat_not_delivered()

        # Cycle accepted -> Count it
        self._cycles_since_reset += 1
        self._current_cycle_start_monotonic = None
        self._cycle_boundary_pending = True

    def _remaining_cycle_min(
        self,
        now_monotonic: float,
        cycle_boundary: bool,
    ) -> float:
        """Return the remaining time before the next cycle boundary."""
        if cycle_boundary or self._current_cycle_start_monotonic is None:
            return 0.0

        elapsed_min = max(0.0, (now_monotonic - self._current_cycle_start_monotonic) / 60.0)
        return max(0.0, self._cycle_min - elapsed_min)

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
    def u_ff3(self) -> float:
        """Last FF3 value injected into the command path."""
        return self._last_u_ff3

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

    @property
    def in_deadtime_window(self) -> bool:
        """Check if we are currently inside the estimated dead time window."""
        return deadtime_window_active(
            self.dt_est,
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
            dt_est=self.dt_est,
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

    def _calculate_forced_calibration(
        self,
        target_temp: float,
        current_temp: float,
        hvac_mode: VThermHvacMode,
        filtered_temp: float | None = None,
    ) -> None:
        """Execute the forced calibration state machine.

        Delegates to CalibrationManager component.
        """
        result = self.calibration_mgr.calculate(
            target_temp=target_temp,
            current_temp=current_temp,
            hvac_mode=hvac_mode,
            dt_est=self.dt_est,
            max_on_percent=self._linear_max_on(),
            twin_diag=self._last_twin_diag,
            boiler_off=self._boiler_on is False,
            filtered_temp=filtered_temp,
        )

        # Apply result
        if result.on_percent is not None:
            self._set_linear_output(result.on_percent)
        else:
            self._set_linear_output(0.0)

        # Update diagnostics
        self.ctl.last_i_mode = "CALIB"
        self.ctl.last_sat = "NO_SAT"
        self._last_u_ff = 0.0
        self._last_ff_reason = "ff_none"
        self._last_u_pi = self._on_percent
        self._last_u_cmd = self._on_percent
        self._last_u_limited = self._on_percent

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
            "cal_state": self.calibration_mgr.save_state(),
            "gs_state": self.gain_scheduler.save_state(),
            "twin_state": self.twin.save_state(),
            "guards_state": self.guards.save_state(),
            "ac_state": self.autocalib.save_state(),
            # FFv2 components
            "ff_v2_trim": self._ff_trim.save_state(),
            "ff_trim_delta": self.fftrim_coord.last_ff_trim_delta,
            "tint_filter_state": self.tint_filter.save_state(),
        }
        hysteresis = self._hysteresis_output_context()
        if hysteresis is not None:
            state["hysteresis_output"] = {
                "on": hysteresis["on"],
                "wall_ts": monotonic_to_wall_ts(hysteresis["ts"]),
                "hvac_mode": hysteresis["hvac_mode"],
            }
        return state

    def _hysteresis_output_context(self) -> dict | None:
        """Return the bootstrap on/off output to resume after a restart."""
        if self._hysteresis_restore is not None:
            # Not yet applied: no calculation since the previous restart.
            return self._hysteresis_restore
        if (
            self.phase != SmartPIPhase.HYSTERESIS
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

    def _take_hysteresis_restore(self, now: float, hvac_mode: VThermHvacMode | None) -> float | None:
        """Consume the saved bootstrap output; return it when it applies.

        It applies after a stop of at most RESPONSE_LAG_RESTART_MAX_GAP_S in
        the same hvac mode: over such a stop the actuator keeps its last state,
        as an uninterrupted hysteresis would have.
        """
        restore = self._hysteresis_restore
        self._hysteresis_restore = None
        if restore is None or hvac_mode is None or str(hvac_mode) != restore["hvac_mode"]:
            return None
        if not 0.0 <= now - restore["ts"] <= RESPONSE_LAG_RESTART_MAX_GAP_S:
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
        self.calibration_mgr.load_state(state.get("cal_state", {}))
        self.gain_scheduler.load_state(state.get("gs_state", {}))
        self.sp_mgr.clear_runtime_transients()
        self.ctl.clear_integral_hold()

        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self.reset_command_ownership("ownership_discontinuity")
        self.fftrim_coord.reset_observation_runtime()
        self._last_u_applied = 0.0
        self._last_restart_reason = "none"
        self.resume_hold.clear()
        self.twin.load_state(state.get("twin_state", {}))
        self._hysteresis_restore = self._load_hysteresis_output(state.get("hysteresis_output"))

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
        self.autocalib.load_state(state.get("ac_state", {}))

        # Load FF trim state; freeze for a few cycles after reboot
        self._ff_trim.load_state(state.get("ff_v2_trim", {}))
        self.fftrim_coord.last_ff_trim_delta = float(state.get("ff_trim_delta", 0.0))
        self.tint_filter.load_state(state.get("tint_filter_state", {}))
        # Freeze trim learning briefly after reboot.
        self.fftrim_coord.freeze_after_reboot()

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

    def _force_off_output(
        self,
        reason: str,
        invalidated_hvac_mode: VThermHvacMode | None,
    ) -> None:
        """Zero the output and committed command; drop FF3, FF trim and episode context."""
        self._set_linear_output(0.0)
        self._committed_on_percent = 0.0
        self._actuator_committed_on_percent = 0.0
        self.ff3_state.reset(reason)
        self.fftrim_coord.invalidate_context(
            reason,
            now_monotonic=time.monotonic(),
            hvac_mode=invalidated_hvac_mode,
        )
        self._last_u_applied = 0.0
        self.ctl.u_prev = 0.0
        self._last_u_cmd = 0.0
        self._last_u_pi = 0.0
        self._last_u_limited = 0.0
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        self._learning_restart_ts = None
        self._output_initialized = True
        self._last_calculate_time = None
        self._current_cycle_start_monotonic = None
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
            _LOGGER.warning("%s - Missing target or current temp, force 0", self._name)
            self.fftrim_coord.invalidate_context(
                "missing_sensor",
                now_monotonic=time.monotonic(),
                hvac_mode=self._last_hvac_mode,
            )
            self._set_linear_output(0.0)
            self._last_u_cmd = 0.0
            self._last_u_pi = 0.0
            self._last_u_limited = 0.0
            self.ctl.last_i_mode = "I:FREEZE(missing_sensor)"
            self._last_calculate_time = None
            return True

        if hvac_mode == VThermHvacMode_OFF:
            self._force_off_output("hvac_off", self._last_hvac_mode)
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
            self._force_off_output("power_shedding", hvac_mode)
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

        # A gap between two ticks never counts for more than one cycle
        dt_min = min(dt_min, self._cycle_min)

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
        self._last_active_wall_ts = now_wall
        return expired

    def _compute_ff3_for_cycle(
        self,
        *,
        cycle_boundary: bool,
        target_temp: float,
        current_temp: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode,
        slope_h: float | None,
        setpoint_changed: bool,
        startup_first_run: bool,
        u_base: float,
    ) -> float:
        """Return the FF3 value to inject in the current calculation."""
        if not cycle_boundary:
            return self.ff3_state.u_cycle

        ff3_context = build_ff3_disturbance_context(
            twin_diag=self._last_twin_diag,
            measured_slope_h=slope_h,
            trajectory_active=self.sp_mgr.trajectory_active,
            trajectory_source=self.sp_mgr.trajectory_source,
        )
        twin_reliable = ff3_context.prediction_usable
        ff3_result: FF3Result = compute_ff3(
            enabled=self._use_ff3,
            twin=self.twin,
            twin_reliable=twin_reliable,
            twin_initialized=self.twin.T_hat is not None,
            twin_disabled_reason=ff3_context.twin_unavailability_reason,
            tau_reliable=self._tau_reliable,
            ext_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            regime=self.gov.regime,
            in_deadband=self.deadband_mgr.in_deadband,
            in_near_band=self.deadband_mgr.in_near_band,
            disturbance_context_active=ff3_context.disturbance_active,
            disturbance_context_reason=ff3_context.reason,
            authority_factor=ff3_context.authority_factor,
            prediction_quality=ff3_context.prediction_quality,
            setpoint_changed=setpoint_changed,
            startup_first_run=startup_first_run,
            last_sat=self.ctl.last_sat,
            u_base=u_base,
            current_temp=current_temp,
            setpoint=target_temp,
            deadband_c=self.deadband_c,
            near_band_below_deg=self.deadband_mgr.near_band_below_deg,
            near_band_above_deg=self.deadband_mgr.near_band_above_deg,
            cycle_min=self._cycle_min,
            a=self.est.a,
            b=self.est.b,
            deadtime_heat_s=self.dt_est.deadtime_heat_s,
        )
        return self.ff3_state.record(ff3_result, ff3_context)

    def _disable_ff3_for_next_cycle_in_deadband(self) -> None:
        """Keep the current FF3 contribution but prevent it from being reused next cycle."""
        if self.ff3_state.disable_pending():
            _LOGGER.debug(
                "%s - Deadband active: disabling pending FF3 for the next cycle",
                self._name,
            )

    def _manage_setpoint(
        self,
        target_temp: float,
        current_temp: float,
        ext_current_temp: float | None,
        hvac_mode: VThermHvacMode,
        dt_min: float,
        now_monotonic: float,
        remaining_cycle_min: float = 0.0,
        resumed_from_off: bool = False,
        startup_first_run: bool = False,
    ) -> tuple[float, bool, float, float, float | None]:
        """Setpoint filtering and boost logic.

        Returns:
            Tuple of (target_temp_filt, setpoint_changed, error_i, error_p, old_target_temp).
            error_i: raw setpoint error (SP_brut - y), signed by hvac_mode — for integral.
            error_p: filtered setpoint error (SP_for_P - y), signed by hvac_mode — for P term.
        """
        # Filter setpoint — only apply in STABLE phase.
        # During HYSTERESIS and CALIBRATION the raw setpoint must be used directly
        # to avoid disrupting bang-bang control and model identification.

        tau_info = self.est.tau_reliability()

        allow_disturbance_trigger = False
        if self._last_target_temp is not None and self._last_current_temp is not None:
            previous_error_i = self._last_target_temp - self._last_current_temp
            if hvac_mode == VThermHvacMode_COOL:
                previous_error_i = -previous_error_i
            allow_disturbance_trigger = (
                abs(previous_error_i) < TRAJECTORY_ENABLE_ERROR_THRESHOLD
            )

        if self.phase == SmartPIPhase.STABLE and not resumed_from_off and not startup_first_run:
            stop_deadtime_s, stop_deadtime_reliable = self.dt_est.passive_deadtime(hvac_mode)
            target_temp_filt = self.sp_mgr.filter_setpoint(
                target_temp=target_temp,
                current_temp=current_temp,
                hvac_mode=hvac_mode,
                a=self.est.a,
                b=self.est.b,
                ext_current_temp=ext_current_temp,
                u_ref=self._committed_on_percent,
                stop_deadtime_s=stop_deadtime_s,
                stop_deadtime_reliable=stop_deadtime_reliable,
                tau_reliable=tau_info.reliable,
                deadband_c=self.deadband_c,
                kp=self.Kp,
                next_cycle_u_ref=self._on_percent,
                cycle_min=self._cycle_min,
                remaining_cycle_min=remaining_cycle_min,
                now_monotonic=now_monotonic,
                allow_disturbance_trigger=allow_disturbance_trigger,
            )
        else:
            # Bypass shaping outside STABLE and on the first cycle after a
            # restart path. After startup or an OFF/window-close resume the
            # controller needs the raw demand immediately; arming the
            # trajectory from T_int would make error_p start at 0 on that cycle.
            if self.phase == SmartPIPhase.STABLE and resumed_from_off:
                self.sp_mgr.arm_pending_braking_after_resume(
                    target_temp=target_temp,
                    current_temp=current_temp,
                    hvac_mode=hvac_mode,
                )
            self.sp_mgr.set_passthrough(target_temp)
            target_temp_filt = target_temp

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

        # error_i: integral error — always uses raw setpoint (Åström rule)
        # error_p: proportional error — uses filtered setpoint
        error_i = target_temp - current_temp
        error_p = target_temp_filt - current_temp
        if hvac_mode == VThermHvacMode_COOL:
            error_i = -error_i
            error_p = -error_p

        self.sp_mgr.update_boost_state(target_temp, error_i, hvac_mode)

        return target_temp_filt, setpoint_changed, error_i, error_p, old_target_temp

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
            error_p: Filtered setpoint error (SP_for_P - y) — returned as e_p for P term.

        Returns:
            e_p, the error used by the P term.
        """
        tau_info = self.est.tau_reliability()
        self._tau_reliable = tau_info.reliable

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
            dt_est=self.dt_est,
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
        current_temp: float,
        e_p: float,
        slope_h: float | None = None,
        setpoint_changed: bool = False,
        cycle_boundary: bool = False,
        startup_first_run: bool = False,
    ) -> float:
        """Calculate gains and feedforward and return the runtime feedforward."""
        # Capture old Ki for bumpless integral rescaling
        old_ki = self.gain_scheduler.ki
        near_band_ratio = self._near_band_gain_ratio(error, hvac_mode)

        # Delegate gain calculation to GainScheduler component
        tau_info = self.est.tau_reliability()
        self.gain_scheduler.calculate(
            tau_reliable=self._tau_reliable,
            tau_min=tau_info.tau_min,
            estimator=self.est,
            dt_est=self.dt_est,
            in_near_band=self.deadband_mgr.in_near_band,
            governance_decision=gov_decision_g,
            near_band_ratio=near_band_ratio,
            cycle_min=self._cycle_min,
            valve_mode_enabled=self._valve_mode_enabled,
            hvac_mode=hvac_mode,
        )
        new_ki = self.gain_scheduler.ki

        # Bumpless Gain Scheduling: Rescale integral to preserve identical energy output.
        # Any change is rescaled: with Ki above its floor the near-band taper moves
        # it by small steps, and a threshold would let Ki * I drift during holds.
        if old_ki > 0.0 and new_ki > 0.0 and old_ki != new_ki:
            self.ctl.integral = self.ctl.integral * (old_ki / new_ki)
            _LOGGER.debug(
                "%s - Ki changed from %.5f to %.5f, rescaled integral to %.4f for bumpless gain transfer",
                self._name, old_ki, new_ki, self.ctl.integral
            )

        # --- FFv2: AB confidence ---
        self._ab_confidence.evaluate(
            tau_reliable=self._tau_reliable,
            learn_ok_count_a=self.est.learn_ok_count_a,
            learn_ok_count_b=self.est.learn_ok_count_b,
        )

        # --- FFv2: compute signed k_ff from the active mono-mode model ---
        k_ff = compute_ff_gain(self.est, hvac_mode, self._tau_reliable)
        warmup_scale = compute_ff_warmup_scale(
            learn_ok_count=self.est.learn_ok_count,
            ff_warmup_ok_count=self.ff_warmup_ok_count,
            cycles_since_reset=self._cycles_since_reset,
            ff_warmup_cycles=self.ff_warmup_cycles,
        )

        # Save previous FF state before computing the new FFResult
        prev_ff_result = self._last_ff_result

        # --- FFv2: nominal FF1 + FF2 path (basis for FF3) ---
        nominal_ff_result = compute_ff(
            k_ff=k_ff,
            target_temp_ff=target_temp_ff,
            ext_temp=ext_current_temp,
            warmup_scale=warmup_scale,
            trim=self._ff_trim,
        )

        u_ff3 = self._compute_ff3_for_cycle(
            cycle_boundary=cycle_boundary,
            target_temp=target_temp_ff,
            current_temp=current_temp,
            ext_current_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            slope_h=slope_h,
            setpoint_changed=setpoint_changed,
            startup_first_run=startup_first_run,
            u_base=nominal_ff_result.u_ff_final,
        )

        # --- FFv2 + FF3: inject FF3 on top of FF1 + FF2 ---
        ff_result = replace(
            nominal_ff_result,
            u_ff3=u_ff3,
            u_ff_eff=clamp(nominal_ff_result.u_ff_final + u_ff3, 0.0, 1.0),
        )
        self._last_ff_result = ff_result
        self._last_ff_reason = ff_result.ff_reason
        self._last_u_ff3 = ff_result.u_ff3

        # Deadband must keep the structural hold power only: FF1 + FF2.
        # FF3 remains a predictive transient action and must not be maintained
        # around the setpoint.
        runtime_ff = (
            ff_result.u_ff_final
            if self.deadband_mgr.in_deadband
            else ff_result.u_ff_eff
        )

        return runtime_ff

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
        boost_ok = self.sp_mgr.boost_active and not self.sp_mgr.trajectory_active
        rate_limit = SETPOINT_BOOST_RATE if boost_ok else MAX_STEP_PER_MINUTE
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
        """
        self.ctl.last_integral_hold_source = "none"
        self.ctl.last_aw_du = 0.0

        self._last_temperature_slope_h = float(slope) if isinstance(slope, (int, float)) else None

        now = time.monotonic()
        self._ext_control = self._ext_control if ext_current_temp is None else ext_current_temp

        # --- 0. HVAC mode transition (HEAT↔COOL) → reset integral ---
        # Track active modes BEFORE validation so that missing presets in COOL
        # don't intercept the cycle and hide the transition.
        if hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            # The persisted model mode covers the first tick after a restart.
            previous_mode = self._last_hvac_mode or self.est.model_hvac_mode
            mode_changed = previous_mode is not None and str(hvac_mode) != str(previous_mode)
            if mode_changed:
                self.ctl.reset()
                self._ff_trim.clear_pending()
                self._ff_trim.clear_last_transaction()
                _LOGGER.info(
                    "%s - HVAC mode changed (%s → %s): PI state reset",
                    self._name, previous_mode, hvac_mode
                )
            self._last_hvac_mode = hvac_mode
            model_reset = self.est.ensure_hvac_mode(hvac_mode)
            self.dt_est.ensure_hvac_mode(hvac_mode)
            if mode_changed or model_reset is True:
                self.fftrim_coord.reset_observation_runtime()
                self.reset_command_ownership("ownership_context_changed")
            if model_reset is True:
                self._ab_confidence.reset()
                self._ff_trim.reset()
                self._cycles_since_reset = 0
                self._session_learn_ok_count_base = self.est.learn_ok_count
                self._session_learn_skip_count_base = self.est.learn_skip_count
                self.twin = self._new_thermal_twin()
                self._last_twin_diag = {}
                _LOGGER.info(
                    "%s - HVAC signed-model migration: reset mode-dependent A, FF trim and twin state",
                    self._name,
                )

        # The saved bootstrap output only applies to the first calculation that
        # has both temperatures; it stays pending while a sensor is missing.
        hysteresis_restore = (
            None
            if current_temp is None or target_temp is None
            else self._take_hysteresis_restore(now, hvac_mode)
        )

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
                if self.phase != SmartPIPhase.HYSTERESIS and not self.calibration_mgr.is_calibrating:
                    self.dt_est.update(
                        now=now,
                        tin=t_int_lp,
                        tin_raw=current_temp,
                        u_applied=self._dead_time_power(),
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
            self.fftrim_coord.invalidate_context(
                "guard_cut",
                now_monotonic=now,
                hvac_mode=hvac_mode,
            )
            self.ctl.last_i_mode = "I:OVERRIDE(guard_cut)"
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
        dt_min, resumed_from_off, startup_first_run, resume_hold_source = self._update_time_tracking(now)

        # --- 2. Predict Cycle Trigger ---
        # Evaluate if we need to cross a cycle boundary (bridged from on_cycle_completed or forced).
        if self._cycle_boundary_pending:
            cycle_boundary = True
            self._cycle_boundary_pending = False
            _LOGGER.debug("%s - Cycle boundary triggered for FF3 prediction", self._name)

        remaining_cycle_min = self._remaining_cycle_min(now, cycle_boundary)

        # --- 3. Setpoint Management ---
        target_temp_filt, setpoint_changed, error_i, error_p, old_target_temp = self._manage_setpoint(
            target_temp,
            current_temp,
            self._ext_control,
            hvac_mode,
            dt_min,
            now,
            remaining_cycle_min,
            resumed_from_off,
            startup_first_run,
        )

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

        # --- 4. Learning & Calibration ---
        # The passive response after a stop is timed only once the active period has settled,
        # by the same gate as an active learning window (DeadTimeEstimator._active_settling_s).
        self.dt_est.active_settle_s = self.learn_win.response_lag.active_settle_s
        # The dead-time estimator is fed also during the boiler pause: it then records the
        # temperature and sees no power, since the boiler is off (_dead_time_power).
        if (
            self._learning_enabled
            and self.phase != SmartPIPhase.HYSTERESIS
            and not self.calibration_mgr.is_calibrating
        ):
            # During forced calibration, the deadtime estimator must be driven
            # only by the calibration state machine outputs. Feeding it first
            # with the stale committed cycle value can create a false 1 -> 0 -> 1
            # transient when calculate() is called twice before the scheduler
            # commits the new cycle.
            self.dt_est.update(
                now=now,
                tin=t_int_lp,
                tin_raw=current_temp,
                u_applied=self._dead_time_power(),
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

        # Calibration state machine
        if self.calibration_mgr.is_calibrating and self.calibration_mgr.calibration_start_time is not None:
            elapsed = (now - self.calibration_mgr.calibration_start_time) / 60.0
            if elapsed > CALIBRATION_TIMEOUT_MIN:
                _LOGGER.warning("%s - Calibration timeout after %.1f minutes", self._name, elapsed)
                self.calibration_mgr.handle_timeout()

        self.calibration_mgr.check_and_start(
            now=now,
            phase=self.phase,
        )

        if self.calibration_mgr.is_calibrating:
            self.fftrim_coord.invalidate_context(
                "calibration",
                now_monotonic=now,
                hvac_mode=hvac_mode,
            )
            self.ctl.clear_integral_hold()
            self._calculate_forced_calibration(
                target_temp, current_temp, hvac_mode, filtered_temp=t_int_lp
            )
            self._output_initialized = True
            self.ctl.last_i_mode = "calibration"
            self._last_target_temp = target_temp
            self._last_current_temp = current_temp
            self._last_ext_temp = ext_current_temp
            # Refresh tau_reliable for twin diagnostics
            self._tau_reliable = self.est.tau_reliability().reliable
            # Update Twin Diagnostics even in calibration
            self._update_twin_diagnostics(current_temp, ext_current_temp, target_temp, hvac_mode, dt_s=dt_min * 60.0)
            return

        # --- 5. Hysteresis Phase ---
        if self.phase == SmartPIPhase.HYSTERESIS:
            self.fftrim_coord.invalidate_context(
                "hysteresis",
                now_monotonic=now,
                hvac_mode=hvac_mode,
            )
            out = self.ctl.calculate_hysteresis(target_temp_filt, current_temp, hvac_mode, self._hyst_off, self._hyst_on)
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
                    u_applied=self._dead_time_power(),
                    hvac_mode=hvac_mode,
                )

            # Update Twin Diagnostics even in hysteresis
            self._update_twin_diagnostics(current_temp, ext_current_temp, target_temp, hvac_mode, dt_s=dt_min * 60.0)
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
            dt_est=self.dt_est,
            ctl=self.ctl,
            in_deadtime_window=lambda: self.in_deadtime_window,
        )
        if in_deadband_now and not cycle_boundary:
            self._disable_ff3_for_next_cycle_in_deadband()

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
            current_temp,
            e_p,
            slope,
            setpoint_changed=setpoint_changed,
            cycle_boundary=cycle_boundary,
            startup_first_run=startup_first_run,
        )
        # After the deadtime hold: deadband shell, then no outdoor value yet; a dropout freezes gains/learning only
        if not integrator_hold and self.deadband_mgr.in_deadband and not in_core_deadband:
            integrator_hold, integrator_hold_source = True, INTEGRAL_HOLD_SOURCE_DEADBAND_SHELL
        elif not integrator_hold and self._ext_control is None:
            integrator_hold, integrator_hold_source = True, "governance_sensor_invalid"
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
            error_i,
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

        # --- 14c. FFv2: freeze management for trim ---
        self.fftrim_coord.update_trim_freeze(regime)

        # --- 14d. Causally aligned FF trim observation ---
        self.fftrim_coord.record_thermal_measurement(
            now_monotonic=now,
            measurement_id=temperature_measure_id,
            current_temp=current_temp,
            target_temp=target_temp,
            ext_current_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            setpoint_changed=setpoint_changed,
        )

        # --- 15. Thermal Twin & ETA (diagnostics-only) ---
        self._update_twin_diagnostics(current_temp, ext_current_temp, target_temp, hvac_mode, dt_s=dt_min * 60.0)

    def _update_twin_diagnostics(
        self,
        current_temp: float,
        ext_current_temp: float | None,
        target_temp: float,
        hvac_mode: VThermHvacMode,
        dt_s: float = 0.0,
    ) -> None:
        """Update thermal twin and compute ETA best-case (diagnostics-only)."""
        mode = "heat" if hvac_mode == VThermHvacMode_HEAT else "cool"
        self._last_twin_diag = self.twin.update_with_eta(
            tin=current_temp,
            text=ext_current_temp,
            target=target_temp,
            on_percent=self._on_percent,
            tau_reliable=self._tau_reliable,
            a=self.est.a,
            b=self.est.b,
            mode=mode,
            deadtime_heat_s=self.dt_est.deadtime_heat_s,
            deadtime_cool_s=self.dt_est.deadtime_cool_s,
            deadtime_heat_reliable=self.dt_est.deadtime_heat_reliable,
            deadtime_cool_reliable=self.dt_est.deadtime_cool_reliable,
            dt_s=dt_s,
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
