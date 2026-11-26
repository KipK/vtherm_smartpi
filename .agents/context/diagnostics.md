# SmartPI diagnostics: root-cause guide for an agent

Audience: an AI agent (user-launched or developer-launched) that must explain a
SmartPI problem from the published diagnostics, history exports and logs.
Rule: quote exact field paths and values as evidence; write "unverified" rather
than guess; never conclude from one field alone (see the checklist in section 5).

Sources of truth: `smartpi/diagnostics.py::build_published_diagnostics` (one
`_build_<section>` per block), `smartpi/diagnostic_history.py`, `sensor.py`,
`smartpi/room_model/public.py`, user docs
`documentation/en/diagnostics.md` and `documentation/en/technical_doc.md`.

## 1. Obtaining the data

### 1.1 The diagnostic sensor

- One sensor per SmartPI thermostat, named `SmartPI Diagnostics`, attached to
  the thermostat device: entity id `sensor.<thermostat_slug>_smartpi_diagnostics`
  (slug depends on the user's naming; unverified for renamed devices).
  The climate entity also carries `specific_states.regulation_diagnostics`
  (the exact diagnostic entity id) and `specific_states.smartpi_learning_enabled`.
- State (`sensor.py::_get_diagnostic_state`): `bootstrap_hysteresis` (first model not
  published, bootstrap start), `learning_pulses` (same stage, direct start), `calibration`, `stable`; `inactive` (attributes empty) when the thermostat no
  longer runs SmartPI; `unknown` before the first publication.
- Attributes (`diagnostic_history.py::build_diagnostic_attributes`):
  `schema_version` (2), `live` (full tree, section 2), `history` (stable series).
- `live` is excluded from the Recorder unless the debug option `smart_pi_debug` of the entry that creates the sensor (options override data; a dedicated
  entry carries its own flag, otherwise the global entry applies) is on (`SmartPIRecordedDiagnosticSensor`). Debug
  changes retention only; the tree is identical. Consequence: past states from
  the history API carry only `schema_version` and `history` in normal mode;
  `live` exists only on the current state. Ask the user to enable debug mode
  and reproduce when past `live` values are needed. The Recorder keeps only its
  configured retention (default 10 days, unverified for the user's setup).
- The sensor only writes when state or attributes change; the 60 s timer does not
  publish if inputs and committed power are unchanged. A flat attribute history
  is not proof that SmartPI stopped.

### 1.2 The `history` block (17 series, `history.<group>.<field>`)

`control.kp`, `control.ki`, `temperature.indoor`, `temperature.anticipated`, `temperature.integral_error`,
`power.applied_percent`, `power.command_percent`,
`power.pi_percent`, `power.ff_percent`, `model.a`, `model.b`,
`model.effective_dead_time_s`, `feedforward.source`, `room_model.a`,
`room_model.b_eq`, `room_model.forecast_error_1h_c`,
`room_model.forecast_error_12h_c`.
The feed-forward, room model and dead-time series are always present, null when
not computed.
No outdoor temperature, regime or integral mode is in `history`: those are
live-only, so a past incident needs debug mode or logs.

### 1.3 Services (domain `vtherm_smartpi`, target: a VT climate entity)

`set_smartpi_learning` (`learning_enabled`), `reset_smartpi_learning` (back to
bootstrap, clears learning), `force_smartpi_calibration` (measurement pulses, may correct the model; never raises: refused in the learning
stage, with learning paused, with the thermostat off or within 24 h of the last end, ignored while one runs; answer in
`calibration.last_request`),
`reset_smartpi_integral`. Ask before suggesting a reset: it destroys the evidence.
Events on the bus: `vtherm_smartpi_event` with `type` (`learning_reset`,
`learning_enabled_changed`, `force_calibration`, `integral_reset`) and `entity_id`.

### 1.4 Logs

- Loggers are module paths: `custom_components.vtherm_smartpi` (handler, algo,
  sensor, state_store, valve_config_check) and children
  `custom_components.vtherm_smartpi.smartpi.<module>` (ab_estimator,
  learning_window, deadtime_estimator, gains, governance,
  calibration, learning_start, setpoint, room_model.manager,
  room_history.manager, compatibility...). `__init__`, `hvac_mode` and
  `compatibility` use `vtherm_api.log_collector.get_vtherm_logger`.
- Enable: `logger: logs: custom_components.vtherm_smartpi: debug` in
  configuration.yaml, or the `logger.set_level` service at runtime. Add
  `custom_components.versatile_thermostat: debug` for VT (that logger path is by
  Home Assistant convention; unverified in the VT repo). VT also keeps an
  in-memory ring buffer of its logger hierarchy (4 h default) that its log
  export tooling can filter by thermostat name (exact service name unverified).
- Messages are prefixed `<thermostat name> - `: filter on it. Useful INFO/WARNING
  lines: `Restart bridged|not bridged: <reason>`, `Reboot: state restored,
  learning frozen`, `Prolonged pause ... purging PI state`, `Target change
  detected ... invalidating learning window`, `HVAC mode changed ... PI state
  reset`, `Heat|Cool Deadtime detected = Ns`, `Calibration started|ended: <result>`,
  `Calibration request refused: <reason>|ignored: in progress`, `Calibration ended: <result> (corrected: ...)`, `Missing target or current temp, force 0`,
  `Failed to load|save SmartPI state|room history data|room model data`,
  `Ignoring unsupported SmartPI persisted state format`, `Versatile Thermostat
  does not report applied commands`, `Invalid SmartPI valve curve parameters`,
  `SmartPI requires Versatile Thermostat >= X`.

### 1.5 What to ask the user to paste (and what not)

Paste: thermostat type (switch or valve, over_switch/over_climate/...), cycle
length, min on/off delays, sensor model and resolution (0.01 or 0.1 degC),
heater type, a one-line symptom with times; the current `live.control`,
`live.power`, `live.temperature`, `live.model`, `live.learning` (without
`window_outcomes_24h` if long), `live.governance`, `live.setpoint`; the
`history` series over the incident window (CSV or a few hundred points).
Do NOT paste whole: `live.analysis` (hundreds of fields: select subtrees),
`live.room_model` (large; select
`status`, reasons), the full
attribute dump, or raw debug logs (filter by thermostat name and time).

### 1.6 Reading it through Home Assistant (generic)

- REST (long-lived token): `GET /api/states/<entity_id>` returns the current
  state with `attributes.live`; `POST /api/template` with
  `{{ state_attr('<entity_id>','live')['model'] | tojson }}` returns one subtree
  (preferred: small payload). History:
  `GET /api/history/period/<iso_start>?filter_entity_id=<id>&end_time=<iso_end>`
  (do not use `minimal_response`, it drops attributes).
- WebSocket: `history/history_during_period` (same filters), `get_states`,
  `render_template` for subtrees; `system_log/list` for warnings/errors.
- Any MCP/CLI wrapper for HA that exposes states, templates, history and logs
  is enough. Never dump the full attributes of the sensor.

## 2. Map of `live`

Conventions: `*_percent` fields are percent (0-100, 1 decimal); raw `u_*` fields
under `analysis.feedforward` are ratios (0-1); times are `_s`
seconds unless `_min` / `_h`; temperatures degC; `a` is degC/min at full power,
`b` is 1/min, `tau_min = 1/b` in minutes. `null` means "not computed/applicable",
never "zero". `tau_min` is the sentinel `9999.0` when tau is unreliable.

| Block | Answers | Key fields (all `live.<block>.`) |
|---|---|---|
| `control` | Which regime, which gains, restarts | `phase` (Hysteresis/Stable/Calibration), `mode`, `hysteresis_state`, `kp`, `ki`, `ti_min` (Kp/Ki min, null if Ki<=0), `saturation_state` (SAT_HI/SAT_LO/NO_SAT/init), `in_deadband`, `in_near_band`, `in_deadtime_window`, `restart_reason`, `last_restart.{bridged,downtime_s,reason}` |
| `power` | What each stage commands (percent) | `pi_percent`, `ff_percent`, `command_percent` (PI+FF clamped), `limited_percent` (after rate limit and max_on_percent), `applied_percent` (after min on/off, valve curve), `current_cycle_percent` vs `next_cycle_percent`, `linear_*` (before valve curve), `hold_percent` (deadband hold), `valve_linearization_enabled` |
| `temperature` | Inputs and integral | `indoor`, `anticipated` (indoor plus `analysis.control.anticipation_c`: what the P and I errors read), `outdoor`, `error` (setpoint minus indoor, >0 = too cold), `integral_error` (degC*min; `ki*integral_error` is its share 0-1), `integral_mode`, `integral_hold_mode`, `integral_hold_source` |
| `model` | Learned room model | `a`, `b`, `tau_min`, `tau_reliable`, `confidence` (ab_bootstrap, or ab_learning with the direct start before the first model/ab_ok/ab_degraded/ab_bad), `deadtime_heat_s` + `deadtime_heat_reliable` (HEAT) or `deadtime_cool_s` + `deadtime_cool_reliable` (COOL): the dead time after a start only, `a_stability_ratio`, `b_stability_ratio` (MAD/median) |
| `learning` | Why learning does/doesn't progress | `stage` (bootstrap, or measurement pulses with the direct start before the first model/learning/monitoring/degraded), `bootstrap_progress_percent`, `bootstrap_status`, `accepted_updates_a/b` (target `history_target` 31), `last_reason`, `a_drift_state`/`b_drift_state` (NORMAL/DRIFT_SUSPECTED/RECENTERING), `learn_b_converged`, `enabled` |
| `governance` | May a/b and gains adapt now | `regime`, `thermal_update_decision`, `thermal_update_reason` |
| `feedforward` | Holding power | `source` (always `learned model`), `deadband_power_source`, `deadband_p_mode` |
| `setpoint` | Setpoint boost | `boost_active` |
| `calibration` | The calibration on request (pulses) | `state`, `pulses_done` (max 3), `result`, `last_request`, `last_time`, `deadtime_start_s`, `reading_a`, `reading_b`, `reading_lag_s`, `in_force_a`, `in_force_b`, `in_force_lag_s` (horizon in use, shown only), `corrected` (`heating rate`, `loss coefficient`; the lag is corrected by the response model's own rule) |
| `analysis.control` | Error terms, anticipation, band geometry, timing | `cycle_min`, `error_p`, `anticipation_c` (degC added to the measured temperature for P and I, 0 when off), `anticipation_horizon_s`, `anticipation_horizon_source` (`response model`, `measurement pulses`, `dead times`, `none`), `forced_by_timing`, `near_band_below_deg`, `near_band_above_deg`, `near_band_source`, `aw_du`, `in_core_deadband`, `temperature_slope_h` |
| `analysis.learning` | Window outcomes | `window_outcomes_24h.{a,b}` (top 8 `[reason, count]`), `balance_pool.{segments_a,segments_b,resolution_c}`, `response_model.{events,pending_outliers,last,last_reason,peak_samples,prediction_rms_c}` (response model; `pending_outliers` = events kept aside for being off the published S by more than a factor 2, waiting for a confirming one; read by the a/b learning, the anticipation horizon, the HEAT near band, the calibration limits and the direct-start pulses; `last.daytime` marks a fit in daylight, fed only as the mode's first event; `last.prediction_rms_c` = rms of the model in force on a fed event's window, top-level = median of the last 7), `learn_ok_count`/`learn_skip_count` (session only). Related: `model.{dead_time_s,response_time_constant_s,response_source,response_confidence}`, `analysis.control.effective_dead_time_s` |
| `analysis.feedforward` | Holding power | `reason`, `u_ff1`, `u_ff_final`, `u_ff_effective` (the last two equal `u_ff1`) |
| `analysis.deadtime`, `analysis.governance` | Dead-time detector, gain freeze | `state`, `heat_last_outcome` (HEAT) or `cool_last_outcome` (COOL); `last_decision_gains`, `last_freeze_reason_gains`, `kp_source` |
| `valve` | VT valve settings (null outside valve mode) | `vt_dpercent`, `vt_period_min`, `opening_remap_active`, `issues`, `quantization_step_linear_percent` |
| `room_model` | Room model with walls | `status`, `tracking`, `walls`, `a`, `b_eq`, `last_refusal_reason`, `energy_balance_*`, `sensor_resolution_c`, `forecast_error_c`, `quiet_periods.{reason,count,last}`, `data_invalid_reason`, `history_h`, `last_error` |

`room_model` is omitted from the tree when the algorithm has no such manager (check
presence, not just null). Valve mode: room model `not identified: valve heaters are not
supported yet`.

## 3. Symptom -> where to look -> likely causes

| # | Symptom | Look at (paths) | Likely causes (module) |
|---|---|---|---|
| 1 | Never leaves bootstrap | `learning.bootstrap_status`, `bootstrap_progress_percent`, `emea_samples_a/b` (target 5), `model.deadtime_*_reliable`, `analysis.deadtime.{heat,cool}_last_outcome`, `analysis.learning.response_model.{events,last,last_reason}`, `window_outcomes_24h` | `step1 - waiting response model`: no window before the mode's model (every fit rejected: `response_model.last.outcome`, e.g. too few temperature changes with a coarse sensor; `smartpi/response_model.py`). `step2 collecting`: b needs cool windows with outdoor gradient (`skip: b heat mode - ...` in `window_outcomes_24h.b`; mild weather starves b), a needs heating windows; `skip: response transient (a|b)` = settling waits (`smartpi/power_steps.py`). Exit rule: `algo.py::phase` |
| 2 | Overshoot after setpoint rise | `temperature.{error,integral_error,integral_mode}`, `analysis.control.{error_p,anticipation_c,anticipation_horizon_s,anticipation_horizon_source}`, `model.{dead_time_s,response_time_constant_s,response_confidence}`, `power.pi_percent` vs `ff_percent`, `control.kp/ki`, `model.a` | integral wound during the rise (`integral_mode` I:RUN while far below setpoint, large `ki*integral_error`; check `deadtime`/`I:HOLD` source); anticipation silent (`anticipation_horizon_source` `none`, `anticipation_horizon_s` 0, or `phase` not Stable) or too short a horizon for a slow emitter (compare with the response model's emitter lag; source `dead times` means no model); `a` too high -> Kp too high / FF too strong; dead time under-estimated (`model.deadtime_*` vs the response model); still in bootstrap (hysteresis overshoot of +0.5 degC is by design); emitter/room inertia (`room_model.forecast_c`). Guard: `I:OVERRIDE(guard_cut)` means the cut protected the room |
| 3 | Stays below setpoint / undershoot | `temperature.error`, `power.*` chain, `control.saturation_state`, `temperature.integral_mode`, `analysis.control.in_core_deadband`, `deadband_power_source` | `limited_percent` < `command_percent` (VT `max_on_percent` or rate limit, `algo.py`); `I:SKIP(SAT_HI)` (PI saturated, emitter too small); sticky deadband (`in_deadband` with offset > deadband, P frozen: `deadband_p_mode`); `I:HOLD(...)`; FF source mismatch (`feedforward.source`); `b` too high (loss overestimated gives strong FF, underestimated gives weak); `applied_percent` << `limited_percent` (min on/off, valve retention: `analysis.control.forced_by_timing`, `valve.issues`) |
| 4 | Oscillation / limit cycle | `control.ti_min`, `kp`, `ki`, `analysis.governance.kp_source`, `control.in_near_band`, `analysis.control.near_band_*`, `integral_mode` sequence, `history.power.*` | gains too high vs actual dead time (`model.deadtime_*`, `analysis.control.effective_dead_time_s`); `kp_source` `safe` (model incomplete: Kp 0.55, Ki 0.010 fixed, `smartpi/gains.py`); cycle too long for the emitter (`analysis.control.cycle_min`); guard_cut/guard_kick toggling (`I:OVERRIDE(guard_cut)`; `smartpi/guards.py`); 0.1 degC sensor quantisation (see 17); hysteresis phase swings of ~0.8 degC are expected |
| 5 | No learning, a/b not published | `learning.{enabled,block_reason,stage,last_reason,accepted_updates_a/b}`, `governance.*`, `analysis.learning.window_outcomes_24h`, `learn_skip_count` | `learning.block_reason` set (`paused` service, persisted; `disturbance` or `heating unavailable` entity on); regime DEAD_BAND/HOLD/PERTURBED/DEGRADED freezes thermal learning (`const.py::GOVERNANCE_MATRIX`); no outdoor temperature (`DEGRADED`, `skip: no external temp`); reasons in 6; `learning_resume` cool-down after window close/restart (`skip: resume cool-down`) |
| 6 | Learning silently rejected | `learning.last_reason` (latest only), `window_outcomes_24h.a/.b` (24 h top-8 counts, numbers normalised to `#`), `analysis.learning.balance_pool` | reasons come from `smartpi/learning_window.py` and `ab_estimator.py`: `skip: governance (<reason>)`, `skip: setpoint transient`, `skip: deadtime window`, `skip: response transient`, `skip: power instability (CV=..)`, `skip: observation gap`, `skip: delta too small`, `skip: low excitation (u mid)`, `skip: window timeout`, `skip: b heat mode - insufficient heat-loss gradient`, `skip: b heat mode - outdoor too close to target`, `skip: a_meas outlier`, `skip: b_meas outlier`, `skip: a_meas wrong hvac sign`. Dominant reason = root cause; `pool:` prefix = balance-pool events |
| 7 | Weird or aggressive gains | `control.{kp,ki,ti_min}`, `analysis.governance.{kp_source,last_decision_gains}`, `model.{a,tau_min,tau_reliable}`, `analysis.control.{gain_delay_s,gain_delay_source,gain_closed_loop_time_s,effective_dead_time_s}` | compare with floors/ceilings `KP 0.10-5.0`, `KI 0.001-0.050` (`const.py`): Ki on the floor means tau/dead time made Ti huge or taper active (`*_nearband` suffix); `safe` source = fallback; `frozen` = governance freeze; `*_softfreeze` = HOLD regime. Kp = 1/(abs(a)*(lambda+L)): wrong `a` (check 8) or `L` (dead time) gives wrong Kp; valve mode uses factor 4 (`imc_simc_valve`) |
| 8 | Implausible a / b / tau | `model.*`, `learning.start.kept_a`, `calibration.reading_a`, `room_model.{a,b_eq}`, `a_stability_ratio`, `b_stability_ratio` | bounds `a` 1e-5..0.5, `b` 1e-5..0.05 (`ab_estimator.py`); tau reliable only with >=5 b updates, MAD/median <= 0.6; sun/occupants bias `b` low (long tau) or `a`; compare `model.a` with `calibration.reading_a` and `room_model.a`: disagreement > ~2x points to a bad window set |
| 9 | Valve never opens / flaps | `valve.*`, `power.{linear_next_cycle_percent,next_cycle_percent,applied_percent}`, `power.valve_linearization_enabled`, `valve.quantization_step_linear_percent` | `valve.issues`: `dpercent_filter` (VT holds small changes), `period_filter` (VT delays), `opening_remap` (VT min/max opening conflicts with curve linearization), also in HA Repairs (`valve_config_check.py`); wrong curve (`Invalid SmartPI valve curve parameters` log); min opening above demand; flapping: step of `quantization_step_linear_percent` large vs P band, or near-band/guard toggling; room model unsupported in valve mode (not a fault) |
| 10 | Does nothing after HA restart | `control.last_restart`, `control.restart_reason`, `temperature.integral_mode`, `power.*`, logs `Restart not bridged`, `Missing target or current temp` | restart not bridged (reasons in docs table of `last_restart.reason`: stop too long, heater state not saved, ...) resets cycle but learning kept; `I:FREEZE(missing_sensor)` = indoor temp unknown, output 0 until it arrives (kept command only within 600 s); `I:FREEZE(hvac_off)`; load errors in log (`Failed to load SmartPI state`, unsupported format) -> bootstrap again; `Prolonged pause` purged PI after 96 h |
| 11 | Command stuck at 0 % | `temperature.integral_mode`, `control.saturation_state`, `power.*`, `analysis.control.forced_by_timing` | `I:OVERRIDE(guard_cut)` (temp above setpoint+near_band_above), `I:FREEZE(hvac_off)`, `I:RESET(...)` (power shedding), window open (VT), `SAT_LO` with FF source giving ~0 (mild weather, outdoor warm), command < min on-time forced to 0 by timing, VT safety (`integral_hold_source vt_safety`) |
| 12 | Command stuck at 100 % | `power.*`, `SAT_HI`, `temperature.error`, `model.a` | undersized emitter or large step (normal); in bootstrap heating runs full until setpoint+0.5; `a` underestimated; setpoint boost (`setpoint.boost_active`); `forced_by_timing` with min off delay |
| 13 | Room model unpublished | `room_model.{status,last_refusal_reason,walls,tracking,energy_balance_refusal_reason,data_invalid_reason,quiet_periods.reason,last_error}` | reasons are plain strings from `room_model/public.py`: needs many nights (quiet periods, home position known, no sun), 0.01 degC sensor for walls (`not identified: temperature sensor too coarse`), `dependencies_unavailable` (numpy/scipy missing), valve mode unsupported. Mostly "wait" states: `learning`, `waiting for a second series...`, `not enough recorded days yet` |
| 14 | Version incompatibility | HA persistent notification `vtherm_smartpi_vtherm_version`, log `SmartPI requires Versatile Thermostat >= ...` | VT older than 10.6.2 or version unreadable: setup refused, no diagnostic sensor (`smartpi/compatibility.py`). Different case: VT lacks command receipts -> warning, SmartPI takes the applied power from the cycle start notifications |
| 15 | Dead time wrong / jumping | `model.deadtime_*`, `analysis.deadtime.*`, `analysis.control.effective_dead_time_s`, `history.model.effective_dead_time_s` | detector outcomes `rejected`/`ignored` (room warming before start), cycle or emitter latency; gains read the dead time after a start, holds read the response model |
| 16 | 0.1 degC / slow sensor behaviour | `room_model.sensor_resolution_c`, `analysis.learning.balance_pool.resolution_c`, `room_model.walls`, `response_model.last_reason`, `analysis.control.near_band_source` | quantisation steps trigger `I:UNWIND`, `I:CLAMP(near_ovr)`, guard-cut reset margin (1.5 sensor steps); few level changes starve slope windows (`skip: delta too small`, `baseline samples insufficient`); no a/b window in the learning stage while every response-model fit is rejected; walls and room-model walls refused; expect best-effort learning and wider noise on `a`, `b`. Target sensor is 0.01 degC on change + 5 min heartbeat; coarse sensors are best effort |
| 17 | Setpoint change ignored / slow | `setpoint.boost_active`, `temperature.{error,integral_error,integral_mode}`, `analysis.control.{error_p,anticipation_c,anticipation_horizon_s}`, `power.{pi_percent,command_percent,limited_percent}` | the regulation works on the raw setpoint: `error` is on the measured temperature, `error_p` on the anticipated one (they differ by `anticipation_c` while the room moves fast toward the setpoint; the correction fades near the setpoint); rate limit lifted only while `boost_active` (step >= 0.3 degC); a saturated command (`SAT_HI`) or an `I:HOLD` source explains a slow rise; `Target change detected` log invalidates the learning window |
| 18 | Calibration never starts / ends early | `calibration.{state,pulses_done,result}`, `learning.enabled`, `control.phase`, `temperature.error`, `power.command_percent` | `calibration.last_request` gives the refusal (learning stage, learning paused, thermostat off, too soon: < 24 h after the last end); `waiting` while the room is not between 0.2 degC over and 0.3 degC under the setpoint or the command is >= 80 %, gap clamp(2 x horizon, 1-6 h) between pulses; `time limit` after 48 h; `cancelled` on a HEAT/COOL change, a learning reset or a pause; a stop ends the pulse and the calibration continues. `learning.stage degraded` (tau unreliable after 31 samples) is unrelated |

## 4. Glossary of published strings

Integral modes (`temperature.integral_mode`, `smartpi/controller.py`, `algo.py`):

| Value | Meaning |
|---|---|
| `I:RUN` | integral integrates normally |
| `I:SKIP(SAT_HI\|SAT_LO)` | output saturated against the error: integration blocked |
| `I:UNWIND(SAT_*)` | saturated but within 1.5 sensor steps of setpoint: integral may shrink toward 0 |
| `I:CLAMP(near_ovr)` | error <= 0.04 degC: integral cannot grow (overshoot clamp) |
| `I:HOLD` / `I:HOLD(<mode>)` | frozen; source in `integral_hold_source`: `deadtime` (effective dead time after an episode start), `deadband_hysteresis_shell`, `governance_sensor_invalid` (no outdoor temp yet), `vt_safety` (VT safety default power), `calibration_recovery` (after a calibration pulse until the setpoint), `heating_unavailable` (entity on), `window_resume`, `power_shedding_resume`, `heating_unavailable_resume` (dead time after the entity turns off) |
| `I:FREEZE(deadband)` | core deadband: PI frozen, hold power = FF + PI |
| `I:FREEZE(missing_sensor)` / `I:FREEZE(hvac_off)` | no indoor temp (output 0 or kept restart command) / HVAC off |
| `I:RESET(<reason>)` | power shedding forced off |
| `I:OVERRIDE(guard_cut)` | overshoot guard forces 0 % until temp falls below threshold; `control.mode` is then `heating cut above the setpoint`, `temperature.error` / `analysis.control.error_p` follow the room and `anticipation_c` is 0 |

Other enums:

| Field | Values and meaning |
|---|---|
| `control.phase` | `Hysteresis` (bootstrap ON/OFF: on at setpoint-0.3, off at +0.5), `Learning` (direct start before the first model: PI with measurement pulses), `Stable` (PI), `Calibration` (a pulse of a calibration commands the output) |
| `learning.bootstrap_status` | `step1 - waiting response model`; `step2 - collecting emeas: A:n/5 B:n/5` |
| `learning.stage` | `bootstrap`, `measurement pulses` (direct start before the first model), `learning` (<31 a or b samples), `monitoring` (full + tau reliable), `degraded` (full, tau unreliable) |
| `learning.start.mode` | `bootstrap`, `direct` (the start the learning in progress began with; not the option when the thermostat had already learned) |
| `learning.start.pulse` | `none`, `rest`, `full power`, `stop` (the measurement pulse in progress); `pulses_done` counts the pulses of the current mode (3 at most) |
| `learning.start.{values_kept,values_kept_at,kept_a,kept_b,kept_lag_s}` | values kept from two agreeing readings for the current mode (`a` C/min, `b` 1/min, emitter lag s); `null` when none; ISO local time for `values_kept_at` |
| `governance.regime` | `warmup`, `excited_stable`, `near_band`, `dead_band`, `hold`, `perturbed`, `degraded` (no outdoor temp), `saturated` (command <=0.1 % or >=99.9 %) |
| `thermal_update_decision` | `adapt_on`, `freeze`, `hard_freeze` (dead_band, hold, perturbed, degraded), `soft_freeze_down` (gains only, in hold). Gains: warmup and saturation freeze; thermal learning stays on in saturation |
| `*_reason` (freeze) | `none`, `regime_transition`, `sensor_invalid`, `dead_band`, `near_band`, `warmup`, `hold`, `perturbed`, `saturation`, `system_inefficient` |
| `analysis.governance.kp_source` | `safe`, `measurement_pulses` (direct start, before the first model), `imc_simc`, `imc_simc_valve`, suffix `_nearband`, `_softfreeze`, or `frozen` |
| `analysis.control.anticipation_horizon_source` | `response model`, `measurement pulses`, `dead time`, `none` |
| `feedforward.source` | `learned model` (the only value: the room model never enters the holding power; `room_model.used_by_regulation` is `false`) |
| `calibration.state` | `idle`, `waiting` (between pulses, PI regulates), `rest`, `full power`, `stop`; `calibration.result`: `in progress`, `readings agree`, `pulses done`, `time limit`, `cancelled`; `calibration.last_request`: `started`, `refused: learning stage|learning paused|thermostat off|too soon`, `ignored: in progress`; `calibration.corrected`: list of `heating rate`, `loss coefficient`, `emitter lag` |
| `analysis.deadtime.state` | `WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`; outcomes `detected`, `timeout`, `aborted`, `rejected`, `ignored` |
| `analysis.control.anticipation_horizon_source` | `response model` (emitter lag theta + tau_e of the mode), without a model `measurement pulses` (direct-start kept lag), `dead time` (active dead time after a start), `none` (no reliable horizon: no anticipation) |
| `analysis.control.near_band_source` | `auto_model_aware`, `no response model`, `fallback_no_ext`, `fallback_model`, `fallback_slope_low`, `reset`: fallback = configured thresholds, model not usable yet |

## 5. Triage and evidence

### 5.1 Procedure

1. Establish scope: which thermostat, time window, HEAT or COOL, switch or valve,
   whether it is a regression (what changed: version, setpoints, config, weather).
2. Read `schema_version` (expect 2), sensor state, `control.phase`. If
   `bootstrap_hysteresis`, go to rows 1, 2 and 6 first: swings are expected (`learning_pulses`: the PI regulates, only the pulses swing).
3. Read `model` + `learning` + `room_model.status`: is the model plausible and reliable?
4. Follow the command chain `power.pi_percent + ff_percent -> command_percent ->
   limited_percent -> applied_percent`: find the first stage where the value
   departs from the symptom's expectation (SmartPI decision, SmartPI limit, VT/actuator).
5. Read `temperature.integral_mode` and `governance.regime` at the incident time
   (live only; else logs or debug mode) and the `history` series for the trend.
6. If learning is the issue, read `window_outcomes_24h` and `learning.last_reason`
   and name the dominant rejection reason.
7. Check VT-side config and sensor behaviour (5.2) before blaming the algorithm.
8. Conclude with the row number, field values as evidence, one root cause, one
   confidence level, and what would falsify it. If two causes fit, ask for one
   more capture rather than pick one.

### 5.2 Evidence checklist (fill before concluding)

| Item | Where / plausibility |
|---|---|
| Model a, b, tau | `model.a` (degC/min at full power; bounds 1e-5..0.5), `model.b` (1/min), `tau_min` (field winter tau is hundreds of hours; 9999 = unreliable). Compare with `calibration.reading_a`, `room_model.a/b_eq`, `energy_balance_loss_per_c` |
| Dead times | `model.deadtime_heat_s/cool_s` + reliability, `analysis.control.{gain_delay_s,effective_dead_time_s}`; emitter type fits (electric: short, water radiator: long, floor: very long) |
| Gains vs floors | `control.kp/ki/ti_min` against 0.10/5.0 and 0.001/0.050; `kp_source`; Ti near `4*(lambda+L)` when learned |
| Command chain | percent values at each stage; `forced_by_timing`; `max_on_percent` effect; `valve.*` |
| Integral and regime | `integral_mode` + hold source, `governance.regime`, `saturation_state` |
| Learning health | `accepted_updates_a/b`, `last_reason`, outcomes top reasons, drift states |
| Sensor | resolution (`sensor_resolution_c`, `resolution_c`), update period, 0.01 degC vs 0.1 degC; outdoor sensor present (`temperature.outdoor` not null) |
| VT config | cycle length (`analysis.control.cycle_min`), min on/off delays (SmartPI options, switch only), switch vs valve, `valve.issues`, `max_on_percent`, VT safety/window/shedding, VT and vtherm_api versions (receipts support) |
| Restarts | `control.last_restart`, `restart_reason`, uptime vs incident time |
| Disturbances | sun, occupants, open windows: `window open` in `data_invalid_reason`; exclude disturbed windows rather than fit them |
| Data quality | which values came from `history` (always there) vs `live` (only current or debug); state "unverified" for anything not seen |

## Schema pin coverage

`tests/fixtures/published_diagnostics_paths.json` pins the `live` tree built by `tests/test_published_diagnostics_schema.py::_representative_smartpi`, which builds `room_model=True`: the `live.room_model.*` paths (history and quiet-period fields included) are pinned too. Renaming or removing a field is caught by the schema test; still check `cards/`, the docs and the Equinox card by hand.
