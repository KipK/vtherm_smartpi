# Maintaining the shared memory

The memory is the current truth about the project, not a log. It stays useful only if every task leaves it correct and smaller-or-equal in noise. Git history is the archive: delete, do not keep tombstones.

## End-of-task checklist (every agent that changed code or took a decision)

1. Which row of the trigger table below did my change hit? Update those files in the SAME change as the code.
2. Update `context/status.md` if a step moved, a verdict arrived, a decision was taken or a question was answered (lifecycle below).
3. Run `python3 .agents/scripts/check_memory.py` (paths, symbols, constants, `live.*` paths, size budgets, privacy patterns, profile parse/mirror). Optionally keep a private word list (names of your rooms, people) in the untracked `.agents/.local-blocklist` or the file named by `SMARTPI_MEMORY_BLOCKLIST`; the check then fails when any of those words appears. Fix every ERROR; treat WARN as a prune request.
4. Report in your final message: "memory: updated X, Y" or "memory: nothing to update because ...".

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
| Work moves forward on the branch (bench verdict, adoption, new question) | `context/status.md` |

## `context/status.md` lifecycle

It is the branch's working journal and must shrink as often as it grows.
- Add: a started lot (goal, mechanism, expected effect), a verdict (rule outcome in plain words), a next step, an open question for the owner.
- When a lot is ADOPTED and merged: move the mechanism to `decisions.md` (adopted table), the new traps to `pitfalls.md`, the code facts to the architecture/algorithm files, then delete the lot from `status.md`.
- When a lot is REJECTED: add one row to the "Removed / rejected" table of `decisions.md` (what, why, what to do instead), then delete it from `status.md`.
- When an open question is answered: if it is a standing rule, put it in `principles.md`; otherwise record the outcome where it applies; delete the question.
- When work ships in a release: remove it from `status.md` (the changelog and decisions hold it).
- Keep it under about 100 lines. No dates, no lot or bench identifiers, no tester data, no logs.
- Branch rule: `master` holds master's state and plans only. On a merge, resolve `status.md` to the merged result (drop what the merge completed, keep what remains).

## Pruning rules (what no longer belongs)

Delete or rewrite when:
- the code it describes no longer exists or changed (verify with grep, never by memory); the replacement fact goes in the same place;
- it is history ("was", "used to", "new", "now", dated entries), a bench log, raw numbers that do not justify a decision, or a conclusion superseded by a later one;
- it duplicates another file (keep the more specific one, link from the other);
- it is task-local context that no future session needs;
- a "names that do not exist" entry that nobody would plausibly look for anymore (keep those lists under 15 names);
- it cites a gitignored local folder, a plan, a lot, or a review identifier.
Prefer a short rewrite over appending. When a file passes its size budget (about 400 lines for a context file), split by theme or cut the least useful content; do not just keep growing.

## Periodic audit

Run it before a release, after a merge between long-lived branches, and whenever `check_memory.py` warns repeatedly:
1. `python3 .agents/scripts/check_memory.py` and fix everything.
2. Per context file, have a cheap read-only explorer verify every module, constant and path claimed, and list the statements that cannot be verified; delete or rewrite them.
3. Re-read `status.md`, `decisions.md` and `pitfalls.md` against the current code for items that completed, were abandoned or became obsolete.
4. Check `principles.md` against the owner's latest instructions; check `AGENTS.md` stays under its size budget and that every file it names exists.
5. Check the agent profiles still match the delegation table and the models in use.

## Merge and conflict handling

Memory files merge like code. On a conflict, take the version that matches the merged code, then re-run the checker. Never resolve a memory conflict by keeping both versions.
