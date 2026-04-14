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

    algo._output_initialized = True
    algo.calibration_mgr.last_calibration_time = time.time()

