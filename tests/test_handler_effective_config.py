"""Tests for SmartPIHandler._resolve_effective_config entry selection."""

from types import SimpleNamespace

from custom_components.vtherm_smartpi.const import (
    CONF_SMART_PI_DEADBAND,
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from fakes.fake_thermostat_runtime import FakeThermostatRuntime

DEDICATED_DEADBAND = 0.123
GLOBAL_DEADBAND = 0.456


def _entry(*, dedicated: bool, deadband: float, disabled: bool = False) -> SimpleNamespace:
    """Return a plugin entry stand-in bound to the fake thermostat or to the defaults."""
    runtime = FakeThermostatRuntime()
    return SimpleNamespace(
        unique_id=f"{DOMAIN}-{runtime.unique_id}" if dedicated else DOMAIN,
        data={CONF_TARGET_VTHERM: runtime.unique_id} if dedicated else {},
        options={CONF_SMART_PI_DEADBAND: deadband},
        disabled_by="user" if disabled else None,
    )


def _resolve(*entries: SimpleNamespace) -> tuple[dict, object | None]:
    thermostat = FakeThermostatRuntime()
    thermostat.hass.config_entries._entries.extend(entries)
    return SmartPIHandler(thermostat)._resolve_effective_config()


def test_dedicated_entry_wins_over_global_entry():
    """An enabled dedicated entry is applied over the global entry."""
    dedicated = _entry(dedicated=True, deadband=DEDICATED_DEADBAND)
    config, applied = _resolve(_entry(dedicated=False, deadband=GLOBAL_DEADBAND), dedicated)

    assert applied is dedicated
    assert config[CONF_SMART_PI_DEADBAND] == DEDICATED_DEADBAND


def test_disabled_dedicated_entry_falls_back_to_enabled_global_entry():
    """A disabled dedicated entry is ignored in favour of the enabled global entry."""
    global_entry = _entry(dedicated=False, deadband=GLOBAL_DEADBAND)
    config, applied = _resolve(
        _entry(dedicated=True, deadband=DEDICATED_DEADBAND, disabled=True), global_entry
    )

    assert applied is global_entry
    assert config[CONF_SMART_PI_DEADBAND] == GLOBAL_DEADBAND


def test_disabled_dedicated_entry_without_global_entry_uses_defaults():
    """Without an enabled global entry, a disabled dedicated entry leaves the defaults."""
    config, applied = _resolve(_entry(dedicated=True, deadband=DEDICATED_DEADBAND, disabled=True))

    assert applied is None
    assert config[CONF_SMART_PI_DEADBAND] == DEFAULT_OPTIONS[CONF_SMART_PI_DEADBAND]


def test_disabled_global_entry_is_ignored():
    """A disabled global entry leaves the defaults."""
    config, applied = _resolve(_entry(dedicated=False, deadband=GLOBAL_DEADBAND, disabled=True))

    assert applied is None
    assert config[CONF_SMART_PI_DEADBAND] == DEFAULT_OPTIONS[CONF_SMART_PI_DEADBAND]


def test_disabled_global_entry_does_not_hide_enabled_dedicated_entry():
    """An enabled dedicated entry stays applied when the global entry is disabled."""
    dedicated = _entry(dedicated=True, deadband=DEDICATED_DEADBAND)
    config, applied = _resolve(
        dedicated, _entry(dedicated=False, deadband=GLOBAL_DEADBAND, disabled=True)
    )

    assert applied is dedicated
    assert config[CONF_SMART_PI_DEADBAND] == DEDICATED_DEADBAND
