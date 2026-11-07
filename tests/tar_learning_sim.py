"""Deterministic closed-loop run of SmartPI on a simulated room (T_ar learning tests).

The room is one thermal capacity with a first-order emitter; the sensor
publishes 0.1 C steps. The clock is simulated (time.time and time.monotonic),
so two runs of the same code give the same outputs bit for bit.
"""

from __future__ import annotations

import asyncio
import math
from unittest.mock import MagicMock, patch

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT

WALL0 = 1_767_250_800.0  # 2026-01-01 07:00 UTC
MONO_OFFSET = 1_700_000_000.0
CYCLE_MIN = 10.0
A_TRUE = 0.0012  # C/s at full power
B_TRUE = 1.0 / (30.0 * 3600.0)  # 1/s
TAU_EMITTER = 420.0
T_EXT = 2.0


def _setpoint(t_wall: float) -> float:
    hour = ((t_wall - WALL0) / 3600.0 + 8.0) % 24.0  # local hour, WALL0 = 08:00
    return 20.0 if 6.0 <= hour < 22.0 else 18.0


def seed_stable(algo: SmartPI) -> None:
    """Stable model with reliable bootstrap dead times."""
    est = algo.est
    for _ in range(40):
        est.a_meas_hist.append(A_TRUE * 60.0)
        est.b_meas_hist.append(B_TRUE * 60.0)
    est.a = A_TRUE * 60.0
    est.b = B_TRUE * 60.0
    est.learn_ok_count_a = 40
    est.learn_ok_count_b = 40
    est.learn_ok_count = 80
    b_min = B_TRUE * 60.0
    for i in range(10):
        est._b_hat_hist.append(b_min * (1.0 + 0.01 * (i - 5)))
    algo.dt_est.deadtime_heat_s = 480.0
    algo.dt_est.deadtime_heat_reliable = True
    algo.dt_est.deadtime_cool_s = 900.0
    algo.dt_est.deadtime_cool_reliable = True
    algo._output_initialized = True
    algo.calibration_mgr.last_calibration_time = WALL0


class _SyncHass:
    """Executor stand-in running the jobs in the loop thread."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def run(hours: float = 30.0, *, tar_learning: bool | None = None, room_model: bool | None = None,
        prepare=None) -> list[list]:
    """Run the closed loop; return one row of regulation outputs per calculation.

    room_model: the room model observes every tick, runs its daily identification and builds its
    diagnostics once per cycle.
    """
    clock = {"t": WALL0}
    loop = asyncio.new_event_loop()
    rows: list[list] = []
    with patch("time.time", lambda: clock["t"]), patch("time.monotonic", lambda: clock["t"] - MONO_OFFSET):
        kwargs = {} if tar_learning is None else {"tar_learning": tar_learning}
        if room_model is not None:
            kwargs["room_model"] = room_model
        hass = _SyncHass()
        pub_time = None
        last_published = None
        algo = SmartPI(
            hass=MagicMock(),
            cycle_min=CYCLE_MIN,
            minimal_activation_delay=0,
            minimal_deactivation_delay=0,
            name="TarSim",
            use_ff3=True,
            **kwargs,
        )
        seed_stable(algo)
        if prepare is not None:
            prepare(algo)
        temp, emitter = 19.0, 0.0
        cycle_s = CYCLE_MIN * 60.0
        n_cycles = int(hours * 3600.0 / cycle_s)
        for _ in range(n_cycles):
            on_percent = None
            for k in range(int(cycle_s // 60)):
                sp = _setpoint(clock["t"])
                published = round(temp * 10.0) / 10.0
                out = algo.calculate(
                    target_temp=sp,
                    current_temp=published,
                    ext_current_temp=T_EXT,
                    slope=None,
                    hvac_mode=VThermHvacMode_HEAT,
                    cycle_boundary=(k == 0),
                )
                if tar_learning:
                    algo.observe_tar_learning(sp, published, T_EXT, VThermHvacMode_HEAT)
                if room_model:
                    if published != last_published:
                        pub_time, last_published = clock["t"], published
                    algo.observe_room_model(published, T_EXT, VThermHvacMode_HEAT, measure_id=pub_time)
                if k == 0:
                    on_percent = algo.on_percent
                    algo._commit_applied_command(on_percent, VThermHvacMode_HEAT, None, cycle_start=True)
                rows.append([
                    round(clock["t"] - WALL0, 1), out, algo.on_percent, algo.Kp, algo.Ki, algo.integral,
                    str(algo.phase), algo.u_ff, algo.deadband_mgr.near_band_below_deg,
                    algo.deadband_mgr.near_band_above_deg, algo.in_deadtime_window,
                ])
                for s in range(6):
                    u = 1.0 if (k * 60 + s * 10) < on_percent * cycle_s else 0.0
                    emitter += (u - emitter) * (1.0 - math.exp(-10.0 / TAU_EMITTER))
                    temp += 10.0 * (A_TRUE * emitter - B_TRUE * (temp - T_EXT))
                    clock["t"] += 10.0
            loop.run_until_complete(algo.on_cycle_completed(e_eff=on_percent, elapsed_ratio=1.0))
            if room_model:
                loop.run_until_complete(algo.room_model.async_run(hass))
                algo.room_model.diagnostics()
    loop.close()
    return rows
