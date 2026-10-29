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

- `AB_MIN_SAMPLES_B = 5`,
- `AB_MIN_SAMPLES_A = 5`,
- `AB_A_SOFT_GATE_MIN_B = 5`.

SmartPI leaves `HYSTERESIS` once `b` has 5 measurements and `a` is published.
`a` is published at 5 measurements, or from `AB_MIN_SAMPLES_A_CONSISTENT = 4`
measurements when they agree: their median absolute deviation is at most
`AB_A_CONSISTENT_MAX_REL_MAD = 0.10` of their median. The first publication of
`a` also waits for the delivered power model (3.7): while the response lag is not
resolved, measurements are collected without publication. Both buffers continue
filling up to `AB_HISTORY_SIZE = 31` during normal SmartPI regulation.

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
- delays the window start until the response transient has decayed
  (`skip: response transient (a|b)`): once the response lag is published (3.7),
  an `a` window starts at least `min(3 lag, half the median active episode)` after
  the power step. A `b` window starts after the power cut at least the longer of
  `3 lag` (published lag) and `AB_SETTLE_K_PASSIVE_DEADTIME = 1.5` reliable
  passive dead times: the emitter keeps heating after a cut, and since the room
  only starts to cool once this residual heat falls below the losses, the passive
  dead time bounds its decay even when the lag is unknown. In the bootstrap and
  the calibration only the `3 lag` term applies,
- does not submit an active window shorter than one PWM cycle
  (`active_window_shorter_than_cycle`): its duration is set before its data, since
  stopping as soon as a short window looks significant favours the steepest noisy
  slopes. In the bootstrap the heater runs at full power between the hysteresis
  thresholds and a heating phase may be shorter than one cycle: the minimum is
  then capped at half the median heating phase (`AB_SETTLE_ACTIVE_CAP_FRAC`),
  measured on the previous phases,
- closes the window at its last observation when two updates are not contiguous
  (heating guard cut, interval capped at one cycle): the window is submitted on
  the data up to that instant when its slope is robust there, otherwise abandoned
  (`skip: observation gap`); it never spans the unobserved period,
- monitors power stability through the coefficient of variation (`U_CV_MAX`),
  evaluated once the window mean power reaches `U_CV_MIN_MEAN = 0.05` (below
  it, power returning in an OFF window is not detected by this check),
- extends the window while the slope is not yet considered robust,
- gives up the slope after `DT_MAX_MIN = 240` minutes if no robust signal emerges.

In practice, the decision to submit or extend the window depends on slope robustness, not on a fixed minimum duration. A window closed without a robust slope is not lost: its integral balance feeds the pools described in 3.5.

### 3.3 Robust slope estimation

`ABEstimator.robust_dTdt_per_min()` applies slope guardrails:

- minimum amplitude: `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- minimum number of level changes: `OLS_MIN_JUMPS = 3`,
- statistical significance: `OLS_T_MIN = 2.5`,
- minimum span: once the slope is significant, the adjusted variation over the
  window (`|slope| × duration`) must cover at least `OLS_MIN_SPAN_STEPS = 1.5` sensor
  steps; the step is the smallest change observed in the window, with a floor
  `OLS_SPAN_MIN_STEP_C = 0.05°C` that acts as a noise margin for fine-resolution
  sensors. Otherwise the window is extended (`span_below_resolution`).

Since the slope is tested at each update, a window that is too short relative to
quantization would be submitted as soon as its slope looks significant: with a
0.1 °C sensor switching between two levels and a slowly cooling room, the sampling
triggered by publications then dominates the fit and `b` can be off by ±50 %. On a
window covering several steps, the slope is unbiased whatever the sensor profile.

Slopes are computed on the raw measured indoor temperature, not on the filtered signals, because the robustness guard counts distinct value jumps of the measurement. The raw history (and the filtered history used by the dead-time detection) keeps the samples of the last `TIN_HISTORY_RETENTION_MIN = 300` minutes (`DT_MAX_MIN` + 60) and at most `TIN_HISTORY_MAX_SAMPLES = 1800` samples. A sample is added on each value change or after 60 s, so the full duration is covered while samples arrive on average at most every 10 s; beyond that rate the sample cap shortens the covered duration (about 150 minutes at one changing sample every 5 s) and the slope of a long window uses only its most recent samples. Validated slopes are then fed into `ABEstimator.learn()`, which:

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

For an active window, `u` is the mean delivered power of the window (3.7), not
the applied command.

Each `a` measurement keeps its observation (`dT/dt`, `T_int - T_ext`, `u`). Each
time a new value of `b` is published, all retained measurements are recomputed
with that `b`, then `a` is aggregated again if it has already been published: an
`a` measurement made before `b` was accurate therefore follows its corrections. A
measurement restored without an observation, or whose recomputation would
contradict the mode sign, keeps its value.

### 3.5 Integral balance of windows without a robust slope

With a coarse sensor (0.2 °C steps) or one that reports rarely, a heating or cooling phase rarely contains the three value jumps required by the OLS slope. The model integrated between two instants nevertheless carries the information without a significant slope:

$$ T(t_2) - T(t_1) = a \cdot U - b \cdot D, \quad U = \int u\,dt, \quad D = \int (T_{int} - T_{ext})\,dt $$

`ABWindowBalance` (`ab_window_balance.py`) tracks each accepted window:

- it records, at each raw temperature step, the cumulative integrals of `u`, of `T_int - T_ext` and of `T_ext`;
- when a window closes through a guard, a timeout or the absence of a robust slope, it keeps the segment between the first and the last step: `b` if the mean power is below `U_OFF_MAX`, `a` if it exceeds `U_ON_MIN`;
- a window submitted through the OLS slope is never reused, and an external reset (resume, learning disabled) discards it;
- a slope timeout does not close the segment, which continues into the next window until a guard.

A segment is kept only if:

- all steps of the window go in the direction of the response (otherwise the sensor is noisy at step scale and the window remains the responsibility of the OLS),
- it lasts at least `AB_BALANCE_MIN_SEGMENT_MIN = 15` minutes,
- the power did not change during collection: collection stops at the first tick that deviates from the mean power, so that the emitter's inertia after a cut-off is not attributed to the previous power,
- for `b`, the inside/outside gradient and outside margin guards of the passive window are respected.

Both endpoints are steps in the same direction. A sensor that reports on change then crosses the same quantization threshold, and the measured variation is exact; a periodically sampled sensor provides values to within half a step. The uncertainty model keeps half a step per endpoint:

$$ \sigma_{rel} = \frac{q \sqrt{n / 6}}{|N|} $$

with `q` the smallest observed step (bounded below by `AB_BALANCE_MIN_RESOLUTION_C = 0.05`, `AB_BALANCE_DEFAULT_RESOLUTION_C = 0.1` until a step has been seen), `n` the number of segments and `N` the numerator (`ΔT + b·D` for `a`, `ΔT` for `b`).

Segments are accumulated per parameter (at most `AB_BALANCE_POOL_MAX_SEGMENTS = 12`, aged at most 24 h, emptied on HVAC mode change). As soon as `σ_rel <= AB_BALANCE_MAX_REL_UNC = 0.25`, the pool is submitted to `ABEstimator.learn()` as a single measurement, with the equivalent slope, power and indoor/outdoor difference (averages over the cumulated duration); median/MAD aggregation, drift detection and the soft gate apply as is. The `a` pool waits until `b` has `AB_A_SOFT_GATE_MIN_B` measurements. The threshold is stricter than that of the OLS slope (`t >= 2.5` accepts a relative error up to 0.40).

### 3.6 Dead-time estimation

`DeadTimeEstimator` implements an independent FSM. Its channels describe the
physical room response, not the power-transition direction:

- `deadtime_heat`: delay before temperature starts rising,
- `deadtime_cool`: delay before temperature starts falling.

Therefore OFF -> ON learns `heat` in HEAT mode but `cool` in COOL mode. The
ON -> OFF transition learns the opposite passive response.

Detection tracks the filtered indoor temperature. A detection in progress is not resumed after a restart, and the minimum OFF duration must be observed again in the new session before a detection can be armed.

A response is detected once the temperature has moved by `0.05 °C` from its trough (heat) or peak (cool) since the power transition. The dead time runs from the transition to the last instant within `0.01 °C` of that trough or peak, tracked during the wait, so it does not depend on the retention of the temperature history. The wait gives up after `DEADTIME_ACTIVE_TIMEOUT_S = 14400 s` for an active response (power started) and `DEADTIME_PASSIVE_TIMEOUT_S = 43200 s` for a passive response (power cut): after a cut, an emitter with a large stored heat (floor heating) keeps warming the room, then plateaus, and the drop can come several hours later. In the bootstrap the passive wait ends by a detection or a power restart, which needs a drop the detection sees first; the limit only bounds how late a drop is still attributed to the cut. A power transition aborts a pending wait. The outcome of the last wait per response and the age of the wait in progress are published (`analysis.deadtime`).

Reliability becomes true as soon as at least one valid measurement has been captured on the corresponding channel. Each channel keeps its last 6 detections; the published value is their median, so a single aberrant detection does not move it (with one or two detections the median equals their mean). On restart, the published value is recomputed from the restored detections.

### 3.7 Response lag and delivered power

At the scale of a learning window, the room behaves as an integrator behind
fast lags (actuator, emitter, air mixing, sensor housing) and a pure delay
(integrating first-order plus dead time response). After a power step, the
slope settles progressively and its ramp asymptote crosses the extrapolated
passive trajectory at $t = T_{ar} = L + \sum \tau_i$, the mean residence time of
the response (Åström and Hägglund). A window that starts $k \cdot T_{ar}$ after the
step has a slope deficit of at most $e^{-k}$ for a single lag, whatever the pure
delay share. A window within the transient underestimates `a`, all the more as
the lags are long or the sensor reports late.

`ResponseLagEstimator` (`response_lag.py`) measures $T_{ar}$ on active episodes
at constant power (deviation at most `RESPONSE_LAG_U_TOL = 0.05`):

- the episode must follow a passive period of at least
  `max(RESPONSE_LAG_MIN_OFF_S = 1800 s, RESPONSE_LAG_MIN_OFF_LAGS = 5 × known lag)`,
  so that the power of the previous episode has decayed;
- the passive baseline is the robust OLS slope of the end of that OFF period (at
  most `RESPONSE_LAG_PASSIVE_SPAN_S = 3600 s`), otherwise the published `b`;
- the late line is fitted on the level crossings of the raw history (at least
  `RESPONSE_LAG_MIN_CROSSINGS = 3`). For a jump of one sensor step, the crossing is
  dated at the midpoint between the two samples; a jump of several steps keeps the
  raw value. This removes the staircase and trigger-threshold biases of sensors
  that report on change;
- the intercept of an OLS line fitted on a transient segment falls before
  $T_{ar}$: the estimator inverts the closed-form intercept of a single-lag
  response over the fitted interval (monotonic in the lag). An episode is not
  used when the inversion is too flat (`RESPONSE_LAG_MIN_INVERSION_SLOPE = 0.05`)
  or when the intercept reaches `RESPONSE_LAG_RESOLVABLE_FRAC = 0.9` of its
  limit for an infinite lag;
- each estimate carries its standard deviation, propagated from the fit
  residuals and from an assumed relative uncertainty of `b` of
  `RESPONSE_LAG_B_REL_UNC = 0.10`.

The last `RESPONSE_LAG_HISTORY = 7` estimates are kept. The lag is published
when at least `RESPONSE_LAG_MIN_ESTIMATES = 3` estimates give a combined standard
deviation of their inverse-variance weighted median at most
`RESPONSE_LAG_MAX_REL_UNC = 0.15` of that median (floor
`RESPONSE_LAG_UNC_FLOOR_S = 30 s`); estimates more than three standard deviations
away from the median are not combined. An inconsistent set keeps the published
value, so dispersed estimates do not switch the delivered power model on and off. A discontinuity of more than 1 s between two updates ends the
episode at the last observed instant, and the power cut is dated at the resume.

The intercept fixes the mean delay of the response, not its shape. The
delivered power model splits it into a pure delay
$L = \min(\text{active dead time}, T_{ar})$, then a first-order lag of
$T_{ar} - L$. The dead time is detected after the response has started, so it
bounds the pure delay from above: the model never delivers power earlier than a
pure delay with the same intercept. Without a published lag or a reliable dead
time, the delivered power equals the applied power. For an active window starting
$t_0$ after the step and lasting $W$, the mean delivered fraction is, with
$\tau = T_{ar} - L$:

$$ \eta = \frac{1}{W}\int_{t_0}^{t_0+W} \left(1 - e^{-(t-L)/\tau}\right)\mathbb{1}_{t>L}\,dt $$

The model is resolved once the lag is published, or after
`RESPONSE_LAG_RESOLVE_EPISODES = 7` active episodes without a publication
(the applied power is then used). Until then, `a` measurements are collected
without publication. When the published lag appears or changes by more than
`RESPONSE_LAG_MODEL_CHANGE_REL = 0.5` before the first publication of `a`, the
collected measurements are recomputed with the new delivered power. Only the
windows that the settling gate admits are recomputed; the others are dropped,
since their correction depends on the shape of the response.

The state is published in `analysis.learning.response_lag` (`lag_s`, `estimates`,
`episode_median_s`, `last_reason`) and persisted with the learning state.

The lag is learned on constant-power episodes, which the hysteresis bootstrap
provides. In PWM regulation the duty cycle changes from cycle to cycle, so an
installation already out of the bootstrap rarely provides one.

---

## 4. Smart-PI control

### 4.1 PI gains

`GainScheduler.calculate()` applies the following logic:

1. if `tau`, the sign of `a`, or the active dead time is unreliable, fall
   back to `KP_SAFE = 0.55` and `KI_SAFE = 0.010`,
2. otherwise compute IMC/SIMC from the active-gain magnitude:

$$ K_p = \frac{1}{|a| \cdot (\lambda + L)} $$

3. compute the SIMC integral time (Skogestad, 2003)
   $T_i = \min(\tau, 4(\lambda + L))$ (`GAIN_TI_LAMBDA_FACTOR = 4`), then
   $K_i = K_p / T_i$,
4. clamp the gains ($K_p \in [0.10, 5.0]$, $K_i \in [0.001, 0.050]$) and apply governance freezes.

For a room, $\tau$ (several hours to several days) far exceeds the closed-loop
horizon $\lambda + L$: the room is close to an integrator and $T_i = 4(\lambda + L)$.
With $T_i = \tau$, $K_i$ would fall on the floor `KI_MIN = 0.001` and the integral
would take days to correct a model error. The near-band taper of $K_i$ is
clamped to the same floor.

The effective integral time $T_i = K_p / K_i$ of the applied gains is published
in minutes as `control.ti_min` (`None` when $K_i \le 0$).

On every change of the applied $K_i$, the integral is rescaled by
$K_{i,old} / K_{i,new}$, so the integral power $K_i \cdot I$ is unchanged (bumpless
transfer). The rescale applies to any change, however small: the near-band taper
moves $K_i$ by small steps, and a threshold would let $K_i \cdot I$ drift during holds.

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
ready (`a` not published with fewer than 5 measurements, or fewer than 5 `b`
measurements), `tau` is not reliable, or the sign
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

This guard does not block integral discharge. An integral that already points
against the requested recovery usually carries the model bias (feed-forward
error), which still holds at the new setpoint: the guard keeps it while the
recovery progresses. Once the recovery stalls, with the release error still
beyond its threshold and the signed recovery slope at or below the release
slope for `INTEGRAL_GUARD_STALL_CYCLES = 3` cycles, the guard allows that
opposing memory to unwind as far as zero. It still blocks any new accumulation
beyond zero in the protected direction. A negative integral inherited from the
previous setpoint therefore cannot hold the room below a new, higher setpoint,
and a setpoint step does not erase the bias that keeps the room from
overshooting at the new setpoint.

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

- `SmartPIGuards` (heating mode only, outside the bootstrap and the calibration): `guard_cut` forces the output to 0 when the measured temperature exceeds `setpoint + near_band_above` while the heater is active, until it falls back to that threshold minus the larger of `NEAR_BAND_HYSTERESIS_C = 0.05°C` and `GUARD_CUT_RESET_STEPS = 1.5` observed sensor steps (a reading flickering between two levels around the threshold would otherwise re-trigger the cut, and restart the cycle, at every measurement). During a cut the output stays at 0 and the integral is frozen (`I:OVERRIDE(guard_cut)`), but the temperature filter, the dead time detector and the published temperature keep following the room; `guard_kick` restarts the cycle when, inside the near-band, the temperature falls below `setpoint - near_band_below` while the command is below 99 %. In cooling mode neither guard acts and the near-band is not auto-sized. In the bootstrap the heating runs between its own hysteresis thresholds and a running guard is released,
- per-tick anti-windup on the applicable upper limit (see below),
- signed integral guard during resumes, catch-up phases, and setpoint changes,
- explicit `I:HOLD` during the post-resume `deadtime_heat` phase for `window_resume` and `power_shedding_resume` in heating mode,
- `I:HOLD` with source `vt_safety` while Versatile Thermostat safety mode applies its default power instead of the SmartPI command,
- `I:HOLD` with source `deadband_hysteresis_shell` while the hysteretic deadband state is active and the error is outside the core deadband,
- `I:HOLD` with source `governance_sensor_invalid` while no outdoor temperature has been received yet (`DEGRADED` regime),
- freeze of P and I inside the core deadband.

The anti-windup acts at every calculation, in the linear demand space, on the applicable upper limit: `1`, or the linear image of `max_on_percent` when it is lower. It has two parts:

- conditional integration: the integral stops growing when the unconstrained request exceeds the applicable upper limit. Within `NEAR_SETPOINT_UNWIND_STEPS = 1.5` observed sensor steps of the setpoint, an output saturated against the error still lets the integral unwind toward zero, without crossing it (`I:UNWIND(SAT_HI|SAT_LO)`), provided the output without the proportional term (`u_ff + Ki·I`) stays within its limits: with a quantized sensor, a reading one step away from the setpoint saturates the output through the proportional term alone, and a blocked integral combined with the deadband freeze would hold a steady offset. An output held at its limit by `u_ff + Ki·I` (for example at `max_on_percent`) keeps the integral, which then carries the model bias;
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

Before and during each phase the manager checks, with the thermal twin, that the threshold of the phase can be reached. The measured temperature comes first: once it has reached the threshold of the phase, the phase moves on and the twin is not consulted. A passive phase converges to `T_steady_passive`; an active phase driven at the allowed power `u_max` (`max_on_percent` in linear space) converges to `T_steady_passive + u_max·(T_steady_max − T_steady_passive)`, the 1R1C equilibrium being linear in power (`T_steady_max` when `u_max` is 1 or the passive equilibrium is not valid); the threshold is reachable when that equilibrium lies beyond it by `CALIBRATION_REACHABILITY_MARGIN_C = 0.5°C` (HEAT: passive `COOL_DOWN` and `COOL_DOWN_FINAL`, active `HEAT_UP`; COOL: active `COOL_DOWN` and `COOL_DOWN_FINAL`, passive `HEAT_UP`). The check only applies while the twin is usable (`status` ok, `model_reliable`, past its warm-up, valid equilibrium, and `cooling_model_available` in COOL). When the threshold is not reachable the cycle ends at once with the result `Unreachable`, distinct from `Cancelled` (HVAC OFF, timeout of `CALIBRATION_TIMEOUT_MIN = 600` minutes, reset): `last_calibration_time` is not set, `AutoCalibTrigger` consumes no retry and publishes `smartpi_autocalib_unreachable`, and the next attempt waits `AUTOCALIB_COOLDOWN_H` hours (`autocalib.next_check_ts`). The result is not a published diagnostic: `calibration.state` carries the phase only.

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
| `deadtime_estimator.py` | `DeadTimeEstimator`, active-episode starts and dead-time window |
| `ab_aggregator.py`     | median or weighted-median aggregation of `a/b` measurements |
| `ab_drift.py`          | persistent drift detection and recentering for `a/b`, with one candidate sequence per channel |
| `learning_window.py`   | learning-window management and guardrails                   |
| `ab_window_balance.py` | integral balance of windows without a robust slope and `a/b` pools |
| `response_lag.py`      | `ResponseLagEstimator`: response lag, settling gates and delivered power model |
| `learning_outcomes.py` | bounded counters of learning-window outcomes                |
| `gains.py`             | gain calculation and freezing                               |
| `controller.py`        | discrete PI, anti-windup, hold, hysteresis                  |
| `deadband.py`          | deadband and near-band                                      |
| `setpoint.py`          | analytical setpoint trajectory for the P reference          |
| `integral_guard.py`    | directional integral guard and the I:HOLD after a window or power-shedding resume |
| `feedforward.py`       | `u_ff1/u_ff2/u_ff3` orchestration, signed FF gain `b/a` and warmup scale |
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
self.resume_hold = ResumeDeadtimeHold()
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

The integral-balance pools and the window-outcome counters are not persisted:
they start empty at each startup. The observations of the `a` measurements are
persisted with the estimator state; a state that does not contain them loads with
measurements that cannot be recomputed.

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
opening commits nothing and opens an unobserved-power interval
(`missing_valve_position`) that lasts until the next observed power (valve
update, cycle start or cycle end). The trace keeps the last known power over
that interval, so a trim window overlapping it is rejected
(`causal_power_not_covered`); a short scheduler gap filled with the previous
power (`legacy_gap_fill`) stays usable.

### 5.5 Published diagnostics

`diagnostics.py` exposes the algorithm and canonical live views:

- `SmartPI.get_published_diagnostics()`: returns the canonical `live` payload for the dedicated diagnostic sensor,
- `build_published_diagnostics()`: the function that builds that payload.

`diagnostic_history.py` wraps this payload in `schema_version`, `live` and the
stable eight-series `history` block. Normal sensor entities exclude `live` from
Recorder; the debug sensor subclass records it directly. The debug option only selects the sensor class that is recorded; it does not
add a second diagnostic block.

`analysis.learning.learn_ok_count` and `learn_skip_count` are runtime counters
relative to the current Home Assistant process. Persisted model counters remain
internal and are used for model quality and warm-up decisions.
`analysis.learning.window_outcomes_24h` lists, for `a` and for `b`, the eight
most frequent window-closure reasons of the last 24 hours as `[reason, count]`
pairs (numeric values removed from the reasons; pool events are prefixed with
`pool:`). `analysis.learning.balance_pool` gives the number of segments waiting
in each pool and the sensor resolution used for their uncertainty; these values
are specific to the current Home Assistant session. When the thermal twin is
usable, its public diagnostic fields are exposed under `analysis.twin`.

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
| `AB_MIN_SAMPLES_B`             | `5`    |
| `AB_MIN_SAMPLES_A`             | `5`    |
| `AB_MIN_SAMPLES_A_CONSISTENT`  | `4`    |
| `AB_A_CONSISTENT_MAX_REL_MAD`  | `0.10` |
| `AB_A_SOFT_GATE_MIN_B`         | `5`    |
| `AB_CONFIDENCE_MIN_SAMPLES_A`  | `11`   |
| `AB_CONFIDENCE_MIN_SAMPLES_B`  | `11`   |
| `AB_B_CONVERGENCE_MIN_SAMPLES` | `11`   |
| `AB_B_CONVERGENCE_MIN_BHIST`   | `5`    |
| `AB_B_CONVERGENCE_MAD_RATIO`   | `0.30` |
| `AB_B_CONVERGENCE_RANGE_RATIO` | `0.10` |
| `DT_DERIVATIVE_MIN_ABS`        | `0.05` |
| `OLS_MIN_JUMPS`                | `3`    |
| `OLS_MIN_SPAN_STEPS`           | `1.5`  |
| `OLS_SPAN_MIN_STEP_C`          | `0.05` |
| `OLS_T_MIN`                    | `2.5`  |
| `DT_MAX_MIN`                   | `240`  |
| `U_OFF_MAX`                    | `0.05` |
| `U_ON_MIN`                     | `0.20` |
| `AB_BALANCE_MAX_REL_UNC`       | `0.25` |
| `AB_BALANCE_MIN_SEGMENT_MIN`   | `15`   |
| `AB_BALANCE_MIN_RESOLUTION_C`  | `0.05` |
| `AB_BALANCE_DEFAULT_RESOLUTION_C` | `0.1` |
| `AB_BALANCE_POOL_MAX_SEGMENTS` | `12`   |
| `RESPONSE_LAG_HISTORY`         | `7`    |
| `RESPONSE_LAG_MIN_ESTIMATES`   | `3`    |
| `RESPONSE_LAG_RESOLVE_EPISODES` | `7`   |
| `RESPONSE_LAG_MAX_REL_UNC`     | `0.15` |
| `RESPONSE_LAG_UNC_FLOOR_S`     | `30`   |
| `RESPONSE_LAG_MIN_OFF_S`       | `1800` |
| `RESPONSE_LAG_MIN_OFF_LAGS`    | `5`    |
| `RESPONSE_LAG_MIN_CROSSINGS`   | `3`    |
| `RESPONSE_LAG_MODEL_CHANGE_REL` | `0.5` |
| `AB_SETTLE_K_ACTIVE`           | `3`    |
| `AB_SETTLE_K_PASSIVE`          | `3`    |
| `AB_SETTLE_ACTIVE_CAP_FRAC`    | `0.5`  |

### 6.2 Regulation

| Constant                      | Value   |
| ----------------------------- | ------- |
| `KP_SAFE`                     | `0.55`  |
| `KI_SAFE`                     | `0.010` |
| `GAIN_TI_LAMBDA_FACTOR`       | `4.0`   |
| `NEAR_SETPOINT_UNWIND_STEPS`  | `1.5`   |
| `INTEGRAL_GUARD_STALL_CYCLES` | `3`     |
| `GUARD_CUT_RESET_STEPS`       | `1.5`   |
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
