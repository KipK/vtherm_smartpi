# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

- Outdoor sensor without a value: SmartPI keeps using the last valid outdoor
  temperature for the feed-forward, the setpoint model and the near band, with
  no age limit, and the integral keeps integrating; gains, learning and the FF
  trim stay frozen (`DEGRADED` regime). Until a first outdoor temperature has
  been received, the feed-forward and the FF trim are zero and the integral is
  held (`governance_sensor_invalid`). Effect: much smaller error while the
  outdoor sensor is out (error integral over the outage about 4 times smaller
  on the dropout bench).
- The integration step after a gap between two calculations is capped at one
  cycle. Before, a gap up to ten cycles was integrated in full and a longer one
  counted for one cycle.
- Periodic FF-trim windows are no longer invalidated by the near-band gain
  taper, periodic proposals survive a saturation freeze, and the trim waits for
  the end of the feed-forward warmup (`ff2_freeze_reason` `ff_warmup`). Effect:
  the periodic trim is admitted and applied far more often.
- A regime transition no longer holds the integrator; gains and learning stay
  frozen. The integral is held only in the deadband hysteresis shell and in the
  `DEGRADED` regime until a first outdoor temperature has been received.
  Effect: smaller steady error after setpoint or regime
  changes, and the FF trim learns more often.
- Anti-windup works per tick on the applicable limit (`max_on_percent`, governor
  cap) and only drains the accumulated integral; the end-of-cycle tracking is
  removed. Effect: no steady offset below the setpoint when Versatile
  Thermostat holds back valve positions (`dpercent`), with possibly more
  overshoot in that configuration. The `aw_du` diagnostic is the correction of
  the last tick.
- In valve mode with linearization, `max_on_percent` limits the valve opening
  sent to Versatile Thermostat, as Versatile Thermostat does: SmartPI caps its
  linear demand at the curve inverse of `max_on_percent`. Before, the cap applied
  to the linear demand gave a much smaller opening (13 % instead of 60 % for
  `max_on_percent` 0.6 with the default curve). Switch mode and valves without
  linearization are unchanged.
- SmartPI attributes each applied command to its request through the
  applied-command receipts of Versatile Thermostat (`vtherm_api` 0.7.0 or later).
  The feed-forward trim and the bumpless transfer require a Versatile Thermostat
  release whose cycle scheduler emits these receipts. With an older Versatile
  Thermostat, a warning is logged and PI regulation is unchanged, but the
  periodic trim and the bumpless transfer stay inactive and the stationary trim
  uses the sampled feed-forward.
- In valve mode, commands are attributed to their SmartPI request, so the
  feed-forward trim and the bumpless transfer work with valves. A valve opening
  held back by Versatile Thermostat (regulation threshold or minimum period) or
  set by its deferred update is not attributed to SmartPI.
- `feedforward.fftrim.command_ownership` diagnostics report the receipts:
  `receipts_supported`, `kind`, `alterations` and `rejection_counts` replace
  `projected_power`, `projected_on_time_sec`, `projected_off_time_sec`,
  `expected_actuator_power`, `scheduler_realized_power` and `actuator_power_delta`;
  `power_delta` is the applied minus the requested power.
- Fix AutoCalib event payloads: `entity_id` now carries the climate entity id
  (fixing the "Invalid entity ID" recorder warning) and the thermostat name is
  in `name`. Automations that read the name from `entity_id` must read `name`.
- Turning the thermostat OFF during the PWM off phase now cancels the running cycle.
- In Versatile Thermostat safety mode the safety default power is applied instead of
  the SmartPI command, and those cycles are not used as SmartPI-owned commands.
- An options change reloads only the affected thermostats, once. Disabled SmartPI
  entries are ignored (thermostats fall back to the enabled defaults entry or to
  built-in defaults), and the defaults entry can be recreated after deletion.
  SmartPI services stay registered when a plugin entry is reloaded.
- Changing between HEAT and COOL across a restart now resets the integral learned
  on the other mode, and skipped ticks (missing sensor, guard cut) no longer
  integrate the error over the skipped period.
- SmartPI state is written at most every five minutes (and on shutdown, service
  calls and removal) instead of on every control pass.
- Restart now restores the temperature histories and the calibration timeout;
  dead time detection in progress is not resumed.
- Learning slopes are computed on raw temperature measurements instead of the
  filtered temperature, which avoids biased early slow slopes.
- The `a` and `b` drift detectors number their candidates separately, so a genuine
  drift on one channel is no longer blocked by measurements of the other.
- SmartPI services use the standard entity target (Versatile Thermostat climates
  only) and raise a validation error when no SmartPI thermostat matches.
- COOL uses the passive (rise) dead time for braking, trajectory floor, governor
  stop delay and thermal twin power-off delay.
- COOL bootstrap hysteresis mirrors HEAT: cooling starts at setpoint + on
  threshold and stops at setpoint - off threshold.
- A setpoint change during a cycle is flagged in command ownership, and the
  feed-forward trim refuses a bumpless transfer for that cycle.
- `calibration.retry_count` now publishes the AutoCalib retry counter, and
  `analysis.feedforward.reason` no longer reports `ff_ab_fallback`.
- Release of stable HEAT setpoint landing plateaus after cycle confirmation, terminal braking release and a more tolerant reference governor cap.
- In valve mode, the minimal activation and deactivation delays no longer raise
  or zero the valve command: valve position does not depend on ON time.
- A Home Assistant repair warns when the Versatile Thermostat valve regulation
  threshold or minimum period filters SmartPI commands, or when an opening
  degree remap is active with valve linearization. The values are published
  under `live.valve`.
- Internal cleanup of dead code.
- While Versatile Thermostat safety mode applies its default power, the integral
  is held (`integral_hold_source` `vt_safety`) instead of integrating the error
  of a stale temperature.

## [0.4.0]

- Reworked attributes schema and HA database recording rules.
  Now it only records the "history" dict in normal mode and booth "live" and "history" dicts in debug mode.
  This will prevent HA database to be bloated by smartpi diagnostics in non debug mode.
- Add action service to pause and resume SmartPI thermal learning
  This can be usefull to trigger when a secondary heating system could
  bias the thermal model.
- Make the mono-mode 1R1C model fully signed in COOL: learn a negative active
  coefficient, use COOL dead time for IMC gains, and enable signed structural
  feed-forward while preserving the positive passive coefficient.
- Classify learned dead times by physical temperature response in both modes
  and migrate incompatible legacy COOL A/dead-time state conservatively;
  prevent HEAT's passive-dead-time AutoCalib fallback from validating COOL.
- Align FF trim learning with distinct temperature measurements and the delayed
  power that caused them, using independent thermal windows and the learned
  1R1C model.
- Transfer only the causally observed stable integral bias into FF trim at the
  post-anti-windup cycle boundary, while retaining the physical deficit as a
  real bounded command change in both HEAT and COOL.
- Expose the aligned P/I/Ki ownership terms, requested and applied transaction
  deltas, command invariant, rejection reason, and actuator engagement state.
- Bind valve ownership to the integer-percent opening published by the
  thermostat, while retaining scheduler realization as independent evidence.
- Synchronize SmartPI cycle projections with the scheduler effective minimum
  activation and deactivation delays.
- Preserve frozen switch command ownership across successive scheduler
  automatic PWM repeat quantizations without accepting unrelated cycles.
- Allow periodic FF trim windows to use observed cycles with negligible
  scheduler limiting, up to five equivalent cycle seconds.

## [0.3.8] 2026-08-18

- Allow the directional integral guard to unwind opposing PI memory to zero, preventing setpoint recovery from stalling below target.
- Remove the obsolete `hysteresis_thermal_guard` control path and diagnostic attribute, preventing stale guard state from blocking positive integral correction after setpoint changes.
- Expire persisted PI controller memory after more than 96 hours without active regulation while preserving learned A/B thermal coefficients.

## [0.3.7] - 2026-08-09

- Fix SmartPI device linking for single config entry ownership ( multi config entry went obsolete on ha 2026.8.0 )

## [0.3.6] - 2026-05-31

- Fix SmartPI diagnostic sensor recreation after VT reload
- Restrict SmartPI A learning fallback to calibration. Require robust ON slope for SmartPI A learning
- Fix restored SmartPI AB confidence state
- Add HVAC and target context to SmartPI learning windows, and skip B learning in HEAT when the passive heat-loss gradient is too weak or outdoor temperature is too close to target.

## [0.3.5] - 2026-05-23

- Fix SmartPI diagnostic entity persistence

## [0.3.4] - 2026-05-22

- Clean up SmartPI diagnostic entity when target algo changes
- Unlink registered device to config entry when not active

## [0.3.3] - 2026-05-20

- Add SmartPI PWM cycle force option for band transitions ( disabled by default )
- publish ab_bootstrap to confidence attribute in bootstrap mode
- publish specific_states.regulation_diagnostics attribute in vtherm to point diagnostic entitiy path

## [0.3.2] - 2026-05-19

- Dampen proportional output inside SmartPI deadband ( when enable P in deadband is activated )
- some attributes changes
- updated markdown cards

## [0.3.1] - 2026-05-04

- Fix SmartPI climate state publishing on sensor updates
- Deadtime-aware open-loop prediction for FF3
- Stabilize SmartPI valve output near minimum activation delay

## [0.3.0] - 2026-04-30

- Improve SmartPI landing release safety with slope timing guard
- Add guarded SmartPI landing cap release
  Release landing when its cap is non-constraining in the residual zone, keep the release sticky, and reset counters on inactive landing paths.
- Make SmartPI landing residual release sticky
  Prevent the landing cap from reactivating after residual release during the same trajectory, while allowing rearm when demand becomes significant again. Add focused landing coverage.
- updated markdown cards

## [0.2.2] - 2026-04-29

- Fix landing cap residual release
  Avoid keeping the setpoint landing cap active in release phase when only a small residual error remains. Add coverage for the stuck filtered setpoint case.

## [0.2.1] - 2026-04-29

- Implement setpoint landing cap for SmartPI
  Adds model-aware landing control to the setpoint filter so heating setpoint increases can reduce internal demand before reaching the target. The proportional trajectory now exposes a HEAT-only landing cap, applied after PI computation and before soft constraints, while keeping the raw PI diagnostic and integral behavior unchanged.

## [0.2.0] - 2026-04-26

- Valve power curve linearisation
  Inspired from @caiusseverus blueprint
  improve valve non linear power curve. more info: https://github.com/jmcollin78/versatile_thermostat/discussions/1704

## [0.1.10] - 2026-04-24

- Sync SmartPI committed power after valve mid-cycle updates
- Fix SmartPI default diagnostics registration
- Quicker boostrap phase:
  Adjust SmartPI bootstrap thresholds and AB publication
  (now needs only 8 B & 6 A before quitting bootstrap mode.)

## [0.1.9] - 2026-04-22

- remove vtherm_api dependency ( use VT installed one )

## [0.1.8] - 2026-04-21

- Align prop handler state change hook with changed flag API

## [0.1.7] - 2026-04-20

- switch to official vtherm_api package

## [0.1.6] - 2026-04-20

- Fix diagnostics sensor creation for default-bound thermostats
- Expose SmartPI phase as diagnostics sensor state
- Mark SmartPI diagnostics sensor as diagnostic entity

## [0.1.5] - 2026-04-19

- Remove default config entry in menu

## [0.1.4] - 2026-04-19

- Fix GuardState runtime typing in SmartPI guards
- Auto-create SmartPI default config entry on first setup

## [0.1.3] - 2026-04-18

- harden plugin services and refresh state after reset

## [0.1.2] - 2026-04-18

- replace vtherm api to main repo

## [0.1.0] - 2026-04-15

- Initial release of Versatile Thermostat SmartPI integration
- SmartPI proportional algorithm implementation
- HACS integration support
- Comprehensive documentation in French and English
- GitHub Actions for automated validation and releases
