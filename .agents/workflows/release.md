# Release procedure (prepared by agents, executed by the maintainer)

Never tag, push or publish without the maintainer's explicit request.

## Checklist

1. Branch: releases are cut from `master`. Features of a development line are merged by the maintainer.
2. **Manifest hygiene.** `custom_components/vtherm_smartpi/manifest.json` must not carry an uncommitted edit pointing `vtherm_api` to a git URL; requirements must be `vtherm_api>=<released version>`, `numpy`, `scipy>=1.14`. The `version` field stays `0.0.0` in git: the release workflow writes the tag into it.
3. **Compatibility.** `hacs.json` minimum Home Assistant; `smartpi/compatibility.py` VT minimum version matches the supported VT.
4. **Docs.** README EN/FR, `documentation/{en,fr}`, `cards/`, translations are in sync with the code and describe the current state (see `docs-i18n.md`). Published diagnostics fixture test passes.
5. **Memory audit.** Run the periodic audit of `memory-maintenance.md`; `status.md` no longer lists what ships.
6. **Tests.** Full suite green (run in the background, wait for the notification).
7. **CHANGELOG.md.** Move `[Unreleased]` entries under the new version heading; user-visible wording, no internal names.
8. **Release notes** (draft for the maintainer): sections Changed / Fixed / Diagnostics, then the generated changelog link.
9. The maintainer pushes the tag `X.Y.Z` (or `X.Y.Z-rc.N` for a pre-release) on master. The `Release` workflow sets the manifest version, zips `custom_components/vtherm_smartpi`, and creates a DRAFT GitHub release. Then `gh release edit X.Y.Z --notes-file <notes> --draft=false` publishes it.
10. After a release, check that the development line includes the release changes (merge master into it).

## Equinox

If published diagnostics paths changed: update `cards/`, then Equinox (`src/data/dashboards/regulation/smartpi.json` in its repo), `npm run build`, commit the rebuilt `dist/equinox-card.js` with the source, `npm run test:smartpi`; keep its path fixture a superset of SmartPI's published paths.
