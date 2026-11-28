# Developing a change

1. **Understand.** Read `../context/principles.md`, `../context/architecture.md` ("Where do I change X?"), the relevant theme of `../context/pitfalls.md` and `../context/decisions.md` (is the idea already rejected?). Locate code with grep and partial reads; `algo.py` and `handler.py` are big: never read them whole.
2. **Plan.** Trivial task (one file, obvious fix, no behaviour or contract change): a three-line plan in the conversation. Otherwise create `.agents/local/tasks/<task-slug>/plan.md` from `../templates/plan.md` and have the user validate it before writing code; revise the plan (and tell the user) when the implementation reveals a different path. Record findings, dead ends and measurements in `notes.md` of the folder as they happen; keep `handoff.md` (`../templates/handoff.md`) current at each milestone.
3. **Place the change.** Logic goes into a `smartpi/` module (existing or a new focused one). `algo.py` orchestrates, `handler.py` is VT/HA glue. Prefer small pure helpers (testable).
4. **Decide with evidence.** A regulation or learning change needs a stated expected effect, the invariants kept and a test or bench (see `benchmarking.md`). If the decision is a design choice not covered by the principles, stop and ask the user.
5. **Implement minimally.** Change only what the task needs. English comments. No "new/changed" wording in comments or docs. Keep `time` calls as `time.monotonic()` / module-level so tests and benches can patch them.
6. **Tests.** Add or adapt tests next to the existing ones (`testing.md`). Run the targeted files in the background and wait for the notification.
7. **Compat checks** when relevant:
   - Changed published diagnostics -> `tests/test_published_diagnostics_schema.py` fixture (regenerate only for an intended change), `cards/`, `documentation/{en,fr}/diagnostics.md`, record the Equinox path impact.
   - Changed config/options -> `config_flow.py`, `const.py` defaults, both `translations/*.json`, docs EN/FR.
   - Changed persisted state -> `save_state`/`load_state` on `SmartPI`, backward compatibility of old stored states, restart behaviour (FF factors must come back with the restored integral).
   - Changed a signature VT calls -> grep VT (`_prop_algorithm.calculate`, receipts API) first; `SmartPI.calculate` positional order is a VT contract.
   - Changed anything about command application or applied-command receipts -> `test_smartpi_cycle_alignment`, `test_smartpi_valve_runtime_sync`, `test_smartpi_handler_safety`.
8. **Re-read your diff** (`git --no-pager diff --stat`, then per file). Check that only intended hunks exist.
9. **Docs.** Update user/technical docs and `CHANGELOG.md` `[Unreleased]` for user-visible behaviour (`docs-i18n.md`). Docs state the current behaviour only.
10. **Hand over.** Summarize the change, the evidence, what is unverified, and give an English commit message (brief but informative, no links or paths). Do NOT commit unless asked. Finish with the checklist of `memory-maintenance.md` (promote 0-3 durable facts from the task folder, update `../context/*` per its trigger table, prune, run `scripts/check_memory.py`).
