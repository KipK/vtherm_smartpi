# Branch status: ongoing work, open questions, next steps

BRANCH-SPECIFIC FILE. It describes the branch it lives on (here `dev-0.7`, written against `ea22807` plus the slope anticipation change). Update it at each milestone (decision, bench verdict, merged lot). On a merge into another branch, resolve it to the state of the merged result: on `master` it only describes what `master` contains and what is planned for it.

## What `dev-0.7` contains beyond `master` (0.6.7)

Already in the code: restart bridge, restart hold of the previous command while the indoor temperature is missing (stop + wait <= 10 min, one single bound), emitter-delay (T_ar) learning, room model, quiet periods, heating balance, slope anticipation in HEAT and COOL (the setpoint trajectory and its two configuration options are gone). `CHANGELOG.md` `[Unreleased]` lists them in user wording. Details: `algorithm-room-model.md`.

Shipped in 0.6.6 and present in both lines: integral guard removal, bootstrap ending after four heating phases when the response lag cannot be measured (heating phases merged when the cut is short), saved bootstrap output waiting for the indoor temperature after a restart.

Shipped in 0.6.7 and present in both lines: calibration reachability judged on the measured outdoor temperature only.

## Work sequence (agreed order)

1. Integral guard removal - done (0.6.6).
2. Bootstrap exit - done (0.6.6). The exit by full collection (31 measurements of a and b) stays: with full histories SmartPI must never remain in bootstrap.
3. Emitter delay from the heating balance, as lag source and bootstrap exit - screened under the frozen grid and REJECTED: exit later in slow-emitter cells (the `a` collection restarts when a balance lag is published), benefit 12 % against the 30 % required, lag 41 % too low in a fast massive room. The balance is good in massless rooms and on a floor-heating cell (where the reference publishes no lag) but biased low by 25-60 % in massive rooms. The gains and the bootstrap exit do not read it; only the slope anticipation reads the median of its kept lags (`analysis.learning.heating_balance.kept_lag_s`). Undecided idea: keep the exit timing and use the balance only where the intercept estimator gives nothing. Exit-on-balance: recommended to drop for good, not yet confirmed by the owner.
4. Gains adapted to slow emitters - diagnostic result: counting the emitter inertia in the gains cuts overshoot by about a third only in a room without mass; in massive rooms the gains already sit at the floors (`KP_MIN`/`KI_MIN`) and overshoot (up to about 0.9 C) is unchanged or worse; half the inertia is enough, the full value adds nothing. Gains alone will not fix floor heating in a massive room. Slow-emitter gains are for 0.7 only.
5. Slope anticipation (landing on the setpoint) - done, adopted by the owner; see below.
6. Wall term: re-measure it on the no-guard baseline before any further wall-term work.

## Slope anticipation (adopted)

- Finding behind it: braking on the setpoint only shaped the P error; at setpoint crossing the command was still 25-70 % (feed-forward + integral grown during the rise); in massive rooms 65-80 % of the overshoot was power applied above the setpoint.
- Mechanism (`smartpi/anticipation.py`): the P and I errors read `T + slope * horizon` (derivative on measurement, series PID form), correction bounded to 1 C and faded in near the setpoint (`clamp(2 - e/c, 0, 1)`); stable phase only; same rule in HEAT and COOL. Without the fade-in the rise time grows 10-35 min: rejected.
- Horizon: in HEAT the median of the kept lags of the heating balance (available within hours in every room), else the longer of the active dead time and half the passive dead time; in COOL the dead times only (the balance is measured in heating). Tried and not retained: the HEAT dead time alone (too short), half the cooling dead time alone (uneven), the sum of the two (too slow), the learned response lag (published in under half of the runs), the rise after a cut divided by the slope (2-3 times too short). In massive rooms every measured delay is about 40 % short of the best-braking horizon.
- Owner decisions: one mechanism for the approach to the setpoint, so the late-braking trajectory is removed, in both modes (one regulation rule for heating and cooling); its two configuration options and the published `setpoint.filtered_setpoint` are removed with it.
- Evidence in HEAT (simulation, frozen grid, screening then confirmation on fresh seeds, fast and slow emitters, valves, window and restart cells, field-like cells, 0.01 C and 0.1 C sensors): confirmed. Safety checks without any violation; median reduction of the mean overshoot of slow emitters 31-41 %; fast emitters, valves, windows, restarts equal or better. Thin margin on one row: in light-mass rooms with 1200-3600 s emitters, deep setback, cold weather, the time more than 0.2 C under the setpoint grows (within the allowance). 0.1 C sensors work with the same code.
- Evidence in COOL (six groups, fast and slow emitters): safety checks pass everywhere; mean overshoot down 73 % (median) with slow emitters, rise time equal or shorter. One comfort row is degraded in one group: 3600 s emitter in a light-mass room, time more than 0.2 C beyond the setpoint 20-43 -> 65-68 min per day. Accepted by the owner (same trade-off as in HEAT for that kind of room).
- Known limits: a field-like floor-heating cell shows no measurable difference (no rise reaches the setpoint in the sense of the metric); in fast light-mass rooms the balance keeps no lag and the horizon comes from the dead times; up to about 0.1 C more overshoot in a cold massive room on some draws.
- The product code was shown bit-identical in HEAT to the bench form that was confirmed; the regulation reference fixture of the test suite carries the anticipation.
- To follow in the field: the horizon and its source (`analysis.control.anticipation_horizon_s`, `anticipation_horizon_source`) and the applied correction (`analysis.control.anticipation_c`) on real slow emitters.

## Open questions for the owner

- Factor 1/2 of the emitter lag: recommendation = half for gains only, full value for other uses.
- Drop exit-on-balance for good (see 3).
- Port of the restart hold to 0.6.x: recommended against; not decided.
- A heat-need observer replacing the error integral: study on hold; to reconsider with field results of the anticipation.

## Process notes for this line

- Every regulation/learning lot is a frozen declaration, screened first (see `../workflows/benchmarking.md`); the bench harness for 0.7 is local to the maintainer.
- The maintainer installs dev builds by copying the integration folder into Home Assistant. When the branch advances, tell them to copy again; verify what runs by looking for a diagnostics field of the expected commit.
- Equinox follow-up: published paths removed or renamed on this line must be recorded for the Equinox branch that follows `dev-0.7`.
