"""Late-braking setpoint trajectory generator for Smart-PI."""
from __future__ import annotations

from math import exp
from typing import Any

from .const import TrajectoryPhase, TRAJECTORY_COMPLETE_EPS_C


class SmartPITrajectoryGenerator:
    """First-order generator used only during late braking/release."""

    def __init__(self) -> None:
        self.active: bool = False
        self.phase: TrajectoryPhase = TrajectoryPhase.IDLE
        self.start_setpoint: float | None = None
        self.target_setpoint: float | None = None
        self.tau_ref_min: float | None = None
        self.current_setpoint: float | None = None
        self.elapsed_s: float = 0.0
        self._last_now_monotonic: float | None = None

    def reset(self) -> None:
        """Clear runtime and persisted trajectory state."""
        self.active = False
        self.phase = TrajectoryPhase.IDLE
        self.start_setpoint = None
        self.target_setpoint = None
        self.tau_ref_min = None
        self.current_setpoint = None
        self.elapsed_s = 0.0
        self._last_now_monotonic = None

    def start(
        self,
        start_setpoint: float,
        target_setpoint: float,
        tau_ref_min: float,
        now_monotonic: float,
    ) -> None:
        """Arm the generator from the current filtered reference to the brake target."""
        self.active = True
        self.phase = TrajectoryPhase.TRACKING
        self.start_setpoint = float(start_setpoint)
        self.target_setpoint = float(target_setpoint)
        self.tau_ref_min = max(float(tau_ref_min), 1e-6)
        self.current_setpoint = float(start_setpoint)
        self.elapsed_s = 0.0
        self._last_now_monotonic = float(now_monotonic)

    def set_target(
        self,
        target_setpoint: float,
        tau_ref_min: float | None = None,
        phase: TrajectoryPhase | None = None,
    ) -> None:
        """Retarget the active generator without creating a jump."""
        if phase is not None and phase != self.phase and self.current_setpoint is not None:
            self.phase = phase
            self.start_setpoint = self.current_setpoint
            self.elapsed_s = 0.0
        self.target_setpoint = float(target_setpoint)
        if tau_ref_min is not None:
            self.tau_ref_min = max(float(tau_ref_min), 1e-6)
        if phase is not None:
            self.phase = phase

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore state from persistence.

        Legacy servo states are mapped to the tracking trajectory.
        """
        if not state:
            return

        active = state.get("trajectory_active")
        if active is None:
            active = state.get("servo_active", False)
        self.active = bool(active)

        phase_raw = state.get("trajectory_phase")
        if phase_raw is None and self.active:
            legacy_phase = state.get("servo_phase")
            if legacy_phase in {"boost", "landing"}:
                phase_raw = TrajectoryPhase.TRACKING.value
            elif legacy_phase in {"none", None}:
                phase_raw = TrajectoryPhase.IDLE.value
        try:
            self.phase = TrajectoryPhase(phase_raw or TrajectoryPhase.IDLE.value)
        except ValueError:
            self.phase = TrajectoryPhase.IDLE

        start_sp = state.get("trajectory_start_setpoint")
        if start_sp is None:
            start_sp = state.get("servo_target_at_activation")
        self.start_setpoint = float(start_sp) if start_sp is not None else None

        target_sp = state.get("trajectory_target_setpoint")
        self.target_setpoint = float(target_sp) if target_sp is not None else None

        tau_ref = state.get("trajectory_tau_ref_min")
        self.tau_ref_min = float(tau_ref) if tau_ref is not None else None
        current_sp = state.get("trajectory_current_setpoint")
        if current_sp is None:
            current_sp = state.get("effective_setpoint")
        if current_sp is None:
            current_sp = self.start_setpoint
        self.current_setpoint = float(current_sp) if current_sp is not None else None

        elapsed_s = state.get("trajectory_elapsed_s")
        self.elapsed_s = float(elapsed_s) if elapsed_s is not None else 0.0
        self._last_now_monotonic = None

        if not self.active:
            self.phase = TrajectoryPhase.IDLE

    def save_state(self) -> dict[str, Any]:
        """Serialize the trajectory state."""
        return {
            "trajectory_active": self.active,
            "trajectory_phase": self.phase.value,
            "trajectory_start_setpoint": self.start_setpoint,
            "trajectory_target_setpoint": self.target_setpoint,
            "trajectory_tau_ref_min": self.tau_ref_min,
            "trajectory_current_setpoint": self.current_setpoint,
            "trajectory_elapsed_s": self.elapsed_s,
        }

    def update(
        self,
        now_monotonic: float,
    ) -> float | None:
        """Advance the generator toward its current target and return SP_for_P."""
        if (
            not self.active
            or self.start_setpoint is None
            or self.target_setpoint is None
            or self.current_setpoint is None
            or self.tau_ref_min is None
        ):
            return None

        dt_s = 0.0
        if self._last_now_monotonic is not None and now_monotonic >= self._last_now_monotonic:
            dt_s = now_monotonic - self._last_now_monotonic
            self.elapsed_s += dt_s
        self._last_now_monotonic = now_monotonic

        if self.current_setpoint == self.target_setpoint:
            return self.current_setpoint

        dt_min = dt_s / 60.0
        if dt_min <= 0.0:
            return self.current_setpoint

        alpha = exp(-dt_min / self.tau_ref_min)
        self.current_setpoint = self.target_setpoint + (self.current_setpoint - self.target_setpoint) * alpha

        if (
            self.phase != TrajectoryPhase.RELEASE
            and abs(self.target_setpoint - self.current_setpoint) <= TRAJECTORY_COMPLETE_EPS_C
        ):
            self.current_setpoint = self.target_setpoint

        return self.current_setpoint
