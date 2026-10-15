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


class FakeReceiptCycleScheduler(FakeCycleScheduler):
    """Scheduler emitting applied-command receipts like Versatile Thermostat.

    A switch request received during a running cycle stays pending (no
    receipt); a valve request during a running cycle is applied at once
    (``update`` receipt); any other request starts a cycle (``cycle_start``).
    """

    def __init__(self, cycle_duration_sec: float = 600.0) -> None:
        super().__init__()
        self.cycle_duration_sec = cycle_duration_sec
        self._receipt_callbacks: list = []
        self.start_cycle = AsyncMock(side_effect=self._start_receipt_cycle)

    def register_command_receipt_callback(self, callback):
        """Register a receipt callback and return its remover."""
        self._receipt_callbacks.append(callback)

        def _remove() -> None:
            if callback in self._receipt_callbacks:
                self._receipt_callbacks.remove(callback)

        return _remove

    async def _start_receipt_cycle(
        self,
        hvac_mode,
        on_percent: float,
        force: bool = False,
        *,
        request_id: int | None = None,
    ) -> None:
        """Apply the request and emit its receipt when it is applied."""
        from helpers import make_receipt

        running = self.is_cycle_running
        self.is_cycle_running = True
        if running and not force and not self.is_valve_mode:
            return
        applied = max(0.0, min(1.0, on_percent))
        if not self.is_valve_mode:
            applied = int(applied * self.cycle_duration_sec) / self.cycle_duration_sec
        receipt = make_receipt(
            request_id,
            applied,
            kind="update" if running and not force else "cycle_start",
            hvac_mode=str(hvac_mode),
            requested=max(0.0, min(1.0, on_percent)),
            cycle_duration_sec=self.cycle_duration_sec,
        )
        for callback in list(self._receipt_callbacks):
            await callback(receipt)
