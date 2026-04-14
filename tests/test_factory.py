"""Tests for the SmartPI plugin factory."""

from __future__ import annotations

from custom_components.vtherm_smartpi.const import PROP_FUNCTION_SMART_PI
from custom_components.vtherm_smartpi.factory import SmartPIHandlerFactory
from custom_components.vtherm_smartpi.handler import SmartPIHandler

from .fakes.fake_thermostat_runtime import FakeThermostatRuntime


def test_factory_name() -> None:
    factory = SmartPIHandlerFactory()

    assert factory.name == PROP_FUNCTION_SMART_PI


def test_factory_creates_handler() -> None:
    factory = SmartPIHandlerFactory()
    thermostat = FakeThermostatRuntime()

    handler = factory.create(thermostat)

    assert isinstance(handler, SmartPIHandler)
