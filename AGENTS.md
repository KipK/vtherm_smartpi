# AGENTS.md - SmartPI (Versatile Thermostat plugin)

Instructions for any AI coding or debugging agent (Claude Code, Codex, others) working in this repository. They are meant to be sufficient on their own: a new session needs only the user's problem statement. Deeper knowledge lives in `.agents/` (versioned, shared memory). Answer in the language the user writes in (often French); code, comments, commit messages and `.agents/` are in English.

## What this is

SmartPI is a Home Assistant custom integration (`custom_components/vtherm_smartpi`) that plugs into **Versatile Thermostat (VT)** as a proportional algorithm through the `vtherm_api` package. It is a **discrete PI regulator on a 1R1C thermal model** (adaptive gains, feed-forward, dead-time estimation, learning governance, calibration, valve linearization; 0.7 line: response model (dead time and response time constant), room model, quiet periods). It follows Astrom & Hagglund, *PID Controllers* (2nd ed.). VT owns the entity, sensors, PWM/valve cycle and safety; SmartPI computes a normalized power in [0, 1]. Branches: `master` = development toward the 1.0 release, `0.6-hotfixes` = maintenance of the released 0.6.x line (see `.agents/context/project.md`).

## Start of a session: what to read

Always read `.agents/README.md` (index), `.agents/context/principles.md` (binding decisions) and `.agents/context/status.md` (workstreams, open questions and next steps of the current branch). When resuming a task, read its `.agents/local/tasks/<task-slug>/handoff.md` first. Then, by task:

| Task | Read |
|------|------|
| Any code change | `workflows/develop.md`, `context/architecture.md`, relevant theme of `context/pitfalls.md` |
| Regulation/learning change | `context/algorithm-core.md`, `algorithm-room-model.md`, `theory.md`, `decisions.md` (do not re-propose rejected ideas), `workflows/benchmarking.md` |
| Runtime, persistence, VT/HA wiring | `context/runtime-flow.md` |
| User problem / debugging | `workflows/debugging-user-reports.md`, `context/diagnostics.md`, `context/field-knowledge.md` |
| Tests | `workflows/testing.md` |
| Docs, translations, release | `workflows/docs-i18n.md`, `workflows/release.md` |
| Finishing any task | `workflows/memory-maintenance.md` (memory checklist, triggers, pruning) |
| Which sub-agent to use | `workflows/delegation.md` (profiles in `.claude/agents/`, `.codex/agents/`) |

Load only what the task needs. The repo is large: use grep and partial reads; never read `algo.py` (~2700 lines) or `handler.py` whole.

## Hard rules

1. **No hallucination.** Never invent, guess or extrapolate. Every claim rests on code, docs, observed data or a demonstrable argument; mark the rest "unverified". When a decision is not certain, stop and ask the user.
2. **No commits.** Never commit, push, tag, amend or add co-author trailers unless the user expressly asks. After a change, give an English commit message (brief, informative, no links or file paths).
3. **Stay in scope.** Change only what the task needs; do not fix other things unasked. Re-read every diff you produced (`git --no-pager diff ...`) to check that only intended hunks exist. Always `git --no-pager` (the pager blocks the shell).
4. **Tests.** pytest, targeted files first (`workflows/testing.md`). Tests can be slow (on the maintainer's WSL/NTFS machine they are), that is expected: run them in the background and wait for the completion notification. **Never poll** a running job (no repeated tail/ls/sleep checks); if a blocking wait is unavoidable use one call with a hard timeout on a PID or marker, never `pgrep -f`. One bench or simulation at a time (CPU).
5. **Comments and docs.** Code comments and docstrings in English. Documentation describes the current state only, as if never published: no "new/now/fixed/changed" wording (only `CHANGELOG.md` lists changes). Update FR and EN docs and Home Assistant translations (`custom_components/vtherm_smartpi/translations/`) after any user-visible change.
6. **Architecture.** `algo.py` and `handler.py` must not grow: put logic in `custom_components/vtherm_smartpi/smartpi/` modules. `SmartPI.calculate` positional order is a VT contract; check VT callers before changing any signature VT calls.
7. **Control principles.** Regulation changes must stay coherent with Astrom-Hagglund practice and the project principles: simple, universal for any on/off emitter, neutral start, bounded, one mechanism per job, HEAT first, disturbances excluded not modelled, no declared occupancy schedules, sensor health is VT's job (see `context/principles.md`). Evidence (test or frozen bench) before adopting a regulation change.
8. **Published diagnostics are an API** (Equinox card, Markdown cards, docs). Keep names plain (no internal codes), pin changes in `tests/test_published_diagnostics_schema.py`, update `cards/` and docs, note the Equinox impact.
9. **Upstream repos** (`versatile_thermostat`, `vtherm_api`): never mention plans, phases, decisions or review IDs in their comments, docs or tests.
10. **Privacy.** Never write credentials, tokens, URLs/addresses, personal names, emails, real entity ids, room nicknames, tester data or habits into any file of this repo, including `.agents/`. Never send such data to external services. Describe tester/field data qualitatively.
11. **Manifests.** Never commit a manifest edit that points `vtherm_api` to a git URL; stage files explicitly (never `git add -A`) when such a local edit exists.
12. **Cost discipline.** Cheapest adequate model per task, tight briefs, short reports, fresh sub-agent per task. The strongest model (Fable) only with the user's explicit approval per use.

## Commands

```
cd <repo root>
pytest -q tests/test_smartpi_feedback.py        # one file
pytest -q tests -k "deadtime"                   # by keyword
pytest -q tests                                 # full suite (slow, background)
git --no-pager status --short ; git --no-pager diff --stat
```
Python >= 3.13, virtualenv `.venv`. Tests import `versatile_thermostat` and `vtherm_api` from sibling folders of the repo (see `.agents/workflows/testing.md`).

## Repository map (short)

- `custom_components/vtherm_smartpi/` - integration: `__init__.py` (setup, services), `handler.py` (VT bridge), `algo.py` (`SmartPI` orchestrator), `config_flow.py`, `sensor.py` (diagnostics sensor), `state_store.py`, `smartpi/` (all domain logic: controller, gains, setpoint, learning, feed-forward, diagnostics, `room_model/`, `room_history/`, `quiet_periods/`), `translations/`.
- `tests/` - pytest suite with fakes; `documentation/{en,fr}/`, `cards/`, `README*.md`, `CHANGELOG.md`.
- `plans/`, `dev_tests/`, `tools/` are gitignored local folders of the maintainer (plans, reference notes, bench drivers): they may not exist for you; nothing in `.agents/` depends on them.
- Full map: `.agents/context/architecture.md`.

## Debug mode for users

If a user launched you to find why SmartPI misbehaves: follow `.agents/workflows/debugging-user-reports.md`; read data from the diagnostics sensor (`live`, `history` attributes) per `.agents/context/diagnostics.md`; stay read-only on their system; do not call learning-reset or calibration services without consent; ask them to redact personal data; produce the report in `.agents/templates/issue-report.md` format.

## Two memory tiers

- **Long-term, versioned**: `.agents/context/` - the current truth about the project, shared with every developer. A fact enters it only if it passes the admission test of `.agents/workflows/memory-maintenance.md`.
- **Short-term, local, never versioned**: `.agents/local/tasks/<task-slug>/` (gitignored) with `plan.md`, `notes.md`, `handoff.md`. Work in progress, findings, dead ends, measurements, bench details, resumption state. Deleted when the task closes, after promoting the few durable facts.

Plan first: a non-trivial task starts with `plan.md` (template `.agents/templates/plan.md`) validated by the user before code; `handoff.md` (template `.agents/templates/handoff.md`) is kept current at milestones and before a context compaction, and is the brief for sub-agents.

Every task ends with the checklist of `.agents/workflows/memory-maintenance.md`: promote 0-3 durable facts from the task folder, update the files the change touched (trigger table there), prune what became obsolete (delete, git is the archive), run `python3 .agents/scripts/check_memory.py`, and say in the final message what memory was updated. Read-only profiles list what should be recorded; the orchestrating session applies it. A rule stated by the user goes into `context/principles.md`. Agent profiles in `.claude/agents/` and `.codex/agents/` mirror each other: edit both.
