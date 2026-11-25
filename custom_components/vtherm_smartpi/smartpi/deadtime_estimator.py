"""
Dead Time Estimator for Smart-PI.
Detects heating/cooling dead time via finite state machine on power transitions.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from typing import Callable, Deque, Tuple

from ..hvac_mode import (
    VThermHvacMode,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from .const import (
    DEADTIME_ACTIVE_TIMEOUT_S,
    DEADTIME_PASSIVE_TIMEOUT_S,
    STOP_PEAK_DROP_MIN_C,
    STOP_PEAK_DROP_STEPS,
)
from .deadtime_trend import prestart_trend
from .peak_search import PeakSearch
from .sensor_step import sensor_step
from .temperature_history import TemperatureHistory
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

# Applied power (share of full power) of the cycle before a stop for the stop to end a heating at full power.
FULL_POWER_STOP_MIN = 0.99

_LOGGER = logging.getLogger(__name__)

# Samples of the dead time after a stop kept for the audit record.
_PASSIVE_RECORD_MAX = 500


class DeadTimeEstimator:
    """
    Simplified Dead Time Estimator for Smart-PI.
    Based on Finite State Machine detecting sharp power transitions.
    """

    def __init__(
        self, passive_by_peak: bool = False, measured_start: Callable[[], bool] | None = None,
        history: TemperatureHistory | None = None,
    ):
        self.deadtime_heat_s: float | None = None
        self.deadtime_cool_s: float | None = None
        self.deadtime_heat_reliable: bool = False
        self.deadtime_cool_reliable: bool = False
        self.model_hvac_mode: str | None = None
        # Learned dead times of the HVAC mode that is not active, by mode name, as saved state (the "heat" and "cool"
        # slots swap meaning between modes, so each mode keeps both). Empty for a thermostat that never changes mode.
        self._mode_stash: dict[str, dict] = {}

        # Configuration
        self.min_off_time_seconds = 600.0
        self.min_power_heat_threshold = 0.80
        self.min_power_cool_threshold = 0.80
        self.detection_threshold = 0.05
        self.timeout_seconds = DEADTIME_ACTIVE_TIMEOUT_S
        self.passive_timeout_seconds = DEADTIME_PASSIVE_TIMEOUT_S

        # State
        self.state = "OFF"  # OFF, HEATING, COOLING, WAITING_HEAT_RESPONSE, WAITING_COOL_RESPONSE
        self.last_power = 0.0
        self.last_stop_time: float | None = None

        # Detection ephemeral data
        self.heat_start_time: float | None = None
        self.heat_trough_temp: float | None = None

        self.cool_start_time: float | None = None
        self.cool_peak_temp: float | None = None
        self._waiting_power_on: bool | None = None
        # Last instant near the trough (heat) or the peak (cool) of the
        # response being waited for: the dead time does not depend on the
        # retention of the temperature history when the drop comes late.
        self._near_extreme_time: float | None = None
        # Free-running trend (degC/s) removed from the temperature while an
        # active response is waited for; 0.0 for a passive response or a room
        # without a trend in the direction of the response.
        self._trend_slope: float = 0.0
        # The armed active response follows a temperature already moving in
        # the direction of the response, or a window too short to evaluate
        # its trend: weak evidence, which never shortens the published value.
        self._weak_evidence: bool = False
        # Outcome of the last detection per response ("detected", "timeout",
        # "aborted", "rejected", "ignored"), published for diagnostics.
        self.last_outcome: dict[str, str | None] = {"heat": None, "cool": None}

        # Last detections; the published dead time is their median so that a
        # single aberrant detection does not move it.
        self._history_heat = deque(maxlen=6)
        # Samples added since the creation, by slot (runtime only): a reader sees what was measured since its last look.
        self.samples_added = {"heat": 0, "cool": 0}
        self._history_cool = deque(maxlen=6)

        # The room temperatures the estimator records at each update (smartpi/temperature_history.py), shared with
        # the learning: the raw history feeds the learning slopes, the filtered one the inflection lookback of the
        # detection, so that it stays consistent with the trough/peak tracking.
        self.history = history if history is not None else TemperatureHistory()
        self._tin_history = self.history.raw
        self._tin_lp_history = self.history.filtered
        # Detection state saved before a restart (wall-clock times), resumed
        # only when the restart is bridged (resume_after_restart).
        self._restart_detection: dict | None = None

        # True: the dead time after a stop is read on the temperature peak (peak_search.py), from full-power stops
        # only. The search runs from a stop to its confirmation: a heating again, a mode change or a reset drops it.
        self.passive_by_peak = bool(passive_by_peak)
        self._stop_peak: PeakSearch | None = None
        self._stop_peak_confirmed = False
        # The cycle before the stop being measured was at full power (only such a stop feeds the value).
        self._stop_from_full_power = False
        # Tells whether a start is made by a measurement pulse or the hysteresis (None: always). False while the
        # regulation drives the command (peak mode): a start then only lengthens the dead time after a start,
        # which the pulses (or the bootstrap) have measured from a rest.
        self._start_source = measured_start
        # Read-only record for the audit: every sample added to the passive dead time, with its origin.
        self._passive_measures: list[dict] = []
        self._passive_first_published_s: float | None = None

    @property
    def measured_start(self) -> bool:
        """True when the start being read is a measurement one (read at the calculation that reads it)."""
        return True if self._start_source is None else bool(self._start_source())

    @property
    def passive_measures(self) -> list[dict]:
        """Samples of the dead time after a stop, oldest first: time (monotonic s), response, value (s), origin.

        ``response`` is the passive response of the mode: ``"cool"`` (the room falls after a stop in HEAT)
        or ``"heat"`` (it rises after a stop in COOL).

        The origin is ``"peak"`` (read on the temperature peak after a stop) or ``"detection"`` (present
        detection of the passive response). ``fed`` is False for a peak that does not feed the published value
        (a stop from a lower power).
        """
        return [dict(m) for m in self._passive_measures]

    @property
    def passive_first_published_s(self) -> float | None:
        """Monotonic time of the first sample of the dead time after a stop, None before."""
        return self._passive_first_published_s

    @property
    def stop_peak_confirmed(self) -> bool:
        """True from the confirmation of the peak of a stop until the heating starts again."""
        return self._stop_peak_confirmed

    @property
    def stop_response_pending(self) -> bool:
        """True while the response to the last stop is awaited: its detection is armed or its peak is searched."""
        waiting = self.state in ("WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE") and self._waiting_power_on is False
        return self._stop_peak is not None or waiting

    @property
    def tin_history(self) -> Deque[Tuple[float, float]]:
        """The raw temperature history of the holder."""
        return self._tin_history

    def reset(self):
        """Reset estimator state, the dead times set aside for the other mode included."""
        self._mode_stash = {}
        self._reset_active()

    def _reset_active(self):
        """Reset the state of the active mode; what is set aside for the other mode stays."""
        self.deadtime_heat_s = None
        self.deadtime_cool_s = None
        self.deadtime_heat_reliable = False
        self.deadtime_cool_reliable = False
        self.model_hvac_mode = None
        self.state = "OFF"
        self.last_power = 0.0
        self.last_stop_time = None
        self.heat_start_time = None
        self.heat_trough_temp = None
        self.cool_start_time = None
        self.cool_peak_temp = None
        self._waiting_power_on = None
        self._near_extreme_time = None
        self._trend_slope = 0.0
        self._weak_evidence = False
        self.last_outcome = {"heat": None, "cool": None}
        self._history_heat.clear()
        self._history_cool.clear()
        self.history.clear()
        self._restart_detection = None
        self._stop_peak = None
        self._stop_peak_confirmed = False
        self._stop_from_full_power = False
        self._passive_measures.clear()
        self._passive_first_published_s = None

    def _learned_snapshot(self) -> dict | None:
        """The learned dead times as saved state, None when nothing was learned."""
        if not (
            self.deadtime_heat_s is not None
            or self.deadtime_cool_s is not None
            or self._history_heat
            or self._history_cool
        ):
            return None
        return {
            "deadtime_heat_s": self.deadtime_heat_s,
            "deadtime_cool_s": self.deadtime_cool_s,
            "deadtime_heat_reliable": self.deadtime_heat_reliable,
            "deadtime_cool_reliable": self.deadtime_cool_reliable,
            "history_heat": list(self._history_heat),
            "history_cool": list(self._history_cool),
        }

    def _restore_learned(self, snap: dict) -> None:
        """Take back the dead times set aside for the mode, on top of a state just reset. A malformed snapshot is
        dropped."""
        try:
            heat, cool = snap["deadtime_heat_s"], snap["deadtime_cool_s"]
            heat = None if heat is None else float(heat)
            cool = None if cool is None else float(cool)
            hist_heat = [float(v) for v in snap["history_heat"]]
            hist_cool = [float(v) for v in snap["history_cool"]]
            heat_reliable = bool(snap["deadtime_heat_reliable"])
            cool_reliable = bool(snap["deadtime_cool_reliable"])
        except (KeyError, TypeError, ValueError):
            return
        self.deadtime_heat_s, self.deadtime_cool_s = heat, cool
        self.deadtime_heat_reliable, self.deadtime_cool_reliable = heat_reliable, cool_reliable
        self._history_heat = deque(hist_heat, maxlen=6)
        self._history_cool = deque(hist_cool, maxlen=6)

    def reset_observation(self, current_power: float = 0.0, keep_history: bool = False) -> None:
        """Clear transient detection state while preserving learned dead times (and the temperature history with
        ``keep_history``: the anticipation and the pulses read it)."""
        self.state = "OFF"
        self.last_power = float(current_power)
        self.last_stop_time = None
        self.heat_start_time = None
        self.heat_trough_temp = None
        self.cool_start_time = None
        self.cool_peak_temp = None
        self._waiting_power_on = None
        self._near_extreme_time = None
        self._trend_slope = 0.0
        self._weak_evidence = False
        if not keep_history:
            self.history.clear()
        self._stop_peak = None
        self._stop_peak_confirmed = False

    @property
    def waiting_elapsed_s(self) -> float | None:
        """Age of the response being waited for, None when no detection is armed."""
        start = (
            self.heat_start_time if self.state == "WAITING_HEAT_RESPONSE"
            else self.cool_start_time if self.state == "WAITING_COOL_RESPONSE"
            else None
        )
        if start is None or not self._tin_history:
            return None
        return max(0.0, self._tin_history[-1][0] - start)

    def ensure_hvac_mode(self, hvac_mode: VThermHvacMode | None) -> bool:
        """Ensure persisted transition data uses physical response semantics.

        Heat/cool dead times describe a rising/falling room-temperature response,
        respectively. Legacy COOL states classified them by power transition and
        are therefore reset once before the new mode-aware detector is used.

        On a change of mode the dead times of the outgoing mode are set aside and
        the ones set aside for the incoming mode, if any, are taken back.
        """
        if hvac_mode not in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
            return False

        mode_name = str(hvac_mode)
        mode_changed = self.model_hvac_mode is not None and self.model_hvac_mode != mode_name
        has_legacy_data = (
            self.deadtime_heat_s is not None
            or self.deadtime_cool_s is not None
            or bool(self._history_heat)
            or bool(self._history_cool)
        )
        reset_required = mode_changed or (
            self.model_hvac_mode is None
            and hvac_mode == VThermHvacMode_COOL
            and has_legacy_data
        )
        if mode_changed:
            outgoing = self._learned_snapshot()
            if outgoing is None:
                self._mode_stash.pop(self.model_hvac_mode, None)
            else:
                self._mode_stash[self.model_hvac_mode] = outgoing
        if reset_required:
            self._reset_active()
        if mode_changed and mode_name in self._mode_stash:
            self._restore_learned(self._mode_stash.pop(mode_name))
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
        off_since: float | None = None,
    ) -> None:
        """Arm a physical rising or falling temperature response.

        An active response (power on) is measured against the free-running
        trend of the OFF period before the start (``off_since`` onwards); a
        trend too irregular to be extrapolated rejects the detection.
        """
        # A response still waited for is replaced by the new power transition.
        if self.state == "WAITING_HEAT_RESPONSE":
            self.last_outcome["heat"] = "aborted"
        elif self.state == "WAITING_COOL_RESPONSE":
            self.last_outcome["cool"] = "aborted"
        self._trend_slope = 0.0
        self._weak_evidence = False
        if power_on:
            trend = prestart_trend(
                self._tin_history,
                start_ts=now,
                off_since_ts=off_since,
                direction=_direction(response),
                detection_threshold_c=self.detection_threshold,
            )
            if trend.rejected:
                self.last_outcome[response] = "rejected"
                self.state = "COOLING" if response == "cool" else "HEATING"
                self._waiting_power_on = None
                _LOGGER.debug(
                    "DeadTime: %s response not armed (trend before the start not extrapolable)",
                    response,
                )
                return
            self._trend_slope = trend.slope_c_per_s
            self._weak_evidence = (
                trend.toward_response or trend.window_too_short
                or (self.passive_by_peak and not self.measured_start)
            )
        self._waiting_power_on = power_on
        self._near_extreme_time = now
        if response == "cool":
            self.cool_start_time = now
            self.cool_peak_temp = tin
            self.state = "WAITING_COOL_RESPONSE"
        else:
            self.heat_start_time = now
            self.heat_trough_temp = tin
            self.state = "WAITING_HEAT_RESPONSE"
        _LOGGER.debug(
            "DeadTime: State -> %s (power_on=%s, temp=%.3f, trend=%.3f degC/h)",
            self.state,
            power_on,
            tin,
            self._trend_slope * 3600.0,
        )

    def _detrended(self, ts: float, value: float, start: float) -> float:
        """Return the temperature minus the extrapolated free-running trend."""
        return value - self._trend_slope * (ts - start)

    def _would_shorten(self, published_s: float | None, dt: float) -> bool:
        """Return True when a weakly supported measurement is shorter than the published value.

        A room warming before the start, or a window too short to tell, makes
        the response look earlier than it is: such a measurement is only kept
        when it lengthens (or equals) the published value. Before the first
        published value nothing is shortened and the usual rules apply.
        """
        return self._weak_evidence and published_s is not None and dt < published_s

    def _trend_dominates(self, response: str, now: float, start: float) -> bool:
        """Return True when the trend accounts for at least the detection threshold.

        The response is then not separable from the free-running trend: the
        extrapolation carries as much of the move as the response itself.
        """
        drift = _direction(response) * self._trend_slope * (now - start)
        return drift >= self.detection_threshold

    def update(
        self,
        now: float,
        tin: float,
        u_applied: float,
        hvac_mode: VThermHvacMode = VThermHvacMode_HEAT,
        tin_raw: float | None = None,
    ) -> None:
        """
        Update state machine with new measures.

        ``tin`` drives the detection (trough/peak tracking and inflection
        lookback). ``tin_raw`` is the raw measured temperature stored in
        ``tin_history`` for learning slopes; it defaults to ``tin``.
        """
        self.ensure_hvac_mode(hvac_mode)
        active_response = "cool" if hvac_mode == VThermHvacMode_COOL else "heat"
        passive_response = "heat" if hvac_mode == VThermHvacMode_COOL else "cool"

        self.history.append(now, tin, tin_raw)

        by_peak = self.passive_by_peak
        if by_peak and u_applied > 0.01:
            # The heating runs (or starts again): the bin in progress of a stop not yet measured is closed and may
            # still confirm its peak early; otherwise the stop gives no measure.
            if self._stop_peak is not None:
                self._feed_stop_peak(now, passive_response, heating=True)
            self._stop_peak = None
            self._stop_peak_confirmed = False

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
                    off_since=self._free_running_since(hvac_mode),
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
            if by_peak:
                # The passive response is not detected: the dead time after this stop is read on its peak.
                history = self._tin_history
                self._stop_peak = PeakSearch(
                    now, -1.0 if hvac_mode == VThermHvacMode_COOL else 1.0,
                    drop_floor_c=max(STOP_PEAK_DROP_MIN_C, STOP_PEAK_DROP_STEPS * sensor_step(history)),
                    cut_value=history[-1][1] if history else None,
                )
                self._stop_peak_confirmed = False
                self._stop_from_full_power = self.last_power >= FULL_POWER_STOP_MIN
                if self.state == "WAITING_HEAT_RESPONSE":
                    self.last_outcome["heat"] = "aborted"
                elif self.state == "WAITING_COOL_RESPONSE":
                    self.last_outcome["cool"] = "aborted"
                self.state = "COOLING" if passive_response == "cool" else "HEATING"
                self._waiting_power_on = None
            elif self.last_power < threshold:
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
                self.last_outcome["heat"] = "aborted"
            else:
                self.cool_start_time = None
                self.last_outcome["cool"] = "aborted"
            current_response = active_response if power_on else passive_response
            self.state = "COOLING" if current_response == "cool" else "HEATING"
            self._waiting_power_on = None

        if self.state == "WAITING_HEAT_RESPONSE":
            if self.heat_start_time is not None and self.heat_trough_temp is not None:
                elapsed = now - self.heat_start_time

                # Check Timeout
                if elapsed > self._timeout_s():
                    self.state = "HEATING"
                    self._waiting_power_on = None
                    self.last_outcome["heat"] = "timeout"
                    _LOGGER.debug("DeadTime: Heat Timeout (%.0fs)", elapsed)
                else:
                    start = self.heat_start_time
                    level = self._detrended(now, tin, start)
                    if level < self.heat_trough_temp:
                        self.heat_trough_temp = level
                    if level <= self.heat_trough_temp + 0.01:
                        self._near_extreme_time = now
                    delta = level - self.heat_trough_temp
                    if delta >= self.detection_threshold:
                        # 3.A Look back for inflection point
                        inflection_time = self._near_extreme_time or now
                        for t_hist, v_hist in reversed(self._tin_lp_history):
                            if t_hist < start:
                                break
                            # The temperature started rising here
                            if self._detrended(t_hist, v_hist, start) <= self.heat_trough_temp + 0.01:
                                inflection_time = t_hist
                                break

                        # True deadtime is from heat_start_time to inflection_time
                        dt = max(0.0, inflection_time - start)

                        self.state = "HEATING"
                        self._waiting_power_on = None
                        if self._trend_dominates("heat", now, start):
                            self.last_outcome["heat"] = "rejected"
                            _LOGGER.debug("DeadTime: Heat detection rejected (trend before the start dominates the rise)")
                        elif self._would_shorten(self.deadtime_heat_s, dt):
                            self.last_outcome["heat"] = "ignored"
                            _LOGGER.debug("DeadTime: Heat detection %.1fs ignored (shorter than %.1fs on weak evidence)", dt, self.deadtime_heat_s)
                        else:
                            if passive_response == "heat":
                                self._record_passive("heat", dt, now, "detection")
                            self._add_sample_heat(dt)
                            self.last_outcome["heat"] = "detected"
                            _LOGGER.info("SmartPI: Heat Deadtime detected = %.1fs (ascension delayed by %.1fs)", dt, now - inflection_time)

        elif self.state == "WAITING_COOL_RESPONSE":
            if self.cool_start_time is not None and self.cool_peak_temp is not None:
                elapsed = now - self.cool_start_time

                # Check Timeout
                if elapsed > self._timeout_s():
                    self.state = "COOLING"
                    self._waiting_power_on = None
                    self.last_outcome["cool"] = "timeout"
                    _LOGGER.debug("DeadTime: Cool Timeout (%.0fs)", elapsed)
                else:
                    start = self.cool_start_time
                    level = self._detrended(now, tin, start)
                    # Peak update
                    if level > self.cool_peak_temp:
                        self.cool_peak_temp = level
                    if level >= self.cool_peak_temp - 0.01:
                        self._near_extreme_time = now

                    # Drop detection
                    delta = self.cool_peak_temp - level
                    if delta >= self.detection_threshold:
                        # 3.A Look back for inflection point
                        inflection_time = self._near_extreme_time or now
                        for t_hist, v_hist in reversed(self._tin_lp_history):
                            if t_hist < start:
                                break
                            # The temperature started dropping here
                            if self._detrended(t_hist, v_hist, start) >= self.cool_peak_temp - 0.01:
                                inflection_time = t_hist
                                break

                        dt = max(0.0, inflection_time - start)

                        self.state = "COOLING"
                        self._waiting_power_on = None
                        if self._trend_dominates("cool", now, start):
                            self.last_outcome["cool"] = "rejected"
                            _LOGGER.debug("DeadTime: Cool detection rejected (trend before the start dominates the drop)")
                        elif self._would_shorten(self.deadtime_cool_s, dt):
                            self.last_outcome["cool"] = "ignored"
                            _LOGGER.debug("DeadTime: Cool detection %.1fs ignored (shorter than %.1fs on weak evidence)", dt, self.deadtime_cool_s)
                        else:
                            if passive_response == "cool":
                                self._record_passive("cool", dt, now, "detection")
                            self._add_sample_cool(dt)
                            self.last_outcome["cool"] = "detected"
                            _LOGGER.info("SmartPI: Cool Deadtime detected = %.1fs (drop delayed by %.1fs)", dt, now - inflection_time)

        # Default states if running without detection
        elif u_applied > 0.01 and self.state == "OFF":
            self.state = "COOLING" if active_response == "cool" else "HEATING"
        elif u_applied <= 0.01:
            if self.state != "OFF" and self.state != "WAITING_COOL_RESPONSE" and self.state != "COOLING":
                self.state = "OFF"

        if by_peak and self._stop_peak is not None:
            self._feed_stop_peak(now, passive_response, heating=False)

        self.last_power = u_applied

    def _feed_stop_peak(self, now: float, passive_response: str, heating: bool) -> None:
        """Feed the sample of ``now`` to the peak search of the last stop. ``heating``: the heating starts again,
        the bin in progress is closed and the search ends. Only a confirmed peak gives a measure; none is made when
        the peak is not found within the wait."""
        search = self._stop_peak
        confirmed = False
        if self._tin_history and self._tin_history[-1][0] == now and now >= search.t_cut:
            # The raw samples of the history feed the peak search, as they feed the measurement events.
            confirmed = search.feed(now, self._tin_history[-1][1])
            if not confirmed and search.expired(now):
                self._stop_peak = None
                return
        if not confirmed and heating:
            confirmed = search.close_at_heating()
        if not confirmed:
            return
        self._stop_peak = None
        # Confirmed at the start of a heating: the stop is over, nothing waits for its peak any more.
        self._stop_peak_confirmed = not heating
        dead_time_s = search.peak_delay_s
        self._add_passive_sample(passive_response, dead_time_s, now, "peak", fed=self._stop_from_full_power)
        _LOGGER.info(
            "SmartPI: Dead time after a stop read on the peak = %.1fs (%s response)", dead_time_s, passive_response,
        )

    def _add_passive_sample(self, response: str, dt: float, now: float, origin: str, fed: bool = True) -> None:
        """Add a sample of the dead time after a stop (median and reliability rule of the estimator); a sample that
        is not fed is only recorded."""
        self._record_passive(response, dt, now, origin, fed)
        if not fed:
            return
        if response == "cool":
            self._add_sample_cool(dt)
        else:
            self._add_sample_heat(dt)

    def _record_passive(self, response: str, dt: float, now: float, origin: str, fed: bool = True) -> None:
        self._passive_measures.append(
            {"t_s": float(now), "response": response, "value_s": float(dt), "origin": origin, "fed": bool(fed)}
        )
        del self._passive_measures[:-_PASSIVE_RECORD_MAX]
        if fed and self._passive_first_published_s is None:
            self._passive_first_published_s = float(now)

    def _free_running_since(self, hvac_mode: VThermHvacMode) -> float | None:
        """Return the start of the free-running part of the current OFF period.

        Right after a stop the emitter keeps acting on the room for the
        passive dead time; the trend before a start is taken after it.
        """
        if self.last_stop_time is None:
            return None
        passive_s, passive_reliable = self.passive_deadtime(hvac_mode)
        delay = passive_s if passive_reliable and passive_s is not None else 0.0
        return self.last_stop_time + max(0.0, float(delay))

    def _timeout_s(self) -> float:
        """Wait limit of the armed response: active after a power start, passive after a cut."""
        return self.timeout_seconds if self._waiting_power_on else self.passive_timeout_seconds

    def last_sample_s(self, response: str) -> float | None:
        """Last sample of the ``response`` slot ("heat" or "cool"), None without one."""
        history = self._history_heat if response == "heat" else self._history_cool
        return float(history[-1]) if history else None

    def _add_sample_heat(self, dt: float):
        self.samples_added["heat"] += 1
        self._history_heat.append(dt)
        self.deadtime_heat_s = statistics.median(self._history_heat)
        self.deadtime_heat_reliable = len(self._history_heat) >= 1

    def _add_sample_cool(self, dt: float):
        self.samples_added["cool"] += 1
        self._history_cool.append(dt)
        self.deadtime_cool_s = statistics.median(self._history_cool)
        self.deadtime_cool_reliable = len(self._history_cool) >= 1

    def _detection_state(self) -> dict:
        """Detection state in progress, in wall-clock time."""
        if self._restart_detection is not None:
            # Saved again before the restart was decided: keep the first stop's state.
            return dict(self._restart_detection)
        return {
            "state": self.state,
            "last_power": self.last_power,
            "last_stop_wall": monotonic_to_wall_ts(self.last_stop_time),
            "heat_start_wall": monotonic_to_wall_ts(self.heat_start_time),
            "heat_trough_temp": self.heat_trough_temp,
            "cool_start_wall": monotonic_to_wall_ts(self.cool_start_time),
            "cool_peak_temp": self.cool_peak_temp,
            "waiting_power_on": self._waiting_power_on,
            "near_extreme_wall": monotonic_to_wall_ts(self._near_extreme_time),
            "trend_slope": self._trend_slope,
        }

    def resume_after_restart(self, bridged: bool) -> None:
        """Apply the restart decision to the detection saved before the stop.

        Bridged: the actuator kept its state over the stop, so the detection in
        progress, the last power and the last stop resume; the timeout still
        applies from the saved start. Otherwise the state of load_state stays.
        """
        saved, self._restart_detection = self._restart_detection, None
        if not bridged or not saved:
            return
        state = saved.get("state")
        if state not in {"OFF", "HEATING", "COOLING", "WAITING_HEAT_RESPONSE", "WAITING_COOL_RESPONSE"}:
            return
        try:
            last_power = float(saved.get("last_power") or 0.0)
            trend = float(saved.get("trend_slope") or 0.0)
            trough = saved.get("heat_trough_temp")
            peak = saved.get("cool_peak_temp")
            trough = None if trough is None else float(trough)
            peak = None if peak is None else float(peak)
        except (TypeError, ValueError):
            return
        waiting = saved.get("waiting_power_on")
        self.state = state
        self.last_power = last_power
        self.last_stop_time = wall_to_monotonic_ts(saved.get("last_stop_wall"))
        self.heat_start_time = wall_to_monotonic_ts(saved.get("heat_start_wall"))
        self.heat_trough_temp = trough
        self.cool_start_time = wall_to_monotonic_ts(saved.get("cool_start_wall"))
        self.cool_peak_temp = peak
        self._waiting_power_on = waiting if isinstance(waiting, bool) else None
        self._near_extreme_time = wall_to_monotonic_ts(saved.get("near_extreme_wall"))
        self._trend_slope = trend

    def save_state(self) -> dict:
        """Save state for persistence."""
        state = {
            "deadtime_heat_s": self.deadtime_heat_s,
            "deadtime_cool_s": self.deadtime_cool_s,
            "deadtime_heat_reliable": self.deadtime_heat_reliable,
            "deadtime_cool_reliable": self.deadtime_cool_reliable,
            "model_hvac_mode": self.model_hvac_mode,
            "history_heat": list(self._history_heat),
            "history_cool": list(self._history_cool),
            **self.history.save(),
            "detection": self._detection_state(),
        }
        if self._mode_stash:
            state["mode_stash"] = {mode: dict(snap) for mode, snap in self._mode_stash.items()}
        return state

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

        stash = state.get("mode_stash")
        self._mode_stash = {
            mode: snap
            for mode, snap in (stash.items() if isinstance(stash, dict) else ())
            if mode in (str(VThermHvacMode_HEAT), str(VThermHvacMode_COOL))
            and mode != self.model_hvac_mode
            and isinstance(snap, dict)
        }

        hh = state.get("history_heat", [])
        self._history_heat = deque(hh, maxlen=6)
        hc = state.get("history_cool", [])
        self._history_cool = deque(hc, maxlen=6)
        # The published value is the median of the restored detections.
        if self._history_heat:
            self.deadtime_heat_s = statistics.median(self._history_heat)
        if self._history_cool:
            self.deadtime_cool_s = statistics.median(self._history_cool)

        # An in-progress detection is restored only across a bridged restart
        # (resume_after_restart): otherwise the actuator state during the
        # outage is unknown, so a response spanning it is not attributable.
        self.reset_observation()
        # The OFF duration before the restart is unknown: the minimum OFF time
        # must elapse within this session before a detection can be armed.
        self.last_stop_time = time.monotonic()
        detection = state.get("detection")
        self._restart_detection = dict(detection) if isinstance(detection, dict) else None

        # Restore temperature histories (a missing filtered history stays empty)
        self.history.load(state)


def _direction(response: str) -> float:
    """Return the sign of the temperature move of a response."""
    return -1.0 if response == "cool" else 1.0


def update_episode_starts(
    u_applied: float,
    hvac_mode: VThermHvacMode,
    now: float,
    t_heat_episode_start: float | None,
    t_cool_episode_start: float | None,
    name: str,
) -> tuple[float | None, float | None]:
    """Return the heating/cooling episode starts after applying power `u_applied`."""
    # Determine if active based on u > 0 and mode
    is_heating = (u_applied > 0.01) and (hvac_mode == VThermHvacMode_HEAT)
    is_cooling = (u_applied > 0.01) and (hvac_mode == VThermHvacMode_COOL)

    # Heat Episode Logic
    if is_heating:
        if t_heat_episode_start is None:
            t_heat_episode_start = now
            _LOGGER.debug("%s - DeadTime: Heating episode started at %s", name, now)
    else:
        if t_heat_episode_start is not None:
            _LOGGER.debug("%s - DeadTime: Heating episode stopped", name)
        t_heat_episode_start = None

    # Cool Episode Logic
    if is_cooling:
        if t_cool_episode_start is None:
            t_cool_episode_start = now
            _LOGGER.debug("%s - DeadTime: Cooling episode started at %s", name, now)
    else:
        if t_cool_episode_start is not None:
            _LOGGER.debug("%s - DeadTime: Cooling episode stopped", name)
        t_cool_episode_start = None

    return t_heat_episode_start, t_cool_episode_start
