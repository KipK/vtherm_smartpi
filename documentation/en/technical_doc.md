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

- `u_ff1`: structural feed-forward (holding power), derived from $b/a$ or from the published room model,
- `u_pi`: discrete PI term.

The value injected into the controller is `u_ff1 + u_pi`, then it goes through:

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
`a` also waits for the delivered power model (3.7): while the emitter lag is not
resolved, measurements are collected without publication, until the collection
holds `AB_HISTORY_SIZE = 31` measurements: the next accepted measurement then
publishes `a`. Both buffers continue
filling up to `AB_HISTORY_SIZE = 31` during normal SmartPI regulation. After a
restart, the restored emitter lag state sets the phase from the first
calculation; a state saved without an emitter lag block waits for the first
learning update.

Inside the hysteresis band, the output keeps its previous on/off state. A
modulated output left by another phase is not a hysteresis state: it is set to 0
until the next threshold. The on/off state is persisted with the wall-clock time
of the last calculation. After a bridged restart (5.4), the first calculation
that has the indoor temperature resumes it inside the band, as an uninterrupted
hysteresis would have; while it is awaited, the on/off state of before the stop
is kept as described in Restarts. Outside the
band, the thresholds decide. After any other restart, and for a state saved
without it, the output starts at 0. A heating or cooling phase in progress can
thus resume after a short restart.

The heating phases of the hysteresis are measurement events: they are recorded
and read as a whole, and when the bootstrap ends the readings give the learning
starting values for `a` and `b` (3.7, Measurement events and starting values).
The hysteresis command, the order above and the exit rule are the same with or
without them.

This order is that of the bootstrap start. With the direct start (option
`smart_pi_learning_start`, 3.7, Direct start) the stage `HYSTERESIS` is the
same learning stage, but the command is the PI and the stage waits for `b` until
a starting value of `b` stands or the learning's own time constant has been
reliable. The option acts on a blank start only: a state with measurements,
starting values, pulses or readings keeps the start its learning began with (the
direct start is saved as `learning_start`; a state without it began with the
bootstrap), and a learning reset takes the configured start.

The `b` convergence used for drift-aware `a` learning relies on `b_converged_for_a()`:

1. `learn_ok_count_b >= 11`,
2. `len(_b_hat_hist) >= 5`,
3. `MAD(b_hat)/Med(b_hat) <= 0.30`,
4. `range(last_5_b_hat)/Med(b_hat) <= 0.10`.

### 3.2 Effective learning window

The code no longer uses a `WINDOW_MIN_MINUTES` constant. Learning now relies on `LearningWindowManager` and on a sliding window that:

- requires outdoor temperature to be available,
- obeys thermal governance,
- rejects or resets an ON/`a` window on the exact observation where the user
  setpoint changes, so a window cannot span the old and new regimes,
- keeps early active ascent after that observation eligible for `a` under the
  existing dead-time, power-stability, robust-slope, and governance guards,
- keeps passive OFF/`b` learning under its existing physical guards;
  disturbance-only responses are not blocked by this setpoint gate,
- blocks bootstrap collection until the required dead times are reliable; in the
  learning stage of the direct start a `b` window needs instead the confirmed peak
  of the stop (3.7, Direct start) or a known emitter lag (published, or kept by
  the readings of the pulses), with which it starts `3 T_ar` after the cut,
- applies a post-resume pause (`LEARNING_PAUSE_RESUME_MIN = 20`),
- anchors the window start after the dead-time end when needed,
- delays the window start until the response transient has decayed
  (`skip: response transient (a|b)`): once the emitter lag is published (3.7),
  an `a` window starts at least `min(3 T_ar, half the median active episode)` after
  the power step. A `b` window starts after the power cut at least the longer of
  `3 T_ar` (published emitter lag) and `AB_SETTLE_K_PASSIVE_DEADTIME = 1.5` reliable
  passive dead times: the emitter keeps heating after a cut, and since the room
  only starts to cool once this residual heat falls below the losses, the passive
  dead time bounds its decay even when the emitter lag is unknown. In the bootstrap
  only the `3 T_ar` term applies,
- does not submit an active window shorter than one PWM cycle
  (`active_window_shorter_than_cycle`): its duration is set before its data, since
  stopping as soon as a short window looks significant favours the steepest noisy
  slopes. In the bootstrap the heater runs at full power between the hysteresis
  thresholds and a heating phase may be shorter than one cycle: the minimum is
  then capped at half the median heating phase (`AB_SETTLE_ACTIVE_CAP_FRAC`),
  measured on the previous phases,
- closes the window at its last observation when two updates are not contiguous
  (interval capped at one cycle): the window is submitted on
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

- `deadtime_heat`: dead time before the temperature starts rising,
- `deadtime_cool`: dead time before the temperature starts falling.

Therefore OFF -> ON learns `heat` in HEAT mode but `cool` in COOL mode. The
ON -> OFF transition learns the opposite passive response.

Detection tracks the filtered indoor temperature, in regulation as during the bootstrap and the measurement pulses: a raw measurement that oscillates by one sensor step would otherwise detect a response as soon as the power steps. A detection in progress resumes after a bridged restart (5.4): its state, start, trough or peak, pre-start trend, last power and last power cut are persisted as wall-clock times, and its timeout still runs from the saved start. After any other restart it is dropped, and the minimum OFF duration must be observed again in the new session before a detection can be armed.

A response is detected once the temperature has moved by `0.05 °C` from its trough (heat) or peak (cool) since the power transition. The dead time runs from the transition to the last instant within `0.01 °C` of that trough or peak, tracked during the wait, so it does not depend on the retention of the temperature history. The wait gives up after `DEADTIME_ACTIVE_TIMEOUT_S = 14400 s` for an active response (power started) and `DEADTIME_PASSIVE_TIMEOUT_S = 43200 s` for a passive response (power cut): after a cut, an emitter with a large stored heat (floor heating) keeps warming the room, then plateaus, and the drop can come several hours later. In the bootstrap the passive wait ends by a detection or a power restart, which needs a drop the detection sees first; the limit only bounds how late a drop is still attributed to the cut. A power transition aborts a pending wait. The outcome of the last wait per response and the age of the wait in progress are published (`analysis.deadtime`).

The response to a power start is the deviation of the temperature from the course it followed before the start. Without heating, a room is flat or cools down: the trough tracking above then dates the response. A room that already warms with the heater off (occupants, sun, a neighbouring room) moves away from the start level by itself, and the trough tracking would date the response too early (in COOL, the same holds for a room that already cools before the cooling starts). So, at an active start (power on), `deadtime_trend.prestart_trend()` fits a line on the raw temperature of the OFF period before the start:

- window: the last `DEADTIME_PRESTART_WINDOW_S = 1800 s` before the start, beginning no earlier than the last power cut plus the passive dead time (the emitter still acts on the room before that);
- with less than `DEADTIME_PRESTART_MIN_SPAN_S = 900 s` or fewer than `DEADTIME_PRESTART_MIN_SAMPLES = 4` samples, no trend is removed;
- a trend whose fitted change stays within one sensor step (the smallest change observed in the window, at least `DEADTIME_PRESTART_STEP_FLOOR_C = 0.01 °C`), or that goes against the response, is not removed either: the detection is the trough (or peak) tracking above, unchanged. This is the usual case in the bootstrap, where the heating restarts because the room has cooled down;
- a trend in the direction of the response is fitted on the whole window and on its recent half (at least `DEADTIME_PRESTART_MIN_HALF_SAMPLES = 3` samples per half). The detection is rejected if the trend is not a line: a residual rms above half the detection threshold (and above one sensor step), or a slope that grows towards the response from the first half to the second by more than the detection threshold per half window. A line would then under-estimate the trend at the start;
- otherwise the detector follows the temperature minus the extrapolated trend, with the steepest slope of the two fits: removing too much only delays the detection. The trough, the `0.05 °C` threshold and the `0.01 °C` lookback apply to that difference;
- at the detection, the measurement is rejected if the trend alone accounts for at least the detection threshold since the start: the response is then not separable from the trend.

A rejected measurement publishes the outcome `rejected` and leaves the detections unchanged: the dead time keeps its previous value. A passive response (power cut) is not concerned: the trend before a cut is that of the heating itself.

A measurement that would lower the published dead time is also ignored (outcome `ignored`, the dead time keeps its value) when the evidence is weak: the trend before the start is clearly in the direction of the response (a fitted change of at least `DEADTIME_PRESTART_RISE_MIN_STEPS = 2` sensor steps over the window, even if it was removed rather than rejected), or the window is too short to evaluate any trend. A room that warms before the start can only make the response look earlier, so it never shortens the dead time. A measurement that equals or lengthens the published value follows the rules above, and so does any measurement before the first published value.

Reliability becomes true as soon as at least one valid measurement has been captured on the corresponding channel. Each channel keeps its last 6 detections; the published value is their median, so a single aberrant detection does not move it (with one or two detections the median equals their mean). On restart, the published value is recomputed from the restored detections.

### 3.7 Emitter lag and delivered power

At the scale of a learning window, the room behaves as an integrator behind
fast inertias (actuator, emitter, air mixing, sensor housing) and a pure dead
time (integrating first-order plus dead time response). After a power step, the
slope settles progressively and its ramp asymptote crosses the extrapolated
passive trajectory at $t = T_{ar} = L + \sum \tau_i$, the mean residence time of
the response (Åström and Hägglund), the emitter lag. A window that starts $k \cdot T_{ar}$ after the
step has a slope deficit of at most $e^{-k}$ for a single inertia, whatever the pure
dead time share. A window within the transient underestimates `a`, all the more as
the emitter lag is long or the sensor reports late.

`ResponseLagEstimator` (`response_lag.py`) measures the emitter lag $T_{ar}$ on active episodes
at constant power (deviation at most `RESPONSE_LAG_U_TOL = 0.05`):

- the episode must follow a passive period of at least
  `max(RESPONSE_LAG_MIN_OFF_S = 1800 s, RESPONSE_LAG_MIN_OFF_LAGS = 5 × known emitter lag)`,
  so that the power of the previous episode has decayed;
- the passive baseline is the robust OLS slope of the end of that OFF period (at
  most `RESPONSE_LAG_PASSIVE_SPAN_S = 3600 s`), otherwise the published `b`;
- the late line is fitted on the level crossings of the raw history (at least
  `RESPONSE_LAG_MIN_CROSSINGS = 3`). For a jump of one sensor step, the crossing is
  dated at the midpoint between the two samples; a jump of several steps keeps the
  raw value. This removes the staircase and trigger-threshold biases of sensors
  that report on change;
- the intercept of an OLS line fitted on a transient segment falls before
  $T_{ar}$: the estimator inverts the closed-form intercept of a single-inertia
  response over the fitted interval (monotonic in $T_{ar}$). An episode is not
  used when the inversion is too flat (`RESPONSE_LAG_MIN_INVERSION_SLOPE = 0.05`)
  or when the intercept reaches `RESPONSE_LAG_RESOLVABLE_FRAC = 0.9` of its
  limit for an infinite inertia. An estimate above `RESPONSE_LAG_MAX_S`
  (`DT_MAX_MIN` × 60 = 14,400 s) is also discarded (`intercept not resolvable`);
- each estimate carries its standard deviation, propagated from the fit
  residuals and from an assumed relative uncertainty of `b` of
  `RESPONSE_LAG_B_REL_UNC = 0.10`.

The last `RESPONSE_LAG_HISTORY = 7` estimates are kept. The emitter lag is published
when at least `RESPONSE_LAG_MIN_ESTIMATES = 3` estimates give a combined standard
deviation of their inverse-variance weighted median at most
`RESPONSE_LAG_MAX_REL_UNC = 0.15` of that median (floor
`RESPONSE_LAG_UNC_FLOOR_S = 30 s`); estimates more than three standard deviations
away from the median are not combined. An inconsistent set keeps the published
value, so dispersed estimates do not switch the delivered power model on and off. A discontinuity of more than 1 s between two updates ends the
episode at the last observed instant, and the power cut is dated at the resume.

A bridged restart (5.4) resumes the passive period or the active episode in
progress at the save. The context is persisted as wall-clock times (last observed
instant, then start of the passive period, or start, power, constancy, preceding
passive time and outdoor temperature of the episode) and converted back to the
monotonic clock on load. The heater kept the committed power over the stop, and
the first update after the restart spans the stop with that power: a passive
period goes on and the stop counts as passive; an episode goes on, and the first
observation continues or ends it like any other. After any other restart the
passive period is dropped and an episode ends without an estimate
(`episode interrupted by restart`); a nonzero power then opens a new
episode without a known passive period. The crossings come from the raw history
of the dead time detector, persisted the same way; it holds no sample during the
stop. A state saved without this context resumes nothing.

The inversion assumes a single-inertia (first-order) response. It recovers the mean
residence time only for such an emitter, or when the episode lasts several times
the emitter lag. On an emitter that combines a pure dead time and an inertia, or two
successive inertias, the intercept of a short episode falls later than that of a
single inertia with the same residence time: the published emitter lag is then overestimated,
by about 1.3 times on the simulated shapes, or is never published when the episode
stays in the transient regime.

The intercept fixes the emitter lag, not the shape of the response. The
delivered power model splits it into a pure dead time
$L = \min(\text{active dead time}, T_{ar})$, then a first-order inertia of
$T_{ar} - L$. The dead time is detected after the response has started, so it
bounds the pure dead time from above: the model never delivers power earlier than a
pure dead time with the same intercept. Without a published emitter lag or a reliable dead
time, the delivered power equals the applied power. For an active window starting
$t_0$ after the step and lasting $W$, the mean delivered fraction is, with
$\tau = T_{ar} - L$:

$$ \eta = \frac{1}{W}\int_{t_0}^{t_0+W} \left(1 - e^{-(t-L)/\tau}\right)\mathbb{1}_{t>L}\,dt $$

The model is resolved once the emitter lag is published, or after
`RESPONSE_LAG_RESOLVE_EPISODES = 4` heating phases without a publication
(the applied power is then used). A heating phase groups the active episodes
separated by power cuts shorter than `RESPONSE_LAG_PHASE_MIN_OFF_S = 600 s`: an
episode that starts less than 600 s after the end of the previous one does not
count as a new phase. Until then, `a` measurements are collected
without publication. When the published emitter lag appears or changes by more than
`RESPONSE_LAG_MODEL_CHANGE_REL = 0.5` before the first publication of `a`, the
collected measurements are recomputed with the new delivered power. Only the
windows that the settling gate admits are recomputed; the others are dropped,
since their correction depends on the shape of the response.

The state is published in `analysis.learning.response_lag` (`lag_s`, `estimates`,
`episodes`, `episodes_target`, `episode_median_s`, `last_reason`) and persisted
with the learning state. `episodes` counts the heating phases ended since the
start of the learning, also after the emitter lag is published; `episodes_target` is
`RESPONSE_LAG_RESOLVE_EPISODES`. When both collections have reached their count
and `a` waits for this model, `learning.bootstrap_status` reads
`step2 - waiting response lag: estimates N, heating phases K/4`.

The emitter lag is learned on constant-power episodes, which the hysteresis bootstrap
provides. In PWM regulation the duty cycle changes from cycle to cycle, so an
installation already out of the bootstrap rarely provides one.

#### Heating balance

`HeatingBalance` (`heating_balance.py`) measures, on each heating phase followed
by a stop, the heating rate `a` (°C/min at full power) and the emitter lag from
an energy balance of the room. It is published; the regulation reads only the
median of the last emitter lags it measured, as the horizon of the anticipation (see
section 4.3). It works in heating mode only, from the heating events of the
measurement event detector (3.7, Measurement events and starting values).

A phase starts at the first interval with a power above 1 % and ends after its
cut. Heating runs separated by less than `HEATING_BALANCE_PHASE_MIN_OFF_S`
(10 minutes) without heating belong to the same phase. Before the phase, the
rest (heater off) must last at least `HEATING_BALANCE_MIN_REST_S = 1800 s`. A
baseline, the least-squares line of the temperature samples over the rest tail
(at most `HEATING_BALANCE_BASELINE_SPAN_S = 3600 s`, at least 4 samples),
stands for the passive trajectory: $T_0(t) = T_0 + s\,(t - t_{start})$. During
the phase the estimator accumulates the full-power time $t_{on}$ (the sum of
power × interval) and the area $A$ of $T - T_0(t)$ in °C·min.

After the cut it looks for the temperature peak: the samples are grouped in
consecutive bins of `HEATING_BALANCE_PEAK_BIN_S = 300 s` from the cut, and the
earliest bin with the strictly highest mean is the peak, confirmed when a closed
bin lies at least `HEATING_BALANCE_PEAK_CONFIRM_S = 1800 s` after it. The phase ends without
a measurement when the heating restarts while the best bin is the last one
holding samples, or when `HEATING_BALANCE_PEAK_MAX_S = 4 h` elapse without a
confirmed peak. With $\Delta_p = T_{peak} - T_0(t_{peak})$, $A_p$ the area at
the peak, $b$ the published loss rate (0 when unknown) and $t_{on}$ in minutes:

$$a = \frac{\Delta_p + b\,A_p}{t_{on}}$$

The measurement is kept when $t_{on} \ge$ `HEATING_BALANCE_MIN_ON_S = 600 s` and
$\Delta_p \ge$ `HEATING_BALANCE_MIN_RISE_C = 0.3 °C`. The last
`HEATING_BALANCE_HISTORY = 7` measurements are kept; the published `a` and emitter lag
are their medians.

The emitter lags are also kept in a history of their own, of the same length: a
measurement that gives no emitter lag (power not constant, not resolved) removes
none of them, unlike the retained measurements. The median of that history is
the kept emitter lag, published as `kept_lag_s` (`kept_lags` is their number). A saved
state that holds a published emitter lag but no kept one keeps it as the single
kept value.

For a phase at constant power (its mean power $u_0 = t_{on}/D$ is at least
`1 - RESPONSE_LAG_U_TOL` times its highest interval power), the heat still
missing at the cut gives the emitter lag. With
$\Delta_c = T_{cut} - T_0(t_{cut})$ ($T_{cut}$ is the mean of the samples within
±150 s of the cut), $A_c$ the area at the cut and $D$ the phase duration, the
delivery deficit is $\delta = \left(t_{on} - 60\,(\Delta_c + b\,A_c)/a\right)/u_0$
in seconds. A first-order emitter of time constant $\tau$ gives
$\delta = \tau\,(1 - e^{-D/\tau})$, solved for $\tau$ by bisection; the emitter lag is 0
when $\delta \le 0$ and unknown when $\delta \ge 0.98\,D$.

On such a phase the heat the emitter still holds at the peak is counted as
well. At the peak the temperature is flat, so the emitter still gives what the
room loses: $p = \max(0,\ b\,\Delta_p - 60\,s)$ in °C/min, with $s$ in °C/s.
That power is taken to arrive over one emitter lag, which adds $p\,\tau/60$ to
the heat of the phase:

$$a(\tau) = \frac{\Delta_p + b\,A_p + p\,\tau/60}{t_{on}}$$

The heating rate and the emitter lag are solved together: starting from
$\tau = 0$, the deficit computed with $a(\tau)$ gives the next $\tau$, until two
successive values differ by less than 1 s, in at most 60 steps. The emitter lag
is unknown when the iteration does not settle. The measurement keeps $a(\tau)$
as its heating rate. Without that term the emitter lag is read short in a room
whose walls absorb heat, where the peak comes while the emitter is still warm.

The state is published in `analysis.learning.heating_balance` and persisted with
the learning state. The measurements are restored after a restart; the phase in
progress resumes after a bridged restart and is dropped otherwise. The kept emitter lags
are restored too.

#### Measurement events and starting values

One detector (`measure_event.py`, `MeasureEventDetector`) watches the power
applied to the emitter and the room temperature, in heating and in cooling. An
event is a rest, a phase of power above 1 % (cuts shorter than
`HEATING_BALANCE_PHASE_MIN_OFF_S` belong to the phase) and a cut, then the
search of the temperature peak. The peak search (`peak_search.py`) is the one
described for the heating balance: 300 s bins, the earliest strictly highest
mean (the lowest in cooling), confirmed by a closed bin 1800 s later, abandoned
after 4 hours. Each event carries the source that drove the command when its
phase began: measurement pulse, bootstrap hysteresis, pulse of a calibration or
regulation. The heating balance reads the heating events whatever their source;
the reader below reads those of the bootstrap and of the pulses.

Until values are kept, during the learning stage, a calibration or a reading, a bounded record (`MeasureRecord`) keeps
the applied power, the room temperature and the outdoor temperature as change
points: at most 6000 points per channel and about 30.5 hours. For a switch, the
power channel holds the real on and off edges of each cycle the scheduler
applied (from the applied-command receipts); for a valve, or a scheduler
without receipts, it holds the cycle fraction. The record is saved.

A closed event of the bootstrap or of a measurement pulse opens a reading (`event_reading.py`). Its window
starts 1800 s before the heating step and first ends when the event closes, so
it holds the samples that the confirmation of the peak waited for. A fit
(`event_fit.py`) then explains the recorded room temperature with the whole
chain: the emitter gives the room the applied power delayed by a pure delay $D$
and filtered by a first-order lag $\tau$, and the room follows
$dT/dt = a\,q - b\,(T - T_{out})$. It finds $D$, $\tau$ and $b$ by search and
$a$ by least squares; the emitter lag of the reading is $D + \tau$. The fit
runs in the background, outside the regulation: it is one of the jobs of the
single background loop that also runs the emitter lag learning and the room
model, one job at a time, and the next calculation applies its result. After
each fit the window is extended to six lags of the result, up to 8 fits, and the
reading is complete when the window covers 90 % of that. numpy and scipy are
loaded when the first fit runs.

Two complete readings of the same mode agree when their `a` and their emitter
lag each differ by at most 20 % of the larger. The means of two agreeing readings
are kept for their mode, saved, and dropped by a reset of the learning. The kept
values are per mode, like the `a` channel of the estimator: a period in the other
mode starts without kept values and runs its own readings and pulses. A stop of
the thermostat, a change of mode or the start of a calibration ends the readings in progress.

**Starting values.** The kept `a` of each mode is given once to the learning of
`a` and `b` as a starting value, and the kept `b` (a property of the room,
shared by the modes) with the first hand-over only: with the direct start at the
keep, with the bootstrap start when the bootstrap ends. They stand for the
learning's own values and write no measurement history: `a` stops standing at
the first publication of `a` by the learning itself, and `b` when the learning's
own time constant becomes reliable. A change of HVAC mode sets the standing `a`
aside with its channel and brings it back with it. The values of the first
hand-over earn 5 measures of feed-forward warm-up credit (4.2) for each value
given, kept when it is released; a later hand-over of the other mode earns none.
With the bootstrap start, the emitter lag of the readings is published with the
values and does not set the horizon of the anticipation nor the emitter lag of
the constant-power phases.

**Direct start.** The PI is the command from the first calculation, on the
fallback gains. Up to `PULSE_MAX_COUNT = 3` measurement pulses per mode
(`measure_pulse.py`) replace the PI command for a short sequence, while the
learning stage lasts and the readings have kept no values in the mode. A pulse
starts when the room is at the setpoint or at most `PULSE_START_UNDER_C = 0.3 °C`
on the demand side, the previous command is under `PULSE_START_MAX_COMMAND = 0.8`
and `PULSE_MIN_GAP_S = 6 h` have passed since the previous pulse. It rests (at
most `PULSE_REST_MAX_S = 45 min`, or until the room is `PULSE_REST_UNDER_C =
0.5 °C` on the demand side), heats at full power until the dead-time estimator
has detected the response, it stands three sensor steps out and the room is back
within `PULSE_HEAT_BACK_C = 0.1 °C` of the demand side of the setpoint, the last
condition only while the step has lasted less than `PULSE_HEAT_EXTEND_MAX_S =
30 min` (at most `PULSE_HEAT_MAX_S = 2 h`, or as soon as the room is
`PULSE_HEAT_OVER_C = 0.3 °C` on the other side), then stops until the temperature peak is confirmed (at most
`PULSE_STOP_MAX_S = 4 h`; the PI takes over when the room is `PULSE_TAKEOVER_C =
0.5 °C` on the demand side and one sensor step lower than at the start of the stop,
and in any case at `PULSE_TAKEOVER_CAP_C = 0.8 °C`). A pulse interrupted by a restart, a stop, a change
of setpoint or of mode is not resumed and counts. The readings of
the events the pulses cause give the kept values. Until the learning publishes
its own model they stand for it: `pre_model_gains` gives the gains from the kept
`a` and the active dead time (the IMC/SIMC rule of a slow room, neither gain
above its fallback value), the kept emitter lag is the horizon of the
anticipation (source `measurement pulses`) and the emitter lag of the
response-lag estimator, which counts as resolved with it. The integral term
starts from zero at the calculation at which the learning stage ends. The
dead-time estimator reads the dead time after a stop on the temperature peak,
with the peak search of the measurement events and an earlier confirmation: the
peak is also confirmed at the close of a bin later than the best one whose mean
lies under the best mean by at least the largest of `STOP_PEAK_DROP_MIN_C =
0.05 °C`, `STOP_PEAK_DROP_STEPS = 2` sensor steps (the smallest change of the
last hour, at least 0.01 °C) and `STOP_PEAK_DROP_RISE_FRAC = 0.5` times the rise
from the cut to the best bin; when the heating starts again, the bin in progress
is closed and gets the same test, otherwise the stop gives no value. The 1800 s
rule is the latest confirmation. Only stops after a cycle at full power feed
the published value.

**Calibration.** On request (`force_smartpi_calibration`), once the first model
is published, a calibration (`calibration.py`) starts measurement pulses in
either start mode, with its own count (`PULSE_MAX_COUNT = 3` pulses at most);
they do not count for the learning stage. `Calibration.request` answers every
request without raising and publishes the answer (`last_request`): refused in
the learning stage, with the learning paused, with the thermostat off by the user
(the last calculation saw HVAC mode OFF without the window reason), and less than
`CALIBRATION_MIN_INTERVAL_S = 24 h` after the end of the last calibration that
was not cancelled; ignored while one is in progress; a window, a guard cut or a
power shedding defers the pulses only. The pulses of a calibration have their own
limits, computed at each calculation by `pulse_limits` from the reliable dead
times of the mode (`La` active, `Lp` passive, 0 when not reliable), the
anticipation horizon in use `H` and the least-squares fall rate `s` of the last
`CALIBRATION_FALL_WINDOW_S = 900 s` of the raw temperature on the demand side
(floored at 0); the static limits are frozen at the start of the pulse:

| Step | Pulse of a calibration |
|---|---|
| start | demand in [-`CALIBRATION_START_OVER_C` = -0.2 °C, `PULSE_START_UNDER_C` = 0.3 °C], previous command < 0.8, `clamp(2 H, 1 h, 6 h)` after the end of the previous pulse |
| rest | ends at `min(Lp + CALIBRATION_REST_EXTRA_S, 4 h)` (`CALIBRATION_REST_EXTRA_S` = 900 s of free-running window + 300 s), or when `demand + s La >= PULSE_REST_UNDER_C` = 0.5 °C |
| heat | rise seen and (`demand <= PULSE_HEAT_BACK_C` = 0.1 °C or step >= `PULSE_HEAT_EXTEND_MAX_S` = 30 min), 0.3 °C over, or `clamp(2 (La + H), 2 h, 4 h)` |
| stop | event closed and stop response not pending; takeover at `demand >= 0.5 °C` once the room is one sensor step lower than at the stop start, or at `demand >= CALIBRATION_DEPTH_CAP_C` = 0.8 °C; 4 h |

The pulses of the learning stage keep the constants of `measure_pulse.py`. A
pulse is applied as an on/off command: committed at once, then the dead-time
estimator is fed with the committed power, once per calculation; its samples
join the median of the dead times as any other. The phase is `Calibration` while
a pulse of the calibration commands the output; between pulses the regulation
runs in the stable phase. The events of the calibration pulses (source
`calibration`) are read like the others but form a set of their own, tested
among themselves only: two agreeing readings (`a` and lag within 20 %) give the
reading of the calibration (`a`, `b`, emitter lag, and whether the two `b` agree
within 20 %). At the end on that reading, `LearningStart._correct` compares each
quantity with the value in force in the mode of the calibration (`|a|`, `b`, the
anticipation horizon in use) and corrects it when `|reading - in force| >
CALIBRATION_CORRECT_REL * in force` (0.30):

- `a`: `ABEstimator.correct_a` resets the channel of the mode and stands the
  reading as a starting value (`StartingValues.correct`), released at the first
  own publication of `a`;
- `b`: read and published only (`CALIBRATION_CORRECT_B = False`: the `b` of a
  pulse reading is biased in a room with heavy walls). With the switch on and the
  two `b` agreeing, `ABEstimator.correct_b` clears the `b` channel (measures,
  published history, counter, drift, `own_b`) and stands the reading, released
  when the own time constant is reliable again; the `a` measures are replayed
  with it;
- lag (direct start only): kept in `calibration.lag_s` by mode and given to the
  response-lag estimator in place of the kept lag of the start at each
  calculation; it never enters the anticipation horizon.

With `CALIBRATION_SKIP_WHEN_OWN_AGREE = True` (off by default), `a` or `b` is not
corrected while its drift buffer is empty and its last
`CALIBRATION_OWN_RECENT = 3` measures are within the threshold of the value in
force. A correction latches the feed-forward credit at the ramp count before it
(`StartingValues.latch`) and notes the learned gain `b / a` before it in the
holding transfer (`HoldingTransfer.note_learned_step`): at the next feed-forward
the step of the learned holding power, evaluated at the same setpoint, outdoor
temperature and warmup, is moved into the integral. A change of `Kp` that follows
a correction is not moved into the integral: the proportional term acts on the error as
usual, and a change of `Ki` only rescales the integral (`SmartPIController.transfer_gains`). A bootstrap start whose `b`
stands from a correction does not return to its learning stage. The heating
balance and the response-lag estimator observe the pulses as any heating phase.
A calibration ends when two of its readings agree (`readings agree`), when its
pulses are done and their readings have ended (`pulses done`), or
`CALIBRATION_MAX_S = 48 h` after its first calculation (`time limit`); its
readings in progress then end. When that end comes during a calibration pulse,
the pulse ends first: in its rest it hands back at once, in its heating step it
goes to its stop step (`MeasurePulse.stop_heating`, reason `calibration ended`),
in its stop step it goes on; the calibration ends at the calculation where the
pulse ends, before the PI computes, and the corrections apply there. The end is
also checked on a calculation cut by the guard
(`LearningStart.follow_calibration`). While a calibration is in progress
(request to end) `ABEstimator.learn` takes no `a` measure: a window with
`u > U_ON_MIN` is skipped (`skip: calibration in progress`); `b` windows, the
dead times, the heating balance and the response lag go on. After each
calibration pulse (`LearningStart.recovering`) the integral is held (`I:HOLD`,
source `calibration_recovery`) until the room reaches the setpoint; the
pulses of the learning stage do not hold it. A stop of the thermostat ends
the pulse in progress, which counts, and the calibration goes on; a change of
HVAC mode, a return of the learning stage, a learning reset or a pause of the
learning cancels it (`cancelled`) and ends its pulse at once. The request, the pulses done, the mode, the
start, the last request, the values in force, the corrections and the corrected
lag are saved (`calibration`); corrected starting values are saved with
`corrected` in `starting_values` and load even when nothing was handed over; a
state saved with the former calibration blocks (`cal_state`, `ac_state`) loads
without a calibration in progress.

The record, the readings in progress, the kept values and the pulses started are
saved (`measure_events`, `event_reading`, `measure_pulse` and `learning_start`,
and `starting_values` inside `est_state`); a state saved without them loads.
After a bridged restart the readings resume (a fit in flight is queued again);
otherwise the readings in progress end and the record is dropped.

### 3.8 Quiet periods of the emitter lag learning

The emitter lag learning (`smartpi/tar_learning/`) and the room model select their data with
`smartpi/quiet_periods/`, a pure Python module shared by the methods that need
a room free of solar gains. No local hour enters the selection; times are epoch
seconds and half-open intervals `[start, end)`.

**Darkness.** The position comes from the Home Assistant configuration
(`hass.config.latitude`, `hass.config.longitude`), injected by the handler and
validated by the manager: finite numbers within [-90, 90] and [-180, 180]
(zero is valid). The geometric elevation of the centre of the sun is computed
with the `astral` package shipped with Home Assistant. Darkness is the centre
below `-0.833 - 0.5 = -1.333` degrees: -0.833 degrees is the standard sunrise
and sunset (the upper limb on the apparent horizon, 34' of refraction plus 16'
of radius), and 0.5 degrees covers the usual spread of the refraction near the
horizon. The elevation is sampled every 600 s on a grid aligned on the epoch; a
sample counts as dark only 0.05 degrees below the threshold (the elevation
departs from a chord by at most 0.014 degrees on a 600 s step near the horizon),
each crossing is refined by bisection to 1 s, then every boundary is moved
120 s inwards for the accuracy of the solar algorithm. The darkness therefore
ends a few minutes before sunrise and starts a few minutes after sunset, never
later than sunrise. A darkness longer than 24 h (polar night) is cut into blocks
of 24 h anchored on its true start; a polar day has no darkness. Without a valid
position (or without `astral`) there is no darkness and no period: the methods
stay silent and `quiet_periods.reason` says why.

**Periods.** A plateau is a maximal interval of constant setpoint. A setback
run goes from a setpoint drop to the next rise (further drops stay in the run).
A rise to a setpoint still below the one before the drop, with the room
strictly above the new setpoint at the rise, calls for no heat: it does not end
the run, and its plateau is a further piece of it.
A darkness holds at most one episode: the longest part of a setback run inside
it, or the whole darkness when the setpoint does not change inside it (held
setpoint). The episode ends at the rise that ends the run, or at the end of the
darkness. Its quiet components are its plateau pieces intersected with the
valid spans of the history (`TarHistory.valid_spans`: everything but the
invalid intervals); a bridged restart leaves no invalid interval and does not
cut them. The selected component is the longest one (at least 1 h, the earliest
on a tie).

**Regulated start.** For a component, the first heater start at or after the
start of its plateau with the indoor temperature within 0.3 °C of the setpoint;
when it lies before the component, the component start if the data between them
are valid. A heater start within 60 s after a restart does not count. None when
the room never regulates there.

**Identification interval.** The selected component, continued after the rise
that ends the episode while it is still dark, the new setpoint constant and the
data valid, for at most 2 h (the response horizon of the comfort-step fit). The
response to the rise is the main excitation of the emitter: without it the
emitter time constant of a regulated room is poorly determined. The regulated
night and the comfort step still end at the rise.

**Keys.** An episode is closed once its end is at or before the last
observation, settled once its identification interval is known too, and
complete when the history covers it from its start. Its key is
`q<version>:<origin>:<start of the selected component>` at 0.1 s, the origin
being the start of the episode. The manager rebuilds the catalogue at most every
10 minutes and stores the settled and complete episodes (60 at most) with their
components: the stored catalogue only grows forward, so a key never changes
when the history is pruned, and a period found again gets the same key.

**Uses.**

| Rule | Data |
|---|---|
| Comfort step | a rise of at least 1 °C after a low setpoint held 2 h (in the history), to a setpoint not below the last indoor temperature, not within 300 s after a restart; evaluated when it ends an episode and the component that ends at the rise covers the 2 h before it |
| Fit window of a step | every response window ends at the end of the darkness; the excitation rules (`exc_window_T`, energy, rise, sensor period) judge the truncated window; less than 600 s left gives no fit. A window `Wsp` cut by the darkness is a censored observation: it gives an estimate only when it lasts at least `TAR_CUT_ESTIMATE_WINDOW_T = 3` times the largest of the fitted value $T$ and the floor (the refined value in force, the published fast value, the lower bound in force and twice the bootstrap dead time: the first moment of a first-order emitter is about twice the apparent dead time the bootstrap measures). Otherwise, once the other rules pass, it gives the lower bound `TAR_CUT_BOUND_FACTOR` $\cdot T = 0.6\,T$ (the margin covers the noise of a short fit), confirmed by the same gate as a value; a cut window that fails another rule is refused as cut |
| Night indicator | passive spans intersected with the quiet components of the episode of the step, on the 600 s grid of the segment |
| G1 / GT4 | longest span without a heater start in `[regulated start, t_ref)`; no regulated start, or a regulated part under 1.5 h, is an unknown observable and leaves the step unconfirmed |
| Regulated night | `[regulated start, end of the episode)` of the selected component, at least 4 h, valid to its end; then the existing quarter, temperature, power-slope and residual rules |
| Identification window | the identification intervals of three consecutive settled episodes (bins wholly inside them), the wall state starting 10 h before the first; compared with the window of the three episodes before, never overlapping |
| Room class, fast value | one night entry and one confirmed value per key, replaced before the room class or the publication is computed |
| Room model | the same windows of three settled episodes and their identification intervals, minus the periods the tracking found disturbed (§4.2); the disturbance of the tracking is summed per key |

Evaluations carry their decision time (end of their last episode); the class,
wall parameters and `τe` in force at a step are those decided before the step.
The local date of the diagnostics (`day`) is for display only.

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
is not advanced by the near-band taper.

The active dead time is `deadtime_heat` in HEAT and `deadtime_cool` in COOL.

With the emitter lag learning, the HEAT dead time $L$ of the gain rule comes
from `tar_learning/policy.applied_value`. The learned emitter lag $T$ is the refined
value when published, else the fast value, raised to the lower bound in force
(the median of the last 5 confirmed bounds) when that is larger. It is an emitter lag (a first
moment), about twice the apparent dead time that the bootstrap measures, so the
rule works on $L_T = T/2$ (`TAR_GAIN_DEADTIME_FACTOR = 0.5`) against the
reference $L_b = \max(L_{b,0}, L_{boot})$, with $L_{b,0}$ the first reliable
bootstrap dead time seen since the learning started (persisted, never lowered)
and $L_{boot}$ the current one:

| Case | $L$ of the gains | Other consumers |
|---|---|---|
| no learned value | $L_{boot}$ | $L_{boot}$ |
| $L_T < L_b$ | $L_b$ | $L_{boot}$ |
| $L_b \le L_T \le 2 L_b$ (`TAR_APPLY_LONG_FACTOR`) | $L_T$ | $T$ |
| $L_T > 2 L_b$, fast value and window value agree, $T$ not a lower bound | $L_T$ | $T$ |
| $L_T > 2 L_b$ otherwise | $2 L_b$ | $L_{boot}$ |

The window value is the refined value when published, else the `τe` of the
latest evaluation whose two windows agree (a diagnostic when its nights are not
regulated). The two agree when $|\ln(T_{fast}/T_{window})| \le \ln 1.25$
(`TAR_AGREE_TOL`). An $L$ that is too short leaves the recommended robustness
range of the loop, and much too short makes a slow emitter unstable; an $L$
that is too long costs integrated error, never stability. A learned value
alone therefore never shortens $L$ below $L_b$, and only a value confirmed by
two methods lengthens it beyond $2 L_b$. The other consumers (near band,
integral hold) use $T$ only when the gains use $L_T$.

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

The cycle count is kept across restarts. The restored integral only holds the
share of the load that the feed-forward did not cover, so the feed-forward
comes back at the level it had before the restart, without a bump.

The number of valid learning episodes is the larger of the accepted measures
and the credit of the starting values (3.7, Measurement events and starting
values): each starting value given counts for 5 measures and keeps that credit
when it is released, so the ramp never drops.

$k_{ff}$ is zero, and therefore $u_{ff1}$ is zero, whenever the model is not
ready (`a` not published with fewer than 5 measurements, or fewer than 5 `b`
measurements), `tau` is not reliable, or the sign
of `a` does not match the active mode.

$T_{ext}$ is the last valid outdoor temperature received by SmartPI. When the
outdoor sensor reports no value, FF1, the setpoint model and the near band keep
using that last value, without age limit. Until a first outdoor temperature has
been received, $u_{ff1}$ is zero (`ff_reason` `ff_no_ext_temp`).

#### Holding power from the room model

In HEAT with heaters driven by PWM cycles, a published room model replaces
$k_{ff} = b/a$ (`smartpi/room_model/feedforward.py`) once a static model of
the energy balance is in force: the slope $s_{EB}$
(`room_model.energy_balance_loss_per_c`) and the constant free gain $g_{EB}$
(`room_model.energy_balance_free_gain_percent` / 100), with the static part
$u_s = s_{EB}(SP - T_{ext}) - g_{EB}$. With $\delta_{wall}/a$ the wall extra
power of the tracking (`room_model.wall_extra_power_percent`, both signs):

| Room model | Source (`feedforward.source`) | $u_{ff1}$ |
|---|---|---|
| walls identified, tracking `ok` | `room model (air and walls)` | $clamp(u_s + \frac{\delta_{wall}}{a}, 0, 1)$ |
| walls identified, tracking not `ok` (paused, settling, drift, ...) | `room model (static part)` | $clamp(u_s, 0, 1)$ |
| simple model | `room model (simple)` | $clamp(u_s, 0, 1)$ |
| published, no static model in force yet, or one whose $s_{EB}$ differs from the published $b_{eq}/a$ by more than 5 % | `learned model` | the learned law above, unchanged, no wall part |
| nothing published, COOL, valve mode | `learned model` | the learned law above, unchanged |

The static part comes from the energy balance, never from the fitted
$b_{eq}/a$. With heavy walls the fitted $b_{eq} = b_{air} - b_{wall}$ is a
small difference of two larger terms, and it moves with the emitter response
time used by the fit (by a fraction of a percent per second of emitter time);
the multi-day energy balance does not depend on it. The fit gives the wall
dynamics, $\delta_{wall}/a$. The static model alone, without a published
model, is never used: without the wall part it gives the wrong holding power
in heavy rooms, low after a setback and high when the walls give heat back.
The free gain is part of the static part: without it the integral would carry
$g_{EB}$ for good.

**Energy balance.** A job of its own computes, once per local day 20 minutes
after its midnight and whatever the quiet periods, the balance of each whole
local day of the emitter lag history before the day (at most 7; 10 min bins, invalid
intervals excluded). Midnight is only the boundary of these civil days (23 h
and 25 h days at the clock changes); it selects no period:

$$ u_d = \frac{\sum u \, \Delta t - \Delta T_{in} / a}{\sum \Delta t}, \qquad D_d = \overline{T_{in} - T_{ext}} $$

with $u$ the applied heat-equivalent power and $\Delta T_{in}$ the change of
the indoor temperature over each valid segment (air storage removed with the
online `a`). A day is used when at most 20 % of it is invalid, its mean duty
is at least 0.02 and $D_d > 0$. Weighted least squares (weight: the valid
share of the day) fit

$$ u_d = s_{EB} \, D_d - g_{EB} + e_d $$

and the slope alone $u_d = s_0 D_d$. Whole days keep the wall storage of a
setpoint or outdoor change a small share of each point. The verdict uses
Student quantiles $t_{0.975}$ on the residual scatter; each standard error is at
least its jackknife value (one day left out at a time), so that an estimate
resting on a single day far from the others is not kept:

- at least 4 usable days, a mean duty of at least 0.05 and a mean $D$ of at
  least 6 °C;
- the form with the gain needs a weighted standard deviation of $D_d$ of at
  least 1 °C (below that, the day-to-day differences of $D$ come from the
  regulation itself and the slope would read them as a loss), a 95 %
  half-width $t_{0.975, n-2} \, \sigma_s / s_{EB} \le 5$ % (the slope is the
  reference of a 5 % agreement), $0 < s_{EB} \le 3$, and a gain that leaves a
  need in $]0, 1]$ on every day used with $|g_{EB}| \le s_{EB} \overline{D}$;
- with that spread, a form with the gain not determined is refused
  (`free heat gains detected; ...` when $g$ is significant and plausible,
  else `the heating need varies too much ...`): the ratio of the means would
  carry what the line does not explain;
- with less spread, the slope alone ($g_{EB} = 0$) with
  $t_{0.975, n-1} \, \sigma_{s_0} / s_0 \le 5$ %; a gain that the days cannot
  show stays in $s_0 = s_{EB} - g/\overline{D}$, right at the usual $D$ only;
- the qualified form must not drift: a linear trend of $u_d$ with the day,
  fitted with the model, is refused when it is significant (95 %) and moves
  the need by more than 2.5 % of its mean across the days (wall storage after
  a change; half the tolerance, since the storage left after the days is not
  in the trend).

A refused day keeps the previous qualified model
(`room_model.energy_balance_refusal_reason` says why); the model in force,
its day and its number of days are saved with the room model.

**Identification windows.** The room model reads the catalogue of quiet
periods of the emitter lag learning (§3.8) through `TarLearningManager.quiet_periods`;
`room_model/periods.py` is its adapter. A window is three consecutive closed
periods of the catalogue, each fitted on its identification interval (the
quiet component, continued by the response to the rise for at most 2 h in the
dark), cut by the invalid intervals and kept in pieces of at least 1 h, each
piece with its own initial air and wall state. It is keyed by its last period
and compared with the window of the three periods before it, so the two never
share a period; the first windows have none (`waiting for a second series of
quiet periods to confirm`). A window is computed once, when its last period is
settled, oldest first, at most one job per hour of elapsed time; no local hour
is involved. A window whose data have left the 7-day history is skipped.

**Disturbed periods.** The tracking keeps the disturbance $d$ of each tracked
60 s sub-step (at most 36 h) until the periods it may belong to are settled,
since the selection of a period is final only then. Each settled period then
gets $\int d \, dt$ and the tracked duration over its identification
interval, a sub-step crossing a bound counting for its fraction inside; the
steps before its end are dropped. A period with at least 4 h tracked whose mean
differs from the median of the last 14 periods by more than 0.08 °C/h is left
out of the windows that hold it.

**Information.** Each fit (simple model and model with walls) gets the
covariance of all its parameters from the Jacobian $J$ of its weighted model
outputs at the optimum: the nonlinear parameters, $a$, the initial air and
wall states of every piece and, when the window estimated the emitter (grid
search, not the refined value in force), the log of the emitter first moment
(central difference at ±2 %):

$$ C = \tau_{int} \, s^2 (J^\top J)^{-1}, \qquad s^2 = \frac{\sum w r^2}{\sum w - p} $$

with $\tau_{int}$ the residual count over its effective count (the integrated
autocorrelation time used by the emitter search): correlated residuals carry
less information than their number. The standard deviations of the
coordinates a fit contributes follow by the delta method: $\ln a$ and
$\ln(b_{eq}/a)$ for the simple model; $\ln a$, $\ln \alpha$ and $\ln c$
($c = \gamma / \beta$) for the model with walls, whose $b_{eq}/a = (\alpha - c)/a$
is never the static loss and is checked against $s_{EB}$ within 5 % before a
publication and before any use. A fit is refused
(`the quiet periods do not determine the model precisely enough yet`) when
$1.96 \, \sigma$ of one of them exceeds the tolerance with which the agreement
of two windows compares it: $\ln 1.15$ for $a$, $\ln 1.25$ for $b_{eq}/a$ and
$\alpha$, $\ln 1.35$ for $c$. A model with walls whose wall share $c/\alpha$ is
below 0.1 is never used, so its precision is not required. The covariance is
the asymptotic one of least squares; the 95 % level and the reuse of the
agreement tolerances are the choices of the method. There is no minimum fit
duration: the precision decides whether the periods carry enough information.
The other conditions are unchanged: at least 40 temperature events, one hour
of full-power equivalent heating and a mean indoor-outdoor difference of 3 °C
over the fitted pieces.

**Agreement with the energy balance.** Every fit of the identification takes
the free gain in force as a known heat input, $a(u_e + g_{EB})$; the gain is
not fitted per window: over the quiet periods $T_{in} - T_{ext}$ varies too
little to separate a constant input from the static loss. A candidate (model
with walls or simple model) is published only if its own $b_{eq}/a$ agrees
with $s_{EB}$ within 5 % ($|\ln(\frac{b_{eq}/a}{s_{EB}})| \le \ln 1.05$);
otherwise the window publishes nothing (`the model's heat loss disagrees with
the measured heat balance`). The model in force is the last qualified one at
the time of the job. It is the only check of $b_{eq}/a$ that does not come
from the fits: without a qualified model in force nothing is published
(`waiting for a qualified heat balance to check the model's heat loss`, shown
with the status `learning`). The published value, the median of the last
candidates, is checked again against the model in force when it is published;
on a disagreement the publication in force is kept. A room model published by
an earlier version stays in force; the holding power still requires the
agreement with the heat balance in force (4.2).

**Emitter search.** The emitter lag (first moment) of the fits is searched on a
fixed grid, independent of the dead time in force: 13 values log-spaced from
60 s (the fit step) to 3600 s (a third of the shortest wall time $1/\beta$ of
3 h, so that the emitter and the walls stay separable), each split into a
first-order part (the refined $\tau_e$ when known, capped by the value) and a pure dead time, the rest. The lowest cost
$J$ is refined by a bounded Brent search in $\log \tau$ between its two grid
neighbours (to 1 %, at most 10 more fits). The profile is significant when
$\max (J - J_{min}) / J_{min} \cdot N_{eff} > \chi^2_{1, 0.95} = 3.84$, with
$N_{eff}$ the number of residuals of the best fit over their integrated
autocorrelation time ($1 + 2\sum \rho_k$ up to the first non-positive
$\rho_k$). An optimum at either end of the grid with a significant profile
is refused (`the heating response delay does not match the measured
temperature`). A profile that is not significant does not determine the
emitter: the fits use the emitter in force when the emitter lag learning has
published a refined value, else the grid optimum, and the agreement with the
energy balance judges the static loss.

**Residual checks.** The runs test of the fit residuals (10 min grid inside
the fit periods) counts the sign changes of their hourly means, when there are
at least 10; the other checks (bias, spread, autocorrelation at 1 h,
correlation with the emitter output, the setpoint and the outdoor temperature)
are unchanged.

$s_{EB}$ is bounded to 3, like $k_{ff}$. A room model source applies at full
weight (`warmup_scale = 1`) and does not use the unmeasured disturbance $d$ of
the tracking. The static part follows the setpoint; the wall part follows the
tracked air. With the air at the setpoint, $u_{ff1} = (b_{air}(SP - T_{ext}) - w)/a$,
with $b_{air}$ the total loss rate of the air and $w$ the wall state.

**Bumpless transfer of the static part.** A discrete change of the static
part moves into the integral, so that $u_{ff} + K_i \cdot I$ does not jump:

$$ I \leftarrow clamp\left(I - \frac{\Delta u_{ff}}{K_i}, -\frac{2}{K_i}, \frac{2}{K_i}\right) $$

The discrete changes of the static part are a change between the learned
source and a room model source (including the change of weight from the
learned warmup to full weight), a change of model structure (simple model and
model with walls), a new daily publication of the parameters and a new
static model ($s_{EB}$ or $g_{EB}$) in force. $\Delta u_{ff}$
is the new static part minus the outgoing one (the learned FF1 for the
learned source), both at the current setpoint and outdoor temperature and both
with the wall part applied at this calculation, inside the $[0, 1]$ clamp.
The switch between `room model (air and walls)` and `room model (static part)`
does not change the static part: nothing is transferred. The continuous
evolution of the setpoint and of the outdoor temperature is not transferred:
it is the action of the feed-forward. The learned source's own evolution
(warmup ramp, new `a`/`b`) is never transferred either. The transfer is
applied in the calculation, before the PI, and may cross zero; it is cut only
by the integral limits. Saturation and anti-windup are unchanged: they act on
the total command.

**Wall part blend.** The wall part is never moved into the integral. When the
wall term in force changes discretely (switched on when the tracking becomes
`ok`, switched off when it is not `ok` any more or when the learned source takes
over, a publication that changes the wall parameters; a new static model
alone does not start a blend), the applied wall part
goes linearly over `RM_WALL_FADE_S` = 30 min from the value applied last to its
target. The target is the live $\delta_{wall}/a$ of a tracking `ok`, or 0 when
the wall term is off; a fade-out never reads the tracking again. A new
discontinuity during a blend starts a new one from the value applied at that
moment, so a tracking that flaps between `ok` and not `ok` neither moves the
integral nor makes the command jump. When the learned source takes over in
heating, a fade-out goes on over the learned holding power; outside heating the
wall part is dropped. Out of a blend the applied wall part follows the live
$\delta_{wall}/a$. The holding power stays clamped to $[0, 1]$, and
`feedforward.wall_power_percent` shows the applied wall part.

**Open window.** There is no hold after a window. While it is open the tracking
pins the air to the measurement and drives the walls with it; the source is
`room model (static part)` and the wall part fades out. At the resume the air
is set to the measurement, its correlations with the walls and the disturbance
are dropped and the innovation statistics restart; the tracking is `ok` again
at once and the wall part blends back in over `RM_WALL_FADE_S`. An open window
raises the real air-wall exchange above the model's, so the tracked walls tend
to be warmer than the real ones and the wall part errs low. A sensor near the
window can bias it the other way; the bound on the wall term and the
innovation checks cover that case.

**Restart.** The source in force is saved with the integral (`ff_source`),
and the wall part blend with it (`ff_wall`: the applied wall part, the value a
blend starts from and its start time on the wall clock). At the first
calculation after a restart, a different source (room model store lost,
tracking restarting) is transferred like any change of source and starts a
wall blend from the restored applied value; the same source resumes without
transfer, and a blend in progress goes on where the wall clock puts it. A state
saved without `ff_source` is taken as the learned source; a state without a
valid `ff_wall` applies the wall term of its saved source in full. A reset of
the PI memory (HEAT/COOL change, prolonged pause, learning reset) forgets the
source and the applied wall part: the next source starts without transfer and
its wall part blends in from 0.

### 4.3 Setpoint management

The regulation works on the raw setpoint in HEAT and in COOL. The I branch and
the P branch read the same setpoint; the P and I errors read an anticipated
temperature (see **Anticipation** below), which is the only mechanism that
lowers the command ahead of the setpoint.

`SmartPISetpointManager` keeps the boost state. A
significant setpoint change (`SETPOINT_BOOST_THRESHOLD = 0.3 °C`) activates the
boost, which raises the rate limit of the output (`SETPOINT_BOOST_RATE`) until
the error falls below `SETPOINT_BOOST_ERROR_MIN = 0.3 °C`. The detection of a
setpoint change that invalidates the learning windows is made by the
orchestrator, from the change of the raw setpoint (more than `0.01 °C`).

#### Anticipation (heating and cooling)

In HEAT and in COOL, in the stable phase, the proportional error and the integral error
read an anticipated temperature: the measured temperature plus a correction
$c$, the distance the room is about to travel during the time between a
command and its effect. The deadband, the near-band, the guards, the
feed-forward and the learning keep the measured temperature.

The correction is the least-squares slope $s$ of the raw temperature history over
the last $W = \min(\max(\text{ANTICIPATION\_MIN\_WINDOW\_S},\ H),\ \text{ANTICIPATION\_MAX\_WINDOW\_S})$
seconds ($H$ is the horizon below), multiplied by $H$ and limited to
$\pm$ `ANTICIPATION_MAX_C` (`1.0 °C`; `ANTICIPATION_MIN_WINDOW_S = 600 s`,
`ANTICIPATION_MAX_WINDOW_S = 1800 s`). A window shorter than $H$ would turn a
quick step of the sensor into a large correction. It is 0 without horizon, outside
the stable phase, with fewer than 3 samples, or when the samples span less than
half of the window. It then fades in with the distance to the setpoint: with
$e = $ target $-$ measured temperature,

$$c \leftarrow c \cdot \mathrm{clamp}\left(2 - \frac{e}{c},\ 0,\ 1\right)$$

so the correction is 0 when the room moves toward the setpoint and is further
from it than $2c$, full within $c$ of it, beyond it, or when the room moves
away from it.

The ratio $e / c$ does not depend on the mode: in COOL the room approaching the
setpoint from above has $e < 0$ and $c < 0$, so the rule is the same in both
modes.

Inside the regulator's deadband (the state left by the previous tick), the
correction is kept until it is 0 or its sign differs from that of the previous
tick, then it is 0 as long as the room stays in the deadband. A room that
arrives at its setpoint keeps its braking, so the command does not step at the
entry; a room settled at its setpoint is not approaching it, and a small quick
move of the sensor would otherwise be extrapolated over the whole horizon. The
correction starts again when the room leaves the deadband.

The errors are computed on the anticipated temperature $T + c$ with the sign of
the mode (target $-$ temperature in HEAT, temperature $-$ target in COOL):

- HEAT: $e_P = \text{target} - (T + c)$ and $e_I = (\text{target} - T) - c$,
- COOL: $e_P = (T + c) - \text{target}$ and $e_I = (T - \text{target}) + c$.

The horizon $H$ is the time between a command and its effect on the room. It
is read from the mode, with the same dead-time source as the gain rule
(`gain_dead_time`):

- in HEAT, the median of the last emitter lags measured by the heating balance (the kept
  emitter lags, section 3.7) when it is positive, published source `heating balance`;
- otherwise, and always in COOL (the heating balance is measured in heating
  only), the longer of the active dead time of the mode (the heating dead time
  in HEAT, the cooling dead time in COOL) and half its passive dead time (the
  cooling dead time in HEAT, the heating dead time in COOL), each counted only
  when reliable and positive, source `dead times`;
- otherwise 0: no anticipation, source `none`.

`analysis.control` publishes `anticipation_c` (the correction applied at the last
tick), `anticipation_horizon_s` and `anticipation_horizon_source`.
`temperature.anticipated` publishes the anticipated temperature (measured
temperature plus correction), in `live` and in `history`.

The canonical `live.setpoint` block publishes `boost_active`.

No command cap is applied after the PI computation: the PI command goes straight to the soft constraints (rate limiter, `max_on_percent`) and then to the timing constraints.

After a setpoint change or a resume, the integral follows the raw setpoint
error without any extra recovery rule: it is limited only by the anti-windup
(below), the deadband and near-band modes, the overshoot clamp and the overheat
cut.

Window resumes and power-shedding resumes in heating mode hold the integral
during the dead time:

- on resume, SmartPI arms an explicit `I:HOLD` (`window_resume` or
  `power_shedding_resume`),
- that hold remains active while the heating response is still inside the usable `deadtime_heat` window,
- when that window ends, the integral runs normally again,
- a resume whose signed residual error is already below the residual error
  floor ends the hold before the dead-time window starts,
- without a reliable heating dead time, no hold is applied.

This sequence prevents the integral from learning a resume transient that is still dominated by heating dead time.

Transient catch-up states are not restored after a reboot:

- any temporary `integral_hold_mode` is cleared.

The restart therefore keeps only useful persistent PI memory, without restoring transient servo states that are no longer physically valid outside their original runtime session. The wall-clock timestamp of the last valid active regulation tick is persisted and is not refreshed while HVAC is OFF. If inactivity exceeds 96 hours, controller PI memory is purged on resume while the learned A/B model remains available. If the HVAC mode after a restart differs from the mode of the persisted model (HEAT versus COOL), the integral is cleared. When a tick is skipped because the temperature sensor is missing or `guard_cut` is active, time tracking restarts like after OFF, so the error is not integrated over the skipped period.

### 4.4 Deadband, near-band, and protections

`DeadbandManager` handles:

- a symmetric deadband with absolute hysteresis (`DEADBAND_HYSTERESIS = 0.025`),
- an asymmetric near-band in heating mode; in cooling mode the near-band uses the configured `near_band_deg` on both sides of the setpoint,
- automatic near-band sizing in heating mode only, when `deadtime_heat` and the model are usable,
- fallback to configured thresholds otherwise.

Additional protections currently present are:

- `SmartPIGuards` (heating mode only, outside the bootstrap and the pulses of a calibration): `guard_cut` forces the output to 0 when the measured temperature exceeds `setpoint + near_band_above` while the heater is active, until it falls back to that threshold minus the larger of `NEAR_BAND_HYSTERESIS_C = 0.05°C` and `GUARD_CUT_RESET_STEPS = 1.5` observed sensor steps (a reading flickering between two levels around the threshold would otherwise re-trigger the cut, and restart the cycle, at every measurement). During a cut the output stays at 0 (`I:OVERRIDE(guard_cut)`). While the PI still requests power, a positive integral tracks the forced 0 % by back-calculation, `d(Ki·I)/dt = −v/Ti` with `v` the PI request and `Ti = Kp/Ki`, at most one cycle per tick. Its rate follows the request, not the overshoot: an integral that carries more than the heat need comes down within a few cuts, even when the room barely exceeds the threshold. It comes down until it alone would hold the room at the cut threshold minus `NEAR_BAND_HYSTERESIS_C` (`Ki·I/Kp`); below that point it is kept, as is the heating need left after a setpoint decrease. Without this, a bias of the integral restored the same excess power at each release and the room cycled between cut and restart. The temperature filter, the dead time detector, the published temperature and the learning keep following the room. The PI is not computed during a cut: the published error (`temperature.error`, `analysis.control.error_p`) is the setpoint minus the temperature of the calculation, the published anticipation is `0` and `control.mode` is `heating cut above the setpoint`; the internal error terms and the anticipation keep their values for the release. The room then cools without heating, for example during the whole fall after a setpoint decrease: this is a passive window for `b` and for the emitter lag baseline. The governance sees a zero command outside any band, and `a` windows are blocked (`skip: guard cut`). The learning stays contiguous at the end of the cut; `guard_kick` restarts the cycle when, inside the near-band, the temperature falls below `setpoint - near_band_below` while the command is below 99 %. In cooling mode neither guard acts and the near-band is not auto-sized. In the bootstrap the heating runs between its own hysteresis thresholds and a running guard is released,
- per-tick anti-windup on the applicable upper limit (see below),
- explicit `I:HOLD` during the post-resume `deadtime_heat` phase for `window_resume` and `power_shedding_resume` in heating mode,
- `I:HOLD` with source `vt_safety` while Versatile Thermostat safety mode applies its default power instead of the SmartPI command,
- `I:HOLD` with source `deadband_hysteresis_shell` while the hysteretic deadband state is active and the error is outside the core deadband,
- `I:HOLD` with source `governance_sensor_invalid` while no outdoor temperature has been received yet (`DEGRADED` regime),
- `I:HOLD` with source `calibration_recovery` after each calibration pulse until the room reaches the setpoint,
- freeze of P and I inside the core deadband.

The anti-windup acts at every calculation, in the linear demand space, on the applicable upper limit: `1`, or the linear image of `max_on_percent` when it is lower. It has two parts:

- conditional integration: the integral stops growing when the unconstrained request exceeds the applicable upper limit. Within `NEAR_SETPOINT_UNWIND_STEPS = 1.5` observed sensor steps of the setpoint, an output saturated against the error still lets the integral unwind toward zero, without crossing it (`I:UNWIND(SAT_HI|SAT_LO)`), provided the output without the proportional term (`u_ff + Ki·I`) stays within its limits: with a quantized sensor, a reading one step away from the setpoint saturates the output through the proportional term alone, and a blocked integral combined with the deadband freeze would hold a steady offset. An output held at its limit by `u_ff + Ki·I` (for example at `max_on_percent`) keeps the integral, which then carries the model bias;
- back-calculation: on every tick where the integral mode is `I:RUN`, `I:SKIP` or `I:CLAMP`, the excess of the PI output (`u_ff + u_pi`, clamped to `[0, 1]`) above the linear image of `max_on_percent` corrects the integral by `beta * excess / Ki`, with `beta = dt / max(Kp/Ki, dt)` (tracking time constant equal to `Ti = Kp/Ki`). The correction only moves the integral toward zero and stops at zero, so an excess carried by the proportional term alone never winds the integral in the opposite direction. The integral stays clamped to `±2/Ki`. Only the integral changes: `u_pi` keeps the decomposition that produced the command.

The following are not treated as saturation: Versatile Thermostat quantization and valve retention (`dpercent`, period), minimum on/off times, and the rate limiter. The behavior is symmetric in HEAT and COOL, does not use Versatile Thermostat command receipts, and is identical with older Versatile Thermostat versions. The `aw_du` diagnostic is the correction (excess, `<= 0`) applied at the last tick.

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
| `response_lag.py`      | `ResponseLagEstimator`: emitter lag on constant-power heating phases, settling gates and delivered power model |
| `heating_balance.py`   | `HeatingBalance`: heating rate and emitter lag measured on heating phases |
| `peak_search.py`       | the search of the temperature peak after a cut |
| `sensor_step.py`       | the step of the sensor, read on the last hour of samples |
| `measure_event.py`     | detector of the measurement events and record of the power and temperatures |
| `event_reading.py`     | reading of the events of the bootstrap and of the pulses, kept values per mode |
| `measure_pulse.py`     | measurement pulses of the direct start                      |
| `event_fit.py`         | fit of the response of the room to a heating event (background job) |
| `starting_values.py`   | starting values of `a` and `b`, release and warm-up credit |
| `learning_start.py`    | start of the learning (phase of the algorithm, source of the command, hand-over of the kept values) |
| `learning_outcomes.py` | bounded counters of learning-window outcomes                |
| `gains.py`             | gain calculation and freezing                               |
| `controller.py`        | discrete PI, anti-windup, hold, hysteresis, integral transfers |
| `deadband.py`          | deadband and near-band                                      |
| `setpoint.py`          | control setpoint and setpoint boost state                   |
| `anticipation.py`      | horizon and correction of the anticipation of the measured temperature (heating and cooling) |
| `resume_hold.py`       | I:HOLD during the dead time after a window or power-shedding resume, residual error floor |
| `feedforward.py`       | `u_ff1` computation, signed FF gain `b/a` and warmup scale, room model FF1 |
| `room_model/feedforward.py` | holding power source from the published room model and the static model of the energy balance in force, bumpless transfer of the discrete changes of its static part, blend of its wall part, static gain for the braking prediction |
| `ff_ab_confidence.py`  | confidence policy for `a/b`                                 |
| `governance.py`        | freeze decisions                                            |
| `calibration.py`       | calibration on request: requests, pulse limits, end, correction rule, published summary |
| `guards.py`            | `guard_cut` and `guard_kick`                                |
| `restart_bridge.py`    | the one decision on whether a restart is bridged, consulted by every component, and the context of the last tick |
| `quiet_periods/`       | darkness from the home position (`astral`), plateaus, setback runs, episodes, keys and regulated start of the quiet periods |
| `diagnostics.py`       | canonical live diagnostic payload                           |
| `diagnostic_history.py` | versioned envelope and stable Recorder series               |
| `tint_filter.py`       | adaptive indoor-temperature filtering                       |
| `timestamp_utils.py`   | monotonic / wall-clock conversion                           |

### 5.3 Internal composition of `SmartPI`

Components instantiated by `SmartPI.__init__()` notably include:

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name)
self.ctl = SmartPIController(name)
self.est = ABEstimator()
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.resume_hold = ResumeDeadtimeHold()
self.dt_est = DeadTimeEstimator()
self.guards = SmartPIGuards()
self._ab_confidence = ABConfidence()
self._ff_holding = HoldingTransfer()
```

### 5.4 Current persistence

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
    "gs_state": {...},
    "guards_state": {...},
    "ff_source": {...},  # holding power source the integral complements
    "ff_wall": {...},  # applied wall part and its blend
    "tint_filter_state": {...},
    "hysteresis_output": {...},  # bootstrap only
    "measure_events": {...},  # record of the power and temperatures (3.7)
    "event_reading": {...},  # readings in progress and kept values (3.7)
    "calibration": {...},  # calibration in progress or last one (3.7)
    "restart": {...},  # last tick, committed power (restart bridge)
}
```

The control loop schedules at most one delayed write of this payload per five-minute window; Home Assistant flushes a pending write at shutdown. Services and entity removal write a snapshot immediately.

Past event timestamps (temperature histories, start of a calibration, passive period and episode of the emitter lag on constant-power heating phases) are persisted as wall-clock times and converted back to the monotonic clock on load, so they keep their position in the past across a restart; in particular, the 48-hour limit of a calibration keeps running after a restart.

The emitter lag learning keeps its own store (state version 3): history,
pending comfort steps, catalogue of quiet periods with their keys, night
entries, regulated-period decisions, windows, evaluations, confirmed values
and lower bounds keyed by period, publications, the reference bootstrap dead
time frozen at the start of the learning and the home position the evidence
was found with. A version 2 store is read whole; its reference starts from the
last reliable bootstrap dead time it recorded, and it has no lower bound. A
version 1 store keeps only its history and its pending rises: its
nights, windows, evaluations, confirmations and published values were selected
by local hours without a solar check and are dropped, then found again from the
history. A change of the home position forgets the evidence of the periods. A
later version is ignored.

The room model keeps its own store (state version 2): published parameters
and their candidates, windows and records keyed by their last quiet period,
the observer state with the disturbance of the periods and the sub-steps
waiting for a period, the energy balance in force with the last day computed,
and the time of the last identification job. A version 1 store keeps its
published model, the observer's physical state (air, walls, disturbance,
command timeline, forecast scores), the energy balance in force and its
records; its windows and candidates (keyed by day, selected by local hours) and
its night disturbances (summed over local hours, without their sub-steps) are
dropped, and the windows are computed again from the quiet periods. A later
version is ignored.

#### Restarts

Versatile Thermostat sends no command to the heater when the integration is
unloaded or Home Assistant stops, and none at the start in heating or cooling
before its first cycle: over a short stop the heater keeps its last commanded
state. Such a
stop is only a gap of a few samples. `restart_bridge.py` decides once, at the
first calculation of a ready thermostat (a target and an indoor temperature are
available) after a load, whether the restart is **bridged**. Until then the
restart stays pending: a sensor that reports a few seconds after the load does
not prevent it, and the downtime is measured up to the calculation that decides.
All of these must hold:

- the downtime, on the wall clock from the oldest last tick of the three stores
  (algorithm, emitter lag learning, room model) to the first tick, is at most
  `RESTART_BRIDGE_MAX_GAP_S = 600 s`; a store written long before the stop (a
  debounced write lost in a crash) makes the downtime long;
- the HVAC mode is the same at the last tick and at the first tick, HEAT or
  COOL, and the cycle configuration (cycle length, minimum on and off delays,
  valve mode) is the same;
- the actuator state was saved: the committed power in the algorithm store and,
  in HEAT, also the heater cycle state in the emitter lag history and the last tick of
  the room model, for the stores that hold data (a store of an older format has
  none);
- SmartPI was in control at the last tick and is at the first tick: no window
  open, power shedding, safety mode, command overridden or unknown (after the
  restart, the window, shedding and safety state of the thermostat is read
  before any command);
- the first indoor temperature passes the jump rule of the emitter lag history (more
  than `0.5 °C` at more than `20 °C/h` is not physical) against the last one,
  dated from its last change;
- the wall clock did not jump (no negative downtime, no store tick in the future).

While the restart is pending because the indoor temperature is missing, a
calculation with a target keeps the command committed before the stop instead
of forcing 0: the heater kept that command over the stop, and cutting it for a
sensor that reports a few seconds later would interrupt the heating. The
command is kept while every rule above that does not read the indoor temperature
holds at the current calculation: the stop and the wait together last at most
`RESTART_BRIDGE_MAX_GAP_S`. Otherwise the output is 0 as for any missing sensor,
and stays at 0 until the temperature comes. A restart whose output was forced to
0 during the wait is not bridged (`indoor temperature unknown` when no other
rule refuses it): the heater did not keep its state.

Every component consults this decision; none decides on its own. The transient
state each component needs is saved with it (wall-clock times) and stays aside
until the decision.

| Component | Bridged restart | Other restart |
|---|---|---|
| Committed power | restored (the heater kept it); the first tick spans the downtime with it | 0 until the first applied command |
| Time tracking | the first tick spans the downtime, without the one-cycle learning freeze; setpoint shaping still starts from the raw setpoint on that cycle | first tick of a new session, learning frozen for one cycle |
| Dead time detection | the detection in progress resumes (3.6) | dropped, minimum OFF time from the load |
| a/b learning window | the window in progress resumes with its accumulators; its integral balance segment starts again | cleared |
| Emitter lag (constant-power phases) | passive period or episode resumed (3.7) | episode counted as interrupted |
| Heating balance | rest or phase in progress resumed (3.7, heating balance) | phase dropped, measurements kept |
| Measurement readings | the record, the readings in progress and the phase of the detector resume (3.7) | the readings in progress end, the record is dropped, the kept values stay |
| Bootstrap on/off output | resumed inside the band (3.1) | starts at 0 |
| Emitter lag history (HEAT only; in COOL as another restart) | the downtime is a missing sample (no invalid interval); the heater state at the stop is held until the first command after it, without any edge | the on period is closed at the last tick and only the downtime is invalid |
| Room model (HEAT only; in COOL as another restart) | the command applied at the stop is held over the downtime; the state is propagated through it and the forecasts in progress go on | the heater is taken as stopped from the saved state, the uncertainty is reopened and the forecasts in progress are dropped |

The heater state at the stop is the one of the store write: a reload writes the
stores at the unload, and Home Assistant writes the pending stores at its stop.
An on time planned to end before the stop has ended; a heater on at the stop
stays on until the first command after the restart.

A restart that is not bridged excludes only its downtime from the emitter lag history
(last tick to first tick), not a margin around it: the actuator state over the
downtime is unknown, but the observations before and after it are valid.

The emitter lag history keeps a marker of every restart (last tick, first tick,
bridged) in `restarts`, so that later rules can still see a restart that left no
invalid interval. The room model has its resume limit of 3 hours.

`control.last_restart` publishes the decision of the current session (5.5).

The integral-balance pools and the window-outcome counters are not persisted:
they start empty at each startup. The observations of the `a` measurements are
persisted with the estimator state; a state that does not contain them loads with
measurements that cannot be recomputed.

The applied power that feeds the dead time, the emitter lag learning and the
room model comes from the applied-command receipts of the Versatile Thermostat
cycle scheduler (`vtherm_api` `AppliedCommandReceipt`): one receipt for each
command it really applies, a `cycle_start` receipt at each master-cycle start
and an `update` receipt for each valve position applied during a cycle. The
receipt gives the applied actuator power, mapped to the linear model space.
SmartPI does not tag its requests: it reads the receipts only.

Without receipt support in Versatile Thermostat, SmartPI commits the power
reported by the cycle-start callback, or the published valve opening in valve
mode (read back after a mid-cycle update). A valve update without a published
opening commits nothing.

### 5.5 Published diagnostics

`diagnostics.py` exposes the algorithm and canonical live views:

- `SmartPI.get_published_diagnostics()`: returns the canonical `live` payload for the dedicated diagnostic sensor,
- `build_published_diagnostics()`: the function that builds that payload.

`diagnostic_history.py` wraps this payload in `schema_version`, `live` and the
stable eighteen-series `history` block. Normal sensor entities exclude `live` from
Recorder; the debug sensor subclass records it directly. The debug option only selects the sensor class that is recorded; it does not
add a second diagnostic block.

`control.last_restart` gives the decision on the restart of the current session:
`bridged`, the downtime `downtime_s` (seconds, `null` when the clock jumped or
nothing was saved) and, for a restart that is not bridged, the `reason` in plain
words. It is `null` before the first tick of a thermostat restored from a store,
and for a thermostat without a saved state.

`learning.start` is a plain summary of the start of the learning (`mode`,
`pulses_done`, `pulse`, `values_kept`, `values_kept_at`, `kept_a`, `kept_b`,
`kept_lag_s`), built by `LearningStart.public()` for the mode the model is in.
The pulse states are translated there (`none`, `rest`, `full power`, `stop`).

`analysis.learning.learn_ok_count` and `learn_skip_count` are runtime counters
relative to the current Home Assistant process. Persisted model counters remain
internal and are used for model quality and warm-up decisions.
`analysis.learning.window_outcomes_24h` lists, for `a` and for `b`, the eight
most frequent window-closure reasons of the last 24 hours as `[reason, count]`
pairs (numeric values removed from the reasons; pool events are prefixed with
`pool:`). `analysis.learning.balance_pool` gives the number of segments waiting
in each pool and the sensor resolution used for their uncertainty; these values
are specific to the current Home Assistant session.

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
| `RESPONSE_LAG_RESOLVE_EPISODES` | `4`   |
| `RESPONSE_LAG_PHASE_MIN_OFF_S` | `600` |
| `RESPONSE_LAG_MAX_REL_UNC`     | `0.15` |
| `RESPONSE_LAG_UNC_FLOOR_S`     | `30`   |
| `RESPONSE_LAG_MIN_OFF_S`       | `1800` |
| `RESPONSE_LAG_MIN_OFF_LAGS`    | `5`    |
| `RESPONSE_LAG_MIN_CROSSINGS`   | `3`    |
| `RESPONSE_LAG_MODEL_CHANGE_REL` | `0.5` |
| `RESPONSE_LAG_U_TOL`           | `0.05` |
| `RESTART_BRIDGE_MAX_GAP_S` (`RESPONSE_LAG_RESTART_MAX_GAP_S`) | `600` |
| `RESPONSE_LAG_PASSIVE_SPAN_S`  | `3600` |
| `RESPONSE_LAG_MIN_INVERSION_SLOPE` | `0.05` |
| `RESPONSE_LAG_RESOLVABLE_FRAC` | `0.9`  |
| `RESPONSE_LAG_B_REL_UNC`       | `0.10` |
| `RESPONSE_LAG_MAX_S`           | `DT_MAX_MIN` × 60 (`14400`) |
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
| `RECOVERY_ERROR_FLOOR_DEADBAND_RATIO` | `2.0` |
| `ANTICIPATION_MAX_C`          | `1.0`   |
| `ANTICIPATION_MIN_WINDOW_S`   | `600`   |
| `ANTICIPATION_MAX_WINDOW_S`   | `1800`  |
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

### 6.4 Calibration

| Constant                          | Value     |
| --------------------------------- | --------- |
| `CALIBRATION_MAX_S`               | `172800`  |
| `CALIBRATION_MIN_INTERVAL_S`      | `86400`   |
| `CALIBRATION_CORRECT_REL`         | `0.30`    |
| `CALIBRATION_CORRECT_B`           | `True`    |
| `CALIBRATION_SKIP_WHEN_OWN_AGREE` | `False`   |
| `CALIBRATION_OWN_RECENT`          | `3`       |
| `CALIBRATION_START_OVER_C`        | `0.2`     |
| `CALIBRATION_GAP_MIN_S`           | `3600`    |
| `CALIBRATION_GAP_MAX_S`           | `21600`   |
| `CALIBRATION_REST_EXTRA_S`        | `1200`    |
| `CALIBRATION_HEAT_MIN_S`          | `7200`    |
| `CALIBRATION_STEP_MAX_S`          | `14400`   |
| `CALIBRATION_DEPTH_CAP_C`         | `0.8`     |
| `CALIBRATION_FALL_WINDOW_S`       | `900`     |

`CALIBRATION_MAX_S` is in `const.py`, the others in `calibration.py`. The pulses of a calibration otherwise use the constants of the measurement pulses (`measure_pulse.py`).

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

The matrix drives the gain updates and thermal learning only. A regime transition (`REGIME_TRANSITION`) freezes gains and learning but does not hold the integral. The integral is held only by the sources listed in the protections of section 4 (deadtime, resume phases, `vt_safety`, `calibration_recovery`) and by two governance-related sources: `deadband_hysteresis_shell` (hysteretic deadband state active, error outside the core deadband) and `governance_sensor_invalid` (`DEGRADED` regime before a first outdoor temperature has been received). When the outdoor sensor stops reporting after a first value, the regime is `DEGRADED`: gains and thermal learning stay frozen, the integral keeps integrating, and the control path uses the last valid outdoor temperature.

---

## 8. References

1. **Astrom K.J. and Hagglund T.**, work on PID regulation and FOPDT model usage.
2. **Sundaresan K.R. and Krishnaswamy P.R.**, estimation of delay and time-constant parameters.
3. **OLS** regression with Student t-test for slope validation.
