# SmartPI beyond PI + A/B learning: room-model subsystems

Audience: AI agents with no prior context. Package root:
`custom_components/vtherm_smartpi/smartpi/` (written `smartpi/` below). Sources of truth: the code,
`documentation/en/technical_doc.md` (sections 3.6-3.8, 4.2, 5.4, 5.5), `documentation/en/diagnostics.md`
and `CHANGELOG.md` ([Unreleased]). References are `path::symbol`, without line numbers.

## 0. Map and what steers the regulation

| Subsystem | Code | Regulation reads it? |
|---|---|---|
| Room history (recorded signals, quiet-period catalogue, daily sun scan) | `smartpi/room_history/` | No: it serves the room model |
| Quiet periods | `smartpi/quiet_periods/` | Indirectly: selects the data of the room model |
| Room model (identify, observer, energy balance) | `smartpi/room_model/` | No: observation only (forecasts, forecast errors, `a`/`b_eq`, wall extra power and energy balance shown). Only link: its last tick enters the restart bridge decision (`SmartPI._restart_stores`) |
| Response model (theta, tau_e) | `smartpi/response_model.py` | Yes: a/b learning (learning-stage gate, delivered power, settling waits, `smartpi/power_steps.py`), anticipation horizon, HEAT near band, calibration limits, direct-start pulses wanted; not the gains nor the holds |
| Restart bridge | `smartpi/restart_bridge.py` | Yes: decides resume vs conservative restart for every component |
| Resume hold | `smartpi/resume_hold.py` | Yes: temporary I-hold after window / power-shedding resume; `recovery_error_floor_c` (residual error under which a recovery counts as closed) |
| Job abort | `smartpi/job_abort.py` | Plumbing for executor jobs |
| Lazy numpy/scipy | `smartpi/room_history/deps.py` | Gate: without them nothing is learned |

Wiring: `SmartPI(..., room_model=True)` (built so by the handler) creates `algo.py::SmartPI.room_history:
RoomHistoryManager` and `room_model: RoomModelManager(name, tz, history)` (the room model reads the history, the
catalogue and the status of the first). `handler.py` loads their stores, injects the home position
(`RoomHistoryManager.set_position`) and the dependency check (`set_dependencies`), runs the due jobs of the room history (sun
scan), the room model and the reading fit in one background loop, one after the other (`SmartPIHandler._process_learning`,
`_run_learning`, `_learning_jobs`), and schedules the saves of the two that have a store. Observers are called from the
regulation tick (`SmartPI.observe_room_history`, `observe_room_model`), wrapped in try/except: a learning failure must never
disturb the regulation.

PWM heaters only: in valve mode `HISTORY_VALVE_MODE_SUPPORTED = False` (`room_history/const.py`), the history stops recording
(`supported` false) and the room model is blocked (`room_model/manager.py::RoomModelManager._blocked_reason`). The room
model is HEAT-only. COOL is never learned nor applied.

## 1. Executor jobs, abort, lazy dependencies

- `room_model/jobs.py` takes a snapshot of plain Python data and returns plain data; it never touches event-loop state.
  `async_run` runs one job at a time via `hass.async_add_executor_job`. The daily sun scan of the room history and the fit
  of the measurement readings (`smartpi/event_fit.py`, `event_reading.py::EventReader`) are two other such jobs.
- `numpy`/`scipy` (manifest: `numpy`, `scipy>=1.14`) are imported only inside those jobs and the numeric modules
  they call (`room_history/signals.py`, `room_model/identification.py`). Event-loop code is pure Python
  (`room_model/linalg.py` is hand-written for the 3-state observer). `room_history/deps.py::dependencies_importable`
  only uses `importlib.util.find_spec`.
- The solar position uses `astral` (shipped with Home Assistant): `quiet_periods/solar.py::astral_available`.
- `job_abort.py`: a started job cannot be cancelled. Objective functions call `job_abort.check()`; the room model's
  `stop()` sets a `threading.Event` attached to the executing thread (`run`), so several thermostats never
  interfere. `JobAborted` ends the job silently (no state change). `RoomHistoryManager.stop()` does nothing.
- Failure handling in `async_run`: `ImportError` -> `set_dependencies(False)` (status `dependencies_unavailable`);
  any other exception -> `last_error` ("the calculation failed, see the log"), the item is marked failed; a
  result whose `_generation` changed (reset, new home position) while running is discarded.

## 2. Quiet periods (`smartpi/quiet_periods/`)

Purpose: select stretches without sun, at constant setpoint, with valid data, for the room model. Pure Python, no
Home Assistant import. Times are epoch seconds, half-open `[start, end)`.

- Inputs: home latitude/longitude (HA config, validated by `solar.py::validate_position`), setpoint timeline,
  valid spans of the room history (`RoomHistory.valid_spans`), heater starts, indoor temperatures.
- Darkness (`solar.py::dark_intervals`): sun centre below `DARK_ELEVATION_DEG = -0.833 - 0.5` degrees, sampled
  every `SUN_SAMPLE_S = 600`, boundaries refined to 1 s, moved inward by `DARK_TIME_MARGIN_S = 120`. Polar night
  is cut into `DARK_BLOCK_S = 24 h` blocks; polar day has no darkness. No margin is taken after sunrise.
- Episode (`selection.py::build_episodes`): at most one per darkness: the longest part of a setback run inside
  it, or the whole darkness when the setpoint never changes. A rise that stays below the pre-drop setpoint
  while the room is still above the new setpoint does not end the run.
- Selected component: longest valid plateau piece, at least `PERIOD_MIN_S = 1 h`.
- Regulated start (`selection.py::regulated_start`): first heater start in the component with the room within
  `REGULATED_ECO_TOL_C = 0.3` of the setpoint; starts within `RESTART_START_GUARD_S = 60` after a restart do
  not count. None means the room never regulated; the room model windows read it (shown as `regulated_from`).
- Identification interval: the component continued after the ending rise, while dark and valid, for at most
  `IDENT_TAIL_S = 2 h`.
- Key: `q<SELECTION_VERSION>:<origin>:<component start>`, 0.1 s resolution (`night_id`). The catalogue is rebuilt
  at most every `_CATALOGUE_EVERY_S = 600` (`RoomHistoryManager._refresh`) and only grows forward (`HISTORY_MAX_PERIODS = 60`
  episodes max), so a key never changes when the history is pruned. Every piece of evidence is keyed by its period: a
  period counts once. States: closed (end <= last observation), settled (identification interval known), complete
  (history covers it from its start).
- Outputs: catalogue read through `RoomHistoryManager.quiet_periods` by the room model (`room_model/periods.py`).
- Silent states: no valid home position, or `astral` missing -> no darkness, no period, the identification is silent;
  diagnostic `room_model.quiet_periods.reason` ("home position unknown: the absence of sun cannot be checked").
  A changed home position forgets the evidence of all periods (`RoomHistoryManager._check_position`).
  The daily energy balance (room model) does NOT need quiet periods.
- Not a regulation input.

## 2b. Pulse timing and automatic calibration (`smartpi/pulse_timing.py`, `auto_calibration.py`, `calibration_report.py`)

- `quiet_window(wall, position, log, lag_s, require_history)`: dark at the home position (`solar.is_dark` via `dark_intervals`), setpoint and mode unchanged `QUIET_STABLE_S`, and, when `SetpointLog` covers `QUIET_DAYS` = 2 days, no change in `[t - d days, + D]` (d = 1, 2; D = clamp(6 H, 3-6 h), H the anticipation horizon). `SetpointLog` (wall times of changes over 72 h, at most 256, a gap over 1 h counts as a change; saved `setpoint_log`) is fed by `LearningStart.begin`.
- `LearningStart._start_allowed` gives `MeasurePulse.update(start_allowed=)` its value: only the pulses of an automatic calibration wait for the window, always with the history (no start without a position); the learning-stage pulses of the direct start and the pulses of a requested calibration have no timing rule (the gate had delayed the first model, see decisions.md); a requested calibration also has no minimum interval (`Calibration.too_soon` only conditions the automatic one).
- `AutoCalibration` (per mode: first-seen wall time of the carried-over response model, attempts, last start; saved `auto_calibration`; `AUTO_CALIBRATION_WAIT_S` 3 days, `_ATTEMPTS` 3, `_RETRY_S` 20 h) is driven by `LearningStart._auto_calibrate`; it asks through `request_calibration(automatic=True)`. Status published in `calibration.automatic_status`.
- `MeasurePulse.interrupted` (setpoint change, thermostat stopped, mode change, learning paused; calibration pulses only) goes to `Calibration.interruptions`; `Calibration.take_report` is read once by `calibration_report.schedule_report` (handler), which fires the event `calibration_ended` and creates or dismisses the notification (texts: `issues` translations).

## 3. Room history (`smartpi/room_history/`)

Purpose: record the signals the room model fits and keep the quiet-period catalogue. It computes nothing about the
emitter: the emitter of the room model is the response model (section 4). Modules: `history.py` (`RoomHistory`),
`manager.py` (`RoomHistoryManager`), `signals.py` (`Anchors`, `duty_bins`, `level_bins`, `q_obs`: signals binned for the
fits), `clock.py` (`LocalClock`), `deps.py`, `const.py` (`HISTORY_*`).

Inputs recorded each tick (`history.py::RoomHistory.record`, `applied_cycle`, `applied_level`): indoor/outdoor temperature,
setpoint, heater edges (PWM cycles rebuilt from applied cycles) or valve levels, invalid intervals (`HISTORY_GAP_S = 1200` s
without observation, unavailable sensor, unknown command, non-bridged restart downtime, spikes `HISTORY_SPIKE_MIN_C = 0.5` C
at more than `HISTORY_SPIKE_RATE_C_PER_H = 20`, and any tick where SmartPI is not in control: window open, shedding, safety,
HVAC not heating, command overridden). Retention `HISTORY_RETENTION_S = 7 d`. Invalid intervals are never filled in.
(This data exclusion is about learning data quality only; see Design stance for sensor health.)

Manager: status `checking_dependencies` before the thermostat starts, `active`, `dependencies_unavailable` (numpy/scipy
missing, history not recorded); `set_position`, `observe`, `on_applied`, restart context (`restart_context`, `on_restart`),
`signals_snapshot` (plain data for executor jobs), `quiet_periods(now)`. `async_run` does one job: the sun scan of the
history in the executor, once per day and position (a polar night search stays out of the event loop).

Persistence: own store (`state_store.py::SmartPIRoomHistoryStore`; the storage key keeps the suffix `.tar_learning` of
existing installs), `STATE_VERSION = 3`: `{version, position, history, episodes}`. An old v2 or v3 state with learned emitter
lag fields loads history, episodes and position and ignores every learned field; v1 keeps the history only; later versions
ignored. Debounce `HISTORY_SAVE_DELAY_S = 1800`. A home position change forgets period evidence. `reset()` (learning reset
service) clears everything.

Published inside `live.room_model` (`room_model.quiet_periods.{reason,count,last.{start,end,regulated_from}}`,
`data_invalid_reason`, `history_h`; built by `RoomHistoryManager.diagnostics`, called from the room model's diagnostics).

Silent / declining states and the field that says why:
| Situation | Field |
|---|---|
| numpy/scipy missing | `room_model` blocked (dependencies wording), history dropped |
| valve mode | `room_model` blocked (valve wording) |
| no home position / no astral | `room_model.quiet_periods.reason` |
| data currently excluded | `room_model.data_invalid_reason` |


## 4. Room model (`smartpi/room_model/`)

Purpose: identify a model of the room (air, walls, emitter), track its state, forecast the temperature, and
show the wall extra power and the daily energy balance. It is observation only: it never changes the command.

Modules: `model.py` (parameters `a, alpha, beta, c = gamma/beta, b_eq = alpha - c`, exact discretisation;
state Ta, w, d), `observer.py` (3-state Kalman observer, never learns parameters), `identification.py` (output-error
fit, variable projection, residual checks, precision from the Jacobian), `gates.py` (publication rules),
`energy_balance.py` (daily static model), `periods.py`
(adapter to the quiet-period catalogue), `scoring.py` (forecast errors), `emitter.py` (command timeline through
dead time L and first order `tau_e`), `public.py` (plain-wording translation), `manager.py` (orchestration, persistence).

Inputs: regulation tick (indoor temperature with measurement identity, outdoor temperature, `a` online, emitter
in force = HEAT response model (delay theta, first order tau_e) when published, else a first order whose time constant
is the HEAT dead time of `dt_est` after a start without delay, else no window: `heating response delay not known yet`),
applied commands (`on_applied`), room history and quiet-period catalogue.
Observer runs only while SmartPI is in control in heating; power shedding keeps it running with zero heat; open
window pins the air to the measurement; other exclusions pause it. A downtime up to `RM_RESUME_MAX_S = 3 h` keeps
the state; longer restarts it at wall equilibrium and it settles for `RM_SETTLE_S = 3 h`.

Three computations:
1. Identification job (`jobs.py::identify_job`, `manager.py::_identify_job`): one per window of `RM_WINDOW_PERIODS = 3` closed quiet periods,
   keyed by its last period, compared with the 3 periods before, at most one job per `_JOB_MIN_GAP_S` (1 h) of elapsed
   time, oldest first. Periods whose mean observer disturbance deviates more than `RM_NIGHT_D_DEV_C_H = 0.08`
   C/h from the recent median (>= `RM_NIGHT_MIN_TRACK_S = 4 h` tracked) are excluded from the windows.
   Emitter search on a fixed grid `RM_EMITTER_GRID_S` (13 values, 60 s to 3600 s) refined by Brent; significance
   `RM_EMITTER_CHI2 = 3.841`; each tried first moment (grid point or Brent evaluation) is fitted in two shapes (`jobs.py::_best_shape`):
   a pure first order, and the split by the delay share theta / (theta + tau_e) of the response model in force
   (`snap["delay_share"]`: L = share x first moment, the rest first order) when that share is > 0; the lower full-fit
   cost is kept and its share (`refine_emitter` out["share"], `rec["emitter"]["share"]`, not published) reaches
   `out["tau_e"]`, `out["L"]`, the window data, the +-2 % neighbours of the precision and the reduced and full fits.
   Without a response model or with a zero share only the pure first order is tried. `snap["te_pub"]` is None: a split
   capped by the response model's tau_e is biased low on fast emitters and breaks the wall fits; the share alone on every
   candidate adds a delay a fast room does not have (measured theta > 0 against a true 0) and the pure first order alone
   loses the real delay of the emitter. Optimum at a grid end with significant profile:
   refused. Not significant: the grid optimum.
2. Energy balance (`energy_balance.py`, `manager.py::_balance_job`): once per local day, `_EB_DELAY_S` (20 min)
   after midnight, whatever the quiet periods; whole local days of the room history (`EB_MAX_DAYS = 7`).
   Day usable with <= 20 % invalid (`EB_MIN_DAY_VALID`), duty >= `EB_MIN_DAY_DUTY = 0.02`, D > 0. Qualification:
   `EB_MIN_DAYS = 4`, mean duty >= `EB_MIN_DUTY = 0.05`, mean D >= `EB_MIN_DELTA_C = 6`, spread
   `EB_MIN_SPREAD_C = 1` to fit the free gain, slope half-width <= 5 %, slope in (0, `EB_MAX_SLOPE`], drift
   rule 2.5 %. Result: slope `s_EB` (`energy_balance_loss_per_c`) and constant free gain `g_EB`. A refused day
   keeps the previous qualified model and records `eb_reason`.
3. Observer (every tick): state, 1/2/6/12 h open-loop forecasts, hourly scored forecast origins (`RM_SCORE_KEEP = 168`).

Publication (`gates.py`, `manager.py::_apply`): a window candidate needs: plausible values, clean residuals
(training and held-out), enough information (95 % interval of each identifiable coordinate within
`RM_AGREE_TOL`), `a` within a factor `RM_A_ONLINE_RATIO_MAX = 2` of the online `a`, agreement with the previous
window, and `b_eq / a` within `RM_EB_AGREE_TOL = ln 1.05` of the energy-balance slope in force. Without a
qualified energy balance nothing is published (`WHY_NO_EB`). Published value = median of the last
`RM_PUB_LAST = 3` candidates of the same structure, re-checked against the slope at publication. Full structure
(walls) needs a precise sensor (`RM_PRECISE_Q` = 0.015 C quantum), wall share >= `RM_WALL_SHARE_MIN = 0.1`, tau_wall >= 3 h and
beating the reduced model on held-out data; otherwise the simple (reduced) model.

What it influences: nothing in the command (owner decision, 26 November 2026). The holding power is always the learned
`b / a` (`smartpi/feedforward.py::compute_ff`); the model published, the energy balance in force and the observer's
wall extra power are never read by `algo.py`, the gains, the anticipation, the setpoint manager or the guards. The
former selection (`select_source`, static part `s_EB (SP - Text) - g_EB`, wall extra power `delta_wall / a`,
bumpless transfer and wall blend, `ff_source` / `ff_wall` in the saved state) is removed; an old saved state with those
keys loads and ignores them. `feedforward.source` is always `learned model`; `room_model.used_by_regulation` is
`false`. The energy balance stays as the reference that the fits are checked against (`gates.py::eb_agrees`) and as a
published diagnostic.

Published, read by nothing in the regulation: `used_by_regulation`, `forecast_c`, `forecast_error_c`, `forecast_checks`,
`unchanged_temperature_error_c`, `a`, `b_eq`, `b_air`, `b_wall`, `wall_extra_power_percent`, `energy_balance_*`, `identified_day`, `last_identification_day`,
`sensor_resolution_c`. The recorded history series also keep the feed-forward source, `a`, `b_eq`
and the 1 h / 12 h forecast errors (`smartpi/diagnostic_history.py`).

Persistence: own store (`SmartPIRoomModelStore`, key suffix `.room_model`), `STATE_VERSION = 2` (published params
and candidates, windows and records keyed by last period, observer state with period disturbances and waiting
sub-steps, energy balance in force with last day computed, last job time, restart context). v1 keeps published
model, observer physical state, energy balance; windows and candidates dropped and recomputed. Debounce
`RM_SAVE_DELAY_S = 1800`.

Published at `live.room_model` (`_build_room_model` -> `RoomModelManager.diagnostics`); plain wording through
`room_model/public.py`.

Silent / declining states:
| Situation | Field |
|---|---|
| nothing published (waiting, refused, blocked) | `status` (`learning`, `not identified: <reason>`), `last_refusal_reason` |
| blocked by valve mode or missing dependencies | `last_refusal_reason` (dependencies / valve wording) |
| no qualified energy balance yet or day refused | `energy_balance_refusal_reason` (null when it gave one); loss/gain fields stay null |
| window waits for a second series | `last_refusal_reason = waiting for a second series of quiet periods to confirm` |
| model with walls not usable | `walls` (`not identified: temperature sensor too coarse / no thermal mass detected / not confirmed yet / no better than the simple model`) |
| tracking not `ok` | `tracking` (`waiting for an identified model`, `paused (...)`, `settling after a restart`, `measurements drift away from the model`, ...); `wall_extra_power_percent = null` |
| computation failed | `last_error` |
Typical outcome with short setbacks and heavy walls: the model stays unpublished.

## 5. Power steps of the a/b learning (`smartpi/power_steps.py`, class `PowerSteps`)

Purpose: track active episodes (step from rest, constant within `RESPONSE_LAG_U_TOL = 0.05`), heating phases (cuts
< `RESPONSE_LAG_PHASE_MIN_OFF_S` join), the last 7 durations, the delivered power (theta then tau_e of the mode's
response model) and the settling gates (waits in `smartpi/response_model.py`; formula in `algorithm-core.md`).
No estimation of its own. Persistence: `lw_state["power_steps"]` (an old `response_lag` block loads its durations
and episodes), restart context with the delivered power and temperatures at the cut. No published block.

## 6. Restart bridge and resume hold

Restart bridge (`smartpi/restart_bridge.py::decide`, `RestartBridge`, `command_holdable`): one decision, taken at the
first calculation of a ready thermostat, whether a stop is bridged. All must hold: downtime (wall clock, from the
oldest last tick of the three stores) <= `RESTART_BRIDGE_MAX_GAP_S = 600 s`; same HVAC mode (HEAT or COOL) and
cycle configuration; actuator state saved (committed power; in HEAT also heater state of the room history and last
tick of the room model); SmartPI in control before and after (no window, shedding, safety, override); first
indoor temperature passes the jump rule of the room history; no clock jump. Learning histories (room history, room model)
bridge only in HEAT (`RestartDecision.heat_bridged`).
- Hold of the last command while indoor temperature is missing: `SmartPI._restart_held_command` keeps the
  committed command of before the stop while the restart is pending, as long as `command_holdable` stays true
  (stop plus wait <= 600 s). Past that the output goes to 0 until the temperature comes and the restart is
  not bridged (`indoor temperature unknown`). The heater cut during the wait is the reason it cannot be bridged.
- Consumers (`SmartPI._apply_restart_decision`): dead-time detection, learning window (with the event detector),
  power steps, bootstrap hysteresis output, room history, room model resume (bridged) or take the
  conservative path (other restart). Component behaviour table: technical_doc.md section 5.4 "Restarts".
- Persistence: context of the last tick and committed power in the algorithm store (`restart`), plus last ticks of
  the two learning stores.
- Diagnostic: `control.last_restart` (`bridged`, `downtime_s`, `reason`). Null before the first tick or without
  saved state. Not bridged is not an error: components restart carefully and only the downtime is excluded.

Resume hold (`smartpi/resume_hold.py::ResumeDeadtimeHold`): a different mechanism, not a restart feature.
After a window or power-shedding resume (`ResumeHoldSource.WINDOW_RESUME`, `POWER_SHEDDING_RESUME`) it starts an
integral hold (`start_integral_hold`) covering the effective dead time of the resumed episode (`hold_s`: the
response model's theta + tau_e/2, else the reliable active dead time), so the integral does not accumulate while
the emitter has not answered. It ends when that window ends, when no effective dead time exists, or immediately when the residual error is already within the recovery floor. Not
persisted (cleared on reset). The hold
source is reported as `control.integral_hold_source` / `integral_hold_mode`.

## Design stance (project rules, user decisions)

- Unmeasured disturbances (sun, occupants) are handled by identifying and EXCLUDING the affected windows
  (darkness-only quiet periods, observer-detected disturbed periods, unconfirmed comfort steps), never by adding
  terms to the fits.
- Never ask the user to declare occupancy schedules and never hard-code sleep/night clock spans. Quiet periods
  come from data (sun position from the home position, setpoint timeline, valid data). Local time is only used
  for display dates and for the whole-day boundaries of the energy balance.
- SmartPI never judges a sensor stale: Versatile Thermostat owns last-seen and safety handling. The learning
  data-gap rules only mark learning intervals invalid; they do not alter the regulation.
- A wrong published value is worse than a silent estimator: when a gate cannot be evaluated the result is
  unconfirmed / unpublished, and a diagnostic field gives the plain reason.
- Target sensors are precise (0.01 C, update on change plus a short heartbeat); 0.1 C on-change sensors are
  supported best-effort (the room model drops the wall structure on a coarse sensor).

## Pointers

- User docs: `documentation/en/vtherm_smartpi.md` ("Dead time and emitter lag", "Room model", "Restarts").
- Diagnostics reference: `documentation/en/diagnostics.md` (`room_model`, `feedforward`,
  `control.last_restart`, `analysis.learning.*`).
- Constants: `smartpi/const.py` (emitter lag, measurement events `EVENT_*`, anticipation, restart bridge),
  `room_history/const.py`, `room_model/const.py`, `quiet_periods/const.py`. These are method parameters, not
  configuration knobs.
