"""
Power steps of the a/b learning: heating episodes, delivered power and settling gates.

An episode is a run of applied power after a step from rest; it ends at a cut. The tracker counts the heating
phases (runs separated by less than RESPONSE_LAG_PHASE_MIN_OFF_S of cut count once), keeps the recent episode
durations, and follows the power delivered to the room with the response model of the mode
(smartpi/response_model.py): the command delayed by the dead time theta, then a first order of the response time
constant tau_e. Without a model the delivered power is the applied power.

Settling gates (wait ``settle_s`` in smartpi/response_model.py, the emitter lag S): an active window starts S after
the power step, capped at AB_SETTLE_ACTIVE_CAP_FRAC of the median episode; a passive window starts S after the cut.
Without a model there is no gate.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import TYPE_CHECKING, Any

from .const import (
    AB_SETTLE_ACTIVE_CAP_FRAC,
    RESPONSE_LAG_HISTORY,
    RESPONSE_LAG_MIN_ESTIMATES,
    RESPONSE_LAG_PHASE_MIN_OFF_S,
    RESPONSE_LAG_RESOLVE_EPISODES,
    RESPONSE_LAG_RESTART_MAX_GAP_S,
    RESPONSE_LAG_U_TOL,
)
from .response_model import settle_s
from .timestamp_utils import monotonic_to_wall_ts, wall_to_monotonic_ts

from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT

if TYPE_CHECKING:
    from ..hvac_mode import VThermHvacMode

_POWER_ON = 0.01
# Two observed intervals are contiguous within this tolerance (s).
_CONTIGUITY_TOL_S = 1.0


def _number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


class PowerSteps:
    """Track the power steps of the learning and the power delivered to the room."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._durations: deque[float] = deque(maxlen=RESPONSE_LAG_HISTORY)
        # Heating phases ended since the start of the learning: active episodes
        # separated by less than RESPONSE_LAG_PHASE_MIN_OFF_S of power cut
        # count once.
        self._episodes: int = 0
        # End of the last ended active episode (monotonic s), None if none.
        self._last_episode_end: float | None = None
        self._model_hvac_mode: str | None = None
        # Episodes of the HVAC mode that is not active, by mode name, as saved state. Empty for a thermostat that
        # never changes mode.
        self._mode_stash: dict[str, dict] = {}
        self._clear_runtime()

    def _clear_runtime(self) -> None:
        self._last_now: float | None = None
        self._u_filt: float | None = None
        self._interval_mean: float = 0.0
        # Applied command segments (start, power) feeding the delayed input.
        self._cmd: deque[tuple[float, float]] = deque(maxlen=512)
        self._ep_start: float | None = None
        self._ep_u0: float = 0.0
        self._ep_const: bool = False
        self._off_start: float | None = None
        # Context saved before a restart, applied on the first observation.
        self._restore: dict | None = None

    # ------------------------------------------------------------------
    # Published values
    # ------------------------------------------------------------------

    @property
    def episodes_count(self) -> int:
        """Heating phases ended since the start of the learning."""
        return self._episodes

    @property
    def phases_resolve(self) -> bool:
        """True once RESPONSE_LAG_RESOLVE_EPISODES heating phases have ended (the a learning goes on without a
        response model)."""
        return self._episodes >= RESPONSE_LAG_RESOLVE_EPISODES

    @property
    def episode_median_s(self) -> float | None:
        """Median duration of the recent active episodes (s)."""
        if len(self._durations) < RESPONSE_LAG_MIN_ESTIMATES:
            return None
        return statistics.median(self._durations)

    @property
    def interval_mean_power(self) -> float:
        """Mean delivered power over the last observed interval (the applied power without a model)."""
        return self._interval_mean

    @property
    def episode_start(self) -> float | None:
        """Start of the current active episode (power step), or None."""
        return self._ep_start

    @property
    def episode_power(self) -> float | None:
        """Applied power of the current episode while it is constant, or None."""
        if self._ep_start is None or not self._ep_const:
            return None
        return self._ep_u0

    def active_settle_s(self, model: dict[str, Any] | None) -> float | None:
        """Settling time of an active window after the power step, or None without a model."""
        settle = settle_s(model)
        episode = self.episode_median_s
        if settle is not None and episode is not None:
            settle = min(settle, AB_SETTLE_ACTIVE_CAP_FRAC * episode)
        return settle

    def active_gate_end(self, model: dict[str, Any] | None) -> float | None:
        """Earliest start of an active window in the current episode."""
        settle = self.active_settle_s(model)
        if settle is None or self._ep_start is None:
            return None
        return self._ep_start + settle

    def passive_gate_end(self, model: dict[str, Any] | None) -> float | None:
        """Earliest start of a passive window after the last cut, or None (no model, no cut seen)."""
        if self._ep_start is not None or self._off_start is None:
            return None
        settle = settle_s(model)
        return None if settle is None else self._off_start + settle

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        now: float,
        dt_s: float,
        u: float,
        hvac_mode: "VThermHvacMode | None",
        model: dict[str, Any] | None,
    ) -> None:
        """Observe the power applied over [now - dt_s, now].

        The interval convention is the one of the learning window: the power passed with an update is applied over
        the elapsed interval. ``model`` is the published response model of the mode (``ResponseModel.values``), None
        without one.
        """
        mode = str(hvac_mode) if hvac_mode is not None else None
        if mode is not None and mode != self._model_hvac_mode:
            if self._model_hvac_mode is not None:
                self._switch_mode(mode)
            self._model_hvac_mode = mode
        if dt_s <= 0.0:
            return
        if self._last_now is not None and now <= self._last_now:
            return
        t_step = now - dt_s
        if self._restore is not None:
            self._resume_after_restart(t_step, u, mode)
        if self._last_now is not None and t_step > self._last_now + _CONTIGUITY_TOL_S:
            # Unobserved time (guard cut, OFF, interval capped at one cycle):
            # the power over the gap is unknown.
            self._on_gap(t_step)
        self._last_now = now
        self._advance_filter(t_step, now, u, model)

        on = u > _POWER_ON
        if on and self._ep_start is None:
            self._ep_start = t_step
            self._ep_u0 = u
            self._ep_const = True
        elif on and abs(u - self._ep_u0) > RESPONSE_LAG_U_TOL:
            self._ep_const = False
        elif not on and self._ep_start is not None:
            self._end_episode(t_step)
            self._off_start = t_step
        elif not on and self._off_start is None:
            self._off_start = t_step

    def resolve_restart(self, bridged: bool) -> None:
        """Apply the restart decision (restart_bridge.py) to the saved period or episode.

        Bridged: the actuator kept its state over the stop, so the period or the
        episode resumes at the first observation, which continues or ends it.
        Otherwise the passive period is dropped and an episode is counted as
        ended.
        """
        restore = self._restore
        if restore is None:
            return
        if bridged:
            restore["bridged"] = True
            return
        self._restore = None
        episode = restore.get("episode")
        if episode is not None:
            self._count_episode(episode["start"], restore["last_obs"])

    def _resume_after_restart(self, t_resume: float, u: float, mode: str | None) -> None:
        """Resume the passive period or the episode saved before a restart.

        After a bridged restart (resolve_restart) the period or the episode
        resumes as saved. Without a restart decision (the tracker used on its
        own), the power over the stop is unknown: the saved context is only
        resumed across a stop of at most RESPONSE_LAG_RESTART_MAX_GAP_S, during
        which the actuator keeps its last state, and in the saved hvac mode (a
        mode change clears the tracker before). An episode is then resumed
        when the first applied power equals its power; otherwise it is counted
        as an ended episode.
        """
        restore = self._restore
        self._restore = None
        if restore is None:
            return
        episode = restore.get("episode")
        short_stop = restore.get("bridged") or (
            0.0 <= t_resume - restore["last_obs"] <= RESPONSE_LAG_RESTART_MAX_GAP_S
            and mode is not None
            and mode == self._model_hvac_mode
        )
        if episode is None:
            if short_stop and restore.get("off_start") is not None:
                # The heater was not driven during the stop: still passive.
                self._off_start = restore["off_start"]
            return
        if restore.get("bridged") or (
            short_stop and u > _POWER_ON and abs(u - episode["u0"]) <= RESPONSE_LAG_U_TOL
        ):
            self._ep_start = episode["start"]
            self._ep_u0 = episode["u0"]
            self._ep_const = episode["const"]
            return
        self._count_episode(episode["start"], restore["last_obs"])

    def _count_episode(self, t_start: float, t_end: float) -> None:
        """Count an ended active episode unless it continues the previous phase."""
        previous = self._last_episode_end
        if previous is None or t_start - previous >= RESPONSE_LAG_PHASE_MIN_OFF_S:
            self._episodes += 1
        self._last_episode_end = t_end

    def _on_gap(self, t_resume: float) -> None:
        """Close the episode at the last observed time and forget the filter.

        A heating guard cut ends an episode this way: the power is known up to
        the last observation, not across the gap.
        """
        if self._ep_start is not None and self._last_now is not None:
            self._end_episode(self._last_now)
        self._ep_start = None
        self._u_filt = None
        self._cmd.clear()
        # The power cut time is unknown: settling is counted from the resume.
        self._off_start = t_resume

    def _advance_filter(self, t_a: float, t_b: float, u: float, model: dict[str, Any] | None) -> None:
        """Advance the delivered power over [t_a, t_b] (command u applied): the command delayed by theta, then a
        first order of tau_e; the applied power without a model."""
        if not self._cmd or self._cmd[-1][1] != u:
            self._cmd.append((t_a, u))
        if self._u_filt is None or model is None:
            self._u_filt = u
            self._interval_mean = u
            self._prune_cmd(t_b, 0.0)
            return
        delay, tau = model["theta_s"], model["tau_e_s"]
        edges = [t_a]
        edges += sorted(ts + delay for ts, _ in self._cmd if t_a < ts + delay < t_b)
        edges.append(t_b)
        total = 0.0
        for x0, x1 in zip(edges, edges[1:]):
            value = self._cmd_at(x0 - delay)
            h = x1 - x0
            if tau <= 0.0:
                total += value * h
                self._u_filt = value
                continue
            gap = self._u_filt - value
            total += value * h + gap * tau * (-math.expm1(-h / tau))
            self._u_filt = value + gap * math.exp(-h / tau)
        self._interval_mean = total / (t_b - t_a)
        self._prune_cmd(t_b, delay)

    def _cmd_at(self, t: float) -> float:
        """Applied command at time t (the oldest known before the history)."""
        value = self._cmd[0][1]
        for ts, u in self._cmd:
            if ts > t:
                break
            value = u
        return value

    def _prune_cmd(self, now: float, delay: float) -> None:
        """Keep the segment active at now - delay and the later ones."""
        while len(self._cmd) >= 2 and self._cmd[1][0] <= now - delay:
            self._cmd.popleft()

    def _end_episode(self, t_end: float) -> None:
        t_start = self._ep_start
        self._ep_start = None
        if t_start is None:
            return
        duration = t_end - t_start
        if duration <= 0.0:
            return
        self._durations.append(duration)
        self._count_episode(t_start, t_end)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Forget the episodes and the tracked step, those set aside for the other mode included."""
        self._mode_stash = {}
        self._clear_active()

    def _switch_mode(self, mode: str) -> None:
        """Set the episodes of the active mode aside and take back the ones set aside for ``mode``, if any."""
        learned = self._learned_snapshot()
        if learned is None:
            self._mode_stash.pop(self._model_hvac_mode, None)
        else:
            self._mode_stash[self._model_hvac_mode] = learned
        self._clear_active()
        snap = self._mode_stash.pop(mode, None)
        if snap is not None:
            self._load_learned(snap)

    def _learned_snapshot(self) -> dict | None:
        """The episodes as saved state, None when there are none."""
        if not (self._durations or self._episodes):
            return None
        return {"durations": list(self._durations), "episodes": self._episodes}

    def _clear_active(self) -> None:
        """Forget the episodes and the tracked step of the active mode."""
        self._durations.clear()
        self._episodes = 0
        self._last_episode_end = None
        self._clear_runtime()

    def _restart_context(self) -> dict | None:
        """Return the passive period or the episode to resume, in monotonic time."""
        if self._restore is not None:
            # Not yet applied: no observation since the previous restart.
            return self._restore
        if self._last_now is None:
            return None
        if self._ep_start is not None:
            return {
                "last_obs": self._last_now,
                "off_start": None,
                "episode": {"start": self._ep_start, "u0": self._ep_u0, "const": self._ep_const},
            }
        if self._off_start is None:
            return None
        return {"last_obs": self._last_now, "off_start": self._off_start, "episode": None}

    def save_state(self) -> dict:
        state = {
            "durations": list(self._durations),
            "episodes": self._episodes,
            "last_episode_end_wall": monotonic_to_wall_ts(self._last_episode_end),
            "hvac_mode": self._model_hvac_mode,
        }
        if self._mode_stash:
            state["mode_stash"] = {mode: dict(snap) for mode, snap in self._mode_stash.items()}
        context = self._restart_context()
        if context is not None:
            episode = context["episode"]
            state["restart"] = {
                "last_obs_wall": monotonic_to_wall_ts(context["last_obs"]),
                "off_start_wall": monotonic_to_wall_ts(context["off_start"]),
                "episode": None if episode is None else {
                    "start_wall": monotonic_to_wall_ts(episode["start"]),
                    "u0": episode["u0"],
                    "const": episode["const"],
                },
            }
        return state

    @staticmethod
    def _load_restart(value: object) -> dict | None:
        """Validate a saved restart context and convert it to monotonic time."""
        if not isinstance(value, dict):
            return None
        last_obs = wall_to_monotonic_ts(value.get("last_obs_wall"))
        if last_obs is None:
            return None
        episode = value.get("episode")
        if episode is None:
            off_start = wall_to_monotonic_ts(value.get("off_start_wall"))
            if off_start is None or off_start > last_obs:
                return None
            return {"last_obs": last_obs, "off_start": off_start, "episode": None}
        if not isinstance(episode, dict):
            return None
        start = wall_to_monotonic_ts(episode.get("start_wall"))
        u0 = _number(episode.get("u0"))
        const = episode.get("const")
        if start is None or start > last_obs or u0 is None or u0 <= _POWER_ON or not isinstance(const, bool):
            return None
        return {
            "last_obs": last_obs,
            "off_start": None,
            "episode": {"start": start, "u0": u0, "const": const},
        }

    def _load_learned(self, state: dict) -> None:
        """Restore the durations and the episode count of a saved state, ignoring what is malformed."""
        durations = state.get("durations")
        if isinstance(durations, list):
            self._durations.extend(v for v in map(_number, durations) if v is not None and v >= 0.0)
        episodes = state.get("episodes")
        if isinstance(episodes, int) and not isinstance(episodes, bool) and episodes >= 0:
            self._episodes = episodes

    def load_state(self, state: dict | None) -> None:
        """Restore the episodes and the passive period or episode in progress (a state of the former response-lag
        estimator loads the same keys; its lag estimates are ignored).

        The saved period or episode is resumed or counted on the first
        observation (see _resume_after_restart).
        """
        self.clear()
        if not state:
            return
        self._load_learned(state)
        mode = state.get("hvac_mode")
        self._model_hvac_mode = str(mode) if isinstance(mode, str) else None
        end_wall = _number(state.get("last_episode_end_wall"))
        if end_wall is not None:
            self._last_episode_end = wall_to_monotonic_ts(end_wall)
        stash = state.get("mode_stash")
        self._mode_stash = {
            mode: snap
            for mode, snap in (stash.items() if isinstance(stash, dict) else ())
            if mode in (str(VThermHvacMode_HEAT), str(VThermHvacMode_COOL))
            and mode != self._model_hvac_mode
            and isinstance(snap, dict)
        }
        if self._model_hvac_mode is not None:
            self._restore = self._load_restart(state.get("restart"))
