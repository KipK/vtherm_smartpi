# tests/test_smartpi_nearband_model.py
"""Near band auto (HEAT): horizons from the response model, slopes from the a/b model."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.deadband import DeadbandManager
from custom_components.vtherm_smartpi.smartpi.measure_event import MODE_HEAT, SOURCE_PULSE
from custom_components.vtherm_smartpi.smartpi.response_model import ResponseModel

CYCLE_MIN = 10.0          # half cycle: 300 s added to both horizons
# a = 0.02 C/min, b = 0.001 /min, room 20 C, outdoor 18 C: losses 0.002 C/min, net rise 0.018 C/min,
# asymmetry 0.002 / 0.018 clamped to 0.3.
NET_C_PER_S = 0.018 / 60.0
ALPHA = 0.3


def _estimator(count: int = 20, a: float = 0.02):
    return SimpleNamespace(learn_ok_count_a=count, a=a, b=0.001)


def _auto(mgr: DeadbandManager, response, estimator=None) -> None:
    mgr.update_near_band_auto(
        VThermHvacMode_HEAT, 20.0, 18.0, response, estimator or _estimator(), CYCLE_MIN, 0.0,
    )


def test_horizons_are_the_effective_dead_time_above_and_the_emitter_lag_below() -> None:
    """theta 600 s, tau_e 1800 s: above theta + tau_e / 2 = 1500 s, below theta + tau_e = 2400 s, plus half a cycle."""
    mgr = DeadbandManager("t", 0.5)
    _auto(mgr, {"theta_s": 600.0, "tau_e_s": 1800.0})
    assert mgr.near_band_source == "auto_model_aware"
    assert mgr.near_band_below_deg == pytest.approx(NET_C_PER_S * (2400.0 + 300.0))
    assert mgr.near_band_above_deg == pytest.approx(ALPHA * NET_C_PER_S * (1500.0 + 300.0))


def test_assumed_split_takes_the_cautious_ends() -> None:
    """An event that does not tell the dead time apart: theta = S / 2, so above 3 S / 4 and below S."""
    model = ResponseModel()
    model.add(MODE_HEAT, SOURCE_PULSE, {"theta_s": 100.0, "tau_e_s": 2300.0, "s_s": 2400.0, "split_ok": False}, 0.0)
    values = model.values(MODE_HEAT)
    assert values["confidence"] == "dead time assumed"
    mgr = DeadbandManager("t", 0.5)
    _auto(mgr, values)
    assert mgr.near_band_below_deg == pytest.approx(NET_C_PER_S * (2400.0 + 300.0))
    assert mgr.near_band_above_deg == pytest.approx(ALPHA * NET_C_PER_S * (1800.0 + 300.0))


def test_no_response_model_keeps_the_configured_band() -> None:
    mgr = DeadbandManager("t", 0.5)
    _auto(mgr, None)
    assert mgr.near_band_source == "no response model"
    assert mgr.near_band_below_deg == 0.5


def test_a_b_model_not_ready_keeps_the_configured_band() -> None:
    mgr = DeadbandManager("t", 0.5)
    _auto(mgr, {"theta_s": 600.0, "tau_e_s": 1800.0}, _estimator(count=0))
    assert mgr.near_band_source == "fallback_model"
    assert mgr.near_band_below_deg == 0.5


@pytest.mark.parametrize("response, auto", [(None, False), ({"theta_s": 600.0, "tau_e_s": 1800.0}, True)])
def test_update_computes_the_auto_band_in_heat_only_with_a_model(response, auto) -> None:
    for mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL):
        mgr = DeadbandManager("t", 0.5)
        mgr.update(
            error=0.2, near_band_error=None, hvac_mode=mode, tau_reliable=True, response=response,
            estimator=_estimator(), current_temp=20.0, ext_temp=18.0, cycle_min=CYCLE_MIN, deadband_c=0.0,
        )
        computed = auto and mode == VThermHvacMode_HEAT
        assert (mgr.near_band_source == "auto_model_aware") is computed
        assert (mgr.near_band_below_deg != 0.5) is computed


def test_smartpi_reads_the_heat_response_model() -> None:
    pi = SmartPI(MagicMock(), CYCLE_MIN, 0, 0, "test_pi", max_on_percent=1.0, deadband_c=0.0, near_band_deg=0.5)
    pi.est = MagicMock()
    pi.est.learn_ok_count_a = 20
    pi.est.a = 0.02
    pi.est.b = 0.001
    pi.update_near_band_auto(VThermHvacMode_HEAT, 20.0, 18.0)
    assert pi.deadband_mgr.near_band_source == "no response model"
    pi._start.model.add(
        MODE_HEAT, SOURCE_PULSE, {"theta_s": 600.0, "tau_e_s": 1800.0, "s_s": 2400.0, "split_ok": True}, 0.0,
    )
    pi.update_near_band_auto(VThermHvacMode_HEAT, 20.0, 18.0)
    assert pi.deadband_mgr.near_band_source == "auto_model_aware"
    assert pi.deadband_mgr.near_band_below_deg == pytest.approx(NET_C_PER_S * 2700.0)
