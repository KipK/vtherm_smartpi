"""Detection of the periods when SmartPI is not in control (T_ar learning data quality)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.vtherm_smartpi.const import HVAC_OFF_REASON_WINDOW_DETECTION
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF


def _thermostat(**kw):
    base = {"vtherm_hvac_mode": VThermHvacMode_HEAT, "hvac_off_reason": None, "is_overpowering_detected": False,
            "on_percent": 0.4, "window_manager": SimpleNamespace(is_window_detected=False)}
    base.update(kw)
    return SimpleNamespace(**base)


ALGO = SimpleNamespace(vt_safety_override=False, on_percent=0.4)


@pytest.mark.parametrize(
    "thermostat, algo, expected",
    [
        (_thermostat(), ALGO, None),
        (_thermostat(vtherm_hvac_mode=VThermHvacMode_OFF), ALGO, "hvac not heating"),
        (_thermostat(window_manager=SimpleNamespace(is_window_detected=True)), ALGO, "window open"),
        (_thermostat(hvac_off_reason=HVAC_OFF_REASON_WINDOW_DETECTION), ALGO, "window open"),
        (_thermostat(is_overpowering_detected=True), ALGO, "power shedding"),
        (_thermostat(), SimpleNamespace(vt_safety_override=True, on_percent=0.4), "safety mode"),
        (_thermostat(on_percent=None), ALGO, "command unknown"),
        (_thermostat(on_percent=0.1), ALGO, "command overridden"),
    ],
)
def test_exclusion_reasons(thermostat, algo, expected) -> None:
    assert SmartPIHandler._tar_exclusion(thermostat, algo) == expected
