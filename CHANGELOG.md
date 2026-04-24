# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-04-15

- Initial release of Versatile Thermostat SmartPI integration
- SmartPI proportional algorithm implementation
- HACS integration support
- Comprehensive documentation in French and English
- GitHub Actions for automated validation and releases
-

## [0.1.2] - 2026-04-18

- replace vtherm api to main repo

## [0.1.3] - 2026-04-18

- harden plugin services and refresh state after reset

## [0.1.4] - 2026-04-19

Fix GuardState runtime typing in SmartPI guards

Auto-create SmartPI default config entry on first setup

## [0.1.5] - 2026-04-19

Remove default config entry in menu

## [0.1.6] - 2026-04-20

Fix diagnostics sensor creation for default-bound thermostats
Expose SmartPI phase as diagnostics sensor state
Mark SmartPI diagnostics sensor as diagnostic entity

## [0.1.7] - 2026-04-20

switch to official vtherm_api package

## [0.1.8] - 2026-04-21

Align prop handler state change hook with changed flag API

## [0.1.9] - 2026-04-22

remove vtherm_api dependency ( use VT installed one )

## [0.1.10] - 2026-04-24

- Sync SmartPI committed power after valve mid-cycle updates
- Fix SmartPI default diagnostics registration
- Quicker boostrap phase:
  Adjust SmartPI bootstrap thresholds and AB publication
 (now needs only 8 B & 6 A before quitting bootstrap mode.)
