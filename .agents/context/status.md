# Branch status: workstreams, open questions, next steps

BRANCH-SPECIFIC index of the branch it lives on (here `master`, carrying the redesign toward the 1.0). Short index only, at most 60 lines: one to three lines per item (goal, state, next step). Narratives, bench numbers and field follow-ups live in the local task folders (`.agents/local/tasks/`, unversioned); outcomes go to `decisions.md`. On a merge, resolve it to the merged result.

## What this line contains beyond the released 0.6.7 (`0.6-hotfixes`)

- Slope anticipation in HEAT and COOL, the one mechanism of the approach to the setpoint (the setpoint trajectory and its two options are gone).
- Restart bridge and hold of the previous command while the indoor temperature is missing.
- One response model per mode (dead time theta, response time constant tau_e) read by the a/b learning, the holds, the anticipation horizon, the calibration limits and the room model; the dead time after a stop, the heating balance, the emitter lag (T_ar) learning and the ramp-asymptote estimator are deleted.
- One measurement event and one reader giving starting values; learning start option (bootstrap by default, or direct pulses).
- Calibration by pulses (on request, and automatic once after an upgrade); the former automatic calibration is deleted.
- Room history, quiet periods and room model, observation only: the holding power is always the learned `b / a`.
- Learning block (pause, disturbance, heating unavailable); FF2, FF3, thermal twin and command ownership removed.

`CHANGELOG.md` `[Unreleased]` lists them in user wording; mechanisms in `decisions.md`, details in `algorithm-core.md` and `algorithm-room-model.md`.

## Workstreams

- **1.0 finish** (current): docs, cards and README aligned with the code; states saved by 0.6.7 and 0.7 load in `tests/test_saved_state_upgrade.py`. Known and left: a saved `None` estimator value makes the load fail (strict xfail, as in 0.6.7); a 0.6.7 bootstrap on/off output is not resumed at the upgrade. Next: `CHANGELOG.md` for 1.0 and release notes, then the 1.0 release.
- **Calibration by pulses**: the code with the owner's fixes is unit-tested; its bench is pending. The automatic calibration after an upgrade and the calibration report are not yet run in the field or benched.
- **Field follow-up of this line**: anticipation (horizon, correction, no command swings or steps once settled), response-model outliers after the repair at load, direct start (`a` gate lock, `b` in fast rooms). Items and readings in the local task notes.

## Next steps (owner's order)

1. 1.0 finish (above).
2. Wording: two names only, "dead time" (start of the response) and "emitter lag" (mean arrival time of the heat); docs, cards and a glossary; no published path change.
3. Offline study of learning `b` at medium command (day and night apart). Unverified whether still wanted after the 1.0 release.

## Open questions for the owner

- Pulse heat step end: detector-based end (in the code) or "three sensor steps over the lowest point" (ends a calibration pulse before the detection).
- Room model emitter search (13-value grid): kept, though the design deletes it; with the exact emitter a wall-mass room gives `b_eq/a` 16 % low and fails the 5 % energy-balance agreement. Delete it or not.
- Port of the restart hold to 0.6.x: recommended against; not decided.
- A heat-need observer replacing the error integral: on hold until field results of the anticipation.
- Anticipation fade ramp (error between c and 2c): the anticipated error is 2e - 2c, so P reacts at twice Kp there; a possible source of command spikes, not benched. Attribute the command jumps of a field history before any change.

## Process notes for this line

- Every regulation/learning change is a frozen declaration, screened first (`../workflows/benchmarking.md`); the bench harness is local to the maintainer.
- Equinox follow-up: published paths added, removed or renamed on this line must be recorded for the Equinox branch that follows this line (the maintainer keeps the list locally).
