"""Test integral hold during deadtime window."""

import time
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.const import AB_HISTORY_SIZE
from custom_components.vtherm_smartpi.smartpi.controller import SmartPIController
from custom_components.vtherm_smartpi.smartpi.diagnostics import (
    build_published_diagnostics,
)
from custom_components.vtherm_smartpi.smartpi.integral_guard import IntegralGuardSource
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT


def make_smartpi(**kwargs):
    defaults = dict(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestIntegralHold",
    )
    defaults.update(kwargs)
    return SmartPI(**defaults)


def _force_stable_phase(pi):
    """Force SmartPI into STABLE phase by populating measurement history."""
    for _ in range(AB_HISTORY_SIZE + 1):
        pi.est.a_meas_hist.append(0.01)
        pi.est.b_meas_hist.append(0.002)
    pi._output_initialized = True
    pi.calibration_mgr.last_calibration_time = time.time()


def _set_reliable_deadtime(pi, dt_s: float):
    """Inject a reliable heat deadtime and a fake episode start."""
    pi.dt_est.deadtime_heat_s = dt_s
    pi.dt_est.deadtime_heat_reliable = True
    pi._t_heat_episode_start = time.monotonic()  # episode started just now


class TestIntegralHoldDuringDeadtime:

    def test_in_deadtime_window_returns_true(self):
        """in_deadtime_window is True when inside the deadtime window."""
        pi = make_smartpi()
        _set_reliable_deadtime(pi, dt_s=300.0)  # 5-minute deadtime
        assert pi.in_deadtime_window is True

    def test_outside_deadtime_window_returns_false(self):
        """in_deadtime_window is False when deadtime has expired."""
        pi = make_smartpi()
        pi.dt_est.deadtime_heat_s = 1.0
        pi.dt_est.deadtime_heat_reliable = True
        # Episode started far in the past
        pi._t_heat_episode_start = time.monotonic() - 10.0
        assert pi.in_deadtime_window is False

    def test_no_reliable_deadtime_returns_false(self):
        """in_deadtime_window is False when deadtime is not reliable."""
        pi = make_smartpi()
        pi.dt_est.deadtime_heat_reliable = False
        pi._t_heat_episode_start = time.monotonic()
        assert pi.in_deadtime_window is False

    def test_compute_pwm_receives_hold_true_during_deadtime(self):
        """calculate() must pass integrator_hold=True to compute_pwm when in deadtime window."""
        from unittest.mock import patch as _patch
        pi = make_smartpi()
        _force_stable_phase(pi)
        _set_reliable_deadtime(pi, dt_s=300.0)
        assert pi.in_deadtime_window is True

        with _patch.object(pi.ctl, 'compute_pwm', wraps=pi.ctl.compute_pwm) as mock_pwm:
            pi.calculate(
                target_temp=20.0,
                current_temp=18.0,
                ext_current_temp=5.0,
                hvac_mode=VThermHvacMode_HEAT,
            )

        mock_pwm.assert_called_once()
        # integrator_hold is positional arg index 7 in the current compute_pwm signature.
        integrator_hold_arg = mock_pwm.call_args[0][7]
        assert integrator_hold_arg is True, (
            f"compute_pwm should receive integrator_hold=True during deadtime, got {integrator_hold_arg}"
        )
        assert mock_pwm.call_args.kwargs["integrator_hold_source"] == "deadtime"
        assert pi.ctl.last_integral_hold_source == "deadtime"
        assert (
            build_published_diagnostics(pi)["temperature"][
                "integral_hold_source"
            ]
            == "deadtime"
        )

    def test_deadtime_does_not_set_hold_outside_window(self):
        """Deadtime code path must not set integrator_hold=True when outside deadtime window.

        We spy on gov.determine_regime to capture the integrator_hold value
        BEFORE governance may independently freeze the integrator for other reasons
        (e.g. saturation). Index 2 is the integrator_hold positional arg.
        """
        from unittest.mock import patch as _patch
        pi = make_smartpi()
        _force_stable_phase(pi)

        # No reliable deadtime → in_deadtime_window == False
        pi.dt_est.deadtime_heat_reliable = False
        assert pi.in_deadtime_window is False

        with _patch.object(pi.gov, 'determine_regime', wraps=pi.gov.determine_regime) as mock_gov:
            pi.calculate(
                target_temp=20.0,
                current_temp=18.0,
                ext_current_temp=5.0,
                hvac_mode=VThermHvacMode_HEAT,
            )

        mock_gov.assert_called_once()
        # integrator_hold is positional arg index 2 (phase, ext_temp, integrator_hold, ...)
        hold_at_governance_entry = mock_gov.call_args[0][2]
        assert hold_at_governance_entry is False, (
            f"Deadtime must not set integrator_hold before governance when outside window, got {hold_at_governance_entry}"
        )

    def test_window_resume_uses_hold_then_promotes_to_guard_only_if_needed(self):
        """A window resume must hold during heating deadtime, then arm guard only for a large residual."""
        pi = make_smartpi()
        _force_stable_phase(pi)
        pi.dt_est.deadtime_heat_s = 300.0
        pi.dt_est.deadtime_heat_reliable = True
        pi._startup_grace_period = False
        pi._last_restart_reason = "window"

        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        assert pi.ctl.integral_hold_mode == IntegralGuardSource.WINDOW_RESUME.value
        assert pi.ctl.last_i_mode == "I:HOLD(window_resume)"
        assert pi.integral_guard.active is False
        assert pi._resume_deadtime_hold_source == IntegralGuardSource.WINDOW_RESUME

        pi._t_heat_episode_start = time.monotonic()
        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        assert pi.ctl.integral_hold_mode == IntegralGuardSource.WINDOW_RESUME.value
        assert pi.ctl.last_i_mode == "I:HOLD(window_resume)"
        assert pi._resume_deadtime_hold_started is True

        pi._t_heat_episode_start = time.monotonic() - 301.0
        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        assert pi.ctl.integral_hold_mode == "none"
        assert pi.integral_guard.active is True
        assert pi.integral_guard.source == IntegralGuardSource.WINDOW_RESUME
        assert pi._resume_deadtime_hold_source == IntegralGuardSource.NONE
        assert pi._resume_deadtime_hold_started is False

    def test_window_resume_skips_guard_when_residual_error_is_already_small(self):
        """A small residual after deadtime must release the hold without arming the guard."""
        pi = make_smartpi()
        _force_stable_phase(pi)
        pi.dt_est.deadtime_heat_s = 300.0
        pi.dt_est.deadtime_heat_reliable = True
        pi._startup_grace_period = False
        pi._last_restart_reason = "window"

        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        pi._t_heat_episode_start = time.monotonic()
        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        pi._t_heat_episode_start = time.monotonic() - 301.0
        pi.calculate(
            target_temp=20.0,
            current_temp=19.92,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        assert pi.ctl.integral_hold_mode == "none"
        assert pi.integral_guard.active is False
        assert pi._resume_deadtime_hold_source == IntegralGuardSource.NONE
        assert pi._resume_deadtime_hold_started is False

    def test_power_shedding_resume_uses_same_deadtime_hold_policy(self):
        """A power-shedding resume must reuse the same post-deadtime decision path."""
        pi = make_smartpi()
        _force_stable_phase(pi)
        pi.dt_est.deadtime_heat_s = 300.0
        pi.dt_est.deadtime_heat_reliable = True
        pi._startup_grace_period = False
        pi._last_restart_reason = "power_shedding"

        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )
        assert pi.ctl.last_i_mode == "I:HOLD(power_shedding_resume)"

        pi._t_heat_episode_start = time.monotonic() - 301.0
        pi._resume_deadtime_hold_started = True
        pi.calculate(
            target_temp=20.0,
            current_temp=18.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            slope=0.0,
        )

        assert pi.ctl.integral_hold_mode == "none"
        assert pi.integral_guard.active is True
        assert pi.integral_guard.source == IntegralGuardSource.POWER_SHEDDING_RESUME


def test_controller_reports_effective_hold_source_and_clears_it_next_cycle() -> None:
    """Explicit hold modes must override the bare source for diagnostics."""
    controller = SmartPIController("hold-source")
    common = {
        "error": 0.2,
        "error_p": 0.2,
        "kp": 0.5,
        "ki": 0.01,
        "u_ff": 0.2,
        "dt_min": 5.0,
        "in_deadband": False,
        "deadband_c": 0.1,
    }

    controller.compute_pwm(
        **common,
        integrator_hold=True,
        integrator_hold_source="external",
    )
    assert controller.last_i_mode == "I:HOLD"
    assert controller.last_integral_hold_source == "external"

    controller.start_integral_hold("window_resume")
    controller.compute_pwm(
        **common,
        integrator_hold=True,
        integrator_hold_source="deadtime",
    )
    assert controller.last_i_mode == "I:HOLD(window_resume)"
    assert controller.last_integral_hold_source == "window_resume"

    controller.clear_integral_hold()
    controller.compute_pwm(
        **common,
        integrator_hold=False,
    )
    assert not controller.last_i_mode.startswith("I:HOLD")
    assert controller.last_integral_hold_source == "none"


def test_early_return_clears_previous_hold_source() -> None:
    """A calculation without PI evaluation must not retain a stale source."""
    pi = make_smartpi()
    pi.ctl.last_integral_hold_source = "external"

    pi.calculate(
        target_temp=20.0,
        current_temp=None,
        ext_current_temp=5.0,
        hvac_mode=VThermHvacMode_HEAT,
    )

    assert pi.ctl.last_i_mode == "I:FREEZE(missing_sensor)"
    assert pi.ctl.last_integral_hold_source == "none"


def test_vt_safety_override_holds_the_integral() -> None:
    """While VT safety substitutes its power, the integral does not integrate."""
    from unittest.mock import PropertyMock, patch

    from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase

    def run(override: bool) -> tuple[float, float, SmartPI]:
        pi = make_smartpi(deadband_c=0.0)
        with (
            patch("custom_components.vtherm_smartpi.algo.time.monotonic") as monotonic,
            patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        ):
            for minute in range(4):
                monotonic.return_value = 1000.0 + 60.0 * minute
                pi.vt_safety_override = override and minute >= 2
                if minute == 2:
                    before = pi.ctl.integral
                pi.calculate(
                    target_temp=20.0,
                    current_temp=19.5,
                    ext_current_temp=5.0,
                    slope=0.0,
                    hvac_mode=VThermHvacMode_HEAT,
                )
        return before, pi.ctl.integral, pi

    before, after, pi = run(override=True)
    assert after == before
    assert pi.ctl.last_i_mode == "I:HOLD"
    assert pi.ctl.last_integral_hold_source == "vt_safety"
    published = build_published_diagnostics(pi)
    assert published["temperature"]["integral_hold_source"] == "vt_safety"

    before_ref, after_ref, ref = run(override=False)
    assert before_ref == before
    assert after_ref != before_ref
    assert ref.ctl.last_integral_hold_source != "vt_safety"


def _run_two_ticks(pi, *, ext_temp, prepare=None):
    """Run two STABLE calculate() ticks one minute apart and return the integral before the second."""
    from unittest.mock import PropertyMock, patch

    from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase

    with (
        patch("custom_components.vtherm_smartpi.algo.time.monotonic") as monotonic,
        patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
    ):
        before = None
        for minute in range(2):
            monotonic.return_value = 1000.0 + 60.0 * minute
            if minute == 1:
                before = pi.ctl.integral
                if prepare is not None:
                    prepare(pi)
            pi.calculate(
                target_temp=20.0,
                current_temp=19.5,
                ext_current_temp=ext_temp,
                slope=0.0,
                hvac_mode=VThermHvacMode_HEAT,
            )
    return before


def test_regime_transition_freezes_gains_but_not_the_integral() -> None:
    """A regime transition inside a cycle freezes gains and learning only."""
    from custom_components.vtherm_smartpi.smartpi.const import (
        FreezeReason,
        GovernanceDecision,
        GovernanceRegime,
    )

    pi = make_smartpi(deadband_c=0.0)

    def mixed_cycle(p):
        p.gov._cycle_regimes = {GovernanceRegime.NEAR_BAND, GovernanceRegime.EXCITED_STABLE}

    before = _run_two_ticks(pi, ext_temp=5.0, prepare=mixed_cycle)
    assert pi.gov.last_decision_gains == GovernanceDecision.HARD_FREEZE
    assert pi.gov.last_freeze_reason_gains == FreezeReason.REGIME_TRANSITION
    assert pi.ctl.last_i_mode == "I:RUN"
    assert pi.ctl.last_integral_hold_source == "none"
    assert pi.ctl.integral != before


def test_degraded_regime_holds_the_integral() -> None:
    """Without outdoor temperature (DEGRADED) the integral is held."""
    from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime

    pi = make_smartpi(deadband_c=0.0)
    before = _run_two_ticks(pi, ext_temp=None)
    assert pi.gov.regime == GovernanceRegime.DEGRADED
    assert pi.ctl.last_i_mode == "I:HOLD"
    assert pi.ctl.last_integral_hold_source == "governance_sensor_invalid"
    assert pi.ctl.integral == before


def test_deadtime_hold_has_priority_over_degraded() -> None:
    """The deadtime hold keeps its source when DEGRADED also holds."""
    pi = make_smartpi(deadband_c=0.0)

    def in_deadtime(p):
        p.dt_est.deadtime_heat_s = 3600.0
        p.dt_est.deadtime_heat_reliable = True
        p._t_heat_episode_start = 1050.0

    before = _run_two_ticks(pi, ext_temp=None, prepare=in_deadtime)
    assert pi.ctl.last_i_mode == "I:HOLD"
    assert pi.ctl.last_integral_hold_source == "deadtime"
    assert pi.ctl.integral == before


def test_deadband_shell_holds_during_a_regime_transition() -> None:
    """The hysteresis shell (deadband, outside the core) holds the integral."""
    from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime

    pi = make_smartpi(deadband_c=0.0)

    def in_shell(p):
        update = p.deadband_mgr.update

        def update_in_shell(*args, **kwargs):
            result = update(*args, **kwargs)
            p.deadband_mgr._in_deadband = True  # hysteresis kept; core is empty (deadband_c = 0)
            return result

        p.deadband_mgr.update = update_in_shell
        p.gov._cycle_regimes = {GovernanceRegime.NEAR_BAND, GovernanceRegime.EXCITED_STABLE}

    before = _run_two_ticks(pi, ext_temp=5.0, prepare=in_shell)
    assert pi.ctl.last_i_mode == "I:HOLD"
    assert pi.ctl.last_integral_hold_source == "deadband_hysteresis_shell"
    assert pi.ctl.integral == before
