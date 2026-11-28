"""Learning stage after a restart while the a learning waits for its delivered power model."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import (
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_B,
    SmartPIPhase,
)


def _smartpi(name: str, saved_state: dict | None = None) -> SmartPI:
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name=name,
        saved_state=saved_state,
    )


def _waiting_state(power_steps: dict | None, model: dict | None = None, key: str = "power_steps") -> dict:
    """State with a collected, b published, the given power steps block (under ``key``) and response model."""
    source = _smartpi("source")
    est = source.est
    est.ensure_hvac_mode(VThermHvacMode_HEAT)
    est.b = 0.001
    est.learn_ok_count_b = 18
    est.learn_ok_count = 18
    est.b_meas_hist.extend([0.001] * 18)
    est.a_meas_hist.extend([0.02, 0.03, 0.025, 0.018, 0.04, 0.022, 0.035, 0.028])
    est._a_obs_hist.extend([None] * 8)
    assert len(est.a_meas_hist) >= AB_MIN_SAMPLES_A
    assert len(est.b_meas_hist) >= AB_MIN_SAMPLES_B
    assert est.learn_ok_count_a == 0
    state = source.save_state()
    state["lw_state"].pop("power_steps", None)
    if power_steps is not None:
        state["lw_state"][key] = power_steps
    if model is not None:
        state["response_state"]["model"] = model
    return state


# Three heating phases, no response model: the delivered power model is not settled.
_UNRESOLVED = {
    "durations": [1500.0, 1500.0, 1500.0, 1500.0],
    "episodes": 3,
    "hvac_mode": "heat",
}
_MODEL = {"seed": {"heat": {"theta_s": 60.0, "tau_e_s": 300.0}}}


def test_restored_unresolved_lag_keeps_the_bootstrap_phase():
    smartpi = _smartpi("restored", _waiting_state(_UNRESOLVED))

    assert smartpi.learn_win.a_model_ready(VThermHvacMode_HEAT) is False
    assert smartpi.est.a_model_ready is False
    assert smartpi.phase == SmartPIPhase.HYSTERESIS


def test_first_calculation_after_restart_is_on_off_in_the_band():
    smartpi = _smartpi("restored", _waiting_state(_UNRESOLVED))

    # Inside the hysteresis band (SP - 0.3 < T < SP + 0.5).
    for _ in range(3):
        smartpi.calculate(
            target_temp=18.0,
            current_temp=17.9,
            ext_current_temp=10.0,
            slope=0,
            hvac_mode=VThermHvacMode_HEAT,
        )
        assert smartpi.phase == SmartPIPhase.HYSTERESIS
        assert smartpi.on_percent in (0.0, 1.0)


def test_restored_resolved_lag_leaves_the_bootstrap():
    resolved = dict(_UNRESOLVED, episodes=4)
    smartpi = _smartpi("restored", _waiting_state(resolved))

    assert smartpi.est.a_model_ready is True
    assert smartpi.phase == SmartPIPhase.STABLE


def test_restored_response_model_leaves_the_learning_stage():
    smartpi = _smartpi("restored", _waiting_state(_UNRESOLVED, _MODEL))

    assert smartpi.est.a_model_ready is True
    assert smartpi.phase == SmartPIPhase.STABLE


def test_state_of_the_former_response_lag_loads_its_heating_phases():
    former = dict(_UNRESOLVED, episodes=4, estimates=[[200.0, 20.0]] * 3, published=200.0)
    smartpi = _smartpi("restored", _waiting_state(former, key="response_lag"))

    assert smartpi.learn_win.power_steps.episodes_count == 4
    assert smartpi.est.a_model_ready is True


def test_state_without_power_steps_nor_model_keeps_the_learning_stage():
    smartpi = _smartpi("restored", _waiting_state(None))

    assert smartpi.est.a_model_ready is False
    assert smartpi.phase == SmartPIPhase.HYSTERESIS


def test_reset_learning_aligns_the_model_flag():
    smartpi = _smartpi("restored", _waiting_state(dict(_UNRESOLVED, episodes=4)))
    assert smartpi.est.a_model_ready is True

    smartpi.reset_learning()

    assert smartpi.learn_win.a_model_ready(VThermHvacMode_HEAT) is False
    assert smartpi.est.a_model_ready is False
    assert smartpi.phase == SmartPIPhase.HYSTERESIS


def test_band_turns_a_modulated_output_off():
    smartpi = _smartpi("band")
    assert smartpi.phase == SmartPIPhase.HYSTERESIS
    smartpi._set_linear_output(0.159)

    smartpi.calculate(
        target_temp=18.0,
        current_temp=17.9,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.ctl.hysteresis_state == "band"
    assert smartpi.on_percent == 0.0


def test_band_keeps_an_on_output():
    smartpi = _smartpi("band")
    smartpi._set_linear_output(1.0)

    smartpi.calculate(
        target_temp=18.0,
        current_temp=17.9,
        ext_current_temp=10.0,
        slope=0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert smartpi.on_percent == 1.0


def test_bootstrap_status_names_the_response_model_wait():
    smartpi = _smartpi("restored", _waiting_state(_UNRESOLVED))

    published = smartpi.get_published_diagnostics()

    assert published["learning"]["bootstrap_status"] == "step1 - waiting response model"
    assert "response_lag" not in published["analysis"]["learning"]


def test_bootstrap_status_keeps_the_collection_counts_below_target():
    state = _waiting_state(_UNRESOLVED, _MODEL)
    state["est_state"]["a_meas_hist"] = [0.02, 0.03]
    state["est_state"]["a_obs_hist"] = [None, None]
    smartpi = _smartpi("restored", state)

    published = smartpi.get_published_diagnostics()

    assert published["learning"]["bootstrap_status"] == "step2 - collecting emeas: A:2/5 B:18/5"
