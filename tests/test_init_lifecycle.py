"""Tests for the SmartPI config entry lifecycle (setup, options, unload, removal)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from awesomeversion import AwesomeVersion
import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import CoreState
from pytest_homeassistant_custom_component.common import MockConfigEntry
from vtherm_api.vtherm_api import VThermAPI

from custom_components.vtherm_smartpi import (
    _async_update_options,
    _get_dedicated_target_unique_ids,
    async_remove_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.vtherm_smartpi.const import (
    CONF_TARGET_VTHERM,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROP_FUNCTION_SMART_PI,
    SERVICE_FORCE_SMARTPI_CALIBRATION,
    SERVICE_RESET_SMARTPI_INTEGRAL,
    SERVICE_RESET_SMARTPI_LEARNING,
    SERVICE_SET_SMARTPI_LEARNING,
)

TARGET_UNIQUE_ID = "vt-target"


@pytest.fixture(autouse=True)
def compatible_vtherm(monkeypatch):
    """Keep lifecycle tests independent of the locally installed VT version."""
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi.smartpi.compatibility.async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version=AwesomeVersion("10.6.2"))),
    )


@pytest.fixture
def reload_helpers(monkeypatch):
    """Replace the VT reload helpers and record the plugin entries visible at call time."""
    visible_entries: list[list[str]] = []

    def _make(name: str) -> AsyncMock:

        async def _record(hass, *args):
            visible_entries.append(
                [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)]
            )

        mock = AsyncMock(side_effect=_record)
        monkeypatch.setattr(f"custom_components.vtherm_smartpi.{name}", mock)
        return mock

    return (
        _make("_reload_smartpi_vtherms_for_target"),
        _make("_reload_smartpi_vtherms_using_defaults"),
        visible_entries,
    )


def _dedicated_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}-{TARGET_UNIQUE_ID}",
        data={CONF_TARGET_VTHERM: TARGET_UNIQUE_ID},
    )


def _global_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=dict(DEFAULT_OPTIONS))


@pytest.mark.asyncio
async def test_async_setup_registers_factory_and_services(hass) -> None:
    """The factory and the services are registered once at integration setup."""
    assert await async_setup(hass, {})

    api = VThermAPI.get_vtherm_api(hass)
    assert api.get_prop_algorithm(PROP_FUNCTION_SMART_PI) is not None
    for service in (
        SERVICE_RESET_SMARTPI_LEARNING,
        SERVICE_SET_SMARTPI_LEARNING,
        SERVICE_FORCE_SMARTPI_CALIBRATION,
        SERVICE_RESET_SMARTPI_INTEGRAL,
    ):
        assert hass.services.has_service(DOMAIN, service)


@pytest.mark.asyncio
async def test_unload_keeps_factory_and_services_and_reloads_nothing(
    hass, reload_helpers
) -> None:
    """Unloading the last entry keeps the factory and services and does not reload VT entries."""
    for_target, using_defaults, _ = reload_helpers
    await async_setup(hass, {})
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True

    for_target.assert_not_awaited()
    using_defaults.assert_not_awaited()
    api = VThermAPI.get_vtherm_api(hass)
    assert api.get_prop_algorithm(PROP_FUNCTION_SMART_PI) is not None
    assert hass.services.has_service(DOMAIN, SERVICE_SET_SMARTPI_LEARNING)


@pytest.mark.asyncio
async def test_unload_failure_is_reported(hass) -> None:
    """A failed platform unload is propagated to Home Assistant."""
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    assert await async_unload_entry(hass, entry) is False


@pytest.mark.asyncio
async def test_remove_dedicated_entry_reloads_target_after_removal(
    hass, reload_helpers
) -> None:
    """Removing a dedicated entry reloads its target once it is gone from the registry."""
    for_target, using_defaults, visible_entries = reload_helpers
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    # Mirrors ConfigEntries._async_remove: unload, delete from registry, then the remove hook.
    assert await async_unload_entry(hass, entry)
    for_target.assert_not_awaited()
    del hass.config_entries._entries[entry.entry_id]
    await async_remove_entry(hass, entry)

    for_target.assert_awaited_once_with(hass, TARGET_UNIQUE_ID)
    using_defaults.assert_not_awaited()
    assert visible_entries == [[]]


@pytest.mark.asyncio
async def test_remove_global_entry_reloads_vtherms_using_defaults(
    hass, reload_helpers
) -> None:
    """Removing the global entry reloads the VT entries relying on the defaults."""
    for_target, using_defaults, _ = reload_helpers
    entry = _global_entry()
    entry.add_to_hass(hass)

    await async_remove_entry(hass, entry)

    using_defaults.assert_awaited_once_with(hass)
    for_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_update_on_dedicated_entry_reloads_only_its_target_once(
    hass, reload_helpers
) -> None:
    """An options change reloads the plugin entry, whose setup reloads the target once."""
    for_target, using_defaults, _ = reload_helpers
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    async def _reload(entry_id: str) -> bool:
        assert entry_id == entry.entry_id
        return await async_setup_entry(hass, entry)

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload)

    await _async_update_options(hass, entry)

    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
    for_target.assert_awaited_once_with(hass, TARGET_UNIQUE_ID)
    using_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_update_on_global_entry_reloads_only_default_vtherms_once(
    hass, reload_helpers
) -> None:
    """A global entry options change reloads only the VT entries using the defaults, once."""
    for_target, using_defaults, _ = reload_helpers
    entry = _global_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    async def _reload(entry_id: str) -> bool:
        return await async_setup_entry(hass, entry)

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload)

    await _async_update_options(hass, entry)

    using_defaults.assert_awaited_once_with(hass)
    for_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_entry_defers_vtherm_reload_until_homeassistant_runs(
    hass, reload_helpers
) -> None:
    """During startup VT handles its own deferred init, so setup must not reload VT entries."""
    for_target, using_defaults, _ = reload_helpers
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.set_state(CoreState.starting)

    assert await async_setup_entry(hass, entry) is True

    for_target.assert_not_awaited()
    using_defaults.assert_not_awaited()


@pytest.fixture
def state_at_reload(reload_helpers):
    """Record the disabled flag and load state of the plugin entry at each VT reload."""
    for_target, using_defaults, _ = reload_helpers
    seen: list[tuple[str, object, ConfigEntryState]] = []

    def _watch(name: str, mock: AsyncMock, entry: MockConfigEntry) -> None:
        inner = mock.side_effect

        async def _record(hass, *args):
            seen.append((name, entry.disabled_by, entry.state))
            await inner(hass, *args)

        mock.side_effect = _record

    return for_target, using_defaults, seen, _watch


@pytest.mark.asyncio
async def test_disabling_dedicated_entry_reloads_its_target_once_after_disabling(
    hass, state_at_reload
) -> None:
    """Disabling a dedicated entry reloads its target once, when the entry is already disabled."""
    for_target, using_defaults, seen, watch = state_at_reload
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    watch("target", for_target, entry)
    for_target.reset_mock()

    assert await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )

    for_target.assert_awaited_once_with(hass, TARGET_UNIQUE_ID)
    using_defaults.assert_not_awaited()
    assert seen == [
        ("target", ConfigEntryDisabler.USER, ConfigEntryState.UNLOAD_IN_PROGRESS)
    ]
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_disabling_global_entry_reloads_default_vtherms_once(
    hass, state_at_reload
) -> None:
    """Disabling the global entry reloads only the VT entries relying on the defaults."""
    for_target, using_defaults, seen, watch = state_at_reload
    entry = _global_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    watch("defaults", using_defaults, entry)
    using_defaults.reset_mock()

    assert await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )

    using_defaults.assert_awaited_once_with(hass)
    for_target.assert_not_awaited()
    assert [(name, disabled_by) for name, disabled_by, _ in seen] == [
        ("defaults", ConfigEntryDisabler.USER)
    ]


@pytest.mark.asyncio
async def test_enabling_dedicated_entry_reloads_its_target_once(
    hass, reload_helpers
) -> None:
    """Re-enabling a dedicated entry sets it up again, which reloads its target once."""
    for_target, using_defaults, _ = reload_helpers
    entry = _dedicated_entry()
    entry.disabled_by = ConfigEntryDisabler.USER
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)

    assert entry.state is ConfigEntryState.LOADED
    for_target.assert_awaited_once_with(hass, TARGET_UNIQUE_ID)
    using_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_unload_and_reload_do_not_reload_on_unload(
    hass, reload_helpers
) -> None:
    """Only a disabled entry triggers a VT reload while unloading."""
    for_target, using_defaults, _ = reload_helpers
    entry = _dedicated_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    for_target.reset_mock()

    assert await hass.config_entries.async_unload(entry.entry_id)

    for_target.assert_not_awaited()
    using_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_dedicated_entry_does_not_count_as_dedicated(hass) -> None:
    """A disabled dedicated entry leaves its target to the global entry."""
    enabled = _dedicated_entry()
    enabled.add_to_hass(hass)
    disabled = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}-vt-disabled",
        data={CONF_TARGET_VTHERM: "vt-disabled"},
        disabled_by=ConfigEntryDisabler.USER,
    )
    disabled.add_to_hass(hass)

    assert _get_dedicated_target_unique_ids(hass) == {TARGET_UNIQUE_ID}
