# Branch status: ongoing work, open questions, next steps

BRANCH-SPECIFIC FILE. It describes the branch it lives on (here `dev-0.7`, written against its head `800a0da`). Update it at each milestone (decision, bench verdict, merged lot). On a merge into another branch, resolve it to the state of the merged result: on `master` it only describes what `master` contains and what is planned for it.

## What `dev-0.7` contains beyond `master` (0.6.6)

Already in the code: restart bridge, restart hold of the previous command while the indoor temperature is missing (stop + wait <= 10 min, one single bound), emitter-delay (T_ar) learning, room model, quiet periods, heating balance in observation mode. `CHANGELOG.md` `[Unreleased]` lists them in user wording. Details: `algorithm-room-model.md`.

Shipped in 0.6.6 and present in both lines: integral guard removal, bootstrap ending after four heating phases when the response lag cannot be measured (heating phases merged when the cut is short), saved bootstrap output waiting for the indoor temperature after a restart.

On `master` after 0.6.6, not released yet, and present in both lines: calibration reachability judged on the measured outdoor temperature only.

## Work sequence (agreed order)

1. Integral guard removal - done (0.6.6).
2. Bootstrap exit - done (0.6.6). The exit by full collection (31 measurements of a and b) stays: with full histories SmartPI must never remain in bootstrap.
3. Emitter delay from the heating balance, as lag source and bootstrap exit - screened under the frozen grid and REJECTED: exit later in slow-emitter cells (the `a` collection restarts when a balance lag is published), benefit 12 % against the 30 % required, lag 41 % too low in a fast massive room. The balance is good in massless rooms and on a floor-heating cell (where the reference publishes no lag) but biased low by 25-60 % in massive rooms. It stays observation-only (`analysis.learning.heating_balance`, read by nothing). Undecided idea: keep the exit timing and use the balance only where the intercept estimator gives nothing. Exit-on-balance: recommended to drop for good, not yet confirmed by the owner.
4. Gains adapted to slow emitters - diagnostic result: counting the emitter inertia in the gains cuts overshoot by about a third only in a room without mass; in massive rooms the gains already sit at the floors (`KP_MIN`/`KI_MIN`) and overshoot (up to about 0.9 C) is unchanged or worse; half the inertia is enough, the full value adds nothing. Gains alone will not fix floor heating in a massive room. Slow-emitter gains are for 0.7 only.
5. Slope anticipation (landing on the setpoint) - see below.
6. Wall term: re-measure it on the no-guard baseline before any further wall-term work.

## Slope anticipation (in progress, not adopted)

- Finding: braking only shapes the P error; at setpoint crossing the command is still 25-70 % (FF + integral grown during the rise); in massive rooms 65-80 % of overshoot is power applied above the setpoint.
- Mechanism under test: regulate on `T + slope * horizon` (derivative on measurement, series PID form), faded in only near the setpoint: correction x `clamp(2 - e/c, 0, 1)`, correction bounded to 1 C. Without the fade-in the rise time grows 10-35 min (over tolerance): rejected.
- Owner decision: the late-braking trajectory (`smartpi/setpoint.py`) is removed and replaced by the anticipation (ONE mechanism for the landing). Known cost, accepted: up to about 0.1 C more overshoot in a cold massive room on some draws, equal or slightly better elsewhere. Two frozen benches did not show the trajectory redundant; this is a decision outside the rule.
- Horizon, frozen benches on slow emitters (simulation, 0.01 C sensors), all without the trajectory, controls never degraded:
  - the HEAT dead time alone is too short; half the cooling dead time alone is uneven; the two move in opposite directions with the weather;
  - the longer of the two: mean overshoot about 0.5 -> 0.25 C (mass, mild), 0.7 -> 0.43 (mass, cold), 0.34 -> 0.09 (no mass);
  - the learned response lag is published in under half of the runs (it then falls back to the longer of the two dead times): retained by the written rule;
  - the heating-balance lag exists in every room within hours once its measured lags are kept (the kept results are overwritten by regulation heatings, which resolve no lag): better in the mild massive room (0.14 C) and for the time under the setpoint without mass, a little short in the cold massive room (0.43-0.51 C) and without mass (0.15 C). The owner accepts it as the candidate horizon;
  - the sum of the two dead times brakes most but lengthens the time under the setpoint beyond tolerance;
  - in massive rooms every measured delay is about 40 % short of the best-braking horizon; the rise after a clean cut divided by the slope at the cut is 2-3 times too short (not a candidate).
- Next, in order: (a) 0.1 C sensors with the code unchanged (running), a slope from value-change instants only if that fails; (b) full-grid adoption bench with the time under the setpoint as a criterion, balance lag against learned-lag-first; (c) removal of the trajectory code, full test suite, docs.
- The experimental code lives on a separate local, unmerged branch of the maintainer (`slope_anticipation`, not pushed), not on `dev-0.7`.

## Open questions for the owner

- Factor 1/2 of the emitter lag: recommendation = half for gains only, full value for other uses.
- Drop exit-on-balance for good (see 3).
- Port of the restart hold to 0.6.x: recommended against; not decided.
- A heat-need observer replacing the error integral: study on hold until the anticipation result is in.

## Process notes for this line

- Every regulation/learning lot is a frozen declaration, screened first (see `../workflows/benchmarking.md`); the bench harness for 0.7 is local to the maintainer.
- The maintainer installs dev builds by copying the integration folder into Home Assistant. When the branch advances, tell them to copy again; verify what runs by looking for a diagnostics field of the expected commit.
- Equinox follow-up: published paths removed or renamed on this line must be recorded for the Equinox branch that follows `dev-0.7`.
