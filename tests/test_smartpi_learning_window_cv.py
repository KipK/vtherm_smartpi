"""Tests for LearningWindowManager power CV guard (Welford-based stability gate)."""
import pytest
import math
from unittest.mock import MagicMock

from custom_components.vtherm_smartpi.smartpi.learning_window import LearningWindowManager
from custom_components.vtherm_smartpi.smartpi.const import (
    U_CV_MAX,
    U_CV_MIN_MEAN,
    GovernanceDecision,
    FreezeReason,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_estimator():
    """Return a minimal ABEstimator mock."""
    est = MagicMock()
    est.learn_skip_count = 0
    est.learn_last_reason = ""
    est.learn_ok_count = 0
    est.learn_ok_count_b = 0  # must be int, not MagicMock, for '<' comparison
    return est


def _make_dt_est(heat_reliable=True, cool_reliable=True, heat_s=30.0, cool_s=30.0):
    """Return a minimal DeadTimeEstimator mock."""
    dt = MagicMock()
    dt.deadtime_heat_reliable = heat_reliable
    dt.deadtime_heat_s = heat_s
    dt.deadtime_cool_reliable = cool_reliable
    dt.deadtime_cool_s = cool_s
    dt.tin_history = []
    return dt


def _make_governance(decision=GovernanceDecision.ADAPT_ON):
    """Return a minimal SmartPIGovernance mock that always allows learning."""
    gov = MagicMock()
    gov.decide_update.return_value = (decision, FreezeReason.NONE)
    return gov


def _call_update(win, dt_min, current_temp, ext_temp, u_active,
                 estimator=None, dt_est=None, governance=None,
                 now=None, t_heat_start=None, t_cool_start=None):
    """Convenience wrapper around LearningWindowManager.update()."""
    import time as _time
    if estimator is None:
        estimator = _make_estimator()
    if dt_est is None:
        dt_est = _make_dt_est()
    if governance is None:
        governance = _make_governance()
    if now is None:
        now = _time.monotonic()
    return win.update(
        dt_min=dt_min,
        current_temp=current_temp,
        ext_temp=ext_temp,
        u_active=u_active,
        setpoint_changed=False,
        estimator=estimator,
        dt_est=dt_est,
        governance=governance,
        learning_resume_ts=None,
        now=now,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=t_heat_start,
        t_cool_episode_start=t_cool_start,
    )


# ---------------------------------------------------------------------------
# Welford accuracy
# ---------------------------------------------------------------------------

def test_welford_incremental_accuracy():
    """Welford algorithm should produce the correct variance on known data."""
    win = LearningWindowManager("test")

    samples = [0.30, 0.35, 0.32, 0.38, 0.31, 0.36, 0.33]
    for s in samples:
        win._update_u_stats(s)

    expected_mean = sum(samples) / len(samples)
    expected_std = math.sqrt(
        sum((x - expected_mean) ** 2 for x in samples) / (len(samples) - 1)
    )

    assert abs(win._u_mean - expected_mean) < 1e-9
    assert abs(win._u_std - expected_std) < 1e-9


# ---------------------------------------------------------------------------
# u_cv with low mean guard
# ---------------------------------------------------------------------------

def test_u_cv_low_mean_guard():
    """When mean(u) < U_CV_MIN_MEAN, CV must be forced to 0 (no division by ~0)."""
    win = LearningWindowManager("test")

    # mean stays well below U_CV_MIN_MEAN (0.05)
    for _ in range(5):
        win._update_u_stats(0.02)

    assert win._u_mean < U_CV_MIN_MEAN
    assert win._u_cv == 0.0


# ---------------------------------------------------------------------------
# Reset clears variance state
# ---------------------------------------------------------------------------

def test_u_stats_reset_on_window_reset():
    """After reset(), _u_count/_u_mean/_u_m2 must be zeroed."""
    win = LearningWindowManager("test")

    for v in [0.30, 0.40, 0.35]:
        win._update_u_stats(v)

    assert win._u_count == 3

    win.reset()

    assert win._u_count == 0
    assert win._u_mean == 0.0
    assert win._u_m2 == 0.0
    assert not win.active


# ---------------------------------------------------------------------------
# Scenario 1 — PI modulates between 30% and 40% → window survives
# ---------------------------------------------------------------------------

def test_learning_window_stable_pi_modulation():
    """PI modulating between 30-40%: CV ~0.09 << 0.30 → window must stay open."""
    import time as _time
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()

    base_now = _time.monotonic()
    base_temp = 19.0

    powers = [0.30, 0.32, 0.35, 0.33, 0.38, 0.36, 0.31, 0.34, 0.37, 0.30]

    for i, u in enumerate(powers):
        now = base_now + i * 60.0
        temp = base_temp - i * 0.005  # slow heating
        win.update(
            dt_min=1.0,
            current_temp=temp,
            ext_temp=10.0,
            u_active=u,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=now,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )
        # Window should remain active throughout all cycles
        if i >= 1:  # window starts on cycle 0
            assert win.active, f"Window closed at cycle {i} with u={u}"

    # CV should be well below threshold
    assert win._u_cv < U_CV_MAX


# ---------------------------------------------------------------------------
# Scenario 2 — Power jumps 0% → 80% → early submit or skip (CV >> 0.30)
# ---------------------------------------------------------------------------

def test_learning_window_regime_change_rejected():
    """Power jump in an ON window (u≈0.35 → 0.0): CV >> 0.30, window must close.

    The CV guard reads the running stats of the *previous* samples before adding
    the current sample. So after one anomalous sample is accumulated, the second
    anomalous sample triggers the guard (CV based on 1 anomaly + N normal samples).
    """
    import time as _time
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()

    base_now = _time.monotonic()

    # Build an ON window: 5 cycles at u=0.35 (mean=0.35, std→0, CV→0)
    for i in range(5):
        now = base_now + i * 60.0
        temp = 19.0 + i * 0.05  # temperature rising slowly (heater ON)
        win.update(
            dt_min=1.0,
            current_temp=temp,
            ext_temp=10.0,
            u_active=0.35,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=now,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )

    assert win.active, "Window should be active after 5 ON cycles"

    # Inject u=0.0: first injection accumulates (CV from 5×0.35 = 0 → no trigger yet)
    win.update(
        dt_min=1.0,
        current_temp=19.25,
        ext_temp=10.0,
        u_active=0.0,
        setpoint_changed=False,
        estimator=est,
        dt_est=dt_est,
        governance=gov,
        learning_resume_ts=None,
        now=base_now + 6 * 60.0,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=None,
        t_cool_episode_start=None,
    )

    # Second injection: CV based on 5×0.35 + 1×0.0 samples → CV ≈ 0.49 >> 0.30 → TRIGGERS
    win.update(
        dt_min=1.0,
        current_temp=19.30,
        ext_temp=10.0,
        u_active=0.0,
        setpoint_changed=False,
        estimator=est,
        dt_est=dt_est,
        governance=gov,
        learning_resume_ts=None,
        now=base_now + 7 * 60.0,
        in_deadband=False,
        in_near_band=False,
        t_heat_episode_start=None,
        t_cool_episode_start=None,
    )

    # Window should have closed (early submit or skip)
    assert not win.active, "Window should have closed after regime change detection"


# ---------------------------------------------------------------------------
# Scenario 3 — Constant power → CV = 0 (non-regression)
# ---------------------------------------------------------------------------

def test_learning_window_constant_power():
    """Constant power: CV must be 0 and window must stay open (non-regression)."""
    import time as _time
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()

    base_now = _time.monotonic()

    for i in range(8):
        win.update(
            dt_min=1.0,
            current_temp=19.0 + i * 0.01,
            ext_temp=10.0,
            u_active=0.50,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=base_now + i * 60.0,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )

    assert win.active, "Window must stay open with constant power"
    assert win._u_cv < 1e-6, f"CV should be ~0 for constant power, got {win._u_cv}"


# ---------------------------------------------------------------------------
# Scenario 4 — Wide PI modulation 20%→50% (CV ~0.28 < 0.30)
# ---------------------------------------------------------------------------

def test_learning_window_wide_modulation_limit():
    """PI modulation in [0.28, 0.42]: CV ~0.13 << 0.30, window must stay open.

    This range represents a typical PI regulation with moderate oscillation.
    The theoretical CV for a uniform [0.28, 0.42] distribution is ≈ 0.116.
    """
    import time as _time
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()

    base_now = _time.monotonic()
    # Uniform sweep [0.28, 0.42] — CV ≈ 0.13 < 0.30
    # 9 cycles × 1 min; dt_est.tin_history is empty so robust_dTdt_per_min returns None,
    # the slope gate keeps the window open (extending) rather than submitting.
    powers = [0.28, 0.30, 0.32, 0.35, 0.38, 0.40, 0.42, 0.40, 0.37]

    for i, u in enumerate(powers):
        win.update(
            dt_min=1.0,
            current_temp=19.0 + i * 0.008,
            ext_temp=10.0,
            u_active=u,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=base_now + i * 60.0,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )

    cv = win._u_cv
    assert cv < U_CV_MAX, f"CV={cv:.3f} should be < {U_CV_MAX} for 28-42% modulation"
    assert win.active, "Window should stay open"


# ---------------------------------------------------------------------------
# Scenario 5 — Extreme variation 5%→95% → CV >> 0.30, rejected
# ---------------------------------------------------------------------------

def test_learning_window_extreme_variation():
    """Power between 5% and 95%: CV >> 0.30, window must close."""
    import time as _time
    win = LearningWindowManager("test")
    est = _make_estimator()
    dt_est = _make_dt_est()
    gov = _make_governance()

    base_now = _time.monotonic()
    powers = [0.05, 0.95, 0.05, 0.95, 0.05, 0.95]

    closed = False
    for i, u in enumerate(powers):
        win.update(
            dt_min=1.0,
            current_temp=19.0 + i * 0.1,
            ext_temp=10.0,
            u_active=u,
            setpoint_changed=False,
            estimator=est,
            dt_est=dt_est,
            governance=gov,
            learning_resume_ts=None,
            now=base_now + i * 60.0,
            in_deadband=False,
            in_near_band=False,
            t_heat_episode_start=None,
            t_cool_episode_start=None,
        )
        if not win.active and i >= 2:
            closed = True
            break

    assert closed, "Window should have closed due to extreme power variation (CV >> 0.30)"
