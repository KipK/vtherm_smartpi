"""
Dead Time Estimator for Smart-PI.
Detects heating/cooling dead time via finite state machine on power transitions.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from typing import Deque, Tuple

from ..hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
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
        self.model_hvac_mode: str | None = None

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
        self.heat_trough_temp: float | None = None

        self.cool_start_time: float | None = None
        self.cool_peak_temp: float | None = None
        self._waiting_power_on: bool | None = None

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
        self.model_hvac_mode = None
        self.state = "OFF"
        self.last_power = 0.0
        self.last_stop_time = None
        self.heat_start_time = None
        self.heat_start_temp = None
        self.heat_trough_temp = None
        self.cool_start_time = None
        self.cool_peak_temp = None
        self._waiting_power_on = None
        self._history_heat.clear()
        self._history_cool.clear()
        self._tin_history.clear()

    def reset_observation(self, current_power: float = 0.0) -> None:
        """Clear transient detection state while preserving learned dead times."""
        self.state = "OFF"
        self.last_power = float(current_power)
        self.last_stop_time = None
        self.heat_start_time = None
        self.heat_start_temp = None
        self.heat_trough_temp = None
        self.cool_start_time = None
        self.cool_peak_temp = None
        self._waiting_power_on = None
        self._tin_history.clear()

    def ensure_hvac_mode(self, hvac_mode: VThermHvacMode | None) -> bool:
        """Ensure persisted transition data uses physical response semantics.

        Heat/cool dead times describe a rising/falling room-temperature response,
        respectively. Legacy COOL states classified them by power transition and
        are therefore reset once before the new mode-aware detector is used.
        """
        if hvac_mode not in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            return False

        mode_name = str(hvac_mode)
        has_legacy_data = (
            self.deadtime_heat_s is not None
            or self.deadtime_cool_s is not None
            or bool(self._history_heat)
            or bool(self._history_cool)
        )
        reset_required = (
            self.model_hvac_mode is not None
            and self.model_hvac_mode != mode_name
        ) or (
            self.model_hvac_mode is None
            and hvac_mode == VThermHvacMode_COOL
            and has_legacy_data
        )
        if reset_required:
            self.reset()
        self.model_hvac_mode = mode_name
        return reset_required

    def active_deadtime(self, hvac_mode: VThermHvacMode) -> tuple[float | None, bool]:
        """Return dead time for the temperature response caused by active power."""
        if hvac_mode == VThermHvacMode_COOL:
            return self.deadtime_cool_s, self.deadtime_cool_reliable
        return self.deadtime_heat_s, self.deadtime_heat_reliable

    def passive_deadtime(self, hvac_mode: VThermHvacMode) -> tuple[float | None, bool]:
        """Return dead time for the passive response after active power stops."""
        if hvac_mode == VThermHvacMode_COOL:
            return self.deadtime_heat_s, self.deadtime_heat_reliable
        return self.deadtime_cool_s, self.deadtime_cool_reliable

    def _start_response(
        self,
        *,
        response: str,
        now: float,
        tin: float,
        power_on: bool,
    ) -> None:
        """Arm a physical rising or falling temperature response."""
        self._waiting_power_on = power_on
        if response == "cool":
            self.cool_start_time = now
            self.cool_peak_temp = tin
            self.state = "WAITING_COOL_RESPONSE"
        else:
            self.heat_start_time = now
            self.heat_start_temp = tin
            self.heat_trough_temp = tin
            self.state = "WAITING_HEAT_RESPONSE"
        _LOGGER.debug(
            "DeadTime: State -> %s (power_on=%s, temp=%.3f)",
            self.state,
            power_on,
            tin,
        )

    def update(  # pylint: disable=unused-argument
        self,
        now: float,
        tin: float,
        sp: float,
        u_applied: float,
        max_on_percent: float = 1.0,
        is_hysteresis: bool = False,
        hvac_mode: VThermHvacMode = VThermHvacMode_HEAT,
    ) -> None:
        """
        Update state machine with new measures.
        """
        self.ensure_hvac_mode(hvac_mode)
        active_response = "cool" if hvac_mode == VThermHvacMode_COOL else "heat"
        passive_response = "heat" if hvac_mode == VThermHvacMode_COOL else "cool"

        # Performance/Redundancy Gate: only append to tin_history if temperature changed
        # or if more than 60 seconds passed since the last sample.
        # This prevents redundant points from high-frequency heartbeat triggers
        # while preserving high-resolution inflection points during transitions.
        if not self._tin_history or abs(tin - self._tin_history[-1][1]) > 0.001 or now - self._tin_history[-1][0] >= 60.0:
            self._tin_history.append((now, tin))

        # --- Power Transition Detection ---

        # 0 -> >0: active HVAC response.
        if self.last_power <= 0.01 and u_applied > 0.01:
            allow_start = True

            threshold = (
                self.min_power_cool_threshold
                if active_response == "cool"
                else self.min_power_heat_threshold
            )
            if u_applied < threshold:
                allow_start = False
                _LOGGER.debug(
                    "DeadTime: Active response ignored (Power %.2f < %s)",
                    u_applied,
                    threshold,
                )

            # 2. Check Min OFF Time
            if allow_start and self.last_stop_time is not None:
                off_duration = now - self.last_stop_time
                if off_duration < self.min_off_time_seconds:
                    allow_start = False
                    _LOGGER.debug("DeadTime: Heat Start ignored (OFF duration %.0fs < %s)", off_duration, self.min_off_time_seconds)

            if allow_start:
                self._start_response(
                    response=active_response,
                    now=now,
                    tin=tin,
                    power_on=True,
                )
            else:
                self.state = "COOLING" if active_response == "cool" else "HEATING"
                self._waiting_power_on = None

        # >0 -> 0: passive room response.
        elif self.last_power > 0.01 and u_applied <= 0.01:
            self.last_stop_time = now

            threshold = (
                self.min_power_cool_threshold
                if passive_response == "cool"
                else self.min_power_heat_threshold
            )
            if self.last_power < threshold:
                self.state = "COOLING" if passive_response == "cool" else "HEATING"
                self._waiting_power_on = None
                _LOGGER.debug(
                    "DeadTime: Passive response ignored (Prev Power %.2f < %s)",
                    self.last_power,
                    threshold,
                )
            else:
                self._start_response(
                    response=passive_response,
                    now=now,
                    tin=tin,
                    power_on=False,
                )

        # --- State Logic ---

        # Abort condition (3.B): if power state reverses while waiting
        # This means the setpoint changed and we shouldn't wait for a response anymore
        power_on = u_applied > 0.01
        if (
            self.state in {"WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE"}
            and self._waiting_power_on is not None
            and power_on != self._waiting_power_on
        ):
            _LOGGER.debug(
                "DeadTime: Aborting %s because power state changed",
                self.state,
            )
            if self.state == "WAITING_HEAT_RESPONSE":
                self.heat_start_time = None
                self.heat_trough_temp = None
            else:
                self.cool_start_time = None
            current_response = active_response if power_on else passive_response
            self.state = "COOLING" if current_response == "cool" else "HEATING"
            self._waiting_power_on = None

        if self.state == "WAITING_HEAT_RESPONSE":
            if self.heat_start_time is not None and self.heat_trough_temp is not None:
                elapsed = now - self.heat_start_time

                # Check Timeout
                if elapsed > self.timeout_seconds:
                    self.state = "HEATING"
                    self._waiting_power_on = None
                    _LOGGER.debug("DeadTime: Heat Timeout (%.0fs)", elapsed)
                else:
                    if tin < self.heat_trough_temp:
                        self.heat_trough_temp = tin
                    delta = tin - self.heat_trough_temp
                    if delta >= self.detection_threshold:
                        # 3.A Look back for inflection point
                        inflection_time = now
                        for t_hist, v_hist in reversed(self._tin_history):
                            if t_hist < self.heat_start_time:
                                break
                            # The temperature started rising here
                            if v_hist <= self.heat_trough_temp + 0.01:
                                inflection_time = t_hist
                                break

                        # True deadtime is from heat_start_time to inflection_time
                        dt = max(0.0, inflection_time - self.heat_start_time)

                        self._add_sample_heat(dt)
                        self.state = "HEATING"
                        self._waiting_power_on = None
                        _LOGGER.info("SmartPI: Heat Deadtime detected = %.1fs (ascension delayed by %.1fs)", dt, now - inflection_time)

        elif self.state == "WAITING_COOL_RESPONSE":
            if self.cool_start_time is not None and self.cool_peak_temp is not None:
                elapsed = now - self.cool_start_time

                # Check Timeout
                if elapsed > self.timeout_seconds:
                    self.state = "COOLING"
                    self._waiting_power_on = None
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
                        self._waiting_power_on = None
                        _LOGGER.info("SmartPI: Cool Deadtime detected = %.1fs (drop delayed by %.1fs)", dt, now - inflection_time)

        # Default states if running without detection
        elif u_applied > 0.01 and self.state == "OFF":
            self.state = "COOLING" if active_response == "cool" else "HEATING"
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
            "model_hvac_mode": self.model_hvac_mode,
            "history_heat": list(self._history_heat),
            "history_cool": list(self._history_cool),
            "state": self.state,
            "last_stop_time": convert_monotonic_to_wall_ts(self.last_stop_time),
            "heat_start_time": convert_monotonic_to_wall_ts(self.heat_start_time),
            "heat_start_temp": self.heat_start_temp,
            "heat_trough_temp": self.heat_trough_temp,
            "cool_start_time": convert_monotonic_to_wall_ts(self.cool_start_time),
            "cool_peak_temp": self.cool_peak_temp,
            "waiting_power_on": self._waiting_power_on,
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
        persisted_mode = state.get("model_hvac_mode")
        self.model_hvac_mode = (
            str(persisted_mode)
            if persisted_mode in {
                str(VThermHvacMode_HEAT),
                str(VThermHvacMode_COOL),
            }
            else None
        )

        hh = state.get("history_heat", [])
        self._history_heat = deque(hh, maxlen=6)
        hc = state.get("history_cool", [])
        self._history_cool = deque(hc, maxlen=6)

        # Restore detection state
        self.state = state.get("state", "OFF")
        self.last_stop_time = convert_wall_to_monotonic_ts(state.get("last_stop_time"))
        self.heat_start_time = convert_wall_to_monotonic_ts(state.get("heat_start_time"))
        self.heat_start_temp = state.get("heat_start_temp")
        self.heat_trough_temp = state.get("heat_trough_temp", self.heat_start_temp)
        self.cool_start_time = convert_wall_to_monotonic_ts(state.get("cool_start_time"))
        self.cool_peak_temp = state.get("cool_peak_temp")
        self._waiting_power_on = state.get("waiting_power_on")

        # Restore tin_history
        th = state.get("tin_history", [])
        self._tin_history.clear()
        for t_wall, v in th:
            t_mono = convert_wall_to_monotonic_ts(t_wall)
            if t_mono is not None:
                self._tin_history.append((t_mono, v))
