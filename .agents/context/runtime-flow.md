# Runtime flow: SmartPI inside Home Assistant and Versatile Thermostat

Audience: an AI agent with no prior context. References are `path::symbol`, relative to
`custom_components/vtherm_smartpi/`. Versatile Thermostat = "VT"; the shared plugin API package is
`vtherm_api`. SmartPI is a plugin: VT owns the thermostat entity, the cycle scheduler and the actuators;
SmartPI owns the control law, learning and persistence.

## 1. Setup and registration

- `__init__.py::async_setup` (config-entry only) runs `smartpi/compatibility.py::async_check_vtherm_version`,
  `_register_factory`, `_keep_factory_registered`, `_register_services`. A failed version check returns False.
- Version guard: `MIN_VTHERM_VERSION = "10.6.2"` against the installed `versatile_thermostat` version (HA loader).
  Too old or unverifiable: error log, persistent notification `vtherm_smartpi_vtherm_version`, setup refused, no
  factory registered. A compatible version dismisses the notification.
- `_register_factory`: `VThermAPI.get_vtherm_api(hass).register_prop_algorithm(SmartPIHandlerFactory())` unless
  `get_prop_algorithm("smartpi")` exists. `factory.py::SmartPIHandlerFactory` has `name == "smartpi"`
  (`const.py::PROP_FUNCTION_SMART_PI`); `create(thermostat)` returns `handler.py::SmartPIHandler`.
- `_keep_factory_registered`: on `SIGNAL_CONFIG_ENTRY_CHANGED`, registers again when a VT entry enters
  `SETUP_IN_PROGRESS` (VT rebuilds its API object, hence an empty registry, after its last entry unloaded).
- What selects SmartPI: the VT config entry value `proportional_function == "smartpi"` (`CONF_PROP_FUNCTION`; VT's
  config flow lists registered names). VT reads it in `versatile_thermostat/thermostat_prop.py::post_init` and
  builds the handler in `_init_algorithm_handler` (`get_prop_algorithm(name).create(thermostat)`, then
  `init_algorithm()`). If SmartPI is not registered yet, VT does not raise and retries in `async_startup`, where
  it also calls `on_scheduler_ready` and `async_added_to_hass` to catch up.
- `PLATFORMS = [Platform.SENSOR]` (diagnostic sensor only). Services: section 7.
- `async_setup_entry` forwards the platform and reloads affected VT entries only when `hass.state` is running
  (at startup VT's deferred init covers it; reloading earlier would recreate entities before state restore).

## 2. Config resolution and reload policy

- `handler.py::SmartPIHandler._resolve_effective_config`, lowest to highest priority:
  1. `const.py::DEFAULT_OPTIONS`.
  2. `thermostat.entry_infos` (VT entry data merged with VT central config; a SmartPI key there overrides defaults).
  3. One plugin entry (enabled ones only): the dedicated entry whose `data[CONF_TARGET_VTHERM]` equals the
     thermostat `unique_id`, else the global entry (`unique_id == "vtherm_smartpi"`). No layering between them.
  4. Inside that entry: `entry.data` first, then `entry.options` (options win). `config_flow.py::SmartPIOptionsFlow`
     seeds from `options or data` and stores the full dict in options.
- Config is read once in `init_algorithm`; no live update, any change needs a VT reload.
- Reload policy (`__init__.py`): an options update reloads the plugin entry (`_async_update_options`); its
  `async_setup_entry` reloads affected VT entries (running HA only): `_reload_smartpi_vtherms_for_target` for a
  dedicated entry, `_reload_smartpi_vtherms_using_defaults` (SmartPI VT entries without a dedicated entry) for the
  global one. Disabling reloads after unload; removal reloads via `async_remove_entry`.
- Entries: global = `SmartPIConfigFlow.async_step_user` (unique id = domain); dedicated = `async_step_thermostat`
  (unique id `vtherm_smartpi-<target unique id>`).

## 3. Handler lifecycle

VT calls the `InterfacePropAlgorithmHandler` methods (contract in `vtherm_api/interfaces.py`) from
`versatile_thermostat/thermostat_prop.py`.

- `init_algorithm` (entity creation): resolves config; builds the stores from the slugified thermostat name
  (`state_store.py::SmartPIStateStore`, `SmartPITarStore`, `SmartPIRoomModelStore`); writes the min delays on the
  thermostat and scheduler; builds `algo.py::SmartPI` (`tar_learning=True`, `room_model=True`) as
  `thermostat.prop_algorithm`; configures the valve curve (active only if configured and the scheduler
  `is_valve_mode`; invalid parameters disable it); sets `restart_exclusion_probe`; sends
  `SIGNAL_SMARTPI_TARGET_UPDATED` (the sensor platform reacts, section 8).
- `async_added_to_hass`: loads algorithm state (`SmartPI.load_state`), emitter lag state (plus home position and
  `dependencies_importable`), room model state. Failures are logged and never block startup.
- `on_scheduler_ready(scheduler)`: re-syncs timing and valve curve, then
  - receipts supported (`hasattr(scheduler, "register_command_receipt_callback")`): registers
    `SmartPI.on_command_receipt`, `algo.receipts_supported = True`;
  - else legacy: warning, `_on_legacy_cycle_started` as cycle-start callback, no ownership (no bumpless
    transfer, no periodic FF trim), valve position read back from `thermostat.valve_open_percent`;
  - always registers `SmartPI.on_cycle_completed` as cycle-end callback.
- `async_startup`: `valve_config_check.apply_valve_config_check` (Repairs issues on VT valve filters, observation
  only), then `on_state_changed(True)`.
- `on_state_changed` (VT calls it after every `update_states`): in HEAT/COOL starts the 60 s timer
  (`SMARTPI_RECALC_INTERVAL_SEC`, calls `async_control_heating(timestamp=None)`); on resume from OFF calls
  `SmartPI.reset_cycle_state` (skipped at the first start after a bridged restart); other modes stop the timer and
  suspend a running calibration (`CalibrationManager.suspend`).
- `control_heating(timestamp, force)`: one tick, section 4. `should_publish_intermediate()` tells VT whether to
  write climate state (true for a periodic `timestamp`, `force`, a changed input signature or committed output).
- Command receipts and ownership (`smartpi/command_ownership.py`):
  - If VT `on_percent` differs from `algo.on_percent` (VT override: safety default, `max_on_percent` clamp) the
    request is untagged and `reset_command_ownership("runtime_power_override")` runs. Otherwise, with receipts,
    `request_id = algo.stage_command(hvac_mode)` goes to `cycle_scheduler.start_cycle(..., request_id=)`.
  - `CommandOwnershipTracker.stage` freezes the control decomposition (`CommandOwnershipSnapshot`) under a strictly
    increasing id (starts at `time.monotonic_ns()`, last 32 kept). VT echoes the id in `AppliedCommandReceipt`
    (`kind` CYCLE_START or UPDATE, `alterations`). `bind_receipt` owns a receipt only for a known id, same HVAC mode,
    no `MIN_ON_OFF`/`VALVE_RETAINED` alteration, no "timing" flag; a rejection is sticky per request.
  - `SmartPI._commit_applied_command` commits the applied power (valve curve inverted to linear) and feeds the
    causal power trace, emitter lag learning, room model; on a cycle start it resets per-cycle governance. Legacy path
    (`on_cycle_started`, `on_applied_power_updated`) commits the same way with no snapshot.
- VT overrides: safety (`safety_manager.is_safety_detected`) sets `algo.vt_safety_override` (integral held, source
  `vt_safety`); power shedding (`is_overpowering_detected`) forces output off; window and OFF go through
  `_validate_and_handle_off`. `_control_exclusion` names why the applied command is not SmartPI's (window,
  shedding, safety, unknown, overridden); it feeds the restart bridge and learning.
- `remove` (entity removal/reload): section 6.

## 4. One control tick

Triggers of `SmartPIHandler.control_heating`: VT periodic tick (switch thermostats: every `cycle_min`, `timestamp`
set), SmartPI's 60 s timer (`timestamp=None`), VT state changes (temperature, setpoint, preset, HVAC mode; `force`
per VT). VT first checks `is_ready`, window auto and safety refresh.

Handler order (`handler.py::control_heating`):
1. Publish signature; if `force` and a cycle runs: `cycle_scheduler.cancel_cycle()` (fires the cycle-end callback)
   and `force = False`.
2. `guards.check_guard_cut` / `check_guard_kick` may set `force`.
3. `cycle_boundary = force or no scheduler or not cycle running`; `algo.vt_safety_override`;
   `algo.resolve_restart(...)` (once per session).
4. `prop_algorithm.calculate(...)`, then `note_control_exclusion`, `observe_tar_learning`, `observe_room_model`
   (background jobs via `_process_tar_learning` / `_process_room_model`).
5. With `smart_pi_allow_pwm_cycle_force`: deadband / near-band transitions set `force`.
6. Actuation: HVAC OFF turns the device off or cancels the cycle; `on_percent is None` keeps the switch state;
   else `cycle_scheduler.start_cycle(hvac_mode, on_percent, force_cycle, [request_id])` with
   `force_cycle = force or phase CALIBRATION or (phase HYSTERESIS and on_percent changed)`.
7. Debounced save, AutoCalib completion/hourly check (events, may request a calibration), then, if publishing,
   `async_dispatcher_send(diag_update_signal(unique_id))`.

`algo.py::SmartPI.calculate(target_temp, current_temp, ext_current_temp, slope, hvac_mode, power_shedding,
cycle_boundary, off_reason, temperature_measure_id)` is the contract with VT:
- The first five parameters are positional for VT: `thermostat_prop.py::recalculate` and `thermostat_valve.py` call
  `calculate(target, cur, ext, slope, hvac_mode)` positionally (the first adds `power_shedding=`/`off_reason=`
  keywords). Never reorder or insert before them. The handler uses keywords. Returns None; results are read from
  properties (`on_percent`, `committed_on_percent`).
- Internal state is a linear demand in [0,1]; the valve curve maps it to the actuator command when VT reads
  `SmartPI.on_percent` (`_to_actuator_power`; identity without linearization).

Pipeline inside `calculate`, in execution order:
1. Reset per-tick markers; store slope and outdoor temperature (previous kept when None).
2. HEAT<->COOL change resets PI/FF state; `est.ensure_hvac_mode` migrates a mode-dependent model.
3. With target and indoor known: `resolve_restart` if pending, `restart.note_tick`; `_take_hysteresis_restore`.
4. `_validate_and_handle_off`: missing sensor (output 0, or held command while a restart is pending), HVAC OFF
   (`_force_off_output`), power shedding; each returns early.
5. Guard cut active: observe the room, unwind integral, `_set_linear_output(0)`, return.
6. `_refresh_active_timestamp_and_expire_controller_memory` (96 h purge), `tint_filter.update`,
   `_update_time_tracking` (dt capped to one cycle, 600 s after a bridged restart).
7. Anticipation: `correction_c` with the horizon of `SmartPI.anticipation_horizon` (STABLE phase only, else 0); then
   `_manage_setpoint` (setpoint change, boost, raw error `error_i` and `error_p` on the anticipated temperature),
   `error_i_ctl` = integral error on the anticipated temperature; setpoint-change handling.
8. Learning: `dt_est.update`, `update_learning` (learning on, not HYSTERESIS, not calibrating), calibration
   timeout/`check_and_start`. Before step 4, `CalibrationManager.follow_mode` runs on every tick (also while OFF, which
   returns at step 4): a stop suspends the calibration, the same mode back within `CALIBRATION_PAUSE_MAX_MIN` resumes it,
   a longer stop or another mode cancels it (`take_completed_result` then reports it to AutoCalib). CALIBRATION: `_calculate_forced_calibration`, return.
9. Phase HYSTERESIS (bootstrap): `ctl.calculate_hysteresis` bang-bang, committed at once, return.
10. `_update_control_context` (error filter, deadband, near-band), `resume_hold.update`.
11. Governance: `gov.determine_regime`, `decide_update` for gains and thermal.
12. `_apply_gains_and_ff` (gain scheduler, FF1/FF2/FF3 incl. `_select_holding_power`), then integral-hold decisions
    (deadtime, deadband shell, no outdoor value, `vt_safety`).
13. `ctl.compute_pwm` (PI + FF, anti-windup, `u_max` = `max_on_percent` in linear space), `track_output_limit`.
14. `_apply_soft_constraints`: rate limit (`MAX_STEP_PER_MINUTE`, boost rate), clamp to `[0, max_on]`.
15. `update_timing_constraints`: min activation delay zeroes a too-short switch ON; skipped in valve mode.
16. `_set_linear_output`, `ctl.finalize_cycle`, bookkeeping, FF trim freeze/measurement, `_update_twin_diagnostics`.
- After return, VT's `on_percent` applies the safety default and its own `max_on_percent` clamp on the actuator
  value. SmartPI has no `update_realized_power`; it learns the applied command from receipts.

## 5. Cycles and scheduling

- VT owns the master cycle (`versatile_thermostat/cycle_scheduler.py::CycleScheduler`): PWM timers for switches, a
  valve-position cycle for valves, length `cycle_min`. SmartPI never switches an underlying, except turning the
  device off in HVAC OFF.
- Hand-over: `start_cycle(hvac_mode, on_percent, force, request_id=)`, `on_percent` = actuator fraction (curve
  applied). `calculate_cycle_times` applies the min activation/deactivation delays, written to the scheduler by
  `_synchronize_scheduler_timing`.
- Running switch cycle without `force`: the request waits for the next automatic repeat (no receipt until applied);
  with `force` it restarts (CYCLE_START receipt, same id on automatic repeats). Running valve cycle: position
  updated at once (UPDATE receipt).
- Cycle end (`_on_master_cycle_end` restarts with `start_cycle(force=True)`, whose cancel fires the end callbacks)
  calls `SmartPI.on_cycle_completed(e_eff, elapsed_ratio)`: FF trim close, cycle counter,
  `_cycle_boundary_pending = True` (next `calculate` is a cycle boundary).
- Timing is applied twice: SmartPI zeroes a too-short ON, the scheduler flags `MIN_ON_OFF` (receipt not owned).
  Valve mode ignores min delays, but VT's valve resolver may retain a position (`VALVE_RETAINED`, not owned).

## 6. Persistence

- Stores (HA `Store`, version 1; keys in `state_store.py`, `{}` = slugified thermostat name):
  `vtherm_smartpi.{}` (algorithm, `SmartPI.save_state`, `"version": 2`); legacy `versatile_thermostat.smartpi.{}`
  (read only when the primary is empty, then copied to the primary); `vtherm_smartpi.{}.tar_learning` (state v3);
  `vtherm_smartpi.{}.room_model` (state v2).
- Persisted: learning flag, last active wall time, model a/b, dead time, governance, controller (integral,
  u_prev), setpoint manager, learning window, deadband, calibration, gain scheduler, twin, guards, AutoCalib, FF
  trim and holding source, tint filter, hysteresis output, and the restart context (last tick, mode, cycle config,
  exclusion, temperature, committed command, wall times).
- Transient, empty after load (`load_state`): committed power (0 unless bridged), command ownership/pending
  requests, FF trim observation runtime, integral hold, learning resume timestamp, cycle
  state. Without a bridge, the first tick freezes learning for one cycle (`_startup_grace_period`).
- Save throttling: `schedule_save` runs every tick but uses `Store.async_delay_save` with a 300 s window
  (`SAVE_DELAY_SEC`), not re-armed while pending; the snapshot is taken when the write runs. emitter lag learning and room model
  use 1800 s (`TAR_SAVE_DELAY_S`, `RM_SAVE_DELAY_S`) and only when `dirty`. `save_now` writes at once and
  supersedes the pending write (services, `remove()`). `remove()` schedules the three writes
  as tasks, stops learning tasks and the timer, removes the receipt callback and valve issues.
- Restart bridge (`smartpi/restart_bridge.py`, `SmartPI.resolve_restart`): decided once, at the first tick with an
  indoor temperature (until then the pre-stop command is held when still holdable). Bridged when downtime <=
  `RESTART_BRIDGE_MAX_GAP_S` (600 s, from the oldest last tick of all stores), same HEAT/COOL mode and cycle
  config, committed/heater state saved, no exclusion before or after, no temperature jump, no clock jump. Then the
  committed command is restored and learning window, dead-time and FF-trim work go on; the HEAT-only histories
  (emitter lag learning, room model) go on only when bridged in HEAT. Otherwise conservative handling. Published as
  `restart.public()`.
- Long OFF: after more than 5760 min (96 h, `PROLONGED_PAUSE_MEMORY_EXPIRATION_MIN`) without activity the
  controller and holding source are reset; the learned model stays. A resume from OFF/window/shedding pauses
  learning for `LEARNING_PAUSE_RESUME_MIN` (20 min).
- Migrations in code: legacy key copy; HVAC signed-model migration (`est.ensure_hvac_mode`); emitter lag store v1/v2 and room
  model v1 readers; an unknown later version is ignored; `load_state` ignores a payload without `est_state`.

## 7. Services and climate extra state

- Domain `vtherm_smartpi` (`__init__.py::_register_services`, `cv.make_entity_service_schema`, VT climate targets,
  `services.yaml`). Targets whose `proportional_function` is not `smartpi` are skipped; none matching raises
  `ServiceValidationError` (translation key `no_smartpi_target`). Each calls a handler method (entity first, then
  `_algo_handler`); all fire `vtherm_smartpi_event` with `entity_id` and a `type`:
  - `reset_smartpi_learning`: `SmartPI.reset_learning` (model, controller, FF, emitter lag learning, room model, auto-calibration supervision), type
    `learning_reset`, forced tick, attributes, state write, `save_now`.
  - `set_smartpi_learning` (`learning_enabled`: bool, required): `set_learning_enabled` (invalidates in-flight
    observations), type `learning_enabled_changed`, attributes, diagnostic refresh, `save_now`.
  - `force_smartpi_calibration`: `force_calibration`, type `force_calibration` (+ AutoCalib event if any), forced
    tick, `save_now`.
  - `reset_smartpi_integral`: integral to 0, integral hold cleared, type `integral_reset`, `save_now`.
- Climate extra state: `handler.py::update_attributes` (VT calls it in `update_custom_attributes`) writes
  `specific_states["smartpi_learning_enabled"]` (bool; True without a SmartPI algorithm) and
  `specific_states["regulation_diagnostics"]` (diagnostic sensor entity id from the entity registry).

## 8. Diagnostic sensor

- `sensor.py::SmartPIDiagnosticSensor`: one per SmartPI thermostat, unique id `smartpi_diag_<target unique id>`,
  diagnostic category, name "SmartPI Diagnostics", state from the phase (`bootstrap_hysteresis`, `calibration`,
  `stable`; `inactive` when the algorithm is missing).
- Targets: the global entry serves SmartPI VT thermostats without a dedicated entry
  (`_get_default_target_unique_ids`); a dedicated entry serves its target. Both listen to
  `SIGNAL_SMARTPI_TARGET_UPDATED` to add a sensor when a thermostat becomes SmartPI; a thermostat that stops using
  SmartPI loses its sensor (registry removal).
- Refresh: climate state changes and `diag_update_signal(unique_id)` (end of each publishing tick, after
  `set_smartpi_learning`). It reads `prop_algorithm` from the climate entity, builds
  `algo.get_published_diagnostics()` (`smartpi/diagnostics.py::build_published_diagnostics`) and writes HA state only
  if state or attributes changed.
- Attributes (`smartpi/diagnostic_history.py::build_diagnostic_attributes`): `schema_version` (2), `live` (full),
  `history` (compact, stable keys, null when not computed: kp/ki, indoor temperature, integral error,
  power applied/command/pi/ff, model a/b, FF source and wall power, room model a/b_eq and 1 h/12 h
  forecast error, applied dead time).
- Recorder profiles: normal = `SmartPIDiagnosticSensor`, `_unrecorded_attributes = {"live"}` (only
  `schema_version` and `history` recorded); debug = `SmartPIRecordedDiagnosticSensor` (`live` recorded too). Chosen at
  sensor creation from the plugin entry creating it (`CONF_SMART_PI_DEBUG`, options over data); the global entry flag
  applies to all default-bound thermostats.
- Device linking (`smartpi/device_link.py::cleanup_config_entry_devices`): the sensor uses the device of the VT
  climate entity (`async_entity_id_to_device`); at setup, a diagnostic entity of the entry on another device moves to
  the target device, and helper devices of the entry are removed (`helper_integration.async_remove_helper_devices`,
  or the older per-device removal), keeping target devices.

## 9. Dependencies and minimum versions

- `manifest.json`: `dependencies: ["versatile_thermostat"]`, `config_flow: true`, `integration_type: service`,
  `iot_class: calculated`, requirements `vtherm_api>=0.7.0`, `numpy` (unpinned), `scipy>=1.14`; version is a
  placeholder (`0.0.0`) set from the release tag by the release workflow.
- `hacs.json`: minimum Home Assistant `2025.8.0`. Minimum VT 10.6.2 (`compatibility.py::MIN_VTHERM_VERSION`). The
  receipt contract is optional at runtime (capability check in `on_scheduler_ready`). `pyproject.toml`:
  `requires-python >= 3.13`.
- numpy/scipy are imported lazily: only the learning executor jobs (`smartpi/tar_learning/jobs.py`,
  `smartpi/room_model/jobs.py`, imported inside functions) load them, so the event loop never imports scipy.
  `smartpi/tar_learning/deps.py::dependencies_importable` uses `find_spec`; without them emitter lag learning reports
  unavailable and the regulation keeps the bootstrap dead time.
