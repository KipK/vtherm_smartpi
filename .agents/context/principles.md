# Project principles (standing decisions)

These are decisions of the project owner. They bind every agent. If a task seems to need an exception, stop and ask.

## Product

- **Simple and universal.** A regulation change uses one or two parameters, works the same way for any on/off emitter (radiator, floor, electric, valve), starts neutral, and is bounded by a cap. Size the solution to the measured defect (SmartPI already holds about +-0.1 C most of the time; remaining gains are small). A rule that needs a per-heating-type exception or a new publication gate must be flagged and justified.
- **One mechanism per job.** Do not keep two mechanisms doing the same thing (example: setpoint landing is one mechanism; if a new one is adopted the old one is removed).
- **One regulation rule for both modes.** Heating and cooling are symmetric: a regulation mechanism applies to both, never one rule in HEAT and another in COOL.
- **HEAT first.** Heating decides. Slow-room COOL cases are rare (air conditioners respond fast) and never block a change. Overshoot during a hysteresis bootstrap is expected (power stays at 100 % until the threshold) and is not a regression.
- **Target sensors:** precise 0.01 C sensors that publish on change (at most every few tens of seconds) plus a short heartbeat (5-10 min). 0.1 C sensors that publish on change with a 30-60 min heartbeat are supported best-effort; weaker regulation there is acceptable (still better than a plain TPI). A 0.1 C sensor is not noisy; its difficulty is the staircase (few changes, long holds). Take slopes from the times of value changes. In benches, slow/coarse sensor cells are reported, never decisional. A silent estimator is acceptable; a wrong published value is not.
- **Disturbances: identify and avoid.** Sun, occupants and other unmeasured gains are handled by excluding the affected windows (nights, overcast days, estimator gates), never by adding disturbance terms to the fits by default.
- **No declared schedules.** Never ask the user to declare occupancy ranges and never hard-code sleep/night clock spans (habits and setback schedules differ between households, so any fixed span fails somewhere). Quiet periods are derived from data (setpoint history, sun position, data validity). An optional presence sensor (mmWave) may come later.
- **Sensor health is VT's job.** SmartPI never marks a sensor stale or frozen and adds no heartbeat/staleness rule. It assumes the sensor works; VT's safety mode is the signal. Data-quality rules may handle gaps, unavailable values, restarts, impossible spikes and periods when SmartPI is not in control.
- **Plain diagnostics.** Attributes shown to users carry no internal bench/rule names or internal codes (no `gate: G1`, no method/class codes). Translate internal codes at the diagnostics boundary (see `smartpi/tar_learning/public.py`). Keep docs tables in sync.
- **Dependencies.** numpy and scipy are in the manifest requirements (lazy imports, heavy fits run in an executor, never on the event loop). A failed install blocking load is a known, accepted trade-off: do not raise it again. HA dropped 32-bit ARM.
- **Real rooms are slow.** Winter field time constants are hundreds of hours; mild-weather estimates of b are biased low. Plants with a 1 h time constant do not exist in practice: do not spend effort on them. Calibrate benches on winter field values.

## Theory

- SmartPI is a discrete PI regulator on a 1R1C-based model, following Astrom & Hagglund, *PID Controllers: Theory, Design and Tuning* (2nd ed.). A change must go the way of the book (anti-windup on actuator saturation, setpoint weighting/trajectory for setpoint response, derivative on measurement, dead-time aware tuning) or say why it departs. See `theory.md`.

## Evidence and benches

- Regulation/learning benches are two-stage: screening on the recommended configurations (switch, valve, quantized/stepped variants) with the five plant models (exact, a +-20 %, b +-20 %), HEAT and COOL, decision scenarios; full confirmation only for a variant about to be adopted.
- Declare before running; never relax a rule after the results. Grid of rules:
  - **A, strict:** zero cases allowed (no added oscillation, no drift, authority cap respected, bit-identical when the function is silent).
  - **B, comfort:** overshoot not judged under 0.15 C, above it limit = reference + 0.10 C; steady error not judged under 0.05 C, above it limit = reference + 0.03 C; a rule fails if more than 10 % of groups violate it or one group exceeds twice the tolerance; measure the seed noise floor first, no tolerance below twice that floor; "indeterminate" means add two seeds. Comfort rules decide on HEAT only.
  - **C, false value:** judged by effect on the command (about 10 power points): +-30 % on the static part, +-35 % on the wall term, which needs an authority cap.
- Do not fight over 0.05 C differences in overshoot; a light overshoot is acceptable when the rest is positive.
- Never re-judge old benches with a new grid.

## Process

- **Never commit, push, tag or amend unless the user says so expressly.** When a change is done, give a commit message in English (brief but informative, no links, no file paths). No co-author trailer.
- Run pytest yourself, targeted files first; tests can be slow (on the maintainer's WSL/NTFS machine they are), that is expected. See `../workflows/testing.md`.
- Comments and docstrings in English. Docs describe the current state only ("as if never published"): no "new", "now", "fixed", "changed" outside CHANGELOG.
- Upstream-bound repos (versatile_thermostat, vtherm_api): comments, docstrings, docs and tests never refer to plans, phases, decisions or review IDs.
- Keep `algo.py` and `handler.py` from growing; put logic in `smartpi/` modules.
- Do not change anything beyond the task. Re-read every diff after editing.
- Zero hallucination: never invent, guess or extrapolate; every claim rests on code, documentation, observed facts or a demonstrable argument. If a decision is not certain, stop and ask.
- Treat as private and never write into versioned files: credentials, tokens, addresses, names, emails, real entity ids, room nicknames, tester data, personal habits.
- Field results from testers are described qualitatively in public text; simulation figures are fine.
- Equinox (dashboard card) reads published diagnostics paths. Removing or renaming a published path is allowed when needed, but record it (path, Equinox file) for the Equinox follow-up and update `cards/` in the same change.
