"""Constants of the room model.

The rule parameters below define the identification, its publication rules and
the observer: changing one changes the qualified behaviour, so they are not
configuration knobs.
"""
from __future__ import annotations

import math

H = 3600.0
DAY = 86400.0

# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------
# Propagation sub-step over the real elapsed time.
RM_STEP_S = 60.0
# Reasons of the regulation for not being in control that the observer treats
# apart from a plain pause.
RM_EXCLUDED_WINDOW = "window open"
RM_EXCLUDED_SHEDDING = "power shedding"
# Continuous process noise: air 0.05 C per sqrt(h); wall state 0.02 C of wall
# temperature per sqrt(h) (scaled by the coupling); disturbance random walk of
# 0.3 x the heating need of 1 C indoor-outdoor difference per sqrt(h).
RM_Q_AIR_C = 0.05
RM_Q_WALL_C = 0.02
RM_Q_DIST_REL = 0.3
# Initial standard deviations: air 0.1 C, wall 0.5 C of wall temperature,
# disturbance 0.3 x the heating need of 1 C.
RM_P0_AIR_C = 0.1
RM_P0_WALL_C = 0.5
RM_P0_DIST_REL = 0.3
# The disturbance is bounded: |d| <= RM_D_MAX_REL x a.
RM_D_MAX_REL = 0.3
# Measurement variance of a precise sensor (noise +-0.02 C, 0.01 C steps);
# a coarse sensor uses q^2 / 12 with each change read at the midpoint of its
# two levels (the crossing of the quantisation boundary).
RM_R_PRECISE = 0.04 ** 2 / 12.0 + 0.01 ** 2 / 12.0
# A downtime up to this long resumes the saved state; a longer one restarts the
# observer at the wall equilibrium.
RM_RESUME_MAX_S = 3.0 * H
# The observer is settling during this time after a restart at equilibrium.
RM_SETTLE_S = 3.0 * H
# Health checks
RM_INNOV_EMA = 0.05          # weight of a new correction in the innovation averages
RM_INNOV_BIAS_MAX = 2.0      # |mean normalised innovation|
RM_INNOV_NIS_MAX = 16.0      # mean squared normalised innovation
RM_INNOV_MIN_COUNT = 20      # corrections before the innovation checks apply
RM_AIR_SD_MAX_C = 0.5        # air uncertainty
RM_D_SAT_MAX_S = 6.0 * H     # disturbance held at its bound
RM_WALL_EXTRA_MAX = 1.0      # |wall extra power| / full power: wall state implausible

# Forecasts (s): open loop from the current state under the current command
RM_HORIZONS_S = (1.0 * H, 2.0 * H, 6.0 * H, 12.0 * H)
RM_HORIZON_LABELS = ("1h", "2h", "6h", "12h")
# Scored forecasts: an origin every hour, replayed with the applied command
# and the measured outdoor temperature; the last errors are kept per horizon.
RM_SCORE_EVERY_S = 1.0 * H
RM_SCORE_KEEP = 168

# Disturbance of the quiet periods (detected heat gains or losses): mean of the
# disturbance over the identification interval of each closed period.
RM_NIGHT_MIN_TRACK_S = 4.0 * H
# A period whose mean disturbance differs from the median of the recent periods
# by more than this (C/h) is excluded from the identification.
RM_NIGHT_D_DEV_C_H = 0.08
RM_NIGHTS_KEEP = 14
# The disturbance of each tracked sub-step is kept until the periods it may
# belong to are closed: longer than the longest identification interval (a
# 24 h block of a polar night and the response tail after it).
RM_STEPS_KEEP_S = 36.0 * H

# ---------------------------------------------------------------------------
# Feed-forward
# ---------------------------------------------------------------------------
# A change of the wall term in force (switched on, switched off, new wall
# parameters) is blended linearly over this time from the wall part applied
# last; it is never moved into the integral.
RM_WALL_FADE_S = 0.5 * H

# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------
RM_FIT_STEP_S = 60.0
RM_MIN_SEGMENT_S = 1.0 * H
RM_MIN_EVENTS = 40
RM_MIN_HEAT_S = 3600.0       # full-power equivalent heating over the fit periods
RM_MIN_DELTA_C = 3.0         # mean indoor-outdoor difference over the fit periods
RM_EVENT_WEIGHT_CAP_S = 1800.0
RM_CHECK_STEP_S = 600.0      # residual checks on a 10 min grid
# Search domain (per second)
RM_TAU_AIR_GRID_H = (0.5, 1.2, 3.0, 7.0, 16.0, 40.0, 100.0, 250.0)
RM_TAU_WALL_GRID_H = (2.0, 6.0, 20.0, 60.0, 200.0, 600.0)
RM_G_GRID = (0.1, 0.4, 0.7, 0.9)
RM_SEARCH_STARTS = 2
# Plausible values (per second)
RM_A_RANGE = (2e-5, 1e-2)
RM_BEQ_RANGE = (1.0 / (3000.0 * H), 1.0 / (3.0 * H))
RM_ALPHA_MAX = 1.0 / (0.25 * H)
RM_BETA_RANGE = (1.0 / (2000.0 * H), 1.0 / (3.0 * H))
# Emitter search: the full model is fitted with the emitter first moment on a
# fixed grid, log-spaced (ratio 60^(1/12), about 1.41) from 60 s, the fit step
# (a faster emitter is not resolved), to 1 h, a third of the shortest wall time
# 1 / RM_BETA_RANGE[1] (a slower one would be confused with the walls). The
# lowest cost is refined by a bounded search in log time between its grid
# neighbours, to 1 % of the time (in heavy rooms the static loss b_eq moves by a
# fraction of a percent per second of emitter time).
RM_EMITTER_GRID_S = tuple(round(60.0 * 60.0 ** (k / 12.0), 1) for k in range(13))
RM_EMITTER_XATOL = 0.01
RM_EMITTER_REFINE_MAX = 10
# The cost profile is significant when its rise above the minimum, in units of
# the residual variance per effective independent residual, exceeds the 95 %
# quantile of a chi-square with one degree of freedom: (J - J_min) / J_min x
# N_eff > RM_EMITTER_CHI2. An optimum at an end of the grid with a
# significant profile is refused (the emitter lies outside); a profile that
# is not significant leaves the emitter undetermined, and the refined T_ar
# value is used when published.
RM_EMITTER_CHI2 = 3.841
# A structure whose heating response a lies beyond this factor of the online
# estimate (either way) is refused: it lumps the walls into the air.
RM_A_ONLINE_RATIO_MAX = 2.0
# A structure whose static gain b_eq / a differs from the energy-balance slope
# in force by more than this (|ln ratio|) is refused: its wall dynamics do not
# explain the measured heat balance.
RM_EB_AGREE_TOL = math.log(1.05)
# Wall identified: the wall carries at least this share of the air loss
RM_WALL_SHARE_MIN = 0.1
# The full model must beat the reduced one on the held-out data by this factor
RM_FULL_HELDOUT_GAIN = 0.9

# Residual checks (training and held-out)
RM_RES_MEAN_MAX = {"precise": 0.03, "coarse": 0.05}
RM_RES_RMS_MAX = {"precise": 0.08, "coarse": 0.12}
RM_RES_HELDOUT_MEAN_MAX = {"precise": 0.05, "coarse": 0.08}
RM_RES_HELDOUT_RMS_MAX = {"precise": 0.12, "coarse": 0.15}
# Structure tests (runs, autocorrelation, correlations) apply only above this
# residual level: a structure below the sensor resolution is not a model error.
RM_RES_STRUCT_FLOOR_C = 0.015
RM_RES_STRUCT_FLOOR_Q = 0.5
RM_RES_RUNS_Z_MIN = -3.5
# The runs test is taken on hourly means of the residuals, when there are enough of them
RM_RES_RUNS_BLOCK_S = 1.0 * H
RM_RES_RUNS_MIN_BLOCKS = 10
RM_RES_AC_1H_MAX = 0.4
RM_RES_CORR_MAX = 0.3

# Information of a fit: the 95 % interval (normal quantile) of each identifiable
# coordinate must be no wider than its agreement tolerance below (gates.py).
RM_INFO_Z = 1.96

# Agreement of two successive windows (|ln ratio|)
RM_AGREE_TOL = {
    "a": math.log(1.15),
    "b_eq": math.log(1.25),
    "alpha": math.log(1.25),
    "c": math.log(1.35),
}
# Publication: median of the last qualified candidates of the same structure
RM_PUB_LAST = 3
RM_MAX_WINDOWS = 8
RM_MAX_RECORDS = 14

# Debounce of the room model store writes
RM_SAVE_DELAY_S = 1800

# Model structures
STRUCT_FULL = "full"
STRUCT_REDUCED = "reduced"
