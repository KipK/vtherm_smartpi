"""Outdoor sensor dropout: the control path keeps the last valid outdoor temperature;
the integral is held only until a first outdoor value is received."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import GovernanceRegime, SmartPIPhase
from custom_components.vtherm_smartpi.smartpi.feedforward import compute_ff
from custom_components.vtherm_smartpi.smartpi.ff_trim import FFTrim

T0 = 1000.0
DAY_S = 86400.0


# ---------------------------------------------------------------------------
# Feedforward without outdoor temperature (FF-02)
# ---------------------------------------------------------------------------
def _ff(ext_temp, trim_value: float):
    trim = FFTrim()
    trim.u_ff_trim = trim_value
    return compute_ff(k_ff=0.05, target_temp_ff=20.0, ext_temp=ext_temp, warmup_scale=1.0, trim=trim)


def test_trim_is_applied_with_outdoor_temperature() -> None:
    result = _ff(5.0, 0.05)
    assert result.u_ff1 == pytest.approx(0.75)
    assert result.u_ff2 == pytest.approx(0.05)
    assert result.u_ff_final == pytest.approx(0.80)
    assert result.trim_applied is True


def test_trim_is_not_applied_without_outdoor_temperature() -> None:
    result = _ff(None, 0.05)
    assert result.u_ff1 == 0.0
    assert result.u_ff2 == 0.0
    assert result.u_ff_trim == 0.0
    assert result.u_ff_final == 0.0
    assert result.trim_applied is False
    assert result.ff_reason == "ff_no_ext_temp"


# ---------------------------------------------------------------------------
# SmartPI runtime
# ---------------------------------------------------------------------------
def _make_pi() -> SmartPI:
    return SmartPI(
        hass=MagicMock(),
        cycle_min=10,
        minimal_activation_delay=0,
        minimal_deactivation_delay=0,
        name="TestOutdoorHold",
        deadband_c=0.0,
    )


def _ticks(pi: SmartPI, schedule: list[tuple[float, float | None]]) -> list[dict]:
    """Run STABLE calculate() ticks at (seconds after T0, outdoor temperature); return per-tick facts."""
    out = []
    learning_ext = []
    ff_ext = []
    original_update_learning = pi.update_learning
    original_apply_gains_and_ff = pi._apply_gains_and_ff

    def update_learning(*args, **kwargs):
        learning_ext.append(kwargs.get("ext_temp"))
        return original_update_learning(*args, **kwargs)

    def apply_gains_and_ff(*args, **kwargs):
        ff_ext.append(args[2])
        return original_apply_gains_and_ff(*args, **kwargs)

    with (
        patch("custom_components.vtherm_smartpi.algo.time.monotonic") as monotonic,
        patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch.object(pi, "update_learning", side_effect=update_learning),
        patch.object(pi, "_apply_gains_and_ff", side_effect=apply_gains_and_ff),
    ):
        for t_s, ext in schedule:
            monotonic.return_value = T0 + t_s
            i_before = pi.ctl.integral
            learning_ext.clear()
            ff_ext.clear()
            pi.calculate(
                target_temp=20.0,
                current_temp=19.5,
                ext_current_temp=ext,
                slope=0.0,
                hvac_mode=VThermHvacMode_HEAT,
            )
            out.append({
                "regime": pi.gov.regime,
                "i_mode": pi.ctl.last_i_mode,
                "hold_source": pi.ctl.last_integral_hold_source,
                "integral_moved": pi.ctl.integral != i_before,
                "ff_reason": pi._last_ff_result.ff_reason,
                "trim_applied": pi._last_ff_result.trim_applied,
                "ff_ext": list(ff_ext),
                "last_ext": pi._last_ext_temp,
                "learning_ext": list(learning_ext),
            })
    return out


@pytest.mark.parametrize("age_s", [60.0, 2 * 3600.0, DAY_S, 30 * DAY_S])
def test_dropout_keeps_the_last_valid_value_and_runs_the_integral(age_s: float) -> None:
    pi = _make_pi()
    ticks = _ticks(pi, [(0.0, 3.0), (60.0, 5.0), (60.0 + age_s, None)])
    tick = ticks[-1]
    assert tick["ff_ext"] == [5.0]  # control path uses the last valid reading, without age limit
    assert tick["ff_reason"] == "ff_none"
    assert tick["trim_applied"] is True
    assert tick["regime"] == GovernanceRegime.DEGRADED  # gains, learning and trim stay frozen
    assert tick["i_mode"] == "I:RUN"
    assert tick["hold_source"] == "none"
    assert tick["integral_moved"] is True


def test_startup_without_outdoor_value_drops_ff_and_holds_the_integral() -> None:
    pi = _make_pi()
    ticks = _ticks(pi, [(0.0, None), (60.0, None), (DAY_S, None)])
    for tick in ticks[1:]:
        assert tick["ff_ext"] == [None]
        assert tick["ff_reason"] == "ff_no_ext_temp"
        assert tick["trim_applied"] is False
        assert tick["regime"] == GovernanceRegime.DEGRADED
        assert tick["i_mode"] == "I:HOLD"
        assert tick["hold_source"] == "governance_sensor_invalid"
        assert tick["integral_moved"] is False


def test_first_valid_value_after_startup_enables_ff_and_frees_the_integral() -> None:
    pi = _make_pi()
    ticks = _ticks(pi, [(0.0, None), (60.0, None), (120.0, 4.0), (180.0, None)])
    assert ticks[1]["i_mode"] == "I:HOLD"
    assert ticks[2]["ff_ext"] == [4.0]
    assert ticks[2]["ff_reason"] == "ff_none"
    assert ticks[2]["regime"] != GovernanceRegime.DEGRADED
    assert ticks[2]["i_mode"] == "I:RUN"
    assert ticks[3]["ff_ext"] == [4.0]  # later dropout: last value kept
    assert ticks[3]["regime"] == GovernanceRegime.DEGRADED
    assert ticks[3]["i_mode"] == "I:RUN"
    assert ticks[3]["hold_source"] == "none"
    assert ticks[3]["integral_moved"] is True


def test_learning_and_published_outdoor_value_see_the_missing_reading() -> None:
    pi = _make_pi()
    ticks = _ticks(pi, [(0.0, 5.0), (60.0, 5.0), (120.0, None)])
    assert ticks[1]["learning_ext"] == [5.0]
    assert ticks[1]["last_ext"] == 5.0
    assert ticks[2]["learning_ext"] == [None]
    assert ticks[2]["last_ext"] is None


def test_returning_outdoor_temperature_leaves_degraded() -> None:
    pi = _make_pi()
    ticks = _ticks(pi, [(0.0, 5.0), (60.0, None), (DAY_S, None), (DAY_S + 60.0, 4.0)])
    assert ticks[2]["regime"] == GovernanceRegime.DEGRADED
    assert ticks[3]["regime"] != GovernanceRegime.DEGRADED
    assert ticks[3]["ff_ext"] == [4.0]
    assert ticks[3]["trim_applied"] is True


def test_ownership_snapshot_excludes_a_trim_that_is_not_applied() -> None:
    pi = _make_pi()
    _ticks(pi, [(0.0, None), (60.0, None)])
    pi._ff_trim.u_ff_trim = 0.05
    snapshot = pi._build_command_ownership_snapshot(VThermHvacMode_HEAT)
    assert snapshot.u_ff_visible == 0.0
    assert snapshot.u_ff3 == 0.0
    assert snapshot.trim_stored == pytest.approx(0.05)
    assert "ff3" not in snapshot.constraint_flags
