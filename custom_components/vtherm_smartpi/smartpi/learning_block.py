"""Learning block: every learning stops while the regulation goes on unchanged.

Three causes block the learning, each on its own:

- the pause of the ``set_smartpi_learning`` service (``paused``, saved with the state);
- the optional disturbance entity (``disturbance``): an unmeasured heat gain or loss (guests, fireplace, sun,
  auxiliary heater). The integral stays free: it is what compensates the disturbance;
- the optional heating-unavailable entity (``heating unavailable``): the driven emitter does not produce (boiler off,
  hot-water priority, heat-pump defrost, tariff cut-off, circulator stopped), in HEAT and COOL alike. The integral is
  held (``INTEGRAL_HOLD_SOURCE_HEATING_UNAVAILABLE``); back to off, the integral stays held for the effective dead
  time of the emitter through the resume hold (smartpi/resume_hold.py), counted from that instant.

An entity is a ``binary_sensor`` or an ``input_boolean``; only the state ``on`` counts as on: ``off``, ``unavailable``,
``unknown`` and an absent entity count as off. The states are read at the start of every calculation, the first one
after a restart included. The entities never change the saved pause.

While the learning is blocked: the a/b windows, the emitter lag, the measurement events and
their readings (start values and response model) are not fed, and no fit runs; the dead-time detection is suspended
but the temperature history that the anticipation and the pulses read is fed as usual; the records of the
measurement events are fed so that they stay continuous; the room history and the room model record each tick with
the block as exclusion reason; no measurement pulse starts and no starting value is handed to the estimator; a
calibration request is refused. When the learning becomes blocked, a calibration in progress is cancelled, the pulse
in progress ends and the readings in progress end without result. At each change of the block the learning window
and the dead-time detection in progress restart; the learning resumes with the waiting rules already in place.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from ..const import CONF_SMART_PI_DISTURBANCE_ENTITY, CONF_SMART_PI_HEATING_UNAVAILABLE_ENTITY
from .resume_hold import ResumeHoldSource
from .room_model.const import RM_EXCLUDED_WINDOW

_LOGGER = logging.getLogger(__name__)

# Published block reasons (live.learning.block_reason), plain words.
BLOCK_PAUSED = "paused"
BLOCK_DISTURBANCE = "disturbance"
BLOCK_HEATING_UNAVAILABLE = "heating unavailable"

# Integral hold source while the heating is unavailable (live.temperature.integral_hold_source).
INTEGRAL_HOLD_SOURCE_HEATING_UNAVAILABLE = "heating_unavailable"

# Exclusion reason given to the learning components (room history, room model, readings) for each block reason.
_EXCLUSION = {
    BLOCK_PAUSED: "learning paused",
    BLOCK_DISTURBANCE: "disturbance",
    BLOCK_HEATING_UNAVAILABLE: "heating unavailable",
}


def entity_on(hass: Any, entity_id: str | None) -> bool:
    """True only when the entity exists and its state is ``on`` (unavailable, unknown and absent count as off)."""
    if not entity_id or hass is None:
        return False
    try:
        state = hass.states.get(entity_id)
    except Exception:  # noqa: BLE001 - reading an entity must never disturb the regulation
        return False
    return getattr(state, "state", None) == "on"


class LearningBlock:
    """Whether the learning is blocked, and why; applies the effects of its changes on the owning ``SmartPI``."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner
        self.paused = False
        self.disturbance = False
        self.heating_unavailable = False
        self._hass: Any = None
        self._disturbance_entity: str | None = None
        self._heating_unavailable_entity: str | None = None
        self._applied = False   # the block whose effects were applied

    def configure(self, hass: Any, config: Mapping[str, Any]) -> None:
        """The entities of the SmartPI configuration (none when not set)."""
        self._hass = hass
        self._disturbance_entity = config.get(CONF_SMART_PI_DISTURBANCE_ENTITY) or None
        self._heating_unavailable_entity = config.get(CONF_SMART_PI_HEATING_UNAVAILABLE_ENTITY) or None

    @property
    def reason(self) -> str | None:
        """The published reason, None when the learning is not blocked (the heating, then the disturbance first)."""
        if self.heating_unavailable:
            return BLOCK_HEATING_UNAVAILABLE
        if self.disturbance:
            return BLOCK_DISTURBANCE
        return BLOCK_PAUSED if self.paused else None

    @property
    def blocked(self) -> bool:
        return self.reason is not None

    @property
    def exclusion(self) -> str | None:
        """Exclusion reason for the learning components, None when the learning is not blocked."""
        reason = self.reason
        return None if reason is None else _EXCLUSION[reason]

    def exclusion_for(self, excluded: str | None, safety: bool) -> str | None:
        """Why the room history or the room model must not learn from this tick: the block, except an open window
        (the room model follows the air through it), else the reason given, else the safety mode."""
        if self.blocked and excluded != RM_EXCLUDED_WINDOW:
            return self.exclusion
        if excluded is None and safety:
            return "safety mode"
        return excluded

    # -- changes -------------------------------------------------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        """The pause of the service (saved); applied at once."""
        self.paused = bool(paused)
        self._apply(time.monotonic())

    def read(self, now: float) -> None:
        """Start of a calculation: the states of the entities, then the effects of a change."""
        was_unavailable = self.heating_unavailable
        self.disturbance = entity_on(self._hass, self._disturbance_entity)
        self.heating_unavailable = entity_on(self._hass, self._heating_unavailable_entity)
        self._apply(now)
        if was_unavailable and not self.heating_unavailable:
            self._heating_back(now)

    def _apply(self, now: float) -> None:
        blocked = self.blocked
        if blocked == self._applied:
            return
        self._applied = blocked
        owner = self._owner
        if blocked:
            owner._start.block(now, self.exclusion)
        owner.learn_win.reset()
        owner.dt_est.reset_observation(owner._committed_on_percent, keep_history=True)
        _LOGGER.info("%s - SmartPI learning %s", getattr(owner, "_name", ""),
                     f"blocked ({self.reason})" if blocked else "resumed")

    def _heating_back(self, now: float) -> None:
        """The emitter produces again: its effective dead time counts from now, and the integral is held over it."""
        owner = self._owner
        if owner._t_heat_episode_start is not None:
            owner._t_heat_episode_start = now
        if owner._t_cool_episode_start is not None:
            owner._t_cool_episode_start = now
        owner.resume_hold.start(ResumeHoldSource.HEATING_UNAVAILABLE_RESUME)

    # -- what the learning sees --------------------------------------------------------------------------------
    def feed_deadtime(self, now: float, tin: float, tin_raw: float | None, u_applied: float, hvac_mode: Any) -> None:
        """Feed the dead-time estimator; while the learning is blocked only its temperature history is fed."""
        dt_est = self._owner.dt_est
        if self.blocked:
            dt_est.history.append(now, tin, tin_raw)
            return
        dt_est.update(now=now, tin=tin, tin_raw=tin_raw, u_applied=u_applied, hvac_mode=hvac_mode)

    def records_only(self, now: float, dt_min: float, u: float, ext: float | None) -> bool:
        """While the learning is blocked, feed only the records of the measurement events (they stay continuous) and
        return True: the learning update is skipped."""
        if not self.blocked:
            return False
        owner = self._owner
        if dt_min > 0.0:
            history = owner.learn_win.temperatures.raw
            for record in (owner._start.record, owner._start.model_reading.record):
                record.feed(now, dt_min * 60.0, max(0.0, min(1.0, u)), ext, history)
        return True
