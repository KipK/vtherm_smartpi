"""A passive response dated back to the boiler's stop follows the estimator's tracking.

A valve room holds a stop of the central boiler as heat (technical_doc.md 3.8.2); when the stop is
confirmed later (the room's own stop applied by Versatile Thermostat, the room's own stop seen by a
calculation, or the hold running out), SmartPI dates the room's stop and its passive response back
to the boiler's stop (``SmartPI._heat_stopped_at``). The response's peak then comes from what the
estimator's tracking followed since, so a raw step of the sensor not confirmed yet counts there as
in the tracking (3.6). A change counts only above the measurement's noise band (Astrom and
Hagglund 1995, 6.4, p. 237; 6.6, pp. 245-246; 2.6, p. 39; Hagglund and Astrom 2000, 4, p. 1174), and a dead
time counts from the onset of the step (Astrom and Hagglund 1995, 2.4, p. 16).

Before, the peak came from the filter's own history: a one-step toggle of the reading, or a step
not confirmed yet, raised it, and the filter's return looked like the room's drop at once (in
synthetic tests, dead times of tens of seconds to two minutes for a room of about 1000 s). The room
here: flat at 19.0 degC, then cooling 780 s after the heat really ended (``test_boiler_cut_heat``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import test_boiler_cut_heat as cut
from test_boiler_cut_heat import STOP, _boiler, _heated, _step

from custom_components.vtherm_smartpi.boiler_call import BOILER_VALVE_HOLD_S
from helpers import make_receipt

STEP_C = 0.1  # one step of a 0.1-degC sensor
OWN_STOP = STOP + 40.0  # the room's own stop, inside the valve's hold
HOLD_ENDS = STOP + BOILER_VALVE_HOLD_S
ONE_CALCULATION_S = 60.0  # the measurement's resolution


def _monotonic(at: float):
    return (
        patch("custom_components.vtherm_smartpi.handler.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.algo.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.smartpi.deadtime_estimator.time.monotonic", return_value=at),
        patch("custom_components.vtherm_smartpi.handler.async_dispatcher_send"),
    )


async def _passive_dead_time(path: str, change: str | None, onset: float, calculations: int) -> tuple[str | None, list[float]]:
    """The passive outcome and the cooling history after a boiler's stop at STOP that the valve
    holds, confirmed by ``path`` at the dating moment; the raw reading steps up one step at
    ``onset`` and stays (``step``) or comes back at the dating moment (``toggle``); exactly
    ``calculations`` calculations from the onset to the dating moment."""
    dating = HOLD_ENDS if path == "hold ends" else OWN_STOP
    stops = path != "hold ends"
    base = cut._temp

    def temp(at: float) -> float:
        up = change is not None and at >= onset and (change == "step" or at < dating)
        return base(at) + (STEP_C if up else 0.0)

    with patch.object(cut, "_temp", temp):
        handler = await _heated()
        algo = handler._thermostat.prop_algorithm
        est = algo.dt_est
        times = [onset + (dating - onset) * i / calculations for i in range(1, calculations + 1)]
        boiler_seen = False
        for at in times[:-1]:
            if not boiler_seen and at >= STOP:
                _boiler(handler, False, STOP)
                boiler_seen = True
            await _step(handler, at, at < STOP, 1.0)
        if not boiler_seen:
            _boiler(handler, False, STOP)
        if path == "applied":
            # Versatile Thermostat applies the room's 0 % at the dating moment (a valve: at once).
            p = _monotonic(dating)
            with p[0], p[1], p[2], p[3]:
                await algo.on_command_receipt(make_receipt(None, 0.0, kind="update"))
            await _step(handler, dating + 1.0, False, 0.0)
        elif path == "own stop":
            await _step(handler, dating, False, 0.0)
        else:
            await _step(handler, dating, False, 1.0)  # the hold runs out while the room calls
        assert est.last_stop_time == STOP and est.cool_start_time == STOP, (est.last_stop_time, est.cool_start_time)
        at = dating + 1.0
        while at < STOP + 3600.0 and est.state == "WAITING_COOL_RESPONSE":
            at += 60.0
            await _step(handler, at, False, 0.0 if stops else 1.0)
        return est.last_outcome["cool"], list(est._history_cool)


PATHS = ["applied", "own stop", "hold ends"]


@pytest.mark.parametrize("path", PATHS)
async def test_without_a_step_the_dated_back_response_is_the_reference(path) -> None:
    """No step: each path dates the response back to the boiler's stop and measures it."""
    outcome, history = await _passive_dead_time(path, None, STOP - 10.0, 3)
    assert outcome == "detected" and len(history) == 1 and history[0] > 900.0


@pytest.mark.parametrize("calculations", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("change", ["step", "toggle"])
@pytest.mark.parametrize("where", ["before the boiler's stop", "in the hold"])
@pytest.mark.parametrize("path", PATHS)
async def test_a_step_near_the_boiler_s_stop_does_not_shorten_the_dated_back_response(path, where, change, calculations) -> None:
    """A raw step of one sensor step 10 s before the boiler's stop, or inside the hold, that stays
    or comes back at the dating moment, ``calculations`` calculations before it: the response
    dated back to the boiler's stop is the room's, within one calculation of the same path
    without the step, not a dead time of seconds."""
    dating = HOLD_ENDS if path == "hold ends" else OWN_STOP
    onset = STOP - 10.0 if where == "before the boiler's stop" else dating - 5.0 * calculations
    reference = (await _passive_dead_time(path, None, onset, calculations))[1]
    outcome, history = await _passive_dead_time(path, change, onset, calculations)
    assert outcome == "detected" and len(history) == 1, (outcome, history)
    assert history[0] == pytest.approx(reference[0], abs=ONE_CALCULATION_S), (history, reference)


async def test_the_raw_history_holds_only_the_sensor_s_readings() -> None:
    """The estimator fed at the command's application gets SmartPI's last raw reading: the raw
    history, which the learning windows read as quantised readings, holds no filtered value
    (Annex 58, 3.2, p. 12: one treatment for all the signals)."""
    with patch.object(cut, "_temp", lambda at: 19.0 if at < 900.0 else 19.1):
        handler = await _heated()
        algo = handler._thermostat.prop_algorithm
        p = _monotonic(1010.0)
        with p[0], p[1], p[2], p[3]:
            algo._committed_on_percent = 1.0
            await algo.on_command_receipt(make_receipt(None, 0.0, kind="update"))
    assert algo.dt_est.last_stop_time == 1010.0
    assert {round(v, 6) for _, v in algo.dt_est.tin_history} <= {19.0, 19.1}
