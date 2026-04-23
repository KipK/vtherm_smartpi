########################################################################
#                                                                      #
#                      Smart-PI Algorithm                              #
#                      ------------------                              #
#  Auto-adaptive PI controller for Versatile Thermostat (VTH).         #
#                                                                      #
########################################################################

"""
SmartPI Algorithm (v2) - Auto-adaptive PI controller for Versatile Thermostat.

This module implements a duty-cycle PI controller for slow thermal systems (heating),
with on-line identification of a 1st-order loss model + dead time.

Key features (v2)
-----------------
1. Hybrid Learning Phases:
   - Hysteresis: Initial ON/OFF control to generate strong signal for learning A/B.
   - Stable: Adaptive PI control once model is reliable (31+ samples).

2. Measurement & Modeling:
   - Online learning of heating efficacy (a) and loss (b) via robust Median + MAD estimation.
   - Automatic Dead Time (L) estimation using Takeoff (Primary) and SK (Backup).

3. Adaptive Control:
   - Feed-forward compensation based on outdoor temperature.
   - PI gains automatically tuned based on inertia (Tau) and Dead Time (IMC-like).
   - Auto-adaptive Near-Band: Reduces gains near setpoint to prevent overshoot,
     sized dynamically based on Dead Time.

4. Comfort & Protections:
   - Setpoint Boost: Faster reaction to manual setpoint increases (>0.3°C).
   - Thermal Guard: Prevents integral windup on setpoint decreases.
   - Analytical Setpoint Trajectory: Shapes the proportional reference toward the target.
   - Anti-windup: Conditional integration + Tracking anti-windup.

The output is a power command between 0 and 1 (0-100%).
"""

from __future__ import annotations

import logging
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

from .smartpi.const import (
    SmartPIPhase,
    GovernanceRegime,
    GovernanceDecision,
    SmartPICalibrationPhase,
    TrajectoryPhase,
    KP_SAFE,
    KI_SAFE,
    KI_MIN,
    MAX_STEP_PER_MINUTE,
    SETPOINT_BOOST_RATE,
    SKIP_CYCLES_AFTER_RESUME,
    SMARTPI_RECALC_INTERVAL_SEC,
    LEARNING_PAUSE_RESUME_MIN,
    PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN,
    HYST_UPPER_C,
    HYST_LOWER_C,
    DEFAULT_DEADBAND_C,
    AB_HISTORY_SIZE,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_A_CONVERGED,
    AB_MIN_SAMPLES_B,
    DEFAULT_NEAR_BAND_DEG,
    DEFAULT_KP_NEAR_FACTOR,
    DEFAULT_KI_NEAR_FACTOR,
    CALIBRATION_TIMEOUT_MIN,
    AW_TRACK_TAU_S,
    AW_TRACK_MAX_DELTA_I,
    ERROR_FILTER_TAU,
    TRAJECTORY_ENABLE_ERROR_THRESHOLD,
    clamp,
)
from .smartpi.autocalib import AutoCalibTrigger, AutoCalibEvent
from .smartpi.deadtime_estimator import DeadTimeEstimator
from .smartpi.ab_estimator import ABEstimator
from .smartpi.thermal_twin_1r1c import ThermalTwin1R1C
from .smartpi.diagnostics import build_diagnostics, build_published_diagnostics, build_debug_diagnostics
from .smartpi.governance import SmartPIGovernance
from .smartpi.setpoint import SmartPISetpointManager
from .smartpi.guards import SmartPIGuards
from .smartpi.controller import SmartPIController
from .smartpi.learning_window import LearningWindowManager
from .smartpi.deadband import DeadbandManager
from .smartpi.calibration import CalibrationManager
from .smartpi.gains import GainScheduler
from .smartpi.feedforward import compute_ff, FFResult
from .smartpi.ff3 import FF3Result, compute_ff3
from .smartpi.ff3_eligibility import (
    build_ff3_disturbance_context,
    get_ff3_twin_unavailability_reason,
)
from .smartpi.ff_trim import FFTrim
from .smartpi.ff_ab_confidence import ABConfidence
from .smartpi.tint_filter import AdaptiveTintFilter
from .smartpi.integral_guard import (
    IntegralGuardSource,
    SmartPIIntegralGuard,
    integral_guard_release_error_threshold,
)
from .smartpi.const import (
    ABConfidenceState,
    FF_TRIM_REBOOT_FREEZE_CYCLES,
    FF_TRIM_K_ERROR,
    FF_TRIM_MAX_ERROR_C,
    FF_TRIM_MAX_SLOPE_H,
    ENABLE_ADAPTIVE_TINT_FILTER,
)
from .smartpi.timestamp_utils import convert_monotonic_to_wall_ts
from .const import HVAC_OFF_REASON_WINDOW_DETECTION

_LOGGER = logging.getLogger(__name__)


class SmartPI:
    """
    SmartPI Algorithm - Auto-adaptive PI controller for Versatile Thermostat (VTH).

    Public API
    ----------
    - calculate(...): compute the next duty-cycle command in [0,1]
    - get_diagnostics(): return a dict of key internal values for UI/attributes
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
        ff_scale_unreliable_max: float = 0.30,
        # --- Setpoint trajectory shaping knobs ---
        setpoint_weight_b: float = 0.3,
        near_band_deg: float = DEFAULT_NEAR_BAND_DEG,
        kp_near_factor: float = DEFAULT_KP_NEAR_FACTOR,
        ki_near_factor: float = DEFAULT_KI_NEAR_FACTOR,
        sign_flip_leak: float = 0.40,
        sign_flip_leak_cycles: int = 3,
        sign_flip_band_mult: float = 2.0,
        use_setpoint_filter: bool = True,
        use_ff3: bool = False,
        hysteresis_on: float = HYST_LOWER_C,
        hysteresis_off: float = HYST_UPPER_C,
        release_tau_factor: float = 0.5,
        deadband_allow_p: bool = False,
        debug_mode: bool = False,
    ) -> None:
        self._hass = hass
        self._name = name
        self._cycle_min = cycle_min
        self._hyst_on = hysteresis_on
        self._hyst_off = hysteresis_off
        self._deadband_allow_p = bool(deadband_allow_p)
        self._debug_mode = debug_mode

        self.deadband_c = float(deadband_c)

        self._minimal_activation_delay = int(minimal_activation_delay)
        self._minimal_deactivation_delay = int(minimal_deactivation_delay)
        self._max_on_percent = max_on_percent

        # FF progressive enablement
        self.ff_warmup_ok_count = max(int(ff_warmup_ok_count), 1)
        self.ff_warmup_cycles = max(int(ff_warmup_cycles), 1)
        self.ff_scale_unreliable_max = clamp(float(ff_scale_unreliable_max), 0.0, 1.0)
        self._cycles_since_reset: int = 0

        # 2DOF / scheduling parameters
        self.setpoint_weight_b = clamp(float(setpoint_weight_b), 0.0, 1.0)
        self.near_band_deg = max(float(near_band_deg), 0.0)
        self.kp_near_factor = clamp(float(kp_near_factor), 0.1, 1.0)
        self.ki_near_factor = clamp(float(ki_near_factor), 0.1, 1.0)
        self.sign_flip_leak = clamp(float(sign_flip_leak), 0.0, 1.0)
        self.sign_flip_leak_cycles = max(int(sign_flip_leak_cycles), 0)
        self.sign_flip_band_mult = max(float(sign_flip_band_mult), 0.0)
        self._use_setpoint_filter = use_setpoint_filter
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
        self.integral_guard = SmartPIIntegralGuard(name)

        self._e_filt: Optional[float] = None

        # Outputs (duty-cycle only, timing calculated by handler)
        self._on_percent: float = 0.0
        self._committed_on_percent: float = 0.0
        self._pending_fftrim_cycle_sample: dict[str, Any] | None = None

        # Diagnostics / status
        self._last_error: float = 0.0
        self._last_error_p: float = 0.0
        self._last_u_ff: float = 0.0
        self._last_u_pi: float = 0.0
        self._last_ff_raw: float = 0.0
        self._last_ff_reason: str = "ff_none"
        self._last_u_ff3: float = 0.0
        self._u_ff3_cycle: float = 0.0
        self._u_ff3_pending: float = 0.0
        self._ff3_active_cycle: bool = False
        self._ff3_pending_active: bool = False

        self._cycle_boundary_pending: bool = True  # Force boundary on first run
        self._current_cycle_start_monotonic: float | None = None

        self._last_ff3_enabled: bool = False
        self._last_ff3_reason_disabled: str = "config_disabled"
        self._last_ff3_candidate_scores: list[dict[str, float]] = []
        self._last_ff3_selected_candidate: float = 0.0
        self._last_ff3_horizon_cycles: int = 1
        self._last_ff3_disturbance_active: bool = False
        self._last_ff3_disturbance_reason: str = "none"
        self._last_ff3_disturbance_kind: str = "none"
        self._last_ff3_residual_persistent: bool = False
        self._last_ff3_dynamic_coherent: bool = False
        self._tau_reliable: bool = False
        self._sign_flip_active: bool = False
        self._last_integral_guard_mode: str = "off"

        # Track last time calculate() was executed for dt-based integration
        self._last_calculate_time: Optional[float] = None
        # Accumulated time for cycle counting (used for FF warm-up)
        self._accumulated_dt: float = 0.0

        # Timestamp for robust learning dt calculation
        self._learn_last_ts: float | None = None

        # Track last target temp for learning invalidation
        self._last_target_temp = None

        # Track last sensor temperature (unrounded) used in calculations
        self._last_current_temp: float | None = None
        self._last_ext_temp: float | None = None

        # Track last HVAC mode for integral reset on HEAT/COOL transitions
        self._last_hvac_mode: VThermHvacMode | None = None

        # Learning start timestamp
        self._learning_start_date: Optional[datetime] = datetime.now()

        # Helper to distinguish Startup (Init) from Resume (OFF->ON)
        # We want to pause learning on Resume, but NOT on Startup/Reboot
        self._startup_grace_period: bool = True
        # Anti-windup deadtime transition guard: True when the previous cycle had
        # integrator hold due to deadtime window. Used to block AW on the first
        # cycle after exiting the window (avoids massive catch-up correction).
        self._prev_deadtime_hold: bool = False

        # Tracking anti-windup diagnostics
        self._last_u_cmd: float = 0.0       # command after [0,1] clamp
        self._last_u_limited: float = 0.0   # after rate-limit and max_on_percent
        self._last_u_applied: float = 0.0   # candidate output after timing constraints
        self._last_aw_du: float = 0.0       # tracking delta for diagnostics
        self._last_ff_trim_delta: float = 0.0  # last slope-based trim signal
        self._last_fftrim_cycle_admissible: bool = False
        self._last_fftrim_reject_reason: str = "none"
        self._last_fftrim_update_reason: str = "none"
        self._cycles_since_fftrim_update: int = 0
        self._last_forced_by_timing: bool = False  # True when timing forced 0%/100%
        self._output_initialized: bool = False # True once calculate() runs successfully

        # --- Dead Time (L) Support ---
        self.dt_est = DeadTimeEstimator()
        # --- Thermal Twin 1R1C (diagnostics-only) ---
        self.twin = ThermalTwin1R1C(
            dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1
        )
        self._last_twin_diag: dict = {}
        self._heat_request_prev: bool = False
        self._t_heat_episode_start: float | None = None
        self._t_cool_episode_start: float | None = None
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
        self._ab_confidence = ABConfidence()
        self._recovery_hold_armed: bool = False
        self._last_restart_reason: str = "none"
        self._resume_deadtime_hold_source: IntegralGuardSource = IntegralGuardSource.NONE
        self._resume_deadtime_hold_started: bool = False

        # Last complete FFResult (used by diagnostics and on_cycle_completed)
        self._last_ff_result: FFResult | None = None

        self._last_regime_prev: str = ""

        # Persistent saturation counter
        self._sat_persistent_cycles: int = 0

        # FFv2 post-reboot freeze countdown (cycles remaining before unfreeze)
        self._ff_v2_reboot_freeze_remaining: int = 0

        # --- Safety-First Governance (Delegated to self.gov) ---
        if saved_state:
            self.load_state(saved_state)

        # Legacy attributes for tests
        self._prev_kp = self.Kp
        self._prev_ki = self.Ki

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
        if self.ctl:
            self.ctl.reset()
        if self.sp_mgr:
            self.sp_mgr.reset()
        if self.gov:
            self.gov.reset()

        self._last_error = 0.0
        self._last_error_p = 0.0
        self._on_percent = 0.0
        self._output_initialized = False
        self._last_u_ff = 0.0
        self._last_u_pi = 0.0
        self._last_ff_raw = 0.0
        self._last_ff_reason = "ff_none"
        self._last_u_ff3 = 0.0
        self._u_ff3_cycle = 0.0
        self._u_ff3_pending = 0.0
        self._ff3_active_cycle = False
        self._ff3_pending_active = False
        self._last_ff3_enabled = False
        self._last_ff3_reason_disabled = "reset"
        self._last_ff3_candidate_scores = []
        self._last_ff3_selected_candidate = 0.0
        self._last_ff3_horizon_cycles = 1
        self._last_ff3_disturbance_active = False
        self._last_ff3_disturbance_reason = "none"
        self._last_ff3_disturbance_kind = "none"
        self._last_ff3_residual_persistent = False
        self._last_ff3_dynamic_coherent = False
        self._last_u_cmd = 0.0
        self._last_u_limited = 0.0
        self._last_u_applied = 0.0
        self._committed_on_percent = 0.0
        self._pending_fftrim_cycle_sample = None
        self._last_aw_du = 0.0
        self._last_ff_trim_delta = 0.0
        self._last_fftrim_cycle_admissible = False
        self._last_fftrim_reject_reason = "none"
        self._last_fftrim_update_reason = "none"
        self._cycles_since_fftrim_update = 0
        self._e_filt = None
        self._cycles_since_reset = 0
        self._accumulated_dt = 0.0
        self._prev_kp = KP_SAFE
        self._prev_ki = KI_SAFE

        # Reset learning states
        self._last_calculate_time = None
        self._learn_last_ts = None
        self._last_target_temp = None
        self._last_current_temp = None
        self._last_ext_temp = None
        self._last_hvac_mode = None
        self._learning_start_date = datetime.now()
        self._current_cycle_start_monotonic = None

        # Learning window state is managed by learn_win component
        self.tint_filter.reset()

        # Reset Thermal Twin
        self.twin = ThermalTwin1R1C(
            dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1
        )
        self._last_twin_diag = {}

        # Reset Dead Time Estimator
        self.dt_est.reset()
        self._heat_request_prev = False
        self._t_heat_episode_start = None
        self._t_cool_episode_start = None
        self._deadtime_skip_count_a = 0
        self._deadtime_skip_count_b = 0

        if self.gov:
            self.gov.reset()

        # Reset guard manager
        self.guards.reset(keep_counts=True)

        # Reset new component managers (Phase 2.5 refactoring)
        if self.learn_win:
            self.learn_win.reset_all()
        if self.deadband_mgr:
            self.deadband_mgr.reset()
        if self.calibration_mgr:
            self.calibration_mgr.reset()
        if self.gain_scheduler:
            self.gain_scheduler.reset()
        if self.integral_guard:
            self.integral_guard.reset()
        self._ff_trim.reset()
        self._ab_confidence.reset()
        self._recovery_hold_armed = False
        self._last_restart_reason = "none"
        self._resume_deadtime_hold_source = IntegralGuardSource.NONE
        self._resume_deadtime_hold_started = False
        self._last_integral_guard_mode = "off"

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
    def _current_governance_regime(self) -> str:
        return self.gov.regime.value if hasattr(self.gov.regime, 'value') else str(self.gov.regime)

    @_current_governance_regime.setter
    def _current_governance_regime(self, value: str):
        self.gov.regime = GovernanceRegime(value) if isinstance(value, str) else value

    @property
    def phase(self) -> str:
        if self.calibration_mgr.state != SmartPICalibrationPhase.IDLE:
            return SmartPIPhase.CALIBRATION
        if len(self.est.a_meas_hist) < AB_MIN_SAMPLES_A or len(self.est.b_meas_hist) < AB_MIN_SAMPLES_B:
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

    def notify_resume_after_interruption(self, skip_cycles: int = None) -> None:
        """Notify SmartPI that the thermostat is resuming after an interruption.

        This is called when the thermostat resumes after a window close event
        or similar interruption. It sets a deadline before which learning is ignored.

        Args:
            skip_cycles: Legacy argument (count of cycles). Converted to duration approx.
        """
        if skip_cycles is None:
            skip_cycles = SKIP_CYCLES_AFTER_RESUME

        # Robust conversion: assume at least 15 min per cycle equivalent if cycle_min is small,
        # or use cycle_min. This is a heuristic.
        duration_min = float(skip_cycles) * max(self._cycle_min, 15.0)
        self.learn_win.set_learning_resume_ts(time.monotonic() + (duration_min * 60.0))

        # Also reset the learning timestamp to avoid using stale dt
        self._learn_last_ts = None

        # Compute wall-clock time for logging/diagnostics
        try:
            # Just for logging
            resume_dt_log = datetime.now().timestamp() + (duration_min * 60.0)
            resume_dt_iso = datetime.fromtimestamp(resume_dt_log).isoformat()
        except (ValueError, TypeError, OverflowError) as e:
            _LOGGER.warning("Could not format resume time for logging: %s", e)
            resume_dt_iso = "unknown"

        _LOGGER.info("%s - SmartPI notified of resume after interruption, skipping learning until (approx) %s", self._name, resume_dt_iso)

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

    @staticmethod
    def _signed_recovery_slope_h(
        slope_h: float | None,
        hvac_mode: VThermHvacMode | None,
    ) -> float | None:
        """Return a signed slope where positive means moving toward the target."""
        if slope_h is None:
            return None
        if hvac_mode == VThermHvacMode_COOL:
            return -float(slope_h)
        return float(slope_h)

    def _arm_integral_guard(
        self,
        source: IntegralGuardSource,
        *,
        block_positive: bool = True,
    ) -> None:
        """Arm the directional integral guard for a recovery phase."""
        if source == IntegralGuardSource.NONE:
            return
        self.integral_guard.arm(source, block_positive=block_positive)

    def _update_integral_guard(
        self,
        *,
        error_i: float,
        error_p: float,
        hvac_mode: VThermHvacMode,
        slope_h: float | None,
        in_core_deadband: bool,
        trajectory_active: bool,
    ) -> tuple[bool, bool, str]:
        """Return which integral direction must stay blocked."""
        release_error = error_p if trajectory_active else error_i
        decision = self.integral_guard.decide(
            raw_error=error_i,
            release_error=release_error,
            deadband_c=self.deadband_c,
            in_core_deadband=in_core_deadband,
            signed_recovery_slope_h=self._signed_recovery_slope_h(slope_h, hvac_mode),
        )
        self._last_integral_guard_mode = decision.mode_suffix
        return decision.block_positive, decision.block_negative, decision.mode_suffix

    def _should_arm_recovery_guard_after_deadtime(
        self,
        *,
        error_i: float,
        in_core_deadband: bool,
    ) -> bool:
        """Return True when a post-deadtime residual error still justifies a guard."""
        if in_core_deadband or error_i <= 0.0:
            return False
        return abs(error_i) > integral_guard_release_error_threshold(self.deadband_c)

    def _update_resume_deadtime_hold(
        self,
        *,
        error_i: float,
        in_core_deadband: bool,
        hvac_mode: VThermHvacMode,
    ) -> None:
        """Manage the temporary I:HOLD window after a window/shedding resume."""
        source = self._resume_deadtime_hold_source
        if source == IntegralGuardSource.NONE:
            return

        if hvac_mode != VThermHvacMode_HEAT:
            if self.ctl.integral_hold_mode == source.value:
                self.ctl.clear_integral_hold()
            if self._should_arm_recovery_guard_after_deadtime(
                error_i=error_i,
                in_core_deadband=in_core_deadband,
            ):
                self._arm_integral_guard(source)
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            return

        deadtime_heat_ready = (
            self.dt_est.deadtime_heat_reliable
            and self.dt_est.deadtime_heat_s is not None
            and self.dt_est.deadtime_heat_s > 0.0
        )
        if not deadtime_heat_ready:
            if self.ctl.integral_hold_mode == source.value:
                self.ctl.clear_integral_hold()
            if self._should_arm_recovery_guard_after_deadtime(
                error_i=error_i,
                in_core_deadband=in_core_deadband,
            ):
                self._arm_integral_guard(source)
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            return

        if not self._resume_deadtime_hold_started:
            self.ctl.start_integral_hold(source.value)

        if self.in_deadtime_window:
            self._resume_deadtime_hold_started = True
            return

        if self._resume_deadtime_hold_started:
            if self.ctl.integral_hold_mode == source.value:
                self.ctl.clear_integral_hold()
            if self._should_arm_recovery_guard_after_deadtime(
                error_i=error_i,
                in_core_deadband=in_core_deadband,
            ):
                self._arm_integral_guard(source)
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            return

        if not self._should_arm_recovery_guard_after_deadtime(
            error_i=error_i,
            in_core_deadband=in_core_deadband,
        ):
            if self.ctl.integral_hold_mode == source.value:
                self.ctl.clear_integral_hold()
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False

    def update_learning(
        self,
        dt_min: float,
        current_temp: float,
        ext_temp: float,
        u_active: float,
        setpoint_changed: bool = False
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
        """
        now = time.monotonic()

        # Delegate to LearningWindowManager
        self._deadtime_skip_count_a, self._deadtime_skip_count_b = self.learn_win.update(
            dt_min=dt_min,
            current_temp=current_temp,
            ext_temp=ext_temp,
            u_active=u_active,
            ff3_active=self._ff3_active_cycle,
            setpoint_changed=setpoint_changed,
            estimator=self.est,
            dt_est=self.dt_est,
            governance=self.gov,
            learning_resume_ts=self.learn_win.learning_resume_ts,
            now=now,
            in_deadband=self.deadband_mgr.in_deadband,
            in_near_band=self.deadband_mgr.in_near_band,
            t_heat_episode_start=self._t_heat_episode_start,
            t_cool_episode_start=self._t_cool_episode_start,
            deadtime_skip_count_a=self._deadtime_skip_count_a,
            deadtime_skip_count_b=self._deadtime_skip_count_b,
            is_calibrating=self.calibration_mgr.is_calibrating,
            is_hysteresis=(self.phase == SmartPIPhase.HYSTERESIS),
        )

    async def on_cycle_started(self, on_time_sec: float, off_time_sec: float, on_percent: float, hvac_mode: str) -> None:
        """Called when a cycle starts."""
        cycle_start_now = time.monotonic()
        self._u_ff3_cycle = self._u_ff3_pending
        self._ff3_active_cycle = self._ff3_pending_active
        self._committed_on_percent = on_percent
        self.u_prev = on_percent
        self._last_u_applied = on_percent
        self._current_cycle_start_monotonic = cycle_start_now
        self._update_deadtime_episode_status(on_percent, hvac_mode, cycle_start_now)
        self._setpoint_changed_in_cycle = False
        # Reset governance regime tracking for new cycle
        self.gov.on_cycle_start()

    async def on_cycle_completed(self, e_eff: float = None, elapsed_ratio: float = 1.0, cycle_duration_min: float = None, **_kw) -> None:
        """Handle end of cycle (learning)."""
        fftrim_cycle_sample = self._pending_fftrim_cycle_sample

        if e_eff is not None:
            # Use the nominal cycle duration (provided by the scheduler) as dt_min for Astrom tracking.
            # This is more accurate than measuring time since last calculate(), which can be short
            # when a recalc timer fires mid-cycle.
            self.update_realized_power(u_applied=e_eff, dt_min=cycle_duration_min, forced_by_timing=False, elapsed_ratio=elapsed_ratio)

        admissible, reason = self._evaluate_fftrim_cycle(fftrim_cycle_sample, e_eff, elapsed_ratio)
        self._last_fftrim_cycle_admissible = admissible
        if admissible and fftrim_cycle_sample is not None:
            slope_h = fftrim_cycle_sample.get("slope_h")
            error = fftrim_cycle_sample.get("error", 0.0)
            a_eff = max(abs(self.est.a), 1e-6)
            delta_slope = -(slope_h / 60.0) / a_eff if slope_h is not None else 0.0
            delta_error = FF_TRIM_K_ERROR * error
            delta_power = delta_slope + delta_error
            self._last_ff_trim_delta = delta_power
            self._ff_trim.update(delta_power, fftrim_cycle_sample["u_ff1"])
            self._last_fftrim_update_reason = "updated"
            self._last_fftrim_reject_reason = "none"
            self._cycles_since_fftrim_update = 0
        else:
            self._last_fftrim_reject_reason = reason
            self._last_fftrim_update_reason = "skipped"
            self._cycles_since_fftrim_update += 1

        self._pending_fftrim_cycle_sample = None

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

    def _evaluate_fftrim_cycle(
        self,
        sample: dict[str, Any] | None,
        e_eff: float | None,
        elapsed_ratio: float,
    ) -> tuple[bool, str]:
        """Validate one cycle for trim learning using thermal signals only."""
        if sample is None:
            return False, "no_sample"
        if self._ff_trim.frozen:
            return False, f"frozen_{self._ff_trim.freeze_reason}"
        if e_eff is None:
            return False, "missing_e_eff"
        if elapsed_ratio < 1.0:
            return False, "partial_cycle"
        if sample.get("ff3_active", False):
            return False, "ff3_active"
        if sample.get("setpoint_changed", False):
            return False, "setpoint_changed"
        if sample.get("sat_state") != "NO_SAT":
            return False, f"sat_{sample.get('sat_state')}"

        regime = sample.get("regime")
        if regime not in (
            GovernanceRegime.EXCITED_STABLE,
            GovernanceRegime.NEAR_BAND,
            GovernanceRegime.DEAD_BAND,
        ):
            regime_value = regime.value if isinstance(regime, GovernanceRegime) else regime
            return False, f"regime_{regime_value}"

        error = sample.get("error")
        if error is None or abs(error) > FF_TRIM_MAX_ERROR_C:
            return False, f"error_{error}"

        slope_h = sample.get("slope_h")
        if slope_h is None:
            return False, "missing_slope"
        if abs(slope_h) > FF_TRIM_MAX_SLOPE_H:
            return False, f"slope_{slope_h:.3f}"

        return True, "ok"

    @property
    def a(self) -> float:
        return self.est.a

    @property
    def b(self) -> float:
        return self.est.b

    @property
    def on_percent(self) -> float:
        return self._on_percent

    @property
    def calculated_on_percent(self) -> float:
        return self._on_percent

    @property
    def committed_on_percent(self) -> float:
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
    def kp_reel(self) -> float:
        """Return the actual Kp used for calculation (after near-band adjustment)."""
        return self.Kp
    @property
    def ki_reel(self) -> float:
        """Return the actual Ki used for calculation (after near-band adjustment)."""
        return self.Ki

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
    def learning_start_dt(self) -> str:
        return self._learning_start_date.isoformat() if self._learning_start_date else None

    @property
    def last_decision_thermal(self) -> str:
        return self.gov.last_decision_thermal.value

    @property
    def freeze_reason_thermal(self) -> str:
        return self.gov.last_freeze_reason_thermal.value

    @property
    def last_decision_gains(self) -> str:
        return self.gov.last_decision_gains.value

    @property
    def freeze_reason_gains(self) -> str:
        return self.gov.last_freeze_reason_gains.value

    @property
    def bootstrap_progress(self) -> int | None:
        """
        Progress of the bootstrap (hysteresis) phase in percent (0-100).
        Returns None if not in Hysteresis phase.
        """
        if self.phase != SmartPIPhase.HYSTERESIS:
            return None

        total_needed = AB_MIN_SAMPLES_A + AB_MIN_SAMPLES_B
        current_count = min(len(self.est.a_meas_hist), AB_MIN_SAMPLES_A) + min(
            len(self.est.b_meas_hist), AB_MIN_SAMPLES_B
        )

        pct = (current_count / total_needed) * 100.0
        return int(clamp(pct, 0, 100))

    @property
    def bootstrap_state(self) -> str | None:
        """
        Detailed state message for the bootstrap process.
        Returns None if not in Hysteresis phase.

        Three steps:
          step1: waiting for deadtimes (A/B collection blocked per mode)
          step2: both deadtimes acquired, collecting initial emeas (<AB_MIN_SAMPLES_A/B)
          step3: full thermal model learning in progress
        """
        if self.phase != SmartPIPhase.HYSTERESIS:
            return None

        nb_a = len(self.est.a_meas_hist)
        nb_b = len(self.est.b_meas_hist)

        dt_heat_ok = self.dt_est.deadtime_heat_reliable
        dt_cool_ok = self.dt_est.deadtime_cool_reliable

        # Step 1: at least one deadtime missing
        if not (dt_heat_ok and dt_cool_ok):
            heat_str = f"{int(self.dt_est.deadtime_heat_s)}s" if dt_heat_ok and self.dt_est.deadtime_heat_s is not None else "null"
            cool_str = f"{int(self.dt_est.deadtime_cool_s)}s" if dt_cool_ok and self.dt_est.deadtime_cool_s is not None else "null"
            parts = [f"step1 - deadtime: heat:{heat_str} cool:{cool_str}"]
            if dt_heat_ok and nb_a > 0:
                parts.append(f"[A:{nb_a}/{AB_MIN_SAMPLES_A}]")
            if dt_cool_ok and nb_b > 0:
                parts.append(f"[B:{nb_b}/{AB_MIN_SAMPLES_B}]")
            return " ".join(parts)

        # Step 2: both deadtimes acquired, collecting initial emeas
        b_converged = self.est.b_converged_for_a()
        min_a = AB_MIN_SAMPLES_A_CONVERGED if b_converged else AB_MIN_SAMPLES_A
        b_target = AB_MIN_SAMPLES_B
        if nb_a < min_a or nb_b < AB_MIN_SAMPLES_B:
            return f"step2 - collecting emeas: A:{nb_a}/{min_a} B:{nb_b}/{b_target}"

        # Step 3: full thermal model learning
        ok_a = self.est.learn_ok_count_a
        ok_b = self.est.learn_ok_count_b
        return f"step3 - learning thermal model: A:{ok_a}/{AB_HISTORY_SIZE} B:{ok_b}/{AB_HISTORY_SIZE}"

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
    def error_filtered(self) -> float:
        return self._e_filt if self._e_filt is not None else 0.0

    @property
    def sign_flip_active(self) -> bool:
        return self._sign_flip_active

    @property
    def guard_cut_active(self) -> bool:
        return self.guards.guard_cut_active

    @guard_cut_active.setter
    def guard_cut_active(self, value: bool) -> None:
        self.guards.guard_cut_active = value

    @property
    def guard_cut_count(self) -> int:
        return self.guards.guard_cut_count

    @property
    def guard_kick_active(self) -> bool:
        return self.guards.guard_kick_active

    @guard_kick_active.setter
    def guard_kick_active(self, value: bool) -> None:
        self.guards.guard_kick_active = value

    @property
    def guard_kick_count(self) -> int:
        return self.guards.guard_kick_count

    @property
    def cycles_since_reset(self) -> int:
        return self._cycles_since_reset

    @property
    def filtered_setpoint(self) -> float | None:
        return self.sp_mgr.effective_setpoint

    @property
    def learning_resume_ts(self) -> float | None:
        """Return monotonic resume timestamp."""
        return self.learn_win.learning_resume_ts

    @property
    def u_cmd(self) -> float:
        return self._last_u_cmd

    @property
    def u_limited(self) -> float:
        return self._last_u_limited

    @property
    def u_applied(self) -> float:
        return self._last_u_applied

    @property
    def aw_du(self) -> float:
        return self._last_aw_du

    @property
    def forced_by_timing(self) -> bool:
        return self._last_forced_by_timing

    def update_timing_constraints(self, u_prev: float, u_cmd: float) -> float:
        """Apply min on/off delays and update forced_by_timing status."""
        self._last_forced_by_timing = False

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
        return self.learn_win.active if self.learn_win else False

    @property
    def learn_win_start_ts(self) -> float | None:
        """Return the monotonic timestamp when the current window started."""
        return self.learn_win.start_ts if self.learn_win else None

    @property
    def learn_T_int_start(self) -> float:
        """Return the indoor temperature at window start."""
        return self.learn_win.T_int_start if self.learn_win else 0.0

    @property
    def learn_T_ext_start(self) -> float:
        """Return the outdoor temperature at window start."""
        return self.learn_win.T_ext_start if self.learn_win else 0.0

    @property
    def learn_u_int(self) -> float:
        """Return the accumulated power integral (u * dt)."""
        return self.learn_win.u_int if self.learn_win else 0.0

    @property
    def learn_t_int_s(self) -> float:
        """Return the accumulated time integral in seconds."""
        return self.learn_win.t_int_s if self.learn_win else 0.0

    @property
    def learn_u_first(self) -> float | None:
        """Return the first power value in the window (for consistency check)."""
        return self.learn_win.u_first if self.learn_win else None

    @property
    def setpoint_boost_active(self) -> bool:
        return self.sp_mgr.boost_active

    @property
    def cycle_start_dt(self) -> str | None:
        """Return the start time of the current cycle (owned by CycleScheduler)."""
        return None

    @property
    def in_deadtime_window(self) -> bool:
        """Check if we are currently inside the estimated dead time window."""
        now = time.monotonic()

        # Check Heat Deadtime
        if self.dt_est.deadtime_heat_reliable and self._t_heat_episode_start is not None and self.dt_est.deadtime_heat_s is not None:
            dt_s = self.dt_est.deadtime_heat_s
            t_start = self._t_heat_episode_start
            # Robustness: ensure both are real numbers (not MagicMock)
            if isinstance(dt_s, (int, float)) and isinstance(t_start, (int, float)):
                elapsed = now - t_start
                if elapsed < dt_s:
                    return True

        # Check Cool Deadtime
        if self.dt_est.deadtime_cool_reliable and self._t_cool_episode_start is not None and self.dt_est.deadtime_cool_s is not None:
            dt_s = self.dt_est.deadtime_cool_s
            t_start = self._t_cool_episode_start
            # Robustness: ensure both are real numbers (not MagicMock)
            if isinstance(dt_s, (int, float)) and isinstance(t_start, (int, float)):
                elapsed = now - t_start
                if elapsed < dt_s:
                    return True

        return False

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
    ) -> None:
        """Execute the forced calibration state machine.

        Delegates to CalibrationManager component.
        """
        # Delegate to CalibrationManager component
        result = self.calibration_mgr.calculate(
            target_temp=target_temp,
            current_temp=current_temp,
            hvac_mode=hvac_mode,
            dt_est=self.dt_est,
            max_on_percent=self._max_on_percent,
        )

        # Calibration state is now fully managed by CalibrationManager

        # Apply result
        if result.on_percent is not None:
            self._on_percent = result.on_percent
        else:
            self._on_percent = 0.0

        # Update diagnostics
        self.ctl.last_i_mode = "CALIB"
        self.ctl.last_sat = "NO_SAT"
        self._last_u_ff = 0.0
        self._last_ff_raw = 0.0
        self._last_ff_reason = "ff_none"
        self._last_u_pi = self._on_percent
        self._last_u_cmd = self._on_percent
        self._last_u_limited = self._on_percent

    def update_realized_power(
        self,
        u_applied: float | None = None,
        dt_min: float = 0.0,
        forced_by_timing: bool = False,
        realized_percent: float | None = None,
        elapsed_ratio: float = 1.0,
        **_kwargs
    ) -> None:
        """Unified Astrom tracking AW based on realized energy. Single AW correction point.

        Args:
            u_applied: instantaneous duty cycle during the cycle's actual lifetime.
            dt_min: nominal cycle duration in minutes (from scheduler config).
            elapsed_ratio: fraction of the full cycle that actually ran (0..1).
                The energy actually delivered relative to a full cycle is
                u_applied * elapsed_ratio, which is comparable to the PI model output.
        """
        # Resolve argument name differences for compatibility with various tests
        val = realized_percent if realized_percent is not None else u_applied
        if val is None:
            return

        # Pre-conditions
        if dt_min <= 0 or abs(self.Ki) < 1e-6:
            return

        if forced_by_timing:
            self._last_aw_du = 0.0
            return

        # Cascade policy: respect the integration decision made by compute_pwm (Path A)
        i_mode = str(self.ctl.last_i_mode)
        if any(i_mode.startswith(p) for p in ("I:SKIP", "I:FREEZE", "I:GUARD", "I:CLAMP", "I:RESET", "I:BLEED")):
            self._last_aw_du = 0.0
            return

        if i_mode.startswith("I:HOLD"):
            if self.ctl.integral_hold_mode not in {
                "servo_recovery",
                "resume_recovery",
                "disturbance_recovery",
            }:
                self._last_aw_du = 0.0
                return

        # Reference = what the PI model would have commanded within valid physical bounds [0, 1]
        u_model = self.ctl.u_ff + (self.Kp * self.ctl.last_error_p_db + self.Ki * self.ctl.integral)
        u_model_clamped = clamp(u_model, 0.0, 1.0)

        # Reality = power actually delivered during the elapsed window
        du = val - u_model_clamped

        # Thermal Invariant for Tracking AW:
        # We only apply tracking AW when downstream constraints (rate limits, shedding, cancelled cycles)
        # PREVENT us from delivering the requested power (val < u_model_clamped -> du < 0).
        # We never artificially inflate the integral if constraints force us to deliver MORE power (du > 0),
        # nor do we drain it due to standard 0-100% saturation (handled perfectly by clamped u_model).
        du = min(0.0, du)

        self._last_aw_du = du

        if abs(du) <= 0.001:
            return

        ki_eff = max(abs(self.Ki), KI_MIN)
        kp_eff = max(abs(self.Kp), 1e-6)

        # Åström tracking dynamics (Åström & Hägglund, §3.5):
        # For a PI controller the tracking time constant Tt should equal the
        # integral time constant Ti = Kp/Ki.  With Tt = Ti the 1/Ki
        # amplification cancels out and dI = dt_min * du / Kp, which scales
        # naturally with the proportional gain and avoids disproportionate
        # integral corrections when Ki is small.
        # AW_TRACK_TAU_S is kept as a floor to avoid infinitely slow tracking.
        effective_dt_min = dt_min * elapsed_ratio
        if effective_dt_min <= 0.001:
            return

        dt_sec = effective_dt_min * 60.0
        Ti_sec = (kp_eff / ki_eff) * 60.0
        Tt_sec = max(Ti_sec, AW_TRACK_TAU_S)
        beta = clamp(dt_sec / max(Tt_sec, dt_sec), 0.0, 1.0)
        dI = beta * (du / ki_eff)

        # Per-cycle bound scaled by elapsed time
        dI_max = AW_TRACK_MAX_DELTA_I * effective_dt_min
        dI = clamp(dI, -dI_max, dI_max)

        # Apply and clamp
        i_max = 2.0 / ki_eff
        old_i = self.integral
        self.integral = clamp(self.integral + dI, -i_max, i_max)
        _LOGGER.debug(
            "%s - AW tracking: du=%.3f dI=%.4f (beta=%.2f) integral %.4f -> %.4f",
            self._name, du, dI, beta, old_i, self.integral,
        )

    def on_applied_power_updated(
        self,
        *,
        on_percent: float,
        hvac_mode: VThermHvacMode,
    ) -> None:
        """Synchronize state after a valve command is applied mid-cycle."""
        applied_on_percent = clamp(on_percent, 0.0, 1.0)
        if abs(applied_on_percent - self._committed_on_percent) <= 0.001:
            return

        now = time.monotonic()
        self._committed_on_percent = applied_on_percent
        self.u_prev = applied_on_percent
        self._last_u_applied = applied_on_percent
        self._update_deadtime_episode_status(applied_on_percent, hvac_mode, now)

    def save_state(self) -> dict:
        """Save algorithm state for persistence."""
        state = {
            "version": 2,
            "on_percent": self._on_percent,
            "last_target_temp": self._last_target_temp,
            "last_calibration_time": self.calibration_mgr.last_calibration_time,
            "cycles_since_reset": self._cycles_since_reset,
            "accumulated_dt": self._accumulated_dt,
            "deadtime_skip_count_a": self._deadtime_skip_count_a,
            "deadtime_skip_count_b": self._deadtime_skip_count_b,
            "learning_resume_ts": convert_monotonic_to_wall_ts(self.learn_win.learning_resume_ts),
            "learning_start_date": self._learning_start_date.isoformat() if self._learning_start_date else None,
            "est_state": self.est.save_state(),
            "dt_est_state": self.dt_est.save_state(),
            "gov_state": self.gov.save_state(),
            "ctl_state": self.ctl.save_state(),
            "sp_mgr_state": self.sp_mgr.save_state() if hasattr(self.sp_mgr, "save_state") else {},
            # Component states
            "lw_state": self.learn_win.save_state() if hasattr(self.learn_win, "save_state") else {},
            "db_state": self.deadband_mgr.save_state() if hasattr(self.deadband_mgr, "save_state") else {},
            "cal_state": self.calibration_mgr.save_state() if hasattr(self.calibration_mgr, "save_state") else {},
            "gs_state": self.gain_scheduler.save_state() if hasattr(self.gain_scheduler, "save_state") else {},
            "integral_guard_state": self.integral_guard.save_state() if hasattr(self.integral_guard, "save_state") else {},
            "twin_state": self.twin.save_state(),
            "guards_state": self.guards.save_state(),
            "ac_state": self.autocalib.save_state(),
            # FFv2 components
            "ff_v2_trim": self._ff_trim.save_state(),
            "ff_trim_delta": self._last_ff_trim_delta,
            "tint_filter_state": self.tint_filter.save_state(),
        }
        return state

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
        self.dt_est.load_state(state.get("dt_est_state", {}))
        self.gov.load_state(state.get("gov_state", {}))
        self.ctl.load_state(state.get("ctl_state", {}))
        self.sp_mgr.load_state(state.get("sp_mgr_state", {}))
        self.learn_win.load_state(state.get("lw_state", {}))
        self._learning_start_date = self.learn_win.learning_start_date
        self.deadband_mgr.load_state(state.get("db_state", {}))
        self.calibration_mgr.load_state(state.get("cal_state", {}))
        self.gain_scheduler.load_state(state.get("gs_state", {}))
        self.integral_guard.load_state(state.get("integral_guard_state", {}))
        self.integral_guard.reset()
        self.sp_mgr.clear_runtime_transients()
        self.ctl.clear_integral_hold()

        self._committed_on_percent = 0.0
        self._pending_fftrim_cycle_sample = None
        self._last_u_applied = 0.0
        self._recovery_hold_armed = False
        self._last_restart_reason = "none"
        self._resume_deadtime_hold_source = IntegralGuardSource.NONE
        self._resume_deadtime_hold_started = False
        self._last_integral_guard_mode = "reboot_reset"
        self.twin.load_state(state.get("twin_state", {}))

        # Load main algorithm scalars
        self._deadtime_skip_count_a = int(state.get("deadtime_skip_count_a", 0))
        self._deadtime_skip_count_b = int(state.get("deadtime_skip_count_b", 0))
        self._accumulated_dt = float(state.get("accumulated_dt", 0.0))
        # The learning_resume_ts from the previous session must NOT be carried over:
        # _startup_grace_period handles the post-restart freeze for exactly one cycle.
        # Keeping a stale resume-ts (e.g., the 20-min OFF-resume value) would overwrite
        # the learn_win ts on the second tick via update_learning() and block learning
        # for the full 20 minutes after every restart.
        self.learn_win.set_learning_resume_ts(None)

        # Load Guard State
        self.guards.load_state(state.get("guards_state", {}))
        self.autocalib.load_state(state.get("ac_state", {}))

        # Load FF trim state; freeze for a few cycles after reboot
        self._ff_trim.load_state(state.get("ff_v2_trim", {}))
        self._last_ff_trim_delta = float(state.get("ff_trim_delta", 0.0))
        self.tint_filter.load_state(state.get("tint_filter_state", {}))
        # Freeze trim learning briefly after reboot.
        self._ff_trim.freeze("reboot")
        self._ff_v2_reboot_freeze_remaining = FF_TRIM_REBOOT_FREEZE_CYCLES

        # Instant FF on reboot when model is well-learned:
        # skip the time_scale ramp by pre-setting _cycles_since_reset.
        # Safety: cycle 1 skips bumpless (is_first_run), cycle 2+ has stable FF (d_uff≈0).
        tau_info = self.est.tau_reliability()
        if self.est.learn_ok_count_a >= 10 and tau_info.reliable:
            self._cycles_since_reset = self.ff_warmup_cycles
            _LOGGER.info(
                "%s - Reboot: model learned (ok_a=%d, tau_reliable) -> FF warmup bypassed",
                self._name, self.est.learn_ok_count_a,
            )
        else:
            _LOGGER.debug(
                "%s - Reboot: model not ready (ok_a=%d, tau_reliable=%s) -> FF ramp active",
                self._name, self.est.learn_ok_count_a, tau_info.reliable,
            )

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
            self._on_percent = 0.0
            self._last_u_cmd = 0.0
            self._last_u_pi = 0.0
            self._last_u_limited = 0.0
            self.ctl.last_i_mode = "I:FREEZE(missing_sensor)"
            return True

        if hvac_mode == VThermHvacMode_OFF:
            self._on_percent = 0.0
            self._committed_on_percent = 0.0
            self._u_ff3_cycle = 0.0
            self._u_ff3_pending = 0.0
            self._ff3_active_cycle = False
            self._ff3_pending_active = False
            self._pending_fftrim_cycle_sample = None
            self._last_u_applied = 0.0
            self.ctl.u_prev = 0.0
            self._last_u_cmd = 0.0
            self._last_u_pi = 0.0
            self._last_u_limited = 0.0
            self.ctl.last_i_mode = "I:FREEZE(hvac_off)"
            self.deadband_mgr.in_deadband = False
            self.deadband_mgr.in_near_band = False
            self._output_initialized = True
            self._last_calculate_time = None
            self._current_cycle_start_monotonic = None
            self._prev_deadtime_hold = False
            self._t_heat_episode_start = None
            self._t_cool_episode_start = None
            self._recovery_hold_armed = False
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            self._last_restart_reason = (
                "window"
                if off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
                else "off"
            )
            return True

        # Handle explicit force off (shedding, windows)
        if power_shedding:
            self._on_percent = 0.0
            self._committed_on_percent = 0.0
            self._u_ff3_cycle = 0.0
            self._u_ff3_pending = 0.0
            self._ff3_active_cycle = False
            self._ff3_pending_active = False
            self._pending_fftrim_cycle_sample = None
            self._last_u_applied = 0.0
            self._t_heat_episode_start = None
            self._t_cool_episode_start = None
            self.ctl.u_prev = 0.0
            self._last_u_cmd = 0.0
            self._last_u_pi = 0.0
            self._last_u_limited = 0.0
            # We update regime to PERTURBED but SKIP PID calculation
            self.gov.on_cycle_start()
            self.gov.update_regime(GovernanceRegime.PERTURBED)
            _, reason = self.gov.decide_update("thermal")
            self.ctl.last_i_mode = f"I:RESET({reason.value})"
            self._output_initialized = True
            self._last_calculate_time = None
            self._current_cycle_start_monotonic = None
            self._recovery_hold_armed = False
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            self._last_restart_reason = "power_shedding"
            return True

        return False

    def _update_time_tracking(self, now: float) -> tuple[float, bool, bool, IntegralGuardSource]:
        """Update dt_min and handles first-run logic.

        Returns:
            Tuple of (dt_min, resumed_from_off, startup_first_run, resume_guard_source).
        """
        dt_min = 0.0
        resumed_from_off = False
        startup_first_run = False
        resume_guard_source = IntegralGuardSource.NONE
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
                self._recovery_hold_armed = False
                if self._last_restart_reason == "window":
                    resume_guard_source = IntegralGuardSource.WINDOW_RESUME
                elif self._last_restart_reason == "power_shedding":
                    resume_guard_source = IntegralGuardSource.POWER_SHEDDING_RESUME
                else:
                    resume_guard_source = IntegralGuardSource.OFF_RESUME
                self._last_restart_reason = "none"
                # Resume from window/OFF -> Pause learning to let system stabilize
                self.learn_win.set_learning_resume_ts(now + (LEARNING_PAUSE_RESUME_MIN * 60.0))
                _LOGGER.debug("%s - Resume from OFF: Learning paused for %d min", self._name, LEARNING_PAUSE_RESUME_MIN)

            if dt_min > PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN:
                _LOGGER.info(
                    "%s - Prolonged pause (%.1f min) detected, purging PI state & integral memory", 
                    self._name, dt_min
                )
                self.ctl.reset()

        # Cap dt to avoid huge jumps after pause
        if dt_min > (self._cycle_min * 10):
            dt_min = self._cycle_min

        return dt_min, resumed_from_off, startup_first_run, resume_guard_source

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
        power_shedding: bool,
        startup_first_run: bool,
        u_base: float,
    ) -> float:
        """Return the FF3 value to inject in the current calculation."""
        if not cycle_boundary:
            return self._u_ff3_cycle

        ff3_context = build_ff3_disturbance_context(
            twin_diag=self._last_twin_diag,
            measured_slope_h=slope_h,
            trajectory_active=self.sp_mgr.trajectory_active,
            trajectory_source=self.sp_mgr.trajectory_source,
        )
        twin_disabled_reason = get_ff3_twin_unavailability_reason(self._last_twin_diag)
        twin_reliable = (
            self._last_twin_diag.get("status") == "ok"
            and self._last_twin_diag.get("model_reliable") is True
            and self._last_twin_diag.get("warming_up") is not True
        )
        ff3_result: FF3Result = compute_ff3(
            enabled=self._use_ff3,
            twin=self.twin,
            twin_reliable=twin_reliable,
            twin_initialized=self.twin.T_hat is not None,
            twin_disabled_reason=twin_disabled_reason,
            tau_reliable=self._tau_reliable,
            ext_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            regime=self.gov.regime,
            in_deadband=self.deadband_mgr.in_deadband,
            in_near_band=self.deadband_mgr.in_near_band,
            disturbance_context_active=ff3_context.disturbance_active,
            disturbance_context_reason=ff3_context.reason,
            is_calibrating=(self.calibration_state != SmartPICalibrationPhase.IDLE),
            power_shedding=power_shedding,
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
            deadtime_cool_s=self.dt_est.deadtime_cool_s,
        )
        self._u_ff3_pending = ff3_result.u_ff3_applied
        self._ff3_pending_active = abs(ff3_result.u_ff3_applied) > 1e-9
        self._last_ff3_enabled = ff3_result.enabled
        self._last_ff3_reason_disabled = ff3_result.reason_disabled
        self._last_ff3_candidate_scores = ff3_result.candidate_scores
        self._last_ff3_selected_candidate = ff3_result.selected_candidate
        self._last_ff3_horizon_cycles = ff3_result.horizon_cycles
        self._last_ff3_disturbance_active = ff3_context.disturbance_active
        self._last_ff3_disturbance_reason = ff3_context.reason
        self._last_ff3_disturbance_kind = ff3_context.disturbance_kind
        self._last_ff3_residual_persistent = ff3_context.residual_persistent
        self._last_ff3_dynamic_coherent = ff3_context.dynamic_coherent
        return self._u_ff3_pending

    def _disable_ff3_for_next_cycle_in_deadband(self) -> None:
        """Keep the current FF3 contribution but prevent it from being reused next cycle."""
        if abs(self._u_ff3_pending) > 1e-9 or self._ff3_pending_active:
            _LOGGER.debug(
                "%s - Deadband active: disabling pending FF3 for the next cycle",
                self._name,
            )

        self._u_ff3_pending = 0.0
        self._ff3_pending_active = False

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
            target_temp_filt = self.sp_mgr.filter_setpoint(
                target_temp=target_temp,
                current_temp=current_temp,
                hvac_mode=hvac_mode,
                a=self.est.a,
                b=self.est.b,
                ext_current_temp=ext_current_temp,
                u_ref=self._committed_on_percent,
                deadtime_cool_s=self.dt_est.deadtime_cool_s,
                deadtime_cool_reliable=self.dt_est.deadtime_cool_reliable,
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
        include_band_state: bool = False,
    ) -> tuple[float, bool] | tuple[float, bool, bool]:
        """Update tau reliability, error weighting, and deadband state.

        Args:
            error_i: Raw setpoint error (SP_brut - y) — used for deadband and stored state.
            error_p: Filtered setpoint error (SP_for_P - y) — returned as e_p for P term.

        Returns:
            Tuple of (e_p, was_in_deadband) by default.
            When include_band_state=True, returns
            (e_p, was_in_deadband, was_in_near_band).
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
        was_in_deadband = self.deadband_mgr.in_deadband
        was_in_near_band = self.deadband_mgr.in_near_band
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

        if include_band_state:
            return e_p, was_in_deadband, was_in_near_band

        return e_p, was_in_deadband

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
        is_first_run: bool = False,
        setpoint_changed: bool = False,
        cycle_boundary: bool = False,
        power_shedding: bool = False,
        startup_first_run: bool = False,
    ) -> tuple[float, bool]:
        """Calculate gains and feedforward, and handles integrator hold.

        Returns:
            Tuple of (runtime_ff, integrator_hold).
        """
        # Capture old Ki for bumpless integral rescaling
        old_ki = self.gain_scheduler.ki

        # Delegate gain calculation to GainScheduler component
        tau_info = self.est.tau_reliability()
        self.gain_scheduler.calculate(
            tau_reliable=self._tau_reliable,
            tau_min=tau_info.tau_min,
            estimator=self.est,
            dt_est=self.dt_est,
            in_near_band=self.deadband_mgr.in_near_band,
            kp_near_factor=self.kp_near_factor,
            ki_near_factor=self.ki_near_factor,
            governance_decision=gov_decision_g,
        )
        new_ki = self.gain_scheduler.ki

        # Bumpless Gain Scheduling: Rescale integral to preserve identical energy output
        if old_ki > 0.0 and new_ki > 0.0 and abs(old_ki - new_ki) > 1e-6:
            self.ctl.integral = self.ctl.integral * (old_ki / new_ki)
            _LOGGER.debug(
                "%s - Ki changed from %.5f to %.5f, rescaled integral to %.4f for bumpless gain transfer",
                self._name, old_ki, new_ki, self.ctl.integral
            )

        # --- FFv2: AB confidence & fallback ---
        self._ab_confidence.evaluate(
            tau_reliable=self._tau_reliable,
            learn_ok_count_a=self.est.learn_ok_count_a,
            learn_ok_count_b=self.est.learn_ok_count_b,
        )
        ab_fallback = self._ab_confidence.get_ff_fallback()

        # --- FFv2: compute k_ff (0 in COOL mode or when model not ready) ---
        if hvac_mode == VThermHvacMode_COOL:
            k_ff = 0.0
            warmup_scale = 0.0
        else:
            model_ready = (
                len(self.est.a_meas_hist) >= AB_MIN_SAMPLES_A
                and len(self.est.b_meas_hist) >= AB_MIN_SAMPLES_B
            )
            if model_ready and self._tau_reliable:
                k_ff = clamp(self.est.b / max(self.est.a, 1e-6), 0.0, 3.0)
            else:
                k_ff = 0.0
            learn_scale = clamp(self.est.learn_ok_count / float(self.ff_warmup_ok_count), 0.0, 1.0)
            time_scale = clamp(self._cycles_since_reset / float(self.ff_warmup_cycles), 0.0, 1.0)
            reliable_cap = 1.0 if self._tau_reliable else self.ff_scale_unreliable_max
            warmup_scale = clamp(reliable_cap * learn_scale * time_scale, 0.0, 1.0)

        # Save previous FF state before computing the new FFResult
        prev_ff_result = self._last_ff_result

        # --- FFv2: nominal FF1 + FF2 path (basis for FF3) ---
        nominal_ff_result = compute_ff(
            k_ff=k_ff,
            target_temp_ff=target_temp_ff,
            ext_temp=ext_current_temp,
            warmup_scale=warmup_scale,
            trim=self._ff_trim,
            regime=self.gov.regime,
            error=error,
            near_band_below_deg=self.deadband_mgr.near_band_below_deg,
            near_band_above_deg=self.deadband_mgr.near_band_above_deg,
            ab_fallback=ab_fallback,
            u_ff3=0.0,
        )

        u_ff3 = self._compute_ff3_for_cycle(
            cycle_boundary=cycle_boundary,
            target_temp=target_temp_ff,
            current_temp=current_temp,
            ext_current_temp=ext_current_temp,
            hvac_mode=hvac_mode,
            slope_h=slope_h,
            setpoint_changed=setpoint_changed,
            power_shedding=power_shedding,
            startup_first_run=startup_first_run,
            u_base=nominal_ff_result.u_ff_final,
        )

        # --- FFv2 + FF3: full FF computation via orchestrator ---
        ff_result = compute_ff(
            k_ff=k_ff,
            target_temp_ff=target_temp_ff,
            ext_temp=ext_current_temp,
            warmup_scale=warmup_scale,
            trim=self._ff_trim,
            regime=self.gov.regime,
            error=error,
            near_band_below_deg=self.deadband_mgr.near_band_below_deg,
            near_band_above_deg=self.deadband_mgr.near_band_above_deg,
            ab_fallback=ab_fallback,
            u_ff3=u_ff3,
        )
        self._last_ff_result = ff_result
        self._last_ff_raw = ff_result.ff_raw
        self._last_ff_reason = ff_result.ff_reason
        self._last_u_ff3 = ff_result.u_ff3

        integrator_hold = gov_decision_g == GovernanceDecision.HARD_FREEZE

        # Deadband must keep the structural hold power only: FF1 + FF2.
        # FF3 remains a predictive transient action and must not be maintained
        # around the setpoint.
        runtime_ff = (
            ff_result.u_db_nominal
            if self.deadband_mgr.in_deadband
            else ff_result.u_ff_eff
        )

        return runtime_ff, integrator_hold

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
        self._on_percent = clamp(u_limited, 0.0, self._max_on_percent if self._max_on_percent is not None else 1.0)
        return self._on_percent

    def calculate(  # pylint: disable=keyword-arg-before-vararg
        self,
        target_temp: float | None,
        current_temp: float | None,
        ext_current_temp: float | None = None,
        hvac_mode: VThermHvacMode | None = None,
        slope: float | None = None,
        integrator_hold: bool = False,
        power_shedding: bool = False,
        cycle_boundary: bool = False,
        off_reason: str | None = None,
        *args,
        **_kwargs,
    ) -> float:
        """
        Compute the next duty-cycle command.
        """
        def _is_hvac_mode_like(value: Any) -> bool:
            """Return True when a value looks like a VTherm HVAC mode object."""
            return value is not None and str(value) in {
                str(VThermHvacMode_OFF),
                str(VThermHvacMode_HEAT),
                str(VThermHvacMode_COOL),
            }

        # Compatibility handling for old signature: calculate(t, c, dt_min, now, hvac_mode)
        # In old calls: ext_current_temp=dt_min, hvac_mode=now, slope=hvac_mode
        if _is_hvac_mode_like(slope) and not _is_hvac_mode_like(hvac_mode):
            # old call: positional args were (target, current, ext, slope_float, hvac_mode)
            # hvac_mode param received the slope float (or None), slope param received the VThermHvacMode
            _slope_val = hvac_mode  # save the float (or None) that landed in hvac_mode
            hvac_mode = slope       # move VThermHvacMode to its proper param
            slope = _slope_val if isinstance(_slope_val, (int, float)) else None
        elif hvac_mode is None and len(args) > 0 and _is_hvac_mode_like(args[0]):
            # another old call variant
            hvac_mode = args[0]

        now = time.monotonic()
        self._pending_fftrim_cycle_sample = None

        # --- 0. HVAC mode transition (HEAT↔COOL) → reset integral ---
        # Track active modes BEFORE validation so that missing presets in COOL
        # don't intercept the cycle and hide the transition.
        if hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            if self._last_hvac_mode is not None and hvac_mode != self._last_hvac_mode:
                self.ctl.reset()
                self._recovery_hold_armed = False
                _LOGGER.info(
                    "%s - HVAC mode changed (%s → %s): PI state reset",
                    self._name, self._last_hvac_mode, hvac_mode
                )
            self._last_hvac_mode = hvac_mode

        # --- 1. Validation & Handle OFF ---
        if self._validate_and_handle_off(target_temp, current_temp, hvac_mode, power_shedding, off_reason):
            return

        # Guard Cut: force 0% if active
        if self.guards.guard_cut_active:
            self._on_percent = 0.0
            return

        # --- 1a. Adaptive T_int Filter ---
        if current_temp is not None:
            t_int_lp, t_int_clean = self.tint_filter.update(current_temp, now)
        else:
            t_int_lp = current_temp
            t_int_clean = current_temp

        # --- 2. Update Time Tracking ---
        dt_min, resumed_from_off, startup_first_run, resume_guard_source = self._update_time_tracking(now)

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
            ext_current_temp,
            hvac_mode,
            dt_min,
            now,
            remaining_cycle_min,
            resumed_from_off,
            startup_first_run,
        )

        # Clear cycle regimes on setpoint change to prevent REGIME_TRANSITION freeze
        if setpoint_changed:
            signed_sp_delta = target_temp - old_target_temp if old_target_temp is not None else 0.0
            if hvac_mode == VThermHvacMode_COOL:
                signed_sp_delta = -signed_sp_delta
            self._arm_integral_guard(
                IntegralGuardSource.SETPOINT_CHANGE,
                block_positive=(signed_sp_delta >= 0.0),
            )
            self.gov.on_cycle_start()
            self._recovery_hold_armed = False
            self._resume_deadtime_hold_source = IntegralGuardSource.NONE
            self._resume_deadtime_hold_started = False
            # Handle integral reset and thermal guard on setpoint changes
            new_error, new_error_p = self.ctl.handle_setpoint_change(
                target_temp, old_target_temp, current_temp, hvac_mode, self.Kp, self.Ki
            )
            if new_error != 0.0:
                self._e_filt = new_error
                self._last_error = new_error
                self._last_error_p = new_error_p
        elif resume_guard_source != IntegralGuardSource.NONE:
            if resume_guard_source in {
                IntegralGuardSource.WINDOW_RESUME,
                IntegralGuardSource.POWER_SHEDDING_RESUME,
            }:
                self.integral_guard.reset()
                self._last_integral_guard_mode = "resume_pending"
                self._resume_deadtime_hold_source = resume_guard_source
                self._resume_deadtime_hold_started = False
            else:
                self._arm_integral_guard(resume_guard_source)

        # --- 4. Learning & Calibration ---
        if self.phase != SmartPIPhase.HYSTERESIS and not self.calibration_mgr.is_calibrating:
            # During forced calibration, the deadtime estimator must be driven
            # only by the calibration state machine outputs. Feeding it first
            # with the stale committed cycle value can create a false 1 -> 0 -> 1
            # transient when calculate() is called twice before the scheduler
            # commits the new cycle.
            self.dt_est.update(
                now=now,
                tin=t_int_lp,
                sp=target_temp_filt,
                u_applied=self._committed_on_percent,
                max_on_percent=self._max_on_percent if self._max_on_percent is not None else 1.0,
                is_hysteresis=False,
            )

        # Heartbeat learning update
        if dt_min > 0:
            self.update_learning(
                dt_min=dt_min,
                current_temp=t_int_clean,
                ext_temp=ext_current_temp,
                u_active=self._committed_on_percent,
                setpoint_changed=setpoint_changed
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
            self._recovery_hold_armed = False
            self.ctl.clear_integral_hold()
            self._calculate_forced_calibration(target_temp, current_temp, hvac_mode)
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
            out = self.ctl.calculate_hysteresis(target_temp_filt, current_temp, hvac_mode, self._hyst_off, self._hyst_on)
            if out is not None:
                self._on_percent = out

            # Hysteresis transitions are applied immediately by the handler,
            # so commit the bang-bang output now for deadtime tracking.
            self._committed_on_percent = self._on_percent
            self.u_prev = self._on_percent
            self._last_u_applied = self._on_percent
            self._update_deadtime_episode_status(self._committed_on_percent, hvac_mode, now)
            self.dt_est.update(
                now=now,
                tin=t_int_lp,
                sp=target_temp_filt,
                u_applied=self._committed_on_percent,
                max_on_percent=self._max_on_percent if self._max_on_percent is not None else 1.0,
                is_hysteresis=True,
            )

            # Update Twin Diagnostics even in hysteresis
            self._update_twin_diagnostics(current_temp, ext_current_temp, target_temp, hvac_mode, dt_s=dt_min * 60.0)
            self._last_current_temp = current_temp
            self._last_ext_temp = ext_current_temp
            return

        # --- 6. Control Context & Deadband ---
        e_p, _was_in_deadband, _was_in_near_band = self._update_control_context(
            error_i,
            hvac_mode,
            current_temp,
            ext_current_temp,
            error_p,
            dt_min,
            include_band_state=True,
        )
        in_deadband_now = self.deadband_mgr.in_deadband
        in_core_deadband = self._tau_reliable and abs(error_i) < max(self.deadband_c, 0.0)
        self._update_resume_deadtime_hold(
            error_i=error_i,
            in_core_deadband=in_core_deadband,
            hvac_mode=hvac_mode,
        )
        if (
            self.sp_mgr.trajectory_active
            and self.sp_mgr.trajectory_source == "disturbance"
        ):
            self._arm_integral_guard(IntegralGuardSource.DISTURBANCE_RECOVERY)
        trajectory_i_active = (
            self.sp_mgr.trajectory_active
            and self.sp_mgr.trajectory_phase in (TrajectoryPhase.TRACKING, TrajectoryPhase.RELEASE)
        )
        (
            block_positive_integral,
            block_negative_integral,
            positive_integral_guard_mode,
        ) = self._update_integral_guard(
            error_i=error_i,
            error_p=e_p,
            hvac_mode=hvac_mode,
            slope_h=slope,
            in_core_deadband=in_core_deadband,
            trajectory_active=trajectory_i_active,
        )
        if in_deadband_now and not cycle_boundary:
            self._disable_ff3_for_next_cycle_in_deadband()

        # --- 6b. Integral freeze during deadtime window ---
        if self.in_deadtime_window:
            integrator_hold = True

        # --- 7. Governance Decision ---
        # Capture previous regime before update (for FFv2 bumpless and diagnostics)
        self._last_regime_prev = self.gov.regime.value if self.gov.regime else ""

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
        u_ff, gov_hold = self._apply_gains_and_ff(
            gov_decision_g,
            target_temp,
            ext_current_temp,
            hvac_mode,
            error_i,
            current_temp,
            e_p,
            slope,
            resumed_from_off,
            setpoint_changed=setpoint_changed,
            cycle_boundary=cycle_boundary,
            power_shedding=power_shedding,
            startup_first_run=startup_first_run,
        )
        # Apply explicit hold (parameter) or governance hold
        integrator_hold = integrator_hold or gov_hold

        # Deadband ENTRY: no bumpless transfer. The integral keeps its
        # naturally accumulated value and is simply frozen by compute_pwm.
        # On deadband exit the PI resumes from this authentic state and the
        # integrator fills the gap to hold power within a few cycles — the
        # building's thermal inertia absorbs the brief transient.

        # --- 10. Thermal Guard ---
        # Activation is handled on signed demand reduction. Release happens once
        # the signed error returns close enough to equilibrium.
        if self.ctl.hysteresis_thermal_guard and error_i <= self.deadband_c:
            self.ctl.hysteresis_thermal_guard = False

        # --- 11. PID Compute ---
        # Capture saturation state before compute_pwm updates it (used for SAT_HI exit detection)
        prev_sat_before_compute = self.ctl.last_sat

        u_cmd = self.ctl.compute_pwm(
            error_i,
            e_p,
            self.Kp,
            self.Ki,
            u_ff,
            dt_min,
            self._cycle_min,
            in_deadband_now,
            self.deadband_mgr.in_near_band,
            integrator_hold,
            self._last_ff_result.u_db_nominal if self._last_ff_result is not None else u_ff,
            hvac_mode,
            current_temp,
            target_temp_filt,
            self.ctl.hysteresis_thermal_guard,
            self._tau_reliable,
            self.est.learn_ok_count_a,
            deadband_c=self.deadband_c,
            trajectory_shaping_active=trajectory_i_active and block_positive_integral,
            core_deadband=in_core_deadband,
            block_positive_integral=block_positive_integral,
            block_negative_integral=block_negative_integral,
            positive_integral_guard_mode=positive_integral_guard_mode,
            deadband_allow_p=self._deadband_allow_p,
        )

        # --- 12. Soft Constraints ---
        u_limited = self._apply_soft_constraints(u_cmd, dt_min, setpoint_changed)
        self._last_u_limited = u_limited

        # --- 13. Timing Constraints & Anti-Windup Tracking ---
        u_final = self.update_timing_constraints(self.u_prev, u_limited)
        self._on_percent = u_final
        self._last_u_applied = u_final

        # --- 13. Store the candidate output only (AW tracking deferred to on_cycle_completed) ---
        prev_deadtime_hold = self._prev_deadtime_hold
        self._prev_deadtime_hold = self.in_deadtime_window
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

        # --- 14b. FF trim sample recording & saturation tracking ---
        # Update persistent saturation counter and detect SAT_HI exit.
        if self.ctl.last_sat != "NO_SAT":
            self._sat_persistent_cycles += 1
        else:
            if prev_sat_before_compute == "SAT_HI":
                _LOGGER.debug(
                    "%s - SAT_HI exit: integral stays at %.2f (natural post-conditional-integration)",
                    self._name, self.ctl.integral
                )
            self._sat_persistent_cycles = 0

        # Save the current cycle sample for trim learning at real cycle completion.
        if self._last_ff_result is not None:
            self._pending_fftrim_cycle_sample = {
                "error": self._last_error,
                "slope_h": slope,  # °C/h as received from thermostat
                "regime": self.gov.regime,
                "sat_state": self.ctl.last_sat,
                "u_ff1": self._last_ff_result.u_ff1,
                "ff3_active": self._ff3_active_cycle,
                "setpoint_changed": setpoint_changed,
            }

        # --- 14c. FFv2: freeze management for trim ---
        # Post-reboot freeze countdown
        if self._ff_v2_reboot_freeze_remaining > 0:
            self._ff_v2_reboot_freeze_remaining -= 1
            if self._ff_v2_reboot_freeze_remaining == 0:
                self._ff_trim.unfreeze()
                _LOGGER.debug("%s - FFv2: post-reboot freeze lifted", self._name)

        # Freeze conditions for trim
        ab_state = self._ab_confidence.state
        is_saturated = regime == GovernanceRegime.SATURATED
        is_perturbed = regime == GovernanceRegime.PERTURBED
        is_degraded = regime == GovernanceRegime.DEGRADED

        if self._ff_v2_reboot_freeze_remaining > 0:
            self._ff_trim.freeze("reboot")
        elif ab_state == ABConfidenceState.AB_BAD:
            self._ff_trim.freeze(f"ab_{ab_state.value}")
        elif ab_state == ABConfidenceState.AB_DEGRADED:
            self._ff_trim.freeze(f"ab_{ab_state.value}")
        elif is_saturated:
            self._ff_trim.freeze("saturated")
        elif is_perturbed:
            self._ff_trim.freeze("perturbed")
        elif is_degraded:
            self._ff_trim.freeze("degraded")
        else:
            self._ff_trim.unfreeze()

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
        """
        Update the start timestamps for heating/cooling episodes.
        Used for in_deadtime_window property.
        """
        # Determine if active based on u > 0 and mode
        is_heating = (u_applied > 0.01) and (hvac_mode == VThermHvacMode_HEAT)
        is_cooling = (u_applied > 0.01) and (hvac_mode == VThermHvacMode_COOL)

        # Heat Episode Logic
        if is_heating:
            if self._t_heat_episode_start is None:
                self._t_heat_episode_start = now
                _LOGGER.debug("%s - DeadTime: Heating episode started at %s", self._name, now)
        else:
            if self._t_heat_episode_start is not None:
                _LOGGER.debug("%s - DeadTime: Heating episode stopped", self._name)
            self._t_heat_episode_start = None

        # Cool Episode Logic
        if is_cooling:
            if self._t_cool_episode_start is None:
                self._t_cool_episode_start = now
                _LOGGER.debug("%s - DeadTime: Cooling episode started at %s", self._name, now)
        else:
            if self._t_cool_episode_start is not None:
                _LOGGER.debug("%s - DeadTime: Cooling episode stopped", self._name)
            self._t_cool_episode_start = None

    def get_diagnostics(self) -> Dict[str, Any]:
        """Return diagnostic information (suitable for attributes/UI)."""
        return build_diagnostics(self, self._debug_mode)

    def get_published_diagnostics(self) -> Dict[str, Any]:
        """Return the compact SmartPI summary published as HA attributes."""
        return build_published_diagnostics(self)

    def get_debug_diagnostics(self) -> Dict[str, Any] | None:
        """Return the full debug diagnostics when debug mode is enabled."""
        if not self._debug_mode:
            return None
        return build_debug_diagnostics(self)
