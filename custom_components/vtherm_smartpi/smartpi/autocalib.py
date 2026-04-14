"""
Smart-PI AutoCalibTrigger Module.

Supervision FSM that monitors parameter convergence quality and triggers forced
calibration when stagnation is detected. External to the control law.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, TYPE_CHECKING

from .const import (
    AutoCalibState,
    AutoCalibWaitingReason,
    AUTOCALIB_SNAPSHOT_PERIOD_H,
    AUTOCALIB_DT_COOL_FALLBACK_DAYS,
    AUTOCALIB_COOLDOWN_H,
    AUTOCALIB_A_MAD_THRESHOLD,
    AUTOCALIB_B_MAD_THRESHOLD,
    AUTOCALIB_TEXT_GRADIENT_C,
    AUTOCALIB_MAX_RETRIES,
    AUTOCALIB_RETRY_DELAY_H,
    AUTOCALIB_EXIT_NEW_OBS_MIN,
    GovernanceRegime,
    SmartPIPhase,
    SmartPICalibrationPhase,
    SmartPICalibrationResult,
)

if TYPE_CHECKING:
    from ..algo import SmartPI

_LOGGER = logging.getLogger(__name__)

# Hourly rate-limit for the check loop (seconds)
_HOURLY_CHECK_INTERVAL_S = 3600.0


@dataclass
class AutoCalibEvent:
    """Event produced by AutoCalibTrigger to be handled by the HA layer."""
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    should_trigger_calibration: bool = False
    should_exit_calibration: bool = False


class AutoCalibTrigger:
    """
    Supervision module for Smart-PI automatic calibration triggering.

    Monitors parameter convergence quality (a, b, deadtime_heat, deadtime_cool)
    and triggers forced calibration when stagnation is detected.

    State machine:
        IDLE            -> initial; autocalib disabled
        WAITING_SNAPSHOT-> waiting for initial conditions to take first snapshot
        MONITORING      -> snapshot taken; evaluating stagnation hourly
        TRIGGERED       -> calibration launched; waiting for completion
        POST_CALIB_CHECK-> calibration done; verifying exit criteria
    """

    def __init__(self, name: str) -> None:
        self._name = name

        # FSM state
        self._state: AutoCalibState = AutoCalibState.WAITING_SNAPSHOT
        self._waiting_reason: AutoCalibWaitingReason = AutoCalibWaitingReason.DEADTIME_COOL_PENDING

        # Snapshot (persistent)
        self._snapshot_ts: float | None = None            # wall-clock
        self._snap_ok_count_a: int = 0
        self._snap_ok_count_b: int = 0
        self._snap_dt_heat_ok: bool = False
        self._snap_dt_cool_ok: bool = False
        self._snap_dt_cool_unavailable: bool = False
        self._snap_a_value: float | None = None
        self._snap_b_value: float | None = None

        # Initial reliables timestamp (wall-clock)
        self._initial_reliables_ts: float | None = None

        # Retry / trigger tracking
        self._retry_count: int = 0
        self._last_trigger_ts: float | None = None        # wall-clock
        self._model_degraded: bool = False
        self._triggered_params: list[str] = []

        # Schedule helpers
        self._next_check_ts: float | None = None          # wall-clock
        self._last_hourly_check_ts: float | None = None   # wall-clock

        # Post-calib tracking
        self._post_calib_snap_a: int = 0
        self._post_calib_snap_b: int = 0

    # -------------------------------------------------------------------------
    # Public properties (diagnostic attributes §6.1)
    # -------------------------------------------------------------------------

    @property
    def state(self) -> AutoCalibState:
        """Current FSM state."""
        return self._state

    @property
    def waiting_reason(self) -> AutoCalibWaitingReason:
        """Reason for staying in waiting_snapshot."""
        return self._waiting_reason

    @property
    def model_degraded(self) -> bool:
        """True if 3 retries failed without improvement."""
        return self._model_degraded

    @property
    def triggered_params(self) -> list[str]:
        """Parameters that triggered the last calibration."""
        return list(self._triggered_params)

    @property
    def retry_count(self) -> int:
        """Current retry counter."""
        return self._retry_count

    @property
    def last_trigger_ts_iso(self) -> str | None:
        """ISO8601 string of last trigger timestamp."""
        if self._last_trigger_ts is None:
            return None
        return datetime.fromtimestamp(self._last_trigger_ts, tz=timezone.utc).isoformat()

    @property
    def next_check_ts_iso(self) -> str | None:
        """ISO8601 string of next scheduled check."""
        if self._next_check_ts is None:
            return None
        return datetime.fromtimestamp(self._next_check_ts, tz=timezone.utc).isoformat()

    @property
    def snapshot_age_h(self) -> int | None:
        """Age of current snapshot in hours (rounded), or None."""
        if self._snapshot_ts is None:
            return None
        now_wall = time.time()
        return int((now_wall - self._snapshot_ts) / 3600.0)

    @property
    def snap_dt_cool_unavailable(self) -> bool:
        """True if snapshot was taken via winter fallback without deadtime_cool."""
        return self._snap_dt_cool_unavailable

    # -------------------------------------------------------------------------
    # Reset / persistence
    # -------------------------------------------------------------------------

    def reset(self) -> None:
        """Reset to initial state."""
        self.__init__(self._name)  # type: ignore[misc]

    def save_state(self) -> dict:
        """Serialize state for persistence."""
        return {
            "state": self._state.value,
            "waiting_reason": self._waiting_reason.value,
            "snapshot_ts": self._snapshot_ts,
            "snap_ok_count_a": self._snap_ok_count_a,
            "snap_ok_count_b": self._snap_ok_count_b,
            "snap_dt_heat_ok": self._snap_dt_heat_ok,
            "snap_dt_cool_ok": self._snap_dt_cool_ok,
            "snap_dt_cool_unavailable": self._snap_dt_cool_unavailable,
            "snap_a_value": self._snap_a_value,
            "snap_b_value": self._snap_b_value,
            "initial_reliables_ts": self._initial_reliables_ts,
            "retry_count": self._retry_count,
            "last_trigger_ts": self._last_trigger_ts,
            "model_degraded": self._model_degraded,
            "triggered_params": list(self._triggered_params),
            "next_check_ts": self._next_check_ts,
            "last_hourly_check_ts": self._last_hourly_check_ts,
            "post_calib_snap_a": self._post_calib_snap_a,
            "post_calib_snap_b": self._post_calib_snap_b,
        }

    def load_state(self, state: dict) -> None:
        """Restore state from persistence dict."""
        if not state:
            return

        try:
            self._state = AutoCalibState(state.get("state", AutoCalibState.WAITING_SNAPSHOT.value))
        except ValueError:
            self._state = AutoCalibState.WAITING_SNAPSHOT

        try:
            self._waiting_reason = AutoCalibWaitingReason(
                state.get("waiting_reason", AutoCalibWaitingReason.DEADTIME_COOL_PENDING.value)
            )
        except ValueError:
            self._waiting_reason = AutoCalibWaitingReason.DEADTIME_COOL_PENDING

        self._snapshot_ts = state.get("snapshot_ts")
        self._snap_ok_count_a = int(state.get("snap_ok_count_a", 0))
        self._snap_ok_count_b = int(state.get("snap_ok_count_b", 0))
        self._snap_dt_heat_ok = bool(state.get("snap_dt_heat_ok", False))
        self._snap_dt_cool_ok = bool(state.get("snap_dt_cool_ok", False))
        self._snap_dt_cool_unavailable = bool(state.get("snap_dt_cool_unavailable", False))
        self._snap_a_value = state.get("snap_a_value")
        self._snap_b_value = state.get("snap_b_value")
        self._initial_reliables_ts = state.get("initial_reliables_ts")
        self._retry_count = int(state.get("retry_count", 0))
        self._last_trigger_ts = state.get("last_trigger_ts")
        self._model_degraded = bool(state.get("model_degraded", False))
        self._triggered_params = list(state.get("triggered_params", []))
        self._next_check_ts = state.get("next_check_ts")
        self._last_hourly_check_ts = state.get("last_hourly_check_ts")
        self._post_calib_snap_a = int(state.get("post_calib_snap_a", 0))
        self._post_calib_snap_b = int(state.get("post_calib_snap_b", 0))

    # -------------------------------------------------------------------------
    # Main hourly entry point
    # -------------------------------------------------------------------------

    def check_hourly(
        self,
        now_wall: float,
        algo: "SmartPI",
        ext_temp: float | None = None,
        current_temp: float | None = None,
    ) -> AutoCalibEvent | None:
        """
        Main supervision entry point.

        Called on every SmartPI cycle. Rate-limited to execute logic once per hour.

        Args:
            now_wall: Current wall-clock timestamp (time.time())
            algo: SmartPI algorithm instance (read-only access to state)
            ext_temp: Current outdoor temperature (°C), optional
            current_temp: Current indoor temperature (°C), optional

        Returns:
            AutoCalibEvent to fire, or None.
        """
        # Rate-limit: only run once per hour
        if (
            self._last_hourly_check_ts is not None
            and (now_wall - self._last_hourly_check_ts) < _HOURLY_CHECK_INTERVAL_S
        ):
            return None

        self._last_hourly_check_ts = now_wall
        _LOGGER.debug("%s - AutoCalib hourly check (state=%s)", self._name, self._state.value)

        # Dispatch based on FSM state
        if self._state == AutoCalibState.WAITING_SNAPSHOT:
            return self._check_phase_initial(now_wall, algo)

        if self._state == AutoCalibState.MONITORING:
            return self._check_monitoring(now_wall, algo, ext_temp, current_temp)

        if self._state == AutoCalibState.POST_CALIB_CHECK:
            return self._check_post_calib(now_wall, algo)

        # TRIGGERED / IDLE: no action needed here
        return None

    # -------------------------------------------------------------------------
    # §4.0 — Phase initial: conditions for first snapshot
    # -------------------------------------------------------------------------

    def _check_phase_initial(self, now_wall: float, algo: "SmartPI") -> AutoCalibEvent | None:
        """
        Evaluate phase-initial conditions to take the first snapshot.

        §4.0.1 Nominal: tau_reliable + deadtime_heat_reliable + deadtime_cool_reliable all True.
        §4.0.2 Fallback: tau_reliable + deadtime_heat_reliable since 7 days without cool.
        """
        tau_reliable = algo.dt_est.deadtime_heat_reliable and algo.est.tau_reliability().reliable
        # Note: tau_reliable from spec refers to ABEstimator.tau_reliability().reliable
        tau_rel = algo.est.tau_reliability().reliable
        dt_heat_rel = algo.dt_est.deadtime_heat_reliable
        dt_cool_rel = algo.dt_est.deadtime_cool_reliable

        # Track when both heat flags first became true
        if tau_rel and dt_heat_rel and self._initial_reliables_ts is None:
            self._initial_reliables_ts = now_wall
            _LOGGER.debug(
                "%s - AutoCalib: tau_reliable + deadtime_heat_reliable first seen, starting 7d countdown",
                self._name,
            )

        # §4.0.1 Nominal condition
        if tau_rel and dt_heat_rel and dt_cool_rel:
            _LOGGER.info("%s - AutoCalib: All reliables True — taking initial snapshot", self._name)
            self._take_snapshot(now_wall, algo, reason="initial", dt_cool_unavailable=False)
            self._state = AutoCalibState.MONITORING
            self._waiting_reason = AutoCalibWaitingReason.NONE
            return AutoCalibEvent(
                event_type="smartpi_autocalib_snapshot_taken",
                payload={
                    "entity_id": self._name,
                    "reason": "initial",
                    "dt_cool_unavailable": False,
                },
            )

        # §4.0.2 Fallback: 7 days with tau+heat without cool
        if (
            tau_rel
            and dt_heat_rel
            and not dt_cool_rel
            and self._initial_reliables_ts is not None
        ):
            elapsed_days = (now_wall - self._initial_reliables_ts) / 86400.0
            remaining_days = AUTOCALIB_DT_COOL_FALLBACK_DAYS - elapsed_days
            self._waiting_reason = AutoCalibWaitingReason.FALLBACK_7D_COUNTDOWN

            if elapsed_days >= AUTOCALIB_DT_COOL_FALLBACK_DAYS:
                _LOGGER.warning(
                    "%s - AutoCalib: 7-day fallback activated (deadtime_cool still unreliable)",
                    self._name,
                )
                self._take_snapshot(now_wall, algo, reason="fallback_7d", dt_cool_unavailable=True)
                self._state = AutoCalibState.MONITORING
                self._waiting_reason = AutoCalibWaitingReason.NONE
                return AutoCalibEvent(
                    event_type="smartpi_autocalib_snapshot_taken",
                    payload={
                        "entity_id": self._name,
                        "reason": "fallback_7d",
                        "dt_cool_unavailable": True,
                    },
                )
            else:
                _LOGGER.debug(
                    "%s - AutoCalib: 7-day fallback countdown: %.1f days remaining",
                    self._name,
                    remaining_days,
                )
        else:
            self._waiting_reason = AutoCalibWaitingReason.DEADTIME_COOL_PENDING

        return None

    # -------------------------------------------------------------------------
    # §4.1 / §4.2 — Monitoring: guard + stagnation evaluation
    # -------------------------------------------------------------------------

    def _check_monitoring(
        self,
        now_wall: float,
        algo: "SmartPI",
        ext_temp: float | None,
        current_temp: float | None,
    ) -> AutoCalibEvent | None:
        """Evaluate guards and stagnation criteria, trigger if needed. §4.1/4.2."""

        # §4.0.2 — If fallback was active and deadtime_cool is now reliable: clear flag
        if self._snap_dt_cool_unavailable and algo.dt_est.deadtime_cool_reliable:
            _LOGGER.info(
                "%s - AutoCalib: deadtime_cool now reliable, clearing winter fallback flag",
                self._name,
            )
            self._snap_dt_cool_unavailable = False
            # Flag will be cleared in snapshot: wait for next rolling snapshot

        # §4.1 Guards
        guard_ok, guard_reason = self._check_guards(now_wall, algo)
        if not guard_ok:
            _LOGGER.debug(
                "%s - AutoCalib: guard KO (%s), skipping stagnation check",
                self._name, guard_reason,
            )
            return None

        # Snapshot must be old enough (≥ 5 days)
        if self._snapshot_ts is None:
            return None
        snap_age_h = (now_wall - self._snapshot_ts) / 3600.0
        if snap_age_h < AUTOCALIB_SNAPSHOT_PERIOD_H:
            _LOGGER.debug(
                "%s - AutoCalib: snapshot too recent (%.1fh < %dh), skipping",
                self._name, snap_age_h, AUTOCALIB_SNAPSHOT_PERIOD_H,
            )
            return None

        # §4.2 Stagnation evaluation
        stagnating = self._evaluate_stagnation(algo, ext_temp, current_temp)
        if stagnating:
            # Trigger calibration
            return self._do_trigger(now_wall, algo, stagnating)

        # §4.0.3 Rolling snapshot check (5-day period from last significant event)
        # If we reached here, age >= 120h and we are NOT stagnating. The model is progressing healthily.
        # We can roll the snapshot to start a new 5-day observation window.
        _LOGGER.info(
            "%s - AutoCalib: snapshot rolling (age=%.1fh >= %dh, not stagnating)",
            self._name, snap_age_h, AUTOCALIB_SNAPSHOT_PERIOD_H,
        )
        self._take_snapshot(now_wall, algo, reason="rolling")
        return AutoCalibEvent(
            event_type="smartpi_autocalib_snapshot_taken",
            payload={
                "entity_id": self._name,
                "reason": "rolling",
                "dt_cool_unavailable": self._snap_dt_cool_unavailable,
            },
        )

    def _check_guards(self, now_wall: float, algo: "SmartPI") -> tuple[bool, str]:
        """§4.1 — Check all precondition guards. Returns (ok, reason)."""

        # Phase must be Stable or Idle (not already calibrating)
        if algo.phase == SmartPIPhase.HYSTERESIS:
            return False, "hysteresis_phase"
        if algo.calibration_mgr.is_calibrating:
            return False, "already_calibrating"

        # §4.1 governance guard
        regime = algo.gov.regime
        if regime == GovernanceRegime.PERTURBED:
            return False, "perturbed_regime"

        # Cooldown: no calibration within last 24h
        if self._last_trigger_ts is not None:
            elapsed_h = (now_wall - self._last_trigger_ts) / 3600.0
            if elapsed_h < AUTOCALIB_COOLDOWN_H:
                return False, f"cooldown ({elapsed_h:.1f}h < {AUTOCALIB_COOLDOWN_H}h)"

        # Also check CalibrationManager's last_calibration_time
        if algo.calibration_mgr.last_calibration_time is not None:
            elapsed_h = (now_wall - algo.calibration_mgr.last_calibration_time) / 3600.0
            if elapsed_h < AUTOCALIB_COOLDOWN_H:
                return False, f"post_calib_cooldown ({elapsed_h:.1f}h)"

        return True, "ok"

    def _evaluate_stagnation(
        self,
        algo: "SmartPI",
        ext_temp: float | None,
        current_temp: float | None,
    ) -> list[str]:
        """
        §4.2 — Evaluate stagnation for each parameter.

        Returns list of stagnating parameter names (empty = no stagnation).
        """
        stagnating: list[str] = []

        # --- Parameter 'a' (§4.2) ---
        count_a_delta = algo.est.learn_ok_count_a - self._snap_ok_count_a
        mad_a = algo.est.diag_a_mad_over_med

        no_progress_a = count_a_delta <= 1
        high_dispersion_a = (
            mad_a is None and algo.est.learn_ok_count_a < 30
        ) or (
            mad_a is not None and mad_a > AUTOCALIB_A_MAD_THRESHOLD
        )
        converged_a = (
            count_a_delta > 1
            and mad_a is not None
            and mad_a < 0.15
        )

        if no_progress_a and high_dispersion_a and not converged_a:
            _LOGGER.debug(
                "%s - AutoCalib: stagnation detected on 'a' (delta=%d, mad=%.3f)",
                self._name, count_a_delta, mad_a if mad_a is not None else -1,
            )
            stagnating.append("a")

        # --- Parameter 'b' (§4.2) ---
        count_b_delta = algo.est.learn_ok_count_b - self._snap_ok_count_b
        mad_b = algo.est.diag_b_mad_over_med
        tau_rel = algo.est.tau_reliability().reliable

        no_progress_b = count_b_delta <= 1
        poor_quality_b = not tau_rel or (mad_b is not None and mad_b > AUTOCALIB_B_MAD_THRESHOLD)
        converged_b = tau_rel and mad_b is not None and mad_b < 0.20

        if no_progress_b and poor_quality_b and not converged_b:
            _LOGGER.debug(
                "%s - AutoCalib: stagnation detected on 'b' (delta=%d, tau_rel=%s, mad=%.3f)",
                self._name, count_b_delta, tau_rel, mad_b if mad_b is not None else -1,
            )
            stagnating.append("b")

        # --- Deadtime heat (§4.2) ---
        if not algo.dt_est.deadtime_heat_reliable and not self._snap_dt_heat_ok:
            _LOGGER.debug("%s - AutoCalib: stagnation detected on 'deadtime_heat'", self._name)
            stagnating.append("deadtime_heat")

        # --- Deadtime cool (§4.2) ---
        # Skip if winter fallback is active (§4.0.2)
        if not self._snap_dt_cool_unavailable:
            if not algo.dt_est.deadtime_cool_reliable and not self._snap_dt_cool_ok:
                # Check gradient condition
                if (
                    current_temp is not None
                    and ext_temp is not None
                    and (current_temp - ext_temp) >= AUTOCALIB_TEXT_GRADIENT_C
                ):
                    _LOGGER.debug(
                        "%s - AutoCalib: stagnation detected on 'deadtime_cool' (gradient ok)",
                        self._name,
                    )
                    stagnating.append("deadtime_cool")
                else:
                    _LOGGER.debug(
                        "%s - AutoCalib: deadtime_cool stagnant but gradient insufficient — no trigger",
                        self._name,
                    )

        return stagnating

    def _do_trigger(
        self, now_wall: float, algo: "SmartPI", triggered_params: list[str]
    ) -> AutoCalibEvent:
        """§4.3 — Trigger calibration and update state."""
        self._triggered_params = list(triggered_params)
        self._last_trigger_ts = now_wall
        # Store learn counts at trigger start for post-calib comparison
        self._post_calib_snap_a = algo.est.learn_ok_count_a
        self._post_calib_snap_b = algo.est.learn_ok_count_b
        self._state = AutoCalibState.TRIGGERED

        _LOGGER.warning(
            "%s - AutoCalib: triggering calibration for params=%s (retry=%d)",
            self._name,
            triggered_params,
            self._retry_count,
        )

        return AutoCalibEvent(
            event_type="smartpi_autocalib_triggered",
            payload={
                "entity_id": self._name,
                "triggered_params": triggered_params,
                "retry_count": self._retry_count,
            },
            should_trigger_calibration=True,
        )

    # -------------------------------------------------------------------------
    # §5 — Called when calibration FSM exits (complete or timeout)
    # -------------------------------------------------------------------------

    def force_manual_trigger(self, now_wall: float, algo: "SmartPI") -> AutoCalibEvent:
        """
        §5.4 — Called when a manual calibration (%force_smart_pi_calibration%) is explicitly triggered via service.
        Resets retry counters and triggers the calibration cycle properly using AutoCalib Trigger mechanism.
        """
        self._retry_count = 0 
        return self._do_trigger(now_wall, algo, ["manual_request"])

    def on_calibration_complete(
        self,
        now_wall: float,
        algo: "SmartPI",
        result: SmartPICalibrationResult = SmartPICalibrationResult.SUCCESS,
    ) -> AutoCalibEvent | None:
        """
        Called by the handler when the CalibrationManager exits IDLE.

        Evaluates §5.1 exit criteria and handles retry / degraded logic.

        Args:
            now_wall: Current wall-clock timestamp
            algo: SmartPI algorithm instance
            result: The result of the calibration cycle

        Returns:
            AutoCalibEvent describing the result, or None if not in expected state.
        """
        if self._state not in (AutoCalibState.TRIGGERED, AutoCalibState.POST_CALIB_CHECK):
            # Not triggered by us — ignore (no-op for unrelated calibrations)
            return None

        if result == SmartPICalibrationResult.CANCELLED:
            _LOGGER.info(
                "%s - AutoCalib: explicit calibration cancellation detected. "
                "Going back to IDLE/WAITING_SNAPSHOT without consuming a retry.",
                self._name,
            )
            # Revert to waiting/idle to not penalize the model for an external interrupt (HVAC OFF, manual cancellation...)
            self._state = AutoCalibState.WAITING_SNAPSHOT
            self._triggered_params = []
            return None

        self._state = AutoCalibState.POST_CALIB_CHECK
        return self._check_post_calib(now_wall, algo)

    def _check_post_calib(self, now_wall: float, algo: "SmartPI") -> AutoCalibEvent | None:
        """§5 — Evaluate exit criteria after calibration."""

        # §5.1 Exit criteria
        new_a = algo.est.learn_ok_count_a - self._post_calib_snap_a
        new_b = algo.est.learn_ok_count_b - self._post_calib_snap_b

        criteria_a = new_a >= AUTOCALIB_EXIT_NEW_OBS_MIN
        criteria_b = new_b >= AUTOCALIB_EXIT_NEW_OBS_MIN

        # deadtime_heat: reliable now, or deadtime_skip_count_a progressed
        criteria_dt_heat = algo.dt_est.deadtime_heat_reliable

        # deadtime_cool: reliable, OR was already reliable before, OR winter fallback
        criteria_dt_cool = (
            algo.dt_est.deadtime_cool_reliable
            or self._snap_dt_cool_ok          # was already reliable at snapshot
            or self._snap_dt_cool_unavailable  # winter fallback active
        )

        all_ok = criteria_a and criteria_b and criteria_dt_heat and criteria_dt_cool

        missing = []
        if not criteria_a:
            missing.append(f"a(new={new_a}<{AUTOCALIB_EXIT_NEW_OBS_MIN})")
        if not criteria_b:
            missing.append(f"b(new={new_b}<{AUTOCALIB_EXIT_NEW_OBS_MIN})")
        if not criteria_dt_heat:
            missing.append("deadtime_heat")
        if not criteria_dt_cool:
            missing.append("deadtime_cool")

        if all_ok:
            # §5.1 Positive exit
            self._retry_count = 0
            self._model_degraded = False
            self._take_snapshot(now_wall, algo, reason="post_calib")
            self._state = AutoCalibState.MONITORING

            improved = [
                p for p in self._triggered_params
                if (p == "a" and criteria_a)
                or (p == "b" and criteria_b)
                or (p == "deadtime_heat" and criteria_dt_heat)
                or (p == "deadtime_cool" and criteria_dt_cool)
            ]

            _LOGGER.info(
                "%s - AutoCalib: positive exit (improved=%s)", self._name, improved
            )
            return AutoCalibEvent(
                event_type="smartpi_autocalib_success",
                payload={
                    "entity_id": self._name,
                    "improved_params": improved,
                    "retry_count": self._retry_count,
                },
            )

        # §5.3 Retry logic
        self._retry_count += 1

        if self._retry_count < AUTOCALIB_MAX_RETRIES:
            # Schedule retry
            retry_ts = now_wall + AUTOCALIB_RETRY_DELAY_H * 3600.0
            self._next_check_ts = retry_ts
            self._state = AutoCalibState.MONITORING  # will re-trigger at next check
            # Force snapshot_ts to be old enough to re-trigger in AUTOCALIB_RETRY_DELAY_H
            self._snapshot_ts = now_wall - (AUTOCALIB_SNAPSHOT_PERIOD_H * 3600.0)
            # Override last_trigger_ts to enforce cooldown = retry delay
            self._last_trigger_ts = now_wall - (AUTOCALIB_COOLDOWN_H * 3600.0)

            _LOGGER.warning(
                "%s - AutoCalib: partial exit (retry %d/%d, missing=%s)",
                self._name, self._retry_count, AUTOCALIB_MAX_RETRIES, missing,
            )
            return AutoCalibEvent(
                event_type="smartpi_autocalib_retry",
                payload={
                    "entity_id": self._name,
                    "retry_count": self._retry_count,
                    "missing_criteria": missing,
                },
            )

        # §5.3 Max retries reached → model degraded
        self._model_degraded = True
        self._state = AutoCalibState.MONITORING
        # Update snapshot anyway so we don't loop on stagnation forever
        self._take_snapshot(now_wall, algo, reason="post_calib_degraded")
        # Reset retry counter to allow future attempts after degraded period
        self._retry_count = 0

        _LOGGER.warning(
            "%s - AutoCalib: max retries reached — model degraded (missing=%s)",
            self._name, missing,
        )
        return AutoCalibEvent(
            event_type="smartpi_autocalib_degraded",
            payload={
                "entity_id": self._name,
                "retry_count": AUTOCALIB_MAX_RETRIES,
            },
        )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _take_snapshot(
        self,
        now_wall: float,
        algo: "SmartPI",
        reason: str,
        dt_cool_unavailable: bool = False,
    ) -> None:
        """Record a snapshot of current parameter state."""
        # Handle dt_cool_unavailable: if previously flagged, keep it unless cool is now reliable
        effective_dt_cool_unavail = dt_cool_unavailable
        if self._snap_dt_cool_unavailable and not algo.dt_est.deadtime_cool_reliable:
            effective_dt_cool_unavail = True

        self._snapshot_ts = now_wall
        self._snap_ok_count_a = algo.est.learn_ok_count_a
        self._snap_ok_count_b = algo.est.learn_ok_count_b
        self._snap_dt_heat_ok = algo.dt_est.deadtime_heat_reliable
        self._snap_dt_cool_ok = algo.dt_est.deadtime_cool_reliable
        self._snap_dt_cool_unavailable = effective_dt_cool_unavail
        self._snap_a_value = algo.est.a
        self._snap_b_value = algo.est.b

        _LOGGER.debug(
            "%s - AutoCalib: snapshot taken (reason=%s, a=%s, b=%s, dt_cool_unavail=%s)",
            self._name, reason, self._snap_a_value, self._snap_b_value, effective_dt_cool_unavail,
        )
