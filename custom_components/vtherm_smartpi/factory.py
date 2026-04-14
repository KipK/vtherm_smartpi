"""Factory for the SmartPI proportional algorithm plugin."""

from __future__ import annotations

from vtherm_api.interfaces import (
    InterfacePropAlgorithmFactory,
    InterfacePropAlgorithmHandler,
    InterfaceThermostatRuntime,
)

from .const import PROP_FUNCTION_SMART_PI
from .handler import SmartPIHandler


class SmartPIHandlerFactory(InterfacePropAlgorithmFactory):
    """Create SmartPI handlers for VT runtime thermostats."""

    @property
    def name(self) -> str:
        """Return the SmartPI proportional function identifier."""
        return PROP_FUNCTION_SMART_PI

    def create(
        self,
        thermostat: InterfaceThermostatRuntime,
    ) -> InterfacePropAlgorithmHandler:
        """Create a handler bound to the runtime thermostat."""
        return SmartPIHandler(thermostat)
