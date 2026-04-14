"""Minimal fake runtime thermostat for plugin unit tests."""

from __future__ import annotations


class FakeThermostatRuntime:
    """Small runtime object used by factory-level tests."""

    def __init__(self) -> None:
        self.name = "Fake SmartPI"
        self.unique_id = "fake-smartpi"
