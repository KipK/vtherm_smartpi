# The SmartPI Algorithm

- [The SmartPI Algorithm](#the-smartpi-algorithm)
  - [How it works](#how-it-works)
  - [Operating phases](#operating-phases)
  - [Advanced features](#advanced-features)
  - [Configuration](#configuration)
  - [Diagnostic metrics](#diagnostic-metrics)
  - [Services](#services)

## How it works

SmartPI is an alternative to the classic TPI approach, provided as a standalone integration (**vtherm_smartpi**) for Versatile Thermostat. Its goal is straightforward: learn the real thermal behavior of the room, then adapt the regulation automatically.

In practice, SmartPI continuously learns three main things:

- **a**: heating capability,
- **b**: thermal loss rate,
- **dead times**: the delay between a heating change and the visible temperature response.

From that model, SmartPI builds a more precise heating command than a fixed TPI:

- a **PI** part to correct the current error,
- a **feed-forward** part to anticipate the holding power,
- extra protections when the temperature is close to the target.

The algorithm is recalculated regularly and keeps learning whenever the current conditions are considered reliable.

## Operating phases

### Phase 1: Hysteresis and bootstrap

At startup, SmartPI begins with a learning phase in hysteresis mode.

By default:

- heating starts below `Setpoint - 0.3°C`,
- heating stops above `Setpoint + 0.5°C`.

These thresholds are configurable.

During this phase, learning follows a strict order visible in `specific_states.smart_pi.ab_learning.bootstrap_status`:

1. **Dead time measurement**: SmartPI waits until it has measured the heating and cooling delays.
2. **Initial collection**: it gathers the first reliable samples.
3. **Model consolidation**: it completes the history until the model becomes robust enough.

Useful reference points:

- `b` needs at least **11** validated samples,
- `a` can start earlier, with a minimum of **7** samples, but remains partially gated until `b` has progressed enough,
- the full history target is **31** validated measurements per parameter.

As long as this phase is not reliable enough, SmartPI stays in hysteresis mode.

### Phase 2: Stable SmartPI regulation

When the thermal model becomes reliable, SmartPI switches to stable mode.

In this phase, the command combines:

- a **PI controller**,
- a **feed-forward** holding term based on setpoint and outdoor temperature,
- dedicated handling for zones close to the setpoint.

Near the target, SmartPI uses:

- a **deadband** to avoid permanent micro-corrections,
- a **near-band** to adapt behavior around the target,
- a dedicated **hold** mode that converges toward the power really needed to stay stable.

If the **FF3** option is enabled, SmartPI can also apply a small short-horizon predictive correction. This feature is only used in heating mode, close to the setpoint, outside setpoint trajectories, and only when a credible external-disturbance context is detected from a persistent mismatch between the thermal-twin prediction and the observed response, combined with compatible thermal dynamics.

### Phase 3: Auto-calibration

SmartPI also supervises the quality of its learning over time.

The general behavior is:

1. When the model becomes reliable, SmartPI stores a reference **snapshot**.
2. Then an hourly supervision loop checks whether learning is still progressing.
3. If the model is stagnating, an automatic calibration can be triggered.

The calibration cycle forces a simple sequence:

1. cool down,
2. forced heating,
3. final cool down.

This sequence is mainly used to revalidate dead times and restart cleaner learning.

Two practical points:

- a rolling snapshot is refreshed roughly every **5 days**,
- if cooling dead time stays unavailable for **7 days**, SmartPI can continue with a partial snapshot,
- after **3** unsuccessful calibration attempts, the thermostat keeps running but reports a degraded model in diagnostics.

## Advanced features

### 1. Automatic dead time estimation

SmartPI automatically measures the delay between a heating change and the room reaction. This is especially useful on slow or highly inertial systems.

### 2. Setpoint-adjacent zone management

When the temperature gets close to the target, SmartPI does not behave the same as when it is far away:

- it uses an asymmetric **near-band** in heating mode,
- it applies protections to limit overshoot,
- it can stop or restart a cycle earlier when needed.

### 3. Setpoint change handling

When a significant thermal gap appears and the model is considered reliable, SmartPI activates an analytical trajectory on the P branch to shape the proportional reference.

This trajectory:

- leaves the I branch using the raw setpoint,
- publishes a filtered reference as `filtered_setpoint` for the P branch,
- keeps the raw setpoint on the P branch while the room is still far from the target,
- uses the exact learned 1R1C model, `deadtime_cool`, the remaining cycle latency, the currently committed cycle power, and the expected next-cycle power to detect when late braking must begin,
- lowers the proportional reference smoothly only near the target while keeping a small positive P demand to avoid over-braking,
- raises the filtered reference back to the raw target progressively when braking is no longer needed,
- once the release phase starts for a setpoint-driven trajectory, it stays in that phase until the trajectory ends,
- keeps the final handoff bumpless by waiting for both a small enough proportional command gap and a measured temperature close enough to the target before disabling the trajectory,
- exposes its status through `trajectory_active`,
- stops once that final handoff remains bumpless and the measured temperature is close enough to the target, or when the reliability conditions are no longer met.

The integral is not used the same way during recovery phases:

- after a significant setpoint change,
- after resuming from a detected window opening,
- after resuming from power shedding,
- during a disturbance-recovery trajectory.

In these cases, SmartPI blocks positive integral growth while the system is still catching up. The integral is still allowed to discharge if the signal changes direction.

Release no longer depends only on the near-band:

- it waits for a real stabilization phase, detected through a persistently collapsed recovery slope,
- that slope check uses both a relative threshold based on the observed recovery peak and a small absolute floor, so release stays robust across different thermal speeds,
- during an active setpoint trajectory, it relies on the error of the reference actually followed by the P branch (`error_p`, i.e. the filtered setpoint),
- outside a trajectory, it still relies on the raw setpoint error (`error_i`),
- in case of a real signed overshoot, release remains immediate from the raw error.

Once the guard is released, the trajectory-specific attenuation of positive integral growth is no longer applied, so the integral can resume a normal correction of the residual steady-state error.

Window resumes and power-shedding resumes also follow a stricter rule in heating mode:

- SmartPI first enters an explicit `I:HOLD` while heating is still inside the useful `deadtime_heat` window,
- at the end of that phase it re-evaluates the residual error,
- it arms the positive-integral guard only if that residual error is still significant enough,
- otherwise it returns directly to normal integral behavior.

This prevents the integral from learning a pure catch-up transient while the heating response is still not fully observable.

Transient recovery states are not carried across a restart:

- an active analytical trajectory,
- a temporary resume `I:HOLD`,
- an already armed recovery guard.

After a reboot, SmartPI therefore starts again without replaying these transient servo states into the next runtime session.

### 4. Hold behavior inside the deadband

Inside the deadband, SmartPI does more than simply "do nothing":

- it enters hold without a brutal command jump,
- it keeps aiming for a coherent holding power,
- it slowly adjusts its feed-forward bias if the room drifts repeatedly.

SmartPI also distinguishes two notions:

- the hysteretic deadband state, used to stabilize the state machine,
- the actual configured deadband, used to decide whether P and I must really be frozen.

This avoids keeping a small residual error frozen only because the hysteresis shell still holds `in_deadband`.

### 5. Additional protections

SmartPI also includes several useful protections:

- integral anti-windup,
- temporary blocking of positive integral growth during resumes and catch-up phases,
- thermal guard when the setpoint is lowered,
- near-setpoint protections that cut faster on overshoot or restart earlier when the room falls back.

## Configuration

Default settings are suitable for most installations.

| Parameter | Role | Default value |
|-----------|------|---------------|
| **Deadband** | Tolerance zone around the setpoint. | `0.05°C` |
| **Setpoint filter** | Enables the late-braking trajectory on the P branch. | `disabled` |
| **FF3** | Short-horizon predictive correction reserved for disturbance recovery near the setpoint. | `enabled` |
| **Lower hysteresis threshold** | Restart threshold during bootstrap. | `0.3°C` |
| **Upper hysteresis threshold** | Stop threshold during bootstrap. | `0.5°C` |
| **SmartPI debug mode** | Adds detailed diagnostics. | `disabled` |

> If the temperature oscillates too much around the setpoint, the first setting to review is usually the **deadband**.

## Diagnostic metrics

SmartPI diagnostics are published in `specific_states.smart_pi`.

- in normal mode, this block contains a structured summary of the current behavior,
- in debug mode, the same block is kept and `specific_states.smart_pi.debug` is added.

### Published structure in normal mode

| Block | Content |
|-------|---------|
| `control` | current phase, mode, hysteresis state, `kp`, `ki`, restart reason |
| `power` | current cycle percent, next cycle percent, PI, feed-forward and hold contributions |
| `temperature` | measured temperature, error, integral, current integral mode, integral guard source |
| `model` | thermal model state: `a`, `b`, confidence level, dead times |
| `ab_learning` | learning tracking: stage, bootstrap progress, sample counters, last accept/reject reason |
| `governance` | current regime and thermal update decision |
| `feedforward` | FF3 status, thermal twin usability, deadband power source |
| `setpoint` | `filtered_setpoint`, `trajectory_active`, trajectory source |
| `autocalib` | automatic supervision state |
| `calibration` | forced calibration state |

### Focus on `ab_learning`

The `ab_learning` block is the most useful entry point to follow learning without enabling debug mode:

| Field | Description |
|-------|-------------|
| `stage` | high-level state: `bootstrap`, `learning`, `monitoring`, or `degraded` |
| `bootstrap_progress_percent` | bootstrap progress |
| `bootstrap_status` | current bootstrap step |
| `accepted_samples_a` | number of validated `a` samples |
| `accepted_samples_b` | number of validated `b` samples |
| `target_samples` | target history size |
| `last_reason` | last reason produced by the learning logic |
| `a_drift_state`, `b_drift_state` | drift monitoring state |

### Debug mode

When SmartPI debug mode is enabled, `specific_states.smart_pi.debug` additionally exposes:

- learning internals (`tau_min`, counters, rejection reasons),
- the full command chain (`u_cmd`, `u_limited`, `u_applied`, `aw_du`),
- the full feed-forward chain (`u_ff1`, `u_ff2`, `u_ff_final`, `u_ff3`, `u_ff_eff`),
- the FF3 activation context (`ff3_disturbance_active`, `ff3_disturbance_reason`, `ff3_disturbance_kind`, `ff3_residual_persistent`, `ff3_dynamic_coherent`),
- band, protection, and dead-time states, including the detailed integral guard state and the `core deadband`,
- setpoint trajectory details (`trajectory_start_sp`, `trajectory_target_sp`, `trajectory_tau_ref`, `trajectory_elapsed_s`, `trajectory_phase`, `trajectory_pending_target_change_braking`, `trajectory_braking_needed`, `trajectory_model_ready`, `trajectory_remaining_cycle_min`, `trajectory_next_cycle_u_ref`, `trajectory_bumpless_u_delta`, `trajectory_bumpless_ready`),
- auto-calibration details,
- advanced thermal twin diagnostics when available.

## Services

### `reset_smart_pi_learning`

Use this if the thermal behavior of the room has changed significantly, for example after replacing emitters or improving insulation.

This service resets SmartPI learning and forces a return to bootstrap mode.

### `force_smart_pi_calibration`

Requests a SmartPI calibration to refresh dead-time measurement and also help adjust `a` and `b`.

This service is useful when:

- the reported dead times look inconsistent,
- the regulation behaves worse than before,
- you want to restart a recalibration sequence without waiting for the automatic trigger.

If the thermostat is still in bootstrap/hysteresis mode, the request is ignored.

### `reset_smartpi_integral`

Resets the SmartPI controller integral accumulator to zero and releases any active integral hold.

This service is useful when:

- the integral has accumulated an unsuitable value following an exceptional event (prolonged heating outage, window left open for a long time, etc.),
- you want to restart from a neutral integral state without resetting the entire SmartPI learning.
