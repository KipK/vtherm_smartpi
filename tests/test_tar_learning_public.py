"""T_ar learning diagnostics show plain values, not the internal codes of the methods."""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.vtherm_smartpi.smartpi.tar_learning import public

SRC = Path(__file__).resolve().parent.parent / "custom_components" / "vtherm_smartpi" / "smartpi" / "tar_learning"


def test_room_classes_are_plain() -> None:
    assert public.room_class("mass") == "thermal_mass"
    assert public.room_class("one") == "no_thermal_mass"
    for code in ("unknown", "inconclusive", "none", "too_short"):
        assert public.room_class(code) == "undetermined"
    assert public.room_class(None) is None


def test_every_morning_reason_of_the_methods_is_translated() -> None:
    """Each literal reason a morning can end with has a plain wording."""
    codes = set()
    for name in ("morning.py", "manager.py"):
        text = (SRC / name).read_text()
        codes |= set(re.findall(r'return False, "([^"]+)"', text))
        codes |= set(re.findall(r'"why": "([^"]+)"', text))
        codes |= set(re.findall(r'\["reason"\] = "([^"]+)"', text))
        codes |= set(re.findall(r'\{"reason": "([^"]+)"\}', text))
        codes |= set(re.findall(r'_skip_morning\([^,]+, "([^"]+)"\)', text))
    codes -= {"window outside history"}  # identification windows, not shown per morning
    assert codes
    for code in codes:
        assert public.morning_reason(code) != code or code in ("accepted", "computation failed"), code


def test_phase_and_unconfirmed_reasons() -> None:
    assert public.morning_reason("phase HYSTERESIS") == "SmartPI not in steady regulation (hysteresis)"
    assert public.unconfirmed_reason("night started passive in the sleep span") == (
        "night with at least 1 hour without heating between 23:00 and the comfort step")
    assert public.morning_reason(None) is None and public.unconfirmed_reason(None) is None
