"""Tests for the FF-trim freeze during the FF1 warmup."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import AB_HISTORY_SIZE
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff
from custom_components.vtherm_smartpi.smartpi.ff_trim import FFTrim


def test_compute_ff_reports_the_warmup_scale() -> None:
    """The FF result carries the warmup factor applied to FF1."""
    result = compute_ff(
        k_ff=0.05,
        target_temp_ff=20.0,
        ext_temp=10.0,
        warmup_scale=0.4,
        trim=FFTrim(),
    )

    assert result.warmup_scale == pytest.approx(0.4)
    assert result.u_ff1 == pytest.approx(0.2)


def _learned_algo(learn_ok_count: int) -> SmartPI:
    algo = SmartPI(
        hass=MagicMock(),
        cycle_min=10.0,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="ff-warmup",
        use_ff3=False,
    )
    algo.set_learning_enabled(False)
    algo.est.ensure_hvac_mode(VThermHvacMode_HEAT)
    algo.est.a, algo.est.b = 0.02, 0.001
    algo.est.a_meas_hist.extend([0.02] * AB_HISTORY_SIZE)
    algo.est.b_meas_hist.extend([0.001] * AB_HISTORY_SIZE)
    algo.est._a_hat_hist.extend([0.02] * 5)
    algo.est._b_hat_hist.extend([0.001] * 5)
    algo.est.learn_ok_count = learn_ok_count
    algo.est.learn_ok_count_a = AB_HISTORY_SIZE
    algo.est.learn_ok_count_b = AB_HISTORY_SIZE
    algo.dt_est.ensure_hvac_mode(VThermHvacMode_HEAT)
    algo.dt_est.deadtime_heat_s = 300.0
    algo.dt_est.deadtime_heat_reliable = True
    algo._last_hvac_mode = VThermHvacMode_HEAT
    algo._cycles_since_reset = algo.ff_warmup_cycles
    return algo


@pytest.mark.parametrize(
    ("learn_ok_count", "expected"),
    ((10, "ff_warmup"), (2 * AB_HISTORY_SIZE, "none")),
)
def test_trim_freezes_while_ff1_warms_up(learn_ok_count: int, expected: str) -> None:
    """The trim must not learn the FF1 share still held back by the warmup."""
    algo = _learned_algo(learn_ok_count)

    with patch(
        "custom_components.vtherm_smartpi.algo.time.monotonic",
        return_value=1000.0,
    ):
        algo.calculate(
            target_temp=20.0,
            current_temp=19.9,
            ext_current_temp=10.0,
            slope=0.0,
            hvac_mode=VThermHvacMode_HEAT,
        )

    assert algo._last_ff_result is not None
    assert (algo._last_ff_result.warmup_scale < 1.0) is (expected == "ff_warmup")
    assert algo._ab_confidence.state.value == "ab_ok"
    assert algo._ff_trim.freeze_reason == expected
    assert algo._ff_trim.frozen is (expected != "none")
