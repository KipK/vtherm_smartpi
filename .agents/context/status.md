# Branch status: ongoing work, open questions, next steps

BRANCH-SPECIFIC FILE. It describes the branch it lives on (here `dev-0.7`, written against its head `800a0da`). Update it at each milestone (decision, bench verdict, merged lot). On a merge into another branch, resolve it to the state of the merged result: on `master` it only describes what `master` contains and what is planned for it.

## What `dev-0.7` contains beyond `master` (0.6.6)

Already in the code: restart bridge, restart hold of the previous command while the indoor temperature is missing (stop + wait <= 10 min, one single bound), emitter-delay (T_ar) learning, room model, quiet periods, heating balance in observation mode. `CHANGELOG.md` `[Unreleased]` lists them in user wording. Details: `algorithm-room-model.md`.

Shipped in 0.6.6 and present in both lines: integral guard removal, bootstrap ending after four heating phases when the response lag cannot be measured (heating phases merged when the cut is short), saved bootstrap output waiting for the indoor temperature after a restart.

## Work sequence (agreed order)

1. Integral guard removal - done (0.6.6).
2. Bootstrap exit - done (0.6.6). The exit by full collection (31 measurements of a and b) stays: with full histories SmartPI must never remain in bootstrap.
3. Emitter delay from the heating balance, as lag source and bootstrap exit - screened under the frozen grid and REJECTED: exit later in slow-emitter cells (the `a` collection restarts when a balance lag is published), benefit 12 % against the 30 % required, lag 41 % too low in a fast massive room. The balance is good in massless rooms and on a floor-heating cell (where the reference publishes no lag) but biased low by 25-60 % in massive rooms. It stays observation-only (`analysis.learning.heating_balance`, read by nothing). Undecided idea: keep the exit timing and use the balance only where the intercept estimator gives nothing. Exit-on-balance: recommended to drop for good, not yet confirmed by the owner.
4. Gains adapted to slow emitters - diagnostic result: counting the emitter inertia in the gains cuts overshoot by about a third only in a room without mass; in massive rooms the gains already sit at the floors (`KP_MIN`/`KI_MIN`) and overshoot (up to about 0.9 C) is unchanged or worse; half the inertia is enough, the full value adds nothing. Gains alone will not fix floor heating in a massive room. Slow-emitter gains are for 0.7 only.
5. Slope anticipation (landing on the setpoint) - see below.
6. Wall term: re-measure it on the no-guard baseline before any further wall-term work.

## Slope anticipation (in progress, not adopted)

- Finding: braking only shapes the P error; at setpoint crossing the command is still 25-70 % (FF + integral grown during the rise); in massive rooms 65-80 % of overshoot is power applied above the setpoint.
- Mechanism under test: regulate on `T + slope * emitter delay` (derivative on measurement, series PID form; SIMC puts derivative time at the whole emitter inertia, simulations prefer half), faded in only near the setpoint: correction x `clamp(2 - e/c, 0, 1)`, horizon = delay + half the inertia.
- Result (frozen bench, simulation): mean overshoot 0.46 -> 0.08 C (mass, mild weather), 0.70 -> 0.38 (mass, cold), 0.35 -> 0.24 (no mass); rise time +5 to +10 min; control cells not degraded; level A passes. The owner approved the approach (a slight overshoot is tolerated). Without the fade-in, overshoot fell 60-100 % in massive slow-emitter rooms but rise time grew 10-35 min (over tolerance).
- Not adopted yet. Next lots, in order: (a) horizon from what SmartPI already knows (measured dead time or the delay the regulation applies; no plant truth) - the owner agreed to test this known-delay point first; one arm also removes the setpoint trajectory; (b) 0.1 C sensors (slope from value-change instants); (c) full-grid adoption bench; (d) full test suite; (e) docs.
- Design constraint (owner): ONE mechanism for the landing on the setpoint. If the anticipation is adopted, the late-braking trajectory (`smartpi/setpoint.py`) must be removed rather than kept alongside; any adoption lot includes an arm without the trajectory.
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
