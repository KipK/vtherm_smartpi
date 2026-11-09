"""Constants of the quiet periods, with their justification.

A quiet period is a stretch of time without sun, at a constant setpoint and
with valid data. These values are design choices of the method, not
configuration knobs.
"""
from __future__ import annotations

H = 3600.0

# Version of the selection rules. It is part of every night key: a change of
# the rules gives new keys instead of mixing nights selected by different rules.
SELECTION_VERSION = 1

# ---------------------------------------------------------------------------
# Darkness
# ---------------------------------------------------------------------------
# Standard sunrise and sunset: the upper limb of the sun on the apparent
# horizon, i.e. the centre 0.833 degrees below the geometric horizon (34' of
# standard refraction plus 16' of apparent radius).
SUN_STANDARD_HORIZON_DEG = -0.833
# The refraction near the horizon departs from its standard value with the
# temperature and pressure profile of the air. Half a degree more depression
# covers its usual spread: the darkness ends a few minutes before the standard
# sunrise and starts a few minutes after the standard sunset. A few degrees of
# sun on a window facing it already bring a heat gain an identification at
# 0.01 C cannot ignore, so there is no margin after sunrise.
SUN_REFRACTION_MARGIN_DEG = 0.5
DARK_ELEVATION_DEG = SUN_STANDARD_HORIZON_DEG - SUN_REFRACTION_MARGIN_DEG
# Accuracy of the solar position and of the boundary search (the solar
# algorithm is accurate to about one minute near the horizon).
DARK_TIME_MARGIN_S = 120.0
# The elevation is sampled on a grid aligned on the epoch, so that every call
# finds the same boundaries. Between two samples the elevation departs from the
# straight line by at most |d2e/dt2| step^2 / 8; near the horizon
# |d2e/dt2| <= (15 deg/h)^2 in radians, i.e. 3.93 deg/h^2, so 0.014 deg on a
# 600 s step. A sample counts as dark only with this guard below the threshold.
SUN_SAMPLE_S = 600.0
SUN_SAMPLE_GUARD_DEG = 0.05
# Boundary refinement (bisection) resolution
SUN_REFINE_S = 1.0
# Search limit of the true ends of a darkness that covers the requested range
# (polar night) and length of the blocks it is cut into, anchored on its start.
DARK_SEARCH_MAX_S = 370.0 * 24.0 * H
DARK_BLOCK_S = 24.0 * H

# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
# Two setpoints closer than this are the same plateau.
SP_EPS = 1e-6
# A quiet component shorter than this is not a night.
PERIOD_MIN_S = 1.0 * H
# Prior stability of the baseline of a setpoint rise: the low setpoint known and
# constant, in the dark and with valid data, over the 2 hours before the rise.
BASELINE_HOLD_S = 2.0 * H
# A heater start counts as the room regulating at the low setpoint when the
# indoor temperature is within this of that setpoint (same tolerance as the
# regulated-night rule).
REGULATED_ECO_TOL_C = 0.3
# Minimum regulated part for the confirmation gate and the regulated-night rule:
# eight half hours, instead of a conclusion drawn from one isolated heater start.
REGULATED_MIN_S = 4.0 * H
# A heater start within this delay after a restart is the cycle restored at the
# start, not a proof that the room regulates.
RESTART_START_GUARD_S = 60.0

# Identification also keeps the response to the setpoint rise that ends a
# period, while it is still dark and the new setpoint constant, for at most
# this long (the response horizon of the comfort-step fit): that response is
# the main excitation of the emitter, and without it the emitter time constant
# of a regulated room is poorly determined.
IDENT_TAIL_S = 2.0 * H

# Time precision of the keys (the history rounds its times to 0.1 s)
KEY_DIGITS = 1
