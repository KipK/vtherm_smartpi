# Documentation and translations

- Locations: `README.md`, `README.fr.md`; `documentation/en|fr/{vtherm_smartpi,technical_doc,diagnostics}.md`; `cards/smartpi_diags-{en,fr}.md` (Markdown dashboard cards); Home Assistant UI strings in `custom_components/vtherm_smartpi/translations/*.json`; `services.yaml`; `CHANGELOG.md`.
- **Docs describe the current state only**, as if the project had never been published: no "new", "now", "fixed", "changed", "no longer". Only `CHANGELOG.md` lists changes.
- A user-visible change (service, option, diagnostic field, threshold, behaviour) updates: the doc source language, the other language, the HA translations if labels/descriptions changed, the cards if a diagnostics path changed, and the changelog.
- Write the source language (usually French for the maintainer's docs; English for code-adjacent texts), then have the other language produced by the cheap translator profile (`smartpi-translator`); keep code identifiers, paths, units, numbers untouched.
- User-facing diagnostics values must be understandable by an outside user: no internal bench/rule names or codes. Translate at the diagnostics boundary and keep the doc tables in sync.
- Markdown cards: guard `states[entity_id] is not none` before reading attributes (`states(entity_id)` returns the truthy string `unknown` for a missing entity); read near-band state from `live.control.in_near_band`; display `live.learning.enabled` independently of learning progress.
- Check translations separately: README/documentation translations and HA translation JSON are different files.
- Privacy: no real entity ids, addresses, names or tester data in docs/examples; use generic names.
