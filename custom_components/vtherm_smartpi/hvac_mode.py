""" A custom data class to manage specific HVAC modes for VTherm. """

import logging
from vtherm_api.log_collector import get_vtherm_logger
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

OFF = HVACMode.OFF.value
HEAT = HVACMode.HEAT.value
COOL = HVACMode.COOL.value

VALID_HVAC_MODES = {OFF, HEAT, COOL, STATE_UNAVAILABLE, STATE_UNKNOWN}

_LOGGER = get_vtherm_logger(__name__)

class VThermHvacMode:
    """
    Container and validator for supported HVAC operating modes.
    """
    def __init__(self, hvac_mode: str):
        """
        Initialize a VThermHvacMode instance.

        Args:
            hvac_mode (VThermHvacMode): The HVAC mode to set.

        Raises:
            ValueError: If the mode is not supported.
        """
        if str(hvac_mode) not in VALID_HVAC_MODES or not isinstance(hvac_mode, str):
            _LOGGER.warning("Unsupported HVAC mode: '%s'. It may be temporary at startup. Your VTherm will be set to OFF mode.", hvac_mode)
            hvac_mode = STATE_UNKNOWN
        self._hvac_mode: str = hvac_mode

    def __str__(self):
        return self._hvac_mode

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self._hvac_mode == other
        else:
            return self._hvac_mode == str(other)

    def __repr__(self):
        return f"[VThermHvacMode: {self._hvac_mode}]"


VThermHvacMode_OFF = VThermHvacMode(OFF)
VThermHvacMode_HEAT = VThermHvacMode(HEAT)
VThermHvacMode_COOL = VThermHvacMode(COOL)
