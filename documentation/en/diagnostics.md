# SmartPI diagnostics

SmartPI exposes a dedicated Home Assistant diagnostic sensor. Its state is the
current regulation phase and its attributes follow schema version 2.

While the first model is not published, the state, `live.control.phase`,
`live.learning.stage` and `live.model.confidence` name the start of the learning:

| Start | State | `control.phase` | `learning.stage` | `model.confidence` |
|---|---|---|---|---|
| Bootstrap | `bootstrap_hysteresis` | `Hysteresis` | `bootstrap` | `ab_bootstrap` |
| Direct start | `learning_pulses` | `Learning` | `measurement pulses` | `ab_learning` |

Afterwards both starts publish the same values (`stable` or `calibration`,
`Stable` or `Calibration`, `learning`, `monitoring` or `degraded`, `ab_ok`,
`ab_degraded` or `ab_bad`).

## Attribute envelope

```yaml
schema_version: 2
live:     # Complete live diagnostics
history:  # Stable time-series contract
```

`live` is the source for dashboards, Markdown cards and interactive analysis.
It has the same content in normal and debug mode. `history` contains only the
values intended for historical charts.

There is no separate `debug` attribute block.

## Recorder profiles

Normal mode excludes the complete `live` attribute from Home Assistant
Recorder. Only `schema_version`, the sensor state and `history` are retained.

Debug mode records both `live` and `history`. The content is not duplicated
inside `history`: the historical contract remains identical in both modes.
Debug mode therefore changes retention, not publication.

The `history` block contains these nineteen series:

| Path under `history` | Meaning |
|---|---|
| `temperature.indoor` | Indoor temperature |
| `temperature.anticipated` | Indoor temperature plus the anticipation correction: the temperature read by the proportional and integral errors |
| `temperature.integral_error` | Integral state (°C·min); `ki × integral_error` is its share of the command (0 to 1) |
| `control.kp` | Proportional gain |
| `control.ki` | Integral gain |
| `power.applied_percent` | Applied command after output constraints |
| `power.command_percent` | Requested command before actuator effects |
| `power.pi_percent` | PI contribution |
| `power.ff_percent` | Feed-forward contribution |
| `model.a` | Learned heating gain |
| `model.b` | Learned heat-loss coefficient |
| `model.effective_dead_time_s` | Effective dead time of the response model in seconds (`model.dead_time_s` plus half of `model.response_time_constant_s`); the integral holds, the near band above the setpoint and the calibration pulses read it |
| `feedforward.source` | Holding power source in force, as in `live.feedforward.source` |
| `feedforward.wall_power_percent` | Wall part of the feed-forward applied, in % of full power (negative when the walls give heat back); `null` when no wall part is applied |
| `room_model.a` | Heating response of the room model, in the units of `model.a`; `null` before a publication |
| `room_model.b_eq` | Static loss of the room model, in the units of `model.b`; `null` before a publication |
| `room_model.forecast_error_1h_c` | Root mean square error of the 1 h forecasts over the last 7 days (°C) |
| `room_model.forecast_error_12h_c` | Same for the 12 h forecasts (°C) |

The feed-forward, room model and effective dead time series are
always present and are `null` when the corresponding block is not computed. The applied command does
not measure delivered heat.

## Live diagnostics

The `live` block is organized by responsibility:

| Block | Content |
|---|---|
| `control` | Phase, regulation mode, gains (`kp`, `ki`), effective integral time `ti_min` (minutes), saturation, regulation bands and the last restart (`last_restart`) |
| `power` | Current and next-cycle power, PI/FF split, limits and applied power |
| `temperature` | Indoor/outdoor temperatures, anticipated temperature (`anticipated`: indoor temperature plus the anticipation correction), errors and integral state |
| `model` | A/B model, confidence, time constant, dead times and the response model |
| `learning` | Learning stage, bootstrap progress, accepted updates, drift state and the start of the learning (`start`) |
| `governance` | Thermal regime and model-update decision |
| `feedforward` | Holding power source, wall part of the holding power and dead-band source |
| `setpoint` | Setpoint boost state |
| `calibration` | The calibration in progress or the last one: state, pulses, result, last request, dead times measured, reading, values in use and corrections |
| `analysis` | Advanced fields used by the supplied diagnostic cards |
| `valve` | Versatile Thermostat valve settings seen by SmartPI, `null` outside valve mode |
| `tar_learning` | Emitter lag learning |
| `room_model` | Room model |

`control.mode` is `smartpi` while the PI regulates, `hysteresis` during the
bootstrap, and `heating cut above the setpoint` during a guard cut (the room went
past the setpoint by the near-band above it: the heating is forced off). The PI is
not computed during a cut: `temperature.error` and `analysis.control.error_p`
are then the setpoint minus the temperature of the last calculation,
`analysis.control.anticipation_c` is `0` and `temperature.anticipated` is the
indoor temperature.

`temperature.integral_hold_source` is `calibration_recovery` while the integral
is held after a calibration pulse, until the room reaches the setpoint. It is
`heating_unavailable` while the heating unavailable entity is on, then
`heating_unavailable_resume` while the integral stays held for the effective dead
time of the emitter after it turns off.

`learning.enabled` is the setting of the `set_smartpi_learning` service.
`learning.block_reason` is `null` while SmartPI learns, else why every learning is
paused: `heating unavailable` (heating unavailable entity on), `disturbance`
(disturbance entity on) or `paused` (service), in that order when several apply.

`control.last_restart` describes the last restart of Home Assistant or of the
integration. It is `null` before the first regulation tick after the restart,
and for a thermostat that starts without saved data.

| Field | Meaning |
|---|---|
| `bridged` | `true` when the restart was bridged: the measurements in progress resumed and nothing was excluded from the learning data. `false` when SmartPI started again carefully |
| `downtime_s` | Duration of the stop, in seconds; `null` when it cannot be measured (clock jump, no saved data) |
| `reason` | Why the restart was not bridged, `null` when it was: `stop too long`, `heater state not saved`, `not heating or cooling before the stop`, `not heating or cooling after the restart`, `HVAC mode changed across the stop`, `cycle settings changed`, `indoor temperature unknown` (also when the command went to 0 while the temperature was awaited), `temperature jump across the stop`, `clock jump`, `no saved state of the last tick`, or an exclusion before the stop or after the restart (for example `window open before the stop`, `safety mode after the restart`) |

`learning.start` summarises how the learning started and what the measurement
pulses of the direct start gave (see the **Start of the learning** option in the
[SmartPI documentation](vtherm_smartpi.md)). The values are those of the HVAC mode
the model is in; the bootstrap start shows `0` pulses and no pulse in progress.

| Field | Meaning |
|---|---|
| `mode` | Start of the learning in progress: `bootstrap` or `direct`. A thermostat that has already learned keeps the start its learning began with, whatever the option says |
| `pulses_done` | Measurement pulses started since the learning began, in the current mode (three at most); the pulses of a calibration are not counted |
| `pulse` | The pulse in progress: `none`, `rest` (no heating, waiting for the room to drop), `full power` or `stop` (waiting for the temperature peak); a pulse of a calibration shows `none` here |
| `values_kept` | `true` when two agreeing readings gave values for the current mode |
| `values_kept_at` | Local time at which the values were kept (ISO 8601), `null` when none are kept or the time is unknown |
| `kept_a` | Kept heating rate in °C/min at full power (5 decimals), `null` when none are kept |
| `kept_b` | Kept loss coefficient in 1/min (6 decimals), `null` when none are kept |
| `kept_lag_s` | Kept emitter lag in seconds, `null` when none are kept |

`calibration` summarises the calibration requested with the
`force_smartpi_calibration` service (see **Calibration** in the
[SmartPI documentation](vtherm_smartpi.md)): the calibration in progress, or the
last one until the next request.

| Field | Meaning |
|---|---|
| `state` | `idle` (no calibration in progress), `waiting` (between pulses, the regulation runs), or the step of its pulse in progress: `rest`, `full power`, `stop` |
| `pulses_done` | Pulses started by the calibration (three at most; an interrupted pulse counts) |
| `result` | `null` before any calibration, `in progress`, `readings agree` (two pulses agree), `pulses done` (three pulses read without agreement), `time limit` (48 hours) or `cancelled` (change between heating and cooling, return of the learning stage, learning reset, learning paused by the service or an entity) |
| `last_request` | Answer to the last request, `null` before any: `started`, `refused: learning stage`, `refused: learning paused` (by the service or an entity), `refused: thermostat off`, `refused: too soon` (less than 24 hours after the end of the last calibration), `ignored: in progress` |
| `last_time` | Local time at which the last calibration that was not cancelled ended (ISO 8601), `null` before |
| `deadtime_start_s` | Last dead time after a start measured during a pulse of the calibration, in seconds, `null` when none |
| `deadtime_stop_s` | Last dead time after a stop measured during a pulse of the calibration, in seconds, `null` when none |
| `reading_a` | Heating rate of the two agreeing pulses in °C/min at full power (5 decimals), `null` until two pulses agree |
| `reading_b` | Loss coefficient of the two agreeing pulses in 1/min (6 decimals), `null` until two pulses agree; published only, never corrected |
| `reading_lag_s` | Emitter lag of the two agreeing pulses in seconds, `null` until two pulses agree |
| `in_force_a` | Heating rate in use when the pulses agreed, in °C/min at full power (5 decimals), compared with `reading_a`; `null` until two pulses agree |
| `in_force_b` | Loss coefficient in use when the pulses agreed, in 1/min (6 decimals); `null` until two pulses agree |
| `in_force_lag_s` | Horizon of the anticipation in use when the pulses agreed, in seconds, shown beside `reading_lag_s`; `null` until two pulses agree |
| `corrected` | What the calibration corrected: a list of `heating rate`, `loss coefficient`; empty when nothing was corrected |

The dead times measured by the pulses join the learned dead times like any other
measurement. A quantity of the reading is corrected only when it differs from
the value in use by more than 30 % (see **Calibration** in the
[SmartPI documentation](vtherm_smartpi.md)). The emitter lag is corrected by the
response model itself: the calibration pulses are events of the model, and two
of them that agree replace its history when their emitter lag is more than 30 %
off the one it published before them.

`analysis` groups the advanced live values into `control`, `learning`,
`deadtime`, `governance` and `feedforward`.

`analysis.control` publishes, among the error terms and the band geometry, the
anticipation of the temperature, in heating and in cooling:

| Field | Meaning |
|---|---|
| `anticipation_c` | Correction in °C added to the measured temperature for the proportional and integral errors at the last tick, `0` when off (outside the stable phase, without horizon, without a rising or falling slope, or during a guard cut) |
| `anticipation_horizon_s` | Horizon of the anticipation in seconds (the time between a command and its effect), `0` when none |
| `anticipation_horizon_source` | Where the horizon comes from: `response model` (emitter lag of the response model of the mode: dead time plus response time constant), and without a model `measurement pulses` (emitter lag kept from the measurement pulses of the direct start), `dead times` (the longer of the active dead time of the mode and half its passive dead time) or `none` |
| `effective_dead_time_s` | Effective dead time of the response model in seconds (dead time plus half the response time constant), `null` without a model; the integral holds, the near band above the setpoint and the calibration pulses read it |
| `gain_delay_s` | Delay read by the gain rule, in seconds: the reliable heating (HEAT) or cooling (COOL) dead time after a start, as it is; `null` without it (safe gains) |
| `gain_delay_source` | Where `gain_delay_s` comes from: `dead time`; `null` without a delay |
| `gain_closed_loop_time_s` | Closed-loop time of the gain rule in seconds: the largest of 3 times `gain_delay_s` (4 times for a valve), 2 cycles and 5 minutes; `null` without a delay |

`analysis.deadtime` follows the dead time detection:

| Field | Meaning |
|---|---|
| `state` | Detector state (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Last power seen by the detector |
| `waiting_elapsed_s` | Age of the response being waited for in seconds, `null` when no detection is armed |
| `heat_last_outcome` / `cool_last_outcome` | Outcome of the last wait for a rising / falling response: `detected`, `timeout`, `aborted` (power transition before a detection), `rejected` (the temperature already moved in the direction of the response before the power start, too steeply or too irregularly to measure it), `ignored` (a measurement shorter than the published dead time, dropped because the temperature was already moving in the direction of the response before the start or the window before the start was too short to tell), `null` before the first one |

During the bootstrap, `learning.bootstrap_status` describes the current step:

| Value | Meaning |
|---|---|
| `step1 - waiting response model` | No learning window before the response model of the mode is published (the first heating phase or pulse read as a whole gives it) |
| `step2 - collecting emeas: A:n/5 B:n/5` | Collecting the `a` and `b` measurements |

`valve` is `null` unless the thermostat drives a valve. In valve mode it contains:

| Field | Meaning |
|---|---|
| `vt_dpercent` | Versatile Thermostat regulation threshold in `%` (`null` when unavailable) |
| `vt_period_min` | Versatile Thermostat regulation minimum period in minutes (`null` when unavailable) |
| `opening_remap_active` | `true` when the Versatile Thermostat opening settings change the opening sent to the valve |
| `quantization_step_linear_percent` | Linear demand, in percent, moved by one `1%` valve step at the committed valve position; `1.0` without linearization, `null` when the valve is closed |
| `issues` | Codes for command filtering, delay or an opening remap conflicting with SmartPI linearization: `dpercent_filter`, `period_filter`, `opening_remap`. The same warnings appear in Home Assistant Repairs; filtering or delay alone does not prove a learning failure |

### Response model

The response model describes, for the HVAC mode the model is in, how the room
answers the power: a dead time (sensor, transport, actuator) followed by a
response time constant (the emitter, the air and the nearby mass together). It is fitted on whole measurement events: the
pulses, the bootstrap phases, the calibration pulses and the comfort returns of
the regulation after a rest (those only at night, with the home position known).
The learning of `a` and `b` reads it (no learning window of the learning phase
before it, delivered power, delay of a learning window after a start or a stop),
and so do the horizon of the anticipation (the emitter lag, dead time plus
response time constant), the near band in heating (the effective dead time above
the setpoint, the emitter lag below it), the limits of the calibration pulses and
the measurement pulses of the direct start (they go on while the model of the mode
is not published). With `dead time assumed` these values are the cautious ends:
the horizon is the whole emitter lag and the effective dead time three quarters of
it. The integral holds after a start or a resume last the effective dead time; the
gains read the dead time after a start, not this model.

| Path | Meaning |
|---|---|
| `model.dead_time_s` | Dead time in seconds, `null` without a model |
| `model.response_time_constant_s` | Response time constant in seconds, `null` without a model |
| `model.response_source` | Source of the latest accepted event: `measurement pulses`, `bootstrap`, `calibration`, `regulation`, or `previous version` (values carried over from the dead time and emitter lag learned before the model existed, until the first accepted event); `null` without a model |
| `model.response_confidence` | `measured` (two events agree on the dead time plus the time constant within 20 %), `provisional` (one event, or values of the previous version), `dead time assumed` (no event tells the dead time apart: half of the total is assumed); `null` without a model |

`analysis.learning.response_model`:

| Field | Meaning |
|---|---|
| `events` | Number of accepted events kept for the mode (at most 7) |
| `last` | Last fitted event, `null` before one: `source`, `dead_time_s`, `response_time_constant_s`, `rms_c` (root mean square residual at the temperature changes, °C), `outcome` (`accepted`, `accepted, response time constant at its lower limit` (60 s, a fast emitter), `used in daylight until an event in darkness`, `not used in daylight`, or why it was rejected: `too few temperature changes`, `temperature rise too small`, `no heating seen`, `dead time at its limit`, `response time constant at its limit`, `fit too far from the room temperature`), `daytime` (`true` for an event in daylight: its heating step out of darkness, or less than three emitter lags of darkness after it; used only while the mode has no other event), `prediction_rms_c` (for an event given to the model: root mean square residual, °C, of the model in force before it on its window, its dead time and response time constant fixed; `null` without a model in force) |
| `last_reason` | Outcome of the last event considered: the outcome of its fit, or why it was not read (for example `rest before heating too short`, `heating too short`, `temperature rise too small`, `SmartPI not in control`, `thermostat stopped`, `home position unknown`, `no record`, `24 hours`, `eight fits`); `null` before the first one |
| `prediction_rms_c` | Median of the last 7 `last.prediction_rms_c` values of the mode (°C): how well the model predicts new events; shown only, `null` before one |
| `peak_samples` | Response time constants in seconds read on the last confirmed temperature peaks after a cut (at most 7), from the dead time, the slope of the room before the cut and its losses; shown only |

### Emitter lag learning

`tar_learning` describes the emitter lag learning (see [Emitter lag learning](./vtherm_smartpi.md#emitter-lag-learning)); the regulation does not read its values:

| Field | Meaning |
|---|---|
| `status` | `active`, `checking_dependencies` (until the thermostat has started), `dependencies_unavailable` (numpy/scipy cannot be loaded: nothing is learned) or `valve_mode_not_supported` |
| `bootstrap_s` | Bootstrap/calibration heating dead time (`model.deadtime_heat_s`); twice it is a floor of the emitter lag fitted on a comfort step |
| `fast.value_s` | Published fast emitter lag (confirmed comfort steps), `null` before 3 confirmed steps |
| `fast.confirmed_mornings` | Number of accepted and confirmed comfort steps, one per quiet period at most |
| `fast.lower_bound_s` | Lower bound of the emitter lag in force: median of the last 5 confirmed comfort steps cut by sunrise too early for a value, `null` without any |
| `fast.lower_bound_mornings` | Number of confirmed comfort steps cut by sunrise that gave a lower bound, one per quiet period at most |
| `fast.last_value_s` | Emitter lag of the last accepted comfort step |
| `fast.last_status` | `confirmed`, `unconfirmed` or `none` for the last accepted comfort step |
| `fast.last_unconfirmed_reason` | Why the last accepted comfort step is unconfirmed (for example `the room did not reach its low setpoint before the comfort step`, or at least 1 hour without heating while the room was regulated at its low setpoint) |
| `fast.last_morning` | Last evaluated comfort step, at any hour: `day` (local date, YYYY-MM-DD), `accepted`, `reason` (`accepted`, or why the step gave no value, for example `temperature rise too small`, `comfort step in daylight`, `sunrise too soon after the comfort step`, `comfort step cut by sunrise before its response could be measured` or `comfort step cut by sunrise: gives only a lower bound of the delay, not a value`), `room_class`, `confirmed` (`true` or `false` for a step that gave a value or a lower bound, `null` otherwise) and `unconfirmed_reason` (why such a step is not confirmed, as in `last_unconfirmed_reason`) |
| `refined.tau_e_s` | Published refined emitter lag `τe`, `null` before a publication |
| `refined.class` | Type of room in force from the identification: `thermal_mass`, `no_thermal_mass` or `undetermined` |
| `refined.k_per_s`, `refined.tau_w_h` | Air-wall coupling and wall time constant in force (room with `thermal_mass`) |
| `refined.last_evaluation` | Last evaluation, made when a quiet period ends: `day` (local date of that end, YYYY-MM-DD), `regulated_nights` (the six periods of its two fits are regulated), `class` and `tau_e_s` before the regulated-night rule, and the identified `a` and `b` in the units of `model.a` and `model.b` (information only, never applied; for a room with `thermal_mass`, `b` is the loss of the air to the outside, the walls being modelled apart, so it differs from the effective `model.b`) |
| `night_room_class` | Type of room from the quiet periods without heating: `thermal_mass`, `no_thermal_mass` or `undetermined` |
| `quiet_periods.reason` | Why no quiet period can be found (`home position unknown: the absence of sun cannot be checked`), `null` otherwise |
| `quiet_periods.count` | Number of quiet periods kept as evidence (one per night at most) |
| `quiet_periods.last` | Last closed quiet period: `start` and `end` (local time), `regulated_from` (start of its regulated part, `null` when the room never regulated at its low setpoint) |
| `pending_steps` | Comfort steps waiting for their 2 h response |
| `data_invalid_reason` | Why the current data are excluded (for example `window open`, `temperature jump`, `hvac not heating`), `null` when they are valid |
| `history_h` | Hours of signals kept |
| `last_error` | Why the last computation failed (`the calculation failed, see the log`; the details are in the log), `null` when none |

### Room model

`room_model` describes the room model (see [Room model](./vtherm_smartpi.md#room-model)). Once published, and once a heat balance is in force, it gives the holding power of the feed-forward in heating, shown under `feedforward`.

| Field | Meaning |
|---|---|
| `status` | `learning`, `identified` (model with walls), `simple model` (model without walls), or `not identified: ` followed by the reason (for example `not identified: too little heating during the quiet periods`) |
| `tracking` | State of the room tracking: `ok`, `waiting for an identified model`, `waiting for measurements`, `paused (SmartPI not heating or not in control)`, `settling after a restart`, `measurements drift away from the model`, `measurements inconsistent with the model`, `temperature estimate uncertain`, `large unexplained heat gains or losses` or `wall state implausible` |
| `walls` | `identified`, or why the walls are not in the model: `not identified: temperature sensor too coarse`, `not identified: no thermal mass detected`, `not identified: not confirmed yet`, `not identified: no better than the simple model`; `null` before any result |
| `a` | Heating response at full power, in the units of `model.a` (°C per minute) |
| `b_eq` | Static loss of the model, in the units of `model.b` (per minute). The holding power does not use it: its static part comes from the heat balance (`energy_balance_loss_per_c` and `energy_balance_free_gain_percent`), and `b_eq / a` must agree with `energy_balance_loss_per_c` within 5 % for a publication (nothing is published before a first heat balance) |
| `b_air` | Total loss rate of the air, to the outside and to the walls (per minute); `null` for the simple model |
| `b_wall` | Steady share of that loss that goes through the walls (per minute), with `b_eq` = `b_air` − `b_wall`; `null` for the simple model |
| `forecast_c` | Forecast room temperature in `1h`, `2h`, `6h` and `12h` if the current heating power were kept (°C); `null` while the tracking is not running |
| `forecast_error_c` | Root mean square error of the past forecasts per horizon over the last 7 days, against the measured temperature (°C); the forecasts are replayed with the power actually applied and the measured outdoor temperature |
| `forecast_checks` | Number of past forecasts checked per horizon |
| `unchanged_temperature_error_c` | Same error for a forecast that keeps the temperature unchanged, as a reference |
| `wall_extra_power_percent` | Extra power the walls currently absorb beyond their steady share, in % of full power (negative when they give heat back); `null` without identified walls, without a precise sensor or while the tracking is not `ok` |
| `identified_day` | Day of the values in use (the day their last quiet period ended), `null` before a publication |
| `last_identification_day` | Day of the last identification (the day its last quiet period ended) |
| `last_refusal_reason` | Why the last identification published nothing (for example `waiting for a second series of quiet periods to confirm`, `the quiet periods do not determine the model precisely enough yet`, `the heating response delay does not match the measured temperature`, `the heating response differs too much from the one measured by the regulation`, `the model's heat loss disagrees with the measured heat balance` or `waiting for a qualified heat balance to check the model's heat loss`), `null` when it published |
| `energy_balance_loss_per_c` | Heat loss of the heat balance in force: the power needed per °C of indoor-outdoor difference, in the units of `model.b / model.a`. It is the slope of a straight line through the daily heat balances of the whole days kept (up to 7), computed once a day. Once a model is published, the static part of the holding power is `energy_balance_loss_per_c` × (setpoint − outdoor temperature) − the free gain. A day whose heat balance is refused keeps the previous values; `null` before a first accepted value |
| `energy_balance_free_gain_percent` | Constant free heat gain of the heat balance in force, in % of full power: heat from a warmer room next door or from steady occupants, negative for a loss to a colder room. `0` while the indoor-outdoor difference has not varied enough between the days to tell a gain apart from the losses (and the days show none); `null` before a first accepted value |
| `energy_balance_days` | Number of days used by the heat balance in force, `null` before a first accepted value |
| `energy_balance_day` | Day the values in force were computed, `null` before a first accepted value |
| `energy_balance_refusal_reason` | Why the last daily heat balance gave no new value (`not enough recorded days yet`, `heating response not known yet`, `too few usable days (missing data or no heating)`, `indoor-outdoor temperature difference too small`, `too little heating`, `free heat gains detected; the outdoor temperature has not varied enough yet to separate them from the losses`, `the heating need varies too much from one day to the next`, `the heating need is still drifting from day to day (walls storing or releasing heat)`, `value out of the plausible range` or `computation failed`), `null` when it gave one |
| `sensor_resolution_c` | Smallest step of the temperature sensor seen in the recorded history (°C) |
| `last_error` | Why the last computation failed (`the calculation failed, see the log`; the details are in the log), `null` when none |

### Feed-forward

`feedforward` starts with the source of the holding power (the structural
feed-forward):

| Field | Meaning |
|---|---|
| `source` | `learned model` (the A/B model, `model.b / model.a`), `room model (air and walls)` (published model with walls, tracking `ok`: static part plus the wall extra power), `room model (static part)` (published model with walls while the tracking is not `ok`: static part only) or `room model (simple)` (published model without walls). The static part of a room model source is `room_model.energy_balance_loss_per_c` × (setpoint − outdoor temperature) − `room_model.energy_balance_free_gain_percent` / 100; while no heat balance is in force, or while its `energy_balance_loss_per_c` differs from the published `b_eq / a` by more than 5 %, the source stays `learned model` |
| `wall_power_percent` | Wall extra power applied in the holding power, in % of full power (negative when the walls give heat back). When the wall term starts, stops or changes with a new publication, the applied value moves over 30 minutes from its last value to the new one, so it can differ from `room_model.wall_extra_power_percent` and stay non-zero for 30 minutes after `source` leaves `room model (air and walls)`; `null` when no wall part is applied |

`analysis.feedforward` publishes `u_ff1`, the holding power injected into the command, and its `reason`; `u_ff_final` and `u_ff_effective` carry the same value.

## Publication cadence

The diagnostic sensor is refreshed for meaningful control inputs, cycle
boundaries, explicit forced calculations and diagnostic services. The internal
60-second recalculation timer does not publish when its inputs and committed
power are unchanged. The sensor also suppresses a write when both its state and
complete attribute envelope are identical to the preceding publication.

This limits state-machine and Recorder churn without changing SmartPI control
timing.

## Consumer paths

Live consumers must read from `attributes.live`. Historical graph consumers
must read from `attributes.history`. Consumers should verify
`attributes.schema_version == 2` before interpreting the nested structure.
