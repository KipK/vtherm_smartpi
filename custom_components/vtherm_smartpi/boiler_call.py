"""Versatile Thermostat's central boiler, read for SmartPI's learning.

A thermostat that Versatile Thermostat counts for its central boiler gets heat
only while the boiler is on. While SmartPI calls for heat and the boiler is
off, the room cools although the command says it heats: SmartPI does not learn
heating then (documentation/en/technical_doc.md, 3.8).
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er

from .smartpi.const import SMARTPI_RECALC_INTERVAL_SEC

# Versatile Thermostat's central boiler state, a binary sensor, by unique ID.
VT_DOMAIN = "versatile_thermostat"
VT_CENTRAL_BOILER_UNIQUE_ID = "central_boiler_state"

# The pause starts once SmartPI has called with the boiler off, without a break, for one period
# of its periodic calculation. When a thermostat's own start turns the boiler on, Versatile
# Thermostat does so only once it counts the thermostat's device, after the device reports its
# new state: seconds with a real valve head. Checks in that time must not pause. One whole period
# is much longer, and two periodic calculations a period apart still confirm a pause: their gap
# differs from the period by milliseconds, hence the margin of 1 s. The value follows SmartPI's
# calculation period, not a publication.
BOILER_PAUSE_TIMER_MARGIN_S = 1.0
BOILER_PAUSE_CONFIRM_S = SMARTPI_RECALC_INTERVAL_SEC - BOILER_PAUSE_TIMER_MARGIN_S

# A room on a valve, or on a relay whose heat flowed, holds a stop of the boiler as heat for this
# long while its heat flows (3.8.2, 3.8.4). Panel radiators measured by thermography lose 63 % of
# their heat only after 18 to 22.4 minutes once their water stops, and heat up to 63 % in 3.6 to
# 4.3 minutes (Bohac 2019, doctoral thesis, CTU Prague, Table 6, p. 77); measured with
# thermocouples, a panel radiator cools to 63 % in 19.8 minutes without a delay (Bartusek 2014,
# master's thesis, Brno University of Technology, Table 8, p. 62). In a first-order model
# (Astrom and Hagglund 1995, 2.4, p. 16: cooling with 18 to 22.4 minutes; heating with the
# apparent delay of 50 s and the time constant of 178 s of a 500 x 1000 mm panel, Bohac 2019,
# p. 77), a panel radiator still gives 89 % of its heat after one stop of 120 s, and 82 to 85 % on
# average when such stops repeat in every 5-minute cycle (Versatile Thermostat's default) of
# another room's relay; 180 s would leave 65 to 69 %. The dead-time estimator measures a start
# only at 80 % of power (min_power_heat_threshold): 120 s, two calculation periods, keeps the
# average above it, the bound lying at about 130 s for the radiators that cool fastest and 145 s
# for the slowest.
BOILER_VALVE_HOLD_S = 2 * SMARTPI_RECALC_INTERVAL_SEC

# The dead-time estimator follows the boiler's state with a filter for its blinks: a start counts
# once the boiler has run this long without a break (from the boiler's start), a stop at once, and
# a boiler back within this time of its stop blinked (the room's heat did not stop). Versatile
# Thermostat can switch its boiler off and on again within milliseconds as it counts the devices
# again (tens of milliseconds in synthetic tests with Versatile Thermostat 10.6). 5 s is two orders
# of magnitude longer than that, a twelfth of the estimator's time step (SmartPI's calculation,
# 60 s), and under 1 % of a room's dead time (a radiator alone takes minutes to answer: Bohac 2019,
# Table 6, p. 77). Waiting for a change to hold before acting on it is an alarm's delay timer
# (Adnan 2013, PhD thesis, University of Alberta, 3.3, p. 30); the value is chosen by this
# reasoning, not taken from a publication.
BOILER_BLINK_S = 5.0


def central_boiler_entity_id(hass: Any) -> str | None:
    """The entity ID of Versatile Thermostat's central boiler binary sensor, None without it."""
    try:
        registry = er.async_get(hass)
    except Exception:  # pylint: disable=broad-except
        return None
    return registry.async_get_entity_id("binary_sensor", VT_DOMAIN, VT_CENTRAL_BOILER_UNIQUE_ID)


def central_boiler_on(hass: Any) -> bool | None:
    """Whether Versatile Thermostat's central boiler is on; None when it cannot be read."""
    entity_id = central_boiler_entity_id(hass)
    states = getattr(hass, "states", None)
    state = states.get(entity_id) if entity_id is not None and states is not None else None
    if state is None or state.state not in ("on", "off"):
        return None
    return state.state == "on"
