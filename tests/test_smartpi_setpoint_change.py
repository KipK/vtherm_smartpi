
import logging
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

def test_smartpi_setpoint_change_restarts_a_learning_after_boundary():
    """An A window cannot cross a setpoint change but can restart afterwards."""
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

    # The exact change tick closes the old-regime A window.
    smart_pi.update_learning(1.0, 18.0, 5.0, 1.0, setpoint_changed=True)
    assert not smart_pi.learn_win_active
    assert smart_pi.est.learn_last_reason == "skip: setpoint transition"

    # The following active-ascent observation can start a clean A window.
    smart_pi.update_learning(1.0, 18.0, 5.0, 1.0, setpoint_changed=False)
    assert smart_pi.learn_win_active
    assert "setpoint change" not in smart_pi.est.learn_last_reason

    # Power transition: with CV guard, the first anomalous sample is accumulated
    # (CV from 2 previous samples at u=1.0 is 0 → no trigger yet).
    smart_pi.update_learning(1.0, 18.0, 5.0, 0.0)  # 1st anomalous: accumulates, no trigger
    assert smart_pi.learn_win_active  # still open

    # Second cycle at u=0.0: CV({1.0, 1.0, 0.0}) ≈ 0.86 >> 0.30 → triggers closure
    smart_pi.update_learning(1.0, 18.0, 5.0, 0.0)
    assert not smart_pi.learn_win_active


def test_smartpi_feedforward_and_integral_keep_the_raw_setpoint_with_an_anticipation():
    """The anticipated temperature feeds the P error; deadband and feedforward use the raw setpoint."""
    smart_pi = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestTherm",
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
        _, error_i, error_p, _ = smart_pi._manage_setpoint(
            target_temp, current_temp, VThermHvacMode_HEAT, anticipation_c=0.25,
        )
        assert error_i == pytest.approx(target_temp - current_temp)
        assert error_p == pytest.approx(target_temp - current_temp - 0.25)

        e_p = smart_pi._update_control_context(
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

    compute_ff_mock.assert_called_once()
    assert compute_ff_mock.call_args.kwargs["target_temp_ff"] == pytest.approx(target_temp)
    assert e_p == pytest.approx(error_p)
