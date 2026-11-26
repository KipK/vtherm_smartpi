# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

- Learning of `b`: when the response model of a mode is first published, the power the emitter still delivers is computed over the commands applied before it, instead of starting from the power applied at that moment. In the bootstrap the model is often published just after a cut: the first `b` windows counted no residual heat while the emitter still gave a fifth to a third of its power, which made `b`, and through it `a`, read low. When the response model of a mode changes, the stored `b` measurements recompute the power delivered over their window with the new model, as the stored `a` measurements already did. The applied command of the last 30.5 hours is saved and survives a bridged restart.
- Response model: an event whose dead time plus response time constant is more than twice, or less than half, the one of the model in force (or of the values carried over from the previous version while the mode has no event) is kept aside and changes nothing (`not used: far from the model in force, waiting for a confirming event`); a later event on the same side that agrees with it within 20 % confirms it, and both replace the history of the mode. Calibration events and the first event of a mode without a model are taken as before. A single regulation event at night in an occupied bedroom had read the slow mode of the room, replaced the carried-over values and stretched the anticipation horizon from about 5 minutes to almost 3 hours, which drove a limit cycle. A model saved by an earlier version is repaired at its load: the carried-over values come back as the reference when they can be computed again from the saved state, and the saved events are read again through the rule. New diagnostic `analysis.learning.response_model.pending_outliers`.
- Emitter lag (`T_ar`) learning removed: the response model already gives the dead time and the response time constant of the emitter. The room model's emitter search splits each tried emitter lag with the response time constant of the heating response model instead of the learned one; without a response model it searches a first order from the heating dead time after a start. The `tar_learning` diagnostics block is removed: the quiet periods (`reason`, `count`, `last`), the data quality (`data_invalid_reason`) and the length of the recorded history (`history_h`) are shown under `room_model`. The recorded history and the quiet periods saved by an earlier version are kept; its learned values are ignored.
- Dead time after a stop removed: SmartPI measures only the dead time after a start (the gains read it), what follows a stop is read by the response model. In heating `model.deadtime_cool_s` and `model.deadtime_cool_reliable` are no longer published (in cooling `model.deadtime_heat_s` and `model.deadtime_heat_reliable`), nor `analysis.deadtime.cool_last_outcome` in heating (`heat_last_outcome` in cooling) and `calibration.deadtime_stop_s`; `analysis.deadtime.state` no longer waits after a stop. Without a response model the anticipation horizon falls back to the dead time after a start (source `dead time`, was `dead times`). The free-running trend before a start is read from one emitter lag of the model after the last stop (the dead time after a start without a model). The stop step of the measurement pulses ends when the measurement event of the pulse is closed, for the pulses of the direct start as for those of a calibration; the early confirmation of the peak after a stop goes with the dead time after a stop. The emitter lag learning starts the fit of a night three emitter lags after the cut (no longer at least 1.5 dead times after a stop). A state saved by an earlier version loads: its dead times after a stop are ignored, the ones after a start are kept and seed the response model as before.
- Response model: a fit is rejected (`temperature rise too small`) when the rise of the room is under 0.10 °C, besides the floor of three sensor steps; on a 0.01 °C sensor a rise of a few steps fitted a wrong dead time and response time constant, and being in darkness replaced the good event in daylight.
- Integral holds read the response model: the holds after a heater start, after a window or power-shedding resume and after a heating unavailable period last the effective dead time of the model of the mode (dead time plus half the response time constant; three quarters of the emitter lag when the dead time is assumed); without a published model they last the dead time as before. The gains, and the pre-model gains of the direct start, read the dead time after a start of the mode. The emitter lag learning no longer changes the gains or the holds: its diagnostics `tar_learning.applied_s`, `gains_s`, `source`, `reference_s`, `in_force_s`, `in_force_source`, `out_of_bound` and `limit_reason`, and the history series `deadtime.applied_s`, are removed. New diagnostics `analysis.control.gain_delay_s` (the dead time read by the gain rule), `gain_delay_source` (`dead time`) and `gain_closed_loop_time_s`. The room model takes its emitter from the heating response model when published.
- Regulation reads the response model: the anticipation horizon is its emitter lag (dead time plus response time constant) in heating and in cooling, source `response model`; without a published model it falls back to the emitter lag kept from the direct start's measurement pulses, then to the dead times. The heating near band uses the model's effective dead time (dead time plus half the response time constant) above the setpoint and its emitter lag below it, instead of the heating and cooling dead times. When the model cannot tell the dead time apart, each of them takes the cautious end: the whole emitter lag for the horizon and the near band below the setpoint, three quarters of it as effective dead time. The calibration pulses rest the emitter lag plus 20 minutes, count the fall of the room over the effective dead time and heat at most twice the effective dead time plus the horizon. The measurement pulses of the direct start go on while the readings keep no values or the model of the mode is not published (instead of while a dead time is unknown).
- Calibration: the emitter lag is corrected by the response model's own rule (two agreeing calibration events replace its history when they are more than 30 % off), in either start mode; the calibration's separate lag correction is removed and `calibration.corrected` no longer lists `emitter lag`. A saved corrected lag is ignored.
- The heating balance is removed with its diagnostics `analysis.learning.heating_balance` (`state`, `measurements`, `a`, `lag_s`, `kept_lag_s`, `kept_lags`, `last_reason`, `last`). The measurement event detector it carried belongs to the learning window; a heating phase saved by an earlier version still resumes after a bridged restart. `analysis.control.anticipation_horizon_source` no longer takes `heating balance`; `analysis.control.near_band_source` takes `no response model` instead of `fallback_deadtime`.
- Learning of `a` and `b`: the learning reads the response model of its mode (below). No learning window of the learning stage, bootstrap or direct start, opens before the model of the mode is published (provisional included). The power delivered to the room for `a` is the command delayed by the dead time, then smoothed by the response time constant. An `a` window starts one emitter lag (the dead time plus the response time constant) after a power step (at most half the median heating phase), a `b` window one emitter lag after a cut, whether the dead time is measured or assumed. The power the emitter still delivers after the cut enters `b` as the delivered power enters `a` (`b = (a p - slope) / (T_int - T_ext)`, with the published `a` or its starting value; not counted before `a` is known); the `b` measurements are recomputed when `a` changes, as the `a` measurements are when `b` changes, one recomputation per change. The `a` windows of the learning before `b` has its 5 measurements are kept (reason `skip: a kept until b is learned`, instead of `skip: a blocked (b insufficient)`) and recomputed when `b` is published. One rule for the regulation, the bootstrap and the direct start. A power start ends a passive window at once. The emitter lag measured on constant-power heating phases, its settling gates and the dead-time gates of the learning windows are removed, and with them `live.analysis.learning.response_lag` (`lag_s`, `estimates`, `episodes`, `episodes_target`, `episode_median_s`, `last_reason`). `live.learning.bootstrap_status` reads `step1 - waiting response model` until the model is published (the `step1 - deadtime: …` and `step2 - waiting response lag: …` values are removed). The emitter lag of a calibration acts on the learning through the response model. Saved states load unchanged.
- Diagnostics: SmartPI fits a response model on whole measurement events (measurement pulses, bootstrap phases, calibration pulses, and comfort returns of the regulation after a rest with the home position known, read over six emitter lags after the heating step; with the home position known a window ends at sunrise, and an event in daylight is shown but used only while the mode has no other event, until the first one in darkness; fits farther than two sensor steps from the room temperature, or with the response time constant on its upper bound, are rejected; one on its 60 s lower bound is accepted and flagged): a dead time and a response time constant (the emitter, the air and the nearby mass together) per mode, split in halves when no event tells the dead time apart (`dead time assumed`), published in `live.model.dead_time_s`, `response_time_constant_s`, `response_source` and `response_confidence`, the effective dead time in `live.analysis.control.effective_dead_time_s` and the history series `model.effective_dead_time_s`, the last fit, the peak samples and the prediction error of the model on new events (`prediction_rms_c`, last and median of the last 7) in `live.analysis.learning.response_model`. Apart from the learning of `a` and `b`, the regulation does not read it. A thermostat updated from an earlier version shows a model carried over from its learned dead time and emitter lag (`previous version`) until its first measured event.
- Start of the learning: a new option, **Start of the learning**, chooses how SmartPI learns a room from a blank state. **Bootstrap** (default) runs hysteresis cycles at full power until the model is learned, then regulates. **Direct start** regulates with the PI from the first minute and measures the room with at most three light measurement pulses near the setpoint; it needs daily scheduled setpoint changes to keep learning and works best with a precise 0.01 °C sensor. The option applies to a new thermostat and after a learning reset; a thermostat that has already learned keeps its model and its start. Existing configurations keep the bootstrap. The start, the pulses done and the values kept from them (one set per mode, heating or cooling) are published in `learning.start`.
- Direct start: the measurement pulses of the learning stage heat until the room has clearly responded and is back at the setpoint (at most 0.1 °C under it) while the heating has lasted less than 30 minutes, past which they stop as soon as the room has clearly responded (the 2 hour and 0.3 °C over limits stay), so that the stop that follows starts high and lasts long enough to confirm the temperature peak. During the stop the regulation takes over only once the room is 0.5 °C under the setpoint and lower than at the start of the stop, and in any case at 0.8 °C under it (before, as soon as the room was 0.5 °C under, which could end the stop before the peak was confirmed and leave the dead time after a stop unmeasured). The pulses of a calibration follow the same rules.
- Direct start: the temperature peak that follows a stop is confirmed as soon as a 5-minute bin after the best one is lower than it by the largest of 0.05 °C, two sensor steps and half the rise since the stop (before, only a bin 30 minutes after the best one confirmed it); when the heating starts again before the confirmation, the bin in progress gets the same test. The dead time after a stop is therefore measured in rooms that lose heat fast, where the regulation took over before the peak could be confirmed, and the stops of the learning pulses end sooner.
- Diagnostics: during a guard cut (heating forced off above the setpoint) `live.control.mode` is `heating cut above the setpoint`, `live.temperature.error` and `live.analysis.control.error_p` follow the room (setpoint minus temperature) and `live.analysis.control.anticipation_c` is 0 (`live.temperature.anticipated` is the indoor temperature); before, they kept the values of the last regulation calculation before the cut. `live.temperature.integral_hold_source` takes the value `calibration_recovery` while the integral is held after a calibration pulse.
- Diagnostics: while the first model is not published with the direct start, the sensor state is `learning_pulses` (was `bootstrap_hysteresis`), `live.control.phase` is `Learning` (was `Hysteresis`), `live.learning.stage` is `measurement pulses` (was `bootstrap`) and `live.model.confidence` is `ab_learning` (was `ab_bootstrap`). The bootstrap start keeps its values. The Markdown cards show the new values.
- Heating and cooling: a thermostat that switches between HEAT and COOL keeps what it learned for each mode (heating or cooling rate, dead times, emitter response) and gets it back when the mode returns; the heat-loss coefficient stays shared.
- Learning: the outlier filter of the heating and loss coefficients keeps a minimum width of 2 % of its median, so a few nearly identical first measurements can no longer block the learning for days.
- Bootstrap: the heating phases of the hysteresis bootstrap and the temperature
  peak that follows each of them are now read as a whole; two agreeing readings
  give the model starting values for the heating rate and the loss rate when the
  bootstrap ends, until the continuous learning has its own. The regulation
  during the bootstrap is unchanged.
- The curve fits of these readings run in the background, one at a time with
  the room history and the room model, and never delay the regulation.
- Feed-forward: the holding power is the only feed-forward. The "Use FF3"
  option, the slow feed-forward trim, the thermal twin and its time-to-setpoint
  estimate are removed. The diagnostics groups `feedforward.fftrim`,
  `analysis.twin` and the FF2 and FF3 fields (`feedforward.ff3_status`,
  `feedforward.ff3_twin_usable`, `feedforward.twin_status`,
  `analysis.feedforward.u_ff2`, `u_ff3`, `ff2_*` and `ff3_*`) are removed;
  `analysis.feedforward.u_ff_final` and `u_ff_effective` equal `u_ff1`. Saved
  states and configurations load unchanged, and the Markdown cards no longer
  show these groups.
- Calibration: the calibration requested with `force_smartpi_calibration`
  makes at most three measurement pulses near the setpoint (in either start
  mode) instead of driving the room down to 0.3 °C under the setpoint and up to
  0.5 °C over it. Its pulses are sized from the dead times and the anticipation
  horizon of the room: they may start from 0.2 °C over the setpoint, rest long
  enough to measure the dead time after a start (ending early when the lowest
  point the room is heading to reaches 0.5 °C under the setpoint), heat at most
  2 to 4 hours, let the regulation take over during the stop only once the room
  is 0.5 °C under and still falling (0.8 °C at most), and are 1 to 6 hours
  apart. Its dead times join the learned ones. When two pulses agree, their
  heating rate, loss coefficient and emitter lag are compared with the values
  in use: a value more than 30 % off is corrected (the heating rate; the
  emitter lag with the direct start, through the response model, never the
  anticipation horizon) and stands until the learning has its own; the loss
  coefficient is read and published only, never corrected (it is biased in a
  room with heavy walls). At a correction the step of the holding power is
  moved into the integral, so the feed-forward and the integral term do not
  jump; corrected values survive a restart. While a
  calibration runs the learning takes no measure of the heating rate, and
  after each pulse the integral is held until the room is back at the
  setpoint. When the end comes during a pulse, a pulse at rest hands back at
  once, one heating at full power goes to its stop step, one in its stop step
  goes on, and the calibration ends with that pulse. The service never fails: a request is
  refused while the first model is learned, while learning is paused, while the
  thermostat is off or within 24 hours of the end of the last calibration, and
  ignored while one runs; a window, power shedding or a guard cut only delays
  the pulses. It ends when two pulses agree, after three pulses, or after 48
  hours, and its end is also checked during a guard cut. Switching the
  thermostat off ends the pulse in progress and the calibration goes on with
  the next one; a change between heating and cooling, a learning reset or a
  pause of the learning cancels it at once. The automatic calibration is removed, with
  its `autocalib` diagnostics block and its `smartpi_autocalib_*` events.
  `calibration.state` takes the values `idle`, `waiting`, `rest`, `full power`
  and `stop`; `calibration.retry_count` is removed; `calibration.pulses_done`,
  `result`, `last_request`, `deadtime_start_s`, `deadtime_stop_s`, `reading_a`,
  `reading_b`, `reading_lag_s`, `in_force_a`, `in_force_b`, `in_force_lag_s`
  and `corrected` are added. A calibration saved in progress by an earlier
  version is not resumed.
- Anticipation (heating): a saved state that holds a measured emitter lag but
  no kept lag (saved by a version that did not keep them) keeps that lag as
  the horizon of the anticipation after a restart, instead of falling back to
  the dead times until a new measurement.
- Heating balance (heating): SmartPI measures, on each heating phase followed
  by a stop after a rest, the heating rate at full power and the emitter lag
  from an energy balance of the room, and publishes them in
  `analysis.learning.heating_balance`, with the median of the last lags
  measured (`kept_lag_s`) and their number (`kept_lags`). Only that median is
  read by the regulation, as the horizon of the anticipation below. On a
  heating phase at constant power, the heat the emitter still holds when the
  temperature peaks is counted in the balance: the lag is closer to the real
  one in rooms whose walls absorb heat, where it was read short.
- Anticipation (heating and cooling): the proportional and integral errors
  read the measured temperature plus the distance the room is about to travel
  during the delay between a command and its effect (the slope of the
  temperature times that delay, at most 1 °C, faded in as the room approaches
  the setpoint; inside the deadband it is kept until it dies out, then there is
  none while the room stays there, so that the command does not step at the
  arrival and a small quick move of the sensor at the setpoint does not move it).
  In heating the delay is the median emitter lag of the heating
  balance, else the longer of the heating dead time and half the cooling dead
  time. In cooling it is the longer of the cooling dead time and half the
  heating dead time. It is the one mechanism of the approach to the setpoint
  in both modes: the setpoint trajectory (late braking) is removed, and the
  proportional branch always works on the raw setpoint. The overshoot of slow
  emitters is reduced. New diagnostics in `analysis.control`:
  `anticipation_c`, `anticipation_horizon_s` and
  `anticipation_horizon_source` (`heating balance`, `dead times` or `none`),
  and `temperature.anticipated` (the measured temperature plus the correction,
  in `live` and in `history`).
  Removed diagnostics: `setpoint.filtered_setpoint` (in `live` and in
  `history`: the regulation follows the setpoint of the thermostat),
  `setpoint.trajectory_active`, `setpoint.trajectory_source` and the whole
  `analysis.trajectory` block. The
  options "Setpoint filter" and "Release tau factor" of the configuration are
  removed: they only drove the trajectory.
- Emitter delay learning (heating, PWM heaters, always on): SmartPI learns how
  long the heater takes to warm the room, from the morning comfort steps and
  from a multi-day model of the room, for the room model. Shown in
  `tar_learning`.
- Quiet periods: the room model uses only stretches without sun (computed from
  the home position set in Home Assistant), at a constant setpoint, with valid
  data. No hour is fixed and nothing has to be declared. Without a home
  position, no quiet period is found and the diagnostics say why.
- Room model (heating, PWM heaters): SmartPI identifies a model of the room
  with its air, walls and emitter, follows its state and publishes temperature
  forecasts and their errors in `room_model`. A daily heat balance measures the
  power needed per degree of indoor-outdoor difference and the steady free
  gains. The model is published only once a qualified heat balance confirms it;
  it then gives the feed-forward holding power, including the extra power of
  cold walls after a night setback. With short setbacks and heavy walls it
  usually stays unpublished and changes nothing.
- Restarts: a restart of up to 10 minutes in the same mode, with SmartPI in
  control before and after, is bridged: measurements and learning resume and
  nothing is excluded from the learning data. `control.last_restart` shows the
  last decision.
- Quiet periods: a night setpoint raised while the room is still above the new
  value calls for no heat and no longer ends the night.
- Regulation: after a setpoint rise, a room that stays just short of the
  setpoint (cold walls) no longer keeps the slow feed-forward correction and
  FF3 blocked for hours.
- Restarts: a bridged restart also keeps the windows already counted by the
  slow feed-forward correction.
- Restarts: while the indoor temperature is missing after a restart, SmartPI
  keeps the command of before the stop instead of cutting the heating, up to
  10 minutes after the stop. Past that the command goes to 0 until the
  temperature comes, and the restart is not bridged.
- Diagnostics: the recorded history also keeps the feed-forward source and its
  wall part, the room model's `a`, `b_eq` and 1 h / 12 h forecast errors, and
  the dead time in force.
- Requirements: `numpy` and `scipy` are installed with the integration. If they
  cannot be loaded, nothing is learned and the bootstrap value stays in use.
- Documentation and cards: two names only, dead time (the time before the room
  temperature first moves after a command) and emitter lag (the mean time the
  commanded heat takes to arrive in the room), with a glossary.
- Anticipation: the slope of the temperature is read over a window equal to the
  delay between a command and its effect, between 10 and 30 minutes. A sensor
  that moves by steps of 0.05 to 0.1 °C makes the command swing much less.
  Rooms with a long delay may overshoot slightly more.

## [0.6.7]

- Calibration: whether a calibration can be completed is decided from the
  measured outdoor temperature only, no longer from the thermal twin. A
  passive phase can only bring the room toward the outdoor temperature, so the
  cycle ends as unreachable when the outdoor temperature is within 0.5 degC of
  (or beyond) the passive threshold to come: the cool-down threshold, or the
  target for the final cool-down (the mirror in cooling). The twin's
  equilibrium can no longer cancel a calibration meant to repair it. Nothing
  is judged while the outdoor temperature is unknown.

## [0.6.6]

- Bootstrap: the initial learning ends sooner when the response lag of the
  emitter cannot be measured. It then stops waiting after 4 heating phases
  instead of 7, with the same heating rate `a` as before. In rooms that heat
  once or twice a day (mild weather, well-insulated rooms, setpoint schedules),
  this shortens the bootstrap by one to two days.
- Bootstrap: heating phases are counted, not on/off switches. Heating runs
  separated by less than 10 minutes without heating count as one phase, so a
  burst of short interruptions no longer ends the bootstrap early.
- Restart during the bootstrap: when the temperature sensor is not yet
  available right after a Home Assistant restart, the heating phase in progress
  resumes once the temperature is back (within 10 minutes), instead of being
  cut until the room cools down to the lower threshold.
- Diagnostics: `learning.bootstrap_status` reads `heating phases K/4` while it
  waits for the response lag, and `analysis.learning.response_lag.episodes`
  counts heating phases.
- Regulation: after a setpoint increase or a resume, the room no longer
  lingers below the setpoint after the rise. The integral now works during the
  whole recovery, so the remaining gap usually closes within half an hour
  instead of one to three hours. The integral hold during the heating dead time after a window
  or power-shedding resume, the anti-windup and the overheat cut are unchanged.
- Slow emitters (floor heating, heavy radiators) may overshoot the setpoint
  more after a large setpoint increase, and rooms with slow emitters may see
  more overheat cuts, until a tuning dedicated to slow emitters arrives.
- Diagnostics: `integral_guard_source` and `integral_guard_mode` are removed,
  and the integral modes `I:GUARD(...)` and `I:GUARD_UNWIND(...)` no longer
  appear.

## [0.6.5]

- Regulation: the slow feed-forward correction is now also learned when the
  room stays in the border of the deadband (for example 0.04 °C above the
  setpoint). Before, it was never applied there and the integral stayed large;
  now the integral moves into the correction without changing the command.

## [0.6.4]

- Regulation: the heating dead time no longer shrinks after a start in a room
  that was warming slightly before the heater started, or when the course
  before the start is too short to judge. Such a measurement is ignored when
  it is shorter than the current dead time and kept when it is at least as
  long.

## [0.6.3]

- Regulation: the heating dead time no longer shrinks in a room that is
  already warming when the heater starts, for example when people get up. The
  rise was measured from the start level, so the room's own warming was taken
  for the heater's response, and the dead time and then the gains drifted
  (shorter dead time, stronger gains). The response is now measured against the
  course the temperature followed before the start. When that course is too
  steep or too irregular to tell the heating apart, the measurement is dropped
  and the previous dead time is kept. A room that is flat or cooling before the
  start is measured as before.

## [0.6.2]

- Regulation: a room no longer stays below the setpoint for good after a
  setpoint increase or a power-shedding, window or OFF resume. When the
  proportional and feed-forward action settle short of the setpoint, the
  integral was held until the room came within 0.1 °C, which it never did. A
  recovery that stays stalled now releases the integral, which then closes the
  gap at a reduced rate to avoid overshooting. Frequent power shedding no longer
  restarts the stall detection at each resume.
- Regulation: a restart no longer causes a dip followed by an overshoot while
  the feed-forward is still warming up. The feed-forward restarted from zero
  while the integral was restored, so part of the heating need went missing
  for half an hour. The feed-forward now comes back at the level it had before
  the restart.
- Diagnostics: the recorded history also keeps the integral state
  (`temperature.integral_error`) and the gains (`control.kp`, `control.ki`),
  so their evolution can be charted.
- Regulation: a room whose integral carries more than the heat need no longer
  cycles for hours between the overheat cut and the restart. During the cut
  the integral now follows the forced zero output at the pace of the power the
  PI still requests, instead of the small overshoot, so the surplus is gone
  within a few cuts. The heating need left after a setpoint decrease is still
  kept.

## [0.6.1]

- Regulation: a room no longer cycles between the overheat cut and the
  restart. During the cut the integral now sees the overshoot and comes back
  down while the PI still asks for power, as long as it alone holds the room
  above the cut threshold. The heating need left after a setpoint decrease is
  kept.
- Calibration: a cool-down is no longer stopped as unreachable on an absurd
  equilibrium. With the heating off, the twin's disturbance estimate is a
  leftover from the last heating; the passive phase is now judged on the more
  favourable of that equilibrium and the outdoor temperature, and a heating
  phase waits until the estimate is fresh again.

## [0.6.0]

- Calibration: with a sensor flickering between two values, the heating deadtime is no longer measured as zero. The calibration now detects the room
  response on the filtered temperature, as the regulation does.
- Learning: `b` is learned again while the heating stays off after a setpoint
  decrease. When the heating was running at the time of the decrease, the
  overheat guard held it off for the whole fall to the new setpoint, often the
  whole night, and SmartPI learned nothing during that time.
- Versatile Thermostat: a thermostat no longer stops with "Unknown
  proportional function: smartpi" after its options are saved or its entry is
  reloaded. SmartPI registers itself again each time Versatile Thermostat
  rebuilds its API, instead of only at Home Assistant start.
- Initial learning: after a restart, SmartPI no longer switches briefly to a
  partial power while it waits for the response delay.
- Initial learning: a short restart (up to 10 minutes) keeps the heating on or
  off as it was, and no longer loses the heating phase in progress.
- Initial learning: `b` is published after 5 cooling phases instead of 8, and
  `a` after 5 heating measurements instead of 6, or 4 when they agree closely.
  Both are the median of their measurements, and an odd count resists as many
  wrong measurements as the next even one.
- Initial learning: the heating runs between the two hysteresis thresholds as
  configured. The near-setpoint guards (cut above the setpoint, restart below
  it) no longer act during this phase: the cut stopped the heating well before
  the configured stop threshold and interrupted the learning.
- Initial learning: with a slow emitter such as floor heating, the delay
  before the room cools after the heating stops is measured even when the
  temperature keeps rising and plateaus for several hours (up to 12 h instead
  of 4 h). Such installations could stay in the initial learning forever.
- Learning: the heat-loss coefficient (`b`) learned during regulation is no
  longer underestimated after the heating stops. An emitter keeps giving off
  heat for a while after a stop; SmartPI now waits for the longer of 1.5 times
  the measured cooling delay and three response delays before measuring the
  cooling, even when the response delay is unknown. With floor heating, fewer
  `b` measurements are taken in regulation.
- Learning: more accurate heating gain (`a`), especially with slow emitters
  (radiators, floor heating) and slow or coarse sensors. SmartPI measures
  how long the installation takes to respond after the heating starts, ignores
  the start of each heating phase until the room has really started warming,
  and accounts for the heat actually delivered. In return, the initial learning
  can take a few hours longer in slow rooms.
- Learning: the heat-loss coefficient (`b`) is no longer distorted by a sensor
  flickering between two values.
- Learning: `a` is corrected automatically when `b` is refined.
- Learning: a heating measurement lasts at least one PWM cycle, or half a
  heating phase during the initial learning, so that a short measurement made
  steeper by sensor noise is no longer kept.
- Regulation: after a heat loss (window, cold evening), the room no longer
  stays stuck below the setpoint for hours when the model is off; once the
  recovery stops progressing, the integral can close the remaining gap.
- Regulation: the integral term corrects lasting errors (sun, weather,
  model error) in a few hours instead of several days.
- Regulation: less overshoot after a setpoint change: the correction already
  learned by the integral term is no longer discarded at each setpoint change.
- Regulation: with a noisy sensor, the overheat guard no longer switches the
  heating off and on at every reading near its threshold. It now releases
  1.5 sensor steps below its threshold, and the room is still observed while it
  holds the heating off. Before, a reading flickering between two values could
  restart the PWM cycle about once a minute and keep the regulation frozen most
  of the time.
- Regulation: with a sensor of 0.1 °C or coarser, the temperature no longer
  stays stuck one step away from the setpoint.
- Diagnostics: `bootstrap_status` shows when the learning waits for the
  response delay; `response_lag` shows the heating phases counted.
- Diagnostics: `analysis.deadtime` shows the outcome of the last delay
  measurement (detected, timed out, aborted) and the age of the one in
  progress.
- Diagnostics: `analysis.learning.response_lag` shows the measured response
  delay of the installation.
- Documentation: recommended PWM cycle duration by installation type and
  temperature sensor.

## [0.5.1]

- Learning: a learning window closed without a robust slope (coarse sensor of
  0.2 °C, or a sensor publishing only a few times per hour) is no longer lost.
  The integral heat balance between its first and last raw temperature steps
  is pooled with the following windows of the same kind, and the pool is
  submitted to the `a` or `b` estimator once its quantisation-bounded relative
  uncertainty is at most 25 %. Since 0.5.0 the learning slopes use the raw
  measurements, and such sensors could stay in the bootstrap phase without
  ever learning `a`. Noisy fine sensors keep the slope-based learning only.
- Diagnostics: `analysis.learning.window_outcomes_24h` lists the most frequent
  learning window outcomes of the last 24 hours for `a` and `b`, and
  `analysis.learning.balance_pool` the pending pooled segments and the sensor
  step used for their uncertainty.
- Documentation: recommended bootstrap hysteresis thresholds by sensor
  resolution and publication rate.

## [0.5.0]

- Compatibility: SmartPI requires Versatile Thermostat 10.6.2 or later. An
  older or unverifiable version prevents SmartPI from loading and displays a
  persistent Home Assistant notification with update instructions.
- Learning: the indoor temperature history used for the learning slopes and
  the dead-time detection keeps the last 300 minutes of samples, within a cap
  of 1800 samples (full coverage while samples arrive on average at most every
  10 s). With a sensor reporting more than once a minute, a long
  learning window used only its last samples (about 75 minutes at one report
  every 15 s) while its start temperature referred to the whole window.
- Dead time: the published `deadtime_heat_s` / `deadtime_cool_s` are the median
  of the last 6 detections of each channel instead of their mean, so a single
  aberrant detection (for example a response already under way at the power
  step) does not shift the learning windows and the gains. The value is
  recomputed from the stored detections at startup.
- Heating guard cut: the output is forced to 0 as soon as the measured
  temperature exceeds `setpoint + near_band_above` while the heater is active,
  without waiting for the filtered error to be inside the near-band, which
  limits slow overshoots after setpoint steps. The guard cut, the guard kick
  and the automatic near-band sizing act in heating mode only (documented).
- Diagnostics: the five `feedforward.fftrim.last_result` fields `admissible`, `mean_ki`,
  `transfer_eligible`, `transfer_reason` and `transfer_quality` are removed from the
  published diagnostics (no consumer; `admissible` was always `true`). The transfer
  reason and quality stay available under `feedforward.fftrim.transfer`.
- Diagnostics: the effective integral time `Ti = Kp / Ki` of the applied gains is
  published in minutes as `control.ti_min` (`None` when `Ki` is not positive)
  and shown in the diagnostic cards.
- Automatic calibration: a calibration is judged on the parameters that
  triggered it (`a`, `b`, dead times; a manual request on the dead times), `b`
  is only monitored when the indoor-outdoor gradient and the outdoor
  temperature allow it to be learned (which stops the repeated calibrations in
  mild weather), and a failed attempt is retried after the documented 6 hours
  instead of 24 hours (`autocalib.next_check_ts` and `autocalib.last_trigger_ts`
  report the real times). A calibration whose threshold the thermal twin cannot
  reach (warm outdoors, undersized emitter) stops at once with the event
  `smartpi_autocalib_unreachable`, uses no retry and waits 24 hours, instead of
  running until the 10-hour timeout. A threshold the measured temperature has
  already reached always moves the calibration on, and an active phase is
  judged at the power allowed by `max_on_percent`.
- The HEAT setpoint landing command cap applied after the PI is removed: the
  PI output is no longer capped near the target. The late-braking
  setpoint trajectory remains on the P branch and ends through its own
  completion (release near the target with a bumpless P step, target reached,
  or setpoint change). ON/`a` learning is no longer blocked while a setpoint
  trajectory runs; only the setpoint-change observation is rejected.
  Diagnostics removed: `live.setpoint.landing_active`, `landing_reason`,
  `landing_u_cap`, `landing_coast_required` and the `live.analysis.landing`
  block (`setpoint_for_p_cap`, `predicted_temperature`, `predicted_rise`,
  `target_margin`, `release_allowed`, `time_to_target_min`,
  `release_blocked_by_slope`, `command_before_cap`, `command_after_cap`); the
  Recorder `history` series are unchanged (`schema_version` stays 2).
  Effect on the B4 bench: switch landings practically unchanged; with valve
  position retention (`dpercent` 10) landings overshoot 0.05 to 0.25 °C more
  on some setpoint steps, while the landing cap was worse on as many other
  rows.
- Outdoor sensor without a value: SmartPI keeps using the last valid outdoor
  temperature for the feed-forward, the setpoint model and the near band, with
  no age limit, and the integral keeps integrating; gains, learning and the FF
  trim stay frozen (`DEGRADED` regime). Until a first outdoor temperature has
  been received, the feed-forward and the FF trim are zero and the integral is
  held (`governance_sensor_invalid`). Effect: much smaller error while the
  outdoor sensor is out (error integral over the outage about 4 times smaller
  on the dropout bench).
- The integration step after a gap between two calculations is capped at one
  cycle. Before, a gap up to ten cycles was integrated in full and a longer one
  counted for one cycle.
- Periodic FF-trim windows are no longer invalidated by the near-band gain
  taper, periodic proposals survive a saturation freeze, and the trim waits for
  the end of the feed-forward warmup (`ff2_freeze_reason` `ff_warmup`). Effect:
  the periodic trim is admitted and applied far more often.
- A regime transition no longer holds the integrator; gains and learning stay
  frozen. The integral is held only in the deadband hysteresis shell and in the
  `DEGRADED` regime until a first outdoor temperature has been received.
  Effect: smaller steady error after setpoint or regime
  changes, and the FF trim learns more often.
- Anti-windup works per tick on the applicable limit (`max_on_percent`) and
  only drains the accumulated integral; the end-of-cycle tracking is
  removed. Effect: no steady offset below the setpoint when Versatile
  Thermostat holds back valve positions (`dpercent`), with possibly more
  overshoot in that configuration. The `aw_du` diagnostic is the correction of
  the last tick.
- In valve mode with linearization, `max_on_percent` limits the valve opening
  sent to Versatile Thermostat, as Versatile Thermostat does: SmartPI caps its
  linear demand at the curve inverse of `max_on_percent`. Before, the cap applied
  to the linear demand gave a much smaller opening (13 % instead of 60 % for
  `max_on_percent` 0.6 with the default curve). Switch mode and valves without
  linearization are unchanged.
- SmartPI attributes each applied command to its request through the
  applied-command receipts of Versatile Thermostat (`vtherm_api` 0.7.0 or later).
  The feed-forward trim and the bumpless transfer require a Versatile Thermostat
  release whose cycle scheduler emits these receipts. With an older Versatile
  Thermostat, a warning is logged and PI regulation is unchanged, but the
  periodic trim and the bumpless transfer stay inactive and the stationary trim
  uses the sampled feed-forward. In valve mode, a trim window that overlaps a
  valve update without a published opening is rejected.
- In valve mode, commands are attributed to their SmartPI request, so the
  feed-forward trim and the bumpless transfer work with valves. A valve opening
  held back by Versatile Thermostat (regulation threshold or minimum period) or
  set by its deferred update is not attributed to SmartPI.
- `feedforward.fftrim.command_ownership` diagnostics report the receipts:
  `receipts_supported`, `kind`, `alterations` and `rejection_counts` replace
  `projected_power`, `projected_on_time_sec`, `projected_off_time_sec`,
  `expected_actuator_power`, `scheduler_realized_power` and `actuator_power_delta`;
  `power_delta` is the applied minus the requested power.
- Fix AutoCalib event payloads: `entity_id` now carries the climate entity id
  (fixing the "Invalid entity ID" recorder warning) and the thermostat name is
  in `name`. Automations that read the name from `entity_id` must read `name`.
- Turning the thermostat OFF during the PWM off phase now cancels the running cycle.
- In Versatile Thermostat safety mode the safety default power is applied instead of
  the SmartPI command, and those cycles are not used as SmartPI-owned commands.
- An options change reloads only the affected thermostats, once. Disabled SmartPI
  entries are ignored (thermostats fall back to the enabled defaults entry or to
  built-in defaults), and the defaults entry can be recreated after deletion.
  SmartPI services stay registered when a plugin entry is reloaded.
- Changing between HEAT and COOL across a restart now resets the integral learned
  on the other mode, and skipped ticks (missing sensor, guard cut) no longer
  integrate the error over the skipped period.
- SmartPI state is written at most every five minutes (and on shutdown, service
  calls and removal) instead of on every control pass.
- Restart now restores the temperature histories and the calibration timeout;
  dead time detection in progress is not resumed.
- Learning slopes are computed on raw temperature measurements instead of the
  filtered temperature, which avoids biased early slow slopes.
- The `a` and `b` drift detectors number their candidates separately, so a genuine
  drift on one channel is no longer blocked by measurements of the other.
- SmartPI services use the standard entity target (Versatile Thermostat climates
  only) and raise a validation error when no SmartPI thermostat matches.
- COOL uses the passive (rise) dead time for setpoint braking and the thermal
  twin power-off delay.
- COOL bootstrap hysteresis mirrors HEAT: cooling starts at setpoint + on
  threshold and stops at setpoint - off threshold.
- A setpoint change during a cycle is flagged in command ownership, and the
  feed-forward trim refuses a bumpless transfer for that cycle.
- `calibration.retry_count` now publishes the AutoCalib retry counter, and
  `analysis.feedforward.reason` no longer reports `ff_ab_fallback`.
- In valve mode, the minimal activation and deactivation delays no longer raise
  or zero the valve command: valve position does not depend on ON time.
- A Home Assistant repair warns when the Versatile Thermostat valve regulation
  threshold or minimum period filters SmartPI commands, or when an opening
  degree remap is active with valve linearization. The values are published
  under `live.valve`.
- Internal cleanup of dead code.
- While Versatile Thermostat safety mode applies its default power, the integral
  is held (`integral_hold_source` `vt_safety`) instead of integrating the error
  of a stale temperature.

## [0.4.0]

- Reworked attributes schema and HA database recording rules.
  Now it only records the "history" dict in normal mode and booth "live" and "history" dicts in debug mode.
  This will prevent HA database to be bloated by smartpi diagnostics in non debug mode.
- Add action service to pause and resume SmartPI thermal learning
  This can be usefull to trigger when a secondary heating system could
  bias the thermal model.
- Make the mono-mode 1R1C model fully signed in COOL: learn a negative active
  coefficient, use COOL dead time for IMC gains, and enable signed structural
  feed-forward while preserving the positive passive coefficient.
- Classify learned dead times by physical temperature response in both modes
  and migrate incompatible legacy COOL A/dead-time state conservatively;
  prevent HEAT's passive-dead-time AutoCalib fallback from validating COOL.
- Align FF trim learning with distinct temperature measurements and the delayed
  power that caused them, using independent thermal windows and the learned
  1R1C model.
- Transfer only the causally observed stable integral bias into FF trim at the
  post-anti-windup cycle boundary, while retaining the physical deficit as a
  real bounded command change in both HEAT and COOL.
- Expose the aligned P/I/Ki ownership terms, requested and applied transaction
  deltas, command invariant, rejection reason, and actuator engagement state.
- Bind valve ownership to the integer-percent opening published by the
  thermostat, while retaining scheduler realization as independent evidence.
- Synchronize SmartPI cycle projections with the scheduler effective minimum
  activation and deactivation delays.
- Preserve frozen switch command ownership across successive scheduler
  automatic PWM repeat quantizations without accepting unrelated cycles.
- Allow periodic FF trim windows to use observed cycles with negligible
  scheduler limiting, up to five equivalent cycle seconds.

## [0.3.8] 2026-08-18

- Allow the directional integral guard to unwind opposing PI memory to zero, preventing setpoint recovery from stalling below target.
- Remove the obsolete `hysteresis_thermal_guard` control path and diagnostic attribute, preventing stale guard state from blocking positive integral correction after setpoint changes.
- Expire persisted PI controller memory after more than 96 hours without active regulation while preserving learned A/B thermal coefficients.

## [0.3.7] - 2026-08-09

- Fix SmartPI device linking for single config entry ownership ( multi config entry went obsolete on ha 2026.8.0 )

## [0.3.6] - 2026-05-31

- Fix SmartPI diagnostic sensor recreation after VT reload
- Restrict SmartPI A learning fallback to calibration. Require robust ON slope for SmartPI A learning
- Fix restored SmartPI AB confidence state
- Add HVAC and target context to SmartPI learning windows, and skip B learning in HEAT when the passive heat-loss gradient is too weak or outdoor temperature is too close to target.

## [0.3.5] - 2026-05-23

- Fix SmartPI diagnostic entity persistence

## [0.3.4] - 2026-05-22

- Clean up SmartPI diagnostic entity when target algo changes
- Unlink registered device to config entry when not active

## [0.3.3] - 2026-05-20

- Add SmartPI PWM cycle force option for band transitions ( disabled by default )
- publish ab_bootstrap to confidence attribute in bootstrap mode
- publish specific_states.regulation_diagnostics attribute in vtherm to point diagnostic entitiy path

## [0.3.2] - 2026-05-19

- Dampen proportional output inside SmartPI deadband ( when enable P in deadband is activated )
- some attributes changes
- updated markdown cards

## [0.3.1] - 2026-05-04

- Fix SmartPI climate state publishing on sensor updates
- Deadtime-aware open-loop prediction for FF3
- Stabilize SmartPI valve output near minimum activation delay

## [0.3.0] - 2026-04-30

- Improve SmartPI landing release safety with slope timing guard
- Add guarded SmartPI landing cap release
  Release landing when its cap is non-constraining in the residual zone, keep the release sticky, and reset counters on inactive landing paths.
- Make SmartPI landing residual release sticky
  Prevent the landing cap from reactivating after residual release during the same trajectory, while allowing rearm when demand becomes significant again. Add focused landing coverage.
- updated markdown cards

## [0.2.2] - 2026-04-29

- Fix landing cap residual release
  Avoid keeping the setpoint landing cap active in release phase when only a small residual error remains. Add coverage for the stuck filtered setpoint case.

## [0.2.1] - 2026-04-29

- Implement setpoint landing cap for SmartPI
  Adds model-aware landing control to the setpoint filter so heating setpoint increases can reduce internal demand before reaching the target. The proportional trajectory now exposes a HEAT-only landing cap, applied after PI computation and before soft constraints, while keeping the raw PI diagnostic and integral behavior unchanged.

## [0.2.0] - 2026-04-26

- Valve power curve linearisation
  Inspired from @caiusseverus blueprint
  improve valve non linear power curve. more info: https://github.com/jmcollin78/versatile_thermostat/discussions/1704

## [0.1.10] - 2026-04-24

- Sync SmartPI committed power after valve mid-cycle updates
- Fix SmartPI default diagnostics registration
- Quicker boostrap phase:
  Adjust SmartPI bootstrap thresholds and AB publication
  (now needs only 8 B & 6 A before quitting bootstrap mode.)

## [0.1.9] - 2026-04-22

- remove vtherm_api dependency ( use VT installed one )

## [0.1.8] - 2026-04-21

- Align prop handler state change hook with changed flag API

## [0.1.7] - 2026-04-20

- switch to official vtherm_api package

## [0.1.6] - 2026-04-20

- Fix diagnostics sensor creation for default-bound thermostats
- Expose SmartPI phase as diagnostics sensor state
- Mark SmartPI diagnostics sensor as diagnostic entity

## [0.1.5] - 2026-04-19

- Remove default config entry in menu

## [0.1.4] - 2026-04-19

- Fix GuardState runtime typing in SmartPI guards
- Auto-create SmartPI default config entry on first setup

## [0.1.3] - 2026-04-18

- harden plugin services and refresh state after reset

## [0.1.2] - 2026-04-18

- replace vtherm api to main repo

## [0.1.0] - 2026-04-15

- Initial release of Versatile Thermostat SmartPI integration
- SmartPI proportional algorithm implementation
- HACS integration support
- Comprehensive documentation in French and English
- GitHub Actions for automated validation and releases
