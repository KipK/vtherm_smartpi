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
| `power.applied_percent` | Physically applied power |
| `power.command_percent` | Requested command before actuator effects |
| `power.pi_percent` | PI contribution |
| `power.ff_percent` | Feed-forward contribution |
| `model.a` | Learned heating gain |
| `model.b` | Learned heat-loss coefficient |

## Live diagnostics

The `live` block is organized by responsibility:

| Block | Content |
|---|---|
| `control` | Phase, regulation mode, gains, saturation and regulation bands |
| `power` | Current and next-cycle power, PI/FF split, limits and applied power |
| `temperature` | Indoor/outdoor temperatures, errors and integral state |
| `model` | A/B model, confidence, time constant and dead times |
| `learning` | Learning stage, bootstrap progress, accepted updates and drift state |
| `governance` | Thermal regime and model-update decision |
| `feedforward` | FF3 status, dead-band source and canonical FFTrim diagnostics |
| `setpoint` | Filtered setpoint, trajectory, boost and landing summary |
| `autocalib` | Automatic calibration supervisor state |
| `calibration` | Calibration state, retry count and last completion time |
| `analysis` | Advanced fields used by the supplied diagnostic cards |

`analysis` groups the advanced live values into `control`, `learning`,
`trajectory`, `landing`, `deadtime`, `governance`, `feedforward` and, when
available, `twin`.

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
