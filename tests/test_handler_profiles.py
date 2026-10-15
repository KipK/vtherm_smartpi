"""Tests for SmartPI seasonal profile handling."""

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.vtherm_smartpi.handler import (
    SMARTPI_PROFILE_STORE_VERSION,
    SmartPIHandler,
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)

HEAT = str(VThermHvacMode_HEAT)
COOL = str(VThermHvacMode_COOL)


def algo_state(mode, marker="x"):
    """A minimal fake of what SmartPI.save_state() returns."""
    return {"est_state": {"model_hvac_mode": mode}, "marker": marker}


def make_handler(hvac_mode=VThermHvacMode_HEAT, live_state=None):
    """Build a handler without running __init__."""
    h = SmartPIHandler.__new__(SmartPIHandler)
    h._thermostat = MagicMock()
    h._thermostat.vtherm_hvac_mode = hvac_mode
    h._thermostat.prop_algorithm = MagicMock()
    h._thermostat.prop_algorithm.save_state.return_value = live_state or algo_state(None)
    h._store = MagicMock()
    h._store.async_load = AsyncMock(return_value=None)
    h._store.async_save = MagicMock()  # plain Mock: avoids un-awaited coroutine warnings
    h._legacy_store = None
    h._profiles = {}
    h._active_profile = None
    h._unassigned_legacy_state = None
    h._profiles_loaded = True
    h._load_failed = False
    return h


# ---------- pure helpers ----------

class TestProfileKey:
    def test_heat(self):
        assert make_handler()._profile_key(VThermHvacMode_HEAT) == "heat"

    def test_cool(self):
        assert make_handler()._profile_key(VThermHvacMode_COOL) == "cool"

    @pytest.mark.parametrize("other", [None, object(), "off"])
    def test_other_modes_have_no_profile(self, other):
        assert make_handler()._profile_key(other) is None


class TestProfileFromAlgorithmState:
    def test_heat(self):
        assert make_handler()._profile_from_algorithm_state(algo_state(HEAT)) == "heat"

    def test_cool(self):
        assert make_handler()._profile_from_algorithm_state(algo_state(COOL)) == "cool"

    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"est_state": {}},
            {"est_state": {"model_hvac_mode": None}},
            {"est_state": {"model_hvac_mode": "auto"}},
            {"est_state": None},
            None,
            "garbage",
        ],
    )
    def test_unidentifiable_returns_none(self, state):
        assert make_handler()._profile_from_algorithm_state(state) is None


class TestIsProfileStore:
    def test_valid(self):
        data = {"profile_store_version": SMARTPI_PROFILE_STORE_VERSION, "profiles": {}}
        assert make_handler()._is_profile_store(data) is True

    @pytest.mark.parametrize(
        "data",
        [
            {"profile_store_version": SMARTPI_PROFILE_STORE_VERSION + 1, "profiles": {}},
            {"profile_store_version": SMARTPI_PROFILE_STORE_VERSION},
            {"profile_store_version": SMARTPI_PROFILE_STORE_VERSION, "profiles": []},
            {"est_state": {}},
            [],
            None,
        ],
    )
    def test_invalid(self, data):
        assert make_handler()._is_profile_store(data) is False


def test_build_profile_store_is_independent_copy():
    h = make_handler()
    h._profiles = {"heat": {"nested": [1, 2]}}
    store = h._build_profile_store()
    assert store["profile_store_version"] == SMARTPI_PROFILE_STORE_VERSION
    h._profiles["heat"]["nested"].append(3)
    assert store["profiles"]["heat"]["nested"] == [1, 2]


# ---------- _ensure_active_profile ----------

class TestEnsureActiveProfile:
    def test_noop_before_load(self):
        h = make_handler()
        h._profiles_loaded = False
        h._ensure_active_profile()
        assert h._active_profile is None

    def test_noop_for_unprofiled_mode(self):
        h = make_handler(hvac_mode=object())
        h._ensure_active_profile()
        assert h._active_profile is None
        h._thermostat.prop_algorithm.load_state.assert_not_called()

    def test_noop_when_already_active(self):
        h = make_handler()
        h._active_profile = "heat"
        h._ensure_active_profile()
        h._thermostat.prop_algorithm.save_state.assert_not_called()
        h._thermostat.prop_algorithm.load_state.assert_not_called()

    def test_first_activation_loads_saved_profile(self):
        h = make_handler()
        h._profiles = {"heat": algo_state(HEAT, "saved")}
        h._ensure_active_profile()
        h._thermostat.prop_algorithm.load_state.assert_called_once_with(
            algo_state(HEAT, "saved")
        )
        assert h._active_profile == "heat"

    def test_first_activation_without_profile_does_not_load(self):
        h = make_handler()
        h._ensure_active_profile()
        h._thermostat.prop_algorithm.load_state.assert_not_called()
        assert h._active_profile == "heat"

    def test_switch_saves_outgoing_and_loads_incoming(self):
        h = make_handler(hvac_mode=VThermHvacMode_COOL, live_state=algo_state(HEAT, "live"))
        h._active_profile = "heat"
        h._profiles = {"cool": algo_state(COOL, "old-cool")}
        h._ensure_active_profile()
        assert h._profiles["heat"] == algo_state(HEAT, "live")
        h._thermostat.prop_algorithm.load_state.assert_called_once_with(
            algo_state(COOL, "old-cool")
        )
        assert h._active_profile == "cool"

    def test_outgoing_profile_is_a_copy(self):
        live = algo_state(HEAT, "live")
        h = make_handler(hvac_mode=VThermHvacMode_COOL, live_state=live)
        h._active_profile = "heat"
        h._ensure_active_profile()
        live["marker"] = "mutated-after-save"
        assert h._profiles["heat"]["marker"] == "live"

    def test_mismatched_outgoing_state_is_not_saved(self):
        # Live model is tagged cool while we think heat is active.
        h = make_handler(hvac_mode=VThermHvacMode_COOL, live_state=algo_state(COOL))
        h._active_profile = "heat"
        h._profiles = {"heat": algo_state(HEAT, "good")}
        h._ensure_active_profile()
        assert h._profiles["heat"]["marker"] == "good"

    def test_untagged_outgoing_state_is_saved(self):
        # Regression: after reset_learning() the live model has no tag yet.
        h = make_handler(hvac_mode=VThermHvacMode_COOL, live_state=algo_state(None, "fresh"))
        h._active_profile = "heat"
        h._profiles = {"heat": algo_state(HEAT, "stale")}
        h._ensure_active_profile()
        assert h._profiles["heat"]["marker"] == "fresh"

    def test_unassigned_legacy_state_goes_to_first_active_mode(self):
        legacy = {"marker": "legacy"}
        h = make_handler(hvac_mode=VThermHvacMode_COOL)
        h._unassigned_legacy_state = legacy
        h._ensure_active_profile()
        assert h._profiles["cool"] == legacy
        assert h._unassigned_legacy_state is None
        h._thermostat.prop_algorithm.load_state.assert_called_once_with(legacy)


# ---------- _async_save ----------

class TestAsyncSave:
    @pytest.mark.asyncio
    async def test_no_active_profile_does_not_save(self):
        h = make_handler(live_state=algo_state(HEAT))
        await h._async_save()
        h._store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_failure_blocks_save(self):
        h = make_handler(live_state=algo_state(HEAT))
        h._active_profile = "heat"
        h._load_failed = True
        await h._async_save()
        h._store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_state_is_saved_alongside_other_profile(self):
        h = make_handler(live_state=algo_state(HEAT, "new"))
        h._active_profile = "heat"
        h._profiles = {"cool": algo_state(COOL, "keep")}
        await h._async_save()
        saved = h._store.async_save.call_args.args[0]
        assert saved["profiles"]["heat"]["marker"] == "new"
        assert saved["profiles"]["cool"]["marker"] == "keep"

    @pytest.mark.asyncio
    async def test_mismatched_state_is_refused(self):
        h = make_handler(live_state=algo_state(COOL))
        h._active_profile = "heat"
        await h._async_save()
        h._store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_untagged_state_is_saved(self):
        h = make_handler(live_state=algo_state(None, "fresh"))
        h._active_profile = "heat"
        await h._async_save()
        h._store.async_save.assert_called_once()


# ---------- async_added_to_hass ----------

class TestLoad:
    @pytest.mark.asyncio
    async def test_empty_storage_still_marks_loaded(self):
        h = make_handler()
        h._profiles_loaded = False
        await h.async_added_to_hass()
        assert h._profiles_loaded is True
        assert h._profiles == {}
        assert h._load_failed is False

    @pytest.mark.asyncio
    async def test_new_format_is_loaded(self):
        h = make_handler()
        h._store.async_load = AsyncMock(return_value={
            "profile_store_version": SMARTPI_PROFILE_STORE_VERSION,
            "profiles": {"heat": algo_state(HEAT), "cool": algo_state(COOL)},
        })
        await h.async_added_to_hass()
        assert set(h._profiles) == {"heat", "cool"}
        h._store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_state_is_migrated_and_persisted(self):
        legacy = algo_state(COOL, "legacy")
        h = make_handler()
        h._store.async_load = AsyncMock(return_value=copy.deepcopy(legacy))
        await h.async_added_to_hass()
        assert h._profiles == {"cool": legacy}
        saved = h._store.async_save.call_args.args[0]
        assert saved["profiles"] == {"cool": legacy}

    @pytest.mark.asyncio
    async def test_legacy_state_without_mode_is_kept_unassigned(self):
        legacy = {"marker": "no-mode"}
        h = make_handler()
        h._store.async_load = AsyncMock(return_value=legacy)
        await h.async_added_to_hass()
        assert h._profiles == {}
        assert h._unassigned_legacy_state == legacy

    @pytest.mark.asyncio
    async def test_unsupported_version_is_left_untouched(self):
        h = make_handler()
        h._store.async_load = AsyncMock(return_value={
            "profile_store_version": SMARTPI_PROFILE_STORE_VERSION + 1,
            "profiles": {"heat": algo_state(HEAT)},
        })
        await h.async_added_to_hass()
        assert h._load_failed is True
        assert h._profiles == {}
        assert h._unassigned_legacy_state is None
        h._store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_during_load_blocks_saves(self):
        h = make_handler()
        h._store.async_load = AsyncMock(side_effect=OSError("disk"))
        await h.async_added_to_hass()
        assert h._load_failed is True
        assert h._profiles_loaded is True
