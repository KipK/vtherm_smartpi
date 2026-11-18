"""
Smart-PI Calibration Manager.

Manages forced calibration state machine for dead time estimation.
The calibration cycle drives the system through temperature thresholds
to measure heating and cooling dead times.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .timestamp_utils import (
    elapsed_wall_minutes,
    monotonic_to_wall_ts,
    normalize_wall_timestamp,
    wall_to_monotonic_ts,
)

from .const import (
    CALIBRATION_PAUSE_MAX_MIN,
    CALIBRATION_REACHABILITY_MARGIN_C,
    HYST_LOWER_C,
    HYST_UPPER_C,
    SmartPICalibrationPhase,
    SmartPICalibrationResult,
    SmartPIPhase,
)

if TYPE_CHECKING:
    from .deadtime_estimator import DeadTimeEstimator

_LOGGER = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    """Result of calibration calculation."""
    on_percent: float | None  # None if not calibrating
    phase: SmartPICalibrationPhase
    result: SmartPICalibrationResult = SmartPICalibrationResult.PENDING
    message: str = ""


class CalibrationManager:
    """
    Manages forced calibration state machine for dead time estimation.
    
    The calibration cycle consists of three phases:
    1. COOL_DOWN: Drive temperature to low threshold
    2. HEAT_UP: Drive temperature to high threshold (triggers heat deadtime detection)
    3. COOL_DOWN_FINAL: Drive back to low threshold (triggers cool deadtime detection)
    
    After completion, the DeadTimeEstimator should have reliable measurements.
    """

    def __init__(self, name: str):
        """
        Initialize CalibrationManager.
        
        Args:
            name: Entity name for logging
        """
        self._name = name
        
        # State variables
        self._last_calibration_time: float | None = None
        self._calibration_state: SmartPICalibrationPhase = SmartPICalibrationPhase.IDLE
        self._calibration_start_time: float | None = None
        self._force_calibration_requested: bool = False
        self._calibration_result: SmartPICalibrationResult = SmartPICalibrationResult.PENDING
        # Result of a run that ended since the last take_completed_result() (runtime only)
        self._completed_result: SmartPICalibrationResult | None = None
        # Mode of the last running tick, and the wall-clock time and mode of
        # the stop that suspends the run (None while it is running)
        self._run_mode: str | None = None
        self._pause_start_time: float | None = None
        self._pause_mode: str | None = None

    def reset(self) -> None:
        """Reset calibration state to initial values."""
        if self.is_calibrating:
            self._completed_result = SmartPICalibrationResult.CANCELLED
        self._calibration_state = SmartPICalibrationPhase.IDLE
        self._calibration_start_time = None
        self._force_calibration_requested = False
        self._calibration_result = SmartPICalibrationResult.PENDING
        self._run_mode = None
        self._pause_start_time = None
        self._pause_mode = None
        # Note: _last_calibration_time is NOT reset to preserve history

    def request_calibration(self, phase=None) -> None:
        """Request a forced calibration cycle.

        Args:
            phase: Current SmartPI phase. If HYSTERESIS, the request is rejected.
        """
        if phase == SmartPIPhase.HYSTERESIS:
            _LOGGER.warning(
                "%s - Calibration request rejected: system is in bootstrap/hysteresis phase",
                self._name,
            )
            return
        _LOGGER.info("%s - Manual force calibration requested", self._name)
        self._force_calibration_requested = True

    def start_calibration(self, now: float) -> None:
        """
        Start a calibration cycle.
        
        Args:
            now: Current monotonic timestamp
        """
        self._calibration_state = SmartPICalibrationPhase.COOL_DOWN
        self._calibration_start_time = now
        self._force_calibration_requested = False
        self._calibration_result = SmartPICalibrationResult.PENDING

    def handle_timeout(self) -> None:
        """Handle calibration timeout."""
        self._calibration_state = SmartPICalibrationPhase.IDLE
        self._calibration_start_time = None
        self._pause_start_time = None
        self._pause_mode = None
        self._calibration_result = SmartPICalibrationResult.CANCELLED
        self._completed_result = self._calibration_result

    @property
    def is_suspended(self) -> bool:
        """Check if the running calibration waits for the thermostat to come back."""
        return self.is_calibrating and self._pause_start_time is not None

    def suspend(self) -> None:
        """Suspend the running calibration: its phase is kept, nothing is commanded."""
        if self.is_calibrating and self._pause_start_time is None:
            self._pause_start_time = time.time()
            self._pause_mode = self._run_mode
            _LOGGER.info("%s - Calibration suspended (thermostat stopped)", self._name)

    def follow_mode(self, hvac_mode, dt_est: "DeadTimeEstimator") -> None:
        """Suspend, resume or cancel the calibration according to the thermostat mode.

        A stop (any mode but heat or cool) suspends the run. The run continues
        in its phase when the same mode comes back within
        CALIBRATION_PAUSE_MAX_MIN of the stop; a longer stop, or another mode,
        cancels it.
        """
        if not self.is_calibrating:
            return
        running = hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL)
        if running and self._pause_start_time is None:
            self._run_mode = str(hvac_mode)
            return
        self.suspend()
        paused_min = elapsed_wall_minutes(self._pause_start_time)
        if paused_min is None or paused_min > CALIBRATION_PAUSE_MAX_MIN:
            self._cancel()
        elif running:
            if self._pause_mode is not None and str(hvac_mode) != self._pause_mode:
                self._cancel()
                return
            _LOGGER.info("%s - Calibration resumed after %.1f min", self._name, paused_min)
            self._pause_start_time = None
            self._pause_mode = None
            self._run_mode = str(hvac_mode)
            # A response detection in progress spans the stop: start it again
            dt_est.reset_observation(0.0)

    def _cancel(self) -> None:
        """Cancel the run; the handler reports the result to the supervision."""
        _LOGGER.info("%s - HVAC OFF: canceling ongoing calibration", self._name)
        self.reset()

    def take_completed_result(self) -> SmartPICalibrationResult | None:
        """Return the result of a run that ended since the last call, then forget it."""
        result, self._completed_result = self._completed_result, None
        return result

    def _unreachable_phase(
        self,
        target_temp: float,
        current_temp: float | None,
        is_cool: bool,
        ext_temp: float | None,
    ) -> str | None:
        """Name the current phase when the outdoor temperature rules the cycle out.

        A passive phase only brings the room toward the outdoor temperature.
        The check uses the threshold of the passive phase to come: the current
        phase when it is passive, else the next passive phase of the cycle
        (none when no passive phase follows). The cycle is unreachable when the
        outdoor temperature lies within CALIBRATION_REACHABILITY_MARGIN_C of
        that threshold, or beyond it. Returns None when the outdoor temperature
        is unknown, or when the current phase is passive and the measured
        temperature already reached its threshold.
        """
        if ext_temp is None:
            return None
        phase = self._calibration_state
        if is_cool:
            # Passive phase: HEAT_UP; the warm-up threshold is the one to come.
            if phase == SmartPICalibrationPhase.COOL_DOWN_FINAL:
                return None
            threshold = target_temp + HYST_UPPER_C
            passive_now = phase == SmartPICalibrationPhase.HEAT_UP
            reached = current_temp is not None and current_temp >= threshold
            unreachable = ext_temp < threshold + CALIBRATION_REACHABILITY_MARGIN_C
        else:
            # Passive phases: COOL_DOWN and COOL_DOWN_FINAL; the final
            # cool-down threshold is the one to come during HEAT_UP.
            threshold = (
                target_temp - HYST_LOWER_C
                if phase == SmartPICalibrationPhase.COOL_DOWN
                else target_temp
            )
            passive_now = phase != SmartPICalibrationPhase.HEAT_UP
            reached = current_temp is not None and current_temp <= threshold
            unreachable = ext_temp > threshold - CALIBRATION_REACHABILITY_MARGIN_C
        if (passive_now and reached) or not unreachable:
            return None
        _LOGGER.warning(
            "%s - Calibration %s unreachable: outdoor temperature %.2f too close to threshold %.2f",
            self._name, phase.value, ext_temp, threshold,
        )
        return phase.value

    def check_and_start(
        self,
        now: float,
        phase,
    ) -> tuple[bool, str]:
        """
        Check if a manually requested calibration should start and start it if needed.

        Automatic calibration scheduling is handled by AutoCalibTrigger.
        This method only handles manual requests.

        Args:
            now: Current monotonic timestamp
            phase: Current SmartPI phase (HYSTERESIS blocks calibration)

        Returns:
            Tuple of (started, reason): reason is "manual" or "not_started"
        """
        # Already calibrating?
        if self._calibration_state != SmartPICalibrationPhase.IDLE:
            return (False, "already_calibrating")

        # Block calibration during bootstrap phase
        if phase == SmartPIPhase.HYSTERESIS:
            if self._force_calibration_requested:
                _LOGGER.warning(
                    "%s - Calibration request ignored: system is in bootstrap/hysteresis phase",
                    self._name,
                )
                self._force_calibration_requested = False
            return (False, "hysteresis_phase")

        # Check for manual request
        if self._force_calibration_requested:
            _LOGGER.info("%s - Starting forced calibration (reason=manual)", self._name)
            self.start_calibration(now)
            return (True, "manual")

        return (False, "not_started")

    def calculate(
        self,
        target_temp: float,
        current_temp: float,
        hvac_mode,  # VThermHvacMode
        dt_est: "DeadTimeEstimator",
        max_on_percent: float | None,
        ext_temp: float | None = None,
        filtered_temp: float | None = None,
    ) -> CalibrationResult:
        """
        Execute calibration state machine.
        
        Args:
            target_temp: Target temperature (°C)
            current_temp: Current temperature (°C)
            filtered_temp: Filtered temperature driving the dead time detection,
                as in regulation (defaults to current_temp)
            hvac_mode: Current HVAC mode (VThermHvacMode)
            dt_est: DeadTimeEstimator instance for feeding measurements
            max_on_percent: Maximum on_percent allowed
            ext_temp: Measured outdoor temperature, used to end the cycle with
                UNREACHABLE when it is too close to the passive threshold

        Returns:
            CalibrationResult with on_percent (or None if not calibrating)
        """
        self.follow_mode(hvac_mode, dt_est)

        # If not calibrating, return None
        if self._calibration_state == SmartPICalibrationPhase.IDLE:
            return CalibrationResult(
                on_percent=None,
                phase=self._calibration_state,
                result=self._calibration_result,
                message="idle",
            )
        
        # Suspended by a stop of the thermostat: nothing is commanded
        if self._pause_start_time is not None:
            return CalibrationResult(
                on_percent=0.0,
                phase=self._calibration_state,
                result=self._calibration_result,
                message="paused",
            )

        # Determine heating vs cooling mode
        is_cool = (hvac_mode == VThermHvacMode_COOL)
        on_low = 1.0 if is_cool else 0.0
        on_high = 0.0 if is_cool else 1.0
        
        unreachable_phase = self._unreachable_phase(
            target_temp, current_temp, is_cool, ext_temp
        )
        if unreachable_phase is not None:
            self._calibration_state = SmartPICalibrationPhase.IDLE
            self._calibration_start_time = None
            self._calibration_result = SmartPICalibrationResult.UNREACHABLE
            self._completed_result = self._calibration_result
            return CalibrationResult(
                on_percent=0.0,
                phase=SmartPICalibrationPhase.IDLE,
                result=self._calibration_result,
                message=f"unreachable_{unreachable_phase}",
            )

        on_percent: float = 0.0
        message: str = ""

        # 1. COOL_DOWN: Drive to Low
        if self._calibration_state == SmartPICalibrationPhase.COOL_DOWN:
            on_percent = on_low
            if current_temp <= target_temp - HYST_LOWER_C:
                _LOGGER.info("%s - Calibration: Reached Low Threshold -> HEAT_UP", self._name)
                self._calibration_state = SmartPICalibrationPhase.HEAT_UP
                # Immediate transition to next state output for better responsiveness
                on_percent = on_high
                message = "transition_to_heat_up"
            else:
                message = "cool_down"

        # 2. HEAT_UP: Drive to High (triggers Heat Deadtime)
        elif self._calibration_state == SmartPICalibrationPhase.HEAT_UP:
            on_percent = on_high
            if current_temp >= target_temp + HYST_UPPER_C:
                _LOGGER.info("%s - Calibration: Reached High Threshold -> COOL_DOWN_FINAL", self._name)
                self._calibration_state = SmartPICalibrationPhase.COOL_DOWN_FINAL
                on_percent = on_low
                message = "transition_to_cool_down_final"
            else:
                message = "heat_up"

        # 3. COOL_DOWN_FINAL: Drive back to Low (triggers Cool Deadtime)
        elif self._calibration_state == SmartPICalibrationPhase.COOL_DOWN_FINAL:
            on_percent = on_low
            if current_temp <= target_temp:
                _LOGGER.info("%s - Calibration: Cycle Completed -> IDLE", self._name)
                self._calibration_state = SmartPICalibrationPhase.IDLE
                self._last_calibration_time = time.time()
                self._calibration_start_time = None
                self._calibration_result = SmartPICalibrationResult.SUCCESS
                self._completed_result = self._calibration_result
                message = "completed"
                
                # Check success (logging)
                if not dt_est.deadtime_heat_reliable:
                    _LOGGER.warning("%s - Calibration finished but DeadTime still unreliable.", self._name)
            else:
                message = "cool_down_final"

        # Apply max_on_percent limit if specified
        if max_on_percent is not None and on_percent > max_on_percent:
            on_percent = max_on_percent

        # Feed Estimator (Critical for detection!)
        # The detection runs on the filtered temperature, as in regulation: a
        # raw reading flickering by one sensor step would detect a response at
        # the power step. The raw reading feeds the slope history.
        if current_temp is not None:
            dt_est.update(
                now=time.monotonic(),
                tin=current_temp if filtered_temp is None else filtered_temp,
                tin_raw=current_temp,
                u_applied=on_percent,
                hvac_mode=hvac_mode,
            )

        return CalibrationResult(
            on_percent=on_percent,
            phase=self._calibration_state,
            result=self._calibration_result if message != "completed" else SmartPICalibrationResult.SUCCESS,
            message=message,
        )

    def load_state(self, state: dict) -> None:
        """
        Load state from persistence dict.
        
        Args:
            state: Dictionary containing calibration state
        """
        self._last_calibration_time = state.get("last_calibration_time")
        
        # Load calibration state enum
        calibration_state_str = state.get("calibration_state")
        if calibration_state_str:
            try:
                self._calibration_state = SmartPICalibrationPhase(calibration_state_str)
            except ValueError:
                self._calibration_state = SmartPICalibrationPhase.IDLE
        else:
            self._calibration_state = SmartPICalibrationPhase.IDLE
            
        self._calibration_start_time = wall_to_monotonic_ts(state.get("calibration_start_time"))
        self._force_calibration_requested = state.get("force_calibration_requested", False)
        self._run_mode = state.get("calibration_mode")
        self._pause_start_time = normalize_wall_timestamp(state.get("calibration_pause_start"))
        self._pause_mode = state.get("calibration_pause_mode")
        if self._calibration_state == SmartPICalibrationPhase.IDLE:
            self._run_mode = self._pause_start_time = self._pause_mode = None

        calibration_result_str = state.get("calibration_result")
        if calibration_result_str:
            try:
                self._calibration_result = SmartPICalibrationResult(calibration_result_str)
            except ValueError:
                self._calibration_result = SmartPICalibrationResult.PENDING
        else:
            self._calibration_result = SmartPICalibrationResult.PENDING

    def save_state(self) -> dict:
        """
        Save state to persistence dict.
        
        Returns:
            Dictionary containing calibration state
        """
        return {
            "last_calibration_time": self._last_calibration_time,
            "calibration_state": self._calibration_state.value if self._calibration_state else None,
            "calibration_start_time": monotonic_to_wall_ts(self._calibration_start_time),
            "force_calibration_requested": self._force_calibration_requested,
            "calibration_mode": self._run_mode,
            "calibration_pause_start": self._pause_start_time,
            "calibration_pause_mode": self._pause_mode,
            "calibration_result": self._calibration_result.value if self._calibration_result else None,
        }

    # -------------------------------------------------------------------------
    # Properties for diagnostic access
    # -------------------------------------------------------------------------

    @property
    def state(self) -> SmartPICalibrationPhase:
        """Get current calibration phase."""
        return self._calibration_state

    @property
    def is_calibrating(self) -> bool:
        """Check if calibration is in progress."""
        return self._calibration_state != SmartPICalibrationPhase.IDLE

    @property
    def calibration_result(self) -> SmartPICalibrationResult:
        """Get the result of the current or last calibration."""
        return self._calibration_result

    @property
    def last_calibration_time(self) -> float | None:
        """Get timestamp of last completed calibration."""
        return self._last_calibration_time

    @last_calibration_time.setter
    def last_calibration_time(self, value: float | None) -> None:
        self._last_calibration_time = value

    @property
    def calibration_start_time(self) -> float | None:
        """Get timestamp of current calibration start."""
        return self._calibration_start_time

    @calibration_start_time.setter
    def calibration_start_time(self, value: float | None) -> None:
        self._calibration_start_time = value

    @property
    def calibration_requested(self) -> bool:
        """Check if calibration has been requested."""
        return self._force_calibration_requested

    @calibration_requested.setter
    def calibration_requested(self, value: bool) -> None:
        self._force_calibration_requested = value


# Import VThermHvacMode at end to avoid circular imports
from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT  # noqa: E402
