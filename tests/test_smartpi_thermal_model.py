"""Analytical checks for the pure signed 1R1C propagator."""

from math import exp

import pytest

from custom_components.vtherm_smartpi.smartpi.thermal_model import propagate_1r1c
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_COOL,
    VThermHvacMode_HEAT,
)
from custom_components.vtherm_smartpi.smartpi.reference_governor import (
    PredictionSegment,
    predict_segmented_1r1c,
)
from custom_components.vtherm_smartpi.smartpi.setpoint import SmartPISetpointManager
from custom_components.vtherm_smartpi.smartpi.ff3_predictor import predict_ff3_open_loop
from custom_components.vtherm_smartpi.smartpi.thermal_twin_1r1c import ThermalTwin1R1C


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_signed_power_and_bias_match_closed_form(direction: float) -> None:
    temperature = 20.0
    external_temperature = 20.0
    a = direction * 0.08
    bias = direction * 0.01
    b = 0.004
    power = 0.5
    duration_min = 10.0

    actual = propagate_1r1c(
        temperature=temperature,
        external_temperature=external_temperature,
        a=a,
        b=b,
        power=power,
        duration_min=duration_min,
        bias=bias,
    )

    expected = temperature + (a * power + bias) * (1 - exp(-b * duration_min)) / b
    assert actual == pytest.approx(expected, abs=1e-12)
    assert (actual - temperature) * direction > 0.0


def test_zero_horizon_preserves_temperature() -> None:
    assert propagate_1r1c(
        temperature=21.0,
        external_temperature=5.0,
        a=-0.08,
        b=0.004,
        power=0.75,
        duration_min=0.0,
        bias=0.01,
    ) == 21.0


def test_small_bh_keeps_first_order_change() -> None:
    duration_min = 1e-8
    actual = propagate_1r1c(
        temperature=20.0,
        external_temperature=20.0,
        a=0.08,
        b=0.004,
        power=0.5,
        duration_min=duration_min,
    )
    assert actual - 20.0 == pytest.approx(0.04 * duration_min, rel=1e-5, abs=0.0)


@pytest.mark.parametrize("bias", [0.0, -0.015])
def test_semigroup_and_legacy_equilibrium_form(bias: float) -> None:
    inputs = dict(external_temperature=6.0, a=0.09, b=0.005, power=0.65, bias=bias)
    start = 19.0
    first = propagate_1r1c(temperature=start, duration_min=7.0, **inputs)
    second = propagate_1r1c(temperature=first, duration_min=13.0, **inputs)
    whole = propagate_1r1c(temperature=start, duration_min=20.0, **inputs)
    equilibrium = inputs["external_temperature"] + (
        inputs["a"] * inputs["power"] + bias
    ) / inputs["b"]
    legacy = equilibrium + (start - equilibrium) * exp(-inputs["b"] * 20.0)

    assert second == pytest.approx(whole, abs=1e-12)
    assert whole == pytest.approx(legacy, abs=1e-12)


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_braking_and_governor_match_segmented_closed_form(direction: float) -> None:
    mode = VThermHvacMode_HEAT if direction > 0 else VThermHvacMode_COOL
    a, b = direction * 0.4, 0.02
    expected = 20.0
    segments = (PredictionSegment(2.0, 0.6), PredictionSegment(3.0, 0.3))
    for segment in segments:
        equilibrium = 20.0 + a * segment.command / b
        expected = equilibrium + (expected - equilibrium) * exp(-b * segment.horizon_min)

    prediction = predict_segmented_1r1c(
        current_temp=20.0, ext_temp=20.0, a=a, b=b,
        hvac_mode=mode, segments=segments,
    )
    change = SmartPISetpointManager("test")._compute_predicted_signed_change(
        current_temp=20.0, ext_current_temp=20.0, hvac_mode=mode,
        a=a, b=b, u_ref=0.6, horizon_min=2.0,
        next_u_ref=0.3, next_horizon_min=3.0,
    )
    assert prediction is not None
    assert prediction.terminal_temp == pytest.approx(expected, abs=1e-12)
    assert change == pytest.approx(direction * (expected - 20.0), abs=1e-12)


def test_ff3_and_twin_match_closed_form_without_mutating_prediction_state() -> None:
    twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
    twin.reset(tin_init=20.0, text_init=5.0, u_init=0.6)
    before = twin.save_state()
    prediction = predict_ff3_open_loop(
        twin=twin, current_temp=20.0, ext_temp=5.0, a=0.12, b=0.004,
        u_first_cycle=0.6, u_base=0.6, cycle_min=1.0,
        deadtime_heat_s=0.0, horizon_cycles=1,
    )
    equilibrium = 5.0 + 0.12 * 0.6 / 0.004
    expected = equilibrium + (20.0 - equilibrium) * exp(-0.004)
    assert prediction.status == "ok"
    assert prediction.terminal_temperature == pytest.approx(expected, abs=1e-12)
    after = twin.save_state()
    # Persistence timestamps are generated on export, not observer mutations.
    before.pop("saved_at")
    after.pop("saved_at")
    assert after == before
    result = twin.step(
        tin_meas=expected, text_meas=5.0, a=0.12, b=0.004,
        u_now=0.6, deadtime_s=0.0,
    )
    assert result["T_pred"] == pytest.approx(expected, abs=1e-12)
    assert result["T_hat_next"] == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    ("mode", "a", "text", "active_deadtime_s", "passive_deadtime_s"),
    [
        ("heat", 0.4, 5.0, 600.0, 120.0),
        ("cool", -0.4, 35.0, 120.0, 600.0),
    ],
    ids=["heat", "cool"],
)
def test_twin_delays_power_on_and_off_by_the_mode_response_deadtime(
    mode: str,
    a: float,
    text: float,
    active_deadtime_s: float,
    passive_deadtime_s: float,
) -> None:
    """Power on uses the mode's active response, power off its passive one.

    The heat dead time (600 s) delays a rise and the cool one (120 s) a fall.
    """
    twin = ThermalTwin1R1C(dt_s=60, gamma=0.1)
    steps = 12

    def run(on_percent: float) -> list[dict]:
        return [
            twin.update_with_eta(
                tin=22.0, text=text, target=22.0, on_percent=on_percent,
                tau_reliable=True, a=a, b=0.02, mode=mode,
                deadtime_heat_s=600.0, deadtime_cool_s=120.0,
                deadtime_heat_reliable=True, deadtime_cool_reliable=True,
                dt_s=60.0,
            )
            for _ in range(steps)
        ]

    run(0.0)
    power_on = run(1.0)
    power_off = run(0.0)

    assert {r["deadtime_s"] for r in power_on} == {active_deadtime_s}
    assert {r["deadtime_s"] for r in power_off} == {passive_deadtime_s}
    # The applied command reaches the plant only after the selected delay.
    assert [r["u_eff"] for r in power_on] == [
        0.0 if index * 60.0 < active_deadtime_s else 1.0 for index in range(steps)
    ]
    assert [r["u_eff"] for r in power_off] == [
        1.0 if index * 60.0 < passive_deadtime_s else 0.0 for index in range(steps)
    ]
