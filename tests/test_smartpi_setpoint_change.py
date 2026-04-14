
import logging
from datetime import datetime, timedelta
import pytest
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.smartpi.ab_estimator import TauReliability
from custom_components.vtherm_smartpi.smartpi.const import GovernanceDecision
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff as compute_ff_impl
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from helpers import force_smartpi_stable_mode
from unittest.mock import MagicMock, patch

# Set up logging to catch the "extending window" message if needed
logging.basicConfig(level=logging.DEBUG)

def test_smartpi_setpoint_change_continues_learning():
    """
    Test that a setpoint change during an active learning window does NOT abort it.
    The window continues; power-transition detection (u_active ≠ u_first) is the
    real guard and will close the window if heating power actually changes.
    """
    smart_pi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestTherm",
    )
    force_smartpi_stable_mode(smart_pi)

    # Open a window via update_learning directly (t_heat_episode_start=None → deadtime bypassed)
    smart_pi.update_learning(1.0, 18.0, 5.0, 1.0)  # ON phase, 1 min
    assert smart_pi.learn_win_active

    # Setpoint change mid-window: window should continue
    smart_pi.update_learning(1.0, 18.0, 5.0, 1.0, setpoint_changed=True)
    assert smart_pi.learn_win_active
    assert "setpoint change" not in smart_pi.est.learn_last_reason

    # Power transition: with CV guard, the first anomalous sample is accumulated
    # (CV from 2 previous samples at u=1.0 is 0 → no trigger yet).
    smart_pi.update_learning(1.0, 18.0, 5.0, 0.0)  # 1st anomalous: accumulates, no trigger
    assert smart_pi.learn_win_active  # still open

    # Second cycle at u=0.0: CV({1.0, 1.0, 0.0}) ≈ 0.86 >> 0.30 → triggers closure
    smart_pi.update_learning(1.0, 18.0, 5.0, 0.0)
    assert not smart_pi.learn_win_active


def test_smartpi_feedforward_uses_raw_setpoint_when_trajectory_is_active():
    """Feedforward must use the raw setpoint even when the P-path is trajectory-shaped."""
    smart_pi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestTherm",
        use_setpoint_filter=True,
    )
    force_smartpi_stable_mode(smart_pi)

    smart_pi.est.a = 0.40
    smart_pi.est.b = 0.02
    smart_pi.est.learn_ok_count = 50
    smart_pi.est.learn_ok_count_a = 20
    smart_pi.est.learn_ok_count_b = 20
    smart_pi._committed_on_percent = 1.0
    smart_pi._cycles_since_reset = smart_pi.ff_warmup_cycles

    target_temp = 21.0
    current_temp = 20.2
    ext_temp = 5.0

    with patch.object(
        smart_pi.est,
        "tau_reliability",
        return_value=TauReliability(reliable=True, tau_min=50.0),
    ):
        smart_pi._manage_setpoint(
            target_temp=target_temp,
            current_temp=current_temp,
            ext_current_temp=ext_temp,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=0.0,
        )

        smart_pi._last_target_temp = target_temp
        smart_pi._last_current_temp = 20.75
        current_temp = 20.6
        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=target_temp,
            current_temp=current_temp,
            ext_current_temp=ext_temp,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=600.0,
        )
        assert target_temp_filt == pytest.approx(target_temp)

        smart_pi._last_target_temp = target_temp
        smart_pi._last_current_temp = current_temp
        current_temp = 20.7
        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=target_temp,
            current_temp=current_temp,
            ext_current_temp=ext_temp,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=1200.0,
        )

    e_p, _ = smart_pi._update_control_context(
        error_i=error_i,
        hvac_mode=VThermHvacMode_HEAT,
        current_temp=current_temp,
        ext_current_temp=ext_temp,
        error_p=error_p,
    )

    with patch(
        "custom_components.vtherm_smartpi.algo.compute_ff",
        wraps=compute_ff_impl,
    ) as compute_ff_mock:
        smart_pi._apply_gains_and_ff(
            gov_decision_g=GovernanceDecision.ADAPT_ON,
            target_temp_ff=target_temp,
            ext_current_temp=ext_temp,
            hvac_mode=VThermHvacMode_HEAT,
            error=error_i,
            current_temp=current_temp,
            e_p=e_p,
        )

    assert compute_ff_mock.call_count == 2
    assert all(
        call.kwargs["target_temp_ff"] == pytest.approx(target_temp)
        for call in compute_ff_mock.call_args_list
    )


def test_smartpi_late_braking_stays_armed_after_positive_setpoint_change():
    """A positive setpoint change must keep late braking pending until the braking zone is reached."""
    smart_pi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestTherm",
        use_setpoint_filter=True,
    )
    force_smartpi_stable_mode(smart_pi)

    smart_pi.est.a = 0.200835
    smart_pi.est.b = 0.005569
    smart_pi.est.learn_ok_count = 50
    smart_pi.est.learn_ok_count_a = 20
    smart_pi.est.learn_ok_count_b = 20
    smart_pi._committed_on_percent = 1.0
    smart_pi.dt_est.deadtime_cool_s = 251.99418194520405
    smart_pi.dt_est.deadtime_cool_reliable = True

    with patch.object(
        smart_pi.est,
        "tau_reliability",
        return_value=TauReliability(reliable=True, tau_min=179.5654516071108),
    ):
        smart_pi._manage_setpoint(
            target_temp=21.0,
            current_temp=21.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=0.0,
        )

        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=22.0,
            current_temp=21.0,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=600.0,
        )
        assert target_temp_filt == pytest.approx(22.0)
        assert error_i == pytest.approx(1.0)
        assert error_p == pytest.approx(1.0)
        assert smart_pi.sp_mgr.trajectory_active is False

        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=22.0,
            current_temp=21.64,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=1200.0,
        )

    assert target_temp_filt == pytest.approx(22.0)
    assert error_i == pytest.approx(0.36)
    assert error_p == pytest.approx(0.36)
    assert smart_pi.sp_mgr.trajectory_active is True


def test_smartpi_resume_from_off_keeps_late_braking_pending():
    """Resume from OFF should arm delayed late braking when demand is still significant."""
    smart_pi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestTherm",
        use_setpoint_filter=True,
    )
    force_smartpi_stable_mode(smart_pi)

    smart_pi.est.a = 0.064326
    smart_pi.est.b = 0.001215
    smart_pi.est.learn_ok_count = 50
    smart_pi.est.learn_ok_count_a = 20
    smart_pi.est.learn_ok_count_b = 20
    smart_pi._committed_on_percent = 1.0
    smart_pi.dt_est.deadtime_cool_s = 932.7435551425029
    smart_pi.dt_est.deadtime_cool_reliable = True

    with patch.object(
        smart_pi.est,
        "tau_reliability",
        return_value=TauReliability(reliable=True, tau_min=823.0452674897119),
    ):
        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=23.0,
            current_temp=21.56,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=0.0,
            resumed_from_off=True,
        )
        assert target_temp_filt == pytest.approx(23.0)
        assert error_i == pytest.approx(1.44)
        assert error_p == pytest.approx(1.44)
        assert smart_pi.sp_mgr.trajectory_active is False
        assert smart_pi.sp_mgr.save_state()["trajectory_pending_target_change_braking"] is True

        target_temp_filt, _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp=23.0,
            current_temp=22.4,
            ext_current_temp=5.0,
            hvac_mode=VThermHvacMode_HEAT,
            dt_min=10.0,
            now_monotonic=600.0,
            remaining_cycle_min=8.0,
        )

    assert target_temp_filt == pytest.approx(23.0)
    assert error_i == pytest.approx(0.6)
    assert error_p == pytest.approx(0.6)
    assert smart_pi.sp_mgr.trajectory_active is True
