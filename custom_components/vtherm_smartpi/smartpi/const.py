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
    CALIBRATION = "Calibration" # Forced calibration cycle in progress

class SmartPICalibrationPhase(str, Enum):
    """Phases of the Smart-PI forced calibration."""
    IDLE = "Idle"
    COOL_DOWN = "CoolDown"
    HEAT_UP = "HeatUp"
    COOL_DOWN_FINAL = "CoolDownFinal"

class SmartPICalibrationResult(str, Enum):
    """Resolution status of a Smart-PI forced calibration cycle."""
    PENDING = "Pending"
    SUCCESS = "Success"
    CANCELLED = "Cancelled"

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
    DEGRADED = "degraded"              # Sensor absent, deadtime unknown
    SATURATED = "saturated"            # Command at 0% or 100%


class FreezeReason(str, Enum):
    """Diagnostic reason why adaptation was frozen."""
    NONE = "none"
    # Structural
    REGIME_TRANSITION = "regime_transition"  # Cycle not homogeneous
    CYCLE_INVALID = "cycle_invalid"
    # Physical / external
    EVENT_POLLUTED = "event_polluted"
    SENSOR_INVALID = "sensor_invalid"
    DEADTIME_UNRELIABLE = "deadtime_unreliable"
    BOOT_GUARD = "boot_guard"
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


# Anti-windup / integrator behavior
INTEGRAL_LEAK = 0.995  # leak factor per cycle when inside deadband
MAX_STEP_PER_MINUTE = 0.25  # max output change per minute (rate limit)

# Setpoint step boost: faster rate limit when setpoint changes significantly
# This allows quick power ramp-up when user increases setpoint
SETPOINT_BOOST_THRESHOLD = 0.3   # min setpoint change (°C) to trigger boost
SETPOINT_BOOST_ERROR_MIN = 0.3   # min error (°C) to keep boost active
SETPOINT_BOOST_RATE = 0.50       # boosted rate limit (/min) vs 0.15 normal

# Setpoint change handling (mode change vs adjustment)
# - Large change (>= threshold): mode change (eco ↔ comfort) -> reset PI state
# - Small change (< threshold): integral preserved, servo via P + SP filter + FF
SETPOINT_MODE_DELTA_C = 0.5      # °C threshold for mode change detection
OVERSHOOT_I_CLAMP_EPS_C = 0.04  # Guard band below setpoint where integral cannot increase (°C)
ENABLE_PROPORTIONAL_DEADZONE = False  # When False, the P path uses the raw proportional error everywhere

# Tracking anti-windup (back-calculation) tuned for slow thermal systems
AW_TRACK_TAU_S = 120.0        # tracking time constant in seconds (typ. 60-180s)
AW_TRACK_MAX_DELTA_I = 5.0    # safety clamp on integral correction per cycle

# Skip cycles after resume from interruption (window, etc.)
SKIP_CYCLES_AFTER_RESUME = 1
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

# Absolute hysteresis for deadband exit (reduces oscillations at boundary)
# Enter deadband at |e| < deadband_c, exit only when |e| > deadband_c + hysteresis
# Using absolute value (not multiplicative) ensures consistent behavior across
# different deadband configurations and typical sensor noise levels.
DEADBAND_HYSTERESIS = 0.025

# --- Asymmetric Deadband / Near-band (HEAT only) ---
# Intent (thermal "rule of thumb"):
# - Make the "quiet zone" a bit wider when slightly below the setpoint (e>0) so the controller
#   does not wait too long before restarting after a setpoint decrease.
# - Make the zone tighter above the setpoint (e<0) to reduce overshoot/hunting.
# Guardrails: asymmetry is applied only in HEAT; COOL keeps symmetric logic.

# Deadband (°C) and its hysteresis (°C)
DEADBAND_BELOW_C = 0.06
DEADBAND_ABOVE_C = 0.04
DEADBAND_HYST_BELOW_C = 0.02
DEADBAND_HYST_ABOVE_C = 0.02

# Near-band asymmetry:
# - below setpoint: use configured near_band_deg (self.near_band_deg)
# - above setpoint: scale it down with a factor
NEAR_BAND_ABOVE_FACTOR = 0.40
NEAR_BAND_HYSTERESIS_C = 0.05

class TrajectoryPhase(str, Enum):
    """Trajectory state for setpoint shaping."""
    IDLE = "idle"
    TRACKING = "tracking"
    RELEASE = "release"


class ServoPhase(str, Enum):
    """Legacy compatibility enum for pre-trajectory tests and persisted state."""
    NONE = TrajectoryPhase.IDLE.value
    BOOST = TrajectoryPhase.TRACKING.value
    LANDING = TrajectoryPhase.TRACKING.value


# Trajectory generator
TRAJECTORY_ENABLE_ERROR_THRESHOLD = 0.3  # °C — signed raw error needed to arm the trajectory
TRAJECTORY_COMPLETE_EPS_C = 0.05         # °C — convergence epsilon to finish tracking
TRAJECTORY_MIN_P_ERROR_RATIO = 0.25      # Fraction of the raw signed error kept on the P path during braking
TRAJECTORY_SETPOINT_RHO = 0.70           # Fraction of available model slope used after a setpoint change
TRAJECTORY_DISTURBANCE_RHO = 0.50        # Fraction of available model slope used after a disturbance recovery
TRAJECTORY_BRAKE_GAIN = 1.20             # Safety factor applied to the predicted inertial rise before braking
TRAJECTORY_BRAKE_RELEASE_HYST_C = 0.10   # Extra signed-error margin to keep an active trajectory in tracking
TRAJECTORY_I_RUN_SCALE = 0.20            # Reduce free integral growth while the late-braking trajectory is still tracking
TRAJECTORY_RELEASE_TAU_FACTOR = 0.5      # Make the return to the raw target faster than braking while staying smooth
TRAJECTORY_BUMPLESS_MAX_U_DELTA = 0.03   # Maximum allowed proportional command step when releasing trajectory
INTEGRAL_GUARD_RELEASE_ERROR_RATIO = 2.0 # Release positive-I guard only once the residual error is close to deadband scale
INTEGRAL_GUARD_RELEASE_SLOPE_RATIO = 0.35 # Release positive-I guard when the signed recovery slope has collapsed enough
INTEGRAL_GUARD_RELEASE_SLOPE_ABS_H = 0.12 # Absolute signed-slope floor (°C/h) below which stabilization may be accepted
INTEGRAL_GUARD_RELEASE_PERSISTENCE = 3    # Consecutive evaluations required before re-enabling positive integral growth
# Error filter time constant
ERROR_FILTER_TAU = 25.0 # Minutes (matches alpha ~0.35 at 10min)


# --- Robust learning / gating constants ---
# Window sizes
B_POINTS_MAX = 40        # OFF samples for b (tau)
A_POINTS_MAX = 25        # ON samples for a
RESIDUAL_HIST_MAX = 60   # Residual history for MAD estimation

# Robust gating
RESIDUAL_GATE_K = 4.5   # |r| > k * sigma_r  -> freeze learning

# Intercept coherence checks (dimensionless ratios)
INTERCEPT_SIGMA_FACTOR = 2.0   # |c| <= factor * sigma_r
INTERCEPT_SCALE_FACTOR = 0.30  # |c| <= factor * median(|y|)

# Tau stability check
B_STABILITY_MAD_RATIO_MAX = 0.60   # MAD(b) / median(b)
LEARN_BOOTSTRAP_COUNT = 10      # Number of learn cycles before applying strict residual gating

# --- SmartPI Robust Learning Constants ---
# Median+MAD Strategy Constants
AB_HISTORY_SIZE = 31      # Keep last 31 (ODD) values
AB_MIN_SAMPLES_B = 11     # Min samples for b median (OFF phase)
AB_MIN_SAMPLES_A = 7      # Min samples for a median before b converges (ON phase)
AB_MIN_SAMPLES_A_CONVERGED = 11  # Min samples for a median once b converged
AB_MAD_SIGMA_MULT = 3.0   # Outlier rejection threshold (sigma)

AB_MAD_K = 1.4826         # Sigma scaling factor for MAD
AB_VAL_TOLERANCE = 1e-12  # Small epsilon
LEARN_SAMPLE_MAX = 240          # Max samples history (e.g. 4h @ 1min)
LEARN_Q_HIST_MAX = 200          # History for quantization estimation
DT_DERIVATIVE_MIN_ABS = 0.05    # Min absolute dT (°C) amplitude guard
OLS_MIN_JUMPS = 3               # Min temperature level changes for OLS validity
OLS_T_MIN = 2.5                 # Min t-statistic for slope significance
LEARN_QUALITY_THRESHOLD = 0.25  # Min QI quality to accept learning
QUANTIZATION_ROUND_TO = 0.001   # Rounding / binning for quantization detection

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
AB_MIN_POINTS_FOR_PUBLISH: int = 11  # Below this count: freeze to default value

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

# --- SmartPI Learning Window Constants ---
# Absolute timeout for a learning window (both A and B).
# The window extends as long as the OLS slope is not yet robust, up to this limit.
DT_MAX_MIN = 240
MIN_ABS_DT = 0.03      # °C  (reference value; not used as a gate in learning_window)
DELTA_MIN = 0.2        # °C (Matches DELTA_MIN_ON)
U_OFF_MAX = 0.05
U_ON_MIN = 0.20
DELTA_MIN_OFF = 0.5        # °C
DELTA_MIN_ON = 0.2         # °C

# Power coefficient of variation gate (Welford-based)
U_CV_MAX = 0.30            # Maximum accepted CV of power over the learning window
U_CV_MIN_MEAN = 0.05       # Minimum mean(u) to compute CV (avoids division by ~0)


# --- SmartPI Near Band Defaults ---
DEFAULT_NEAR_BAND_DEG = 0.40
DEFAULT_KP_NEAR_FACTOR = 1.0
DEFAULT_KI_NEAR_FACTOR = 1.0


# --- Forced Calibration Constants ---
CALIBRATION_TIMEOUT_MIN = 600  # 10 hours timeout

# --- AutoCalibTrigger Enums ---

class AutoCalibState(str, Enum):
    """State machine states for AutoCalibTrigger."""
    IDLE = "idle"
    WAITING_SNAPSHOT = "waiting_snapshot"
    MONITORING = "monitoring"
    TRIGGERED = "triggered"
    POST_CALIB_CHECK = "post_calib_check"


class AutoCalibWaitingReason(str, Enum):
    """Reason for remaining in waiting_snapshot state."""
    NONE = "none"
    DEADTIME_COOL_PENDING = "deadtime_cool_pending"
    FALLBACK_7D_COUNTDOWN = "fallback_7d_countdown"


# --- AutoCalibTrigger Constants ---
AUTOCALIB_SNAPSHOT_PERIOD_H = 120          # Rolling snapshot period: 5 days
AUTOCALIB_DT_COOL_FALLBACK_DAYS = 7       # Days before fallback if cool deadtime never reliable
AUTOCALIB_COOLDOWN_H = 24                  # Minimum hours between calibrations
AUTOCALIB_A_MAD_THRESHOLD = 0.25          # MAD/med threshold for 'a' stagnation
AUTOCALIB_B_MAD_THRESHOLD = 0.30          # MAD/med threshold for 'b' stagnation
AUTOCALIB_TEXT_GRADIENT_C = 5.0           # Min Tin-Text gradient to check deadtime_cool stagnation
AUTOCALIB_MAX_RETRIES = 3                  # Max retries before declaring model degraded
AUTOCALIB_RETRY_DELAY_H = 6               # Hours between retries
AUTOCALIB_EXIT_NEW_OBS_MIN = 1            # Minimum new observations (a/b) for positive exit

# --- FFv2 Governance Enums ---

class ABConfidenceState(str, Enum):
    """Confidence state for a,b model parameters."""
    AB_OK = "ab_ok"
    AB_DEGRADED = "ab_degraded"
    AB_BAD = "ab_bad"


# --- FFv2 Normative Constants ---

# Trim slow correction
FF_TRIM_RHO = 0.15        # Max trim authority relative to u_ff_ab (dimensionless ratio)
FF_TRIM_LAMBDA = 0.05     # Trim EMA learning rate per admissible episode
FF_TRIM_EPSILON = 0.02    # Min u_ff_ab denominator for relative budget (avoids div-by-~0)
FF_TRIM_K_ERROR = 0.1     # Error recentering gain for slope-based trim update
FF_TRIM_REBOOT_FREEZE_CYCLES = 3  # Freeze trim learning for a few cycles after reboot
FF_TRIM_MAX_ERROR_C = 0.3         # Max |error| (°C) for a cycle to be admissible for trim learning
FF_TRIM_MAX_SLOPE_H = 0.6        # Max |slope| (°C/h) for a cycle to be admissible for trim learning

# AB confidence & fallback
AB_BAD_PERSIST_CYCLES = 3           # Cycles in AB_BAD before fallback activates

# --- FF3 conservative pseudo-MPC constants ---
FF3_DELTA_U = 0.05
FF3_MAX_AUTHORITY = 0.20
FF3_SCORE_EPS_C = 0.01
FF3_LAMBDA_DU = 0.05
FF3_NEARBAND_GAIN = 0.50
FF3_PREDICTION_HORIZON_MIN = 30.0

# --- Adaptive T_int Filter ---
ENABLE_ADAPTIVE_TINT_FILTER = True
TINT_LP_ALPHA = 0.2              # EMA smoothing factor (0..1)
TINT_SIGMA_WINDOW = 30           # Rolling window size for sigma estimation
TINT_SIGMA_MIN = 0.02            # Floor on sigma (degC)
TINT_ADAPTIVE_K = 3.0            # Threshold multiplier: publish when |delta| > k * sigma
