import pytest
from unittest.mock import MagicMock
from custom_components.vtherm_smartpi.algo import SmartPI


@pytest.mark.asyncio
async def test_smartpi_cycle_feedback_keeps_command_state():
    """Realized cycle power feeds the causal trace, not u_prev or the integral."""
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestAlgo",
        max_on_percent=1.0,
        deadband_c=0.0,
    )
    algo.ctl.last_i_mode = "I:RUN"
    algo.ctl.integral = 0.5
    algo.u_prev = 0.5

    await algo.on_cycle_completed(e_eff=0.1, elapsed_ratio=1.0, cycle_duration_min=5.0)
    await algo.on_cycle_completed(e_eff=0.0, elapsed_ratio=0.4, cycle_duration_min=5.0)

    assert algo.u_prev == 0.5
    assert algo.integral == 0.5
    assert algo.aw_du == 0.0
    assert algo.forced_by_timing is False
    assert not hasattr(algo, "update_realized_power")
