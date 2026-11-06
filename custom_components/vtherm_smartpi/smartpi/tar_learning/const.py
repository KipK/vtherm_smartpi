"""Constants of the T_ar learning (emitter response delay).

The rule parameters below are part of the validated methods: changing one changes
the behaviour that was qualified, so they are not configuration knobs.
"""
from __future__ import annotations

import math

H = 3600.0
DAY = 86400.0

# ---------------------------------------------------------------------------
# Applied value
# ---------------------------------------------------------------------------
# A learned value is applied only within [low, high] x the bootstrap dead time.
TAR_APPLY_BOUND_LOW = 0.5
TAR_APPLY_BOUND_HIGH = 2.0
# The gain rule expects an effective dead time. The learned value is the first
# moment of the emitter response, twice the effective dead time of a first-order lag.
TAR_GAIN_DEADTIME_FACTOR = 0.5

# Confirmation gate of the fast morning value: "G1" or "GT4".
TAR_GATE_G1 = "G1"
TAR_GATE_GT4 = "GT4"
TAR_FAST_GATE = TAR_GATE_G1

# Valve mode: the history records the applied heat-equivalent power as a level
# timeline, but the learning stays off for valves. The learned value is only as
# right as the valve curve that converts the opening to heating power, and on a
# slow emitter its use left the comfort rise short of the setpoint.
TAR_VALVE_MODE_SUPPORTED = False

SOURCE_BOOTSTRAP = "bootstrap"
SOURCE_FAST = "fast"
SOURCE_REFINED = "refined"

# ---------------------------------------------------------------------------
# History and memory bounds
# ---------------------------------------------------------------------------
TAR_HISTORY_RETENTION_S = 7.0 * DAY
TAR_HISTORY_MAX_TEMPERATURES = 60000
TAR_HISTORY_MAX_EDGES = 12000
# Valve power levels (one per change of the applied valve command)
TAR_HISTORY_MAX_LEVELS = 60000
TAR_HISTORY_MAX_OUTDOOR = 12000
TAR_HISTORY_MAX_EVENTS = 4000
TAR_MAX_NIGHTS = 60
TAR_MAX_MORNINGS = 60
TAR_MAX_EVALUATIONS = 30
TAR_MAX_WINDOWS = 12
# Data quality. Every rule marks an interval invalid; invalid intervals break
# nights, identification bins and morning steps like a data gap.
# A bin is valid only with an observation within this delay.
TAR_GAP_S = 1200.0
# Bins around a restart of Home Assistant or SmartPI are invalid.
TAR_RESTART_MARGIN_S = 600.0
# A temperature jump larger than TAR_SPIKE_MIN_C faster than TAR_SPIKE_RATE_C_PER_H
# is not physical (a strong morning heating rises by a few C per hour).
TAR_SPIKE_MIN_C = 0.5
TAR_SPIKE_RATE_C_PER_H = 20.0
# A new level confirmed by this many consecutive samples ends a spike.
TAR_SPIKE_CONFIRM = 3
# Debounce of the learning store writes.
TAR_SAVE_DELAY_S = 1800

# ---------------------------------------------------------------------------
# Morning comfort step (event detection on the live setpoint)
# ---------------------------------------------------------------------------
STEP_MIN_RISE_C = 1.0
STEP_FROM_H = 3.0
STEP_TO_H = 10.0
STEP_MERGE_S = 900.0
# The morning is evaluated once the 2 h response after the step is recorded.
MORNING_EVAL_DELAY_S = 2.0 * H + 900.0 + 300.0

# ---------------------------------------------------------------------------
# Response fit (shared by every morning path)
# ---------------------------------------------------------------------------
FIT_DT = 10.0
END_RISE_S = 300.0
BASE_SPAN_S = 7200.0
SENSOR_SPAN_S = 24.0 * H

# Night indicator (passive night: one against two time constants)
RM_GRID_S = 60.0
RM_BIN_S = 120.0
RM_N_EFF_STEP_S = 600.0
RM_SIGMA_FLOOR_C = 0.003
RM_TAU_MIN_S = 0.1 * H
RM_TAU_MAX_S = 2000.0 * H
RM_TAU_COUNT = 56
RM_RATIO_MIN = 1.5
RM_RULE = {
    "min_len_h": 1.0,
    "dbic_min": 6.0,
    "tau_ratio_min": 3.0,
    "fast_amp_min_c": 0.1,
    "fast_amp_min_q": 2.0,
    "share_amp_min": 0.03,
    "tau1_max_len_frac": 1.0,
    "tau1_min_h": 0.25,
    "room_min_segments": 2,
}
TAU2_EDGE_H = 1000.0
NIGHT_LOOKBACK_S = 10.0 * H
NIGHT_FROM_H = 18.0
NIGHT_TO_H = 8.0
WASHOUT_FALLBACK_S = 3.0 * H
MIN_FIT_S = 1.0 * H
PEAK_SMOOTH_S = 1800.0
LONG_NIGHT_H = 4.0
CYC_MIN_ANCHORS = 2
CYC_MIN_BINS = 6

# Regulated-night branch
R_SPAN_S = 7200.0
R_QUARTER_S = 1800.0
R_POST_S = 7200.0
R_JUMP_CYCLES = 2
ONSET_GAP_S = 1800.0
ONSET_LOOKBACK_S = 14.0 * H

# Morning gates
RULES = {
    "base_rms_c": 0.03,
    "base_rms_q": 0.6,
    "fit_rms_c": 0.05,
    "fit_rms_q": 0.6,
    "exc_window_T": 1.5,
    "exc_energy_T": 0.5,
    "exc_energy_min_s": 600.0,
    "exc_rise_c": 0.3,
    "exc_rise_q": 4.0,
    "sensor_T_factor": 2.0,
}
RULES_R = {
    "reg_rms_cyc_c": 0.03,
    "r_quarter_min_starts": 1,
    "r_eco_tol_c": 0.3,
    "r_u_slope_max_h": 0.05,
    "r_u_resid_max": 0.10,
    "r_jump_min": 0.20,
}
CLASS_REASONS = (
    "night not usable for the room class",
    "no heater start after the comfort step",
)

# Publication of the fast value
PUB_MIN_ACCEPTED = 3
PUB_LAST = 5
PUB_MAD_REL = 0.25

# Occupant gates of the fast value
GATE_IDLE_MIN_H = 1.0
GATE_SLEEP_START_H = 23.0
GT4_T_MIN_S = 900.0
GT4_BFIT_REL_MIN = 0.6

# ---------------------------------------------------------------------------
# Multi-day identification
# ---------------------------------------------------------------------------
ID_DT = 600.0
ID_FINE_S = 60.0
ID_SIGMA_FLOOR = 0.003
ID_DBIC_MASS = 10.0
ID_DBIC_ONE = 0.0
ID_KB_MIN = 1.0
ID_TW_MIN_H = 2.0
ID_TW_MAX_H = 300.0
ID_TE_GRID = (0.0, 900.0, 2400.0)
ID_TE_MAX = 7200.0
ID_TW_LO = 1.0 * H
ID_TW_HI = 800.0 * H
ID_MIN_SAMPLES = 50
ID_WIN_DAYS = 3
ID_WINDOW_END_H = 8.0
ID_K_TOL = math.log(1.25)
ID_TW_TOL = math.log(1.5)
ID_PRECISE_Q = 0.015
ID_ONE_DBIC_MAX = 0.0
ID_ONE_RMS_C = 0.025
ID_ONE_B_TOL = math.log(1.15)
ID_ONE_BPUB_TOL = math.log(1.35)
ID_TE_PUB_MIN = 1.0
ID_TE_PUB_MAX_FRAC = 0.95
ID_TE_TOL = math.log(1.5)
ID_PUB_LAST = 5

# Regulated night (hand-over of the refined value)
RN_SPAN_START_H = 23.0
RN_SP_RISE_FROM_H = 3.0
RN_SPAN_END_MAX_H = 8.0
RN_QUARTER_S = 1800.0
RN_QUARTER_MERGE_S = 900.0
RN_ECO_TOL_C = 0.3
RN_U_SLOPE_MAX_H = 0.05
RN_U_RESID_MAX = 0.10
RN_NIGHTS_PER_WINDOW = 3
RN_SP_EPS = 1e-6
