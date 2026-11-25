"""Tests of the anticipation of the measured slope on the P and I errors (heating and cooling)."""

from __future__ import annotations

import time as real_time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi import algo as algo_module
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.anticipation import (
    DeadbandGate,
    anticipated_error,
    correction_c,
    select_horizon,
)
from custom_components.vtherm_smartpi.smartpi.const import (
    ANTICIPATION_MAX_C,
    SmartPIPhase,
)
from custom_components.vtherm_smartpi.smartpi.measure_event import MODE_COOL, MODE_HEAT, SOURCE_PULSE

from helpers import force_smartpi_stable_mode

TARGET = 20.0
EXT = 5.0
STEP_S = 60.0


class _Clock:
    """Monotonic clock moved by hand."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    """Replace the time source of the algorithm by a clock moved by hand.

    ``_run`` restarts the clock at the same value for every instance, so that two
    instances fed the same script see the same times.
    """
    clk = _Clock()
    monkeypatch.setattr(
        algo_module,
        "time",
        SimpleNamespace(monotonic=clk.monotonic, time=real_time.time),
    )
    return clk


def _make(stable: bool = True, horizon_s: float | None = None) -> SmartPI:
    """A SmartPI; ``horizon_s`` gives it a HEAT response model of that emitter lag, hence that horizon."""
    smartpi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="anticipation",
    )
    if stable:
        force_smartpi_stable_mode(smartpi)
    if horizon_s is not None:
        _with_model(smartpi, horizon_s)
    return smartpi


def _with_model(smartpi: SmartPI, lag_s: float, mode: str = MODE_HEAT, split_ok: bool = True) -> SmartPI:
    """A response model of ``mode`` with the emitter lag ``lag_s`` (dead time a quarter of it when told apart)."""
    smartpi._start.model.add(
        mode, SOURCE_PULSE,
        {"theta_s": 0.25 * lag_s, "tau_e_s": 0.75 * lag_s, "s_s": lag_s, "split_ok": split_ok}, 0.0,
    )
    return smartpi


def _with_dead_times(smartpi: SmartPI, heat=None, heat_ok=False, cool=None, cool_ok=False) -> SmartPI:
    smartpi.dt_est.deadtime_heat_s = heat
    smartpi.dt_est.deadtime_heat_reliable = heat_ok
    smartpi.dt_est.deadtime_cool_s = cool
    smartpi.dt_est.deadtime_cool_reliable = cool_ok
    return smartpi


def _run(
    smartpi: SmartPI,
    clk: _Clock,
    temps: list[float],
    target: float = TARGET,
    mode=VThermHvacMode_HEAT,
) -> list[dict]:
    """Feed a scripted temperature sequence and return what each tick published."""
    clk.now = 10_000.0
    out = []
    for temp in temps:
        clk.now += STEP_S
        smartpi.calculate(
            target_temp=target,
            current_temp=temp,
            ext_current_temp=EXT,
            slope=None,
            hvac_mode=mode,
        )
        out.append(
            {
                "u_cmd": smartpi.u_cmd,
                "on_percent": smartpi.on_percent,
                "error": smartpi.error,
                "error_p": smartpi.error_p,
                "integral": smartpi.integral,
                "anticipation_c": smartpi.anticipation_c,
                "in_deadband": smartpi.in_deadband,
                "in_near_band": smartpi.in_near_band,
                "e_filt": smartpi.error_filtered,
            }
        )
    return out


def _rise(n: int, start: float = 19.0, per_step: float = 0.02) -> list[float]:
    return [round(start + per_step * i, 4) for i in range(n)]


# --- the correction in the regulation ------------------------------------------------------


def test_no_horizon_is_the_plain_behaviour(clock: _Clock) -> None:
    """Without any lag nor reliable dead time the horizon is 0: no correction, plain errors."""
    temps = _rise(40)
    smartpi = _make()
    assert smartpi.anticipation_horizon_in_use_s == 0.0
    rows = _run(smartpi, clock, temps)
    assert all(row["anticipation_c"] == 0.0 for row in rows)
    # the errors are the plain ones: target - measured temperature
    assert rows[-1]["error"] == pytest.approx(TARGET - temps[-1])
    assert rows[-1]["error_p"] == pytest.approx(TARGET - temps[-1])


def test_rising_temperature_lowers_the_error_and_the_command(clock: _Clock) -> None:
    """With a horizon and a rising room, the P and I errors shrink and so does the command."""
    temps = _rise(40)  # 0.02 degC per minute, from 19.0 to 19.78 degC
    plain_rows = _run(_make(), clock, temps)
    ant_rows = _run(_make(horizon_s=1800.0), clock, temps)

    last_plain, last_ant = plain_rows[-1], ant_rows[-1]
    # slope = 0.02 degC/min = 3.33e-4 degC/s, horizon 1800 s -> +0.6 degC, error 0.22 < c: no fade
    assert last_ant["anticipation_c"] == pytest.approx(0.6, abs=0.02)
    assert last_ant["error_p"] < last_plain["error_p"]
    assert last_ant["error_p"] == pytest.approx(
        last_plain["error_p"] - last_ant["anticipation_c"], abs=1e-6
    )
    assert last_ant["integral"] < last_plain["integral"]
    assert last_ant["u_cmd"] < last_plain["u_cmd"]


def test_falling_temperature_raises_the_error(clock: _Clock) -> None:
    temps = [round(19.0 - 0.02 * i, 4) for i in range(40)]
    plain = _run(_make(), clock, temps)
    ant = _run(_make(horizon_s=1800.0), clock, temps)
    assert ant[-1]["anticipation_c"] == pytest.approx(-0.6, abs=0.02)
    assert ant[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] - ant[-1]["anticipation_c"], abs=1e-6)
    assert ant[-1]["error_p"] > plain[-1]["error_p"]


def test_correction_is_clamped(clock: _Clock) -> None:
    """A steep slope with a long horizon never adds more than ANTICIPATION_MAX_C."""
    rows = _run(_make(horizon_s=7200.0), clock, _rise(30, per_step=0.1))  # 6 degC/h x 2 h = 12 degC
    assert rows[-1]["anticipation_c"] == pytest.approx(ANTICIPATION_MAX_C)
    assert max(abs(r["anticipation_c"]) for r in rows) <= ANTICIPATION_MAX_C + 1e-12
    # and the other sign (target just below the room, so that the room moves away from it)
    rows = _run(_make(horizon_s=7200.0), clock, [25.0 - 0.1 * i for i in range(30)], target=22.0)
    assert rows[-1]["anticipation_c"] == pytest.approx(-ANTICIPATION_MAX_C)


def test_too_little_history_gives_no_correction(clock: _Clock) -> None:
    """Fewer than 3 samples, or a span under half the window, give a null slope."""
    rows = _run(_make(horizon_s=1800.0), clock, _rise(2))
    assert rows[-1]["anticipation_c"] == 0.0
    # window 600 s: 4 ticks of 60 s span 180 s < 300 s
    rows = _run(_make(horizon_s=1800.0), clock, _rise(4, per_step=0.05))
    assert rows[-1]["anticipation_c"] == 0.0


def test_no_effect_in_the_bootstrap_phase(clock: _Clock) -> None:
    """In the HYSTERESIS phase the correction is 0 and the output is the same."""
    temps = _rise(40, start=15.0, per_step=0.05)
    plain = _make(stable=False)
    anticipating = _make(stable=False, horizon_s=1800.0)
    assert plain.phase == SmartPIPhase.HYSTERESIS
    plain_rows = _run(plain, clock, temps)
    ant_rows = _run(anticipating, clock, temps)
    assert plain_rows == ant_rows
    assert all(row["anticipation_c"] == 0.0 for row in ant_rows)


def test_deadband_and_near_band_use_the_measured_temperature(clock: _Clock) -> None:
    """The deadband inputs (error, filtered error, bands) do not see the correction."""
    temps = _rise(40, start=19.0, per_step=0.02)
    plain_rows = _run(_make(), clock, temps)
    ant_rows = _run(_make(horizon_s=1800.0), clock, temps)
    assert ant_rows[-1]["anticipation_c"] > 0.1
    for a, b in zip(plain_rows, ant_rows):
        assert a["error"] == b["error"]
        assert a["e_filt"] == b["e_filt"]
        assert a["in_deadband"] == b["in_deadband"]
        assert a["in_near_band"] == b["in_near_band"]


def _with_reliable_tau(smartpi: SmartPI) -> SmartPI:
    """Enough accepted loss measurements for the deadband to be in use."""
    smartpi.est.learn_ok_count_b = 11
    smartpi.est._b_hat_hist.extend([smartpi.est.b] * 5)
    return smartpi


def test_no_correction_while_settled_in_the_deadband(clock: _Clock) -> None:
    """A room settled at its setpoint is not anticipated: a small quick move of the sensor is not extrapolated."""
    temps = [TARGET] * 25 + [TARGET + 0.03] * 12 + [TARGET - 0.03] * 12
    plain_rows = _run(_with_reliable_tau(_make()), clock, temps)
    ant_rows = _run(_with_reliable_tau(_make(horizon_s=1800.0)), clock, temps)
    settled = [i for i in range(1, len(ant_rows)) if ant_rows[i - 1]["in_deadband"]]
    assert len(settled) > 40
    for i in settled:
        assert ant_rows[i]["anticipation_c"] == 0.0
        assert ant_rows[i]["error_p"] == plain_rows[i]["error_p"]
        assert ant_rows[i]["u_cmd"] == plain_rows[i]["u_cmd"]


def test_correction_resumes_when_the_room_leaves_the_deadband(clock: _Clock) -> None:
    """Outside the deadband the same slope is anticipated again."""
    temps = [TARGET] * 25 + [round(TARGET + 0.03 * i, 4) for i in range(1, 16)]
    rows = _run(_with_reliable_tau(_make(horizon_s=1800.0)), clock, temps)
    assert rows[24]["in_deadband"] and rows[25]["anticipation_c"] == 0.0
    assert not rows[-2]["in_deadband"]
    assert rows[-1]["anticipation_c"] > 0.1


def test_correction_is_kept_at_the_deadband_entry_until_it_dies_out(clock: _Clock) -> None:
    """A room arriving at its setpoint keeps its braking inside the deadband; once it has died out it stays off."""
    rise = [round(TARGET - 0.60 + 0.02 * i, 4) for i in range(30)]
    temps = rise + [rise[-1]] * 40 + [rise[-1] + 0.02] * 10
    rows = _run(_with_reliable_tau(_make(horizon_s=1800.0)), clock, temps)
    entry = next(i for i in range(1, len(rows)) if rows[i - 1]["in_deadband"])
    assert rows[entry - 1]["anticipation_c"] > 0.0
    assert rows[entry]["anticipation_c"] > 0.0
    inside = [i for i in range(entry, len(rows)) if rows[i - 1]["in_deadband"]]
    assert inside == list(range(entry, len(rows)))
    off = next(i for i in inside if rows[i]["anticipation_c"] == 0.0)
    assert off > entry
    assert all(rows[i]["anticipation_c"] > 0.0 for i in range(entry, off))
    assert all(rows[i]["anticipation_c"] == 0.0 for i in range(off, len(rows)))


def _make_cool(stable: bool = True, horizon_s: float | None = None) -> SmartPI:
    """A SmartPI whose active COOL dead time (the cooling one) gives that horizon."""
    smartpi = _make(stable=False)
    smartpi.est.ensure_hvac_mode(VThermHvacMode_COOL)
    smartpi.dt_est.ensure_hvac_mode(VThermHvacMode_COOL)
    if stable:
        force_smartpi_stable_mode(smartpi)
        smartpi.est.a_meas_hist = type(smartpi.est.a_meas_hist)(-abs(v) for v in smartpi.est.a_meas_hist)  # a signed model: negative in cooling
        smartpi.est.a = -abs(smartpi.est.a)
    if horizon_s is not None:
        _with_dead_times(smartpi, cool=horizon_s, cool_ok=True)
    return smartpi


def _fall(n: int, start: float = 21.0, per_step: float = 0.02) -> list[float]:
    return [round(start - per_step * i, 4) for i in range(n)]


def test_cool_without_horizon_is_the_plain_behaviour(clock: _Clock) -> None:
    temps = _fall(40)
    smartpi = _make_cool()
    rows = _run(smartpi, clock, temps, mode=VThermHvacMode_COOL)
    assert all(row["anticipation_c"] == 0.0 for row in rows)
    # the cooling errors are signed by the mode: measured - target
    assert rows[-1]["error"] == pytest.approx(temps[-1] - TARGET)
    assert rows[-1]["error_p"] == pytest.approx(temps[-1] - TARGET)


def test_cool_falling_temperature_lowers_the_error_and_the_command(clock: _Clock) -> None:
    """With a horizon and a room cooling toward the setpoint, the P and I errors shrink and so does the command."""
    temps = _fall(40)  # 0.02 degC per minute, from 21.0 to 20.22 degC
    plain = _run(_make_cool(), clock, temps, mode=VThermHvacMode_COOL)
    ant = _run(_make_cool(horizon_s=1800.0), clock, temps, mode=VThermHvacMode_COOL)
    # slope -3.33e-4 degC/s, horizon 1800 s -> -0.6 degC, error -0.22 within c: no fade
    assert ant[-1]["anticipation_c"] == pytest.approx(-0.6, abs=0.02)
    # the cooling error is measured - target: the anticipated temperature lowers it by |c|
    assert ant[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] + ant[-1]["anticipation_c"], abs=1e-6)
    assert ant[-1]["error_p"] < plain[-1]["error_p"]
    assert ant[-1]["integral"] < plain[-1]["integral"]
    assert ant[-1]["u_cmd"] < plain[-1]["u_cmd"]


def test_cool_rising_temperature_raises_the_error(clock: _Clock) -> None:
    temps = _rise(40, start=21.0)  # moving away from a setpoint below the room
    plain = _run(_make_cool(), clock, temps, mode=VThermHvacMode_COOL)
    ant = _run(_make_cool(horizon_s=1800.0), clock, temps, mode=VThermHvacMode_COOL)
    assert ant[-1]["anticipation_c"] == pytest.approx(0.6, abs=0.02)
    assert ant[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] + ant[-1]["anticipation_c"], abs=1e-6)
    assert ant[-1]["error_p"] > plain[-1]["error_p"]


def test_cool_is_the_mirror_of_heat(clock: _Clock) -> None:
    """The room mirrored about the setpoint gives the opposite correction and the same mode-signed errors."""
    heat_temps = _rise(40)
    cool_temps = [round(2 * TARGET - t, 4) for t in heat_temps]
    heat = _run(_make(horizon_s=1800.0), clock, heat_temps)
    cool = _run(_make_cool(horizon_s=1800.0), clock, cool_temps, mode=VThermHvacMode_COOL)
    for h, c in zip(heat, cool):
        assert c["anticipation_c"] == pytest.approx(-h["anticipation_c"], abs=1e-9)
        assert c["error"] == pytest.approx(h["error"], abs=1e-9)
        assert c["error_p"] == pytest.approx(h["error_p"], abs=1e-9)


def test_cool_correction_is_clamped(clock: _Clock) -> None:
    rows = _run(_make_cool(horizon_s=7200.0), clock, _fall(30, per_step=0.1), mode=VThermHvacMode_COOL)
    assert rows[-1]["anticipation_c"] == pytest.approx(-ANTICIPATION_MAX_C)
    rows = _run(_make_cool(horizon_s=7200.0), clock, _rise(30, start=18.0, per_step=0.1), target=21.0,
                mode=VThermHvacMode_COOL)
    assert rows[-1]["anticipation_c"] == pytest.approx(ANTICIPATION_MAX_C)


def test_cool_no_effect_in_the_bootstrap_phase(clock: _Clock) -> None:
    temps = _fall(40, start=25.0, per_step=0.05)
    plain_rows = _run(_make_cool(stable=False), clock, temps, mode=VThermHvacMode_COOL)
    ant_rows = _run(_make_cool(stable=False, horizon_s=1800.0), clock, temps, mode=VThermHvacMode_COOL)
    assert plain_rows == ant_rows
    assert all(row["anticipation_c"] == 0.0 for row in ant_rows)


def test_cool_deadband_and_near_band_use_the_measured_temperature(clock: _Clock) -> None:
    temps = _fall(40)
    plain = _run(_make_cool(), clock, temps, mode=VThermHvacMode_COOL)
    ant = _run(_make_cool(horizon_s=1800.0), clock, temps, mode=VThermHvacMode_COOL)
    assert ant[-1]["anticipation_c"] < -0.1
    for a, b in zip(plain, ant):
        assert a["error"] == b["error"]
        assert a["e_filt"] == b["e_filt"]
        assert a["in_deadband"] == b["in_deadband"]
        assert a["in_near_band"] == b["in_near_band"]


@pytest.mark.parametrize(
    ("mode", "error", "expected"),
    [
        (VThermHvacMode_HEAT, 0.5, 0.2),  # target - (measured + c) = 0.5 - 0.3
        (VThermHvacMode_HEAT, -0.5, -0.8),
        (VThermHvacMode_COOL, 0.5, 0.8),  # (measured + c) - target = 0.5 + 0.3
        (VThermHvacMode_COOL, -0.5, -0.2),
    ],
)
def test_anticipated_error_is_signed_by_the_mode(mode, error, expected) -> None:
    assert anticipated_error(error, 0.3, mode) == pytest.approx(expected)
    assert anticipated_error(error, 0.0, mode) == error


def test_manage_setpoint_signs_the_anticipated_errors_by_the_mode() -> None:
    """Errors are computed on the measured temperature plus the correction, with the sign of the mode."""
    smartpi = _make()
    _, error_i, error_p, _ = smartpi._manage_setpoint(20.0, 19.5, VThermHvacMode_HEAT, 0.3)
    assert error_i == pytest.approx(0.5)
    assert error_p == pytest.approx(20.0 - (19.5 + 0.3))
    smartpi = _make()
    _, error_i, error_p, _ = smartpi._manage_setpoint(20.0, 20.5, VThermHvacMode_COOL, -0.3)
    assert error_i == pytest.approx(0.5)
    assert error_p == pytest.approx((20.5 - 0.3) - 20.0)


def test_published_anticipated_temperature_is_the_measured_one_plus_the_correction(clock: _Clock) -> None:
    smartpi = _make(horizon_s=1800.0)
    temps = _rise(40)
    _run(smartpi, clock, temps)
    assert smartpi.anticipation_c > 0.1
    temperature = smartpi.get_published_diagnostics()["temperature"]
    assert temperature["indoor"] == temps[-1]
    assert temperature["anticipated"] == round(temps[-1] + smartpi.anticipation_c, 2)

    plain = _make()
    _run(plain, clock, temps)
    assert plain.get_published_diagnostics()["temperature"]["anticipated"] == round(temps[-1], 2)


def test_published_anticipated_temperature_is_null_without_a_measure() -> None:
    assert _make().get_published_diagnostics()["temperature"]["anticipated"] is None


def test_reset_learning_clears_the_applied_correction(clock: _Clock) -> None:
    smartpi = _make(horizon_s=1800.0)
    _run(smartpi, clock, _rise(40))
    assert smartpi.anticipation_c != 0.0
    smartpi.reset_learning()
    assert smartpi.anticipation_c == 0.0


# --- fade-in near the setpoint -------------------------------------------------------------


def _pair(clock: _Clock, temps: list[float], target: float):
    """Run the same script without and with a horizon."""
    return (
        _run(_make(), clock, temps, target),
        _run(_make(horizon_s=1800.0), clock, temps, target),
    )


def test_fade_far_from_the_setpoint_while_rising_is_without_anticipation(clock: _Clock) -> None:
    """e > 2c while rising: the output is the one without anticipation."""
    temps = _rise(40)  # c = 0.6, e = 25 - 19.78 = 5.2 > 1.2
    plain, faded = _pair(clock, temps, 25.0)
    assert all(row["anticipation_c"] == 0.0 for row in faded)
    assert faded == plain


def test_fade_at_one_and_a_half_times_c_applies_half(clock: _Clock) -> None:
    temps = _rise(40)
    c = _pair(clock, temps, TARGET)[1][-1]["anticipation_c"]
    assert c == pytest.approx(0.6, abs=0.02)
    target = temps[-1] + 1.5 * c  # e = 1.5 c
    plain, faded = _pair(clock, temps, target)
    assert faded[-1]["anticipation_c"] == pytest.approx(0.5 * c)
    assert faded[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] - 0.5 * c, abs=1e-6)
    assert faded[-1]["u_cmd"] < plain[-1]["u_cmd"]


def test_fade_within_c_of_the_setpoint_and_beyond_it_applies_all(clock: _Clock) -> None:
    temps = _rise(40)
    c = _pair(clock, temps, TARGET)[1][-1]["anticipation_c"]
    for target in (temps[-1] + c, temps[-1] + 0.5 * c, temps[-1], temps[-1] - 0.4):
        plain, faded = _pair(clock, temps, target)
        assert faded[-1]["anticipation_c"] == pytest.approx(c)
        assert faded[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] - c, abs=1e-6)


def test_fade_moving_away_from_the_setpoint_applies_all(clock: _Clock) -> None:
    """Below the setpoint and falling: error and slope have opposite signs, w = 1."""
    temps = [round(19.0 - 0.02 * i, 4) for i in range(40)]
    _, faded = _pair(clock, temps, TARGET)
    assert faded[-1]["anticipation_c"] == pytest.approx(-0.6, abs=0.02)
    # above the setpoint and rising: also moving away
    _, faded = _pair(clock, _rise(40, start=21.0), TARGET)
    assert faded[-1]["anticipation_c"] > 0.0


def _pair_cool(clock: _Clock, temps: list[float], target: float):
    return (
        _run(_make_cool(), clock, temps, target, mode=VThermHvacMode_COOL),
        _run(_make_cool(horizon_s=1800.0), clock, temps, target, mode=VThermHvacMode_COOL),
    )


def test_cool_fade_far_from_the_setpoint_while_approaching_is_without_anticipation(clock: _Clock) -> None:
    """Falling toward a setpoint further than 2|c|: the output is the one without anticipation."""
    plain, faded = _pair_cool(clock, _fall(40), 15.0)  # c = -0.6, e = 15 - 20.22 = -5.2
    assert all(row["anticipation_c"] == 0.0 for row in faded)
    assert faded == plain


def test_cool_fade_at_one_and_a_half_times_c_applies_half(clock: _Clock) -> None:
    temps = _fall(40)
    c = _pair_cool(clock, temps, TARGET)[1][-1]["anticipation_c"]
    assert c == pytest.approx(-0.6, abs=0.02)
    plain, faded = _pair_cool(clock, temps, temps[-1] + 1.5 * c)  # e = 1.5 c
    assert faded[-1]["anticipation_c"] == pytest.approx(0.5 * c)
    assert faded[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] + 0.5 * c, abs=1e-6)
    assert faded[-1]["u_cmd"] < plain[-1]["u_cmd"]


def test_cool_fade_within_c_of_the_setpoint_and_beyond_it_applies_all(clock: _Clock) -> None:
    temps = _fall(40)
    c = _pair_cool(clock, temps, TARGET)[1][-1]["anticipation_c"]
    for target in (temps[-1] + c, temps[-1] + 0.5 * c, temps[-1], temps[-1] + 0.4):
        plain, faded = _pair_cool(clock, temps, target)
        assert faded[-1]["anticipation_c"] == pytest.approx(c)
        assert faded[-1]["error_p"] == pytest.approx(plain[-1]["error_p"] + c, abs=1e-6)


def test_cool_fade_moving_away_from_the_setpoint_applies_all(clock: _Clock) -> None:
    """Below the setpoint and falling, or above it and rising: error and slope are opposed, w = 1."""
    _, faded = _pair_cool(clock, _fall(40), 22.0)
    assert faded[-1]["anticipation_c"] == pytest.approx(-0.6, abs=0.02)
    _, faded = _pair_cool(clock, _rise(40, start=21.0), TARGET)
    assert faded[-1]["anticipation_c"] == pytest.approx(0.6, abs=0.02)


# --- horizon -------------------------------------------------------------------------------


def test_horizon_is_the_emitter_lag_of_the_response_model() -> None:
    smartpi = _with_dead_times(_make(), heat=420.0, heat_ok=True, cool=1800.0, cool_ok=True)
    smartpi._start.reading._kept["heat"] = {"mode": "heat", "a_per_min": 0.02, "lag_s": 2000.0, "at_s": 0.0}
    smartpi._start.mode = "direct"
    _with_model(smartpi, 1600.0)
    horizon = smartpi.anticipation_horizon(VThermHvacMode_HEAT)
    assert (horizon.seconds, horizon.source) == (pytest.approx(1600.0), "response model")


def test_horizon_with_an_assumed_split_is_the_emitter_lag() -> None:
    """The split is not told apart: dead time S / 2 and the horizon the whole emitter lag S (its cautious end)."""
    smartpi = _with_model(_make(), 2400.0, split_ok=False)
    values = smartpi.learn_win.model(VThermHvacMode_HEAT)
    assert values["confidence"] == "dead time assumed" and values["theta_s"] == 1200.0
    horizon = smartpi.anticipation_horizon(VThermHvacMode_HEAT)
    assert (horizon.seconds, horizon.source) == (2400.0, "response model")


def test_horizon_without_a_model_is_the_pulse_lag_then_the_longer_of_the_delays() -> None:
    smartpi = _with_dead_times(_make(), heat=420.0, heat_ok=True, cool=1800.0, cool_ok=True)
    assert smartpi.anticipation_horizon_in_use_s == 900.0  # half the cooling dead time
    assert smartpi.anticipation_horizon().source == "dead times"
    smartpi.dt_est.deadtime_heat_s = 1200.0
    assert smartpi.anticipation_horizon_in_use_s == 1200.0
    smartpi._start.reading._kept["heat"] = {"mode": "heat", "a_per_min": 0.02, "lag_s": 2000.0, "at_s": 0.0}
    smartpi._start.mode = "direct"
    horizon = smartpi.anticipation_horizon(VThermHvacMode_HEAT)
    assert (horizon.seconds, horizon.source) == (2000.0, "measurement pulses")


def test_horizon_without_a_model_nor_a_reliable_delay_is_zero() -> None:
    smartpi = _with_dead_times(_make(), heat=420.0, heat_ok=False, cool=1800.0, cool_ok=False)
    assert smartpi.anticipation_horizon_in_use_s == 0.0
    assert smartpi.anticipation_horizon().source == "none"


def test_cool_horizon_reads_the_cool_model_else_the_dead_times() -> None:
    smartpi = _with_dead_times(_make(), heat=1800.0, heat_ok=True, cool=420.0, cool_ok=True)
    horizon = smartpi.anticipation_horizon(VThermHvacMode_COOL)
    assert (horizon.seconds, horizon.source) == (900.0, "dead times")
    smartpi.dt_est.deadtime_cool_s = 1200.0
    assert smartpi.anticipation_horizon(VThermHvacMode_COOL).seconds == 1200.0
    _with_model(smartpi, 1600.0)  # a HEAT model does not serve the cooling
    assert smartpi.anticipation_horizon(VThermHvacMode_COOL).source == "dead times"
    assert smartpi.anticipation_horizon(VThermHvacMode_HEAT).source == "response model"
    _with_model(smartpi, 700.0, MODE_COOL)
    horizon = smartpi.anticipation_horizon(VThermHvacMode_COOL)
    assert (horizon.seconds, horizon.source) == (pytest.approx(700.0), "response model")
    smartpi._start.model.reset()
    _with_dead_times(smartpi, heat=1800.0, heat_ok=False, cool=420.0, cool_ok=False)
    horizon = smartpi.anticipation_horizon(VThermHvacMode_COOL)
    assert (horizon.seconds, horizon.source) == (0.0, "none")


def test_horizon_follows_the_mode_of_the_last_calculation(clock: _Clock) -> None:
    smartpi = _with_dead_times(_make_cool(), heat=300.0, heat_ok=True, cool=1000.0, cool_ok=True)
    assert smartpi.anticipation_horizon_in_use_s == 500.0  # no mode yet: half the passive cooling one of HEAT
    _run(smartpi, clock, _fall(5), mode=VThermHvacMode_COOL)
    assert smartpi.anticipation_horizon_in_use_s == 1000.0  # active COOL (cooling) dead time


def test_cool_horizon_and_correction_are_published(clock: _Clock) -> None:
    smartpi = _make_cool(horizon_s=900.0)
    _run(smartpi, clock, _fall(40), mode=VThermHvacMode_COOL)
    control = smartpi.get_published_diagnostics()["analysis"]["control"]
    assert control["anticipation_horizon_s"] == 900.0
    assert control["anticipation_horizon_source"] == "dead times"
    assert control["anticipation_c"] == pytest.approx(smartpi.anticipation_c, abs=1e-4)
    assert control["anticipation_c"] < 0.0


def test_horizon_is_published(clock: _Clock) -> None:
    smartpi = _with_dead_times(_make(), cool=900.0, cool_ok=True)
    _run(smartpi, clock, _rise(40))
    control = smartpi.get_published_diagnostics()["analysis"]["control"]
    assert control["anticipation_horizon_s"] == 450.0
    assert control["anticipation_horizon_source"] == "dead times"
    _with_model(smartpi, 600.0)
    control = smartpi.get_published_diagnostics()["analysis"]["control"]
    assert control["anticipation_horizon_s"] == 600.0
    assert control["anticipation_horizon_source"] == "response model"
    assert control["anticipation_c"] == pytest.approx(smartpi.anticipation_c, abs=1e-4)


def test_no_horizon_is_published_as_none() -> None:
    control = _make().get_published_diagnostics()["analysis"]["control"]
    assert control["anticipation_c"] == 0.0
    assert control["anticipation_horizon_s"] == 0.0
    assert control["anticipation_horizon_source"] == "none"


# --- select_horizon ------------------------------------------------------------------------


def _dt_est(heat=None, heat_ok=False, cool=None, cool_ok=False):
    return _with_dead_times(_make(), heat, heat_ok, cool, cool_ok).dt_est


def _horizon(pulse_lag, mode=VThermHvacMode_HEAT, model=None, **kwargs):
    dt_est = _dt_est(**kwargs)
    return select_horizon(model, lambda: dt_est, mode, pulse_lag)


@pytest.mark.parametrize(
    "heat, heat_ok, cool, cool_ok, expected",
    [
        (420.0, True, 1800.0, True, 900.0),  # half the cooling one is longer
        (1200.0, True, 1800.0, True, 1200.0),  # the HEAT one is longer
        (420.0, True, 1800.0, False, 420.0),  # only HEAT reliable
        (420.0, False, 1800.0, True, 900.0),  # only cooling reliable
        (420.0, False, 1800.0, False, 0.0),  # none reliable
        (0.0, True, None, True, 0.0),  # reliable but not positive
        (420.0, True, 0.0, True, 420.0),  # a null cooling dead time does not count
        (-5.0, True, -5.0, True, 0.0),
    ],
)
def test_select_horizon_from_the_dead_times(heat, heat_ok, cool, cool_ok, expected) -> None:
    for lag in (None, 0.0, -10.0):
        horizon = _horizon(lag, heat=heat, heat_ok=heat_ok, cool=cool, cool_ok=cool_ok)
        assert horizon.seconds == expected
        assert horizon.source == ("dead times" if expected > 0.0 else "none")


@pytest.mark.parametrize(
    "cool, cool_ok, heat, heat_ok, expected",
    [
        (420.0, True, 1800.0, True, 900.0),  # half the passive (heating) one is longer
        (1200.0, True, 1800.0, True, 1200.0),  # the active (cooling) one is longer
        (420.0, True, 1800.0, False, 420.0),  # only the active one reliable
        (420.0, False, 1800.0, True, 900.0),  # only the passive one reliable
        (420.0, False, 1800.0, False, 0.0),  # none reliable
        (0.0, True, None, True, 0.0),  # reliable but not positive
        (420.0, True, 0.0, True, 420.0),  # a null passive dead time does not count
        (-5.0, True, -5.0, True, 0.0),
    ],
)
def test_select_horizon_in_cool_from_the_dead_times(cool, cool_ok, heat, heat_ok, expected) -> None:
    for lag in (None, 0.0, -10.0):
        horizon = _horizon(lag, VThermHvacMode_COOL, heat=heat, heat_ok=heat_ok, cool=cool, cool_ok=cool_ok)
        assert horizon.seconds == expected
        assert horizon.source == ("dead times" if expected > 0.0 else "none")


@pytest.mark.parametrize("mode", [VThermHvacMode_HEAT, VThermHvacMode_COOL])
def test_select_horizon_order_model_then_pulse_lag_then_dead_times(mode) -> None:
    model = {"theta_s": 300.0, "tau_e_s": 1500.0}
    horizon = _horizon(650.0, mode, model, heat=420.0, heat_ok=True, cool=420.0, cool_ok=True)
    assert (horizon.seconds, horizon.source) == (1800.0, "response model")
    horizon = _horizon(650.0, mode, None, heat=420.0, heat_ok=True, cool=420.0, cool_ok=True)
    assert (horizon.seconds, horizon.source) == (650.0, "measurement pulses")
    horizon = _horizon(None, mode, {"theta_s": 0.0, "tau_e_s": 0.0}, heat=420.0, heat_ok=True, cool=420.0, cool_ok=True)
    assert (horizon.seconds, horizon.source) == (420.0, "dead times")   # a null emitter lag does not count


def test_select_horizon_reads_the_dead_times_only_when_nothing_before_decides() -> None:
    calls = []
    dt_est = _dt_est(heat=420.0, heat_ok=True)

    def view():
        calls.append(1)
        return dt_est

    assert select_horizon({"theta_s": 100.0, "tau_e_s": 550.0}, view, VThermHvacMode_HEAT).seconds == 650.0
    assert select_horizon(None, view, VThermHvacMode_HEAT, 500.0).seconds == 500.0
    assert calls == []
    assert select_horizon(None, view, VThermHvacMode_HEAT).seconds == 420.0
    assert calls == [1]


# --- correction_c --------------------------------------------------------------------------


def _history(now: float, slope_per_s: float, span_s: float, step_s: float = 60.0, base: float = 19.0):
    """Samples every step_s up to now (excluded), at base + slope * (t - first)."""
    n = int(span_s // step_s)
    return [(now - (n - i) * step_s, base + slope_per_s * i * step_s) for i in range(n)]


def test_correction_is_slope_times_horizon() -> None:
    now = 10_000.0
    slope = 0.02 / 60.0
    history = _history(now, slope, 1800.0)
    current = history[-1][1] + slope * 60.0
    c = correction_c(1800.0, now, current, None, history, True)
    assert c == pytest.approx(slope * 1800.0, rel=1e-9)


def _least_squares_slope(points) -> float:
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / sum((t - mean_t) ** 2 for t, _ in points)


@pytest.mark.parametrize(
    "horizon, window",
    [(300.0, 600.0), (1200.0, 1200.0), (3600.0, 1800.0)],
)
def test_correction_slope_window_is_the_horizon_bounded_to_10_and_30_minutes(horizon, window) -> None:
    """The slope window is the horizon bounded to [600 s, 1800 s]; older samples are ignored."""
    now = 20_000.0
    rate = 2e-4  # degC/s of the ramp, which starts in the middle of the window
    kink = now - window / 2.0
    history = [
        (t, 19.0 + rate * max(0.0, t - kink))
        for t in (now - window - 1800.0 + 60.0 * i for i in range(int((window + 1800.0) // 60.0)))
    ]
    current = 19.0 + rate * (now - kink)
    expected_slope = _least_squares_slope([(t, v) for t, v in history if t >= now - window] + [(now, current)])
    c = correction_c(horizon, now, current, None, history, True)
    assert c == pytest.approx(expected_slope * horizon, rel=1e-9)
    assert abs(c) < ANTICIPATION_MAX_C
    # a window one third shorter or a third longer would read another slope
    for other in (window * 2.0 / 3.0, window * 4.0 / 3.0):
        other_slope = _least_squares_slope([(t, v) for t, v in history if t >= now - other] + [(now, current)])
        assert abs(other_slope - expected_slope) > 0.05 * expected_slope


def test_correction_is_clamped_to_the_limit() -> None:
    now = 10_000.0
    up = _history(now, 0.5 / 60.0, 2400.0)
    assert correction_c(7200.0, now, up[-1][1] + 0.5, None, up, True) == ANTICIPATION_MAX_C
    down = _history(now, -0.5 / 60.0, 2400.0)
    assert correction_c(7200.0, now, down[-1][1] - 0.5, None, down, True) == -ANTICIPATION_MAX_C


@pytest.mark.parametrize("horizon", [0.0, -1.0])
def test_correction_is_zero_without_horizon(horizon) -> None:
    now = 10_000.0
    history = _history(now, 0.02 / 60.0, 600.0)
    assert correction_c(horizon, now, 19.2, None, history, True) == 0.0


def test_correction_is_zero_when_not_active() -> None:
    now = 10_000.0
    history = _history(now, 0.02 / 60.0, 600.0)
    assert correction_c(1800.0, now, 19.2, None, history, False) == 0.0


def test_gate_passes_the_correction_outside_the_deadband() -> None:
    gate = DeadbandGate()
    for correction in (0.3, 0.0, -0.2, 0.1):
        assert gate.apply(correction, False) == correction
        assert gate.value == correction


def test_gate_keeps_the_correction_at_the_deadband_entry_until_it_is_zero() -> None:
    gate = DeadbandGate()
    assert gate.apply(0.30, False) == 0.30
    assert gate.apply(0.25, True) == 0.25
    assert gate.apply(0.10, True) == 0.10
    assert gate.apply(0.0, True) == 0.0
    assert gate.apply(0.08, True) == 0.0
    assert gate.apply(-0.08, True) == 0.0
    assert gate.value == 0.0


def test_gate_ends_the_correction_when_its_sign_changes() -> None:
    gate = DeadbandGate()
    gate.apply(0.20, False)
    assert gate.apply(0.05, True) == 0.05
    assert gate.apply(-0.04, True) == 0.0
    assert gate.apply(-0.06, True) == 0.0
    assert gate.apply(0.06, True) == 0.0


def test_gate_starts_again_when_the_room_leaves_the_deadband() -> None:
    gate = DeadbandGate()
    assert gate.apply(0.0, True) == 0.0
    assert gate.apply(0.2, True) == 0.0
    assert gate.apply(0.2, False) == 0.2
    assert gate.apply(0.15, True) == 0.15


def test_gate_reset_forgets_the_settled_mark() -> None:
    gate = DeadbandGate()
    gate.apply(0.0, True)
    assert gate.apply(0.1, True) == 0.0
    gate.reset()
    assert gate.value == 0.0
    assert gate.apply(0.1, True) == 0.1


def test_correction_needs_three_samples_spanning_half_the_window() -> None:
    now = 10_000.0
    slope = 0.02 / 60.0
    # the current tick makes the third sample; they span 120 s < 300 s
    short = _history(now, slope, 120.0)
    assert correction_c(600.0, now, 19.1, None, short, True) == 0.0
    # two samples in all
    assert correction_c(600.0, now, 19.1, None, short[:1], True) == 0.0
    # enough samples, span of 360 s >= 300 s
    ok = _history(now, slope, 360.0)
    assert correction_c(600.0, now, ok[-1][1] + slope * 60.0, None, ok, True) != 0.0


def test_correction_counts_the_current_tick_once() -> None:
    now = 10_000.0
    slope = 0.02 / 60.0
    history = _history(now, slope, 1800.0) + [(now, 19.0 + slope * 1800.0)]
    without = correction_c(1800.0, now, history[-1][1], None, history, True)
    assert without == pytest.approx(slope * 1800.0, rel=1e-9)


def test_fade_in_weight() -> None:
    now = 10_000.0
    slope = 0.02 / 60.0
    history = _history(now, slope, 1800.0)
    current = history[-1][1] + slope * 60.0
    c = slope * 1800.0
    # error e = target - measured
    for e, weight in ((5.0 * c, 0.0), (2.0 * c, 0.0), (1.5 * c, 0.5), (c, 1.0), (0.2 * c, 1.0), (-1.0, 1.0)):
        assert correction_c(1800.0, now, current, current + e, history, True) == pytest.approx(weight * c)
    # moving away from the setpoint: error and correction have opposite signs
    assert correction_c(1800.0, now, current, current - 3.0, history, True) == pytest.approx(c)


def test_fade_in_of_a_null_slope_stays_null() -> None:
    now = 10_000.0
    flat = _history(now, 0.0, 600.0)
    assert correction_c(1800.0, now, 19.0, 20.0, flat, True) == 0.0
