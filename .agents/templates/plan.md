# Plan: <task title>

Location: `.agents/local/tasks/<task-slug>/plan.md` (local, never versioned).

## Goal
One or two sentences; acceptance criteria.

## Verified facts
- `path::Symbol` - what it does today (how verified).
- Unverified: ...

## Principles and decisions check
Which rules of `context/principles.md` apply; is the idea already rejected in `context/decisions.md`?

## Options
| Option | Pros | Cons |
| --- | --- | --- |

Chosen: ... because ... (simplicity, universality, one mechanism per job, architecture fit).

## Changes
- Files and symbols touched; new `smartpi/` modules.
- Published diagnostics, persisted state, config or VT-contract changes; backward compatibility of saved states.

## Evidence
- Tests to add or change; targeted commands; full suite needed?
- Regulation or learning change: expected effect, invariants kept, bench declaration (`templates/bench-declaration.md`).

## Docs, translations, memory
- Docs pages (EN/FR), cards, translation keys, `CHANGELOG.md`, Equinox impact.
- Expected `.agents/context` updates.

## Open questions for the user
