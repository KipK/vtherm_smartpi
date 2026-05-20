# SmartPI Diagnostics Documentation

**SmartPI** provides extensive diagnostic attributes to monitor the online identification of the thermal model, safety-first governance, setpoint trajectories, and feed-forward loops.

These diagnostics are published at the root of the attributes of the SmartPI diagnostic sensor entity in Home Assistant.

See also: [SmartPI user documentation](./vtherm_smartpi.md).

---

## 1. Diagnostics Modes

SmartPI operates in two diagnostic modes configured via the **SmartPI debug mode** setting:

- **Normal Mode (Default)**: Publishes only the **Essential Keys** (`ESSENTIAL_KEYS`) to keep the payload clean and reduce database overhead.
- **Debug Mode**: Publishes the complete, unfiltered dictionary with detailed internal tracking, including the advanced **`debug`** block containing the full raw variables and the predictive **`pred`** sub-block when the thermal twin is active.

---

## 2. Normal Mode Diagnostics (Essential Keys)

These attributes are always published by the SmartPI integration, regardless of the debug mode setting.

| Attribute | Type | Category | Description |
| :--- | :--- | :--- | :--- |
| `phase` | `string` / `Enum` | General | Current SmartPI operating phase (`HYSTERESIS` during bootstrap, `SMARTPI` during active regulation). |
| `regulation_mode` | `string` | General | Current regulation mode mapping (`hysteresis` or `smartpi`). |
| `hysteresis_state` | `string` | General | State of the hysteresis controller (e.g., HVAC demand state like ON/OFF). |
| `on_percent` | `float` | Command | Computed output duty cycle / command percentage (0.0 to 1.0) for the next cycle. |
| `error` | `float` | Temperature | Current temperature error (`Setpoint - Indoor Temperature`) in °C. |
| `a` | `float` | Model | Estimated heating thermal gain ($a$ in the room's physical model). |
| `b` | `float` | Model | Estimated room heat-loss coefficient ($b$ in the room's physical model). |
| `u_pi` | `float` | Command | Proportional-Integral contribution of the control command (0.0 to 1.0). |
| `u_ff` | `float` | Command | Total feed-forward contribution of the control command (0.0 to 1.0). |
| `u_hold` | `float` | Command | Frozen command value applied when the output is locked or held. |
| `Kp` | `float` | PI Gains | Calculated proportional gain ($K_p$) currently applied. |
| `Ki` | `float` | PI Gains | Calculated integral gain ($K_i$) currently applied. |
| `integral_error` | `float` | PI State | Accumulated integral error (internal state of the I branch). |
| `governance_regime` | `string` | Safety | Active safety-first governance regime (e.g. `WARMUP`, `EXCITED_STABLE`, `NEAR_BAND`, `DEAD_BAND`, `SATURATED`, `HOLD`, `PERTURBED`, `DEGRADED`). |
| `last_decision_thermal` | `string` | Safety | Governance decision applied to thermal model updates (e.g. `ADAPT_ON`, `FREEZE`, `HARD_FREEZE`). |
| `bootstrap_progress` | `float` | Bootstrap | Completion percentage of the bootstrap learning phase (only present in the `HYSTERESIS` phase). |
| `bootstrap_state` | `string` | Bootstrap | Current sub-state of the bootstrap learning process. |
| `a_drift_state` | `string` | Drift | State of the drift detector for parameter $a$. |
| `b_drift_state` | `string` | Drift | State of the drift detector for parameter $b$. |
| `a_drift_buffer_count` | `int` | Drift | Number of items in the drift-detection buffer for parameter $a$. |
| `b_drift_buffer_count` | `int` | Drift | Number of items in the drift-detection buffer for parameter $b$. |
| `a_drift_last_reason` | `string` | Drift | Reason for the last drift state change or update for parameter $a$. |
| `b_drift_last_reason` | `string` | Drift | Reason for the last drift state change or update for parameter $b$. |
| `deadtime_heat_s` | `float` | Model | Estimated heating reaction delay (dead time) in seconds. |
| `deadtime_cool_s` | `float` | Model | Estimated cooling reaction delay (dead time) in seconds. |
| `autocalib_last_trigger_ts` | `string` | AutoCalib | ISO timestamp of the last automatic calibration execution. |
| `autocalib_next_check_ts` | `string` | AutoCalib | ISO timestamp of the next scheduled check by the autocalibration supervisor. |
| `autocalib_snapshot_age_h` | `float` | AutoCalib | Elapsed time in hours since the baseline model snapshot was captured. |
| `sensor_temperature` | `float` | Temperature | Raw indoor temperature measured and used in the last calculation cycle. |
| `ext_sensor_temperature` | `float` | Temperature | Raw outdoor temperature measured and used in the last calculation cycle. |
| `t_int_clean` | `float` | Temperature | Clean filtered indoor temperature (after adaptive low-pass filtering and spike rejection). |
| `u_ff1` | `float` | Command | Structural feed-forward contribution derived from $b/a$. |
| `u_ff2` | `float` | Command | Slow `FFTrim` bias component of the feed-forward command. |
| `u_ff_final` | `float` | Command | Pre-adjusted total feed-forward value before constraints. |
| `u_ff3` | `float` | Command | Bounded short-horizon predictive feed-forward component. |
| `u_db_nominal` | `float` | Command | Estimated command required to maintain equilibrium inside the deadband. |
| `u_ff_eff` | `float` | Command | Effective total feed-forward applied to the controller. |
| `ff3_enabled` | `boolean` | FF3 | Boolean flag indicating if the short-horizon predictive correction (FF3) is active. |
| `ff3_reason_disabled` | `string` | FF3 | User-friendly reason explaining why FF3 is disabled (`none` when active). |
| `ff3_raw_reason_disabled` | `string` | FF3 | Internal raw status code explaining why FF3 is disabled. |
| `ff3_horizon_cycles` | `int` | FF3 | Prediction horizon used by FF3 in control cycles. |
| `ff3_deadtime_cycles` | `int` | FF3 | Estimated dead time in control cycles used for horizon offsets. |
| `ff3_horizon_capped` | `boolean` | FF3 | Indicates if the calculated FF3 horizon was capped by the maximum bounds. |
| `ff3_action_sensitivity` | `float` | FF3 | Estimated thermal slope variation per command unit ($\Delta u$). |
| `ff3_prediction_quality` | `string` | FF3 | Ranked model quality used by FF3 (e.g. `robust` or `degraded`). |
| `ff3_authority_factor` | `float` | FF3 | Safety factor reducing the maximum permitted authority of FF3 corrections. |
| `twin_status` | `string` | Twin | Status of the 1R1C thermal twin model (`ok` or `unavailable`). |
| `ff3_twin_usable` | `boolean` | Twin | Indicates if the thermal twin has warmed up and is reliable for predictive correction. |
| `ab_confidence_state` | `string` | Model | Overall confidence state of $a$ and $b$ identification (e.g. `AB_BOOTSTRAP`, `AB_OK`, `AB_BAD`). |
| `deadband_power_source` | `string` | Deadband | Power source used during the deadband state. |
| `deadband_p_mode` | `string` | Deadband | Proportional branch calculation mode applied in the deadband. |
| `ff2_trim_delta` | `float` | Command | Slow feed-forward trim delta correction (`FFTrim`). |
| `fftrim_last_reject_reason` | `string` | Command | Reason why the last slow trim update was rejected. |
| `fftrim_last_update_reason` | `string` | Command | Reason why the last slow trim update was accepted. |
| `fftrim_cycles_since_update` | `int` | Command | Number of control cycles elapsed since the last `FFTrim` update. |
| `integral_hold_active` | `boolean` | PI State | Indicates if the integral branch accumulator is currently frozen/held. |
| `integral_hold_mode` | `string` | PI State | Active mode or reason for the integral freeze (e.g. `window_hold`, `deadband_hold`). |
| `restart_reason` | `string` | General | Cause of the last algorithm or integration restart. |
| `filtered_setpoint` | `float` | Setpoint | Dynamic reference temperature followed by the proportional branch (P branch). |
| `setpoint_trajectory_active` | `boolean` | Setpoint | Indicates if the analytical proportional setpoint trajectory is active. |

---

## 3. Debug Mode Diagnostics (Additional Full Keys)

When **SmartPI debug mode** is enabled, all normal mode attributes are accompanied by the nested **`debug`** block. This block publishes the full unfiltered state of the controller.

### 3.1 Advanced Model & Learning Parameters

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `tau_min` | `float` | Minimum allowable identified time constant ($\tau_{min}$) in minutes. |
| `tau_reliable` | `boolean` | Indicates if the estimated time constant ($\tau = 1/b$) is considered reliable. |
| `learn_ok_count` | `int` | Session-relative count of successful parameter learning events. |
| `learn_ok_count_a` | `int` | Number of validated heating measurements added to the history of parameter $a$. |
| `learn_ok_count_b` | `int` | Number of validated cooling measurements added to the history of parameter $b$. |
| `learn_skip_count` | `int` | Session-relative count of skipped learning episodes. |
| `learn_last_reason` | `string` | Detailed text status / reason from the last parameter update attempt. |
| `learn_b_converged` | `boolean` | Indicates if the loss coefficient $b$ has statistically converged (gating $a$ learning). |
| `learn_a_blocked_by_b` | `boolean` | Indicates if $a$ learning is gated because $b$ has not achieved convergence. |
| `diag_dTdt_method` | `string` | Numerical method applied to compute the temperature derivative (e.g. `OLS`). |
| `diag_b_mad_over_med` | `float` | Relative Median Absolute Deviation to Median of $b$ history. |
| `diag_a_mad_over_med` | `float` | Relative Median Absolute Deviation to Median of $a$ history. |
| `diag_ab_bootstrap` | `boolean` | Indicates if bootstrap identification of $a$ and $b$ is currently running. |
| `diag_ab_points` | `int` | Number of points currently held in the estimators. |
| `diag_ab_mode_effective` | `string` | Active mathematical estimation/aggregation mode (e.g. `median`). |
| `learning_start_dt` | `string` | ISO timestamp of the start of the current learning window. |
| `learn_u_avg` | `float` | Average command power inside the active learning window. |
| `learn_u_cv` | `float` | Command coefficient of variation ($u_{cv}$) inside the active learning window. |
| `learn_u_std` | `float` | Command standard deviation ($u_{std}$) inside the active learning window. |

### 3.2 Advanced Control, PI & Error Parameters

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `i_mode` | `string` | Active mode for integral branch error evaluation. |
| `integral_guard_active` | `boolean` | Indicates if the positive integral growth guard is currently triggered. |
| `integral_guard_source` | `string` | Condition or module that triggered the integral growth guard. |
| `integral_guard_mode` | `string` | Active regime of the integral guard. |
| `sat` | `string` | Output saturation flag (`SAT_HIGH`, `SAT_LOW`, or `NONE`). |
| `error_p` | `float` | Proportional error (`Filtered Setpoint - Indoor Temperature`) in °C. |
| `error_filtered` | `float` | Low-pass filtered temperature error in °C. |
| `temperature_slope_h` | `float` | Estimated hourly room temperature slope in °C/h. |
| `near_band_deg` | `float` | Offset boundary defining the near-band region in °C. |
| `kp_near_factor` | `float` | Custom gain modifier applied to $K_p$ inside the near-band. |
| `ki_near_factor` | `float` | Custom gain modifier applied to $K_i$ inside the near-band. |
| `sign_flip_leak` | `float` | Leakage coefficient applied to damp the integral state on setpoint flip. |
| `sign_flip_active` | `boolean` | Indicates if the damping logic on setpoint flips is currently active. |

### 3.3 Advanced Outputs & Feed-Forward Parameters

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `ff_raw` | `float` | Raw computed feed-forward command before clamping. |
| `ff_reason` | `string` | Parameter identification context used to scale structural feed-forward. |
| `ff_warmup_ok_count` | `int` | Number of successful cycles completed during feed-forward warm-up. |
| `ff_warmup_cycles` | `int` | Total warm-up cycles executed by the feed-forward scheduler. |
| `ff_scale_unreliable_max` | `float` | Maximum scaling factor allowed for the model under low confidence. |
| `ff2_authority` | `float` | Maximum command authority cap allowed for the slow `FFTrim` correction. |
| `ff2_frozen` | `boolean` | Indicates if slow trim adjustments are frozen. |
| `ff2_freeze_reason` | `string` | Reason for freezing the slow trim update loop. |
| `fftrim_cycle_admissible` | `boolean` | Indicates if the current cycle satisfies all stability criteria for `FFTrim` updates. |
| `u_ff_ab` | `float` | Pure feed-forward command component derived from learned $a$ and $b$. |
| `u_ff_trim` | `float` | Slow bias contribution component calculated by the trim algorithm. |
| `u_ff_base` | `float` | Base feed-forward command prior to trim. |

### 3.4 Advanced FF3 Predictive Parameters

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `ff3_candidate_scores` | `list` | Quadratic scores evaluated for each predictive candidate ($\Delta u$) of FF3. |
| `ff3_selected_candidate` | `float` | Delta command command ($\Delta u$) selected by the predictive solver. |
| `ff3_disturbance_active` | `boolean` | Indicates if an external disturbance is detected by the predictor. |
| `ff3_disturbance_reason` | `string` | Diagnostic reason detailing the disturbance detection mechanism. |
| `ff3_disturbance_kind` | `string` | Disturbance classification (e.g. `gain` or `loss`). |
| `ff3_residual_persistent` | `boolean` | Indicates if a persistent forecast mismatch has been detected. |
| `ff3_dynamic_coherent` | `boolean` | Indicates if observed temperature trends align dynamically with the disturbance hypothesis. |
| `integral_hold_reason` | `string` | Duplicate/alias of `integral_hold_mode`. |
| `signed_error_mode` | `string` | Internal convention mapping error sign to demand (`positive_means_hvac_demand`). |
| `trim_freeze_reason` | `string` | Duplicate/alias of `ff2_freeze_reason`. |
| `regime_prev` | `string` | Governance regime active in the previous calculation cycle. |
| `sat_persistent_cycles` | `int` | Number of continuous cycles the command output has been saturated. |
| `cycles_since_reset` | `int` | Number of cycles elapsed since the integration started or was reset. |
| `calculated_on_percent` | `float` | Raw computed output duty cycle before saturation or rate limits. |
| `committed_on_percent` | `float` | Final committed duty cycle applied to the system for the active cycle. |
| `linear_on_percent` | `float` | Output duty cycle calculated in linear command space (before valve correction). |
| `linear_committed_on_percent` | `float` | Final committed linear duty cycle (before valve curve linearization). |
| `valve_linearization_enabled` | `boolean` | Indicates if the valve curve non-linear linearization is active. |
| `cycle_min` | `float` | Configured duration of a control cycle in minutes. |

### 3.5 Advanced Proportional Setpoint Trajectory & Landing Parameters

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `trajectory_start_sp` | `float` | Initial setpoint temperature before the trajectory began. |
| `trajectory_target_sp` | `float` | Final target setpoint temperature. |
| `trajectory_tau_ref` | `float` | Time constant used as a speed reference to shape the proportional curve. |
| `trajectory_elapsed_s` | `float` | Duration in seconds since the setpoint trajectory started. |
| `trajectory_phase` | `string` | Active sub-phase of the setpoint trajectory (e.g., `braking`, `release`). |
| `trajectory_pending_target_change_braking` | `boolean` | Indicates if a secondary setpoint change occurred during an active trajectory. |
| `trajectory_braking_needed` | `boolean` | Indicates if deceleration is required based on model prediction to prevent overshoot. |
| `trajectory_model_ready` | `boolean` | Indicates if estimated parameters are suitable to calculate the analytical path. |
| `trajectory_remaining_cycle_min` | `float` | Estimated minutes remaining to complete the active trajectory phase. |
| `trajectory_next_cycle_u_ref` | `float` | Proportional reference command calculated for the next cycle. |
| `trajectory_bumpless_u_delta` | `float` | Proportional command offset applied to ensure bumpless handoff. |
| `trajectory_bumpless_ready` | `boolean` | Indicates if the bumpless handoff calculation is valid. |
| `landing_active` | `boolean` | Indicates if the landing cap constraint is currently limiting the command. |
| `landing_reason` | `string` | Diagnostic reason explaining why the landing cap is active or bypassed. |
| `landing_u_cap` | `float` | Maximum linear command limit calculated to keep the temperature below the landing target. |
| `landing_sp_for_p_cap` | `float` | Setpoint limit used to cap the proportional command. |
| `landing_predicted_temperature` | `float` | Temperature predicted at the end of the landing horizon if heating stops. |
| `landing_predicted_rise` | `float` | Total estimated temperature rise during the landing phase in °C. |
| `landing_target_margin` | `float` | Margin relative to target setpoint used in safe landing calculations. |
| `landing_release_allowed` | `boolean` | Indicates if the landing supervisor allows exiting the constraint. |
| `landing_coast_required` | `boolean` | Indicates if the heating system must coast (minimum command) to avoid overshoot. |
| `landing_non_constraining_count` | `int` | Number of cycles where the landing constraint was inactive. |
| `landing_time_to_target_min` | `float` | Estimated minutes remaining until target setpoint is reached. |
| `landing_release_blocked_by_slope` | `boolean` | Indicates if exit from landing is blocked due to an excessively high slope. |
| `landing_u_cmd_before_cap` | `float` | Computed command before applying the landing cap. |
| `landing_u_cmd_after_cap` | `float` | Final command after applying the landing cap. |

### 3.6 Advanced Protections & Timers

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `learning_resume_ts` | `int` | Monotonic timestamp when the pause following a reset or resume ends. |
| `u_cmd` | `float` | Raw computed output from the loop controller. |
| `u_limited` | `float` | Command value after applying maximum change limits. |
| `u_applied` | `float` | Command value committed to the underlying climate entities. |
| `aw_du` | `float` | Delta command difference used by the anti-windup tracking mechanism. |
| `forced_by_timing` | `boolean` | Indicates if calculations were forced by cycle timeout limits. |
| `in_deadband` | `boolean` | Indicates if the indoor temperature is within the configured deadband. |
| `in_core_deadband` | `boolean` | Indicates if the temperature is within the narrow core deadband. |
| `in_near_band` | `boolean` | Indicates if the temperature is within the near-band. |
| `setpoint_boost_active` | `boolean` | Indicates if setpoint boost/pre-heating is currently running. |
| `hysteresis_thermal_guard` | `boolean` | Indicates if the hysteresis safety guard is active on setpoint decrease. |
| `deadtime_heat_reliable` | `boolean` | Indicates if the estimated heating dead time is reliable. |
| `deadtime_cool_reliable` | `boolean` | Indicates if the estimated cooling dead time is reliable. |
| `in_deadtime_window` | `boolean` | Indicates if the controller is in a dead-time blanking window after a command change. |
| `kp_source` | `string` | Heuristic source used for $K_p$ gain scheduling (`heuristic`, `imc`, or `safe`). |
| `deadtime_skip_count_a` | `int` | Number of samples for $a$ skipped because they fell inside the heating dead time. |
| `deadtime_skip_count_b` | `int` | Number of samples for $b$ skipped because they fell inside the cooling dead time. |
| `deadtime_state` | `string` | Internal state of the DeadTimeEstimator state machine. |
| `deadtime_last_power` | `float` | Output command recorded at the start of the active dead time test. |
| `deadtime_heat_start_time` | `float` | Monotonic timestamp when dead time heat calculation started. |
| `deadtime_cool_start_time` | `float` | Monotonic timestamp when dead time cool calculation started. |
| `near_band_below_deg` | `float` | Dynamic near-band lower offset in °C. |
| `near_band_above_deg` | `float` | Dynamic near-band upper offset in °C. |
| `near_band_source` | `string` | Source of near-band calculation (`auto` or `config`). |
| `guard_cut_active` | `boolean` | Indicates if `guard_cut` protection is currently limiting the command. |
| `guard_cut_count` | `int` | Number of times `guard_cut` has triggered in the current session. |
| `guard_kick_active` | `boolean` | Indicates if `guard_kick` protection is currently active. |
| `guard_kick_count` | `int` | Number of times `guard_kick` has triggered in the current session. |
| `calibration_state` | `string` | State of the forced-calibration FSM (`idle`, `cool_down`, `heat_up`, `final_cool_down`). |
| `last_calibration_time` | `string` | ISO timestamp of the last forced calibration. |
| `calibration_retry_count` | `int` | Count of retries for the forced-calibration sequence. |
| `autocalib_state` | `string` | State of the automatic calibration supervisor state machine. |
| `autocalib_waiting_reason` | `string` | supervisor waiting reason explaining why autocalibration has not triggered. |
| `autocalib_model_degraded` | `boolean` | Indicates if model performance has degraded to a degree requiring recalibration. |
| `autocalib_triggered_params` | `list` | List of parameters that exceeded limits and triggered autocalibration. |
| `autocalib_retry_count` | `int` | Count of retries executed by the autocalibration supervisor. |
| `autocalib_dt_cool_unavailable` | `boolean` | Indicates if autocalibration fallback triggered due to missing cool dead time. |
| `governance_cycle_regimes` | `list` | Governance regimes observed during the active control cycle. |
| `last_freeze_reason_thermal` | `string` | Governance reason for freezing thermal model updates. |
| `last_freeze_reason_gains` | `string` | Governance reason for freezing gain updates. |
| `last_decision_gains` | `string` | Governance decision applied to gain schedules. |
| `boost_active` | `boolean` | Duplicate/alias of `setpoint_boost_active`. |
| `t_int_raw` | `float` | Raw indoor temperature reading before filtering. |
| `t_int_lp` | `float` | Exponential low-pass filtered value of the indoor temperature. |
| `sigma_t_int` | `float` | Calculated standard deviation (noise measurement) of the temperature sensor. |
| `adaptive_tint_update` | `boolean` | Indicates if the adaptive filter published an update in the current cycle. |
| `adaptive_tint_hold_duration_s`| `float` | Duration in seconds the filter has held a steady value. |

---

## 4. Thermal Twin Predictive Diagnostics (`pred` Block)

When the **1R1C Thermal Twin** is active and usable, a nested **`pred`** block is added to the debug diagnostics dictionary.

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `twin_status` | `string` | Status of the twin model (`ok` or `unavailable`). |
| `twin_T_hat` | `float` | One-step-ahead temperature prediction in °C. |
| `twin_T_pred` | `float` | Long-horizon projected temperature in °C. |
| `twin_innovation` | `float` | Mismatch between projected temperature and observed value (Kalman innovation). |
| `twin_rmse_30` | `float` | Root Mean Square Error evaluated over a 30-cycle sliding window. |
| `twin_model_reliable` | `boolean` | Indicates if twin parameters match the physical constraints. |
| `twin_perturbation_dTdt` | `float` | Slope perturbation representing unmodeled external influences. |
| `twin_cusum_pos` | `float` | Cumulative positive sum (detects unmodeled heat gains). |
| `twin_cusum_neg` | `float` | Cumulative negative sum (detects unmodeled heat losses). |
| `twin_external_gain` | `boolean` | Indicates if unmodeled heat gain has been detected. |
| `twin_external_loss` | `boolean` | Indicates if unmodeled heat loss has been detected. |
| `twin_T_steady` | `float` | Steady-state temperature calculated under current conditions. |
| `twin_T_steady_reliable`| `boolean` | Indicates if steady-state prediction is reliable. |
| `twin_T_steady_max` | `float` | Maximum steady-state temperature reachable at full power. |
| `twin_T_steady_immediate`| `float` | Estimated immediate steady-state temperature without thermal lag. |
| `twin_T_steady_passive` | `float` | Passive steady-state temperature (unheated equilibrium). |
| `twin_setpoint_reachable`| `boolean` | Indicates if the active setpoint is reachable under current conditions. |
| `twin_setpoint_reachable_max`| `boolean` | Indicates if the setpoint is reachable at maximum heating command power. |
| `twin_emitter_saturated`| `boolean` | Indicates if the heating emitter is fully saturated. |
| `twin_cooling_model_available`| `boolean` | Indicates if a cooling model parameters are available. |
| `twin_d_hat_fresh` | `boolean` | Indicates if the unmodeled disturbance estimation is fresh. |
| `twin_warming_up` | `boolean` | Indicates if the twin's observer is warming up. |
| `twin_u_eff` | `float` | Effective duty cycle command used in twin equations. |
| `twin_deadtime_s` | `float` | Transit dead time used by the thermal twin in seconds. |
| `twin_dead_steps` | `int` | Number of discrete delay steps used in the twin observer. |
| `twin_T_hat_error` | `float` | Tracking error of the twin observer. |
| `twin_rmse_pure` | `float` | Pure prediction RMSE error without Kalman filter updates. |
| `twin_innovation_bias`| `float` | Running bias estimate of the prediction mismatch. |
| `twin_bias_warning` | `boolean` | Indicates if a warning is active for high prediction bias. |
| `twin_auto_reset_triggered`| `boolean` | Indicates if the twin observer was reset due to divergence. |
| `twin_reset_count` | `int` | Total number of observer resets executed. |
| `eta_s` | `float` | Estimated time (in seconds) remaining to hit setpoint target. |
| `eta_u` | `float` | Average heating command expected during the ETA period. |
| `eta_reason` | `string` | Diagnostic reason / status code for the ETA calculation results. |
| `twin_d_hat` | `float` | Exponential moving average (EMA) of the estimated thermal disturbance. |

---

## 5. Home Assistant Published Summary Structure

Home Assistant uses a structured summary under the attribute **`specific_states.smart_pi`** of the thermostat entity. This structure is mapped directly from the raw diagnostics to provide cards (such as the Equinox dashboard) with high-level parameters.

```json
{
  "control": {
    "phase": "smartpi",
    "mode": "smartpi",
    "hysteresis_state": "idle",
    "kp": 0.85,
    "ki": 0.005,
    "restart_reason": "power_on",
    "saturation_state": "none",
    "in_deadband": false,
    "in_near_band": true,
    "in_deadtime_window": false
  },
  "power": {
    "current_cycle_percent": 35.0,
    "next_cycle_percent": 40.0,
    "linear_current_cycle_percent": 35.0,
    "linear_next_cycle_percent": 40.0,
    "valve_linearization_enabled": false,
    "pi_percent": 25.0,
    "ff_percent": 10.0,
    "hold_percent": 0.0,
    "command_percent": 35.0,
    "limited_percent": 35.0,
    "applied_percent": 35.0
  },
  "temperature": {
    "sensor": 19.5,
    "ext_sensor": 10.2,
    "error": 0.5,
    "integral_error": 50.0,
    "integral_mode": "normal",
    "integral_hold_mode": "none",
    "integral_guard_source": "none"
  },
  "model": {
    "a": 0.0125,
    "b": 0.00035,
    "confidence": "AB_OK",
    "tau_reliable": true,
    "tau_min": 15.0,
    "deadtime_heat_s": 240,
    "deadtime_cool_s": 180,
    "deadtime_heat_reliable": true,
    "deadtime_cool_reliable": true,
    "a_stability_ratio": 0.05,
    "b_stability_ratio": 0.08
  },
  "ab_learning": {
    "stage": "monitoring",
    "bootstrap_progress_percent": 100,
    "bootstrap_status": "done",
    "emea_samples_a": 15,
    "emea_samples_b": 18,
    "bootstrap_target_a": 6,
    "bootstrap_target_b": 8,
    "history_target": 31,
    "accepted_updates_a": 15,
    "accepted_updates_b": 18,
    "learn_b_converged": true,
    "accepted_samples_a": 15,
    "accepted_samples_b": 18,
    "target_samples": 31,
    "last_reason": "ols_robust_success",
    "a_drift_state": "stable",
    "b_drift_state": "stable"
  },
  "governance": {
    "regime": "excited_stable",
    "thermal_update_decision": "adapt_on",
    "thermal_update_reason": "excited_stable"
  },
  "feedforward": {
    "ff3_status": "active",
    "ff3_twin_usable": true,
    "twin_status": "ok",
    "deadband_power_source": "none",
    "deadband_p_mode": "none"
  },
  "setpoint": {
    "filtered_setpoint": 20.0,
    "trajectory_active": false,
    "trajectory_source": "none",
    "landing_active": false,
    "landing_reason": "none",
    "landing_u_cap": null,
    "landing_coast_required": false
  },
  "autocalib": {
    "state": "idle",
    "model_degraded": false,
    "last_trigger_ts": "2026-05-18T12:00:00Z",
    "next_check_ts": "2026-05-23T12:00:00Z",
    "snapshot_age_h": 24.0
  },
  "calibration": {
    "state": "idle",
    "retry_count": 0,
    "last_time": "2026-05-15T08:00:00Z"
  }
}
```
