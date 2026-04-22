"""Fake cycle scheduler used by SmartPI tests."""

from __future__ import annotations

from unittest.mock import AsyncMock


class FakeCycleScheduler:
    """Small scheduler exposing the handler-facing contract."""

    def __init__(self) -> None:
        self.is_cycle_running = False
        self.is_valve_mode = False
        self.start_cycle = AsyncMock(side_effect=self._start_cycle)
        self.cancel_cycle = AsyncMock(side_effect=self._cancel_cycle)
        self._cycle_start_callbacks: list = []
        self._cycle_end_callbacks: list = []

    def register_cycle_start_callback(self, callback) -> None:
        """Register a cycle start callback."""
        self._cycle_start_callbacks.append(callback)

    def register_cycle_end_callback(self, callback) -> None:
        """Register a cycle end callback."""
        self._cycle_end_callbacks.append(callback)

    async def _start_cycle(
        self,
        _hvac_mode,
        _on_percent: float,
        force: bool = False,
    ) -> None:
        """Record that a cycle was started."""
        self.is_cycle_running = True
        if force:
            self.is_cycle_running = True

    async def _cancel_cycle(self) -> None:
        """Record that a cycle was cancelled."""
        self.is_cycle_running = False
