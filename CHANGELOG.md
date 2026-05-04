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

## [0.2.0] - 2026-04-26

- Valve power curve linearisation
  Inspired from @caiusseverus blueprint
  improve valve non linear power curve. more info: https://github.com/jmcollin78/versatile_thermostat/discussions/1704

  ## [0.2.1] - 2026-04-29

- Implement setpoint landing cap for SmartPI
  Adds model-aware landing control to the setpoint filter so heating setpoint increases can reduce internal demand before reaching the target. The proportional trajectory now exposes a HEAT-only landing cap, applied after PI computation and before soft constraints, while keeping the raw PI diagnostic and integral behavior unchanged.

  ## [0.2.2] - 2026-04-29

- Fix landing cap residual release
  Avoid keeping the setpoint landing cap active in release phase when only a small residual error remains. Add coverage for the stuck filtered setpoint case.

  ## [0.3.0] - 2026-04-30

- Improve SmartPI landing release safety with slope timing guard
- Add guarded SmartPI landing cap release
  Release landing when its cap is non-constraining in the residual zone, keep the release sticky, and reset counters on inactive landing paths.
- Make SmartPI landing residual release sticky
  Prevent the landing cap from reactivating after residual release during the same trajectory, while allowing rearm when demand becomes significant again. Add focused landing coverage.
- updated markdown cards


  ## [0.3.1] - 2026-05-04

- Fix SmartPI climate state publishing on sensor updates
- Deadtime-aware open-loop prediction for FF3
- Stabilize SmartPI valve output near minimum activation delay