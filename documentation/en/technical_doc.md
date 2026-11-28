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

- `u_ff1`: structural feed-forward (holding power), derived from $b/a$ (the room model never enters the command),
- `u_pi`: discrete PI term.

The value injected into the controller is `u_ff1 + u_pi`, then it goes through:

- rate limiting,
- anti-windup,
- cycle constraints and protections.

---

## 3. Online identification

### 3.1 Bootstrap order

In phase `HYSTERESIS`, learning follows a strict order:

1. the response model of the mode (3.7) must be published before any `a` or
   `b` window,
2. `b` must progress before `a` through a soft gate: an `a` window submitted
   while `b` has fewer than `AB_A_SOFT_GATE_MIN_B` measurements is kept
   (`skip: a kept until b is learned`), recomputed when `b` is published (3.4),
   and published with the collection once `b` has its measurements.

Rules effectively coded:

- `AB_MIN_SAMPLES_B = 5`,
- `AB_MIN_SAMPLES_A = 5`,
- `AB_A_SOFT_GATE_MIN_B = 5`.

SmartPI leaves `HYSTERESIS` once `b` has 5 measurements and `a` is published.
`a` is published at 5 measurements, or from `AB_MIN_SAMPLES_A_CONSISTENT = 4`
measurements when they agree: their median absolute deviation is at most
`AB_A_CONSISTENT_MAX_REL_MAD = 0.10` of their median. The first publication of
`a` also waits for the delivered power model (3.7): until it is settled (the
response model of the mode is published, or `RESPONSE_LAG_RESOLVE_EPISODES = 4`
heating phases have ended without it), measurements are collected without
publication, until the collection holds `AB_HISTORY_SIZE = 31` measurements: the
next accepted measurement then publishes `a`. Both buffers continue
filling up to `AB_HISTORY_SIZE = 31` during normal SmartPI regulation.

Inside the hysteresis band, the output keeps its previous on/off state. A
modulated output left by another phase is not a hysteresis state: it is set to 0
until the next threshold. The on/off state is persisted with the wall-clock time
of the last calculation. After a bridged restart (5.4), the first calculation
that has the indoor temperature resumes it inside the band, as an uninterrupted
hysteresis would have; while it is awaited, the on/off state of before the stop
is kept as described in Restarts. Outside the
band, the thresholds decide. After any other restart the output starts at 0. A heating or cooling phase in progress can
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
direct start is saved as `learning_start`), and a learning reset takes the configured start.

The `b` convergence used for drift-aware `a` learning relies on `b_converged_for_a()`:

1. `learn_ok_count_b >= 11`,
2. `len(_b_hat_hist) >= 5`,
3. `MAD(b_hat)/Med(b_hat) <= 0.30`,
4. `range(last_5_b_hat)/Med(b_hat) <= 0.10`.

### 3.2 Effective learning window

Learning relies on `LearningWindowManager` and on a sliding window that:

- requires outdoor temperature to be available,
- obeys thermal governance,
- rejects or resets an ON/`a` window on the exact observation where the user
  setpoint changes, so a window cannot span the old and new regimes,
- keeps early active ascent after that observation eligible for `a` under the
  existing dead-time, power-stability, robust-slope, and governance guards,
- keeps passive OFF/`b` learning under its existing physical guards;
  disturbance-only responses are not blocked by this setpoint gate,
- opens no window of the learning stage (bootstrap or direct start) before the
  response model of the mode is published, provisional included
  (`skip: learning stage - response model not yet published`, 3.7),
- applies a post-resume pause (`LEARNING_PAUSE_RESUME_MIN = 20`),
- delays the window start until the response transient has decayed
  (`skip: response transient (a|b)`), with the waits of the response model (3.7):
  an `a` window starts at least `min(θ + τ_e, half the median active episode)`
  after the power step, a `b` window at least `θ + τ_e` after the power cut.
  Without a model (outside the learning stage) there is no wait,
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

$$ b = \frac{a \cdot p - dT/dt}{T_{int} - T_{ext}} $$

with $p$ the mean power still delivered over the window after the cut (3.7) and
$a$ the usable value (published, or a starting value standing); before `a` is
usable, $p$ is not counted ($b = -\frac{dT/dt}{T_{int} - T_{ext}}$).

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

Likewise each `b` measurement keeps its observation (`dT/dt`, `T_int - T_ext`,
`p`, mode, and the start and end of its window, saved as wall-clock times). Each
time `a` is published, given as a starting value or corrected,
the retained `b` measurements of that mode with a residual power are recomputed
with that `a`, then `b` is aggregated again if it has already been published
(unless a starting value of `b` stands). A change below `AB_VAL_TOLERANCE`
recomputes nothing. A recomputation never starts another one: a change of `b`
recomputes the `a` measurements, a change of `a` recomputes the `b` measurements,
once each. A change of the response model recomputes the `p` of the `b`
measurements (3.7, model settled); the `b` it gives recomputes nothing either.

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

`DeadTimeEstimator` implements an independent FSM that measures the dead time
after a power start (OFF -> ON). Its channels describe the physical room
response, not the power-transition direction:

- `deadtime_heat`: dead time before the temperature starts rising,
- `deadtime_cool`: dead time before the temperature starts falling.

Therefore OFF -> ON learns `heat` in HEAT mode and `cool` in COOL mode; the
other channel stays empty. A power cut arms nothing: what follows a
stop is read by the response model (3.7).

Detection tracks the filtered indoor temperature, in regulation as during the bootstrap and the measurement pulses: a raw measurement that oscillates by one sensor step would otherwise detect a response as soon as the power steps. A detection in progress resumes after a bridged restart (5.4): its state, start, trough or peak, pre-start trend, last power and last power cut are persisted as wall-clock times, and its timeout still runs from the saved start. After any other restart it is dropped, and the minimum OFF duration must be observed again in the new session before a detection can be armed.

A response is detected once the temperature has moved by `0.05 °C` from its trough (heat) or peak (cool) since the power transition. The dead time runs from the transition to the last instant within `0.01 °C` of that trough or peak, tracked during the wait, so it does not depend on the retention of the temperature history. The wait gives up after `DEADTIME_ACTIVE_TIMEOUT_S = 14400 s`. A power cut aborts a pending wait. The outcome of the last wait per response and the age of the wait in progress are published (`analysis.deadtime`).

The response to a power start is the deviation of the temperature from the course it followed before the start. Without heating, a room is flat or cools down: the trough tracking above then dates the response. A room that already warms with the heater off (occupants, sun, a neighbouring room) moves away from the start level by itself, and the trough tracking would date the response too early (in COOL, the same holds for a room that already cools before the cooling starts). So, at an active start (power on), `deadtime_trend.prestart_trend()` fits a line on the raw temperature of the OFF period before the start:

- window: the last `DEADTIME_PRESTART_WINDOW_S = 1800 s` before the start, beginning no earlier than the last power cut plus the emitter lag $\theta + \tau_e$ of the response model of the mode (the emitter still acts on the room before that), plus the dead time after a start without a model;
- with less than `DEADTIME_PRESTART_MIN_SPAN_S = 900 s` or fewer than `DEADTIME_PRESTART_MIN_SAMPLES = 4` samples, no trend is removed;
- a trend whose fitted change stays within one sensor step (the smallest change observed in the window, at least `DEADTIME_PRESTART_STEP_FLOOR_C = 0.01 °C`), or that goes against the response, is not removed either: the detection is the trough (or peak) tracking above, unchanged. This is the usual case in the bootstrap, where the heating restarts because the room has cooled down;
- a trend in the direction of the response is fitted on the whole window and on its recent half (at least `DEADTIME_PRESTART_MIN_HALF_SAMPLES = 3` samples per half). The detection is rejected if the trend is not a line: a residual rms above half the detection threshold (and above one sensor step), or a slope that grows towards the response from the first half to the second by more than the detection threshold per half window. A line would then under-estimate the trend at the start;
- otherwise the detector follows the temperature minus the extrapolated trend, with the steepest slope of the two fits: removing too much only delays the detection. The trough, the `0.05 °C` threshold and the `0.01 °C` lookback apply to that difference;
- at the detection, the measurement is rejected if the trend alone accounts for at least the detection threshold since the start: the response is then not separable from the trend.

A rejected measurement publishes the outcome `rejected` and leaves the detections unchanged: the dead time keeps its previous value.

A measurement that would lower the published dead time is also ignored (outcome `ignored`, the dead time keeps its value) when the evidence is weak: the trend before the start is clearly in the direction of the response (a fitted change of at least `DEADTIME_PRESTART_RISE_MIN_STEPS = 2` sensor steps over the window, even if it was removed rather than rejected), or the window is too short to evaluate any trend. A room that warms before the start can only make the response look earlier, so it never shortens the dead time. A measurement that equals or lengthens the published value follows the rules above, and so does any measurement before the first published value.

Reliability becomes true as soon as at least one valid measurement has been captured on the corresponding channel. Each channel keeps its last 6 detections; the published value is their median, so a single aberrant detection does not move it (with one or two detections the median equals their mean). On restart, the published value is recomputed from the restored detections.

### 3.7 Emitter lag and delivered power

At the scale of a learning window, the room behaves as an integrator behind
a pure dead time $\theta$ (sensor, transport, actuator) and a first-order response
time constant $\tau_e$ (the emitter, the air and the nearby mass): the response
model of the mode (Response model, below). After a power step the slope settles
progressively; a window within the transient underestimates `a`, all the more as
the emitter lag $\theta + \tau_e$ is long or the sensor reports late. The a/b
learning reads the published model of its mode (`ResponseModel.values`), and
nothing else, for three things.

**Power steps.** `PowerSteps` (`power_steps.py`) follows the power applied over each
update interval: an active episode is a run of power after a step from rest (it
stays constant while the power moves by at most `RESPONSE_LAG_U_TOL = 0.05`), a cut
ends it. A discontinuity of more than 1 s between two updates ends the episode at
the last observed instant, and the power cut is dated at the resume. The last
`RESPONSE_LAG_HISTORY = 7` episode durations are kept (their median, once
`RESPONSE_LAG_MIN_ESTIMATES = 3` are known, caps the `a` wait). A heating phase
groups the active episodes separated by power cuts shorter than
`RESPONSE_LAG_PHASE_MIN_OFF_S = 600 s`.

**Delivered power.** The power delivered to the room is the command delayed by
$\theta$, then a first order of $\tau_e$ (the published split). Without a
model the delivered power equals the applied power. `a` is referred to the mean
delivered power of its window; the residual delivered power of a `b` window
enters `b` (3.4); the balance pools of `b` (3.5) keep the applied power. The
applied command is kept as change points over the last `RECORD_SPAN_S` (30.5 h,
at most `RECORD_MAX_POINTS = 6000` points), the unobserved stretches marked. When
the filter followed the applied power (no model yet, or a restart without its
history) and the model of the mode is published, the filter is rebuilt over the
commands observed since the last unobserved stretch, its output equal to the
oldest of them at its start: the first `b` windows after a cut count the heat the
emitter still gives, instead of a filter starting from the applied power (0).
After an unobserved stretch (guard cut, OFF, interval capped at one cycle) the
filter starts again at the applied power. For an active
window starting $t_0$ after the step and lasting $W$, the mean delivered fraction
is (`response_model.delivered_fraction`):

$$ \eta = \frac{1}{W}\int_{t_0}^{t_0+W} \left(1 - e^{-(t-\theta)/\tau_e}\right)\mathbb{1}_{t>\theta}\,dt $$

**Settling waits.** A window starts one emitter lag $H = \theta + \tau_e$
after the power step (`a`) or the cut (`b`), whatever the split (while the split
is assumed, $H = S$). The `a` wait is capped at half the median active episode
(`AB_SETTLE_ACTIVE_CAP_FRAC`): a longer wait would exceed a bootstrap heating
phase with a slow emitter. The delivered power model corrects the rest of the
transient: the rise in `a`, the residual heat of the emitter after the cut in `b`.
The same rule holds in the regulation, the bootstrap and the direct start.
Without a model there is no wait.

**Model settled.** The delivered power model of `a` is settled once the model of
the mode is published, or after `RESPONSE_LAG_RESOLVE_EPISODES = 4` heating
phases without it (the applied power is then used); until then `a` measurements
are collected without publication. In the learning stage no window opens before
the model, so its first publication finds no collection; when the model changes
later by more than `RESPONSE_LAG_MODEL_CHANGE_REL = 0.5` of its emitter lag (on
$S$ or on $\theta$) before the first publication of `a`, the collected
measurements are recomputed with the new delivered power. Only the windows that
the settling wait admits are recomputed; the others are dropped, since their
correction depends on the shape of the response. On the same change (the first
publication included, not the first update after a load), the `b` measurements
of the mode recompute their delivered power over their window from the command
history with the new model, then their value (3.4); a window the history does not
cover (before it, within `RECORD_PRE_S` = 6 h of its trimmed front, or across an
unobserved stretch) keeps its power. With no model, no window of the
learning stage opens, so the stage waits for the model (`learning.bootstrap_status`
reads `step1 - waiting response model`).

A bridged restart (5.4) resumes the passive period or the active episode in
progress at the save. The context is persisted as wall-clock times (last observed
instant, then start of the passive period with the delivered power and the
temperatures at the cut, or start, power and constancy of the episode) and
converted back to the monotonic clock on load. The heater kept the committed
power over the stop, and the first update after the restart spans the stop with
that power: a passive period goes on and the stop counts as passive; an episode
goes on, and the first observation continues or ends it like any other. After
any other restart the passive period is dropped and an episode is counted as
ended. The episodes are saved under `lw_state.power_steps`, and the command
history under `lw_state.power_steps.commands` (wall-clock origin, offsets in
seconds): a bridged restart resumes it, the heater having kept its command over
the stop, and a model published later rebuilds the filter over it; after any
other restart the history and the filter start again at the first observation.

#### Measurement events and starting values

One detector (`measure_event.py`, `MeasureEventDetector`, owned by the learning
window) watches the power applied to the emitter and the room temperature, in
heating and in cooling. An event is a rest, a phase of power above 1 % and a cut,
then the search of the temperature peak. Heating runs separated by less than
`EVENT_PHASE_MIN_OFF_S` (10 minutes) belong to the same phase. A rest shorter
than `EVENT_MIN_REST_S = 1800 s` before the phase marks the event as such (the
regulation events are then not read). A baseline, the least-squares line of the
temperature samples over the rest tail (at most `EVENT_BASELINE_SPAN_S = 3600 s`,
at least 4 samples), stands for the passive trajectory. The peak search
(`peak_search.py`) groups the samples in consecutive bins of
`EVENT_PEAK_BIN_S = 300 s` from the cut; the earliest bin with the strictly
highest mean (the lowest in cooling) is the peak, confirmed when a closed bin lies
at least `EVENT_PEAK_CONFIRM_S = 1800 s` after it; the search is abandoned when
the heating restarts while the best bin is the last one holding samples, or after
`EVENT_PEAK_MAX_S = 4 h`. Each event carries the source that drove the command
when its phase began: measurement pulse, bootstrap hysteresis, pulse of a
calibration or regulation. The reader below reads those of the bootstrap and of
the pulses; the reader of the response model reads all of them (3.7, Response model). The heating
phase in progress, or the rest before it, is saved (`lw_state.event_restart`),
resumed after a bridged restart and dropped otherwise.

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
single background loop that also runs the room history (its daily sun scan), the
room model and the reading fits, one job at a time, and the next calculation applies its result. After
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
values and does not set the horizon of the anticipation.

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
`PULSE_HEAT_OVER_C = 0.3 °C` on the other side), then stops until the
measurement event of the pulse is closed: its cut seen and its peak search
ended (at most
`PULSE_STOP_MAX_S = 4 h`; the PI takes over when the room is `PULSE_TAKEOVER_C =
0.5 °C` on the demand side and one sensor step lower than at the start of the stop,
and in any case at `PULSE_TAKEOVER_CAP_C = 0.8 °C`). A pulse interrupted by a restart, a stop, a change
of setpoint or of mode is not resumed and counts. The readings of
the events the pulses cause give the kept values. Until the learning publishes
its own model they stand for it: `pre_model_gains` gives the gains from the kept
`a` and the active dead time after a start (4.1; the IMC/SIMC rule of a slow room, neither gain
above its fallback value), the kept emitter lag is the horizon of the
anticipation (source `measurement pulses`). The integral term
starts from zero at the calculation at which the learning stage ends.

**Calibration.** On request (`force_smartpi_calibration`), once the first model
is published, a calibration (`calibration.py`) starts measurement pulses in
either start mode, with its own count (`PULSE_MAX_COUNT = 3` pulses at most);
they do not count for the learning stage. `Calibration.request` answers every
request without raising and publishes the answer (`last_request`): refused in
the learning stage, with the learning paused, with the thermostat off by the user
(the last calculation saw HVAC mode OFF without the window reason); ignored while
one is in progress; a window, a guard cut or a power shedding defers the pulses
only. A request has no minimum interval since the last calibration
(`Calibration.too_soon`, `CALIBRATION_MIN_INTERVAL_S = 24 h`, is a condition of
the automatic calibration only). The pulses of a calibration have their own
limits, computed at each calculation by `pulse_limits` from the response model of
the mode (`Le` = $\theta + \tau_e/2$ the effective dead time, `S` = $\theta + \tau_e$
the emitter lag, both 0 without a model), the anticipation horizon in use `H` and the least-squares fall rate `s` of the last
`CALIBRATION_FALL_WINDOW_S = 900 s` of the raw temperature on the demand side
(floored at 0); the static limits are frozen at the start of the pulse:

| Step | Pulse of a calibration |
|---|---|
| start | demand in [-`CALIBRATION_START_OVER_C` = -0.2 °C, `PULSE_START_UNDER_C` = 0.3 °C], previous command < 0.8, `clamp(2 H, 1 h, 6 h)` after the end of the previous pulse |
| rest | ends at `min(S + CALIBRATION_REST_EXTRA_S, 4 h)` (`CALIBRATION_REST_EXTRA_S` = 900 s of free-running window + 300 s), or when `demand + s Le >= PULSE_REST_UNDER_C` = 0.5 °C |
| heat | rise seen and (`demand <= PULSE_HEAT_BACK_C` = 0.1 °C or step >= `PULSE_HEAT_EXTEND_MAX_S` = 30 min), 0.3 °C over, or `clamp(2 (Le + H), 2 h, 4 h)` |
| stop | event closed; takeover at `demand >= 0.5 °C` once the room is one sensor step lower than at the stop start, or at `demand >= CALIBRATION_DEPTH_CAP_C` = 0.8 °C; 4 h |

The pulses of the learning stage keep the constants of `measure_pulse.py`. A
pulse is applied as an on/off command: committed at once, then the dead-time
estimator is fed with the committed power, once per calculation; its samples
join the median of the dead times after a start as any other. The phase is `Calibration` while
a pulse of the calibration commands the output; between pulses the regulation
runs in the stable phase. The events of the calibration pulses (source
`calibration`) are read like the others but form a set of their own, tested
among themselves only: two agreeing readings (`a` and lag within 20 %) give the
reading of the calibration (`a`, `b`, emitter lag, and whether the two `b` agree
within 20 %). At the end on that reading, `LearningStart._correct` compares `a`
and `b` with the values in force in the mode of the calibration (`|a|`, `b`; the
anticipation horizon in use is published beside the lag of the reading) and
corrects them when `|reading - in force| > CALIBRATION_CORRECT_REL * in force`
(0.30):

- `a`: `ABEstimator.correct_a` resets the channel of the mode and stands the
  reading as a starting value (`StartingValues.correct`), released at the first
  own publication of `a`;
- `b`: read and published only (`CALIBRATION_CORRECT_B = False`: the `b` of a
  pulse reading is biased in a room with heavy walls). With the switch on and the
  two `b` agreeing, `ABEstimator.correct_b` clears the `b` channel (measures,
  published history, counter, drift, `own_b`) and stands the reading, released
  when the own time constant is reliable again; the `a` measures are replayed
  with it.

The emitter lag is corrected by the response model itself, in either start
mode: the agreeing calibration events replace its history under the same 30 %
rule (Response model, below), and the horizon, the near band and the a/b
learning follow it.

With `CALIBRATION_SKIP_WHEN_OWN_AGREE = True` (off by default), `a` or `b` is not
corrected while its drift buffer is empty and its last
`CALIBRATION_OWN_RECENT = 3` measures are within the threshold of the value in
force. A correction latches the feed-forward credit at the ramp count before it
(`StartingValues.latch`) and notes the learned gain `b / a` before it in the
step transfer (`LearnedStepTransfer.note`): at the next feed-forward
the step of the learned holding power, evaluated at the same setpoint, outdoor
temperature and warmup, is moved into the integral. A change of `Kp` that follows
a correction is not moved into the integral: the proportional term acts on the error as
usual, and a change of `Ki` only rescales the integral (`SmartPIController.transfer_gains`). A bootstrap start whose `b`
stands from a correction does not return to its learning stage. The event
detector and the power steps observe the pulses as any heating phase.
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
dead time, the measurement events and the power steps go on. After each
calibration pulse (`LearningStart.recovering`) the integral is held (`I:HOLD`,
source `calibration_recovery`) until the room reaches the setpoint; the
pulses of the learning stage do not hold it. A stop of the thermostat ends
the pulse in progress, which counts, and the calibration goes on; a change of
HVAC mode, a return of the learning stage, a learning reset or a pause of the
learning cancels it (`cancelled`) and ends its pulse at once. The request, the pulses done, the mode, the
start, the last request, the values in force and the corrections are saved
(`calibration`); corrected starting values are saved with
`corrected` in `starting_values` and load even when nothing was handed over.

**Timing of the pulses, automatic calibration, report.**
`pulse_timing.quiet_window(wall, position, log, lag_s, require_history)` (pure) is
true when: it is dark at the home position (`quiet_periods/solar.py`, `is_dark`);
the setpoint and the HVAC mode have not changed for `QUIET_STABLE_S = 1 h`
(`SetpointLog.last_change`, the start of the observation when there is no change);
and, when the log covers at least `QUIET_DAYS = 2` days (`wall - since`), for
$d = 1, 2$ no change lies in $[t - 24d\,\mathrm{h},\ t - 24d\,\mathrm{h} + D]$ with
$D = \mathrm{clamp}(6S, 3\,\mathrm{h}, 6\,\mathrm{h})$ (`pulse_window_s`, $S$ the
anticipation horizon of the mode, 3 h when unknown); without that coverage the check
is skipped, or the window is refused when `require_history`. `SetpointLog` holds the
wall times of the setpoint and mode changes of the last `LOG_KEEP_S = 72 h`, at
most `LOG_MAX = 256`, and the start of the observation; it is fed once per
calculation (`LearningStart.begin`), a gap of more than `LOG_GAP_S = 1 h` between two
calculations counts as a change, and it is saved as `setpoint_log` (a state saved
without it starts a new log). `LearningStart._start_allowed` gives
`MeasurePulse.update(start_allowed=...)` its boolean: the pulses of an automatic
calibration need the window with the two-day history (`model_reading.position`
valid and astral importable; without it they never start); the pulses of the
learning stage and those of a requested calibration have no rule. Nothing else of
the pulse logic changes.

`LearningStart._auto_calibrate` (rules in `auto_calibration.py`) runs at each
calculation in HEAT or COOL. `AutoCalibration.observe` records the wall time at
which the response model of the mode was first seen carried over (no event, source
`previous version`) and forgets it when the mode has an event. It asks for a
calibration (`request_calibration(automatic=True)`, the refusals of the service)
when `due` (seen for `AUTO_CALIBRATION_WAIT_S = 3 days`, fewer than
`AUTO_CALIBRATION_ATTEMPTS = 3` attempts in the mode, the last one at least
`AUTO_CALIBRATION_RETRY_S = 20 h` ago), the learning stage is over, the learning is
not blocked, the thermostat is on, no calibration is in progress, the last one
ended more than `CALIBRATION_MIN_INTERVAL_S` ago, the home position is known and
the quiet window with `require_history` holds; an attempt counts when the request
starts. The state is saved as `auto_calibration` (`seen`, `attempts`, `last` per
mode). `automatic_status` is published from it.

Each calibration records the interruptions of its pulses (`MeasurePulse.interrupted`
taken by `LearningStart._note_interruptions`; reasons `setpoint change`, `thermostat
stopped`, `mode change`, `learning paused`), saved with the calibration with its
`automatic` flag. `Calibration.finish` leaves a report (`take_report`, once; a
learning reset leaves none). The handler calls `calibration_report.schedule_report`
after its calculation; a task fires the `calibration_ended` event and creates, replaces
or dismisses the notification `vtherm_smartpi_calibration_<unique id>` (texts from the
`issues` translations), catching and logging every exception.

The record, the readings in progress, the kept values and the pulses started are
saved (`measure_events`, `event_reading`, `measure_pulse` and `learning_start`,
and `starting_values` inside `est_state`).
After a bridged restart the readings resume (a fit in flight is queued again);
otherwise the readings in progress end and the record is dropped.

#### Response model

`response_model.py` holds, per mode, a dynamic model of the response of the room
to the power: a pure dead time $\theta$ (sensor, transport, actuator) followed by
a first-order response time constant $\tau_e$ (the emitter, the air and the
nearby mass together). The a/b learning reads it (3.7: no window of the learning
stage before it, delivered power, settling waits), and so do the anticipation
horizon (emitter lag $\theta + \tau_e$, 4.3), the near band in heating (effective
dead time above the setpoint, emitter lag below, 4.4), the limits of the
calibration pulses (above) and the measurement pulses of the direct start, wanted
while the model of the mode is not published or the readings keep no values. The
integral holds (4.3) read its effective dead time; the gains (4.1) keep the active
dead time after a start. It is published next to the dead times and lags above.

A second reader (`ModelReader`, `event_reading.py`) reads the measurement events
with the same windows as the reader above (lead 1800 s, first end at the close of
the event, extended to six lags of the last fit, 8 fits, 24 h), on a record of its
own that runs in every stage, and with its own background job. It reads the
events of the pulses, of the bootstrap and of the calibrations whatever values
are kept, and the regulation events that follow a rest of 1800 s, heat for at
least 600 s of full-power equivalent, rise by at least 0.3 °C to their peak, while
SmartPI drives the actuator over the whole window (no stop, no exclusion seen by
the thermostat: window, shedding, safety mode, command overridden) and only with
a known home position. The window of a regulation event ends six emitter lags
after its heating step (the published lag; without a published model, the lag
of its last fit, a longer window being fitted again shorter), whenever the
regulated phase closes.

With a known home position, the window of an event whose heating step starts in
darkness (`quiet_periods/solar.py`) ends at the next sunrise. An event is in
daylight when its heating step is not in darkness, or when the darkness after
its heating step lasts less than three emitter lags (the published lag, else the
lag of its fit). An event in daylight, of any source, is fitted and
shown with `daytime`; it is given to the model only when the mode has no event
yet, and the first event in darkness then replaces it. Without a home position
no event is in daylight (and no regulation event is read).

The fit (`event_fit.fit_response`) explains the room with the chain of the
reader above, with residuals only at the level crossings of the room: a change
of one sensor step (at most 1.5 steps) is taken at the midpoint of the two
levels, a larger change at its new value, the model is interpolated at those
instants. A profile grid of $\theta$ (0 to 1200 s by 60 s, then 1500 to 5400 s
by 300 s) and $\tau_e$ (24 values on a log scale, 60 s to 4 h) searches $b$ by
a bounded scalar search for each cell ($a$ and the start temperature by least
squares), then Nelder-Mead polishes the best cell within the bounds of the
grid. The residuals of successive crossings are correlated over about
$S = \theta + \tau_e$, so the number of independent residuals is taken as
$n = \max(W/S, 1)$, with $W$ the window after the heating step. The cells whose
cost is at most the minimum times $1 + 3.84 / n$ give the ranges of $\theta$
and of $S$; the fit tells $\theta$ apart when the range of $\theta$ is at most
$\max(300\,s, 0.5\,S)$. A fit is rejected with fewer than 8 crossings, a rise
of less than 3 sensor steps and of less than 0.10 °C (on a 0.1 °C sensor the steps
rule gives 0.3 °C), no heating seen ($a = 0$), $\theta$ or $\tau_e$ at
its upper bound (5400 s, 4 h), or a root mean square residual above 2 sensor
steps (0.01 °C at least): `fit too far from the room temperature`. A fit with
$\tau_e$ on its lower bound (60 s, a fast emitter whose true value may be
shorter) is accepted at 60 s and shown as `accepted, response time constant at
its lower limit`.

Per mode the last 7 accepted events are kept. $S$ is their median, $\theta$ the
median of the events that tell it apart (at most $S$), else $S/2$ (the middle
of $[0, S]$: effective dead time $3S/4$); $\tau_e = S - \theta$. One event publishes the model as `provisional`, two events whose
$S$ agree within 20 % make it `measured`, no event that tells $\theta$ apart
makes it `dead time assumed`. The events of a calibration join the history;
when two of them agree on $S$ and their mean is off the $S$ of the history by
more than 30 %, those two replace it. Derived values: effective dead time
$\theta + \tau_e/2$, emitter lag $\theta + \tau_e$, settling waits of the a/b
learning (3.7). Each confirmed peak after a cut also gives a peak sample
$\tau_e = (t_{peak} - \theta) / \ln(1 + s_{cut}/\ell)$, with $\ell = b\,(T_{cut}
- T_{out})$ and $s_{cut}$ the least-squares slope of the room over the last
$\min(\tau_e, 1800\,s)$ before the cut, when $s_{cut} \ge \ell$ and the peak
comes at least 600 s after $\theta$; the samples are published only.

An event far from the model in force needs a confirming event. The reference
of a mode is its published $S_{ref}$: the median of its events, or the seed
while it has none. An event (not of a calibration) whose $S$ lies outside
$[S_{ref}/2, 2\,S_{ref}]$ is kept pending, at most 3 per mode (the oldest
dropped), and changes nothing: `not used: far from the model in force, waiting
for a confirming event`. A later event outside the band on the same side whose
$S$ agrees with a pending one within 20 % confirms it: both replace the history
of the mode (`accepted with an earlier event far from the model, both replace
it`) and the pending events of the mode are dropped, so that a durable change of
the room, such as a new emitter, still moves the model. A mode without a
reference (no event, no seed) takes its first event as it comes. A single
event can otherwise read a slow mode of the room instead of the emitter (for
example at night in an occupied bedroom) and stretch the anticipation horizon
tenfold.

When the split is uncertain (`dead time assumed`), a consumer of the model takes
the cautious end of $\theta \in [0, S]$ for its own use: the integral holds read
the effective dead time $3S/4$, the near band above the setpoint and the calibration pulses read
$3S/4$ too, and the anticipation horizon and the near band below the setpoint read
$S$. The windows of the learning wait $S$ whatever the split.

The model, its readings and its record are saved under `response_state`. A
state saved without it is seeded from the active dead time $L$ of each mode
(`dt_est_state`) and the emitter lag $S$ kept by the readings, when there is one:
$\theta = \mathrm{clamp}(2L - S, 0, L)$, or $2L/3$ without $S$ (the split in
halves of $S = 4L/3$), and $\tau_e = 2(L - \theta)$, so that the effective dead time equals $L$. The seed is
published as `previous version`, `provisional` (values carried over from a saved
state without a response model), until the first event fed to the mode; a mode
without a reliable $L$ has no seed.

### 3.8 Quiet periods of the room model

The room model selects its data, kept by the room history (`smartpi/room_history/`), with
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
stay silent and `room_model.quiet_periods.reason` says why.

**Periods.** A plateau is a maximal interval of constant setpoint. A setback
run goes from a setpoint drop to the next rise (further drops stay in the run).
A rise to a setpoint still below the one before the drop, with the room
strictly above the new setpoint at the rise, calls for no heat: it does not end
the run, and its plateau is a further piece of it.
A darkness holds at most one episode: the longest part of a setback run inside
it, or the whole darkness when the setpoint does not change inside it (held
setpoint). The episode ends at the rise that ends the run, or at the end of the
darkness. Its quiet components are its plateau pieces intersected with the
valid spans of the history (`RoomHistory.valid_spans`: everything but the
invalid intervals); a bridged restart leaves no invalid interval and does not
cut them. The selected component is the longest one (at least 1 h, the earliest
on a tie).

**Regulated start** (`regulated_from` in the diagnostics). For a component, the first heater start at or after the
start of its plateau with the indoor temperature within 0.3 °C of the setpoint;
when it lies before the component, the component start if the data between them
are valid. A heater start within 60 s after a restart does not count. None when
the room never regulates there.

**Identification interval.** The selected component, continued after the rise
that ends the episode while it is still dark, the new setpoint constant and the
data valid, for at most 2 h. The response to the rise is the main excitation of the
emitter response: without it the heating response of a regulated room is poorly
determined.

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
| Identification window | the identification intervals of three consecutive settled episodes (bins wholly inside them), the wall state starting 10 h before the first; compared with the window of the three episodes before, never overlapping |
| Room model | the same windows of three settled episodes and their identification intervals, minus the periods the tracking found disturbed (§4.2); the disturbance of the tracking is summed per key |

---

## 4. Smart-PI control

### 4.1 PI gains

`GainScheduler.calculate()` applies the following logic:

1. if `tau` or the sign of `a` is unreliable, or no delay $L$ is known, fall
   back to `KP_SAFE = 0.55` and `KI_SAFE = 0.010`,
2. otherwise compute IMC/SIMC from the active-gain magnitude, with
   $\lambda = \max(3L, 2\,cycle, 5\,min)$ ($4L$ in valve mode):

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

The delay $L$ of the rule (`gains.start_dead_time`) is the reliable active dead
time after a start of the mode (`deadtime_heat` in HEAT, `deadtime_cool` in COOL),
as it is, source `dead time`; without it, safe gains. The response model (3.7)
does not enter the gains: the integral holds read its effective dead time
$\theta + \tau_e/2$ (4.3). The active dead time is a threshold reading of the
first rise, shorter than the effective dead time; the rule and its factor 3 are
calibrated on it.

The pre-model gains of the direct start (heating rate kept from the pulses,
before the first a/b model) read the same $L$. `analysis.control` publishes
`gain_delay_s`, `gain_delay_source` and the closed-loop time
`gain_closed_loop_time_s` ($\lambda$).

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

#### The room model does not enter the holding power

The holding power is always the learned law above, in HEAT and in COOL, with
PWM heaters and with valves. The room model (below) is observation only: it
identifies, tracks and forecasts, and publishes `live.room_model`
(`used_by_regulation` is `false`); it never changes the command, the gains,
the anticipation, the setpoint or the guards. `feedforward.source` is always
`learned model`. A bumpless transfer remains for the learned law only: the
step of a correction by the calibration (3.7) is moved into the integral.

The static model of the energy balance (below, `room_model.energy_balance_*`)
is the reference that the fits of the room model are checked against, not a
holding power. With heavy walls the fitted $b_{eq} = b_{air} - b_{wall}$ is a
small difference of two larger terms, and it moves with the emitter response
time used by the fit (by a fraction of a percent per second of emitter time);
the multi-day energy balance does not depend on it.

**Energy balance.** A job of its own computes, once per local day 20 minutes
after its midnight and whatever the quiet periods, the balance of each whole
local day of the room history before the day (at most 7; 10 min bins, invalid
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
periods of the room history (§3.8) through `RoomHistoryManager.quiet_periods`;
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
search, not the emitter in force), the log of the emitter first moment
(central difference at ±2 %):

$$ C = \tau_{int} \, s^2 (J^\top J)^{-1}, \qquad s^2 = \frac{\sum w r^2}{\sum w - p} $$

with $\tau_{int}$ the residual count over its effective count (the integrated
autocorrelation time used by the emitter search): correlated residuals carry
less information than their number. The standard deviations of the
coordinates a fit contributes follow by the delta method: $\ln a$ and
$\ln(b_{eq}/a)$ for the simple model; $\ln a$, $\ln \alpha$ and $\ln c$
($c = \gamma / \beta$) for the model with walls, whose $b_{eq}/a = (\alpha - c)/a$
is never the static loss and is checked against $s_{EB}$ within 5 % before a
publication. A fit is refused
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
on a disagreement the publication in force is kept.

**Emitter search.** The emitter in force is the HEAT response model (pure
delay its dead time $\theta$, first order its response time constant $\tau_e$,
3.7); without it, a first order whose time constant is the HEAT dead time after
a start; without either, a window is refused (`heating response delay not known
yet`). The emitter lag (first moment) of the fits is searched on a
fixed grid, independent of the dead time in force: 13 values log-spaced from
60 s (the fit step) to 3600 s (a third of the shortest wall time $1/\beta$ of
3 h, so that the emitter and the walls stay separable). Each first moment
tried (grid point or Brent evaluation) is fitted in two shapes: a pure first
order, and a split into a pure delay and a first order by the delay share of the
response model in force, $\theta / (\theta + \tau_e)$ (the delay is that share
of the first moment), when that share is positive; the shape with the lower
cost $J$ is kept, with its share for the rest of the computation; without a
response model or with a zero share, only the pure first order is tried. The lowest cost
$J$ is refined by a bounded Brent search in $\log \tau$ between its two grid
neighbours (to 1 %, at most 10 more fits). The profile is significant when
$\max (J - J_{min}) / J_{min} \cdot N_{eff} > \chi^2_{1, 0.95} = 3.84$, with
$N_{eff}$ the number of residuals of the best fit over their integrated
autocorrelation time ($1 + 2\sum \rho_k$ up to the first non-positive
$\rho_k$). An optimum at either end of the grid with a significant profile
is refused (`the heating response delay does not match the measured
temperature`). A profile that is not significant does not determine the
emitter: the fits use the grid optimum, and the agreement with the
energy balance judges the static loss.

**Residual checks.** The runs test of the fit residuals (10 min grid inside
the fit periods) counts the sign changes of their hourly means, when there are
at least 10; the other checks (bias, spread, autocorrelation at 1 h,
correlation with the emitter output, the setpoint and the outdoor temperature)
are unchanged.

**Wall extra power.** With the walls identified and the tracking `ok`, the
room model shows the wall extra power $\delta_{wall}/a$ of the tracking
(`room_model.wall_extra_power_percent`, both signs, in % of full power). It is
a display: the command does not read it.

**Open window.** There is no hold after a window. While it is open the tracking
pins the air to the measurement and drives the walls with it. At the resume the
air is set to the measurement, its correlations with the walls and the
disturbance are dropped and the innovation statistics restart; the tracking is
`ok` again at once. An open window raises the real air-wall exchange above the
model's, so the tracked walls tend to be warmer than the real ones and the
wall extra power shown errs low. A sensor near the window can bias it the other
way; the bound on the wall term and the innovation checks cover that case.

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
is read from the mode:

- the emitter lag $\theta + \tau_e$ of the response model of the mode (3.7,
  Response model) when one is published, source `response model`; with
  `dead time assumed` it is the emitter lag $S$ itself, the cautious end of
  $\theta \in [0, S]$;
- otherwise the emitter lag kept from the readings of the measurement pulses of
  the direct start in the mode, when positive, source `measurement pulses`;
- otherwise the dead time after a start of the mode (the heating dead time in
  HEAT, the cooling dead time in COOL) when reliable and positive, source
  `dead time`;
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
during the effective dead time of the mode: $\theta + \tau_e/2$ of the response
model, else the reliable active dead time (`smartpi/resume_hold.py`):

- on resume, SmartPI arms an explicit `I:HOLD` (`window_resume` or
  `power_shedding_resume`),
- that hold remains active while the heating episode is younger than that effective dead time,
- when that window ends, the integral runs normally again,
- a resume whose signed residual error is already below the residual error
  floor ends the hold before the dead-time window starts,
- without an effective dead time, no hold is applied.

This sequence prevents the integral from learning a resume transient that is still dominated by the dead time.

The end of a heating unavailable period (the entity turns off, heating or cooling)
arms the same hold (`heating_unavailable_resume`); the heating or cooling episode in
progress restarts at that instant, so the hold covers the effective dead time counted
from it. Unlike a window resume, it does not pause the learning for
`LEARNING_PAUSE_RESUME_MIN`.

**Learning block** (`learning_block.py`). The pause of `set_smartpi_learning`
(saved), the disturbance entity and the heating unavailable entity each block every
learning; their states are read at the start of every calculation, and only the
state `on` counts (`off`, `unavailable`, `unknown` and an absent entity count as
off). While blocked: `update_learning` only feeds the records of the measurement
events (so they stay continuous) and skips the windows and the
event detector; the dead-time detection is suspended but the
temperature history is fed (anticipation, pulses); the room history and the room
model receive the block as exclusion reason (an open window keeps its own reason)
and the handler starts none of their computations; no measurement pulse is advanced
and no kept value is handed over; a calibration request is refused. When the
learning becomes blocked, a calibration in progress is cancelled, the pulse in
progress ends and the readings in progress end without result; the response-model
reader records the block as an exclusion, so a regulation event overlapping it is
refused. Each change of the block restarts the learning window and the dead-time
detection, keeping the temperature history and the heating episode. The published
reason is `learning.block_reason`. The disturbance entity leaves the integral free;
the heating unavailable entity holds it (above).

Transient catch-up states are not restored after a reboot:

- any temporary `integral_hold_mode` is cleared.

The restart therefore keeps only useful persistent PI memory, without restoring transient servo states that are no longer physically valid outside their original runtime session. The wall-clock timestamp of the last valid active regulation tick is persisted and is not refreshed while HVAC is OFF. If inactivity exceeds 96 hours, controller PI memory is purged on resume while the learned A/B model remains available. If the HVAC mode after a restart differs from the mode of the persisted model (HEAT versus COOL), the integral is cleared. When a tick is skipped because the temperature sensor is missing or `guard_cut` is active, time tracking restarts like after OFF, so the error is not integrated over the skipped period.

### 4.4 Deadband, near-band, and protections

`DeadbandManager` handles:

- a symmetric deadband with absolute hysteresis (`DEADBAND_HYSTERESIS = 0.025`),
- an asymmetric near-band in heating mode; in cooling mode the near-band uses the configured `near_band_deg` on both sides of the setpoint,
- automatic near-band sizing in heating mode only, when the response model of the mode and the a/b model are usable: $NB = db + slope \cdot (L + cycle/2)$ with $L$ the effective dead time $\theta + \tau_e/2$ above the setpoint and the emitter lag $\theta + \tau_e$ below it ($3S/4$ and $S$ with `dead time assumed`); `near_band_source` is `no response model` without a response model,
- fallback to configured thresholds otherwise.

Additional protections currently present are:

- `SmartPIGuards` (heating mode only, outside the bootstrap and the pulses of a calibration): `guard_cut` forces the output to 0 when the measured temperature exceeds `setpoint + near_band_above` while the heater is active, until it falls back to that threshold minus the larger of `NEAR_BAND_HYSTERESIS_C = 0.05°C` and `GUARD_CUT_RESET_STEPS = 1.5` observed sensor steps (a reading flickering between two levels around the threshold would otherwise re-trigger the cut, and restart the cycle, at every measurement). During a cut the output stays at 0 (`I:OVERRIDE(guard_cut)`). While the PI still requests power, a positive integral tracks the forced 0 % by back-calculation, `d(Ki·I)/dt = −v/Ti` with `v` the PI request and `Ti = Kp/Ki`, at most one cycle per tick. Its rate follows the request, not the overshoot: an integral that carries more than the heat need comes down within a few cuts, even when the room barely exceeds the threshold. It comes down until it alone would hold the room at the cut threshold minus `NEAR_BAND_HYSTERESIS_C` (`Ki·I/Kp`); below that point it is kept, as is the heating need left after a setpoint decrease. Without this, a bias of the integral restored the same excess power at each release and the room cycled between cut and restart. The temperature filter, the dead time detector, the published temperature and the learning keep following the room. The PI is not computed during a cut: the published error (`temperature.error`, `analysis.control.error_p`) is the setpoint minus the temperature of the calculation, the published anticipation is `0` and `control.mode` is `heating cut above the setpoint`; the internal error terms and the anticipation keep their values for the release. The room then cools without heating, for example during the whole fall after a setpoint decrease: this is a passive window for `b`. The governance sees a zero command outside any band, and `a` windows are blocked (`skip: guard cut`). The learning stays contiguous at the end of the cut; `guard_kick` restarts the cycle when, inside the near-band, the temperature falls below `setpoint - near_band_below` while the command is below 99 %. In cooling mode neither guard acts and the near-band is not auto-sized. In the bootstrap the heating runs between its own hysteresis thresholds and a running guard is released,
- per-tick anti-windup on the applicable upper limit (see below),
- explicit `I:HOLD` during the effective dead time after a resume for `window_resume` and `power_shedding_resume` in heating mode,
- `I:HOLD` with source `vt_safety` while Versatile Thermostat safety mode applies its default power instead of the SmartPI command,
- `I:HOLD` with source `deadband_hysteresis_shell` while the hysteretic deadband state is active and the error is outside the core deadband,
- `I:HOLD` with source `governance_sensor_invalid` while no outdoor temperature has been received yet (`DEGRADED` regime),
- `I:HOLD` with source `calibration_recovery` after each calibration pulse until the room reaches the setpoint,
- `I:HOLD` with source `heating_unavailable` while the heating unavailable entity is on (`smartpi/learning_block.py`), then the explicit `I:HOLD` `heating_unavailable_resume` over the effective dead time counted from the instant it turns off (the heating or cooling episode restarts then), through the resume hold below,
- freeze of P and I inside the core deadband.

The anti-windup acts at every calculation, in the linear demand space, on the applicable upper limit: `1`, or the linear image of `max_on_percent` when it is lower. It has two parts:

- conditional integration: the integral stops growing when the unconstrained request exceeds the applicable upper limit. Within `NEAR_SETPOINT_UNWIND_STEPS = 1.5` observed sensor steps of the setpoint, an output saturated against the error still lets the integral unwind toward zero, without crossing it (`I:UNWIND(SAT_HI|SAT_LO)`), provided the output without the proportional term (`u_ff + Ki·I`) stays within its limits: with a quantized sensor, a reading one step away from the setpoint saturates the output through the proportional term alone, and a blocked integral combined with the deadband freeze would hold a steady offset. An output held at its limit by `u_ff + Ki·I` (for example at `max_on_percent`) keeps the integral, which then carries the model bias;
- back-calculation: on every tick where the integral mode is `I:RUN`, `I:SKIP` or `I:CLAMP`, the excess of the PI output (`u_ff + u_pi`, clamped to `[0, 1]`) above the linear image of `max_on_percent` corrects the integral by `beta * excess / Ki`, with `beta = dt / max(Kp/Ki, dt)` (tracking time constant equal to `Ti = Kp/Ki`). The correction only moves the integral toward zero and stops at zero, so an excess carried by the proportional term alone never winds the integral in the opposite direction. The integral stays clamped to `±2/Ki`. Only the integral changes: `u_pi` keeps the decomposition that produced the command.

The following are not treated as saturation: Versatile Thermostat quantization and valve retention (`dpercent`, period), minimum on/off times, and the rate limiter. The behavior is symmetric in HEAT and COOL, does not use Versatile Thermostat command receipts, and is identical with older Versatile Thermostat versions. The `aw_du` diagnostic is the correction (excess, `<= 0`) applied at the last tick.

The calculation also distinguishes two zones around the setpoint:

- the hysteretic deadband, used to stabilize the `in_deadband` state,
- the core deadband, defined by `abs(error_i) < deadband_c` once the time constant is reliable, used to actually freeze P and I.

This separation avoids keeping P and I frozen only because the hysteresis still holds the deadband state while the error has already left the configured deadband.

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
| `deadtime_estimator.py` | `DeadTimeEstimator` and active-episode starts |
| `ab_aggregator.py`     | median or weighted-median aggregation of `a/b` measurements |
| `ab_drift.py`          | persistent drift detection and recentering for `a/b`, with one candidate sequence per channel |
| `learning_window.py`   | learning-window management and guardrails                   |
| `ab_window_balance.py` | integral balance of windows without a robust slope and `a/b` pools |
| `power_steps.py`       | `PowerSteps`: power steps of the a/b learning (episodes, heating phases), delivered power and settling gates from the response model |
| `peak_search.py`       | the search of the temperature peak after a cut |
| `sensor_step.py`       | the step of the sensor, read on the last hour of samples |
| `measure_event.py`     | detector of the measurement events and record of the power and temperatures |
| `event_reading.py`     | reading of the events of the bootstrap and of the pulses, kept values per mode; reader of every event for the response model |
| `measure_pulse.py`     | measurement pulses of the direct start                      |
| `event_fit.py`         | fit of the response of the room to a heating event, and profile fit of the response model (background jobs) |
| `response_model.py`    | dead time and response time constant per mode, published only |
| `temperature_history.py` | raw and filtered room temperature history read by the learning |
| `starting_values.py`   | starting values of `a` and `b`, release and warm-up credit |
| `learning_start.py`    | start of the learning (phase of the algorithm, source of the command, hand-over of the kept values) |
| `learning_block.py`    | learning block: pause of the service, disturbance and heating unavailable entities, their effects |
| `learning_outcomes.py` | bounded counters of learning-window outcomes                |
| `gains.py`             | gain calculation and freezing                               |
| `controller.py`        | discrete PI, anti-windup, hold, hysteresis, integral transfers |
| `deadband.py`          | deadband and near-band                                      |
| `setpoint.py`          | control setpoint and setpoint boost state                   |
| `anticipation.py`      | horizon and correction of the anticipation of the measured temperature (heating and cooling) |
| `resume_hold.py`       | I:HOLD during the effective dead time after a heater start (`control.in_deadtime_window`) and after a window, power-shedding or heating-unavailable resume, residual error floor |
| `feedforward.py`       | `u_ff1` computation, signed FF gain `b/a` and warmup scale, transfer of a calibration step into the integral |
| `ff_ab_confidence.py`  | confidence policy for `a/b`                                 |
| `governance.py`        | freeze decisions                                            |
| `calibration.py`       | calibration on request: requests, pulse limits, end, correction rule, published summary |
| `guards.py`            | `guard_cut` and `guard_kick`                                |
| `restart_bridge.py`    | the one decision on whether a restart is bridged, consulted by every component, and the context of the last tick |
| `quiet_periods/`       | darkness from the home position (`astral`), plateaus, setback runs, episodes, keys and regulated start of the quiet periods |
| `room_history/`        | `RoomHistoryManager`: the 7-day signals the room model needs (indoor and outdoor temperature, setpoint, heater edges, invalid intervals), the catalogue of quiet periods, its own store and the daily sun scan |
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
self._ff_step = LearnedStepTransfer()
```

### 5.4 Current persistence

The current payload from `SmartPI.save_state()` notably contains:

```python
{
    "version": 2,
    "learning_enabled": ...,
    "last_active_wall_ts": ...,
    "est_state": {...},
    "dt_est_state": {...},
    "gov_state": {...},
    "ctl_state": {...},
    "sp_mgr_state": {...},
    "lw_state": {...},
    "db_state": {...},
    "gs_state": {...},
    "guards_state": {...},
    "tint_filter_state": {...},
    "hysteresis_output": {...},  # bootstrap only
    "measure_events": {...},  # record of the power and temperatures (3.7)
    "event_reading": {...},  # readings in progress and kept values (3.7)
    "calibration": {...},  # calibration in progress or last one (3.7)
    "restart": {...},  # last tick, committed power (restart bridge)
}
```

The control loop schedules at most one delayed write of this payload per five-minute window; Home Assistant flushes a pending write at shutdown. Services and entity removal write a snapshot immediately.

Past event timestamps (temperature histories, start of a calibration, passive period and episode of the power steps) are persisted as wall-clock times and converted back to the monotonic clock on load, so they keep their position in the past across a restart; in particular, the 48-hour limit of a calibration keeps running after a restart.

The room history keeps its own store (state version 3): the history, the
catalogue of quiet periods with their keys and the home position the catalogue
was found with. A change of the home position forgets the catalogue. A later
version is ignored. It needs numpy and scipy (without them nothing is recorded
and the room model shows `numerical libraries unavailable`), records nothing in
valve mode (the room model shows `valve heaters are not supported yet`), and has
no quiet period without a home position. Only the room model reads it; the
regulation reads neither the history nor the catalogue.

The room model keeps its own store (state version 2): published parameters
and their candidates, windows and records keyed by their last quiet period,
the observer state with the disturbance of the periods and the sub-steps
waiting for a period, the energy balance in force with the last day computed,
and the time of the last identification job. A later
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
  (algorithm, room history, room model) to the first tick, is at most
  `RESTART_BRIDGE_MAX_GAP_S = 600 s`; a store written long before the stop (a
  debounced write lost in a crash) makes the downtime long;
- the HVAC mode is the same at the last tick and at the first tick, HEAT or
  COOL, and the cycle configuration (cycle length, minimum on and off delays,
  valve mode) is the same;
- the actuator state was saved: the committed power in the algorithm store and,
  in HEAT, also the heater cycle state in the room history and the last tick of
  the room model, for the stores that hold data;
- SmartPI was in control at the last tick and is at the first tick: no window
  open, power shedding, safety mode, command overridden or unknown (after the
  restart, the window, shedding and safety state of the thermostat is read
  before any command);
- the first indoor temperature passes the jump rule of the room history (more
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
| Power steps (a/b learning) | passive period, episode and command history resumed (3.7) | passive period dropped, episode counted as ended, command history started again |
| Measurement readings | the record, the readings in progress and the rest or phase of the detector (heating) resume (3.7) | the readings in progress end, the record is dropped, the kept values stay |
| Response model readings | the record and the readings in progress resume (3.7) | the readings in progress end, the record is dropped, the model stays |
| Guard cut (HEAT only) | a cut running at the stop goes on until the usual release threshold | released |
| Bootstrap on/off output | resumed inside the band (3.1) | starts at 0 |
| Room history (HEAT only; in COOL as another restart) | the downtime is a missing sample (no invalid interval); the heater state at the stop is held until the first command after it, without any edge | the on period is closed at the last tick and only the downtime is invalid |
| Room model (HEAT only; in COOL as another restart) | the command applied at the stop is held over the downtime; the state is propagated through it and the forecasts in progress go on | the heater is taken as stopped from the saved state, the uncertainty is reopened and the forecasts in progress are dropped |

The heater state at the stop is the one of the store write: a reload writes the
stores at the unload, and Home Assistant writes the pending stores at its stop.
An on time planned to end before the stop has ended; a heater on at the stop
stays on until the first command after the restart.

A restart that is not bridged excludes only its downtime from the room history
(last tick to first tick), not a margin around it: the actuator state over the
downtime is unknown, but the observations before and after it are valid.

The room history keeps a marker of every restart (last tick, first tick,
bridged) in `restarts`, so that later rules can still see a restart that left no
invalid interval. The room model has its resume limit of 3 hours.

`control.last_restart` publishes the decision of the current session (5.5).

The integral-balance pools and the window-outcome counters are not persisted:
they start empty at each startup. The observations of the `a` and `b`
measurements are persisted with the estimator state.

The applied power that feeds the dead time, the room history and the
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
stable seventeen-series `history` block. Normal sensor entities exclude `live` from
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
| `RESPONSE_LAG_MODEL_CHANGE_REL` | `0.5` |
| `RESPONSE_LAG_U_TOL`           | `0.05` |
| `RESTART_BRIDGE_MAX_GAP_S` (`RESPONSE_LAG_RESTART_MAX_GAP_S`) | `600` |
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
| `CALIBRATION_CORRECT_B`           | `False`   |
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

`CALIBRATION_MAX_S` is in `const.py`, the others in `calibration.py`. The pulses of a calibration otherwise use the constants of the measurement pulses (`measure_pulse.py`). `CALIBRATION_MIN_INTERVAL_S` conditions the automatic calibration only.

### 6.5 Pulse timing and automatic calibration

| Constant                      | Value    | Module                |
| ----------------------------- | -------- | --------------------- |
| `QUIET_STABLE_S`              | `3600`   | `pulse_timing.py`     |
| `QUIET_DAYS`                  | `2`      | `pulse_timing.py`     |
| `WINDOW_MIN_S`                | `10800`  | `pulse_timing.py`     |
| `WINDOW_MAX_S`                | `21600`  | `pulse_timing.py`     |
| `WINDOW_LAG_FACTOR`           | `6`      | `pulse_timing.py`     |
| `LOG_KEEP_S`                  | `259200` | `pulse_timing.py`     |
| `LOG_MAX`                     | `256`    | `pulse_timing.py`     |
| `LOG_GAP_S`                   | `3600`   | `pulse_timing.py`     |
| `AUTO_CALIBRATION_WAIT_S`     | `259200` | `auto_calibration.py` |
| `AUTO_CALIBRATION_ATTEMPTS`   | `3`      | `auto_calibration.py` |
| `AUTO_CALIBRATION_RETRY_S`    | `72000`  | `auto_calibration.py` |

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
