# Project overview

## What SmartPI is

SmartPI is a Home Assistant custom integration (`custom_components/vtherm_smartpi`, domain `vtherm_smartpi`) that registers a **proportional algorithm plugin** for **Versatile Thermostat (VT)**. VT owns the thermostat entity, the sensors, the PWM/valve cycle and the safety logic; SmartPI computes the normalized power command in [0, 1] each tick. SmartPI talks to VT only through the `vtherm_api` package (handler factory registration, applied-command receipts).

SmartPI is a discrete **PI regulator on a 1R1C thermal model** with adaptive gains, feed-forward, dead-time estimation, learning governance, calibration and valve-curve linearization. The 0.7 line adds emitter lag learning, a room model, quiet-period selection and a heating balance. Theory basis: Astrom & Hagglund, *PID Controllers* (2nd ed.) (see `theory.md`).

Users install it through HACS (zip release) or by copying the integration folder into Home Assistant. Minimum HA 2025.8 (`hacs.json`), Python >= 3.13, VT >= 10.6.2 (enforced at setup by `smartpi/compatibility.py`), `vtherm_api >= 0.7.0` (see `manifest.json` for the exact requirements).

## Repositories and layout (monorepo workspace)

Sibling folders of the workspace (paths relative to the workspace root, which contains this repo as `vtherm_smartpi/`):

| Folder | What | Notes |
|--------|------|-------|
| `vtherm_smartpi` | this repo | branches: `master` (released 0.6.x), `dev-0.7` (development line) |
| `versatile_thermostat` | VT integration (upstream project) | read-only reference; changes go through upstream PRs |
| `vtherm_api` | plugin API between VT and algorithms | upstream-bound: no plan/phase/review vocabulary in comments, docs or tests |
| `vtherm_hysteresis` | simple hysteresis plugin | scaffold example for VT plugins |
| `vtherm_adaptive_tpi` | adaptive TPI plugin (1R1C, learns Kint/Kext) | a "mini SmartPI" without the integral and FF2/FF3 |
| `versatile-thermostat-ui-card` | official VT card | |
| `equinox` | dashboard card for VT climates, reads SmartPI diagnostics | branch `smartpi-0.7` follows dev-0.7; a second local checkout of Equinox (a worktree on its release branch) follows the release; `dist/equinox-card.js` is tracked: rebuild with `npm run build` and commit with the source |

A sibling folder may be absent in another environment; the SmartPI repo is self-sufficient for most tasks, but its integration tests need `versatile_thermostat` and `vtherm_api` importable (see `../workflows/testing.md`).

## Local-only folders (gitignored, not available to other readers)

`plans/`, `dev_tests/`, `tools/` are in `.gitignore`: personal plans, reference notes, history analysis scripts, bench drivers and scenarios live there on the maintainer's machine. This `.agents/` tree is the versioned, shareable memory; never make it depend on those folders.

## Release lines and branches

- `master`: released line (0.6.x). Releases are tags `X.Y.Z` on master; a workflow builds the zip and a draft release.
- `dev-0.7`: development line with the 0.7 features; its user-visible changes are in `CHANGELOG.md` under `[Unreleased]`.
- Other short-lived branches exist for experiments; they are not part of the shared knowledge.
- Dev install: the maintainer copies `custom_components/vtherm_smartpi` into Home Assistant and restarts it; HACS keeps showing the last released version. To know what runs, look for a diagnostics field introduced by the expected commit (for example `analysis.learning.heating_balance`), not the HACS version. After copying from a version without restart context (0.6.x), the first restart shows `last_restart: no saved state of the last tick`, which is expected.

## Manifest requirement trap

Before the `vtherm_api` 0.7.0 release is on PyPI, field testing may use an UNCOMMITTED local manifest edit pointing `vtherm_api` to a git URL (VT and SmartPI manifests). Never commit it; stage files explicitly (never `git add -A`/`-u`) while it exists; restore the manifest (`git checkout -- <manifest>`) before a release or an upstream PR. HA treats a URL requirement as never satisfied and reinstalls it at each start. If the shared venv shows an old `vtherm_api`, reinstall it editable from the sibling folder (`pip install --no-deps -e <workspace>/vtherm_api`).

## Languages

- Code, comments, docstrings, commit messages, this `.agents/` tree: English.
- User-facing docs: English and French (`README.md`/`README.fr.md`, `documentation/{en,fr}`, `cards/*-{en,fr}.md`), plus Home Assistant translations in `custom_components/vtherm_smartpi/translations/`.
- The maintainer usually talks in French: answer in the language the user writes in.
