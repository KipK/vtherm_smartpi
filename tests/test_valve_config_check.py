"""Tests for the diagnostic check of the Versatile Thermostat valve settings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.helpers import issue_registry as ir

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.const import DOMAIN
from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.smartpi.valve_curve import ValveCurveParams
from custom_components.vtherm_smartpi.valve_config_check import (
    ISSUE_DPERCENT_FILTER,
    ISSUE_OPENING_REMAP,
    ISSUE_PERIOD_FILTER,
    apply_valve_config_check,
    inspect_valve_config,
)

from fakes.fake_thermostat_runtime import FakeThermostatRuntime

MODULE = "custom_components.vtherm_smartpi.valve_config_check"


def _under(**overrides) -> SimpleNamespace:
    """Return a valve underlying with the identity opening remap."""
    values = {
        "_has_valve_control": False,
        "_min_opening_degree": 0,
        "_max_opening_degree": 100,
        "_max_closing_degree": 100,
        "_opening_threshold": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _thermostat(dpercent=0.0, period=0, *, valve_mode=True, under=None, regulation=None):
    """Return a thermostat stand-in exposing the VT valve attributes."""
    thermostat = FakeThermostatRuntime()
    thermostat.cycle_scheduler.is_valve_mode = valve_mode
    thermostat._auto_regulation_dpercent = dpercent
    thermostat._auto_regulation_period_min = period
    thermostat._underlyings = [under or _under()]
    if regulation is not None:
        thermostat._underlyings_valve_regulation = regulation
    return thermostat


def test_over_valve_defaults_raise_dpercent_and_period_issues():
    """VT over_valve defaults (dpercent 10, period 5) filter SmartPI commands."""
    check = inspect_valve_config(_thermostat(10, 5), linearization_enabled=False)

    assert check.dpercent == 10.0
    assert check.period_min == 5.0
    assert not check.opening_remap_active
    assert check.issues == (ISSUE_DPERCENT_FILTER, ISSUE_PERIOD_FILTER)


def test_zero_dpercent_and_period_raise_no_issue():
    """A transparent VT configuration raises nothing."""
    check = inspect_valve_config(_thermostat(0, 0), linearization_enabled=True)

    assert check.issues == ()


def test_sub_one_percent_dpercent_is_not_a_problem():
    """Below 1 % the integer rounding already hides the dpercent filter."""
    check = inspect_valve_config(_thermostat(0.5, 0), linearization_enabled=False)

    assert check.issues == ()


def test_remap_needs_linearization_to_raise_an_issue():
    """A non-trivial remap only matters when SmartPI inverts its valve curve."""
    thermostat = _thermostat(under=_under(_has_valve_control=True, _min_opening_degree=20))

    assert inspect_valve_config(thermostat, False).issues == ()
    assert inspect_valve_config(thermostat, False).opening_remap_active
    assert inspect_valve_config(thermostat, True).issues == (ISSUE_OPENING_REMAP,)


def test_over_valve_without_valve_control_has_no_remap():
    """over_valve ignores its opening parameters when none is configured."""
    thermostat = _thermostat(under=_under(_has_valve_control=False, _min_opening_degree=20))

    assert not inspect_valve_config(thermostat, True).opening_remap_active


def test_climate_valve_remap_uses_regulation_underlyings():
    """over_climate_valve always remaps through its valve regulation underlyings."""
    thermostat = _thermostat(regulation=[_under(_opening_threshold=10)])

    assert inspect_valve_config(thermostat, True).issues == (ISSUE_OPENING_REMAP,)


def test_small_opening_threshold_is_the_identity():
    """A threshold up to 0.5 % is absorbed by the integer opening degree."""
    thermostat = _thermostat(regulation=[_under(_opening_threshold=0.5)])

    assert not inspect_valve_config(thermostat, True).opening_remap_active


def test_non_default_max_opening_and_closing_are_remaps():
    """Max opening and max closing different from 100 remap the opening."""
    assert inspect_valve_config(
        _thermostat(regulation=[_under(_max_opening_degree=80)]), True
    ).opening_remap_active
    assert inspect_valve_config(
        _thermostat(regulation=[_under(_max_closing_degree=90)]), True
    ).opening_remap_active


def test_not_valve_mode_returns_none():
    """Switch and climate thermostats are out of scope."""
    assert inspect_valve_config(_thermostat(10, 5, valve_mode=False), True) is None


def test_missing_attributes_do_not_crash():
    """A bare object yields no check; partial attributes yield no issue."""
    assert inspect_valve_config(SimpleNamespace(), True) is None

    partial = SimpleNamespace(cycle_scheduler=SimpleNamespace(is_valve_mode=True))
    check = inspect_valve_config(partial, True)
    assert check.dpercent is None
    assert check.period_min is None
    assert check.issues == ()


def test_diagnostics_payload():
    """The published payload carries the settings and the issue codes."""
    check = inspect_valve_config(_thermostat(10, 0), False)

    assert check.to_diagnostics() == {
        "vt_dpercent": 10.0,
        "vt_period_min": 0.0,
        "opening_remap_active": False,
        "issues": [ISSUE_DPERCENT_FILTER],
    }


def test_apply_creates_and_deletes_issues():
    """Problems create repair issues, absent problems delete them."""
    thermostat = _thermostat(10, 0)
    with patch(f"{MODULE}.ir.async_create_issue") as create, patch(
        f"{MODULE}.ir.async_delete_issue"
    ) as delete:
        apply_valve_config_check(thermostat, False)

    create.assert_called_once()
    args, kwargs = create.call_args
    assert args[1:] == (DOMAIN, f"{ISSUE_DPERCENT_FILTER}_fake-smartpi")
    assert kwargs["is_fixable"] is False
    assert kwargs["severity"] == ir.IssueSeverity.WARNING
    assert kwargs["translation_key"] == ISSUE_DPERCENT_FILTER
    assert kwargs["translation_placeholders"]["name"] == "Fake SmartPI"
    assert kwargs["translation_placeholders"]["dpercent"] == "10"
    assert {c.args[2] for c in delete.call_args_list} == {
        f"{ISSUE_PERIOD_FILTER}_fake-smartpi",
        f"{ISSUE_OPENING_REMAP}_fake-smartpi",
    }


def test_apply_without_problem_only_deletes():
    """A transparent configuration clears every issue."""
    with patch(f"{MODULE}.ir.async_create_issue") as create, patch(
        f"{MODULE}.ir.async_delete_issue"
    ) as delete:
        apply_valve_config_check(_thermostat(0, 0), True)

    create.assert_not_called()
    assert delete.call_count == 3


def test_apply_outside_valve_mode_clears_and_unpublishes():
    """Outside valve mode the diagnostics subtree is empty and issues are cleared."""
    thermostat = _thermostat(valve_mode=False)
    thermostat.prop_algorithm = MagicMock(valve_config_diag={"stale": True})
    with patch(f"{MODULE}.ir.async_create_issue") as create, patch(
        f"{MODULE}.ir.async_delete_issue"
    ) as delete:
        apply_valve_config_check(thermostat, False)

    create.assert_not_called()
    assert delete.call_count == 3
    assert thermostat.prop_algorithm.valve_config_diag is None


def test_apply_survives_registry_errors():
    """A registry failure never breaks the startup."""
    with patch(f"{MODULE}.ir.async_create_issue", side_effect=RuntimeError("boom")):
        apply_valve_config_check(_thermostat(10, 5), True)


async def test_issue_lifecycle_with_real_registry(hass):
    """The repair issue is registered at startup and removed with the handler."""
    thermostat = _thermostat(10, 5, under=_under(_has_valve_control=True, _opening_threshold=10))
    thermostat.hass = hass
    thermostat.prop_algorithm = SmartPI(
        hass=MagicMock(),
        cycle_min=5,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="valve check",
    )
    handler = SmartPIHandler(thermostat)
    handler._valve_linearization_configured = True
    registry = ir.async_get(hass)

    with patch.object(handler, "on_state_changed"):
        await handler.async_startup()

    for code in (ISSUE_DPERCENT_FILTER, ISSUE_PERIOD_FILTER, ISSUE_OPENING_REMAP):
        issue = registry.async_get_issue(DOMAIN, f"{code}_fake-smartpi")
        assert issue is not None
        assert issue.translation_key == code
        assert issue.is_fixable is False
        assert issue.severity == ir.IssueSeverity.WARNING
    assert thermostat.prop_algorithm.valve_config_diag["issues"] == [
        ISSUE_DPERCENT_FILTER,
        ISSUE_PERIOD_FILTER,
        ISSUE_OPENING_REMAP,
    ]

    handler.remove()

    for code in (ISSUE_DPERCENT_FILTER, ISSUE_PERIOD_FILTER, ISSUE_OPENING_REMAP):
        assert registry.async_get_issue(DOMAIN, f"{code}_fake-smartpi") is None


def test_published_valve_subtree():
    """live.valve is null outside valve mode and carries the quantization step."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=5,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="valve subtree",
        enable_valve_linearization=True,
        valve_curve_params=ValveCurveParams(
            min_valve=7.0, knee_demand=80.0, knee_valve=15.0, max_valve=100.0
        ),
    )
    assert algo.get_published_diagnostics()["valve"] is None

    algo.valve_config_diag = {
        "vt_dpercent": 0.0,
        "vt_period_min": 0.0,
        "opening_remap_active": False,
        "issues": [],
    }
    algo._committed_on_percent = 0.5
    valve = algo.get_published_diagnostics()["valve"]

    # Below the knee: 80 demand points over 8 valve points.
    assert valve["quantization_step_linear_percent"] == 10.0
    assert valve["issues"] == []

    algo._committed_on_percent = 0.0
    assert algo.get_published_diagnostics()["valve"]["quantization_step_linear_percent"] is None
