from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakes.fake_thermostat_runtime import FakeThermostatRuntime

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)


class MockThermostat(FakeThermostatRuntime):
    def __init__(self):
        super().__init__()
        self.name = "MockThermostat"
        self.vtherm_hvac_mode = VThermHvacMode_HEAT
        self.hass = MagicMock()  # _async_save calls hass.async_create_task


def make_algo(name="profiles"):
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name=name,
    )


def make_real_handler():
    thermostat = MockThermostat()
    algo = make_algo()
    thermostat.prop_algorithm = algo
    handler = SmartPIHandler(thermostat)
    handler._store = MagicMock()
    handler._store.async_save = MagicMock()
    handler._profiles_loaded = True
    handler._load_failed = False
    return handler, thermostat, algo


def learned_a_value(profile_state):
    return profile_state["est_state"]["a"]


def test_real_algorithm_state_is_classified_by_mode():
    handler, _, algo = make_real_handler()

    algo.est.ensure_hvac_mode(VThermHvacMode_HEAT)
    assert handler._profile_from_algorithm_state(algo.save_state()) == "heat"

    algo.est.ensure_hvac_mode(VThermHvacMode_COOL)
    assert handler._profile_from_algorithm_state(algo.save_state()) == "cool"


def test_mode_tag_survives_save_and_load():
    # If this fails, a restored profile would be refused on every later save.
    handler, _, algo = make_real_handler()
    algo.est.ensure_hvac_mode(VThermHvacMode_COOL)

    restored = make_algo("restored")
    restored.load_state(algo.save_state())

    assert handler._profile_from_algorithm_state(restored.save_state()) == "cool"


@pytest.mark.asyncio
async def test_reset_learning_is_persisted_for_active_profile():
    handler, _, algo = make_real_handler()
    algo.load_state({
        "version": 2,
        "est_state": {"a": 0.01, "b": 0.02, "model_hvac_mode": str(VThermHvacMode_HEAT)},
    })
    handler._profiles["heat"] = algo.save_state()
    handler._active_profile = "heat"

    algo.reset_learning()
    assert algo.est.model_hvac_mode is None  # reset really untags the model

    await handler._async_save()

    handler._store.async_save.assert_called_once()
    saved = handler._store.async_save.call_args.args[0]
    assert learned_a_value(saved["profiles"]["heat"]) != pytest.approx(0.01)


def test_reset_is_not_undone_by_season_switch():
    handler, thermostat, algo = make_real_handler()
    algo.load_state({
        "version": 2,
        "est_state": {"a": 0.01, "b": 0.02, "model_hvac_mode": str(VThermHvacMode_HEAT)},
    })
    handler._profiles["heat"] = algo.save_state()
    handler._active_profile = "heat"

    algo.reset_learning()

    thermostat.vtherm_hvac_mode = VThermHvacMode_COOL
    handler._ensure_active_profile()
    thermostat.vtherm_hvac_mode = VThermHvacMode_HEAT
    handler._ensure_active_profile()

    assert algo.est.a != pytest.approx(0.01)


@pytest.mark.asyncio
async def test_reset_service_persists_reset_when_no_calculation_runs():
    handler, thermostat, algo = make_real_handler()
    algo.load_state({
        "version": 2,
        "est_state": {"a": 0.01, "b": 0.02, "model_hvac_mode": str(VThermHvacMode_HEAT)},
    })
    handler._profiles["heat"] = algo.save_state()
    handler._active_profile = "heat"
    thermostat.async_write_ha_state = MagicMock()

    with (
        patch.object(handler, "control_heating", new_callable=AsyncMock),
        patch.object(handler, "update_attributes"),
    ):
        await handler.service_reset_smart_pi_learning()

    handler._store.async_save.assert_called_once()
    saved = handler._store.async_save.call_args.args[0]
    assert learned_a_value(saved["profiles"]["heat"]) != pytest.approx(0.01)
