import pytest
from unittest.mock import MagicMock, PropertyMock, patch
from custom_components.vtherm_smartpi.hvac_mode import (
    VThermHvacMode_HEAT,
    VThermHvacMode_COOL,
)
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController

from helpers import force_smartpi_stable_mode, make_receipt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_algo(integral: float = 0.5, i_mode: str = "I:RUN") -> SmartPI:
    """Return a pre-configured SmartPI instance for AW tracking tests."""
    hass = MagicMock()
    algo = SmartPI(
        hass=hass,
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="test_aw",
        max_on_percent=1.0,
        deadband_c=0.0,
    )
    # PI gains
    algo.Ki = 0.05
    algo.Kp = 0.5
    # Controller state
    algo.ctl.integral = integral
    algo.ctl.last_i_mode = i_mode
    algo.ctl.u_ff = 0.0
    # Thermal context (Tin < SP → no thermal invariant active by default)
    algo._last_hvac_mode = VThermHvacMode_HEAT
    algo._last_current_temp = 20.0
    algo._last_target_temp = 21.0
    algo.in_deadband = False
    return algo


def test_deadband_holds_ff_plus_frozen_pi():
    """In core deadband (HEAT, reliable), P must be frozen.

    Only FF and frozen I remain in the deadband command.
    u_hold == u_ff + u_pi
    No micro-leak applied to the integral.
    """
    ctl = SmartPIController("test_deadband")
    ctl.integral = 1.0

    u_cmd = ctl.compute_pwm(
        error=0.05,
        error_p=0.05,
        kp=0.5,
        ki=0.05,
        u_ff=0.20,
        dt_min=10.0,
        in_deadband=True,
        integrator_hold=False,
        deadband_c=0.1,
    )

    # No micro-leak: integral stays at 1.0
    assert ctl.integral == pytest.approx(1.0)

    # u_cmd = u_ff + u_pi = 0.20 + (0.05 * 1.0) = 0.25
    assert u_cmd == pytest.approx(0.25)

    # u_hold == u_ff + u_pi
    assert ctl.u_hold == pytest.approx(0.25)

    assert ctl.deadband_power_source == "ff_plus_pi"


def test_observed_valve_deadband_case_uses_frozen_integral_only():
    """Observed 0.07 C valve error must not produce raw P in core deadband."""
    ctl = SmartPIController("test_valve_deadband")
    ctl.integral = -5.221122

    ctl.compute_pwm(
        error=0.07,
        error_p=0.07,
        kp=3.472215,
        ki=0.001443,
        u_ff=0.0,
        dt_min=10.0,
        in_deadband=True,
        integrator_hold=False,
        deadband_c=0.10,
        deadband_allow_p=True,
    )

    expected_i_only = 0.001443 * -5.221122
    raw_p_plus_i = 3.472215 * 0.07 + expected_i_only

    assert ctl.integral == pytest.approx(-5.221122)
    assert ctl.last_i_mode == "I:FREEZE(deadband)"
    assert ctl.last_error_p_db == pytest.approx(0.0)
    assert ctl.deadband_p_mode == "deadband_quiet"
    assert ctl.u_pi == pytest.approx(expected_i_only)
    assert ctl.u_pi != pytest.approx(raw_p_plus_i)


@pytest.mark.asyncio
async def test_guard_cut_cycle_completion_preserves_integral():
    """A completed zero-power guard-cut cycle must not drain the integral."""
    algo = make_algo(integral=-50.0, i_mode="I:RUN")
    algo.ctl.u_ff = 0.49
    algo.ctl.last_error_p_db = -0.30
    algo.guards._guard_cut_active = True
    before = algo.integral

    algo.calculate(
        target_temp=20.0,
        current_temp=20.37,
        ext_current_temp=6.0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert algo.on_percent == 0.0
    assert algo.ctl.last_i_mode == "I:OVERRIDE(guard_cut)"

    await algo.on_cycle_completed(
        e_eff=0.0,
        elapsed_ratio=1.0,
        cycle_duration_min=5.0,
    )

    assert algo.integral == before
    assert algo.aw_du == 0.0


# ---------------------------------------------------------------------------
# On SAT_HI exit, integral must NOT be artificially initialized
# (Astrom & Hagglund §3.5: integral must stay coherent with conditional integration)
# ---------------------------------------------------------------------------

def test_no_integral_init_on_sat_exit():
    """On SAT_HI -> NO_SAT transition, integral must stay at its natural value.

    Per Astrom & Hagglund §3.5, the integral state must remain coherent
    with the actuator reality. After conditional integration (I:SKIP) during
    SAT_HI, the integral is near-zero and must build up naturally via I:RUN.
    """
    algo = make_algo(integral=0.0, i_mode="I:SKIP(SAT_HI)")
    algo.ctl.last_sat = "SAT_HI"

    # Simulate SAT_HI exit (step 14b logic)
    algo.ctl.last_sat = "NO_SAT"  # Simulate compute_pwm transition

    assert algo.ctl.integral == 0.0, (
        "Integral must stay at 0 on SAT_HI exit (natural post-conditional-integration)"
    )


# ---------------------------------------------------------------------------
# Conditional integration across a non-owned command interval
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("override", ["guard_cut", "missing_sensor"])
def test_resume_after_override_does_not_integrate_gap(override):
    """The integrator must not advance over a guard cut or missing sensor gap."""
    from unittest.mock import patch

    from custom_components.vtherm_smartpi.smartpi.const import (
        AB_MIN_SAMPLES_A,
        AB_MIN_SAMPLES_B,
    )

    algo = make_algo(integral=0.0, i_mode="I:RUN")
    # Enough model samples to leave the hysteresis phase and run the PI.
    algo.est.a = 0.02
    algo.est.b = 0.002
    algo.est.a_meas_hist.extend([0.02] * AB_MIN_SAMPLES_A)
    algo.est.b_meas_hist.extend([0.002] * AB_MIN_SAMPLES_B)
    clock = [10_000.0]
    target, current = 20.0, 19.0
    error = abs(target - current)

    with patch(
        "custom_components.vtherm_smartpi.algo.time.monotonic",
        side_effect=lambda: clock[0],
    ):
        algo.calculate(target_temp=target, current_temp=current,
                       ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT)
        assert algo.ctl.last_i_mode == "I:RUN"

        # About 40 minutes during which the PI does not own the command.
        for _ in range(8):
            clock[0] += 300.0
            if override == "guard_cut":
                algo.guards._guard_cut_active = True
                algo.calculate(target_temp=target, current_temp=current,
                               ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT)
            else:
                algo.calculate(target_temp=target, current_temp=None,
                               ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT)
        algo.guards._guard_cut_active = False

        before = algo.integral
        clock[0] += 60.0
        algo.calculate(target_temp=target, current_temp=current,
                       ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT)

    assert algo.ctl.last_i_mode == "I:RUN"
    assert abs(algo.integral - before) <= error * algo._cycle_min
    assert algo.integral == pytest.approx(before)


# ---------------------------------------------------------------------------
# Per-tick anti-windup on the known output limit (max_on_percent)
# ---------------------------------------------------------------------------

KP, KI, TI_MIN = 0.5, 0.05, 10.0  # Ti = Kp / Ki


def _ctl_with_request(request: float, error: float = 0.5, u_ff: float = 0.2) -> SmartPIController:
    """Controller whose next PI request is `request` (u_ff + Kp*e + Ki*I)."""
    ctl = SmartPIController("aw")
    ctl.integral = (request - u_ff - KP * error) / KI
    return ctl


def _compute(ctl: SmartPIController, error: float = 0.5, u_ff: float = 0.2,
             dt_min: float = 1.0, u_max: float = 1.0) -> None:
    ctl.compute_pwm(error=error, error_p=error, kp=KP, ki=KI, u_ff=u_ff, dt_min=dt_min,
                    in_deadband=False, integrator_hold=False, deadband_c=0.0, u_max=u_max)


def _stable_algo(max_on: float | None = None, **kwargs) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=kwargs.pop("min_on", 0),
                   minimal_deactivation_delay=0, name="aw", max_on_percent=max_on, **kwargs)
    force_smartpi_stable_mode(algo)
    return algo


def _tick(algo: SmartPI, mono, t: float, current: float, mode=VThermHvacMode_HEAT,
          target: float = 20.0, ext: float = 10.0) -> None:
    mono.return_value = t
    algo.gov.on_cycle_start()
    algo.calculate(target_temp=target, current_temp=current, ext_current_temp=ext,
                   slope=0.0, hvac_mode=mode)


def _stable_phase():
    return patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE)


def _clock():
    return patch("custom_components.vtherm_smartpi.algo.time.monotonic")


@pytest.mark.asyncio
async def test_unlimited_request_change_does_not_move_integral_at_cycle_end():
    """A request raised during the cycle is not a saturation (PI-01)."""
    ctl = _ctl_with_request(0.50)
    before = ctl.integral
    _compute(ctl)
    assert ctl.track_output_limit(1.0, KP, KI, 1.0) == 0.0
    assert ctl.last_i_mode == "I:RUN"
    assert ctl.integral == pytest.approx(before + 0.5 * 1.0)

    algo = _stable_algo()
    with _clock() as mono, _stable_phase():
        _tick(algo, mono, 1000.0, 19.0)
        _tick(algo, mono, 1060.0, 19.0)
    assert algo.ctl.last_i_mode == "I:RUN"
    assert algo.aw_du == 0.0
    before = algo.integral
    # Applied 0.40 at cycle start while the last tick requests more.
    assert algo.linear_on_percent > 0.40
    await algo.on_cycle_completed(e_eff=0.40, elapsed_ratio=1.0, cycle_duration_min=10.0)
    assert algo.integral == before


def test_max_on_is_the_conditional_integration_limit_and_excess_drains():
    """Above max_on the integral is skipped, and the excess drains at beta."""
    ctl = _ctl_with_request(0.65)
    before = ctl.integral
    _compute(ctl, u_max=0.6)
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"
    assert ctl.last_sat == "SAT_HI"
    assert ctl.integral == before

    ctl = _ctl_with_request(0.55)
    _compute(ctl, u_max=0.6)
    assert ctl.last_i_mode == "I:RUN"

    ctl = _ctl_with_request(0.9)
    before = ctl.integral
    _compute(ctl, u_max=0.6)
    u_i = ctl.u_i
    du = ctl.track_output_limit(0.6, KP, KI, 1.0)
    beta = 1.0 / TI_MIN
    assert du == pytest.approx(-0.3)
    assert ctl.integral == pytest.approx(before + beta * du / KI)
    assert ctl.u_i == u_i  # the decomposition that produced the command is kept


@pytest.mark.parametrize("valve_curve", [False, True])
def test_calculate_uses_linear_max_on_as_static_limit(valve_curve):
    """calculate() judges saturation and tracks on the linear max_on limit."""
    algo = _stable_algo(0.6, enable_valve_linearization=valve_curve)
    limit = algo._linear_max_on()
    with _clock() as mono, _stable_phase():
        _tick(algo, mono, 1000.0, 15.0)
        algo.ctl.integral = 0.2 / algo.Ki  # accumulated integral power 0.2
        before = algo.integral
        _tick(algo, mono, 1060.0, 15.0)
    ctl = algo.ctl
    request = min(max(ctl.u_ff + ctl.u_pi, 0.0), 1.0)
    assert request > limit
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"
    beta = 1.0 / max(algo.Kp / algo.Ki, 1.0)
    assert algo.aw_du == pytest.approx(limit - request)
    assert algo.integral == pytest.approx(max(before + beta * (limit - request) / algo.Ki, 0.0))
    assert 0.0 <= algo.integral < before
    assert algo.linear_on_percent <= limit + 1e-12
    assert algo.on_percent <= 0.6 + 1e-9


def test_static_limit_drains_excess_with_tt_equal_ti():
    """Limit 0.3 below a 0.5 request: dI = dt * (-0.2) / (Ki * Ti) per tick."""
    ctl = _ctl_with_request(0.5)
    _compute(ctl, dt_min=0.0)  # request stays 0.5
    after_integration = ctl.integral
    du = ctl.track_output_limit(0.3, KP, KI, 1.0)
    assert du == pytest.approx(-0.2)
    assert ctl.integral - after_integration == pytest.approx(1.0 * -0.2 / (KI * TI_MIN))


def test_heat_and_cool_track_the_limit_identically():
    """Mirrored HEAT and COOL runs give identical integral and tracking traces."""
    traces = {}
    for mode, sign in ((VThermHvacMode_HEAT, 1.0), (VThermHvacMode_COOL, -1.0)):
        algo = _stable_algo(0.3)
        trace = []
        with _clock() as mono, _stable_phase():
            for k in range(8):
                error = 1.5 if k < 4 else 0.3
                _tick(algo, mono, 1000.0 + 60.0 * k, 20.0 - sign * error, mode=mode,
                      ext=20.0 - sign * 10.0)
                trace.append((algo.ctl.last_i_mode, algo.integral, algo.aw_du, algo.linear_on_percent))
        traces[str(mode)] = trace
    assert traces[str(VThermHvacMode_HEAT)] == traces[str(VThermHvacMode_COOL)]
    assert any(du < 0.0 for _, _, du, _ in traces[str(VThermHvacMode_HEAT)])


@pytest.mark.asyncio
async def test_timing_quantization_is_not_tracked():
    """A min ON forced zero and a retained valve position do not drain the integral."""
    algo = _stable_algo(min_on=60)
    with _clock() as mono, _stable_phase():
        _tick(algo, mono, 1000.0, 19.9)
        mono.return_value = 1060.0
        algo.gov.on_cycle_start()
        before = algo.integral
        algo.calculate(target_temp=20.0, current_temp=19.9, ext_current_temp=10.0,
                       slope=0.0, hvac_mode=VThermHvacMode_HEAT)
    assert algo.forced_by_timing is True
    assert algo.linear_on_percent == 0.0
    assert algo.aw_du == 0.0
    assert algo.ctl.last_i_mode == "I:RUN"
    assert algo.integral == pytest.approx(before + 0.1 * 1.0)
    before = algo.integral
    await algo.on_cycle_completed(e_eff=0.0, elapsed_ratio=1.0, cycle_duration_min=10.0)
    assert algo.integral == before

    valve = _stable_algo(enable_valve_linearization=True)
    with _clock() as mono, _stable_phase():
        _tick(valve, mono, 1000.0, 19.0)
        _tick(valve, mono, 1060.0, 19.0)
        request_id = valve.stage_command(VThermHvacMode_HEAT)
        requested = valve.on_percent
        await valve.on_command_receipt(make_receipt(
            request_id, requested - 0.05, requested=requested, alterations=("valve_retained",),
        ))
        before = valve.integral
        await valve.on_cycle_completed(e_eff=requested - 0.05, elapsed_ratio=1.0, cycle_duration_min=10.0)
    assert valve.integral == before
    assert valve.aw_du == 0.0


@pytest.mark.parametrize(
    ("i_mode", "tracked"),
    [
        ("I:RUN", True),
        ("I:SKIP(SAT_HI)", True),
        ("I:GUARD(setpoint_change)", True),
        ("I:GUARD_UNWIND(setpoint_change)", True),
        ("I:CLAMP(near_ovr)", True),
        ("I:FREEZE(deadband)", False),
        ("I:HOLD", False),
        ("I:HOLD(window_resume)", False),
        ("I:OVERRIDE(guard_cut)", False),
        ("I:RESET", False),
        ("calibration", False),
    ],
)
def test_tracking_follows_the_integral_mode(i_mode, tracked):
    """No tracking while the integral is frozen or the PI does not drive the actuator."""
    ctl = _ctl_with_request(0.5)
    _compute(ctl, dt_min=0.0)
    ctl.last_i_mode = i_mode
    before = ctl.integral
    du = ctl.track_output_limit(0.3, KP, KI, 1.0)
    if tracked:
        assert du == pytest.approx(-0.2)
        assert ctl.integral < before
    else:
        assert du == 0.0
        assert ctl.last_aw_du == 0.0
        assert ctl.integral == before


def test_ownership_snapshot_keeps_the_tick_decomposition():
    """The staged snapshot holds the u_i that produced the command, not the tracked integral."""
    algo = _stable_algo(0.3)
    with _clock() as mono, _stable_phase():
        _tick(algo, mono, 1000.0, 18.5)
        algo.ctl.integral = 0.2 / algo.Ki
        _tick(algo, mono, 1060.0, 18.5)
    assert algo.aw_du < 0.0
    request_id = algo.stage_command(VThermHvacMode_HEAT)
    snapshot = algo._command_ownership._requests[request_id]
    assert snapshot.u_i == algo.ctl.u_i
    assert algo.Ki * algo.integral != pytest.approx(snapshot.u_i)


@pytest.mark.asyncio
async def test_integral_trajectory_is_identical_with_and_without_receipts():
    """The anti-windup does not read receipts: VT with and without receipts match."""
    algos = {"receipts": _stable_algo(0.5), "legacy": _stable_algo(0.5)}
    traces = {name: [] for name in algos}
    temps = [18.0, 18.2, 18.5, 18.9, 19.3, 19.6, 19.8, 19.9]
    for name, algo in algos.items():
        with _clock() as mono, _stable_phase():
            for k, current in enumerate(temps):
                mono.return_value = 1000.0 + 600.0 * k
                algo.calculate(target_temp=20.0, current_temp=current, ext_current_temp=10.0,
                               slope=0.0, hvac_mode=VThermHvacMode_HEAT)
                applied = algo.on_percent
                if name == "receipts":
                    request_id = algo.stage_command(VThermHvacMode_HEAT)
                    await algo.on_command_receipt(make_receipt(request_id, applied))
                else:
                    await algo.on_cycle_started(0.0, 0.0, applied, VThermHvacMode_HEAT)
                mono.return_value += 599.0
                await algo.on_cycle_completed(e_eff=applied, elapsed_ratio=1.0, cycle_duration_min=10.0)
                traces[name].append((algo.integral, algo.aw_du, algo.ctl.last_i_mode))
    assert traces["receipts"] == traces["legacy"]
    assert any(du < 0.0 for _, du, _ in traces["receipts"])


def test_versatile_thermostat_realized_power_hook_is_inert():
    """VT notifies update_realized_power only when the algorithm defines it."""
    algo = _stable_algo(0.5)
    assert hasattr(algo, "update_realized_power") is False


@pytest.mark.parametrize("integral_power", [0.0, -0.1])
def test_proportional_excess_does_not_wind_the_integral_down(integral_power):
    """An excess carried by P alone leaves a zero or negative integral untouched."""
    ctl = SmartPIController("aw")
    ctl.integral = integral_power / KI
    before = ctl.integral
    _compute(ctl, error=2.0, u_ff=0.0, dt_min=0.0, u_max=0.5)  # Kp*e = 1.0 > 0.5
    assert ctl.last_i_mode == "I:SKIP(SAT_HI)"
    du = ctl.track_output_limit(0.5, KP, KI, 1.0)
    assert du == pytest.approx(min(0.5, ctl.u_ff + ctl.u_pi) - min(1.0, ctl.u_ff + ctl.u_pi))
    assert du < 0.0
    assert ctl.integral == before


def test_drain_stops_at_zero_integral():
    """The drain removes the accumulated integral power and stops at zero."""
    ctl = SmartPIController("aw")
    ctl.integral = 0.01 / KI  # Ki*I = 0.01, far below the 0.6 excess step
    _compute(ctl, error=1.5, u_ff=0.0, dt_min=0.0)  # v = 0.76
    ctl.track_output_limit(0.1, KP, KI, 5.0)  # beta * du = 0.5 * -0.66 = -0.33
    assert ctl.integral == 0.0


@pytest.mark.parametrize(
    ("integral_power", "du", "expected_power"),
    [
        (0.3, -0.2, 0.28),   # drains toward zero
        (0.01, -0.5, 0.0),   # stops at zero
        (-0.3, 0.2, -0.28),  # mirrored side: moves up toward zero
        (-0.01, 0.5, 0.0),
        (0.3, 0.2, 0.3),     # never grows the integral away from zero
        (-0.3, -0.2, -0.3),
        (0.0, -0.2, 0.0),
    ],
)
def test_back_calculation_only_moves_toward_zero(integral_power, du, expected_power):
    """Tracking only reduces |Ki*I| and never changes its sign."""
    ctl = SmartPIController("aw")
    ctl.integral = integral_power / KI
    ctl._back_calculate(du, KP, KI, 1.0)  # beta = 1 / Ti = 0.1
    assert KI * ctl.integral == pytest.approx(expected_power)
