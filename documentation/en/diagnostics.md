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

The `history` block contains these eight series:

| Path under `history` | Meaning |
|---|---|
| `temperature.indoor` | Indoor temperature |
| `setpoint.filtered_setpoint` | Filtered control setpoint |
| `power.applied_percent` | Applied command after output constraints |
| `power.command_percent` | Requested command before actuator effects |
| `power.pi_percent` | PI contribution |
| `power.ff_percent` | Feed-forward contribution |
| `model.a` | Learned heating gain |
| `model.b` | Learned heat-loss coefficient |

The applied command does not measure delivered heat.

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
| `feedforward` | FF3 status, dead-band source and canonical FFTrim diagnostics |
| `setpoint` | Filtered setpoint, trajectory and boost summary |
| `autocalib` | Automatic calibration supervisor state |
| `calibration` | Calibration state, AutoCalib retry counter (`autocalib.retry_count`, up to `AUTOCALIB_MAX_RETRIES`) and last completion time |
| `analysis` | Advanced fields used by the supplied diagnostic cards |
| `valve` | Versatile Thermostat valve settings seen by SmartPI, `null` outside valve mode |
| `tar_learning` | Experimental emitter delay learning; present only when the option is enabled |

`analysis` groups the advanced live values into `control`, `learning`,
`trajectory`, `deadtime`, `governance`, `feedforward` and, when
available, `twin`.

`analysis.deadtime` follows the dead time detection:

| Field | Meaning |
|---|---|
| `state` | Detector state (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Last power seen by the detector |
| `waiting_elapsed_s` | Age of the response being waited for in seconds, `null` when no detection is armed |
| `heat_last_outcome` / `cool_last_outcome` | Outcome of the last wait for a rising / falling response: `detected`, `timeout`, `aborted` (power transition before a detection), `null` before the first one |

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

`tar_learning` is present only when the experimental option is enabled (see [Emitter delay learning](./vtherm_smartpi.md#emitter-delay-learning-experimental)):

| Field | Meaning |
|---|---|
| `status` | `active`, `checking_dependencies` (until the thermostat has started), `dependencies_unavailable` (numpy/scipy cannot be loaded: bootstrap value in use) or `valve_mode_not_supported` |
| `gate` | Confirmation rule of the morning values (`G1`) |
| `applied_s` | HEAT active dead time used by the regulation, in seconds |
| `source` | Origin of `applied_s`: `bootstrap`, `fast` or `refined` |
| `bootstrap_s` | Bootstrap/calibration dead time (`model.deadtime_heat_s`), never modified |
| `in_force_s` / `in_force_source` | Learned value in force (refined, else fast) and its origin, `null` before any publication |
| `out_of_bound` | `true` when the value in force lies outside 0.5 to 2 times `bootstrap_s` and is therefore not applied |
| `fast.value_s` | Published fast value (confirmed mornings), `null` before 3 confirmed mornings |
| `fast.confirmed_mornings` | Number of accepted and confirmed mornings |
| `fast.last_value_s` | Value of the last accepted morning |
| `fast.last_status` | `confirmed`, `unconfirmed` or `none` for the last accepted morning |
| `fast.last_unconfirmed_reason` | Why the last accepted morning is unconfirmed |
| `fast.last_morning` | Last evaluated morning: `day`, `accepted`, `reason` (first failed rule or `accepted`), `path` (`night`: baseline from the night class, night with or without heating; `night_regulated`: regulated night with that baseline; `regulated`: regulated night refused by the night class), `room_class` |
| `refined.tau_e_s` | Published refined value `τe`, `null` before a publication |
| `refined.class` | Room class in force from the identification: `mass`, `one` or `unknown` |
| `refined.k_per_s`, `refined.tau_w_h` | Air-wall coupling and wall time constant in force (room classed `mass`) |
| `refined.last_evaluation` | Last daily evaluation: `day`, `regulated_nights` (the six nights qualify), `class` and `tau_e_s` before the regulated-night rule, and the identified `a` and `b` in the units of `model.a` and `model.b` (information only, never applied; for a room classed `mass`, `b` is the loss of the air to the outside, the walls being modelled apart, so it differs from the effective `model.b`) |
| `night_room_class` | Room class from the nights without heating (`mass`, `one`, `inconclusive`) |
| `pending_steps` | Morning steps waiting for their 2 h response |
| `data_invalid_reason` | Why the current data are excluded (for example `window open`, `temperature jump`, `hvac not heating`), `null` when they are valid |
| `history_h` | Hours of signals kept |
| `last_error` | Last computation error, `null` when none |

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
