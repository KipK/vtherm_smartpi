"""
AB Estimator for Smart-PI.
Online robust estimator for thermal model parameters a and b.

Model: dT/dt = a*u - b*(T_int - T_ext)
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from .ab_aggregator import ab_publish
from .ab_drift import (
    DriftChannelState,
    DriftDecision,
    append_drift_candidate,
    clear_channel_state,
    compute_recenter_virtual_center,
    deserialize_channel_state,
    detect_persistent_drift,
    robust_mad,
    robust_median,
    serialize_channel_state,
)
from .const import (
    AB_A_SOFT_GATE_MIN_B,
    AB_B_CONVERGENCE_MAD_RATIO,
    AB_B_CONVERGENCE_MIN_BHIST,
    AB_B_CONVERGENCE_MIN_SAMPLES,
    AB_B_CONVERGENCE_RANGE_RATIO,
    AB_HISTORY_SIZE,
    AB_MAD_K,
    AB_MAD_SIGMA_MULT,
    AB_DRIFT_BUFFER_MAXLEN,
    AB_DRIFT_MAX_BUFFER_MAD_FACTOR,
    AB_DRIFT_MAD_FLOOR_ABS,
    AB_DRIFT_MIN_COUNT,
    AB_DRIFT_MIN_SHIFT_FACTOR,
    AB_DRIFT_RECENTER_ALPHA,
    AB_DRIFT_RECENTER_MAX_CYCLES,
    AB_DRIFT_RECENTER_STEP_MAX_FACTOR,
    AB_DRIFT_SEQ_GAP_MAX,
    AB_MIN_POINTS_FOR_PUBLISH,
    AB_MIN_SAMPLES_A,
    AB_MIN_SAMPLES_A_CONVERGED,
    AB_MIN_SAMPLES_B,
    AB_VAL_TOLERANCE,
    AB_WMED_ALPHA,
    AB_WMED_PLATEAU_N,
    AB_WMED_R,
    B_STABILITY_MAD_RATIO_MAX,
    DELTA_MIN_OFF,
    DELTA_MIN_ON,
    DT_DERIVATIVE_MIN_ABS,
    OLS_MIN_JUMPS,
    OLS_T_MIN,
    U_OFF_MAX,
    U_ON_MIN,
    clamp,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TauReliability:
    """Result of tau (time constant) reliability check."""
    reliable: bool
    tau_min: float  # minutes (min of candidates used)


class ABEstimator:
    """
    Robust Online Estimator for a and b using Continuous approach:

    Model: dT/dt = a*u - b*(T_int - T_ext)

    1. OLS is used for dT/dt calculation over a sliding window.
    2. Median + MAD is used for robust a and b parameter estimation from history.
    """

    def __init__(self, a_init: float = 0.0005, b_init: float = 0.0010):
        self.A_INIT = a_init
        self.B_INIT = b_init

        self.a = a_init
        self.b = b_init

        # Robust bounds
        self.A_MIN: float = 1e-5
        self.A_MAX: float = 0.5
        self.B_MIN: float = 1e-5
        self.B_MAX: float = 0.05

        # --- Strategy: Median + MAD (Robust) ---
        # Raw measurement history
        self.a_meas_hist: Deque[float] = deque(maxlen=AB_HISTORY_SIZE) # Keep last 31
        self.b_meas_hist: Deque[float] = deque(maxlen=AB_HISTORY_SIZE)

        # Stability tracking for a and b (tau) - used for reliability check
        self._b_hat_hist: Deque[float] = deque(maxlen=20)
        self._a_hat_hist: Deque[float] = deque(maxlen=20)

        # Counters
        self.learn_ok_count = 0  # Total successful updates
        self.learn_ok_count_a = 0
        self.learn_ok_count_b = 0
        self.learn_skip_count = 0
        self.learn_last_reason: Optional[str] = "init"
        self._learn_seq = 0

        # Diagnostics
        self.diag_dTdt_method: str = "init"

        self.diag_b_mad_over_med: Optional[float] = None
        self.diag_a_mad_over_med: Optional[float] = None

        # Aggregation diagnostics
        self.diag_ab_bootstrap: bool = False
        self.diag_ab_points: int = 0
        self.diag_ab_mode_effective: str = "init"
        self.diag_a_last_reason: str = "init"
        self.diag_b_last_reason: str = "init"

        # Persistent drift state per parameter
        self.a_drift = DriftChannelState()
        self.b_drift = DriftChannelState()

    def reset(self) -> None:
        """Reset learned parameters and history to initial values."""
        self.a = self.A_INIT
        self.b = self.B_INIT
        self.learn_ok_count = 0
        self.learn_ok_count_a = 0
        self.learn_ok_count_b = 0
        self.learn_skip_count = 0
        self.learn_last_reason = "reset"
        self._learn_seq = 0
        self._b_hat_hist.clear()
        self._a_hat_hist.clear()
        self.a_meas_hist.clear()
        self.b_meas_hist.clear()
        clear_channel_state(self.a_drift)
        clear_channel_state(self.b_drift)

        self.diag_b_mad_over_med = None
        self.diag_a_mad_over_med = None
        self.diag_ab_bootstrap = False
        self.diag_ab_points = 0
        self.diag_ab_mode_effective = "init"
        self.diag_a_last_reason = "reset"
        self.diag_b_last_reason = "reset"

    # ---------- Robust helpers (Static) ----------

    @staticmethod
    def _mad(values):
        if len(values) < 2:
            return None
        med = statistics.median(values)
        try:
            return statistics.median(abs(v - med) for v in values)
        except statistics.StatisticsError:
            return None

    def _get_window(self, history: Deque[float]):
        """
        Get the learning window subset according to step logic.
        - The window size grows progressively up to the max size.
        """
        return list(history)

    @staticmethod
    def _ols_slope(x: list[float], y: list[float]) -> float | None:
        """Ordinary Least Squares slope estimator."""
        n = len(x)
        if n < 2:
            return None
        sx = sum(x)
        sy = sum(y)
        sxx = sum(xi * xi for xi in x)
        sxy = sum(xi * yi for xi, yi in zip(x, y))
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-15:
            return None
        return (n * sxy - sx * sy) / denom

    @staticmethod
    def robust_dTdt_per_min(
        samples: list[Tuple[float, float]],
        *,
        trim_start_frac: float = 0.0,
        trim_end_frac: float = 0.0,
    ) -> Tuple[float | None, str, int]:
        """
        Calculate robust dT/dt (°C/min) given a list of (t_sec, T_int).

        Uses a 2-layer validation:
        1. Jump count guardrail: reject if too few temperature level changes
        2. OLS on all points + Student t-test for slope significance

        Args:
            samples: list of (timestamp, value)
            trim_start_frac: fraction of time window to ignore at start (0.0-0.5)
            trim_end_frac: fraction of time window to ignore at end (0.0-0.5)

        Returns:
            (slope_per_min, method_used, n_points)
            slope_per_min is None if calculation impossible
        """
        if not samples or len(samples) < 6:
            return None, "insufficient_samples", len(samples)

        samples_sorted = sorted(samples, key=lambda p: p[0])

        # Optional trimming by time span (safety-clamped)
        if trim_start_frac > 0.0 or trim_end_frac > 0.0:
            t_start = samples_sorted[0][0]
            t_end = samples_sorted[-1][0]
            span = t_end - t_start

            tf_start = clamp(trim_start_frac, 0.0, 0.45)
            tf_end = clamp(trim_end_frac, 0.0, 0.45)

            t_valid_start = t_start + span * tf_start
            t_valid_end = t_end - span * tf_end

            samples_trimmed = [p for p in samples_sorted if t_valid_start <= p[0] <= t_valid_end]

            if len(samples_trimmed) < 4:
                return None, "insufficient_samples_trimmed", len(samples_trimmed)

            samples_sorted = samples_trimmed

        x = [p[0] for p in samples_sorted]
        y = [p[1] for p in samples_sorted]
        n = len(x)

        # Layer 1: Jump count guardrail
        jumps = 0
        last_v = y[0]
        for v in y[1:]:
            if v != last_v:
                jumps += 1
                last_v = v
        if jumps < OLS_MIN_JUMPS:
            return None, "too_few_jumps", jumps

        # Amplitude guard (secondary)
        amp = max(y) - min(y)
        if amp < DT_DERIVATIVE_MIN_ABS:
            return None, "low_amplitude", n

        # Layer 2: OLS on all original points + t-test
        sx = sum(x)
        sy = sum(y)
        sxx = sum(xi * xi for xi in x)
        sxy = sum(xi * yi for xi, yi in zip(x, y))

        ss_xx = sxx - (sx * sx) / n
        if ss_xx < 1e-15:
            return None, "ols_fail", n

        ss_xy = sxy - (sx * sy) / n
        b1 = ss_xy / ss_xx
        b0 = (sy - b1 * sx) / n

        # SSE = sum of squared residuals
        sse = sum((yi - (b0 + b1 * xi)) ** 2 for xi, yi in zip(x, y))

        # Effective sample count: repeated identical values do not add
        # independent information. Use distinct levels (jumps + 1) as
        # degrees of freedom to avoid artificially inflating confidence.
        n_eff = jumps + 1
        if n_eff <= 2:
            return None, "ols_fail", n

        mse = sse / (n_eff - 2)
        se_b1_sq = mse / ss_xx
        if se_b1_sq <= 0:
            # Perfect fit (no residual variance)
            return b1 * 60.0, "ols_ttest", n

        se_b1 = math.sqrt(se_b1_sq)
        if se_b1 < 1e-15:
            return b1 * 60.0, "ols_ttest", n

        t_stat = abs(b1) / se_b1
        if t_stat < OLS_T_MIN:
            return None, "slope_not_significant", n

        return b1 * 60.0, "ols_ttest", n

    def _update_mad_diag(self, param_name: str, history: Deque[float]) -> None:
        """Refresh MAD diagnostics using accepted history only."""

        values = list(history)
        if len(values) < 2:
            if param_name == "a":
                self.diag_a_mad_over_med = None
            else:
                self.diag_b_mad_over_med = None
            return

        med = robust_median(values)
        mad = robust_mad(values, med)
        ratio = 0.0
        if med is not None and mad is not None and mad > AB_VAL_TOLERANCE:
            ratio = mad / (abs(med) + 1e-9)

        if param_name == "a":
            self.diag_a_mad_over_med = ratio
        else:
            self.diag_b_mad_over_med = ratio

    def _process_param_measure(
        self,
        *,
        name: str,
        value: float,
        history: Deque[float],
        drift_channel: DriftChannelState,
        min_collect_count: int,
        allow_drift: bool,
    ) -> DriftDecision:
        """Apply nominal or drift-aware gating for a measured parameter."""

        hist_values = list(history)
        if len(hist_values) + 1 < min_collect_count:
            clear_channel_state(drift_channel)
            drift_channel.last_reason = "COLLECTING"
            return DriftDecision(
                accepted=True,
                accepted_reason="COLLECTING",
                used_recenter=False,
                gate_center=None,
                candidate_center=None,
                candidate_mad=None,
                shift=None,
                side=None,
            )

        hist_center = robust_median(hist_values)
        if hist_center is None:
            clear_channel_state(drift_channel)
            drift_channel.last_reason = "NORMAL_ACCEPT"
            return DriftDecision(
                accepted=True,
                accepted_reason="NORMAL_ACCEPT",
                used_recenter=False,
                gate_center=None,
                candidate_center=None,
                candidate_mad=None,
                shift=None,
                side=None,
            )

        hist_mad = robust_mad(hist_values, hist_center)
        hist_mad_eff = max(hist_mad or 0.0, AB_DRIFT_MAD_FLOOR_ABS)

        using_virtual_gate = (
            drift_channel.state == "RECENTERING"
            and drift_channel.virtual_center is not None
        )
        if using_virtual_gate:
            gate_center = drift_channel.virtual_center
        else:
            gate_center = hist_center
        drift_channel.last_gate_center = gate_center

        gate_sigma = AB_MAD_SIGMA_MULT * AB_MAD_K * hist_mad_eff
        if abs(value - gate_center) <= gate_sigma:
            if not using_virtual_gate:
                clear_channel_state(drift_channel)
                drift_channel.last_reason = "NORMAL_ACCEPT"
                used_recenter = False
            else:
                drift_channel.reject_streak = 0
                drift_channel.recenter_cycles_left -= 1
                drift_channel.last_reason = "ACCEPTED_BY_RECENTERING"
                used_recenter = True
                if drift_channel.recenter_cycles_left <= 0:
                    clear_channel_state(drift_channel)
                    drift_channel.last_reason = "RECENTERING_FINISHED"

            return DriftDecision(
                accepted=True,
                accepted_reason=drift_channel.last_reason,
                used_recenter=used_recenter,
                gate_center=gate_center,
                candidate_center=drift_channel.last_candidate_center,
                candidate_mad=drift_channel.last_candidate_mad,
                shift=drift_channel.last_shift,
                side=drift_channel.last_side,
            )

        if not allow_drift:
            clear_channel_state(drift_channel)
            drift_channel.last_reason = (
                "REJECT_NOT_ELIGIBLE_FOR_DRIFT" if name == "a" else "REJECT_OUTLIER"
            )
            return DriftDecision(
                accepted=False,
                accepted_reason=drift_channel.last_reason,
                used_recenter=False,
                gate_center=gate_center,
                candidate_center=None,
                candidate_mad=None,
                shift=None,
                side=None,
            )

        drift_channel.reject_streak += 1
        append_drift_candidate(
            channel=drift_channel,
            value=value,
            seq=self._learn_seq,
            hist_center=hist_center,
        )

        drift_ok, drift_diag = detect_persistent_drift(
            channel=drift_channel,
            hist_center=hist_center,
            hist_mad_eff=hist_mad_eff,
            min_count=AB_DRIFT_MIN_COUNT,
            max_buffer_mad_factor=AB_DRIFT_MAX_BUFFER_MAD_FACTOR,
            min_shift_factor=AB_DRIFT_MIN_SHIFT_FACTOR,
            seq_gap_max=AB_DRIFT_SEQ_GAP_MAX,
        )

        if not drift_ok:
            drift_channel.state = (
                "DRIFT_SUSPECTED" if len(drift_channel.drift_buffer) > 0 else "NORMAL"
            )
            drift_channel.last_reason = drift_diag["reason"]
            return DriftDecision(
                accepted=False,
                accepted_reason=drift_diag["reason"],
                used_recenter=False,
                gate_center=gate_center,
                candidate_center=drift_channel.last_candidate_center,
                candidate_mad=drift_channel.last_candidate_mad,
                shift=drift_channel.last_shift,
                side=drift_channel.last_side,
            )

        candidate_center = float(drift_diag["cand_center"])
        candidate_mad = float(drift_diag["cand_mad"])
        shift = float(drift_diag["shift"])
        side = int(drift_diag["side"])
        virtual_center = compute_recenter_virtual_center(
            current_center=gate_center,
            candidate_center=candidate_center,
            hist_mad_eff=hist_mad_eff,
            alpha=AB_DRIFT_RECENTER_ALPHA,
            step_max_factor=AB_DRIFT_RECENTER_STEP_MAX_FACTOR,
        )

        drift_channel.state = "RECENTERING"
        drift_channel.virtual_center = virtual_center
        drift_channel.recenter_cycles_left = AB_DRIFT_RECENTER_MAX_CYCLES
        drift_channel.last_candidate_center = candidate_center
        drift_channel.last_candidate_mad = candidate_mad
        drift_channel.last_shift = shift
        drift_channel.last_side = side
        drift_channel.last_gate_center = virtual_center
        drift_channel.last_reason = "PERSISTENT_DRIFT_DETECTED"

        if abs(value - virtual_center) <= gate_sigma:
            drift_channel.reject_streak = 0
            drift_channel.recenter_cycles_left -= 1
            drift_channel.last_reason = "ACCEPTED_BY_RECENTERING"
            return DriftDecision(
                accepted=True,
                accepted_reason="ACCEPTED_BY_RECENTERING",
                used_recenter=True,
                gate_center=virtual_center,
                candidate_center=candidate_center,
                candidate_mad=candidate_mad,
                shift=shift,
                side=side,
            )

        return DriftDecision(
            accepted=False,
            accepted_reason="PERSISTENT_DRIFT_DETECTED",
            used_recenter=True,
            gate_center=virtual_center,
            candidate_center=candidate_center,
            candidate_mad=candidate_mad,
            shift=shift,
            side=side,
        )

    def _apply_published_value(
        self,
        *,
        param_name: str,
        history: Deque[float],
        value: float,
        default_value: float,
    ) -> float:
        """Append a real measurement and publish the aggregated parameter."""

        history.append(value)
        new_value, ab_diag = ab_publish(
            history,
            plateau_n=AB_WMED_PLATEAU_N,
            alpha=AB_WMED_ALPHA,
            r=AB_WMED_R,
            min_points_for_publish=AB_MIN_POINTS_FOR_PUBLISH,
            default_value=default_value,
        )
        self.diag_ab_bootstrap = ab_diag.get("ab_bootstrap", False)
        self.diag_ab_points = ab_diag.get("ab_points", len(history))
        self.diag_ab_mode_effective = ab_diag.get("ab_mode_effective", "weighted_median")
        self._update_mad_diag(param_name, history)
        return new_value

    # ---------- Main learning ----------

    def learn(
        self,
        dT_int_per_min: float,
        u: float,
        t_int: float,
        t_ext: float,
        *,
        max_abs_dT_per_min: float = 0.35,
    ) -> None:
        """
        Update (a,b) using Median + MAD approach.

        Model: dT/dt = a*u - b*(T_int - T_ext)
        """
        dTdt = float(dT_int_per_min)
        delta = float(t_int - t_ext)

        # 1. Reject gross physics outliers
        if abs(dTdt) > max_abs_dT_per_min:
            self.learn_skip_count += 1
            self.learn_last_reason = "skip: slope outlier"
            return

        # ---------- OFF phase: learn b ----------
        # dT/dt = -b * delta  =>  b = -dT/dt / delta
        if u < U_OFF_MAX:
            if abs(delta) < DELTA_MIN_OFF:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: b delta too small"
                return

            b_meas = -dTdt / delta
            if b_meas <= 0:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: b_meas <= 0"
                self.diag_b_last_reason = self.learn_last_reason
                return

            self._learn_seq += 1
            decision = self._process_param_measure(
                name="b",
                value=b_meas,
                history=self.b_meas_hist,
                drift_channel=self.b_drift,
                min_collect_count=AB_MIN_SAMPLES_B,
                allow_drift=True,
            )
            self.diag_b_last_reason = decision.accepted_reason

            if decision.accepted_reason == "COLLECTING":
                self.b_meas_hist.append(b_meas)
                self.learn_skip_count += 1
                self.learn_last_reason = (
                    f"skip: collecting b meas ({len(self.b_meas_hist)}/{AB_MIN_SAMPLES_B})"
                )
                self.diag_b_last_reason = self.learn_last_reason
                self._update_mad_diag("b", self.b_meas_hist)
                return

            if not decision.accepted:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: b_meas outlier"
                self.diag_b_last_reason = decision.accepted_reason
                self._update_mad_diag("b", self.b_meas_hist)
                return

            new_b = self._apply_published_value(
                param_name="b",
                history=self.b_meas_hist,
                value=b_meas,
                default_value=self.B_INIT,
            )
            new_b = clamp(new_b, self.B_MIN, self.B_MAX)

            self.b = new_b
            self._b_hat_hist.append(new_b)
            self.learn_ok_count += 1
            self.learn_ok_count_b += 1
            self.learn_last_reason = f"learned b ({self.diag_ab_mode_effective})"
            self.diag_b_last_reason = decision.accepted_reason
            return

        # ---------- ON phase: learn a ----------
        # dT/dt = a*u - b*delta  =>  a = (dT/dt + b*delta) / u
        if u > U_ON_MIN:
            if abs(delta) < DELTA_MIN_ON:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: a delta too small"
                return

            if self.learn_ok_count_b < AB_A_SOFT_GATE_MIN_B:
                self.learn_skip_count += 1
                self.learn_last_reason = (
                    f"skip: a blocked (b insufficient, "
                    f"{self.learn_ok_count_b}/{AB_A_SOFT_GATE_MIN_B} b samples)"
                )
                return

            min_a_samples = (
                AB_MIN_SAMPLES_A_CONVERGED
                if self.b_converged_for_a()
                else AB_MIN_SAMPLES_A
            )

            a_meas = (dTdt + self.b * delta) / u
            if a_meas <= 0:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: a_meas <= 0"
                self.diag_a_last_reason = self.learn_last_reason
                return

            self._learn_seq += 1
            decision = self._process_param_measure(
                name="a",
                value=a_meas,
                history=self.a_meas_hist,
                drift_channel=self.a_drift,
                min_collect_count=min_a_samples,
                allow_drift=self.b_converged_for_a(),
            )
            self.diag_a_last_reason = decision.accepted_reason

            if decision.accepted_reason == "COLLECTING":
                self.a_meas_hist.append(a_meas)
                self.learn_skip_count += 1
                self.learn_last_reason = (
                    f"skip: collecting a meas ({len(self.a_meas_hist)}/{min_a_samples})"
                )
                self.diag_a_last_reason = self.learn_last_reason
                self._update_mad_diag("a", self.a_meas_hist)
                return

            if not decision.accepted:
                self.learn_skip_count += 1
                self.learn_last_reason = "skip: a_meas outlier"
                self.diag_a_last_reason = decision.accepted_reason
                self._update_mad_diag("a", self.a_meas_hist)
                return

            new_a = self._apply_published_value(
                param_name="a",
                history=self.a_meas_hist,
                value=a_meas,
                default_value=self.A_INIT,
            )
            new_a = clamp(new_a, self.A_MIN, self.A_MAX)

            self.a = new_a
            self._a_hat_hist.append(new_a)
            self.learn_ok_count += 1
            self.learn_ok_count_a += 1
            self.learn_last_reason = f"learned a ({self.diag_ab_mode_effective})"
            self.diag_a_last_reason = decision.accepted_reason
            return

        self.learn_skip_count += 1
        self.learn_last_reason = "skip: u mid-range"

    def tau_reliability(self) -> TauReliability:
        """
        Check if tau (1/b) is statistically stable and within bounds.
        """
        # Enough updates?
        if self.learn_ok_count_b < 5:
            return TauReliability(reliable=False, tau_min=9999.0)

        if len(self._b_hat_hist) < 5:
            return TauReliability(reliable=False, tau_min=9999.0)

        med_b = statistics.median(self._b_hat_hist)
        mad_b = self._mad(self._b_hat_hist)

        if mad_b is None or med_b <= 0:
            return TauReliability(reliable=False, tau_min=9999.0)

        # Stability check: Relative dispersion of b estimates (0.60 threshold)
        if (mad_b / med_b) > B_STABILITY_MAD_RATIO_MAX:
            return TauReliability(reliable=False, tau_min=9999.0)

        # Value bounds check
        if med_b < self.B_MIN or med_b > self.B_MAX:
            return TauReliability(reliable=False, tau_min=9999.0)

        tau = 1.0 / med_b
        # tau bounds are implicitly enforced by B_MIN/B_MAX
        return TauReliability(reliable=True, tau_min=tau)

    def b_converged_for_a(self) -> bool:
        """
        Return True if b is stable enough to enable a learning.
        """
        if self.learn_ok_count_b < AB_B_CONVERGENCE_MIN_SAMPLES:
            return False

        if len(self._b_hat_hist) < AB_B_CONVERGENCE_MIN_BHIST:
            return False

        recent = list(self._b_hat_hist)
        med_b = statistics.median(recent)
        if med_b <= 0:
            return False

        mad_b = self._mad(recent)
        if mad_b is None:
            return False

        if (mad_b / med_b) > AB_B_CONVERGENCE_MAD_RATIO:
            return False

        last_5 = recent[-5:] if len(recent) >= 5 else recent
        range_5 = max(last_5) - min(last_5)
        if (range_5 / med_b) > AB_B_CONVERGENCE_RANGE_RATIO:
            return False

        return True

    def save_state(self) -> dict:
        """Save state for persistence."""
        return {
            "a": self.a,
            "b": self.b,
            "learn_ok_count": self.learn_ok_count,
            "learn_ok_count_a": self.learn_ok_count_a,
            "learn_ok_count_b": self.learn_ok_count_b,
            "learn_skip_count": self.learn_skip_count,
            "a_meas_hist": list(self.a_meas_hist),
            "b_meas_hist": list(self.b_meas_hist),
            "a_hat_hist": list(self._a_hat_hist),
            "b_hat_hist": list(self._b_hat_hist),
            "learn_seq": self._learn_seq,
            "a_drift": serialize_channel_state(self.a_drift),
            "b_drift": serialize_channel_state(self.b_drift),
        }

    def load_state(self, state: dict) -> None:
        """Restore state."""
        if not state:
            return
        self.a = float(state.get("a", self.A_INIT))
        self.b = float(state.get("b", self.B_INIT))
        self.learn_ok_count = int(state.get("learn_ok_count", 0))
        self.learn_ok_count_a = int(state.get("learn_ok_count_a", 0))
        self.learn_ok_count_b = int(state.get("learn_ok_count_b", 0))
        self.learn_skip_count = int(state.get("learn_skip_count", 0))
        self._learn_seq = int(state.get("learn_seq", 0))

        amh = state.get("a_meas_hist", [])
        self.a_meas_hist = deque(amh, maxlen=AB_HISTORY_SIZE)
        bmh = state.get("b_meas_hist", [])
        self.b_meas_hist = deque(bmh, maxlen=AB_HISTORY_SIZE)

        # Restore filtered histories for tau reliability
        a_hat = state.get("a_hat_hist", [])
        self._a_hat_hist = deque(a_hat, maxlen=20)
        b_hat = state.get("b_hat_hist", [])
        self._b_hat_hist = deque(b_hat, maxlen=20)
        self.a_drift = deserialize_channel_state(
            state.get("a_drift", {}),
            buffer_maxlen=AB_DRIFT_BUFFER_MAXLEN,
        )
        self.b_drift = deserialize_channel_state(
            state.get("b_drift", {}),
            buffer_maxlen=AB_DRIFT_BUFFER_MAXLEN,
        )
        self._update_mad_diag("a", self.a_meas_hist)
        self._update_mad_diag("b", self.b_meas_hist)
