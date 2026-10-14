"""Persistence of the SmartPI algorithm state through the Home Assistant Store."""

from __future__ import annotations

import logging
import time
from collections.abc import Coroutine
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "vtherm_smartpi.{}"
LEGACY_STORAGE_KEY = "versatile_thermostat.smartpi.{}"
# Debounce window for writes triggered by the control loop.
SAVE_DELAY_SEC = 300


class SmartPIStateStore:
    """Load and save the state of one SmartPI algorithm instance."""

    def __init__(self, hass: HomeAssistant, safe_name: str) -> None:
        """Create the primary and legacy stores for a slugified thermostat name."""
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY.format(safe_name))
        self._legacy_store = Store(
            hass, STORAGE_VERSION, LEGACY_STORAGE_KEY.format(safe_name)
        )
        # time.monotonic() deadline of the pending delayed write (0 when none).
        self._save_due = 0.0

    async def async_load(self) -> dict[str, Any] | None:
        """Load the saved state, migrating data found under the legacy key."""
        data = await self._store.async_load()
        if data is None:
            data = await self._legacy_store.async_load()
            if data is not None:
                self._hass.async_create_task(self._store.async_save(data))
        return data

    def save_now(self, algo) -> Coroutine[Any, Any, None]:
        """Snapshot the algorithm synchronously and return the coroutine writing it.

        An immediate Store.async_save supersedes any pending delayed write.
        """
        self._save_due = 0.0
        return self._store.async_save(algo.save_state())

    def schedule_save(self, algo) -> None:
        """Schedule a debounced write unless one is already pending.

        Store.async_delay_save re-arms its timer on every call, so calling it on
        each control cycle would postpone the write indefinitely.
        """
        now = time.monotonic()
        if now < self._save_due:
            return
        try:
            # save_state is evaluated when the write happens, not now.
            self._store.async_delay_save(algo.save_state, SAVE_DELAY_SEC)
            self._save_due = now + SAVE_DELAY_SEC
        except Exception as e:
            _LOGGER.error("Failed to schedule SmartPI state save: %s", e)
