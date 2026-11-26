# .agents - shared project memory for AI agents

Versioned, tool-neutral memory of the SmartPI project. `AGENTS.md` (repo root) is the entry point and holds the binding rules; this tree holds the knowledge. Everything here is current-state, English, free of personal data, and independent of the gitignored local folders (`plans/`, `dev_tests/`, `tools/`).

## Layout

| Path | Content |
|------|---------|
| `context/status.md` | BRANCH-SPECIFIC: ongoing work, bench verdicts, next steps, open questions of the current branch |
| `context/principles.md` | Binding product, process and evidence decisions of the maintainer |
| `context/project.md` | What SmartPI is, sibling repos, branches, release lines, manifest trap, languages |
| `context/theory.md` | Control theory and thermal physics background (Astrom-Hagglund, 1R1C, tuning, estimation) |
| `context/architecture.md` | Verified code map, tests by area, "where do I change X?", names that do not exist |
| `context/runtime-flow.md` | Setup, config resolution, handler lifecycle, tick pipeline, persistence, services, sensor |
| `context/algorithm-core.md` | PI, gains, setpoint, A/B learning, dead time, bootstrap, measurement events and starting values, calibration, feed-forward, guards |
| `context/algorithm-room-model.md` | 0.7 subsystems: room history, room model, quiet periods, power steps, restart bridge |
| `context/diagnostics.md` | How to read published diagnostics, symptom table, status strings, triage |
| `context/pitfalls.md` | Durable traps by theme (regulation, learning, persistence, VT/HA, tests/benches, sensors) |
| `context/decisions.md` | Adopted mechanisms, rejected/removed approaches and why, release timeline, open questions |
| `context/field-knowledge.md` | Anonymized real-room scales, sensors, emitters, observed behaviours |
| `workflows/` | memory-maintenance (checklist, triggers, pruning, audit), develop, testing, benchmarking, debugging-user-reports, release, docs-i18n, delegation |
| `scripts/check_memory.py` | Consistency check: paths/symbols/constants/`live.*` paths, size budgets, privacy patterns (+ optional local blocklist), profile parse and mirror |
| `templates/` | bench declaration, diagnostic report |

Agent profiles: `.claude/agents/*.md` (Claude Code) and `.codex/agents/*.toml` (Codex), same names, mirrored. Claude project settings: `.claude/settings.json`; Codex project config: `.codex/config.toml`.

## Rules for editing this memory

1. Current state only; no dates, no "new/changed", no plan/bench/review identifiers.
2. Reference code as `path::symbol`; avoid line numbers (they rot) and avoid copying values that change often (name the constant and its file).
3. One fact in one place; link instead of duplicating.
4. Verify against the code before writing; write "unverified" otherwise.
5. No personal data: names, emails, credentials, tokens, addresses, real entity ids, room nicknames, tester data.
6. Keep files short enough to load whole; split by theme rather than grow a file.
7. Update it in the same change as the code it describes, and prune obsolete content: see `workflows/memory-maintenance.md`.

## Memory and branches

The memory is versioned with the code, so each branch carries its own state.
- Branch-neutral files (principles, theory, project, field knowledge, workflows, templates, agent profiles) change rarely and merge like any file.
- Code-dependent files (architecture, runtime-flow, algorithm-*, diagnostics, pitfalls, decisions) describe the code of the branch they are on; update them in the same commit as the code, so a merge brings them along and conflicts are resolved to the merged code.
- `context/status.md` is the in-progress journal of the branch: on `master` only master's state and plans, on a development branch its own work. On merge, resolve it to the merged result. Do not create files named after branches.
