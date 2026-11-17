# Benchmarks (simulation studies of regulation and learning)

Bench drivers, scenarios and results are local to the maintainer (`tools/`, gitignored); this file is the method. A new bench is built from the `smartpi/` code driven by a simulated plant and recorded sensor/actuator models.

## Method

1. **Declare first** with `../templates/bench-declaration.md`: question, arms (baseline + variants, each a code ref or a knob), cells (plant x emitter x sensor x scenario), seeds, rules, verdict logic. Freeze it (hash or commit) before the first run. Never relax a rule after results. A second pre-frozen verdict may encode accepted deviations when re-declaring on a new base.
2. **Two stages.** Stage 1 screening on the recommended configurations (switch, valve, quantized variants), five plant models (exact, a +-20 %, b +-20 %), HEAT and COOL, decision scenarios: about a third of the runs. Failing variants stop there. Stage 2 full confirmation only for a variant about to be adopted.
3. **Rule grid** (A strict / B comfort / C false value): see `../context/principles.md`. Measure the seed noise floor first; tolerances never below twice the floor; undetermined = add two seeds. HEAT decides; COOL only reported; slow/coarse sensor cells reported only; runs that never leave bootstrap for lack of demand do not block; stratify overshoot by bootstrap-exit delay.
4. **Realism.** Plants calibrated on winter field values (tau hundreds of hours), emitters with pure delay and inertia, sensors per `../context/field-knowledge.md` (0.01 C on change + short heartbeat; 0.1 C on change + long heartbeat; reporting threshold knob). Check the harness publishes the declared sensor quantum (a rounding to 0.1 C once silently changed every sub-0.1 declaration). A restart cell must hide the sensor on every path during the gap.
5. **Comparing code versions.** Use git worktrees of the refs and verify that the harness AND the SmartPI modules are imported from the arm tree, otherwise both arms silently run the main tree. Copy the local `tools/` into each worktree. Bench drivers hook private SmartPI names on the instance: after moving a method, keep internal calls going through `self.<name>`.
6. **Frozen-model experiments** seeding a/b histories start in STABLE and never learn the dead time (no bootstrap power steps): inject the dead time or compare end-to-end cold starts.
7. **Metrics reading.** IAE in sun scenarios and setpoint-step scenarios is dominated by uncontrollable or tracking parts; steady-state regulation shows in the steady scenario. A bench rule inherited from an older base must be re-derived if the new base already has a mechanism of the same kind (sign of the expected difference may flip).

## Running

- One bench at a time (CPU bound). Never two at once, including from subagents.
- Launch as a normal background task of the session and wait for the completion notification. No polling, no `pgrep -f` wait loops (it matches itself and never ends), no leftover wait tasks. Use `setsid nohup` only for a run that must outlive the session, and say so.
- Process pools with numpy: set `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1` or workers oversubscribe BLAS threads and stall.
- Persistent worker mode is bit-identical to one process per run; keep an isolated mode for audits. zsh does not word-split `$var`: use `sh` scripts for job lists.
- Typical cost: about 35 s per 288 h simulated run at two processes.

## Reporting

Verdict table per rule and per group, noise floor, undetermined items, exact commands to rerun, and which results are reported-only. Adoption is the user's decision: a rejected variant under the frozen grid can still be adopted by the user as an explicit exception; record it as such in `../context/decisions.md`.
