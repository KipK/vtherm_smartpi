# Maintaining the memory

The long-term memory (`.agents/context/`) is the current truth about the project, shared with every developer. It is not a log and not a dump of a development session. Git history is the archive: delete, do not keep tombstones.

## The two tiers

- **Short-term**: `.agents/local/tasks/<task-slug>/` (gitignored, local to one checkout).
  - `plan.md`: the validated plan (template `templates/plan.md`).
  - `notes.md`: findings, dead ends, measurements, bench verdict details, field readings to follow, anything useful to this task only.
  - `handoff.md`: state for resumption and for sub-agent briefs (template `templates/handoff.md`).
  - Created at planning of a non-trivial task, `handoff.md` updated at milestones and before an automatic context compaction, deleted at close after promotion.
- **Long-term**: `.agents/context/` (versioned). Written only through the admission test below.

## Admission test (all four must hold)

1. A future session working on a **different** task will need it.
2. It stays true beyond this task and this release.
3. It cannot be found with two greps in the code (a non-obvious coupling, a trap, a reason, a decision).
4. It is current state: no history, dates, lot or bench identifiers, logs, raw numbers that do not justify a decision.

Everything else stays in the task folder or goes into the commit message. Binding owner decisions always pass: one or two lines in `principles.md` (standing rule) or `decisions.md` (outcome).

## End-of-task checklist (every agent that changed code or took a decision)

1. **Promote**: read the task's `notes.md`; keep 0 to 3 facts that pass the admission test and write each in the file the trigger table names (rewrite the existing paragraph rather than appending).
2. **Update** the files the code change touched (trigger table), in the same change as the code; update `context/status.md` only when a long-running workstream started, moved or finished, or an owner question was opened or answered (lifecycle below).
3. **Prune** what the change made wrong or obsolete.
4. Run `python3 .agents/scripts/check_memory.py` (paths, symbols, constants, `live.*` paths, size budgets, privacy patterns, profile parse/mirror, nothing tracked under `.agents/local/`, task folders untouched for 30 days, `status.md` over 60 lines). Optionally keep a private word list (names of your rooms, people) in the untracked `.agents/.local-blocklist` or the file named by `SMARTPI_MEMORY_BLOCKLIST`; the check then fails when any of those words appears. Fix every ERROR; treat WARN as a prune request.
5. Report in your final message: "memory: updated X, Y" or "memory: nothing to update because ...".
6. Delete the task folder when the user considers the task done.

Who writes: the agent that implemented the change updates the memory (it knows the facts). Read-only profiles (architect, reviewer, researcher, debugger) do not edit; they list what must be recorded in their report, and the orchestrating session applies it. The reviewer checks that the memory diff matches the code diff.

## Trigger table

| Event | Update |
|-------|--------|
| New, moved, renamed or deleted module/file; role change | `context/architecture.md` (map, "where do I change X", names that do not exist) |
| Change of setup, config resolution, tick pipeline, persistence, services, sensor/diagnostic dispatch | `context/runtime-flow.md` |
| Change of regulation, gains, learning gates, feed-forward, bootstrap, calibration | `context/algorithm-core.md` |
| Change in room_history, room_model, quiet_periods, power steps, restart bridge | `context/algorithm-room-model.md` |
| Published diagnostics field added/renamed/removed, status strings, history series | `context/diagnostics.md` (+ schema test fixture, `cards/`, docs, Equinox note) |
| Mechanism adopted, removed or rejected (with the reason) | `context/decisions.md` |
| Durable trap discovered (cost someone hours; will bite again) | `context/pitfalls.md` |
| Owner states a rule or preference | `context/principles.md` |
| New measured field fact (scales, sensors, emitters) | `context/field-knowledge.md` (anonymized) |
| A constant quoted in the memory changes value or name | the file quoting it (`check_memory.py` flags `NAME` (value) mismatches and unknown constants) |
| Branch/release/dependency/version facts change | `context/project.md` |
| Test, bench, release, docs or delegation procedure changes | the matching `workflows/*.md` |
| Agent profile added, removed or retuned | both `.claude/agents/` and `.codex/agents/`, then the table in `workflows/delegation.md` |
| Long-running workstream started, advanced or finished; owner question opened or answered | `context/status.md` (details stay in the task's `notes.md`) |

## `context/status.md` lifecycle

An index of the branch's long-running workstreams, next steps and open owner questions, at most 60 lines; one to three lines per item (goal, state, next step).
- Never in it: narratives, bench numbers, screening results, field readings to follow, dates, lot or bench identifiers, tester data, logs. They go to the task's `notes.md`.
- When a workstream is ADOPTED and merged: move the mechanism to `decisions.md` (adopted table, one row), the new traps to `pitfalls.md`, the code facts to the architecture/algorithm files, then delete the item.
- When it is REJECTED: add one row to the "Removed / rejected" table of `decisions.md` (what, why, what to do instead), then delete the item.
- When an open question is answered: a standing rule goes to `principles.md`; otherwise record the outcome where it applies; delete the question.
- When work ships in a release: remove it (the changelog and decisions hold it).
- Branch rule: `master` holds master's state and plans only. On a merge, resolve `status.md` to the merged result (drop what the merge completed, keep what remains).

## Pruning rules (what no longer belongs)

Delete or rewrite when:
- the code it describes no longer exists or changed (verify with grep, never by memory); the replacement fact goes in the same place;
- it is history ("was", "used to", "new", "now", dated entries), a bench log, raw numbers that do not justify a decision, or a conclusion superseded by a later one;
- it duplicates another file (keep the more specific one, link from the other);
- it is task-local context that no future session needs (it belongs in `.agents/local/tasks/`);
- a "names that do not exist" entry that nobody would plausibly look for anymore (keep those lists under 15 names);
- it cites a gitignored local folder, a plan, a lot, or a review identifier.
Prefer a short rewrite over appending. When a file passes its size budget (about 400 lines for a context file), split by theme or cut the least useful content; do not just keep growing.

## Periodic audit

Run it before a release, after a merge between long-lived branches, and whenever `check_memory.py` warns repeatedly:
1. `python3 .agents/scripts/check_memory.py` and fix everything.
2. Per context file, have a cheap read-only explorer verify every module, constant and path claimed, and list the statements that cannot be verified; delete or rewrite them.
3. Re-read `status.md`, `decisions.md` and `pitfalls.md` against the current code for items that completed, were abandoned or became obsolete. Promote and delete the local task folders of finished tasks.
4. Check `principles.md` against the owner's latest instructions; check `AGENTS.md` stays under its size budget and that every file it names exists.
5. Check the agent profiles still match the delegation table and the models in use.

## Merge and conflict handling

Memory files merge like code. On a conflict, take the version that matches the merged code, then re-run the checker. Never resolve a memory conflict by keeping both versions.
