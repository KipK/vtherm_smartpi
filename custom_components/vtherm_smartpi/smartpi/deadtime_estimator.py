"""
Dead Time Estimator for Smart-PI.
Detects heating/cooling dead time via finite state machine on power transitions.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from typing import Deque, Tuple

from .timestamp_utils import convert_monotonic_to_wall_ts, convert_wall_to_monotonic_ts

_LOGGER = logging.getLogger(__name__)


class DeadTimeEstimator:
    """
    Simplified Dead Time Estimator for Smart-PI.
    Based on Finite State Machine detecting sharp power transitions.
    """

    def __init__(self):
        self.deadtime_heat_s: float | None = None
        self.deadtime_cool_s: float | None = None
        self.deadtime_heat_reliable: bool = False
        self.deadtime_cool_reliable: bool = False

        # Configuration
        self.min_off_time_seconds = 600.0
        self.min_power_heat_threshold = 0.80
        self.min_power_cool_threshold = 0.80
        self.detection_threshold = 0.05
        self.timeout_seconds = 14400.0  # 4 hours timeout for slow systems with inertia

        # State
        self.state = "OFF"  # OFF, HEATING, COOLING, WAITING_HEAT_RESPONSE, WAITING_COOL_RESPONSE
        self.last_power = 0.0
        self.last_stop_time: float | None = None

        # Detection ephemeral data
        self.heat_start_time: float | None = None
        self.heat_start_temp: float | None = None

        self.cool_start_time: float | None = None
        self.cool_peak_temp: float | None = None

        # History for averaging
        self._history_heat = deque(maxlen=6)
        self._history_cool = deque(maxlen=6)

        # History for external access (SmartPI learning)
        self._tin_history: Deque[Tuple[float, float]] = deque(maxlen=300)

    @property
    def tin_history(self) -> Deque[Tuple[float, float]]:
        """Temperature history for learning window slope calculation."""
        return self._tin_history

    def reset(self):
        """Reset estimator state."""
        self.deadtime_heat_s = None
        self.deadtime_cool_s = None
        self.deadtime_heat_reliable = False
        self.deadtime_cool_reliable = False
        self.state = "OFF"
        self.last_power = 0.0
        self.last_stop_time = None
        self._history_heat.clear()
        self._history_cool.clear()
        self._tin_history.clear()

    def update(  # pylint: disable=unused-argument
        self, now: float, tin: float, sp: float, u_applied: float, max_on_percent: float = 1.0, is_hysteresis: bool = False
    ) -> None:
        """
        Update state machine with new measures.
        """
        # Performance/Redundancy Gate: only append to tin_history if temperature changed
        # or if more than 60 seconds passed since the last sample.
        # This prevents redundant points from high-frequency heartbeat triggers
        # while preserving high-resolution inflection points during transitions.
        if not self._tin_history or abs(tin - self._tin_history[-1][1]) > 0.001 or now - self._tin_history[-1][0] >= 60.0:
            self._tin_history.append((now, tin))

        # --- Power Transition Detection ---

        # 0 -> >0 (Heat Start)
        if self.last_power <= 0.01 and u_applied > 0.01:
            allow_start = True

            # 1. Check Power Level
            if u_applied < self.min_power_heat_threshold:
                allow_start = False
                _LOGGER.debug("DeadTime: Heat Start ignored (Power %.2f < %s)", u_applied, self.min_power_heat_threshold)

            # 2. Check Min OFF Time
            if allow_start and self.last_stop_time is not None:
                off_duration = now - self.last_stop_time
                if off_duration < self.min_off_time_seconds:
                    allow_start = False
                    _LOGGER.debug("DeadTime: Heat Start ignored (OFF duration %.0fs < %s)", off_duration, self.min_off_time_seconds)

            if allow_start:
                self.heat_start_time = now
                self.heat_start_temp = tin
                self.state = "WAITING_HEAT_RESPONSE"
                _LOGGER.debug("DeadTime: State -> WAITING_HEAT_RESPONSE (u=%.2f, temp=%.3f)", u_applied, tin)
            else:
                self.state = "HEATING"  # Active but not detecting

        # >0 -> 0 (Cool Start)
        elif self.last_power > 0.01 and u_applied <= 0.01:
            self.last_stop_time = now

            if self.last_power < self.min_power_cool_threshold:
                self.state = "COOLING" # Ignore
                _LOGGER.debug("DeadTime: Cool Start ignored (Prev Power %.2f < %s)", self.last_power, self.min_power_cool_threshold)
            else:
                self.cool_start_time = now
                self.cool_peak_temp = tin
                self.state = "WAITING_COOL_RESPONSE"
                _LOGGER.debug("DeadTime: State -> WAITING_COOL_RESPONSE (temp=%.3f)", tin)

        # --- State Logic ---

        # Abort condition (3.B): if power state reverses while waiting
        # This means the setpoint changed and we shouldn't wait for a response anymore
        if self.state == "WAITING_HEAT_RESPONSE" and u_applied <= 0.01:
            _LOGGER.debug("DeadTime: Aborting %s because power dropped to %.2f", self.state, u_applied)
            self.state = "OFF"
            self.heat_start_time = None
        elif self.state == "WAITING_COOL_RESPONSE" and u_applied > 0.01:
            _LOGGER.debug("DeadTime: Aborting %s because power rose to %.2f", self.state, u_applied)
            self.state = "HEATING"
            self.cool_start_time = None

        if self.state == "WAITING_HEAT_RESPONSE":
            if self.heat_start_time is not None and self.heat_start_temp is not None:
                elapsed = now - self.heat_start_time

                # Check Timeout
                if elapsed > self.timeout_seconds:
                    self.state = "HEATING"
                    _LOGGER.debug("DeadTime: Heat Timeout (%.0fs)", elapsed)
                else:
                    delta = tin - self.heat_start_temp
                    if delta >= self.detection_threshold:
                        # 3.A Look back for inflection point
                        inflection_time = now
                        for t_hist, v_hist in reversed(self._tin_history):
                            if t_hist < self.heat_start_time:
                                break
                            # The temperature started rising here
                            if v_hist <= self.heat_start_temp + 0.01:
                                inflection_time = t_hist
                                break

                        # True deadtime is from heat_start_time to inflection_time
                        dt = max(0.0, inflection_time - self.heat_start_time)

                        self._add_sample_heat(dt)
                        self.state = "HEATING"
                        _LOGGER.info("SmartPI: Heat Deadtime detected = %.1fs (ascension delayed by %.1fs)", dt, now - inflection_time)

        elif self.state == "WAITING_COOL_RESPONSE":
            if self.cool_start_time is not None and self.cool_peak_temp is not None:
                elapsed = now - self.cool_start_time

                # Check Timeout
                if elapsed > self.timeout_seconds:
                    self.state = "COOLING"
                    _LOGGER.debug("DeadTime: Cool Timeout (%.0fs)", elapsed)
                else:
                    # Peak update
                    if tin > self.cool_peak_temp:
                        self.cool_peak_temp = tin

                    # Drop detection
                    delta = self.cool_peak_temp - tin
                    if delta >= self.detection_threshold:
                        # 3.A Look back for inflection point
                        inflection_time = now
                        for t_hist, v_hist in reversed(self._tin_history):
                            if t_hist < self.cool_start_time:
                                break
                            # The temperature started dropping here
                            if v_hist >= self.cool_peak_temp - 0.01:
                                inflection_time = t_hist
                                break

                        dt = max(0.0, inflection_time - self.cool_start_time)

                        self._add_sample_cool(dt)
                        self.state = "COOLING"
                        _LOGGER.info("SmartPI: Cool Deadtime detected = %.1fs (drop delayed by %.1fs)", dt, now - inflection_time)

        # Default states if running without detection
        elif u_applied > 0.01 and self.state == "OFF":
            self.state = "HEATING"
        elif u_applied <= 0.01:
            if self.state != "OFF" and self.state != "WAITING_COOL_RESPONSE" and self.state != "COOLING":
                self.state = "OFF"

        self.last_power = u_applied

    def _add_sample_heat(self, dt: float):
        self._history_heat.append(dt)
        self.deadtime_heat_s = statistics.mean(self._history_heat)
        self.deadtime_heat_reliable = len(self._history_heat) >= 1

    def _add_sample_cool(self, dt: float):
        self._history_cool.append(dt)
        self.deadtime_cool_s = statistics.mean(self._history_cool)
        self.deadtime_cool_reliable = len(self._history_cool) >= 1

    def save_state(self) -> dict:
        """Save state for persistence."""
        return {
            "deadtime_heat_s": self.deadtime_heat_s,
            "deadtime_cool_s": self.deadtime_cool_s,
            "deadtime_heat_reliable": self.deadtime_heat_reliable,
            "deadtime_cool_reliable": self.deadtime_cool_reliable,
            "history_heat": list(self._history_heat),
            "history_cool": list(self._history_cool),
            "state": self.state,
            "last_stop_time": convert_monotonic_to_wall_ts(self.last_stop_time),
            "heat_start_time": convert_monotonic_to_wall_ts(self.heat_start_time),
            "heat_start_temp": self.heat_start_temp,
            "cool_start_time": convert_monotonic_to_wall_ts(self.cool_start_time),
            "cool_peak_temp": self.cool_peak_temp,
            "tin_history": [(convert_monotonic_to_wall_ts(t), v) for t, v in self._tin_history],
        }

    def load_state(self, state: dict) -> None:
        """Restore state."""
        if not state:
            return
        self.deadtime_heat_s = state.get("deadtime_heat_s")
        self.deadtime_cool_s = state.get("deadtime_cool_s")
        self.deadtime_heat_reliable = bool(state.get("deadtime_heat_reliable", False))
        self.deadtime_cool_reliable = bool(state.get("deadtime_cool_reliable", False))

        hh = state.get("history_heat", [])
        self._history_heat = deque(hh, maxlen=6)
        hc = state.get("history_cool", [])
        self._history_cool = deque(hc, maxlen=6)

        # Restore detection state
        self.state = state.get("state", "OFF")
        self.last_stop_time = convert_wall_to_monotonic_ts(state.get("last_stop_time"))
        self.heat_start_time = convert_wall_to_monotonic_ts(state.get("heat_start_time"))
        self.heat_start_temp = state.get("heat_start_temp")
        self.cool_start_time = convert_wall_to_monotonic_ts(state.get("cool_start_time"))
        self.cool_peak_temp = state.get("cool_peak_temp")

        # Restore tin_history
        th = state.get("tin_history", [])
        self._tin_history.clear()
        for t_wall, v in th:
            t_mono = convert_wall_to_monotonic_ts(t_wall)
            if t_mono is not None:
                self._tin_history.append((t_mono, v))
