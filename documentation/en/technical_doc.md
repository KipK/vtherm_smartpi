# Smart-PI: Technical and Scientific Documentation

## 1. Introduction

**Smart-PI** is a self-adaptive discrete proportional-integral controller, available as a standalone integration for *Versatile Thermostat*. Its goal is to replace a fixed-coefficient TPI with a loop that learns the real thermal behavior of the room online.

The current codebase is built around three guiding ideas:

1. identify a simple first-order thermal model with dead time,
2. adapt the PI command from that model,
3. freeze or limit some adaptations when the physical regime is not considered reliable.

This document describes the behavior that is actually implemented in the current SmartPI code.

---

## 2. Thermal model and control law

### 2.1 Thermal model in use

The learned thermal model is:

$$ \frac{dT_{int}}{dt} = a \cdot u(t) - b \cdot (T_{int}(t) - T_{ext}(t)) $$

With:

- $T_{int}$: indoor temperature,
- $T_{ext}$: outdoor temperature,
- $u(t) \in [0,1]$: normalized heating command,
- $a$: heating thermal gain,
- $b$: loss coefficient.

The associated time constant is:

$$ \tau = \frac{1}{b} $$

The model is intentionally simple. Dead time is learned separately and injected into tuning heuristics and protections.

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

1. `deadtime_heat` must be reliable before useful collection for `a`,
2. `deadtime_cool` must be reliable before useful collection for `b`,
3. `b` must progress before `a` through a soft gate.

Rules effectively coded:

- `AB_MIN_SAMPLES_B = 8`,
- `AB_MIN_SAMPLES_A = 6`,
- `AB_MIN_SAMPLES_A_CONVERGED = 6`,
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
- blocks bootstrap collection until the required dead times are reliable,
- applies a post-resume pause (`LEARNING_PAUSE_RESUME_MIN = 20`),
- anchors the window start after the dead-time end when needed,
- monitors power stability through the coefficient of variation (`U_CV_MAX`),
- extends the window while the slope is not yet considered robust,
- gives up after `DT_MAX_MIN = 240` minutes if no robust signal emerges.

In practice, the decision to submit or extend the window depends on slope robustness, not on a fixed minimum duration.

### 3.3 Robust slope estimation

`ABEstimator.robust_dTdt_per_min()` applies slope guardrails:

- minimum amplitude: `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- minimum number of level changes: `OLS_MIN_JUMPS = 3`,
- statistical significance: `OLS_T_MIN = 2.5`.

Validated slopes are then fed into `ABEstimator.learn()`, which:

- rejects physical outliers (`max_abs_dT_per_min = 0.35`),
- learns `b` if `u < U_OFF_MAX`,
- learns `a` if `u > U_ON_MIN`,
- publishes values with robust aggregation (median or weighted median depending on configuration).

### 3.4 Estimation of `a` and `b`

The calculation followed by `learn()` is:

- OFF phase:

$$ b = -\frac{dT/dt}{T_{int} - T_{ext}} $$

- ON phase:

$$ a = \frac{dT/dt + b \cdot (T_{int} - T_{ext})}{u} $$

Histories are filtered through median/MAD and published values are bounded.

### 3.5 Dead-time estimation

`DeadTimeEstimator` implements an independent FSM:

- OFF -> ON transition: wait for a heating response,
- ON -> OFF transition: wait for a cooling response.

Reliability becomes true as soon as at least one valid measurement has been captured on the corresponding channel. Published values are the averages of the `heat` and `cool` histories.

---

## 4. Smart-PI control

### 4.1 PI gains

`GainScheduler.calculate()` applies the following logic:

1. if `tau` is not reliable, fall back to `KP_SAFE = 0.55` and `KI_SAFE = 0.010`,
2. otherwise compute the heuristic:

$$ K_{p,heu} = 0.35 + 0.9 \cdot \sqrt{\frac{\tau}{200}} $$

3. if `deadtime_heat` is reliable and `a > 0`, compute IMC:

$$ K_{p,IMC} = \frac{1}{2 \cdot a \cdot (L/60)} $$

4. choose `min(Kp_IMC, Kp_heu)`,
5. compute:

$$ K_i = \frac{K_p}{\max(\tau, 10)} $$

6. apply governance freezes.

Important:

- the bounds effectively applied in `GainScheduler` are currently wide (`Kp` clamped to `[0.05, 10.0]`, `Ki` to `[0.0001, 1.0]`),
- current default near-band factors are `DEFAULT_KP_NEAR_FACTOR = 1.0` and `DEFAULT_KI_NEAR_FACTOR = 1.0`.

### 4.2 Feed-forward

The structural feed-forward is computed from:

$$ k_{ff} = \frac{b}{a} $$

and:

$$ u_{ff1} = clamp(k_{ff} \cdot (SP - T_{ext}), 0, 1) \cdot warmup\_scale $$

`warmup_scale` is not a simple on/off switch. It depends on:

- the number of valid learning episodes,
- the number of cycles since reset,
- the reliability of `tau`.

This is combined with:

- `u_ff2`: slow `FFTrim` correction,
- `ABConfidence`: confidence policy for `a/b`,
- fallback in `AB_BAD` mode to a zero feed-forward after `AB_BAD_PERSIST_CYCLES = 3`.

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
- the exact learned 1R1C model, `deadtime_cool`, the remaining cycle latency, and the committed cycle power are used to detect the braking zone,
- a smooth late-braking trajectory is then applied near the target while preserving a minimum positive proportional demand,
- for HEAT setpoint trajectories, a landing cap can constrain the internal command after PI computation when the model predicts that stored heat is enough to reach the target,
- when braking is no longer needed, the filtered reference returns progressively to the raw target before the trajectory stops,
- for a trajectory triggered by a setpoint change, entering the `release` phase keeps that phase locked until the trajectory ends, with no return to `tracking`,
- `trajectory_active` indicates whether the analytical trajectory is running,
- the trajectory ends only once the handoff remains bumpless, the measured temperature is close enough to the target, and the landing state allows release, or when the reliability conditions are no longer met.

The landing cap uses the discrete 1R1C form in internal linear command space:

$$
\alpha = e^{-b \cdot h}
$$

$$
T_{pred} = T_{ext} + (T - T_{ext}) \cdot \alpha + \frac{a}{b}(1-\alpha) \cdot u
$$

The cap solves the maximum command that keeps the predicted temperature below `target - LANDING_SAFETY_MARGIN_C`. It is applied after normal PI computation and before soft constraints, so `u_pi` remains the raw PI diagnostic while `landing_u_cap` explains the final command reduction.

In normal mode, the `setpoint` diagnostic block publishes only:

- `filtered_setpoint`,
- `trajectory_active`,
- `trajectory_source`,
- `landing_active`,
- `landing_reason`,
- `landing_u_cap`,
- `landing_coast_required`.

In debug mode, it also exposes the trajectory details:

- `trajectory_start_sp`,
- `trajectory_target_sp`,
- `trajectory_tau_ref`,
- `trajectory_elapsed_s`,
- `trajectory_phase`,
- `trajectory_pending_target_change_braking`,
- `trajectory_braking_needed`,
- `trajectory_model_ready`,
- `trajectory_remaining_cycle_min`,
- `trajectory_next_cycle_u_ref`,
- `trajectory_bumpless_u_delta`,
- `trajectory_bumpless_ready`,
- `landing_sp_for_p_cap`,
- `landing_predicted_temperature`,
- `landing_predicted_rise`,
- `landing_target_margin`,
- `landing_release_allowed`,
- `landing_u_cmd_before_cap`,
- `landing_u_cmd_after_cap`.

The setpoint reference shaping remains limited to the P branch so the integral path keeps the raw setpoint untouched and learning is not disturbed. The landing cap is a separate post-PI command governor for HEAT setpoint trajectories; it does not rewrite the integral and does not change the valve linearization curve.

The current code also applies an explicit guard on positive integral growth during catch-up phases:

- after a significant setpoint change,
- after resuming from `hvac_off`,
- after resuming from detected window opening,
- after resuming from power shedding,
- during a disturbance-recovery trajectory.

This guard does not block integral discharge. It is released only when two conditions are met:

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

As long as the guard stays active, `TRAJECTORY_I_RUN_SCALE` may limit positive integral growth during the trajectory. Once the guard is released, that attenuation is no longer applied: the I branch returns to its normal dynamics to correct the static residual.

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

The restart therefore keeps only useful persistent PI memory, without restoring transient servo states that are no longer physically valid outside their original runtime session.

### 4.5 Deadband, near-band, and protections

`DeadbandManager` handles:

- a symmetric deadband with absolute hysteresis (`DEADBAND_HYSTERESIS = 0.025`),
- an asymmetric near-band in heating mode,
- automatic near-band sizing when `deadtime_heat` and the model are usable,
- fallback to configured thresholds otherwise.

Additional protections currently present are:

- `SmartPIGuards`: `guard_cut` and `guard_kick`,
- tracking anti-windup,
- positive-integral guard during resumes and catch-up phases,
- explicit `I:HOLD` during the post-resume `deadtime_heat` phase for `window_resume` and `power_shedding_resume` in heating mode,
- thermal guard on setpoint decrease,
- deadband hold logic.

### 4.6 Auto-calibration

`AutoCalibTrigger` supervises the algorithm outside the control law.

Implemented behavior:

- initial snapshot when `tau`, `deadtime_heat`, and `deadtime_cool` are reliable,
- fallback snapshot after `AUTOCALIB_DT_COOL_FALLBACK_DAYS = 7` days without reliable `deadtime_cool`,
- rolling snapshot every `AUTOCALIB_SNAPSHOT_PERIOD_H = 120` hours,
- hourly check loop,
- cooldown guard `AUTOCALIB_COOLDOWN_H = 24` hours,
- stagnation thresholds `AUTOCALIB_A_MAD_THRESHOLD = 0.25` and `AUTOCALIB_B_MAD_THRESHOLD = 0.30`,
- positive exit only after at least `AUTOCALIB_EXIT_NEW_OBS_MIN = 1` new observation on both `a` and `b`, plus coherent dead times,
- up to `AUTOCALIB_MAX_RETRIES = 3` attempts,
- planned retry delay `AUTOCALIB_RETRY_DELAY_H = 6` hours.

The forced cycle is managed by `CalibrationManager`:

- `COOL_DOWN` down to `sp - 0.3°C`,
- `HEAT_UP`,
- `COOL_DOWN_FINAL` back to `sp`.

---

## 5. Software architecture

### 5.1 Orchestrators

| File                      | Class            | Role                                                                     |
| ------------------------- | ---------------- | ------------------------------------------------------------------------ |
| `prop_algo_smartpi.py`    | `SmartPI`        | algorithm facade, full orchestration                                     |
| `prop_handler_smartpi.py` | `SmartPIHandler` | Home Assistant bridge: persistence, periodic timer, services, attributes |

### 5.2 `smartpi/` modules effectively used

| Module                 | Main role                                                   |
| ---------------------- | ----------------------------------------------------------- |
| `const.py`             | constants, enums, governance matrix                         |
| `learning.py`          | slope estimation, `ABEstimator`, `DeadTimeEstimator`        |
| `ab_aggregator.py`     | median or weighted-median aggregation of `a/b` measurements |
| `ab_drift.py`          | persistent drift detection and recentering for `a/b`        |
| `learning_window.py`   | learning-window management and guardrails                   |
| `gains.py`             | gain calculation and freezing                               |
| `controller.py`        | discrete PI, anti-windup, hold, hysteresis                  |
| `deadband.py`          | deadband and near-band                                      |
| `setpoint.py`          | analytical setpoint trajectory and HEAT landing cap         |
| `feedforward.py`       | `u_ff1/u_ff2/u_ff3` orchestration                           |
| `ff_trim.py`           | slow feed-forward bias                                      |
| `ff_ab_confidence.py`  | confidence policy for `a/b`                                 |
| `ff3.py`               | bounded FF3 predictive correction                           |
| `governance.py`        | freeze decisions                                            |
| `calibration.py`       | forced-calibration FSM                                      |
| `autocalib.py`         | supervision and automatic triggering                        |
| `guards.py`            | `guard_cut` and `guard_kick`                                |
| `thermal_twin_1r1c.py` | thermal twin and predictive diagnostics                     |
| `diagnostics.py`       | published payload and debug payload                         |
| `tint_filter.py`       | adaptive indoor-temperature filtering                       |
| `timestamp_utils.py`   | monotonic / wall-clock conversion                           |

### 5.3 Internal composition of `SmartPI`

Components instantiated by `SmartPI.__init__()` notably include:

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name, enabled=use_setpoint_filter)
self.ctl = SmartPIController(name)
self.est = ABEstimator(mode=aggregation_mode)
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.calibration_mgr = CalibrationManager(name)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.twin = ThermalTwin1R1C(dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1)
self.guards = SmartPIGuards()
self.autocalib = AutoCalibTrigger(name)
self._ff_trim = FFTrim()
self._ab_confidence = ABConfidence()
```

### 5.4 Current persistence

The current payload from `SmartPI.save_state()` notably contains:

```python
{
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
    "ff_v2_trim": {...},
    "tint_filter_state": {...},
}
```

Loading can also read `ac_state`, but that block is not currently emitted again by `save_state()`.

### 5.5 Published diagnostics

`diagnostics.py` exposes three levels:

- `build_diagnostics()`: compact or full version depending on `debug_mode`,
- `build_published_diagnostics()`: structured summary for `specific_states.smart_pi`,
- `build_debug_diagnostics()`: published summary plus the `debug` sub-block.

In the debug diagnostics, `learn_ok_count` and `learn_skip_count` are runtime counters relative to the current Home Assistant process. Persisted model counters remain internal and are used for model quality and warm-up decisions.

When the thermal twin is usable, a `pred` sub-block is added to debug diagnostics.

---

## 6. Important constants

### 6.1 Learning

| Constant                       | Value  |
| ------------------------------ | ------ |
| `AB_HISTORY_SIZE`              | `31`   |
| `AB_MIN_SAMPLES_B`             | `8`    |
| `AB_MIN_SAMPLES_A`             | `6`    |
| `AB_MIN_SAMPLES_A_CONVERGED`   | `6`    |
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
| `AW_TRACK_TAU_S`              | `120.0` |
| `SMARTPI_RECALC_INTERVAL_SEC` | `60`    |
| `LEARNING_PAUSE_RESUME_MIN`   | `20`    |

### 6.3 Bands and setpoint shaping

| Constant                   | Value   |
| -------------------------- | ------- |
| `DEFAULT_DEADBAND_C`       | `0.05`  |
| `DEADBAND_HYSTERESIS`      | `0.025` |
| `DEFAULT_NEAR_BAND_DEG`    | `0.40`  |
| `DEFAULT_KP_NEAR_FACTOR`   | `1.0`   |
| `DEFAULT_KI_NEAR_FACTOR`   | `1.0`   |
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
| `AB_BAD_PERSIST_CYCLES`           | `3`    |
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

---

## 8. References

1. **Astrom K.J. and Hagglund T.**, work on PID regulation and FOPDT model usage.
2. **Sundaresan K.R. and Krishnaswamy P.R.**, estimation of delay and time-constant parameters.
3. **OLS** regression with Student t-test for slope validation.
