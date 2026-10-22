"""Check the Versatile Thermostat version before registering SmartPI."""

from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation
from homeassistant.loader import LoaderError, async_get_integration
from vtherm_api.log_collector import get_vtherm_logger

from ..const import DOMAIN

MIN_VTHERM_VERSION = "10.6.2"
NOTIFICATION_ID = f"{DOMAIN}_vtherm_version"

_LOGGER = get_vtherm_logger(__name__)
_TITLE = "SmartPI cannot start"
_MESSAGE = (
    "SmartPI requires Versatile Thermostat {minimum_version} or later. "
    "Installed version: {installed_version}.\n\n"
    "SmartPI cannot start because this version is too old or could not be verified. "
    "Update Versatile Thermostat, then restart Home Assistant."
)


async def async_check_vtherm_version(hass: HomeAssistant) -> bool:
    """Notify and refuse setup unless VT meets the minimum supported version."""
    installed_version = "?"
    compatible = False
    try:
        integration = await async_get_integration(hass, "versatile_thermostat")
        version = integration.version
        if version is not None:
            installed_version = str(version) or "?"
            compatible = version.valid and version >= AwesomeVersion(MIN_VTHERM_VERSION)
    except (LoaderError, AwesomeVersionException):
        compatible = False

    if compatible:
        persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
        return True

    _LOGGER.error(
        "SmartPI requires Versatile Thermostat >= %s; installed version: %s. "
        "Setup refused because the version is too old or could not be verified",
        MIN_VTHERM_VERSION,
        installed_version,
    )
    translations = await translation.async_get_translations(
        hass, hass.config.language, "issues", {DOMAIN}
    )
    prefix = f"component.{DOMAIN}.issues.vtherm_version"
    message = translations.get(f"{prefix}.description", _MESSAGE).format(
        minimum_version=MIN_VTHERM_VERSION,
        installed_version=installed_version,
    )
    persistent_notification.async_create(
        hass,
        message,
        title=translations.get(f"{prefix}.title", _TITLE),
        notification_id=NOTIFICATION_ID,
    )
    return False
