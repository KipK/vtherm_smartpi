"""Tests for LearningWindowManager near_band thermal learning behaviour.

near_band does NOT block thermal (a/b) learning — the governance matrix sets
ADAPT_ON for the thermal domain in NEAR_BAND regime.  Quality gates (U_ON_MIN,
OLS t-test, power CV) are the sole filters for A learning quality.
"""
import pytest
from unittest.mock import MagicMock

from custom_components.versatile_thermostat.smartpi.learning_window import LearningWindowManager
from custom_components.versatile_thermostat.smartpi.const import (
    GovernanceDecision,
    FreezeReason,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_estimator():
    est = MagicMock()
    est.learn_skip_count = 0
    est.learn_last_reason = ""
    est.learn_ok_count = 0
    est.learn_ok_count_b = 0
    return est


def _make_dt_est():
    dt = MagicMock()
    dt.deadtime_heat_reliable = False
    dt.deadtime_heat_s = None
    dt.deadtime_cool_reliable = False
    dt.deadtime_cool_s = None
    dt.tin_history = []
    return dt


def _make_governance(decision, reason=FreezeReason.NONE):
    gov = MagicMock()
    gov.decide_update.return_value = (decision, reason)
    return gov


def _call_update(win, u_active, governance, estimator, now=1000.0):
    return win.update(
        dt_min=1.0,
        current_temp=18.5,
        ext_temp=11.0,
        u_active=u_active,
        setpoint_changed=False,
        estimator=estimator,
        dt_est=_make_dt_est(),
        governance=governance,
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=True,
        t_heat_episode_start=None,
        t_cool_episode_start=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNearBandThermalLearning:
    """near_band governance returns ADAPT_ON for thermal: no governance block."""

    def test_near_band_adapt_on_does_not_block(self):
        """ADAPT_ON from governance must not skip learning regardless of u."""
        win = LearningWindowManager("test")
        est = _make_estimator()
        gov = _make_governance(GovernanceDecision.ADAPT_ON, FreezeReason.NONE)

        _call_update(win, u_active=0.50, governance=gov, estimator=est)

        assert "governance" not in est.learn_last_reason

    def test_other_hard_freeze_still_blocks(self):
        """HARD_FREEZE for non-near_band reasons must still block learning."""
        for reason in (FreezeReason.DEAD_BAND, FreezeReason.HOLD, FreezeReason.PERTURBED):
            win = LearningWindowManager("test")
            est = _make_estimator()
            gov = _make_governance(GovernanceDecision.HARD_FREEZE, reason)

            _call_update(win, u_active=0.70, governance=gov, estimator=est)

            assert est.learn_skip_count == 1, f"Expected block for reason {reason}"
            assert "governance" in est.learn_last_reason

    def test_freeze_decision_blocks(self):
        """FREEZE decision (any reason) must block learning."""
        win = LearningWindowManager("test")
        est = _make_estimator()
        gov = _make_governance(GovernanceDecision.FREEZE, FreezeReason.HOLD)

        _call_update(win, u_active=0.50, governance=gov, estimator=est)

        assert est.learn_skip_count == 1
        assert "governance" in est.learn_last_reason
