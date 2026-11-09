# SmartPI diagnostics

SmartPI exposes a dedicated Home Assistant diagnostic sensor. Its state is the
current regulation phase and its attributes follow schema version 2.

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

The `history` block contains these eighteen series:

| Path under `history` | Meaning |
|---|---|
| `temperature.indoor` | Indoor temperature |
| `temperature.integral_error` | Integral state (°C·min); `ki × integral_error` is its share of the command (0 to 1) |
| `control.kp` | Proportional gain |
| `control.ki` | Integral gain |
| `setpoint.filtered_setpoint` | Filtered control setpoint |
| `power.applied_percent` | Applied command after output constraints |
| `power.command_percent` | Requested command before actuator effects |
| `power.pi_percent` | PI contribution |
| `power.ff_percent` | Feed-forward contribution |
| `model.a` | Learned heating gain |
| `model.b` | Learned heat-loss coefficient |
| `feedforward.source` | Holding power source in force, as in `live.feedforward.source` |
| `feedforward.wall_power_percent` | Wall part of the feed-forward applied, in % of full power (negative when the walls give heat back); `null` when no wall part is applied |
| `room_model.a` | Heating response of the room model, in the units of `model.a`; `null` before a publication |
| `room_model.b_eq` | Static loss of the room model, in the units of `model.b`; `null` before a publication |
| `room_model.forecast_error_1h_c` | Root mean square error of the 1 h forecasts over the last 7 days (°C) |
| `room_model.forecast_error_12h_c` | Same for the 12 h forecasts (°C) |
| `deadtime.applied_s` | Heating dead time used by the regulation, in seconds (bootstrap or learned value) |

The feed-forward, room model and dead-time series are always present and are
`null` when the corresponding block is not computed. The applied command does
not measure delivered heat.

## Live diagnostics

The `live` block is organized by responsibility:

| Block | Content |
|---|---|
| `control` | Phase, regulation mode, gains (`kp`, `ki`), effective integral time `ti_min` (minutes), saturation and regulation bands |
| `power` | Current and next-cycle power, PI/FF split, limits and applied power |
| `temperature` | Indoor/outdoor temperatures, errors and integral state |
| `model` | A/B model, confidence, time constant and dead times |
| `learning` | Learning stage, bootstrap progress, accepted updates and drift state |
| `governance` | Thermal regime and model-update decision |
| `feedforward` | Holding power source, FF3 status, dead-band source and canonical FFTrim diagnostics |
| `setpoint` | Filtered setpoint, trajectory and boost summary |
| `autocalib` | Automatic calibration supervisor state |
| `calibration` | Calibration state, AutoCalib retry counter (`autocalib.retry_count`, up to `AUTOCALIB_MAX_RETRIES`) and last completion time |
| `analysis` | Advanced fields used by the supplied diagnostic cards |
| `valve` | Versatile Thermostat valve settings seen by SmartPI, `null` outside valve mode |
| `tar_learning` | Emitter delay learning |
| `room_model` | Room model |

`analysis` groups the advanced live values into `control`, `learning`,
`trajectory`, `deadtime`, `governance`, `feedforward` and, when
available, `twin`.

`analysis.deadtime` follows the dead time detection:

| Field | Meaning |
|---|---|
| `state` | Detector state (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Last power seen by the detector |
| `waiting_elapsed_s` | Age of the response being waited for in seconds, `null` when no detection is armed |
| `heat_last_outcome` / `cool_last_outcome` | Outcome of the last wait for a rising / falling response: `detected`, `timeout`, `aborted` (power transition before a detection), `rejected` (the temperature already moved in the direction of the response before the power start, too steeply or too irregularly to measure it), `null` before the first one |

`analysis.learning.response_lag` describes the response lag used by the `a`
learning:

| Field | Meaning |
|---|---|
| `lag_s` | Published response lag in seconds, `null` until a consistent set of estimates |
| `estimates` | Number of retained estimates (at most 7) |
| `episodes` | Number of active episodes ended since the start of the learning; it keeps counting after the lag is published and may exceed `episodes_target` |
| `episodes_target` | Number of episodes after which the delivered power model is resolved without a published lag (`7`) |
| `episode_median_s` | Median duration of the recent active episodes in seconds, `null` with fewer than 3 episodes |
| `last_reason` | Outcome of the last active episode (estimate kept, or reason it was not used). `episode interrupted by restart`: episode in progress at a restart, not resumed, counted without an estimate. May be empty right after a restart |

During the bootstrap, `learning.bootstrap_status` describes the current step:

| Value | Meaning |
|---|---|
| `step1 - deadtime: heat:… cool:…` | Waiting for the active and passive dead times |
| `step2 - collecting emeas: A:n/5 B:n/5` | Collecting the `a` and `b` measurements |
| `step2 - waiting response lag: estimates N, episodes K/7` | Collections complete; the `a` publication waits for the delivered power model (published lag, or `K` reaches 7) |

`valve` is `null` unless the thermostat drives a valve. In valve mode it contains:

| Field | Meaning |
|---|---|
| `vt_dpercent` | Versatile Thermostat regulation threshold in `%` (`null` when unavailable) |
| `vt_period_min` | Versatile Thermostat regulation minimum period in minutes (`null` when unavailable) |
| `opening_remap_active` | `true` when the Versatile Thermostat opening settings change the opening sent to the valve |
| `quantization_step_linear_percent` | Linear demand, in percent, moved by one `1%` valve step at the committed valve position; `1.0` without linearization, `null` when the valve is closed |
| `issues` | Codes for command filtering, delay or an opening remap conflicting with SmartPI linearization: `dpercent_filter`, `period_filter`, `opening_remap`. The same warnings appear in Home Assistant Repairs; filtering or delay alone does not prove a learning failure |

### Emitter delay learning

`tar_learning` describes the emitter delay learning (see [Emitter delay learning](./vtherm_smartpi.md#emitter-delay-learning)):

| Field | Meaning |
|---|---|
| `status` | `active`, `checking_dependencies` (until the thermostat has started), `dependencies_unavailable` (numpy/scipy cannot be loaded: bootstrap value in use) or `valve_mode_not_supported` |
| `applied_s` | HEAT active dead time used by the regulation, in seconds |
| `source` | Origin of `applied_s`: `bootstrap`, `fast` or `refined` |
| `gains_s` | Dead time used by the gains, in seconds: half of `applied_s` for a learned value, `applied_s` itself for the bootstrap value |
| `bootstrap_s` | Bootstrap/calibration dead time (`model.deadtime_heat_s`), never modified |
| `in_force_s` / `in_force_source` | Learned value in force (refined, else fast) and its origin, `null` before any publication |
| `out_of_bound` | `true` when the value in force lies outside 0.5 to 2 times `bootstrap_s` and is therefore not applied |
| `fast.value_s` | Published fast value (confirmed mornings), `null` before 3 confirmed mornings |
| `fast.confirmed_mornings` | Number of accepted and confirmed mornings |
| `fast.last_value_s` | Value of the last accepted morning |
| `fast.last_status` | `confirmed`, `unconfirmed` or `none` for the last accepted morning |
| `fast.last_unconfirmed_reason` | Why the last accepted morning is unconfirmed (for example a night with at least 1 hour without heating between 23:00 and the comfort step) |
| `fast.last_morning` | Last evaluated morning: `day`, `accepted`, `reason` (`accepted`, or why the morning gave no value, for example `temperature rise too small`), `room_class` |
| `refined.tau_e_s` | Published refined value `τe`, `null` before a publication |
| `refined.class` | Type of room in force from the identification: `thermal_mass`, `no_thermal_mass` or `undetermined` |
| `refined.k_per_s`, `refined.tau_w_h` | Air-wall coupling and wall time constant in force (room with `thermal_mass`) |
| `refined.last_evaluation` | Last daily evaluation: `day`, `regulated_nights` (the six nights qualify), `class` and `tau_e_s` before the regulated-night rule, and the identified `a` and `b` in the units of `model.a` and `model.b` (information only, never applied; for a room with `thermal_mass`, `b` is the loss of the air to the outside, the walls being modelled apart, so it differs from the effective `model.b`) |
| `night_room_class` | Type of room from the nights without heating: `thermal_mass`, `no_thermal_mass` or `undetermined` |
| `pending_steps` | Morning steps waiting for their 2 h response |
| `data_invalid_reason` | Why the current data are excluded (for example `window open`, `temperature jump`, `hvac not heating`), `null` when they are valid |
| `history_h` | Hours of signals kept |
| `last_error` | Last computation error, `null` when none |

### Room model

`room_model` describes the room model (see [Room model](./vtherm_smartpi.md#room-model)). Once published, and once a heat balance is in force, it gives the holding power of the feed-forward in heating, shown under `feedforward`.

| Field | Meaning |
|---|---|
| `status` | `learning`, `identified` (model with walls), `simple model` (model without walls), or `not identified: ` followed by the reason (for example `not identified: too little heating during the nights`) |
| `tracking` | State of the room tracking: `ok`, `waiting for an identified model`, `waiting for measurements`, `paused (SmartPI not heating or not in control)`, `settling after a restart`, `measurements drift away from the model`, `measurements inconsistent with the model`, `temperature estimate uncertain`, `large unexplained heat gains or losses` or `wall state implausible` |
| `walls` | `identified`, or why the walls are not in the model: `not identified: temperature sensor too coarse`, `not identified: no thermal mass detected`, `not identified: not confirmed yet`, `not identified: no better than the simple model`; `null` before any result |
| `a` | Heating response at full power, in the units of `model.a` (°C per minute) |
| `b_eq` | Static loss of the model, in the units of `model.b` (per minute). The holding power does not use it: its static part comes from the heat balance (`energy_balance_loss_per_c` and `energy_balance_free_gain_percent`), and `b_eq / a` must agree with `energy_balance_loss_per_c` within 5 % for a publication |
| `b_air` | Total loss rate of the air, to the outside and to the walls (per minute); `null` for the simple model |
| `b_wall` | Steady share of that loss that goes through the walls (per minute), with `b_eq` = `b_air` − `b_wall`; `null` for the simple model |
| `forecast_c` | Forecast room temperature in `1h`, `2h`, `6h` and `12h` if the current heating power were kept (°C); `null` while the tracking is not running |
| `forecast_error_c` | Root mean square error of the past forecasts per horizon over the last 7 days, against the measured temperature (°C); the forecasts are replayed with the power actually applied and the measured outdoor temperature |
| `forecast_checks` | Number of past forecasts checked per horizon |
| `unchanged_temperature_error_c` | Same error for a forecast that keeps the temperature unchanged, as a reference |
| `wall_extra_power_percent` | Extra power the walls currently absorb beyond their steady share, in % of full power (negative when they give heat back); `null` without identified walls, without a precise sensor or while the tracking is not `ok` |
| `identified_day` | Day of the values in use, `null` before a publication |
| `last_identification_day` | Day of the last daily identification |
| `last_refusal_reason` | Why the last daily identification published nothing (for example `waiting for a second series of nights to confirm`, `the heating response delay does not match the measured temperature`, `the heating response differs too much from the one measured by the regulation` or `the model's heat loss disagrees with the measured heat balance`), `null` when it published |
| `energy_balance_loss_per_c` | Heat loss of the heat balance in force: the power needed per °C of indoor-outdoor difference, in the units of `model.b / model.a`. It is the slope of a straight line through the daily heat balances of the whole days kept (up to 7), computed once a day. Once a model is published, the static part of the holding power is `energy_balance_loss_per_c` × (setpoint − outdoor temperature) − the free gain. A day whose heat balance is refused keeps the previous values; `null` before a first accepted value |
| `energy_balance_free_gain_percent` | Constant free heat gain of the heat balance in force, in % of full power: heat from a warmer room next door or from steady occupants, negative for a loss to a colder room. `0` while the indoor-outdoor difference has not varied enough between the days to tell a gain apart from the losses (and the days show none); `null` before a first accepted value |
| `energy_balance_days` | Number of days used by the heat balance in force, `null` before a first accepted value |
| `energy_balance_day` | Day the values in force were computed, `null` before a first accepted value |
| `energy_balance_refusal_reason` | Why the last daily heat balance gave no new value (`not enough recorded days yet`, `heating response not known yet`, `too few usable days (missing data or no heating)`, `indoor-outdoor temperature difference too small`, `too little heating`, `free heat gains detected; the outdoor temperature has not varied enough yet to separate them from the losses`, `the heating need varies too much from one day to the next`, `the heating need is still drifting from day to day (walls storing or releasing heat)`, `value out of the plausible range` or `computation failed`), `null` when it gave one |
| `sensor_resolution_c` | Smallest step of the temperature sensor seen in the recorded history (°C) |
| `last_error` | Last computation error, `null` when none |

### Feed-forward

`feedforward` starts with the source of the holding power (the structural
feed-forward):

| Field | Meaning |
|---|---|
| `source` | `learned model` (the A/B model, `model.b / model.a`), `room model (air and walls)` (published model with walls, tracking `ok`: static part plus the wall extra power), `room model (static part)` (published model with walls while the tracking is not `ok`: static part only) or `room model (simple)` (published model without walls). The static part of a room model source is `room_model.energy_balance_loss_per_c` × (setpoint − outdoor temperature) − `room_model.energy_balance_free_gain_percent` / 100; while no heat balance is in force, or while its `energy_balance_loss_per_c` differs from the published `b_eq / a` by more than 5 %, the source stays `learned model` |
| `wall_power_percent` | Wall extra power applied in the holding power, in % of full power (negative when the walls give heat back). When the wall term starts, stops or changes with a new publication, the applied value moves over 30 minutes from its last value to the new one, so it can differ from `room_model.wall_extra_power_percent` and stay non-zero for 30 minutes after `source` leaves `room model (air and walls)`; `null` when no wall part is applied |

While a room model source is in force, `analysis.feedforward.ff2_frozen` is
`true` with `ff2_freeze_reason` `room_model`, and `u_ff2` is `0`.

FFTrim uses nested canonical blocks:

```yaml
live:
  feedforward:
    fftrim:
      stationary: {}
      periodic: {}
      transfer: {}
      command_ownership: {}
      observation_mode: ...
      last_reject_reason: ...
      last_update_reason: ...
      last_result: ...
      last_transaction: ...
      windows_since_update: ...
```

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
