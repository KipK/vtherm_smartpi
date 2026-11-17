"""The dead-time estimator sees a command at the moment Versatile Thermostat applies it.

A dead time counts from the onset of the step of the control signal (Astrom and Hagglund 1995,
2.4, p. 16). Versatile Thermostat applies a command after SmartPI's calculation and confirms it with
a receipt that carries the moment it was applied (a valve's position at once, a relay's cycle at its
start). SmartPI commits the applied power then, and feeds the estimator a start or a stop at that
moment, not at its next calculation, up to a calculation period later (documentation/en/
technical_doc.md, 3.6). Before, the start and the stop a room made itself both reached the
estimator up to 60 s late, so both its dead times came out shorter by as much.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.handler import SmartPIHandler
from custom_components.vtherm_smartpi.smartpi.calibration import CalibrationManager
from fakes.fake_thermostat_runtime import FakeThermostatRuntime
from helpers import force_smartpi_stable_mode, make_receipt

DEAD = 780.0
T0 = 19.0


def _handler(*, valve: bool = True, counted: bool = False, heating_since: float | None = None) -> SmartPIHandler:
    """A learned room (dead times 780 s, reliable), at rest, or heating at full power since
    ``heating_since``."""
    thermostat = FakeThermostatRuntime()
    thermostat.cycle_scheduler.is_valve_mode = valve
    thermostat.is_used_by_central_boiler = counted
    handler = SmartPIHandler(thermostat)
    with patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"):
        handler.init_algorithm()
    algo = thermostat.prop_algorithm
    force_smartpi_stable_mode(algo)
    est = algo.dt_est
    est.deadtime_heat_s = est.deadtime_cool_s = DEAD
    est.deadtime_heat_reliable = est.deadtime_cool_reliable = True
    est._history_heat.extend([DEAD] * 6)
    est._history_cool.extend([DEAD] * 6)
    if heating_since is None:
        est.state, est.last_power, est.last_start_time, est.last_stop_time = "OFF", 0.0, None, -10_000.0
    else:
        algo._committed_on_percent = 1.0
        est.state, est.last_power, est.last_start_time, est.last_stop_time = "HEATING", 1.0, heating_since, -10_000.0
    return handler


def _patched(at: float, boiler_on: bool | None = None):
    return (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.smartpi.deadtime_estimator.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.central_boiler_on", return_value=boiler_on),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    )


async def _calculation(handler: SmartPIHandler, at: float, boiler_on: bool | None = None) -> None:
    """SmartPI's calculation at ``at``; the committed power stays what the receipts committed."""
    handler._thermostat.current_temperature = T0
    p = _patched(at, boiler_on)
    with p[0], p[1], p[2], p[3], p[4], patch.object(handler, "_async_save", new_callable=AsyncMock):
        await handler.control_heating(force=False)


async def _applied(handler: SmartPIHandler, at: float, power: float, kind: str = "update") -> None:
    """Versatile Thermostat applies ``power`` at ``at`` and confirms it with a receipt."""
    algo = handler._thermostat.prop_algorithm
    p = _patched(at)
    with p[0], p[1], p[2], p[3], p[4]:
        await algo.on_command_receipt(make_receipt(None, power, kind=kind))


@pytest.mark.parametrize(("valve", "kind"), [(True, "update"), (False, "cycle_start")], ids=["valve", "relay"])
async def test_a_start_counts_from_the_command_s_application(valve, kind) -> None:
    """The room at rest calls: SmartPI calculates at 1000 s, Versatile Thermostat applies the
    command at 1010 s. The heating detection counts from 1010 s, not from the next calculation at
    1060 s."""
    handler = _handler(valve=valve)
    est = handler._thermostat.prop_algorithm.dt_est
    for at in range(100, 1001, 60):
        await _calculation(handler, float(at))
    await _applied(handler, 1010.0, 1.0, kind)
    assert (est.state, est.heat_start_time, est.last_start_time) == ("WAITING_HEAT_RESPONSE", 1010.0, 1010.0)

    await _calculation(handler, 1060.0)
    assert (est.state, est.heat_start_time, est.last_start_time) == ("WAITING_HEAT_RESPONSE", 1010.0, 1010.0)


@pytest.mark.parametrize(("valve", "kind"), [(True, "update"), (False, "cycle_start")], ids=["valve", "relay"])
async def test_a_stop_counts_from_the_command_s_application(valve, kind) -> None:
    """The room heated from 0 s, settled, and its command falls to 0, applied at 1030 s (a relay:
    at the start of its next cycle, when it really stays off). The passive detection counts from
    1030 s, not from the next calculation at 1060 s."""
    handler = _handler(valve=valve, heating_since=0.0)
    est = handler._thermostat.prop_algorithm.dt_est
    for at in range(400, 1001, 60):
        await _calculation(handler, float(at))
    await _applied(handler, 1030.0, 0.0, kind)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", 1030.0, 1030.0)

    await _calculation(handler, 1060.0)
    assert (est.state, est.cool_start_time, est.last_stop_time) == ("WAITING_COOL_RESPONSE", 1030.0, 1030.0)


async def test_a_command_that_does_not_cross_zero_feeds_nothing() -> None:
    """A heating room whose command changes from 100 % to 60 %: no start, no stop."""
    handler = _handler(heating_since=0.0)
    est = handler._thermostat.prop_algorithm.dt_est
    await _calculation(handler, 1000.0)
    before = (est.state, est.last_start_time, est.last_stop_time, len(est.tin_history))

    await _applied(handler, 1010.0, 0.6)

    assert (est.state, est.last_start_time, est.last_stop_time, len(est.tin_history)) == before


async def test_a_calibration_feeds_the_estimator_itself() -> None:
    """During a calibration the estimator follows the calibration's own command only: a receipt
    does not feed it."""
    handler = _handler()
    est = handler._thermostat.prop_algorithm.dt_est
    await _calculation(handler, 1000.0)
    with patch.object(CalibrationManager, "is_calibrating", new_callable=PropertyMock, return_value=True):
        await _applied(handler, 1010.0, 1.0)

    assert (est.state, est.last_start_time) == ("OFF", None)


async def test_a_room_whose_boiler_is_off_gets_no_start_at_the_application() -> None:
    """A room the central boiler counts, the boiler off: the applied command is no heat for the
    estimator (3.8.2), so no start; the boiler's start will be its start."""
    handler = _handler(counted=True)
    est = handler._thermostat.prop_algorithm.dt_est
    for at in range(100, 1001, 60):
        await _calculation(handler, float(at), boiler_on=False)
    await _applied(handler, 1010.0, 1.0)

    assert est.last_start_time is None and est.last_power == 0.0
