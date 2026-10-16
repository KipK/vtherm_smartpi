# Smart-PI: Technical and Scientific Documentation

## 1. Introduction

**Smart-PI** is a self-adaptive discrete proportional-integral controller, available as a standalone integration for *Versatile Thermostat*. Its goal is to replace a fixed-coefficient TPI with a loop that learns the real thermal behavior of the room online.

The current codebase is built around three guiding ideas:

1. identify a simple first-order thermal model with dead time,
2. adapt the PI command from that model,
3. admit or reject each learning window according to the physical regime that
   generated it.

This document describes the behavior that is actually implemented in the current SmartPI code.

---

## 2. Thermal model and control law

### 2.1 Thermal model in use

The learned thermal model is:

$$ \frac{dT_{int}}{dt} = a \cdot u(t) - b \cdot (T_{int}(t) - T_{ext}(t)) $$

With:

- $T_{int}$: indoor temperature,
- $T_{ext}$: outdoor temperature,
- $u(t) \in [0,1]$: normalized active-actuator command,
- $a$: signed actuator thermal gain, positive in HEAT and negative in COOL,
- $b > 0$: passive heat-exchange coefficient with outdoors.

A SmartPI VTherm is mono-mode: it learns either a HEAT model or a COOL model.
The equation is identical in both cases; the sign of $a$ carries the direction
of active thermal action.

The associated time constant is:

$$ \tau = \frac{1}{b} $$

The model is intentionally simple. Dead time is learned separately and injected into tuning heuristics and protections.
For higher-order physical plants, the learned `a` is therefore an effective local
1R1C gain over the operating regime represented by the accepted windows, not a
unique physical constant of the whole installation. It must be validated across
representative operating conditions rather than tuned from a single history.

### 2.2 Command chain

The current command follows this structure:

$$ u = u_{FF} + u_{PI} $$

with:

- `u_ff1`: structural feed-forward derived from $b/a$,
- `u_ff2`: slow `FFTrim` bias,
- `u_ff3`: bounded short-horizon predictive correction, enabled by default and zero outside eligible contexts,
- `u_pi`: discrete PI term.

The value effectively injected into the controller is `u_ff_eff + u_pi`, then it goes through:

- rate limiting,
- anti-windup,
- cycle constraints and protections.

---

## 3. Online identification

### 3.1 Bootstrap order

In phase `HYSTERESIS`, learning follows a strict order:

1. the active-response dead time must be reliable before useful collection for
   `a` (`heat` in HEAT, `cool` in COOL),
2. the passive-response dead time must be reliable before useful collection
   for `b` (`cool` in HEAT, `heat` in COOL),
3. `b` must progress before `a` through a soft gate.

Rules effectively coded:

- `AB_MIN_SAMPLES_B = 8`,
- `AB_MIN_SAMPLES_A = 6`,
- `AB_A_SOFT_GATE_MIN_B = 8`.

SmartPI leaves `HYSTERESIS` once `b` has 8 measurements and `a` has
6 measurements. Both buffers continue filling up to `AB_HISTORY_SIZE = 31`
during normal SmartPI regulation.

The `b` convergence used for drift-aware `a` learning relies on `b_converged_for_a()`:

1. `learn_ok_count_b >= 11`,
2. `len(_b_hat_hist) >= 5`,
3. `MAD(b_hat)/Med(b_hat) <= 0.30`,
4. `range(last_5_b_hat)/Med(b_hat) <= 0.10`.

### 3.2 Effective learning window

The code no longer uses a `WINDOW_MIN_MINUTES` constant. Learning now relies on `LearningWindowManager` and on a sliding window that:

- requires outdoor temperature to be available,
- obeys thermal governance except during calibration,
- rejects or resets an ON/`a` window on the exact observation where the user
  setpoint changes, so a window cannot span the old and new regimes,
- keeps early active ascent after that observation eligible for `a` under the
  existing dead-time, power-stability, robust-slope, FF3, and governance guards,
- keeps passive OFF/`b` learning under its existing physical guards; calibration
  keeps its dedicated learning contract and disturbance-only responses are not
  blocked by this setpoint gate,
- blocks bootstrap collection until the required dead times are reliable,
- applies a post-resume pause (`LEARNING_PAUSE_RESUME_MIN = 20`),
- anchors the window start after the dead-time end when needed,
- monitors power stability through the coefficient of variation (`U_CV_MAX`),
  evaluated once the window mean power reaches `U_CV_MIN_MEAN = 0.05` (below
  it, power returning in an OFF window is not detected by this check),
- extends the window while the slope is not yet considered robust,
- gives up after `DT_MAX_MIN = 240` minutes if no robust signal emerges.

In practice, the decision to submit or extend the window depends on slope robustness, not on a fixed minimum duration.

### 3.3 Robust slope estimation

`ABEstimator.robust_dTdt_per_min()` applies slope guardrails:

- minimum amplitude: `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- minimum number of level changes: `OLS_MIN_JUMPS = 3`,
- statistical significance: `OLS_T_MIN = 2.5`.

Slopes are computed on the raw measured indoor temperature, not on the filtered signals, because the robustness guard counts distinct value jumps of the measurement. The raw history (and the filtered history used by the dead-time detection) keeps the samples of the last `TIN_HISTORY_RETENTION_MIN = 300` minutes (`DT_MAX_MIN` + 60) whatever the sensor update rate, so the slope of a window uses the samples from the window start; `TIN_HISTORY_MAX_SAMPLES = 1800` only bounds memory (about one sample every 10 s over that duration). Validated slopes are then fed into `ABEstimator.learn()`, which:

- rejects physical outliers (`max_abs_dT_per_min = 0.35`),
- learns `b` if `u < U_OFF_MAX`,
- learns `a` if `u > U_ON_MIN`,
- requires `a > 0` in HEAT and `a < 0` in COOL,
- publishes values with robust aggregation (median or weighted median depending on configuration).

### 3.4 Estimation of `a` and `b`

The calculation followed by `learn()` is:

- OFF phase:

$$ b = -\frac{dT/dt}{T_{int} - T_{ext}} $$

- ON phase:

$$ a = \frac{dT/dt + b \cdot (T_{int} - T_{ext})}{u} $$

Histories are filtered through median/MAD. The published magnitude of `a` is
bounded while preserving the mode sign; `b` remains strictly positive.

### 3.5 Dead-time estimation

`DeadTimeEstimator` implements an independent FSM. Its channels describe the
physical room response, not the power-transition direction:

- `deadtime_heat`: delay before temperature starts rising,
- `deadtime_cool`: delay before temperature starts falling.

Therefore OFF -> ON learns `heat` in HEAT mode but `cool` in COOL mode. The
ON -> OFF transition learns the opposite passive response.

Detection tracks the filtered indoor temperature. A detection in progress is not resumed after a restart, and the minimum OFF duration must be observed again in the new session before a detection can be armed.

Reliability becomes true as soon as at least one valid measurement has been captured on the corresponding channel. Each channel keeps its last 6 detections; the published value is their median, so a single aberrant detection does not move it (with one or two detections the median equals their mean). On restart, the published value is recomputed from the restored detections.

---

## 4. Smart-PI control

### 4.1 PI gains

`GainScheduler.calculate()` applies the following logic:

1. if `tau`, the sign of `a`, or the active dead time is unreliable, fall
   back to `KP_SAFE = 0.55` and `KI_SAFE = 0.010`,
2. otherwise compute IMC/SIMC from the active-gain magnitude:

$$ K_p = \frac{1}{|a| \cdot (\lambda + L)} $$

3. compute $K_i = K_p / \tau$,
4. clamp the gains ($K_p \in [0.10, 5.0]$, $K_i \in [0.001, 0.050]$) and apply governance freezes.

With typical room dynamics ($\tau$ of several hours) $K_p / \tau$ falls below
`KI_MIN = 0.001`, so the applied $K_i$ is usually the floor `KI_MIN`; the
near-band taper of $K_i$ is clamped to the same floor.

The effective integral time $T_i = K_p / K_i$ of the applied gains is published
in minutes as `control.ti_min` (`None` when $K_i \le 0$). With $K_i$ on the
floor, $T_i$ still follows the room inertia through $K_p$
($K_p = 1 / (|a| (\lambda + L))$ grows with $\tau$): bench values are about
146 min for $\tau \approx 3$ h, 247 min for $\tau \approx 5$ h and 390 min for
$\tau \approx 8$ h. In a fast room ($\tau \approx 1$ h), $K_p / \tau$ is above the
floor and $K_i$ follows the model.

`GainScheduler.generation` counts the changes of the untapered model gains
(IMC/SIMC or safe gains). It advances only while the gains are not frozen and
is not advanced by the near-band taper. The periodic FF-trim window rejects a
window that spans several generations (`periodic_gains_changed`).

The active dead time is `deadtime_heat` in HEAT and `deadtime_cool` in COOL.

Important:

- near-band damping is applied continuously between the configured near-band edge and the setpoint,
- `NEAR_BAND_TAPER_KI_MIN` reduces the integral gain more strongly than `NEAR_BAND_TAPER_KP_MIN` near the setpoint.

### 4.2 Feed-forward

The structural feed-forward is computed from:

$$ k_{ff} = \frac{b}{a} $$

and:

$$ u_{ff1} = clamp(k_{ff} \cdot (SP - T_{ext}), 0, 1) \cdot warmup\_scale $$

In COOL, $a < 0$ and therefore $k_{ff} < 0$. When outdoors is warmer than the
setpoint, the two negative differences correctly produce positive cooling
power.

`warmup_scale` is not a simple on/off switch. It is the product of:

- the number of valid learning episodes,
- the number of cycles since reset.

The FF2 trim stays frozen (`ff_warmup`) while `warmup_scale` is below 1.

$k_{ff}$ is zero, and therefore $u_{ff1}$ is zero, whenever the model is not
ready (not enough `a` and `b` measurements), `tau` is not reliable, or the sign
of `a` does not match the active mode.

$T_{ext}$ is the last valid outdoor temperature received by SmartPI. When the
outdoor sensor reports no value, FF1, the setpoint model and the near band keep
using that last value, without age limit. Until a first outdoor temperature has
been received, $u_{ff1}$ and $u_{ff2}$ are both zero (`ff_reason`
`ff_no_ext_temp`).

This is combined with:

- `u_ff2`: slow `FFTrim` correction, frozen while `ABConfidence` is `AB_DEGRADED` or `AB_BAD`, or while `warmup_scale` is below 1 (`ff2_freeze_reason` `ff_warmup`), and zero until a first outdoor temperature has been received.

### 4.3 FF3

`ff3.py` adds a predictive correction that is enabled by default and can be disabled by configuration:

- horizon computed by `ff3_predictor.compute_ff3_horizon()`: `deadtime_cycles + FF3_RESPONSE_LOOKAHEAD_CYCLES`, clamped between `FF3_MIN_HORIZON_CYCLES` and `FF3_MAX_HORIZON_CYCLES`,
- local open-loop prediction with the exact discrete ZOH 1R1C model, without calling `ThermalTwin1R1C.step()` and without updating the twin observer,
- max authority `FF3_MAX_AUTHORITY = 0.20`, multiplied by `authority_factor`,
- exploration step `FF3_DELTA_U = 0.05`,
- local quadratic scoring with tracking cost, terminal cost, overshoot cost, and movement cost.

FF3 is disabled if any of the following conditions is not met:

- configuration enabled,
- heating mode,
- outdoor temperature available,
- reliable `tau`,
- thermal twin initialized and prediction usable,
- thermal twin not warming up,
- valid twin steady state,
- no calibration,
- no power shedding,
- no recent setpoint change,
- not in deadband,
- in near-band,
- compatible governance regime,
- no active `setpoint` trajectory,
- a credible external-disturbance context is present.

The disturbance context accepts a structurally usable model even when `model_reliable = False`.
In that case, `prediction_quality = "degraded"` and authority is reduced through `FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR`.
`warming_up = True` and `T_steady_valid = False` remain absolute blockers.

This disturbance context does not use `T_steady` as an RMSE false negative.
It relies on:

- a persistent mismatch between the twin prediction and the observed behavior, interpreted as a credible residual (`bias_warning` or `external_gain_detected` or `external_loss_detected`),
- then dynamic coherence through `perturbation_dTdt` and, when needed, the measured thermal slope.

Under this contract, FF3 is no longer a generic near-band optimizer. It is reserved for disturbance recovery, and it no longer triggers a dedicated cycle restart on deadband entry.

### 4.4 Setpoint management

`SmartPISetpointManager` now drives an analytical trajectory for the P branch.
This trajectory is activated only when a significant thermal gap appears, the model is considered reliable, and the setpoint change calls for shaping the proportional reference.

The principle is:

- the I branch keeps using the raw setpoint,
- the P branch receives `filtered_setpoint`,
- the P branch keeps the raw setpoint until the predicted braking zone is reached,
- a significant setpoint change arms late braking once; HEAT and COOL then use the same signed braking-window equations,
- disturbance-triggered entry continues to require the significant-gap threshold,
- the exact learned 1R1C model, the passive-response dead time of the mode (`deadtime_cool` in HEAT, `deadtime_heat` in COOL), the remaining cycle latency, and the committed cycle power are used to detect the braking zone,
- a smooth late-braking trajectory then shapes the P reference toward a braking target below the raw target in the demand direction,
- when braking is no longer needed, the filtered reference returns progressively to the raw target before the trajectory stops,
- for a trajectory triggered by a setpoint change, entering the `release` phase keeps that phase locked until the trajectory ends, with no return to `tracking`,
- during `release`, the P reference stays on the demand side of the measured room temperature without crossing the raw target: it never exceeds the target in HEAT and never falls below it in COOL,
- the trajectory ends once it is in `release`, the P reference and the room are both within `0.05 °C` of the target, and switching P back to the raw target changes the proportional command by at most `0.03`,
- the trajectory also ends as soon as the room reaches or crosses the target in the demand direction, or on any real setpoint change; the P branch then uses the raw target again,
- `trajectory_active` indicates whether the analytical trajectory is running.

The thermal twin, FF3 and the braking-entry prediction share a pure
constant-power 1R1C propagation function. Each consumer retains its own horizons,
delay policy and input validation.

The propagation uses the discrete 1R1C form in internal linear command space:

$$
\alpha = e^{-b \cdot h}
$$

$$
T_{pred} = T_{ext} + (T - T_{ext}) \cdot \alpha + \frac{a}{b}(1-\alpha) \cdot u
$$

The braking entry predicts the signed change in the active demand direction (`+1` for HEAT, `-1` for COOL) over the remaining cycle, the passive dead time and any queued next-cycle power. The braking gap is that predicted change multiplied by `TRAJECTORY_BRAKE_GAIN = 1.20`.

The canonical `live.setpoint` block publishes the summary:

- `filtered_setpoint`,
- `trajectory_active`,
- `trajectory_source`,
- `boost_active`.

The same payload in both modes exposes trajectory details under
`live.analysis.trajectory`:

- `start_setpoint`, `target_setpoint`, `tau_ref_min`, `elapsed_s` and `phase`,
- `pending_target_change_braking`, `braking_needed` and `model_ready`,
- `remaining_cycle_min`, `next_cycle_reference`, `bumpless_delta` and `bumpless_ready`.

Setpoint reference shaping is limited to the P branch, so the integral path keeps the raw setpoint untouched. No command cap is applied after the PI computation: the PI command goes straight to the soft constraints (rate limiter, `max_on_percent`) and then to the timing constraints.

The current code also applies an explicit guard on positive integral growth during catch-up phases:

- after a significant setpoint change,
- after resuming from `hvac_off`,
- after resuming from detected window opening,
- after resuming from power shedding,
- during a disturbance-recovery trajectory.

This guard does not block integral discharge. If the stored integral already
points against the requested recovery, the guard
allows that opposing memory to unwind as far as zero. It still blocks any new
accumulation beyond zero in the protected direction. This prevents a negative
integral inherited from the previous setpoint from holding the room below a
new, higher setpoint.

The guard is released only when two conditions are met:

- the release error has returned close to the configured deadband scale,
- the signed recovery slope has collapsed persistently.

The slope test combines two criteria:

- a relative threshold based on the maximum recovery slope observed during the catch-up phase,
- an absolute floor in `°C/h` so a very small peak slope cannot make release overly permissive.

The signal used for that release error depends on the servo context:

- during an active setpoint trajectory, release is based on `error_p`, i.e. the filtered-setpoint error actually followed by the proportional branch,
- outside a trajectory, release is based on `error_i`, i.e. the raw setpoint error.

This separation prevents the guard from remaining active after the trajectory has already converged to its filtered reference while the system is physically stabilized below the raw setpoint.

The signed raw error still remains the safety reference: if `error_i <= 0`, the guard is released immediately.

The recovery slope is handled symmetrically:

- in heating mode, a positive slope means moving toward the setpoint,
- in cooling mode, the slope is logically inverted so the same physical reading remains valid.

Window resumes and power-shedding resumes in heating mode add one more stage:

- on resume, SmartPI first arms an explicit `I:HOLD`,
- that hold remains active while the heating response is still inside the usable `deadtime_heat` window,
- when that phase ends, SmartPI re-evaluates the signed residual error,
- it arms the positive guard only if that residual error is still large enough to characterize a meaningful catch-up phase,
- otherwise the I branch returns directly to normal behavior.

This sequence prevents the integral from learning a resume transient that is still dominated by heating dead time.

Transient catch-up states are not restored after a reboot:

- the active analytical trajectory is cleared,
- the integral guard is reset,
- any temporary `integral_hold_mode` is cleared.

The restart therefore keeps only useful persistent PI memory, without restoring transient servo states that are no longer physically valid outside their original runtime session. The wall-clock timestamp of the last valid active regulation tick is persisted and is not refreshed while HVAC is OFF. If inactivity exceeds 96 hours, controller PI memory is purged on resume while the learned A/B model remains available. If the HVAC mode after a restart differs from the mode of the persisted model (HEAT versus COOL), the integral is cleared. When a tick is skipped because the temperature sensor is missing or `guard_cut` is active, time tracking restarts like after OFF, so the error is not integrated over the skipped period.

### 4.5 Deadband, near-band, and protections

`DeadbandManager` handles:

- a symmetric deadband with absolute hysteresis (`DEADBAND_HYSTERESIS = 0.025`),
- an asymmetric near-band in heating mode; in cooling mode the near-band uses the configured `near_band_deg` on both sides of the setpoint,
- automatic near-band sizing in heating mode only, when `deadtime_heat` and the model are usable,
- fallback to configured thresholds otherwise.

Additional protections currently present are:

- `SmartPIGuards` (heating mode only): `guard_cut` forces the output to 0 when the measured temperature exceeds `setpoint + near_band_above` while the heater is active, until it falls back to that threshold minus `NEAR_BAND_HYSTERESIS_C = 0.05°C`; `guard_kick` restarts the cycle when, inside the near-band, the temperature falls below `setpoint - near_band_below` while the command is below 99 %. In cooling mode neither guard acts and the near-band is not auto-sized,
- per-tick anti-windup on the applicable upper limit (see below),
- signed integral guard during resumes, catch-up phases, and setpoint changes,
- explicit `I:HOLD` during the post-resume `deadtime_heat` phase for `window_resume` and `power_shedding_resume` in heating mode,
- `I:HOLD` with source `vt_safety` while Versatile Thermostat safety mode applies its default power instead of the SmartPI command,
- `I:HOLD` with source `deadband_hysteresis_shell` while the hysteretic deadband state is active and the error is outside the core deadband,
- `I:HOLD` with source `governance_sensor_invalid` while no outdoor temperature has been received yet (`DEGRADED` regime),
- freeze of P and I inside the core deadband.

The anti-windup acts at every calculation, in the linear demand space, on the applicable upper limit: `1`, or the linear image of `max_on_percent` when it is lower. It has two parts:

- conditional integration: the integral stops growing when the unconstrained request exceeds the applicable upper limit;
- back-calculation: on every tick where the integral mode is `I:RUN`, `I:SKIP`, `I:GUARD` or `I:CLAMP`, the excess of the PI output (`u_ff + u_pi`, clamped to `[0, 1]`) above the linear image of `max_on_percent` corrects the integral by `beta * excess / Ki`, with `beta = dt / max(Kp/Ki, dt)` (tracking time constant equal to `Ti = Kp/Ki`). The correction only moves the integral toward zero and stops at zero, so an excess carried by the proportional term alone never winds the integral in the opposite direction. The integral stays clamped to `±2/Ki`. Only the integral changes: `u_pi` keeps the decomposition that produced the command.

The following are not treated as saturation: Versatile Thermostat quantization and valve retention (`dpercent`, period), minimum on/off times, and the rate limiter. The behavior is symmetric in HEAT and COOL, does not use Versatile Thermostat command receipts, and is identical with older Versatile Thermostat versions. The `aw_du` diagnostic is the correction (excess, `<= 0`) applied at the last tick.

### 4.6 Auto-calibration

`AutoCalibTrigger` supervises the algorithm outside the control law.

Implemented behavior:

- initial snapshot when `tau`, `deadtime_heat`, and `deadtime_cool` are reliable,
- fallback snapshot after `AUTOCALIB_DT_COOL_FALLBACK_DAYS = 7` days without reliable `deadtime_cool`,
- rolling snapshot every `AUTOCALIB_SNAPSHOT_PERIOD_H = 120` hours,
- hourly check loop,
- cooldown guard `AUTOCALIB_COOLDOWN_H = 24` hours,
- stagnation thresholds `AUTOCALIB_A_MAD_THRESHOLD = 0.25` and `AUTOCALIB_B_MAD_THRESHOLD = 0.30`,
- `b` stagnation is only evaluated while `b` can be learned: the indoor-outdoor gradient is at least `AB_B_MIN_PASSIVE_GRADIENT_C` and the outdoor temperature is at least `AB_B_OUTDOOR_TARGET_MARGIN_C` away from the setpoint on the passive side (below it in HEAT, above it in COOL), the limits of the learning window; without an outdoor or indoor temperature `b` is not evaluated,
- positive exit is evaluated on the parameters that triggered the calibration (`triggered_params`): a triggering `a` or `b` needs at least `AUTOCALIB_EXIT_NEW_OBS_MIN = 1` new observation, a triggering dead time must be reliable; a manual request (`manual_request`) is judged on `deadtime_heat` and `deadtime_cool`,
- up to `AUTOCALIB_MAX_RETRIES = 3` attempts,
- a failed attempt schedules the retry `AUTOCALIB_RETRY_DELAY_H = 6` hours later: until then the cooldown guard is replaced by this delay and the retry does not wait for the snapshot period. The scheduled time is published as `autocalib.next_check_ts` and cleared when the retry starts, when a snapshot is taken or when the stagnation that required it is gone (the retry counter is then reset).

`AutoCalibTrigger` publishes its transitions on the Home Assistant event bus through the handler:
`smartpi_autocalib_snapshot_taken`, `smartpi_autocalib_triggered`, `smartpi_autocalib_success`,
`smartpi_autocalib_retry`, `smartpi_autocalib_degraded` and `smartpi_autocalib_unreachable`. Every event payload carries `entity_id`
(the climate entity, for example `climate.salon`) and `name` (the thermostat name, for example `Salon`),
followed by event-specific fields (`smartpi_autocalib_unreachable`: `triggered_params` and `retry_count`).

The forced cycle is managed by `CalibrationManager`:

- `COOL_DOWN` down to `sp - 0.3°C`,
- `HEAT_UP`,
- `COOL_DOWN_FINAL` back to `sp`.

Before and during each phase the manager checks, with the thermal twin, that the threshold of the phase can be reached. A phase driven at full power converges to `T_steady_max` and a passive phase to `T_steady_passive`; the threshold is reachable when that equilibrium lies beyond it by `CALIBRATION_REACHABILITY_MARGIN_C = 0.5°C` (HEAT: passive `COOL_DOWN` and `COOL_DOWN_FINAL`, full-power `HEAT_UP`; COOL: full-power `COOL_DOWN` and `COOL_DOWN_FINAL`, passive `HEAT_UP`). The check only applies while the twin is usable (`status` ok, `model_reliable`, past its warm-up, valid equilibrium, and `cooling_model_available` in COOL). When the threshold is not reachable the cycle ends at once with the result `Unreachable`, distinct from `Cancelled` (HVAC OFF, timeout of `CALIBRATION_TIMEOUT_MIN = 600` minutes, reset): `last_calibration_time` is not set, `AutoCalibTrigger` consumes no retry and publishes `smartpi_autocalib_unreachable`, and the next attempt waits `AUTOCALIB_COOLDOWN_H` hours (`autocalib.next_check_ts`). The equilibrium at full power does not account for `max_on_percent`. The result is not a published diagnostic: `calibration.state` carries the phase only.

`CalibrationManager.take_completed_result()` returns, once, the result of a run that ended (`Success`, `Cancelled`, `Unreachable`), so that the handler reports every run to `AutoCalibTrigger`, including one that ends in the tick that started it.

---

## 5. Software architecture

### 5.1 Orchestrators

| File                      | Class            | Role                                                                     |
| ------------------------- | ---------------- | ------------------------------------------------------------------------ |
| `algo.py`                 | `SmartPI`        | algorithm facade, full orchestration                                     |
| `handler.py`              | `SmartPIHandler` | Home Assistant bridge: periodic timer, services, attributes              |
| `state_store.py`          | `SmartPIStateStore` | Home Assistant `Store` persistence of the algorithm state             |

### 5.2 `smartpi/` modules effectively used

| Module                 | Main role                                                   |
| ---------------------- | ----------------------------------------------------------- |
| `const.py`             | constants, enums, governance matrix                         |
| `ab_estimator.py`      | slope estimation and `ABEstimator`                          |
| `deadtime_estimator.py` | `DeadTimeEstimator`                                        |
| `ab_aggregator.py`     | median or weighted-median aggregation of `a/b` measurements |
| `ab_drift.py`          | persistent drift detection and recentering for `a/b`, with one candidate sequence per channel |
| `learning_window.py`   | learning-window management and guardrails                   |
| `gains.py`             | gain calculation and freezing                               |
| `controller.py`        | discrete PI, anti-windup, hold, hysteresis                  |
| `deadband.py`          | deadband and near-band                                      |
| `setpoint.py`          | analytical setpoint trajectory for the P reference          |
| `feedforward.py`       | `u_ff1/u_ff2/u_ff3` orchestration                           |
| `ff_trim.py`           | causal slow bias and bounded trim/integral ownership plans  |
| `ff_trim_runtime.py`   | `FFTrimCoordinator`: frozen control decomposition of scheduler requests, routing of trim observer results, bumpless trim/integral transactions at the cycle boundary, transfer state |
| `command_ownership.py` | request identifiers, frozen snapshots and binding of the scheduler applied-command receipts |
| `ff_trim_periodic.py`  | phase-closed periodic-equilibrium window selection          |
| `ff_ab_confidence.py`  | confidence policy for `a/b`                                 |
| `ff3.py`               | bounded FF3 predictive correction and its per-cycle runtime state (`FF3RuntimeState`) |
| `governance.py`        | freeze decisions                                            |
| `calibration.py`       | forced-calibration FSM                                      |
| `autocalib.py`         | supervision and automatic triggering                        |
| `guards.py`            | `guard_cut` and `guard_kick`                                |
| `thermal_twin_1r1c.py` | thermal twin and predictive diagnostics                     |
| `diagnostics.py`       | canonical live diagnostic payload                           |
| `diagnostic_history.py` | versioned envelope and stable Recorder series               |
| `tint_filter.py`       | adaptive indoor-temperature filtering                       |
| `timestamp_utils.py`   | monotonic / wall-clock conversion                           |

### 5.3 Internal composition of `SmartPI`

Components instantiated by `SmartPI.__init__()` notably include:

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name, enabled=use_setpoint_filter, release_tau_factor=release_tau_factor)
self.ctl = SmartPIController(name)
self.est = ABEstimator()
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.calibration_mgr = CalibrationManager(name)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.integral_guard = SmartPIIntegralGuard(name)
self.dt_est = DeadTimeEstimator()
self.twin = ThermalTwin1R1C(dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1)
self.guards = SmartPIGuards()
self.autocalib = AutoCalibTrigger(name)
self.ff3_state = FF3RuntimeState()
self._ff_trim = FFTrim()
self._fftrim_observer = CausalFFTrimObserver(cycle_min, self._causal_power_trace)
self._fftrim_periodic_observer = PeriodicFFTrimObserver(cycle_min)
self._command_ownership = CommandOwnershipTracker()
self.fftrim_coord = FFTrimCoordinator(self)
self._ab_confidence = ABConfidence()
```

### 5.4 Current persistence

The causal observer aligns a second, time-weighted control-ownership timeline
with the physical-power timeline. A strict deadband window can therefore
separate the stable integral bias $J=mean(K_i integral)$ from the physical
deficit $D=H-mean(u_{applied})$. After three independent coherent windows, one
bounded transaction applies
`delta_trim = alpha * lambda * (J + D)` and
`delta_u_i = -alpha * lambda * J`, hence
`delta_u_cmd = alpha * lambda * D`. The same factor `alpha` handles trim
authority, FF branch bounds, integral capacity, and command headroom. The
transaction runs only at the cycle boundary and is identical
in the signed HEAT and COOL model power space.
Transfers below the generic actuator-precision threshold remain ignored unless
they strictly reduce opposing stored trim and integral contributions. This
ownership compaction keeps the same atomic bounds and command invariant.

The stationary and periodic observers reuse one physical power and ownership
timeline but keep separate thermal samples, washout state, and persistence
buffers. The periodic path accepts lower saturation and integral hold as parts
of a controller-driven cycle. It closes only after a return to the same thermal
phase, following at least `max(6 VT cycles, 2 × dead time)`, so its horizon has
no fixed thirty-minute floor. Its time-weighted causal estimate never performs
an integral ownership transfer; persistent corrections remain subject to the
same trim learning rate and authority limit.

A trim freeze clears the stationary buffer. A freeze with reason `saturated`
keeps the periodic samples and pending proposals, and the periodic window
stays valid through it; the trim itself is not modified while it is frozen. Any
other freeze reason clears the periodic buffer and invalidates the window.

The current payload from `SmartPI.save_state()` notably contains:

```python
{
    "version": 2,
    "learning_enabled": ...,
    "last_active_wall_ts": ...,
    "deadtime_skip_count_a": ...,
    "deadtime_skip_count_b": ...,
    "est_state": {...},
    "dt_est_state": {...},
    "gov_state": {...},
    "ctl_state": {...},
    "sp_mgr_state": {...},
    "lw_state": {...},
    "db_state": {...},
    "cal_state": {...},
    "gs_state": {...},
    "twin_state": {...},
    "guards_state": {...},
    "ac_state": {...},
    "ff_v2_trim": {...},
    "ff_trim_delta": ...,
    "tint_filter_state": {...},
}
```

The control loop schedules at most one delayed write of this payload per five-minute window; Home Assistant flushes a pending write at shutdown. Services and entity removal write a snapshot immediately.

Past event timestamps (temperature histories, calibration start) are persisted as wall-clock times and converted back to the monotonic clock on load, so they keep their position in the past across a restart; in particular, the calibration timeout keeps running after a restart.

Only the learned trim remains persistent. Ownership windows, pending
proposals, and integral transfers are transient; after restart the observer is
rebuilt and the existing three-cycle trim freeze still applies.

Command ownership comes from the applied-command receipts of the Versatile
Thermostat cycle scheduler (`vtherm_api` `AppliedCommandReceipt`). Immediately
before submitting a request, SmartPI freezes its control decomposition and
passes a strictly increasing `request_id` to `start_cycle`. The scheduler
echoes that identifier in a receipt for each command it really applies: a
`cycle_start` receipt at each master-cycle start, including its automatic
restart, and an `update` receipt for each valve position applied during a
cycle. No receipt is emitted for a switch request waiting for the next cycle.
The receipt commits the applied actuator power, mapped to the linear model
space, and binds the frozen snapshot of its request. A receipt is owned only
when its request is known, keeps its HVAC mode, carries no `min_on_off` or
`valve_retained` alteration, and was not timing constrained; a rejected
request stays rejected for its later receipts. Untagged commands are never
owned: Versatile Thermostat overrides such as the safety default power, and
the deferred valve updates of Versatile Thermostat. The last 32 requests are
kept so that a scheduler applying an older request still attributes it to
that request. A missing or rejected receipt never falls back to the live
controller state. A bumpless transfer is engaged once an owned receipt
carries a request staged after the transfer.

Without receipt support in Versatile Thermostat, SmartPI submits untagged
requests and commits the power reported by the cycle-start callback, or the
published valve opening in valve mode, without ownership. The stationary trim
then uses the sampled `u_ff1`, the periodic trim and the bumpless transfer stay
inactive, and PI regulation is unchanged. A valve update without a published
opening commits nothing and marks a trace discontinuity.

### 5.5 Published diagnostics

`diagnostics.py` exposes the algorithm and canonical live views:

- `SmartPI.get_published_diagnostics()`: returns the canonical `live` payload for the dedicated diagnostic sensor,
- `build_published_diagnostics()`: the function that builds that payload.

`diagnostic_history.py` wraps this payload in `schema_version`, `live` and the
stable eleven-series `history` block. Normal sensor entities exclude `live` from
Recorder; the debug sensor subclass records it directly. The debug option only selects the sensor class that is recorded; it does not
add a second diagnostic block.

`analysis.learning.learn_ok_count` and `learn_skip_count` are runtime counters
relative to the current Home Assistant process. Persisted model counters remain
internal and are used for model quality and warm-up decisions. When the thermal
twin is usable, its public diagnostic fields are exposed under `analysis.twin`.

`feedforward.fftrim.command_ownership` exposes whether the scheduler emits
receipts, the latest binding status and rejection reason, the request
identifier (`request_sequence`), the receipt kind, the requested and applied
powers and their delta, the applied ON/OFF durations, the receipt alterations,
the timing-constraint flag, and the rejection counters of the current Home
Assistant session. These values are observational: they explain a causal
acceptance or rejection without changing the learning decision.

---

## 6. Important constants

### 6.1 Learning

| Constant                       | Value  |
| ------------------------------ | ------ |
| `AB_HISTORY_SIZE`              | `31`   |
| `AB_MIN_SAMPLES_B`             | `8`    |
| `AB_MIN_SAMPLES_A`             | `6`    |
| `AB_A_SOFT_GATE_MIN_B`         | `8`    |
| `AB_CONFIDENCE_MIN_SAMPLES_A`  | `11`   |
| `AB_CONFIDENCE_MIN_SAMPLES_B`  | `11`   |
| `AB_B_CONVERGENCE_MIN_SAMPLES` | `11`   |
| `AB_B_CONVERGENCE_MIN_BHIST`   | `5`    |
| `AB_B_CONVERGENCE_MAD_RATIO`   | `0.30` |
| `AB_B_CONVERGENCE_RANGE_RATIO` | `0.10` |
| `DT_DERIVATIVE_MIN_ABS`        | `0.05` |
| `OLS_MIN_JUMPS`                | `3`    |
| `OLS_T_MIN`                    | `2.5`  |
| `DT_MAX_MIN`                   | `240`  |
| `U_OFF_MAX`                    | `0.05` |
| `U_ON_MIN`                     | `0.20` |

### 6.2 Regulation

| Constant                      | Value   |
| ----------------------------- | ------- |
| `KP_SAFE`                     | `0.55`  |
| `KI_SAFE`                     | `0.010` |
| `MAX_STEP_PER_MINUTE`         | `0.25`  |
| `SETPOINT_BOOST_RATE`         | `0.50`  |
| `SMARTPI_RECALC_INTERVAL_SEC` | `60`    |
| `LEARNING_PAUSE_RESUME_MIN`   | `20`    |

### 6.3 Bands and setpoint shaping

| Constant                   | Value   |
| -------------------------- | ------- |
| `DEFAULT_DEADBAND_C`       | `0.05`  |
| `DEADBAND_HYSTERESIS`      | `0.025` |
| `DEFAULT_NEAR_BAND_DEG`    | `0.40`  |
| `NEAR_BAND_TAPER_KP_MIN`   | `0.75`  |
| `NEAR_BAND_TAPER_KI_MIN`   | `0.45`  |
| `SETPOINT_BOOST_THRESHOLD` | `0.3`   |
| `SETPOINT_BOOST_ERROR_MIN` | `0.3`   |

### 6.4 Auto-calibration and FF

| Constant                          | Value  |
| --------------------------------- | ------ |
| `AUTOCALIB_SNAPSHOT_PERIOD_H`     | `120`  |
| `AUTOCALIB_DT_COOL_FALLBACK_DAYS` | `7`    |
| `AUTOCALIB_COOLDOWN_H`            | `24`   |
| `AUTOCALIB_A_MAD_THRESHOLD`       | `0.25` |
| `AUTOCALIB_B_MAD_THRESHOLD`       | `0.30` |
| `AUTOCALIB_MAX_RETRIES`           | `3`    |
| `AUTOCALIB_RETRY_DELAY_H`         | `6`    |
| `AUTOCALIB_EXIT_NEW_OBS_MIN`      | `1`    |
| `FF_TRIM_RHO`                     | `0.15` |
| `FF_TRIM_LAMBDA`                  | `0.05` |
| `FF3_DELTA_U`                     | `0.05` |
| `FF3_MAX_AUTHORITY`               | `0.20` |
| `FF3_NEARBAND_GAIN`               | `0.50` |
| `FF3_MIN_HORIZON_CYCLES`          | `2`    |
| `FF3_RESPONSE_LOOKAHEAD_CYCLES`   | `2`    |
| `FF3_MAX_HORIZON_CYCLES`          | `8`    |
| `FF3_ACTION_SENSITIVITY_EPS_C`    | `1e-4` |
| `FF3_SCORE_EPS_COST`              | `1e-4` |
| `FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR` | `0.5` |

---

## 7. Safety-First governance

The matrix actually used in `const.py` is:

| Regime           | Thermal (`a/b`) | Gains              |
| ---------------- | --------------- | ------------------ |
| `WARMUP`         | `ADAPT_ON`      | `FREEZE`           |
| `EXCITED_STABLE` | `ADAPT_ON`      | `ADAPT_ON`         |
| `NEAR_BAND`      | `ADAPT_ON`      | `ADAPT_ON`         |
| `DEAD_BAND`      | `HARD_FREEZE`   | `HARD_FREEZE`      |
| `SATURATED`      | `ADAPT_ON`      | `FREEZE`           |
| `HOLD`           | `HARD_FREEZE`   | `SOFT_FREEZE_DOWN` |
| `PERTURBED`      | `HARD_FREEZE`   | `HARD_FREEZE`      |
| `DEGRADED`       | `HARD_FREEZE`   | `HARD_FREEZE`      |

This governance is central because:

- it prevents learning on data considered polluted,
- it avoids moving gains in poorly informative regimes,
- it still keeps thermal learning active in some constrained regimes such as `SATURATED`.

The matrix drives the gain updates and thermal learning only. A regime transition (`REGIME_TRANSITION`) freezes gains and learning but does not hold the integral. The integral is held only by the sources listed in the protections of section 4 (deadtime, resume phases, `vt_safety`) and by two governance-related sources: `deadband_hysteresis_shell` (hysteretic deadband state active, error outside the core deadband) and `governance_sensor_invalid` (`DEGRADED` regime before a first outdoor temperature has been received). When the outdoor sensor stops reporting after a first value, the regime is `DEGRADED`: gains, thermal learning and the FF trim stay frozen, the integral keeps integrating, and the control path uses the last valid outdoor temperature.

---

## 8. References

1. **Astrom K.J. and Hagglund T.**, work on PID regulation and FOPDT model usage.
2. **Sundaresan K.R. and Krishnaswamy P.R.**, estimation of delay and time-constant parameters.
3. **OLS** regression with Student t-test for slope validation.
