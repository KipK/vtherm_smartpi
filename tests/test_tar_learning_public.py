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
    assert public.unconfirmed_reason("idle in the regulated part") == (
        "at least 1 hour without heating while the room was regulated at its low setpoint before the comfort step")
    assert public.unconfirmed_reason("idle in the regulated part, short delay") == public.unconfirmed_reason(
        "idle in the regulated part")
    assert public.unconfirmed_reason("no regulated start") == (
        "the room did not reach its low setpoint before the comfort step")
    assert public.unconfirmed_reason("regulated part too short") == (
        "the room was regulated at its low setpoint for less than 4 hours before the comfort step")
    assert public.period_reason("home position unknown") == (
        "home position unknown: the absence of sun cannot be checked")


def test_no_public_text_sets_a_clock_condition() -> None:
    """No reason refers to a fixed local hour."""
    texts = list(public._MORNING_REASONS.values()) + list(public._UNCONFIRMED.values())
    texts += list(public._PERIOD_REASONS.values())
    for text in texts:
        assert not re.search(r"\d{1,2}:\d{2}", text), text
        assert "morning" not in text, text
    assert public.morning_reason(None) is None and public.unconfirmed_reason(None) is None
