"""Fake runtime thermostat for plugin unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
)

from .fake_cycle_scheduler import FakeCycleScheduler


class _FakeConfigEntries:
    """Minimal config entry registry for handler tests."""

    def __init__(self) -> None:
        self._entries: list[object] = []

    def async_entries(self, _domain: str | None = None) -> list[object]:
        """Return the currently registered entries."""
        return list(self._entries)


@dataclass
class FakeHass:
    """Small Home Assistant stand-in used by tests."""

    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.bus = MagicMock()
        self.config = MagicMock()
        self.config_entries = _FakeConfigEntries()

    def async_create_task(self, coro):
        """Return the coroutine object to keep tests side-effect free."""
        return coro

    def verify_event_loop_thread(self, _msg: str | None = None) -> None:
        """Mimic the Home Assistant loop-thread guard used by helpers."""
        return None


class FakeThermostatRuntime:
    """Runtime object used by SmartPI unit tests."""

    def __init__(self) -> None:
        self.hass = FakeHass()
        self.name = "Fake SmartPI"
        self.unique_id = "fake-smartpi"
        self.entity_id = "climate.fake_smartpi"
        self.entry_infos: dict = {}
        self.prop_algorithm = None
        self.minimal_activation_delay = 0
        self.minimal_deactivation_delay = 0
        self.current_temperature = 19.0
        self.current_outdoor_temperature = 10.0
        self.target_temperature = 20.0
        self.last_temperature_slope = 0.0
        self.vtherm_hvac_mode = VThermHvacMode_HEAT
        self.hvac_action = None
        self.hvac_off_reason = None
        self.cycle_min = 10
        self.cycle_scheduler = FakeCycleScheduler()
        self.is_device_active = False
        self.is_overpowering_detected = False
        self.max_on_percent = 1.0
        self._custom_attributes_updates = 0
        self._ha_state_writes = 0
        self.async_underlying_entity_turn_off = AsyncMock()
        self.async_control_heating = AsyncMock(return_value=True)
        self._state = SimpleNamespace()

    def update_custom_attributes(self) -> None:
        """Track attribute refresh requests."""
        self._custom_attributes_updates += 1

    def async_write_ha_state(self) -> None:
        """Track state publication requests."""
        self._ha_state_writes += 1

    def turn_off(self) -> None:
        """Set the thermostat to OFF mode."""
        self.vtherm_hvac_mode = VThermHvacMode_OFF
