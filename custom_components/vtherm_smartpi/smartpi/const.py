"""
Constants and Enums for Smart-PI Algorithm.
"""
from enum import Enum
import logging

_LOGGER = logging.getLogger(__name__)

def clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x into [lo, hi]."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x

class SmartPIPhase(str, Enum):
    """Phases of the Smart-PI algorithm."""
    HYSTERESIS = "Hysteresis"  # Learning phase with ON/OFF control
    STABLE = "Stable"          # PI control with reliable model
    CALIBRATION = "Calibration" # A pulse of a calibration commands the output

# ########################################################################
#                      SAFETY-FIRST GOVERNANCE ENUMS                   #
# ########################################################################

class GovernanceRegime(str, Enum):
    """Physical regime detected during a calculation step."""
    WARMUP = "warmup"                  # Hysteresis / bootstrap phase
    EXCITED_STABLE = "excited_stable"  # Normal PI regulation, significant error
    NEAR_BAND = "near_band"            # Close to setpoint, weak signal
    DEAD_BAND = "dead_band"            # In dead band, no action
    HOLD = "hold"                      # Integrator hold active
    PERTURBED = "perturbed"            # External disturbance (window, shedding)
    DEGRADED = "degraded"              # Outdoor temperature unavailable
    SATURATED = "saturated"            # Command at 0% or 100%


class FreezeReason(str, Enum):
    """Diagnostic reason why adaptation was frozen."""
    NONE = "none"
    # Structural
    REGIME_TRANSITION = "regime_transition"  # Cycle not homogeneous
    # Physical / external
    SENSOR_INVALID = "sensor_invalid"
    # Regime-specific
    DEAD_BAND = "dead_band"
    NEAR_BAND = "near_band"
    WARMUP = "warmup"
    HOLD = "hold"
    PERTURBED = "perturbed"
    SATURATION = "saturation"
    SYSTEM_INEFFICIENT = "system_inefficient"


class GovernanceDecision(str, Enum):
    """Decision level for parameter adaptation."""
    ADAPT_ON = "adapt_on"                # Calculation and update allowed
    FREEZE = "freeze"                    # Keep previous values
    HARD_FREEZE = "hard_freeze"          # Absolute prohibition of update
    SOFT_FREEZE_DOWN = "soft_freeze_down" # Only decrease allowed


# Governance matrix: regime -> {domain: (decision, freeze_reason)}
# Domains: 'thermal' (a/b learning), 'gains' (Kp/Ki adaptation)
GOVERNANCE_MATRIX = {
    GovernanceRegime.WARMUP: {
        "thermal": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),
        "gains": (GovernanceDecision.FREEZE, FreezeReason.WARMUP),
    },
    GovernanceRegime.EXCITED_STABLE: {
        "thermal": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),
        "gains": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),
    },
    GovernanceRegime.NEAR_BAND: {
        "thermal": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),
        "gains": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),
    },
    GovernanceRegime.DEAD_BAND: {
        "thermal": (GovernanceDecision.HARD_FREEZE, FreezeReason.DEAD_BAND),
        "gains": (GovernanceDecision.HARD_FREEZE, FreezeReason.DEAD_BAND),
    },
    GovernanceRegime.SATURATED: {
        "thermal": (GovernanceDecision.ADAPT_ON, FreezeReason.NONE),  # Allow learning
        "gains": (GovernanceDecision.FREEZE, FreezeReason.SATURATION),  # Keep gains frozen
    },
    GovernanceRegime.HOLD: {
        "thermal": (GovernanceDecision.HARD_FREEZE, FreezeReason.HOLD),
        "gains": (GovernanceDecision.SOFT_FREEZE_DOWN, FreezeReason.HOLD),
    },
    GovernanceRegime.PERTURBED: {
        "thermal": (GovernanceDecision.HARD_FREEZE, FreezeReason.PERTURBED),
        "gains": (GovernanceDecision.HARD_FREEZE, FreezeReason.PERTURBED),
    },
    GovernanceRegime.DEGRADED: {
        "thermal": (GovernanceDecision.HARD_FREEZE, FreezeReason.SENSOR_INVALID),
        "gains": (GovernanceDecision.HARD_FREEZE, FreezeReason.SENSOR_INVALID),
    },
}

# ------------------------------
# Default controller parameters
# ------------------------------

# Safe fallback gains when model is unreliable
KP_SAFE = 0.55
KI_SAFE = 0.010

# Allowed ranges for computed gains
KP_MIN = 0.10
KP_MAX = 5.0
KI_MIN = 0.001
KI_MAX = 0.050

# IMC/SIMC gain tuning constants
GAIN_LAMBDA_DEADTIME_FACTOR = 3.0
GAIN_LAMBDA_VALVE_DEADTIME_FACTOR = 4.0
GAIN_LAMBDA_CYCLE_FACTOR = 2.0
GAIN_LAMBDA_MIN_MIN = 5.0
GAIN_TI_LAMBDA_FACTOR = 4.0      # SIMC: Ti = min(tau, 4 (lambda + L))
# A saturated output may unwind the integral toward zero within this many
# observed sensor steps of the setpoint (quantization trap).
NEAR_SETPOINT_UNWIND_STEPS = 1.5


# Anti-windup / integrator behavior
MAX_STEP_PER_MINUTE = 0.25  # max output change per minute (rate limit)

# Setpoint step boost: faster rate limit when setpoint changes significantly
# This allows quick power ramp-up when user increases setpoint
SETPOINT_BOOST_THRESHOLD = 0.3   # min setpoint change (°C) to trigger boost
SETPOINT_BOOST_ERROR_MIN = 0.3   # min error (°C) to keep boost active
SETPOINT_BOOST_RATE = 0.50       # boosted rate limit (/min) vs MAX_STEP_PER_MINUTE normal

# Setpoint change handling (mode change vs adjustment)
# - Large change (>= threshold): mode change (eco ↔ comfort) -> restart error history, integral preserved
# - Small change (< threshold): integral preserved, servo via P + FF
SETPOINT_MODE_DELTA_C = 0.5      # °C threshold for mode change detection
OVERSHOOT_I_CLAMP_EPS_C = 0.04  # Guard band below setpoint where integral cannot increase (°C)

LEARNING_PAUSE_RESUME_MIN = 20  # Pause learning after resume (window close, etc.) to allow stabilization. NB: Corrected duplication in original file comments

# Periodic recalculation interval (seconds) for SmartPI
# This ensures the rate-limit progresses even when temperature sensors don't update frequently
SMARTPI_RECALC_INTERVAL_SEC = 60

# Max continuous pause duration (minutes) before explicitly purging PI memory
PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN = 5760.0  # 96 hours (4 days)

# --- Hysteresis Mode (during learning phase) ---
HYST_UPPER_C = 0.5  # ON -> OFF threshold (°C above setpoint)
HYST_LOWER_C = 0.3  # OFF -> ON threshold (°C below setpoint)

# Default deadband around setpoint (°C)
DEFAULT_DEADBAND_C = 0.05

# Source reported by the controller when the integral is held because the room
# is inside the deadband hysteresis shell (inside the deadband but not in its core)
INTEGRAL_HOLD_SOURCE_DEADBAND_SHELL = "deadband_hysteresis_shell"

# Absolute hysteresis for deadband exit (reduces oscillations at boundary)
# Enter deadband at |e| < deadband_c, exit only when |e| > deadband_c + hysteresis
# Using absolute value (not multiplicative) ensures consistent behavior across
# different deadband configurations and typical sensor noise levels.
DEADBAND_HYSTERESIS = 0.025

# --- Near-band asymmetry ---
# Near-band asymmetry:
# - below setpoint: use configured near_band_deg (self.near_band_deg)
# - above setpoint: scale it down with a factor
NEAR_BAND_ABOVE_FACTOR = 0.40
NEAR_BAND_HYSTERESIS_C = 0.05
# The guard cut resets at least this many observed sensor steps below its
# threshold, so that a reading flickering between two levels cannot re-trigger it.
GUARD_CUT_RESET_STEPS = 1.5

RECOVERY_ERROR_FLOOR_DEADBAND_RATIO = 2.0  # Residual error (multiples of the deadband) under which a recovery counts as closed

# Anticipation of the measured temperature (heating and cooling)
ANTICIPATION_MAX_C = 1.0                 # °C — largest correction added to the measured temperature
ANTICIPATION_MIN_WINDOW_S = 600.0        # s — shortest window of the least-squares slope
ANTICIPATION_MAX_WINDOW_S = 1800.0       # s — longest slope window: beyond 30 min the slope comes too late for long-horizon rooms

# Error filter time constant
ERROR_FILTER_TAU = 25.0 # Minutes (matches alpha ~0.35 at 10min)


# --- Robust learning / gating constants ---
# Tau stability check
B_STABILITY_MAD_RATIO_MAX = 0.60   # MAD(b) / median(b)

# --- SmartPI Robust Learning Constants ---
# Median+MAD Strategy Constants
AB_HISTORY_SIZE = 31      # Keep last 31 (ODD) values
# The bootstrap publishes the plain median of its measurements: an odd count
# tolerates as many outliers as the next even one, (N - 1) // 2.
AB_MIN_SAMPLES_B = 5      # Min samples for b publication (OFF phase)
AB_MIN_SAMPLES_A = 5      # Min samples for a publication (ON phase)
# The first a publication may take place from this count when the collected
# measurements agree (median absolute deviation within the fraction below).
AB_MIN_SAMPLES_A_CONSISTENT = 4
AB_A_CONSISTENT_MAX_REL_MAD = 0.10
AB_MAD_SIGMA_MULT = 3.0   # Outlier rejection threshold (sigma)

AB_MAD_K = 1.4826         # Sigma scaling factor for MAD
AB_VAL_TOLERANCE = 1e-12  # Small epsilon
DT_DERIVATIVE_MIN_ABS = 0.05    # Min absolute dT (°C) amplitude guard
OLS_MIN_JUMPS = 3               # Min temperature level changes for OLS validity
OLS_T_MIN = 2.5                 # Min t-statistic for slope significance
OLS_MIN_SPAN_STEPS = 1.5        # Min fitted change over the window, in sensor steps
OLS_SPAN_MIN_STEP_C = 0.05      # Floor on the sensor step for the span guard (noise margin)

# Sequential gate a->b
AB_B_CONVERGENCE_MIN_SAMPLES: int = 11
AB_B_CONVERGENCE_MAD_RATIO: float = 0.30
AB_B_CONVERGENCE_RANGE_RATIO: float = 0.10
AB_B_CONVERGENCE_MIN_BHIST: int = 5
AB_A_SOFT_GATE_MIN_B: int = 5

# --- ABEstimator weighted-median aggregation parameters ---
AB_WMED_PLATEAU_N: int = 11          # Most-recent N points assigned weight 1.0 (plateau)
AB_WMED_ALPHA: float = 1.0           # Weight factor at the start of the tail
AB_WMED_R: float = 0.85              # Geometric decay factor for tail weights

# --- ABEstimator persistent drift detection ---
AB_DRIFT_BUFFER_MAXLEN: int = 8
AB_DRIFT_MIN_COUNT: int = 6
AB_DRIFT_MAX_BUFFER_MAD_FACTOR: float = 1.5
AB_DRIFT_MIN_SHIFT_FACTOR: float = 3.0
AB_DRIFT_RECENTER_MAX_CYCLES: int = 5
AB_DRIFT_RECENTER_ALPHA: float = 0.25
AB_DRIFT_RECENTER_STEP_MAX_FACTOR: float = 2.0
AB_DRIFT_MAD_FLOOR_ABS: float = 1e-4
AB_DRIFT_SEQ_GAP_MAX: int = 3
# Relative floor of the outlier gate width: measures that agree closely must not shrink the gate to a sliver of the median.
AB_MAD_REL_FLOOR: float = 0.02

# --- SmartPI Learning Window Constants ---
# Absolute timeout for a learning window (both A and B).
# The window extends as long as the OLS slope is not yet robust, up to this limit.
DT_MAX_MIN = 240
DELTA_MIN = 0.2        # °C (Matches DELTA_MIN_ON)
U_OFF_MAX = 0.05
U_ON_MIN = 0.20
DELTA_MIN_OFF = 0.5        # °C
DELTA_MIN_ON = 0.2         # °C
AB_B_MIN_PASSIVE_GRADIENT_C = 2.0
AB_B_OUTDOOR_TARGET_MARGIN_C = 2.0

# Power coefficient of variation gate (Welford-based)
U_CV_MAX = 0.30            # Maximum accepted CV of power over the learning window
U_CV_MIN_MEAN = 0.05       # Minimum mean(u) to compute CV (avoids division by ~0)

# Integral balance pools (windows closed without a robust OLS slope).
# Each closed window contributes the segment between its first and last raw
# temperature steps in the response direction; segments are pooled per
# parameter until the quantisation-bounded relative uncertainty is reached.
AB_BALANCE_MAX_REL_UNC = 0.25         # Emission threshold (OLS t >= 2.5 accepts up to 0.40)
AB_BALANCE_DEFAULT_RESOLUTION_C = 0.1  # Sensor step assumed before any raw step is observed
AB_BALANCE_MIN_RESOLUTION_C = 0.05     # Floor on the step used for uncertainty (noise margin)
AB_BALANCE_POOL_MAX_AGE_S = 86400.0    # Segments older than this are dropped
AB_BALANCE_POOL_MAX_SEGMENTS = 12      # Oldest segments are dropped beyond this count
AB_BALANCE_MIN_SEGMENT_MIN = 15.0      # Shorter segments are dominated by noise and actuator lag

# Power steps of the a/b learning (smartpi/power_steps.py): heating episodes and their durations.
RESPONSE_LAG_HISTORY = 7               # Episode durations kept (median: cap of the active gate)
RESPONSE_LAG_MIN_ESTIMATES = 3         # Durations required before their median is used
# Heating phases after which the a learning goes on without a response model
# (the applied power is then used for the a measurements).
RESPONSE_LAG_RESOLVE_EPISODES = 4
# Power cut shorter than this between two active episodes: same heating phase.
RESPONSE_LAG_PHASE_MIN_OFF_S = 600.0
# Relative change of the response model (sum and dead time, of the former sum) that recomputes the a collection
# during the learning stage (measurements of different power models are not comparable).
RESPONSE_LAG_MODEL_CHANGE_REL = 0.5
RESPONSE_LAG_U_TOL = 0.05              # Power deviation ending a constant-power episode
# Longest stop of the regulation (restart) that can be bridged (restart_bridge.py):
# the work in progress resumes and the stop is only a gap in the observations. A
# reload of the integration or a restart of Home Assistant keeps the last
# actuator state; over a longer stop (host down, power outage, update) the
# heater power is unknown.
RESTART_BRIDGE_MAX_GAP_S = 600.0
# Same bound for the power-step episode resumed across a restart.
RESPONSE_LAG_RESTART_MAX_GAP_S = RESTART_BRIDGE_MAX_GAP_S
# Measurement events (smartpi/measure_event.py): a rest, a heating phase, its cut and the peak that follows.
EVENT_MIN_REST_S = 1800.0    # Rest before the heating, for a settled baseline
EVENT_BASELINE_SPAN_S = 3600.0  # Max rest tail used to fit the baseline
EVENT_MIN_ON_S = 600.0       # Full-power seconds required in a regulation event
EVENT_PHASE_MIN_OFF_S = RESPONSE_LAG_PHASE_MIN_OFF_S  # Shorter cut: same phase
EVENT_PEAK_BIN_S = 300.0     # Width of the bins of the peak search
EVENT_PEAK_CONFIRM_S = 1800.0  # A bin this long after the best bin confirms the peak
EVENT_PEAK_MAX_S = 4 * 3600.0  # Longest wait for the peak after the cut
# Early confirmation of a peak (smartpi/peak_search.py, a search built with a drop floor): a closed bin after the best
# one, lower than it by max(the drop floor, STOP_PEAK_DROP_RISE_FRAC of the rise since the cut).
STOP_PEAK_DROP_RISE_FRAC = 0.5
EVENT_MIN_RISE_C = 0.3       # Smallest temperature rise of a regulation event read by the response model
# Active gate cap, fraction of the median episode (the settling waits: smartpi/response_model.py).
AB_SETTLE_ACTIVE_CAP_FRAC = 0.5

# Indoor temperature histories of the dead time estimator (learning slopes and
# dead time inflection lookback). Samples are kept by age so that a learning
# window or a dead time detection (both bounded by 240 min) is covered; the
# sample cap bounds memory and also limits the covered duration when samples
# arrive on average more often than every 10 s (a sample is added on each
# value change or after 60 s).
TIN_HISTORY_RETENTION_MIN = DT_MAX_MIN + 60
# Dead time detection: the response to a power start is attributed to the
# power step within this delay.
DEADTIME_ACTIVE_TIMEOUT_S = 14400.0
# Free-running trend before an active power start: fitted on the raw samples of
# the last 30 min of the OFF period (one emitter lag after the last stop),
# with at least 15 min and 4 samples (3 per half window to compare the slopes
# of the two halves). A trend whose fitted change stays within one sensor step
# (floored at this value) is flat.
DEADTIME_PRESTART_WINDOW_S = 1800.0
DEADTIME_PRESTART_MIN_SPAN_S = 900.0
DEADTIME_PRESTART_MIN_SAMPLES = 4
DEADTIME_PRESTART_MIN_HALF_SAMPLES = 3
DEADTIME_PRESTART_STEP_FLOOR_C = 0.01
# A trend in the direction of the response counts as rising (or falling, in
# cool) when its fitted change over the window reaches this many sensor steps.
# One step may be a single quantisation toggle, two steps is a real move. Such
# a trend, like a window too short to tell any trend, is weak evidence: a
# measurement shorter than the published dead time is then ignored instead of
# lowering it, while a longer one is kept (a room warming before the start can
# only make the response look earlier, never later).
DEADTIME_PRESTART_RISE_MIN_STEPS = 2.0
TIN_HISTORY_MAX_SAMPLES = 1800


# --- SmartPI Near Band Defaults ---
DEFAULT_NEAR_BAND_DEG = 0.40
NEAR_BAND_TAPER_KP_MIN = 0.75
NEAR_BAND_TAPER_KI_MIN = 0.45


# --- Calibration ---
CALIBRATION_MAX_S = 48.0 * 3600.0  # longest calibration, from its first calculation

# --- AB confidence ---

class ABConfidenceState(str, Enum):
    """Confidence state for a,b model parameters."""
    AB_BOOTSTRAP = "ab_bootstrap"
    AB_OK = "ab_ok"
    AB_DEGRADED = "ab_degraded"
    AB_BAD = "ab_bad"


# AB confidence thresholds keep full confidence stricter than bootstrap exit.
AB_CONFIDENCE_MIN_SAMPLES_A = 11
AB_CONFIDENCE_MIN_SAMPLES_B = 11

# Deadband output shaping
DEADBAND_EDGE_PERSISTENCE = 2

# --- Adaptive T_int Filter ---
ENABLE_ADAPTIVE_TINT_FILTER = True
TINT_LP_ALPHA = 0.2              # EMA smoothing factor (0..1)
TINT_SIGMA_WINDOW = 30           # Rolling window size for sigma estimation
TINT_SIGMA_MIN = 0.02            # Floor on sigma (degC)
TINT_ADAPTIVE_K = 3.0            # Threshold multiplier: publish when |delta| > k * sigma
