"""Local helpers shared by SmartPI tests."""

from __future__ import annotations

import time

from custom_components.vtherm_smartpi.smartpi.const import AB_HISTORY_SIZE


def force_smartpi_stable_mode(algo) -> None:
    """Populate enough history to make SmartPI leave hysteresis mode."""
    missing_a = max(0, AB_HISTORY_SIZE - len(algo.est.a_meas_hist))
    missing_b = max(0, AB_HISTORY_SIZE - len(algo.est.b_meas_hist))

    for _ in range(missing_a):
        algo.est.a_meas_hist.append(0.01)
    for _ in range(missing_b):
        algo.est.b_meas_hist.append(0.002)
    # A stable model has published a and b.
    algo.est.learn_ok_count_a = max(algo.est.learn_ok_count_a, 1)
    algo.est.learn_ok_count_b = max(algo.est.learn_ok_count_b, 1)

    algo._output_initialized = True
    algo.calibration_mgr.last_calibration_time = time.time()



def make_receipt(
    request_id: int | None,
    applied: float,
    *,
    kind: str = "cycle_start",
    hvac_mode: str = "heat",
    requested: float | None = None,
    is_repeat: bool = False,
    alterations: tuple[str, ...] = (),
    cycle_duration_sec: float = 120.0,
):
    """Build an applied-command receipt as the VT cycle scheduler emits it."""
    from vtherm_api.command_receipt import (
        AppliedCommandReceipt,
        CommandAlteration,
        CommandReceiptKind,
    )

    on_time_sec = int(applied * cycle_duration_sec)
    return AppliedCommandReceipt(
        kind=CommandReceiptKind(kind),
        request_id=request_id,
        hvac_mode=hvac_mode,
        requested_on_percent=applied if requested is None else requested,
        applied_on_percent=applied,
        on_time_sec=on_time_sec,
        off_time_sec=int(cycle_duration_sec - on_time_sec),
        cycle_duration_sec=cycle_duration_sec,
        cycle_start_monotonic=time.monotonic(),
        monotonic=time.monotonic(),
        is_repeat=is_repeat,
        alterations=frozenset(CommandAlteration(item) for item in alterations),
    )
