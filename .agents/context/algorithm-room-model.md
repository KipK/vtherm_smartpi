# SmartPI beyond PI + A/B learning: room-model subsystems

Audience: AI agents with no prior context. Package root:
`custom_components/vtherm_smartpi/smartpi/` (written `smartpi/` below). Sources of truth: the code,
`documentation/en/technical_doc.md` (sections 3.6-3.8, 4.2, 5.4, 5.5), `documentation/en/diagnostics.md`
and `CHANGELOG.md` ([Unreleased]). References are `path::symbol`, without line numbers.

## 0. Map and what steers the regulation

| Subsystem | Code | Regulation reads it? |
|---|---|---|
| Emitter delay (T_ar) learning | `smartpi/tar_learning/` | Yes (HEAT, PWM only): dead time used by gains, near band, I-hold, FF3, twin |
| Quiet periods | `smartpi/quiet_periods/` | Indirectly: selects the data of the two learnings above and below |
| Room model (identify, observer, energy balance) | `smartpi/room_model/` | Partly: holding power (FF1). Forecasts, forecast errors, `a`/`b_eq` display: no |
| Response lag | `smartpi/response_lag.py` | Yes: delivered-power model for the `a` learning |
| Heating balance | `smartpi/heating_balance.py` | Yes, one value: the median kept lag is the HEAT horizon of the anticipation (`smartpi/anticipation.py`); `a` and the other values are observation only |
| Restart bridge | `smartpi/restart_bridge.py` | Yes: decides resume vs conservative restart for every component |
| Resume hold | `smartpi/resume_hold.py` | Yes: temporary I-hold after window / power-shedding resume; `recovery_error_floor_c` (residual error under which a recovery counts as closed) |
| Job abort | `smartpi/job_abort.py` | Plumbing for executor jobs |
| Thermal measurement | `smartpi/thermal_measurement.py` | Plumbing for the FF trim coordinator only |
| Lazy numpy/scipy | `smartpi/tar_learning/deps.py` | Gate: without them nothing is learned |

Wiring: `algo.py::SmartPI` owns `tar_learning: TarLearningManager` and `room_model: RoomModelManager` (the
room model needs the tar manager: it reads its history, catalogue, status). `handler.py` creates both,
loads their stores, injects the home position (`TarLearningManager.set_position`) and the dependency check
(`set_dependencies`), starts the executor tasks (`_process_tar_learning`, `_process_room_model`) and schedules
saves. Observers are called from the regulation tick (`SmartPI.observe_tar_learning`, `observe_room_model`),
wrapped in try/except: a learning failure must never disturb the regulation.

Both managers are HEAT-only. PWM heaters only: in valve mode `TAR_VALVE_MODE_SUPPORTED = False`
(`tar_learning/const.py`), tar status becomes `valve_mode_not_supported` and the room model is blocked
(`room_model/manager.py::RoomModelManager._blocked_reason`). COOL is never learned nor applied.

## 1. Executor jobs, abort, lazy dependencies

- `tar_learning/jobs.py` and `room_model/jobs.py` take a snapshot of plain Python data and return plain data;
  they never touch event-loop state. `async_run` runs one job at a time via `hass.async_add_executor_job`.
- `numpy`/`scipy` (manifest: `numpy`, `scipy>=1.14`) are imported only inside those jobs and the numeric modules
  they call (`tar_learning/morning.py`, `response.py`, `room_mass.py`, `identification.py`, `signals.py`,
  `room_model/identification.py`). Event-loop code is pure Python (`room_model/linalg.py` is hand-written for
  the 3-state observer). `tar_learning/deps.py::dependencies_importable` only uses `importlib.util.find_spec`.
- The solar position uses `astral` (shipped with Home Assistant): `quiet_periods/solar.py::astral_available`.
- `job_abort.py`: a started job cannot be cancelled. Objective functions call `job_abort.check()`; the manager's
  `stop()` sets a `threading.Event` attached to the executing thread (`run`), so several thermostats never
  interfere. `JobAborted` ends the job silently (no state change).
- Failure handling in `async_run`: `ImportError` -> `set_dependencies(False)` (status `dependencies_unavailable`);
  any other exception -> `last_error` ("the calculation failed, see the log"), the item is marked failed; a
  result whose `_generation` changed (reset, new home position) while running is discarded.

## 2. Quiet periods (`smartpi/quiet_periods/`)

Purpose: select stretches without sun, at constant setpoint, with valid data, shared by the T_ar learning and
the room model. Pure Python, no Home Assistant import. Times are epoch seconds, half-open `[start, end)`.

- Inputs: home latitude/longitude (HA config, validated by `solar.py::validate_position`), setpoint timeline,
  valid spans of the T_ar history (`TarHistory.valid_spans`), heater starts, indoor temperatures.
- Darkness (`solar.py::dark_intervals`): sun centre below `DARK_ELEVATION_DEG = -0.833 - 0.5` degrees, sampled
  every `SUN_SAMPLE_S = 600`, boundaries refined to 1 s, moved inward by `DARK_TIME_MARGIN_S = 120`. Polar night
  is cut into `DARK_BLOCK_S = 24 h` blocks; polar day has no darkness. No margin is taken after sunrise.
- Episode (`selection.py::build_episodes`): at most one per darkness: the longest part of a setback run inside
  it, or the whole darkness when the setpoint never changes. A rise that stays below the pre-drop setpoint
  while the room is still above the new setpoint does not end the run.
- Selected component: longest valid plateau piece, at least `PERIOD_MIN_S = 1 h`.
- Regulated start (`selection.py::regulated_start`): first heater start in the component with the room within
  `REGULATED_ECO_TOL_C = 0.3` of the setpoint; starts within `RESTART_START_GUARD_S = 60` after a restart do
  not count. None means the room never regulated.
- Identification interval: the component continued after the ending rise, while dark and valid, for at most
  `IDENT_TAIL_S = 2 h`.
- Key: `q<SELECTION_VERSION>:<origin>:<component start>`, 0.1 s resolution (`night_id`). The catalogue is rebuilt
  at most every `_CATALOGUE_EVERY_S = 600` (`TarLearningManager._refresh`) and only grows forward (60 episodes
  max), so a key never changes when the history is pruned. Every piece of evidence is keyed by its period: a
  period counts once. States: closed (end <= last observation), settled (identification interval known), complete
  (history covers it from its start).
- Outputs: catalogue consumed by tar learning (`TarLearningManager.quiet_periods`) and by the room model through
  `room_model/periods.py`.
- Silent states: no valid home position, or `astral` missing -> no darkness, no period, both learnings silent;
  diagnostic `tar_learning.quiet_periods.reason` ("home position unknown: the absence of sun cannot be checked").
  A changed home position forgets the evidence of all periods (`TarLearningManager._check_position`).
  The daily energy balance (room model) does NOT need quiet periods.
- Not a regulation input.

## 3. Emitter delay (T_ar) learning (`smartpi/tar_learning/`)

Purpose: learn the HEAT emitter delay `T_ar` (first moment of the emitter response; about twice the apparent
dead time the bootstrap measures) during normal regulation, and feed a safe value to the gains.

Inputs recorded each tick (`history.py::TarHistory.record`, `applied_cycle`): indoor/outdoor temperature,
setpoint, SmartPI phase, published `b`, reliable cool dead time, response lag, heater edges (PWM cycles
rebuilt from applied cycles), invalid intervals (`TAR_GAP_S = 1200` s without observation, unavailable sensor,
unknown command, non-bridged restart downtime, spikes `TAR_SPIKE_MIN_C = 0.5` C at more than
`TAR_SPIKE_RATE_C_PER_H = 20`, and any tick where SmartPI is not in control: window open, shedding, safety,
HVAC not heating, command overridden). Retention `TAR_HISTORY_RETENTION_S = 7 d`. Invalid intervals are never
filled in. (This data exclusion is about learning data quality only; see Design stance for sensor health.)

Two methods, one policy:

| Method | Module | What it does |
|---|---|---|
| Fast value ("morning" / comfort step) | `morning.py`, `response.py`, `gates.py` | A comfort step is a setpoint rise >= `STEP_MIN_RISE_C = 1` C after a low setpoint held 2 h in the dark, that ends a quiet period. Evaluated `MORNING_EVAL_DELAY_S` after the rise; step-response fit of T with a baseline chosen by room class (`room_mass.py`) and night type (passive / regulated). Fit window ends at the end of darkness; a window cut by sunrise gives an estimate only if >= `TAR_CUT_ESTIMATE_WINDOW_T = 3` x the larger of fitted T and the floor, else a lower bound `TAR_CUT_BOUND_FACTOR = 0.6` x T |
| Multi-day identification ("refined") | `identification.py`, `room_mass.py` | Air-wall model fitted on a window of `ID_WIN_PERIODS = 3` settled periods, compared with the 3 periods before (disjoint). Class `mass` (dBIC >= `ID_DBIC_MASS`), `one` or undetermined. Yields emitter time constant `tau_e`, `k`, `tau_w` |

Gates (`gates.py`, `const.py`):
- Confirmation of a fast value (default `TAR_FAST_GATE = G1`): at least `GATE_IDLE_MIN_H = 1` h without heater
  start in the regulated part before the step; regulated part unknown or under `GATE_REGULATED_MIN_S = 1.5 h`
  leaves the step unconfirmed (unknown is never taken as favourable). GT4 variant also reads `T`, the fitted
  baseline `b` and a heater start in the 2 h before.
- Regulated night (hand-over of the refined value): regulated part >= `REGULATED_MIN_S = 4 h`, constant setpoint,
  a heater start in each 30 min quarter, mean temperature within `RN_ECO_TOL_C = 0.3`, power slope
  `RN_U_SLOPE_MAX_H = 0.05`/h and residual `RN_U_RESID_MAX = 0.10`, no gap. Refined value is published only
  when the six periods of its two windows are regulated; otherwise it is a diagnostic.
- Morning excitation/quality rules in `const.py::RULES` / `RULES_R` (baseline rms, fit rms, window, energy, rise,
  sensor period).

Policy (`policy.py`):
- Fast value: >= `PUB_MIN_ACCEPTED = 3` accepted and confirmed mornings, median of last `PUB_LAST = 5`,
  MAD/median <= `PUB_MAD_REL = 0.25`.
- Lower bound: median of last 5 confirmed cut steps; never enters the fast value, never confirmed by a second method.
- In force: refined value if published, else fast value; raised to the lower bound when that is larger.
- Applied (`policy.py::applied_value`): learned effective dead time `L_T = TAR_GAIN_DEADTIME_FACTOR (0.5) x learned`
  versus reference `L_b` (larger of the first reliable bootstrap HEAT dead time seen since the learning started,
  frozen and stored in `reference_heat_s`, and the current one):
  - `L_T < L_b`: gains keep `L_b` (limit `short`);
  - `L_b <= L_T <= TAR_APPLY_LONG_FACTOR (2.0) x L_b`: gains use `L_T`;
  - above: `L_T` only if fast and window values agree within `TAR_AGREE_TOL = ln 1.25` and the learned source
    is not a lower bound; else `2 x L_b` (limit `long`).
- Without a reliable bootstrap dead time, or when inactive, the bootstrap value stays (`source = bootstrap`).

What it influences (`algo.py::SmartPI.regulation_dead_time`, `gain_dead_time`): in HEAT only, a
`RegulationDeadTime` view replaces the HEAT active dead time. Gains use `gains_s`; near band, I-hold, FF3, twin
and the room-model emitter first moment use `applied_s`. The bootstrap/calibration value is never modified;
`a`/`b` learning, FF trim, calibration and AutoCalib keep the bootstrap value.
Observation only: `refined.last_evaluation.a` and `.b` ("information only, never applied"), `refined.k_per_s`,
`refined.tau_w_h` (the room-model identification does not read them; room class is used inside tar morning
fits), `night_room_class`, `pending_steps`, `history_h`, `fast.last_morning`.

Persistence: own store (`state_store.py::SmartPITarStore`, key suffix `.tar_learning`), `STATE_VERSION = 3`
(history, pending steps, catalogue, night entries, regulated decisions, windows, evaluations, confirmed values,
bounds, publications, frozen reference, home position). v2 read whole; v1 keeps only history and pending rises
(evidence recomputed); later versions ignored. Debounce `TAR_SAVE_DELAY_S = 1800`. Home position change forgets
period evidence. `reset()` (learning reset service) clears everything.

Published at `live.tar_learning` (built by `smartpi/diagnostics.py::_build_tar_learning` ->
`TarLearningManager.diagnostics`). Translation of internal codes to plain text is `tar_learning/public.py`
(room class, morning reasons, unconfirmed reasons, limit reasons); user-facing fields never carry internal codes.

Silent / declining states and the field that says why:
| Situation | Field |
|---|---|
| numpy/scipy missing | `status = dependencies_unavailable` (history dropped; bootstrap value used) |
| before thermostat start | `status = checking_dependencies` |
| valve mode | `status = valve_mode_not_supported` |
| no home position / no astral | `quiet_periods.reason` |
| step gave no value | `fast.last_morning.reason` (plain wording), `accepted = false` |
| accepted step not confirmed | `fast.last_status = unconfirmed`, `fast.last_unconfirmed_reason` |
| refined value not handed over (nights not regulated) | `refined.last_evaluation.regulated_nights = false`; `refined.tau_e_s` stays null |
| learned value not used as is | `out_of_bound = true`, `limit_reason` |
| computation failed | `last_error` |
| data currently excluded | `data_invalid_reason` |

## 4. Room model (`smartpi/room_model/`)

Purpose: identify a model of the room (air, walls, emitter), track its state, forecast the temperature, and
provide the feed-forward holding power with the wall part.

Modules: `model.py` (parameters `a, alpha, beta, c = gamma/beta, b_eq = alpha - c`, exact discretisation;
state Ta, w, d), `observer.py` (3-state Kalman observer, never learns parameters), `identification.py` (output-error
fit, variable projection, residual checks, precision from the Jacobian), `gates.py` (publication rules),
`energy_balance.py` (daily static model), `feedforward.py` (holding power source and transfers), `periods.py`
(adapter to the quiet-period catalogue), `scoring.py` (forecast errors), `emitter.py` (command timeline through
delay L and first order `tau_e`), `public.py` (plain-wording translation), `manager.py` (orchestration, persistence).

Inputs: regulation tick (indoor temperature with measurement identity, outdoor temperature, `a` online, emitter
first moment = HEAT dead time in force), applied commands (`on_applied`), T_ar history and quiet-period catalogue.
Observer runs only while SmartPI is in control in heating; power shedding keeps it running with zero heat; open
window pins the air to the measurement; other exclusions pause it. A downtime up to `RM_RESUME_MAX_S = 3 h` keeps
the state; longer restarts it at wall equilibrium and it settles for `RM_SETTLE_S = 3 h`.

Three computations:
1. Identification job (`jobs.py::identify_job`, `manager.py::_identify_job`): one per window of 3 closed quiet periods,
   keyed by its last period, compared with the 3 periods before, at most one job per `_JOB_MIN_GAP_S` (1 h) of elapsed
   time, oldest first. Periods whose mean observer disturbance deviates more than `RM_NIGHT_D_DEV_C_H = 0.08`
   C/h from the recent median (>= `RM_NIGHT_MIN_TRACK_S = 4 h` tracked) are excluded from the windows.
   Emitter search on a fixed grid `RM_EMITTER_GRID_S` (13 values, 60 s to 3600 s) refined by Brent; significance
   `RM_EMITTER_CHI2 = 3.841`. Optimum at a grid end with significant profile: refused. Not significant: the
   refined T_ar value is used if published, else the grid optimum.
2. Energy balance (`energy_balance.py`, `manager.py::_balance_job`): once per local day, `_EB_DELAY_S` (20 min)
   after midnight, whatever the quiet periods; whole local days of the T_ar history (`EB_MAX_DAYS = 7`).
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
(walls) needs a precise sensor (quantum <= 0.015 C), wall share >= `RM_WALL_SHARE_MIN = 0.1`, tau_wall >= 3 h and
beating the reduced model on held-out data; otherwise the simple (reduced) model.

What it influences (REGULATION):
- `room_model/feedforward.py::select_source` / `static_model`, `algo.py::_select_holding_power`: replaces the
  learned FF1 `b/a` only in HEAT, PWM (not valve), when a model is published AND a qualified energy balance is
  in force AND published `b_eq/a` agrees with `s_EB` within 5 %. Static part `s_EB (SP - Text) - g_EB`, clamped
  to [0, 1] (`FF_GAIN_MAX = 3` on the slope).

  | Source (`feedforward.source`) | Holding power |
  |---|---|
  | `room model (air and walls)` | static + wall extra power `delta_wall / a` (observer health `ok`) |
  | `room model (static part)` | static only (walls identified, tracking not `ok`) |
  | `room model (simple)` | static only |
  | `learned model` | unchanged learned `b/a` (nothing published, no energy balance, disagreement, COOL, valve) |
  The fitted `b_eq/a` itself is never the static loss; the observer's disturbance `d` is not used.
- Static-part discrete changes are moved bumplessly into the integral (`HoldingTransfer.update`); the wall part
  is never moved into the integral, it blends over `RM_WALL_FADE_S = 30 min`.
- While a room-model source is in force FF2 is zeroed and frozen (`ff2_freeze_reason = room_model`).
- `algo.py` also passes `static_model` (slope, free gain) to the setpoint manager as equilibrium of the braking
  prediction (`SmartPISetpointManager.filter_setpoint`).
- Source in force and applied wall part persist with the PI state (`ff_source`, `ff_wall`).

Observation only (read by nothing in the regulation): `forecast_c`, `forecast_error_c`, `forecast_checks`,
`unchanged_temperature_error_c`, `a`, `b_eq`, `b_air`, `b_wall`, `wall_extra_power_percent` as display (the live
wall term inside the observer does feed the holding power), `identified_day`, `last_identification_day`,
`sensor_resolution_c`. The recorded history series also keep the feed-forward source and wall part, `a`, `b_eq`
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
| tracking not `ok` | `tracking` (`waiting for an identified model`, `paused (...)`, `settling after a restart`, `measurements drift away from the model`, ...); `wall_extra_power_percent = null`; source drops to `static part` |
| model published but not in force | `feedforward.source = learned model` while `energy_balance_loss_per_c` is null or differs > 5 % from `b_eq/a` |
| computation failed | `last_error` |
Typical outcome with short setbacks and heavy walls: model stays unpublished and changes nothing.

## 5. Response lag (`smartpi/response_lag.py`, class `ResponseLagEstimator`)

Purpose: measure the intercept `T_ar` of the response on constant-power active episodes (deviation <=
`RESPONSE_LAG_U_TOL = 0.05`) after a passive period >= `max(1800 s, 5 x known lag)`, and expose the settling
gates plus the delivered-power model (pure delay `L = min(active dead time, T_ar)` then first order of `T_ar - L`).
Estimates: last `RESPONSE_LAG_HISTORY = 7`; published when >= `RESPONSE_LAG_MIN_ESTIMATES = 3` and combined
uncertainty <= `RESPONSE_LAG_MAX_REL_UNC = 0.15` of the median (inconsistent sets keep the published value).
Model resolved when lag published or after `RESPONSE_LAG_RESOLVE_EPISODES = 4` heating phases without publication
(then applied power is used).
Regulation: yes. It gates the first publication of `a` (`bootstrap_status = step2 - waiting response lag ...`),
sets the settling delay of `a`/`b` windows and the delivered fraction of power. In PWM regulation constant-power
episodes are rare, so an installation already out of the bootstrap may never publish a lag.
Persistence: in the learning-window state (`lw_state["response_lag"]`), passive period/episode context as
wall-clock times (resumed after a bridged restart; otherwise episode ends "episode interrupted by restart").
Diagnostics: `analysis.learning.response_lag` (`lag_s`, `estimates`, `episodes`, `episodes_target`,
`episode_median_s`, `last_reason`). Silent: `lag_s = null` with `last_reason` saying why (for example
`intercept not resolvable`).

## 6. Heating balance (`smartpi/heating_balance.py`, class `HeatingBalance`)

Measures heating rate `a` (C/min at full power) and emitter lag from an energy balance of each heating phase
followed by a stop, in HEAT, from the same observations as the response lag (`learning_window.py` owns it).
Conditions: rest >= `HEATING_BALANCE_MIN_REST_S = 1800`, baseline of up to 3600 s (>= 4 samples), peak search in
`HEATING_BALANCE_PEAK_BIN_S = 300` bins confirmed after `HEATING_BALANCE_PEAK_CONFIRM_S = 1800`, wait at most
`HEATING_BALANCE_PEAK_MAX_S = 4 h`, kept when full-power time >= 600 s and rise >= 0.3 C. Last
`HEATING_BALANCE_HISTORY = 7` measurements; published values are medians; lag only for constant-power phases.
Regulation consumer: only `kept_lag_s` (median of the last `HEATING_BALANCE_HISTORY` lags, None while none measured), the HEAT
horizon of the anticipation (`smartpi/anticipation.py::select_horizon`); no learning consumer. `a` and `lag_s` are observation only.
Persistence: with the learning state (`lw_state["heating_balance"]`); phase in progress resumes after a bridged
restart, dropped otherwise. A saved state with a published `lag_s` but no kept lag (`lags` missing or empty) keeps that lag as the
single kept lag. Diagnostics: `analysis.learning.heating_balance` (`state`, `measurements`, `a`,
`lag_s`, `kept_lag_s`, `kept_lags`, `last_reason`, `last`). Silent: `a`/`lag_s` null, `last_reason` gives the refusal.

## 7. Restart bridge and resume hold

Restart bridge (`smartpi/restart_bridge.py::decide`, `RestartBridge`, `command_holdable`): one decision, taken at the
first calculation of a ready thermostat, whether a stop is bridged. All must hold: downtime (wall clock, from the
oldest last tick of the three stores) <= `RESTART_BRIDGE_MAX_GAP_S = 600 s`; same HVAC mode (HEAT or COOL) and
cycle configuration; actuator state saved (committed power; in HEAT also heater state of the tar history and last
tick of the room model); SmartPI in control before and after (no window, shedding, safety, override); first
indoor temperature passes the jump rule of the tar history; no clock jump. Learning histories (tar, room model)
bridge only in HEAT (`RestartDecision.heat_bridged`).
- Hold of the last command while indoor temperature is missing: `SmartPI._restart_held_command` keeps the
  committed command of before the stop while the restart is pending, as long as `command_holdable` stays true
  (stop plus wait <= 600 s). Past that the output goes to 0 until the temperature comes and the restart is
  not bridged (`indoor temperature unknown`). The heater cut during the wait is the reason it cannot be bridged.
- Consumers (`SmartPI._apply_restart_decision`): dead-time detection, learning window, FF trim, response lag,
  heating balance, bootstrap hysteresis output, tar history, room model resume (bridged) or take the
  conservative path (other restart). Component behaviour table: technical_doc.md section 5.4 "Restarts".
- Persistence: context of the last tick and committed power in the algorithm store (`restart`), plus last ticks of
  the two learning stores.
- Diagnostic: `control.last_restart` (`bridged`, `downtime_s`, `reason`). Null before the first tick or without
  saved state. Not bridged is not an error: components restart carefully and only the downtime is excluded.

Resume hold (`smartpi/resume_hold.py::ResumeDeadtimeHold`): a different mechanism, not a restart feature.
After a window or power-shedding resume (`ResumeHoldSource.WINDOW_RESUME`, `POWER_SHEDDING_RESUME`) it starts an
integral hold (`start_integral_hold`) covering the active dead time of the resumed episode, so the integral does
not accumulate while the emitter has not answered. It ends when the dead-time window ends, when no reliable
active dead time exists, or immediately when the residual error is already within the recovery floor. Not
persisted (cleared on reset). While active it flags FF-trim windows as `recovery` (`ff_trim_runtime.py`). The hold
source is reported as `control.integral_hold_source` / `integral_hold_mode`.

## 8. Thermal measurement (`smartpi/thermal_measurement.py`)

`ThermalMeasurementSource.observe` returns an immutable `ThermalMeasurement` only for a new distinct VT
measurement identity (a repeated value is not new); `reset` bumps an epoch. Consumer: the FF trim coordinator
(`ff_trim_runtime.py::record_thermal_measurement`). Not used by tar learning or the room model (they have their own
change detection: history change records, observer `meas_key`). No persistence, no diagnostic.

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

- User docs: `documentation/en/vtherm_smartpi.md` ("Emitter delay learning", "Room model", "Restarts").
- Diagnostics reference: `documentation/en/diagnostics.md` (`tar_learning`, `room_model`, `feedforward`,
  `control.last_restart`, `analysis.learning.*`).
- Constants: `smartpi/const.py` (response lag, heating balance, anticipation, restart bridge),
  `tar_learning/const.py`, `room_model/const.py`, `quiet_periods/const.py`. These are method parameters, not
  configuration knobs.
