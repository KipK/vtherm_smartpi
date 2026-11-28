"""Constants of the room history (signals recorded for the room model).

The rule parameters below are part of the validated methods: changing one changes
the behaviour that was qualified, so they are not configuration knobs.
"""
from __future__ import annotations

H = 3600.0
DAY = 86400.0

# Valve mode: the history records the applied heat-equivalent power as a level
# timeline, but the room model stays off for valves (the heating power is only as
# right as the valve curve that converts the opening to it).
HISTORY_VALVE_MODE_SUPPORTED = False

# ---------------------------------------------------------------------------
# History and memory bounds
# ---------------------------------------------------------------------------
HISTORY_RETENTION_S = 7.0 * DAY
HISTORY_MAX_TEMPERATURES = 60000
HISTORY_MAX_EDGES = 12000
# Valve power levels (one per change of the applied valve command)
HISTORY_MAX_LEVELS = 60000
HISTORY_MAX_OUTDOOR = 12000
HISTORY_MAX_EVENTS = 4000
# Quiet periods kept in the catalogue
HISTORY_MAX_PERIODS = 60
# Data quality. Every rule marks an interval invalid; invalid intervals break
# quiet periods and identification bins like a data gap.
# A bin is valid only with an observation within this delay.
HISTORY_GAP_S = 1200.0
# A temperature jump larger than HISTORY_SPIKE_MIN_C faster than HISTORY_SPIKE_RATE_C_PER_H
# is not physical (a strong morning heating rises by a few C per hour).
HISTORY_SPIKE_MIN_C = 0.5
HISTORY_SPIKE_RATE_C_PER_H = 20.0
# A new level confirmed by this many consecutive samples ends a spike.
HISTORY_SPIKE_CONFIRM = 3
# Debounce of the history store writes.
HISTORY_SAVE_DELAY_S = 1800
