"""Test SmartPI state persistence: SmartPIStateStore and its handler wiring."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vtherm_smartpi import state_store
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.state_store import (
    SAVE_DELAY_SEC,
    SmartPIStateStore,
)
from fakes.fake_thermostat_runtime import FakeThermostatRuntime

_MONOTONIC = "custom_components.vtherm_smartpi.state_store.time.monotonic"


def _build_store() -> tuple[SmartPIStateStore, MagicMock, MagicMock, MagicMock]:
    """Return a state store over mocked primary and legacy HA Store objects."""
    primary, legacy = MagicMock(), MagicMock()
    for mock in (primary, legacy):
        mock.async_save = AsyncMock()
        mock.async_load = AsyncMock(return_value=None)
    hass = MagicMock()
    with patch.object(state_store, "Store", side_effect=[primary, legacy]) as cls:
        store = SmartPIStateStore(hass, "my_vtherm")
    assert [c.args[1:] for c in cls.call_args_list] == [
        (1, "vtherm_smartpi.my_vtherm"),
        (1, "versatile_thermostat.smartpi.my_vtherm"),
    ]
    return store, primary, legacy, hass


def _algo(snapshot: dict) -> MagicMock:
    """Return an algorithm stand-in with a controllable snapshot."""
    algo = MagicMock()
    algo.save_state.return_value = snapshot
    return algo


@pytest.mark.asyncio
async def test_load_returns_primary_data_without_reading_legacy():
    """Data found under the primary key is returned as is."""
    store, primary, legacy, hass = _build_store()
    primary.async_load.return_value = {"k": 1}

    assert await store.async_load() == {"k": 1}

    legacy.async_load.assert_not_awaited()
    primary.async_save.assert_not_called()
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_load_migrates_legacy_data_to_primary_key():
    """Legacy data is returned and written to the primary key."""
    store, primary, legacy, hass = _build_store()
    legacy.async_load.return_value = {"k": "legacy"}

    assert await store.async_load() == {"k": "legacy"}

    primary.async_save.assert_called_once_with({"k": "legacy"})
    hass.async_create_task.assert_called_once()
    hass.async_create_task.call_args.args[0].close()


@pytest.mark.asyncio
async def test_load_without_any_data_returns_none_and_writes_nothing():
    """Nothing is migrated when neither key holds data."""
    store, primary, _legacy, hass = _build_store()

    assert await store.async_load() is None

    primary.async_save.assert_not_called()
    hass.async_create_task.assert_not_called()


def test_schedule_save_arms_one_delayed_write_while_pending():
    """Repeated schedule calls must not re-arm the Store debounce timer."""
    store, primary, _legacy, _hass = _build_store()
    algo = _algo({"snapshot": 1})

    for now in (1000.0, 1060.0, 1000.0 + SAVE_DELAY_SEC - 1):
        with patch(_MONOTONIC, return_value=now):
            store.schedule_save(algo)

    primary.async_delay_save.assert_called_once()
    data_func, delay = primary.async_delay_save.call_args.args
    assert delay == SAVE_DELAY_SEC
    assert data_func() == {"snapshot": 1}
    primary.async_save.assert_not_called()


def test_schedule_save_arms_again_after_pending_window():
    """A new delayed write is scheduled once the previous one is due."""
    store, primary, _legacy, _hass = _build_store()
    algo = _algo({"snapshot": 1})

    with patch(_MONOTONIC, return_value=1000.0):
        store.schedule_save(algo)
    with patch(_MONOTONIC, return_value=1000.0 + SAVE_DELAY_SEC):
        store.schedule_save(algo)

    assert primary.async_delay_save.call_count == 2


def test_delayed_write_reads_state_at_write_time():
    """The scheduled data function returns the state current when it is called."""
    store, primary, _legacy, _hass = _build_store()
    algo = _algo({"snapshot": 1})

    with patch(_MONOTONIC, return_value=1000.0):
        store.schedule_save(algo)
    algo.save_state.return_value = {"snapshot": 2}

    assert primary.async_delay_save.call_args.args[0]() == {"snapshot": 2}


def test_schedule_save_failure_is_logged_and_retried_on_next_call():
    """A scheduling error does not raise and leaves no pending marker."""
    store, primary, _legacy, _hass = _build_store()
    primary.async_delay_save.side_effect = [RuntimeError("boom"), None]
    algo = _algo({"snapshot": 1})

    with patch(_MONOTONIC, return_value=1000.0):
        store.schedule_save(algo)
        store.schedule_save(algo)

    assert primary.async_delay_save.call_count == 2


@pytest.mark.asyncio
async def test_save_now_snapshots_synchronously_and_supersedes_pending_write():
    """save_now captures the state at call time and clears the pending marker."""
    store, primary, _legacy, _hass = _build_store()
    algo = _algo({"snapshot": 1})
    with patch(_MONOTONIC, return_value=1000.0):
        store.schedule_save(algo)

    write = store.save_now(algo)
    algo.save_state.return_value = {"snapshot": 2}
    await write

    primary.async_save.assert_awaited_once_with({"snapshot": 1})
    with patch(_MONOTONIC, return_value=1001.0):
        store.schedule_save(algo)
    assert primary.async_delay_save.call_count == 2


def _build_handler() -> tuple[SmartPIHandler, FakeThermostatRuntime, MagicMock]:
    """Return a handler (OFF mode, real SmartPI) wired to a mocked state store."""
    thermostat = FakeThermostatRuntime()
    thermostat.turn_off()
    thermostat.cycle_min = 5
    thermostat.hass.async_create_task = MagicMock()
    thermostat.prop_algorithm = SmartPI(
        hass=MagicMock(),
        cycle_min=5,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="persistence",
    )
    handler = SmartPIHandler(thermostat)
    store = MagicMock()
    store.save_now = MagicMock(side_effect=lambda _algo: AsyncMock()())
    handler._store = store
    return handler, thermostat, store


@pytest.mark.asyncio
async def test_control_heating_schedules_debounced_save_not_immediate_write():
    """The control loop uses the debounced path only."""
    handler, thermostat, store = _build_handler()

    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        await handler.control_heating(force=False)

    store.schedule_save.assert_called_once_with(thermostat.prop_algorithm)
    store.save_now.assert_not_called()


def test_remove_snapshots_synchronously_and_schedules_immediate_write():
    """remove() captures the state at call time, before the write task runs."""
    handler, thermostat, store = _build_handler()

    handler.remove()

    store.save_now.assert_called_once_with(thermostat.prop_algorithm)
    thermostat.hass.async_create_task.assert_called_once()
    store.schedule_save.assert_not_called()
    thermostat.hass.async_create_task.call_args.args[0].close()


@pytest.mark.asyncio
async def test_service_writes_immediately():
    """A mutating service awaits the immediate write."""
    handler, thermostat, store = _build_handler()
    write = AsyncMock()
    store.save_now = MagicMock(return_value=write())

    with (
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
        patch.object(handler, "update_attributes"),
    ):
        await handler.service_set_smartpi_learning(False)

    store.save_now.assert_called_once_with(thermostat.prop_algorithm)
    write.assert_awaited_once()
    store.schedule_save.assert_not_called()


@pytest.mark.asyncio
async def test_async_added_to_hass_loads_state_into_algorithm():
    """The handler feeds the loaded data to the algorithm."""
    handler, thermostat, store = _build_handler()
    thermostat.prop_algorithm = MagicMock()
    store.async_load = AsyncMock(return_value={"k": 1})

    await handler.async_added_to_hass()

    thermostat.prop_algorithm.load_state.assert_called_once_with({"k": 1})
