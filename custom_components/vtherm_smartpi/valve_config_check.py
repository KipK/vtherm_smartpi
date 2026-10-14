"""Diagnostic check of the Versatile Thermostat valve settings used with SmartPI.

In valve mode, Versatile Thermostat post-processes the opening requested by
SmartPI before it reaches the valve. This module only observes those settings,
reports them through Home Assistant Repairs and the published diagnostics, and
never alters the regulation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_DPERCENT_FILTER = "dpercent_filter"
ISSUE_PERIOD_FILTER = "period_filter"
ISSUE_OPENING_REMAP = "opening_remap"
ISSUE_CODES = (ISSUE_DPERCENT_FILTER, ISSUE_PERIOD_FILTER, ISSUE_OPENING_REMAP)

# Below one percent the integer rounding of the valve position already hides
# the filter: a smaller dpercent never holds or zeroes a command.
_DPERCENT_EFFECTIVE_MIN = 1.0
# An opening threshold up to 0.5 % is absorbed by the integer rounding of the
# opening degree (the remap stays the identity on every integer percent).
_THRESHOLD_IDENTITY_MAX = 0.5


@dataclass(frozen=True, slots=True)
class ValveConfigCheck:
    """Valve settings seen by SmartPI and the problems they cause."""

    dpercent: float | None
    period_min: float | None
    opening_remap_active: bool
    issues: tuple[str, ...]

    def to_diagnostics(self) -> dict[str, Any]:
        """Return the settings published under the live diagnostics tree."""
        return {
            "vt_dpercent": self.dpercent,
            "vt_period_min": self.period_min,
            "opening_remap_active": self.opening_remap_active,
            "issues": list(self.issues),
        }


def _as_float(value: Any) -> float | None:
    """Return a float, or None when the value is missing or not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _remapping_underlyings(thermostat: Any) -> list[Any]:
    """Return the underlyings that apply the opening-degree remap."""
    regulation = getattr(thermostat, "_underlyings_valve_regulation", None)
    if isinstance(regulation, (list, tuple)) and regulation:
        # over_climate_valve always remaps through send_percent_open.
        return list(regulation)
    underlyings = getattr(thermostat, "_underlyings", None)
    if not isinstance(underlyings, (list, tuple)):
        return []
    # over_valve remaps only when at least one opening parameter is set.
    return [u for u in underlyings if getattr(u, "_has_valve_control", False) is True]


def _is_remap_active(thermostat: Any) -> bool:
    """Return True when the opening-degree remap is not the identity."""
    for under in _remapping_underlyings(thermostat):
        min_open = _as_float(getattr(under, "_min_opening_degree", None))
        max_open = _as_float(getattr(under, "_max_opening_degree", None))
        max_closing = _as_float(getattr(under, "_max_closing_degree", None))
        threshold = _as_float(getattr(under, "_opening_threshold", None))
        if (
            (min_open is not None and min_open != 0.0)
            or (max_open is not None and max_open != 100.0)
            or (max_closing is not None and max_closing != 100.0)
            or (threshold is not None and threshold > _THRESHOLD_IDENTITY_MAX)
        ):
            return True
    return False


def inspect_valve_config(
    thermostat: Any, linearization_enabled: bool
) -> ValveConfigCheck | None:
    """Read the valve settings of a thermostat; None when it is not in valve mode."""
    scheduler = getattr(thermostat, "cycle_scheduler", None)
    if getattr(scheduler, "is_valve_mode", False) is not True:
        return None

    dpercent = _as_float(getattr(thermostat, "_auto_regulation_dpercent", None))
    period_min = _as_float(getattr(thermostat, "_auto_regulation_period_min", None))
    remap_active = _is_remap_active(thermostat)

    issues: list[str] = []
    if dpercent is not None and dpercent >= _DPERCENT_EFFECTIVE_MIN:
        issues.append(ISSUE_DPERCENT_FILTER)
    # The period delays every non-forced command, including mid-cycle updates.
    if period_min is not None and period_min > 0:
        issues.append(ISSUE_PERIOD_FILTER)
    if remap_active and linearization_enabled:
        issues.append(ISSUE_OPENING_REMAP)
    return ValveConfigCheck(dpercent, period_min, remap_active, tuple(issues))


def _issue_id(code: str, thermostat: Any) -> str:
    """Return the repair issue id of one problem of one thermostat."""
    return f"{code}_{thermostat.unique_id}"


def clear_valve_config_issues(thermostat: Any) -> None:
    """Delete every valve repair issue of a thermostat."""
    try:
        for code in ISSUE_CODES:
            ir.async_delete_issue(thermostat.hass, DOMAIN, _issue_id(code, thermostat))
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("%s - cannot clear valve repair issues: %s", thermostat, err)


def apply_valve_config_check(thermostat: Any, linearization_enabled: bool) -> None:
    """Publish the valve check on the algorithm and sync the repair issues."""
    check = inspect_valve_config(thermostat, linearization_enabled)
    algo = getattr(thermostat, "prop_algorithm", None)
    if algo is not None:
        algo.valve_config_diag = None if check is None else check.to_diagnostics()
    if check is None:
        clear_valve_config_issues(thermostat)
        return

    try:
        for code in ISSUE_CODES:
            issue_id = _issue_id(code, thermostat)
            if code not in check.issues:
                ir.async_delete_issue(thermostat.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                thermostat.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=code,
                translation_placeholders={
                    "name": str(thermostat.name),
                    "dpercent": f"{check.dpercent:g}" if check.dpercent is not None else "?",
                    "period_min": f"{check.period_min:g}" if check.period_min is not None else "?",
                },
            )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.warning("%s - cannot sync valve repair issues: %s", thermostat, err)
