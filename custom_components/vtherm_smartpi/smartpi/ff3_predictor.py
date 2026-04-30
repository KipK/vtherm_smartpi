"""Open-loop FF3 predictor based on the discrete 1R1C model."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, exp, isfinite

from .const import (
    FF3_MAX_HORIZON_CYCLES,
    FF3_MIN_HORIZON_CYCLES,
    FF3_RESPONSE_LOOKAHEAD_CYCLES,
    clamp,
)
from .thermal_twin_1r1c import ThermalTwin1R1C


@dataclass(frozen=True)
class FF3OpenLoopPrediction:
    temperatures: list[float]
    effective_powers: list[float]
    terminal_temperature: float
    status: str


@dataclass(frozen=True)
class FF3Horizon:
    horizon_cycles: int
    deadtime_cycles: int
    horizon_capped: bool


def compute_ff3_horizon(
    *,
    cycle_min: float,
    deadtime_heat_s: float | None,
) -> FF3Horizon:
    cycle_s = max(cycle_min * 60.0, 60.0)
    deadtime_s = max(deadtime_heat_s or 0.0, 0.0)
    deadtime_cycles = int(ceil(deadtime_s / cycle_s))
    raw_horizon = max(
        FF3_MIN_HORIZON_CYCLES,
        deadtime_cycles + FF3_RESPONSE_LOOKAHEAD_CYCLES,
    )
    horizon_capped = raw_horizon > FF3_MAX_HORIZON_CYCLES
    horizon_cycles = min(raw_horizon, FF3_MAX_HORIZON_CYCLES)
    return FF3Horizon(horizon_cycles, deadtime_cycles, horizon_capped)


def predict_ff3_open_loop(
    *,
    twin: ThermalTwin1R1C,
    current_temp: float,
    ext_temp: float,
    a: float,
    b: float,
    u_first_cycle: float,
    u_base: float,
    cycle_min: float,
    deadtime_heat_s: float | None,
    horizon_cycles: int,
) -> FF3OpenLoopPrediction:
    if (
        not isfinite(a)
        or not isfinite(b)
        or a < 0.0
        or b <= 0.0
        or not isfinite(current_temp)
        or not isfinite(ext_temp)
    ):
        return FF3OpenLoopPrediction([], [], current_temp, "invalid_params")

    state = twin.save_state()
    sim_time_s = float(state.get("sim_time_s", 0.0) or 0.0)
    u_buffer = list(state.get("u_buffer", []))
    d_hat_ema = float(state.get("d_hat_ema", 0.0) or 0.0)
    if not u_buffer:
        u_buffer = [(sim_time_s, u_base)]

    temperatures: list[float] = []
    effective_powers: list[float] = []
    temperature = current_temp
    cycle_s = max(cycle_min * 60.0, 60.0)
    dt_min = cycle_s / 60.0
    deadtime_s = max(deadtime_heat_s or 0.0, 0.0)

    for cycle_index in range(1, horizon_cycles + 1):
        sim_time_s += cycle_s
        u_now = u_first_cycle if cycle_index == 1 else u_base
        u_buffer.append((sim_time_s, clamp(u_now, 0.0, 1.0)))
        target_time = sim_time_s - deadtime_s
        u_eff = u_buffer[0][1]
        for ts, uv in u_buffer:
            if ts <= target_time:
                u_eff = uv
            else:
                break
        alpha = exp(-b * dt_min)
        t_eq = ext_temp + ((a * u_eff + d_hat_ema) / b)
        temperature = t_eq + ((temperature - t_eq) * alpha)
        if not isfinite(temperature):
            return FF3OpenLoopPrediction([], [], current_temp, "invalid_prediction")
        temperatures.append(temperature)
        effective_powers.append(u_eff)

        purge_horizon = sim_time_s - deadtime_s - cycle_s
        while len(u_buffer) > 1 and u_buffer[0][0] < purge_horizon:
            u_buffer.pop(0)

    return FF3OpenLoopPrediction(
        temperatures=temperatures,
        effective_powers=effective_powers,
        terminal_temperature=temperatures[-1],
        status="ok",
    )


def compute_ff3_action_sensitivity(predictions: list[FF3OpenLoopPrediction]) -> float:
    valid_predictions = [
        prediction
        for prediction in predictions
        if prediction.status == "ok"
    ]
    if len(valid_predictions) < 2:
        return 0.0
    terminal_temperatures = [
        prediction.terminal_temperature
        for prediction in valid_predictions
    ]
    return max(terminal_temperatures) - min(terminal_temperatures)
