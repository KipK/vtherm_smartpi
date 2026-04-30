{% set climate_entity = 'climate.test_4_switchs' %}
{% set sensor_entity = 'sensor.smartpi_diagnostics' %}
{% set entity_name = 'Simu' %}

{% set spi = states[sensor_entity].attributes if states(sensor_entity) else none %}

{% if not spi or spi.get('control') is none %}
<ha-alert alert-type="warning">No SmartPI data available for this entity. Check the sensor ID.</ha-alert>
{% else %}

{% set control = spi.get('control', {}) %}
{% set power = spi.get('power', {}) %}
{% set temp = spi.get('temperature', {}) %}
{% set model = spi.get('model', {}) %}
{% set learning = spi.get('ab_learning', {}) %}
{% set gov = spi.get('governance', {}) %}
{% set ff = spi.get('feedforward', {}) %}
{% set setpoint = spi.get('setpoint', {}) %}
{% set autocalib = spi.get('autocalib', {}) %}
{% set calibration = spi.get('calibration', {}) %}
{% set debug = spi.get('debug', {}) %}
{% set has_debug = debug | count > 0 %}

{% set phase = control.get('phase', 'unknown') %}
{% set mode = control.get('mode', 'unknown') %}
{% set hyst_state = control.get('hysteresis_state', '—') %}
{% set restart_reason = control.get('restart_reason', 'none') %}
{% set kp = control.get('kp') %}
{% set ki = control.get('ki') %}
{% set t_in = temp.get('sensor') %}
{% set t_set = state_attr(climate_entity, 'temperature') %}
{% set c_attrs = states[climate_entity].attributes if states(climate_entity) else {} %}
{% set t_ext = c_attrs.get('ext_current_temperature', c_attrs.get('specific_states', {}).get('ext_current_temperature')) %}
{% set error = temp.get('error') %}
{% set integral_error = temp.get('integral_error') %}
{% set integral_mode = temp.get('integral_mode', temp.get('integral_hold_mode', 'none')) %}
{% set hold_mode = temp.get('integral_hold_mode', 'none') %}
{% set integral_guard_source_pub = temp.get('integral_guard_source', 'none') %}

{% set current_cycle = power.get('current_cycle_percent', 0) | float(0) %}
{% set next_cycle = power.get('next_cycle_percent', 0) | float(0) %}
{% set valve_linearization_enabled = power.get('valve_linearization_enabled', debug.get('valve_linearization_enabled', false)) %}
{% set linear_current_cycle = power.get('linear_current_cycle_percent', debug.get('linear_committed_on_percent', current_cycle)) | float(0) %}
{% set linear_next_cycle = power.get('linear_next_cycle_percent', debug.get('linear_on_percent', next_cycle)) | float(0) %}
{% set ff_pct = power.get('ff_percent', 0) | float(0) %}
{% set pi_pct = power.get('pi_percent', 0) | float(0) %}
{% set hold_pct = power.get('hold_percent', 0) | float(0) %}

{% set a = model.get('a') %}
{% set b = model.get('b') %}
{% set ab_conf = model.get('confidence', 'unknown') %}
{% set tau_reliable = model.get('tau_reliable', false) %}
{% set dt_heat = model.get('deadtime_heat_s') %}
{% set dt_cool = model.get('deadtime_cool_s') %}

{% set stage = learning.get('stage', 'unknown') %}
{% set bootstrap_progress = learning.get('bootstrap_progress_percent') %}
{% set bootstrap_status = learning.get('bootstrap_status') %}
{% set samples_a = learning.get('accepted_samples_a', 0) | int(0) %}
{% set samples_b = learning.get('accepted_samples_b', 0) | int(0) %}
{% set target_samples = learning.get('target_samples', 0) | int(0) %}
{% set last_reason = learning.get('last_reason', '—') %}
{% set a_drift = learning.get('a_drift_state', '—') %}
{% set b_drift = learning.get('b_drift_state', '—') %}

{% set regime = gov.get('regime', 'unknown') %}
{% set thermal_decision = gov.get('thermal_update_decision', 'unknown') %}
{% set thermal_reason = gov.get('thermal_update_reason', 'none') %}

{% set ff3_status = ff.get('ff3_status', 'unknown') %}
{% set ff3_twin_usable = ff.get('ff3_twin_usable', false) %}
{% set twin_status = ff.get('twin_status', 'unavailable') %}
{% set deadband_source = ff.get('deadband_power_source', 'none') %}

{% set trajectory_active = setpoint.get('trajectory_active', false) %}
{% set published_filtered_sp = setpoint.get('filtered_setpoint') %}
{% set trajectory_source_pub = setpoint.get('trajectory_source', 'none') %}
{% set landing_active = setpoint.get('landing_active', false) %}
{% set landing_reason = setpoint.get('landing_reason', 'inactive') %}
{% set landing_u_cap = setpoint.get('landing_u_cap') %}
{% set landing_coast = setpoint.get('landing_coast_required', false) %}

{% set autocalib_state = autocalib.get('state', 'unknown') %}
{% set autocalib_degraded = autocalib.get('model_degraded', false) %}
{% set autocalib_last = autocalib.get('last_trigger_ts') %}
{% set autocalib_next = autocalib.get('next_check_ts') %}
{% set autocalib_age = autocalib.get('snapshot_age_h') %}

{% set calibration_state = calibration.get('state', 'unknown') %}
{% set calibration_retry = calibration.get('retry_count', 0) %}
{% set calibration_last = calibration.get('last_time') %}

{% set regime_icon = {
  'excited_stable': '🟢',
  'near_band': '🟡',
  'dead_band': '⚫',
  'warmup': '🔵',
  'hold': '🟣',
  'saturated': '🟠',
  'perturbed': '🔴',
  'degraded': '🔴'
}.get(regime, '⬜') %}

{% set stage_icon = {
  'bootstrap': '🔵',
  'learning': '🟡',
  'monitoring': '🟢',
  'degraded': '🔴'
}.get(stage, '⬜') %}

{% set ab_label = {
  'ab_ok': '🟢 AB_OK',
  'ab_degraded': '🟡 AB_DEGRADED',
  'ab_bad': '🔴 AB_BAD'
}.get(ab_conf, ab_conf) %}

{% set hold_mode_label = {
  'none': 'none',
  'servo_recovery': 'servo recovery',
  'resume_recovery': 'resume recovery',
  'disturbance_recovery': 'disturbance recovery'
}.get(hold_mode, hold_mode) %}

{% set integral_guard_label = {
  'none': 'none',
  'setpoint_change': 'setpoint change',
  'off_resume': 'off resume',
  'window_resume': 'window resume',
  'power_shedding_resume': 'power shedding resume',
  'disturbance_recovery': 'disturbance recovery'
}.get(integral_guard_source_pub, integral_guard_source_pub) %}

{% set trajectory_source_label = {
  'none': 'none',
  'setpoint': 'setpoint',
  'disturbance': 'disturbance'
}.get(trajectory_source_pub, trajectory_source_pub) %}

{% set current_ff_blk = (ff_pct * 18) | int %}
{% set current_pi_blk = (pi_pct * 18) | int %}
{% set current_off_blk = [18 - current_ff_blk - current_pi_blk, 0] | max %}
{% set power_bar = '█' * current_ff_blk ~ '░' * current_pi_blk ~ '·' * current_off_blk %}

{% if has_debug %}
{% set cycle_min = debug.get('cycle_min', '—') %}
{% set i_mode = debug.get('i_mode', '—') %}
{% set sat = debug.get('sat', 'NO_SAT') %}
{% set filt_sp = debug.get('filtered_setpoint', published_filtered_sp) %}
{% set error_f = debug.get('error_filtered') %}
{% set err_display = error_f | float(error | float(0)) %}
{% set ep = debug.get('error_p', 0) | float %}
{% set u_pi = debug.get('u_pi', 0) | float %}
{% set u_ff = debug.get('u_ff', 0) | float %}
{% set on_pct = debug.get('on_percent', next_cycle) | float %}
{% set u_cmd = debug.get('u_cmd', 0) | float %}
{% set u_limited = debug.get('u_limited', 0) | float %}
{% set u_applied = debug.get('u_applied', 0) | float %}
{% set aw_du = debug.get('aw_du', 0) | float %}
{% set u_pi_th = (debug.get('Kp', 0) | float * ep) + (debug.get('Ki', 0) | float * (integral_error | float(0))) %}
{% set db_active = debug.get('in_deadband', false) %}
{% set nb_active = debug.get('in_near_band', false) %}
{% set in_dt = debug.get('in_deadtime_window', false) %}
{% set forced_tm = debug.get('forced_by_timing', false) %}
{% set boost = debug.get('boost_active', debug.get('setpoint_boost_active', false)) %}
{% set hyst_guard = debug.get('hysteresis_thermal_guard', false) %}
{% set integral_hold_active = debug.get('integral_hold_active', false) %}
{% set integral_guard_active = debug.get('integral_guard_active', integral_guard_source_pub != 'none') %}
{% set integral_guard_source = debug.get('integral_guard_source', integral_guard_source_pub) %}
{% set integral_guard_mode = debug.get('integral_guard_mode', '—') %}
{% set core_db_active = debug.get('in_core_deadband', false) %}
{% set nb_below = debug.get('near_band_below_deg') %}
{% set nb_above = debug.get('near_band_above_deg') %}
{% set nb_src = debug.get('near_band_source', 'unknown') %}
{% set tau_s = debug.get('tau_min') %}
{% set learn_ok = debug.get('learn_ok_count', 0) | int %}
{% set learn_skip = debug.get('learn_skip_count', 0) | int %}
{% set ff_ok = debug.get('ff_warmup_ok_count', 0) | int %}
{% set ff_cyc = debug.get('ff_warmup_cycles', 0) | int %}
{% set traj_start_sp = debug.get('trajectory_start_sp') %}
{% set traj_target_sp = debug.get('trajectory_target_sp') %}
{% set traj_tau_ref = debug.get('trajectory_tau_ref') %}
{% set traj_elapsed_s = debug.get('trajectory_elapsed_s') %}
{% set traj_phase = debug.get('trajectory_phase') %}
{% set traj_source = debug.get('trajectory_source', trajectory_source_pub) %}
{% set traj_pending = debug.get('trajectory_pending_target_change_braking') %}
{% set traj_braking_needed = debug.get('trajectory_braking_needed') %}
{% set traj_model_ready = debug.get('trajectory_model_ready') %}
{% set traj_remaining_cycle_min = debug.get('trajectory_remaining_cycle_min') %}
{% set traj_next_cycle_u_ref = debug.get('trajectory_next_cycle_u_ref') %}
{% set traj_bumpless_u_delta = debug.get('trajectory_bumpless_u_delta') %}
{% set traj_bumpless_ready = debug.get('trajectory_bumpless_ready') %}
{% set landing_sp_for_p_cap = debug.get('landing_sp_for_p_cap') %}
{% set landing_predicted_temperature = debug.get('landing_predicted_temperature') %}
{% set landing_predicted_rise = debug.get('landing_predicted_rise') %}
{% set landing_target_margin = debug.get('landing_target_margin') %}
{% set landing_release_allowed = debug.get('landing_release_allowed', true) %}
{% set landing_coast_required = debug.get('landing_coast_required', landing_coast) %}
{% set landing_time_to_target_min = debug.get('landing_time_to_target_min') %}
{% set landing_release_blocked_by_slope = debug.get('landing_release_blocked_by_slope', false) %}
{% set temperature_slope_h = debug.get('temperature_slope_h') %}
{% set landing_u_cmd_before_cap = debug.get('landing_u_cmd_before_cap') %}
{% set landing_u_cmd_after_cap = debug.get('landing_u_cmd_after_cap') %}
{% set learn_progress = debug.get('learn_progress_percent') %}
{% set learn_time_remaining = debug.get('learn_time_remaining') %}
{% set learn_u_avg = debug.get('learn_u_avg') %}
{% set learn_u_cv = debug.get('learn_u_cv') %}
{% set learn_u_std = debug.get('learn_u_std') %}
{% set deadtime_heat_reliable = debug.get('deadtime_heat_reliable', false) %}
{% set deadtime_cool_reliable = debug.get('deadtime_cool_reliable', false) %}
{% set deadtime_state = debug.get('deadtime_state', '—') %}
{% set deadtime_last_power = debug.get('deadtime_last_power') %}
{% set deadtime_heat_start_time = debug.get('deadtime_heat_start_time') %}
{% set deadtime_cool_start_time = debug.get('deadtime_cool_start_time') %}
{% set t_freeze = debug.get('last_freeze_reason_thermal', debug.get('freeze_reason_thermal', 'none')) %}
{% set g_freeze = debug.get('last_freeze_reason_gains', debug.get('freeze_reason_gains', 'none')) %}
{% set g_dec_t = debug.get('last_decision_thermal', debug.get('governance_decision_thermal', 'unknown')) %}
{% set g_dec_g = debug.get('last_decision_gains', debug.get('governance_decision_gains', 'unknown')) %}
{% set kp_debug = debug.get('Kp', 0) | float %}
{% set ki_debug = debug.get('Ki', 0) | float %}
{% set kp_src = debug.get('kp_source', 'heuristic') %}
{% set ff_raw = debug.get('ff_raw', 0) | float %}
{% set ff_reason = debug.get('ff_reason', '—') %}
{% set u_ff1 = debug.get('u_ff1', debug.get('u_ff_ab', 0)) | float %}
{% set u_ff2 = debug.get('u_ff2', debug.get('u_ff_trim', 0)) | float %}
{% set u_ff3 = debug.get('u_ff3', 0) | float %}
{% set u_ff_final = debug.get('u_ff_final', debug.get('u_ff_eff', 0)) | float %}
{% set u_ff_eff = debug.get('u_ff_eff', u_ff_final) | float %}
{% set u_db_nominal = debug.get('u_db_nominal', u_ff_final) | float %}
{% set ff2_authority = debug.get('ff2_authority') %}
{% set ff2_frozen = debug.get('ff2_frozen', false) %}
{% set ff2_freeze_reason = debug.get('ff2_freeze_reason', debug.get('trim_freeze_reason', 'none')) %}
{% set ff2_trim_delta = debug.get('ff2_trim_delta', 0) | float %}
{% set fftrim_last_reject_reason = debug.get('fftrim_last_reject_reason', '—') %}
{% set fftrim_last_update_reason = debug.get('fftrim_last_update_reason', '—') %}
{% set fftrim_cycles_since_update = debug.get('fftrim_cycles_since_update', 0) | int %}
{% set fftrim_cycle_admissible = debug.get('fftrim_cycle_admissible', false) %}
{% set ff3_enabled = debug.get('ff3_enabled', false) %}
{% set ff3_reason = debug.get('ff3_reason_disabled', '—') %}
{% set ff3_raw_reason = debug.get('ff3_raw_reason_disabled', ff3_reason) %}
{% set ff3_selected_candidate = debug.get('ff3_selected_candidate') %}
{% set ff3_horizon = debug.get('ff3_horizon_cycles', 1) %}
{% set ff3_deadtime_cycles = debug.get('ff3_deadtime_cycles', 0) %}
{% set ff3_horizon_capped = debug.get('ff3_horizon_capped', false) %}
{% set ff3_action_sensitivity = debug.get('ff3_action_sensitivity') %}
{% set ff3_prediction_quality = debug.get('ff3_prediction_quality', 'unavailable') %}
{% set ff3_authority_factor = debug.get('ff3_authority_factor') %}
{% set ff3_disturbance_active = debug.get('ff3_disturbance_active', false) %}
{% set ff3_disturbance_reason = debug.get('ff3_disturbance_reason', '—') %}
{% set ff3_disturbance_kind = debug.get('ff3_disturbance_kind', 'none') %}
{% set ff3_residual_persistent = debug.get('ff3_residual_persistent', false) %}
{% set ff3_dynamic_coherent = debug.get('ff3_dynamic_coherent', false) %}
{% set pred = debug.get('pred', {}) %}
{% set twin_t_hat = pred.get('twin_T_hat') %}
{% set twin_t_pred = pred.get('twin_T_pred') %}
{% set twin_innovation = pred.get('twin_innovation') %}
{% set twin_rmse_30 = pred.get('twin_rmse_30') %}
{% set twin_rmse_pure = pred.get('twin_rmse_pure') %}
{% set twin_model_reliable = pred.get('twin_model_reliable') %}
{% set twin_perturbation = pred.get('twin_perturbation_dTdt') %}
{% set twin_cusum_pos = pred.get('twin_cusum_pos') %}
{% set twin_cusum_neg = pred.get('twin_cusum_neg') %}
{% set twin_external_gain = pred.get('twin_external_gain') %}
{% set twin_external_loss = pred.get('twin_external_loss') %}
{% set twin_t_steady = pred.get('twin_T_steady') %}
{% set twin_t_steady_reliable = pred.get('twin_T_steady_reliable') %}
{% set twin_t_steady_max = pred.get('twin_T_steady_max') %}
{% set twin_setpoint_reachable = pred.get('twin_setpoint_reachable') %}
{% set twin_setpoint_reachable_max = pred.get('twin_setpoint_reachable_max') %}
{% set twin_emitter_saturated = pred.get('twin_emitter_saturated') %}
{% set twin_cooling_model_available = pred.get('twin_cooling_model_available') %}
{% set twin_d_hat_fresh = pred.get('twin_d_hat_fresh') %}
{% set twin_warming_up = pred.get('twin_warming_up') %}
{% set twin_u_eff = pred.get('twin_u_eff') %}
{% set twin_deadtime_s = pred.get('twin_deadtime_s') %}
{% set twin_dead_steps = pred.get('twin_dead_steps') %}
{% set twin_t_hat_error = pred.get('twin_T_hat_error') %}
{% set twin_innovation_bias = pred.get('twin_innovation_bias') %}
{% set twin_bias_warning = pred.get('twin_bias_warning', false) %}
{% set twin_auto_reset = pred.get('twin_auto_reset_triggered', false) %}
{% set twin_reset_count = pred.get('twin_reset_count', 0) %}
{% set eta_s = pred.get('eta_s') %}
{% set eta_u = pred.get('eta_u') %}
{% set eta_reason = pred.get('eta_reason') %}
{% set twin_d_hat = pred.get('twin_d_hat') %}
{% set ff1_pct = (u_ff1 * 100) | round(1) %}
{% set ff2_pct = (u_ff2 * 100) | round(1) %}
{% set ff3_pct = (u_ff3 * 100) | round(1) %}
{% set ffinal_pct = (u_ff_final * 100) | round(1) %}
{% set ffeff_pct = (u_ff_eff * 100) | round(1) %}
{% set dbnom_pct = (u_db_nominal * 100) | round(1) %}
{% set err_clamp = [[err_display, -2] | max, 2] | min %}
{% set bar_pos = ((err_clamp + 2) / 4 * 18) | int %}
{% set bar_line = '─' * bar_pos ~ '▲' ~ '─' * (18 - bar_pos) %}
{% set ff_blk = (u_ff * 18) | int %}
{% set tot_blk = (on_pct * 18) | int %}
{% set pi_blk = [tot_blk - ff_blk, 0] | max %}
{% set off_dot = [18 - ff_blk - pi_blk, 0] | max %}
{% set pwr_bar = '█' * ff_blk ~ '░' * pi_blk ~ '·' * off_dot %}
{% if nb_below is not none and nb_above is not none %}
  {% set nb_low = (t_set | float(0)) - (nb_below | float) %}
  {% set nb_high = (t_set | float(0)) + (nb_above | float) %}
  {% if (t_in | float(0)) < nb_low %}
    {% set nb_status = 'below band' %}
  {% elif (t_in | float(0)) > nb_high %}
    {% set nb_status = 'above' %}
  {% else %}
    {% set nb_status = 'in band' %}
  {% endif %}
{% else %}
  {% set nb_status = '—' %}
{% endif %}
{% set sat_icons = {
  'NO_SAT': '—',
  'SAT_HI': '🔴 high limit',
  'SAT_LO': '🔵 low limit'
} %}
{% set ff3_label = {
  'none': 'active',
  'config_disabled': 'config off',
  'first_cycle_after_restart': '1st cycle',
  'cool_mode': 'cool mode',
  'missing_ext_temp': 'ext missing',
  'tau_not_reliable': 'tau not reliable',
  'twin_not_initialized': 'twin not initialized',
  'twin_not_reliable': 'twin not reliable',
  'twin_unavailable': 'twin unavailable',
  'twin_steady_invalid': 'invalid steady state',
  'calibration': 'calibration',
  'power_shedding': 'shedding',
  'recent_setpoint_change': 'recent setpoint change',
  'deadband': 'deadband',
  'not_near_band': 'outside near-band',
  'pending_cycle_boundary': 'cycle boundary',
  'saturated_high': 'high saturation',
  'system_not_stable': 'unstable',
  'score_not_better': 'insufficient gain',
  'authority_zero': 'zero authority',
  'authority_tapered_to_zero': 'authority tapered to zero',
  'horizon_no_candidate_effect': 'horizon sees no candidate effect',
  'simulation_invalid_params': 'simulation invalid params',
  'simulation_invalid_prediction': 'simulation invalid',
  'trajectory_setpoint_active': 'setpoint trajectory active',
  'twin_not_ready': 'twin unavailable',
  'twin_warming_up': 'twin warming up',
  'residual_not_persistent': 'non-persistent residual',
  'disturbance_unclassified': 'unclassified disturbance',
  'dynamic_incoherent': 'incoherent dynamics',
  'no_disturbance_context': 'no disturbance context'
}.get(ff3_reason, ff3_reason) %}
{% set ff3_disturbance_reason_label = {
  'none': 'valid context',
  'trajectory_setpoint_active': 'setpoint trajectory active',
  'twin_not_ready': 'twin unavailable',
  'twin_unavailable': 'twin unavailable',
  'twin_warming_up': 'twin warming up',
  'twin_not_reliable': 'twin not reliable',
  'twin_steady_invalid': 'invalid steady state',
  'residual_not_persistent': 'non-persistent residual',
  'disturbance_unclassified': 'unclassified disturbance',
  'dynamic_incoherent': 'incoherent dynamics'
}.get(ff3_disturbance_reason, ff3_disturbance_reason) %}
{% set ff3_disturbance_kind_label = {
  'none': 'none',
  'gain': 'external gain',
  'loss': 'external loss'
}.get(ff3_disturbance_kind, ff3_disturbance_kind) %}
{% set restart_reason_label = {
  'none': 'none',
  'external_force': 'external force',
  'deadband_transition': 'deadband transition',
  'near_band_transition': 'near-band transition',
  'guard_cut': 'guard cut',
  'guard_kick': 'guard kick',
  'off': 'off',
  'window': 'window',
  'power_shedding': 'power shedding'
}.get(restart_reason, restart_reason) %}

{% set debug_integral_guard_label = {
  'none': 'none',
  'setpoint_change': 'setpoint change',
  'off_resume': 'off resume',
  'window_resume': 'window resume',
  'power_shedding_resume': 'power shedding resume',
  'disturbance_recovery': 'disturbance recovery'
}.get(integral_guard_source, integral_guard_source) %}

{% set debug_trajectory_source_label = {
  'none': 'none',
  'setpoint': 'setpoint',
  'disturbance': 'disturbance'
}.get(traj_source, traj_source) %}
{% endif %}

## 🏠 {{ entity_name }}

{{ regime_icon }} **{{ regime | replace('_', ' ') | upper }}** · `{{ phase | upper }}`
{%- if has_debug %} · {{ cycle_min }} min{% endif %}
 · `{{ mode }}`
{%- if trajectory_active %} · 🎯 trajectory{% endif %}
{%- if landing_active %} · 🛬 landing{% if landing_coast %} (coast){% endif %}{% endif %}
{%- if ff3_status == 'active' %} · 🔮 FF3{% endif %}
{%- if has_debug and debug.get('in_deadband', false) %} · 💤 DB{% elif has_debug and debug.get('in_near_band', false) %} · 〰️ NB{% endif %}
{%- if autocalib_degraded %} · ⚠️ degraded model{% endif %}
{%- if has_debug and twin_status == 'ok' %} · 🧠 TWIN{% endif %}

---

### 🌡️ Temperatures

| Metric | Value |
|---|---:|
| Room | {% if t_in is not none %}**{{ t_in | float | round(2) }}°C**{% else %}—{% endif %} |
| Setpoint | {% if t_set is not none %}{{ t_set | float | round(2) }}°C{% else %}—{% endif %}{% set display_sp = filt_sp if has_debug else published_filtered_sp %}{% if display_sp is not none and (display_sp | float) != (t_set | float(0)) %} → **{{ display_sp | float | round(2) }}°C**{% endif %} |
| Outdoor | {% if t_ext is not none %}{{ t_ext }}°C{% else %}—{% endif %} |
| Error | {% if error is not none %}`{{ '%+.2f' | format((error | float)) }}°C`{% else %}—{% endif %}{% if has_debug and error_f is not none %} → `{{ '%+.2f' | format(err_display) }}°C`{% endif %} |
| Integral | {% if integral_error is not none %}{{ integral_error | float | round(4) }}{% else %}—{% endif %} |
| Mode I | `{{ integral_mode }}` |
| Guard I | `{{ integral_guard_label }}` |
{%- if has_debug %}
| Hysteresis | `{{ hyst_state }}`{% if hyst_guard %} · guard active{% endif %} |
| Near-band | {{ nb_status }} · `{{ nb_src }}` |
{%- endif %}

{% if has_debug %}
`{{ bar_line }}`
<small>Error ±2°C</small>
{% endif %}

---

### 🎯 Trajectory

| Signal | Value |
|---|---:|
| Active | {% if trajectory_active %}yes{% else %}no{% endif %} |
| Source | `{{ trajectory_source_label }}` |
| Filtered setpoint | {% if published_filtered_sp is not none %}{{ published_filtered_sp | float | round(2) }}°C{% else %}—{% endif %} |
{%- if has_debug %}
| Debug source | `{{ debug_trajectory_source_label }}` |
| Start | {% if traj_start_sp is not none %}{{ traj_start_sp | float | round(3) }}°C{% else %}—{% endif %} |
| Target | {% if traj_target_sp is not none %}{{ traj_target_sp | float | round(3) }}°C{% else %}—{% endif %} |
| `tau_ref` | {% if traj_tau_ref is not none %}{{ traj_tau_ref | float | round(3) }} min{% else %}—{% endif %} |
| Elapsed time | {% if traj_elapsed_s is not none %}{{ traj_elapsed_s | float | round(1) }} s{% else %}—{% endif %} |
| Phase | `{{ traj_phase if traj_phase is not none else '—' }}` |
| Pending braking | {% if traj_pending is sameas true %}yes{% elif traj_pending is sameas false %}no{% else %}—{% endif %} |
| Braking needed | {% if traj_braking_needed is sameas true %}yes{% elif traj_braking_needed is sameas false %}no{% else %}—{% endif %} |
| Model ready | {% if traj_model_ready is sameas true %}yes{% elif traj_model_ready is sameas false %}no{% else %}—{% endif %} |
| Remaining cycle | {% if traj_remaining_cycle_min is not none %}{{ traj_remaining_cycle_min | float | round(3) }} min{% else %}—{% endif %} |
| `u_ref` cycle+1 | {% if traj_next_cycle_u_ref is not none %}{{ (traj_next_cycle_u_ref | float * 100) | round(1) }}%{% else %}—{% endif %} |
| `u_delta` bumpless | {% if traj_bumpless_u_delta is not none %}{{ (traj_bumpless_u_delta | float * 100) | round(1) }}%{% else %}—{% endif %} |
| Bumpless ready | {% if traj_bumpless_ready is sameas true %}yes{% elif traj_bumpless_ready is sameas false %}no{% else %}—{% endif %} |
{%- endif %}

{% if has_debug %}
### 🛬 Setpoint landing

| Signal | Value |
|---|---:|
| Active | {% if landing_active %}yes{% else %}no{% endif %} |
| Reason | `{{ landing_reason }}` |
| `u_cap` | {% if landing_u_cap is not none %}{{ (landing_u_cap | float * 100) | round(2) }}%{% else %}—{% endif %} |
| `SP_for_P` cap | {% if landing_sp_for_p_cap is not none %}{{ landing_sp_for_p_cap | float | round(3) }}°C{% else %}—{% endif %} |
| Predicted temperature | {% if landing_predicted_temperature is not none %}{{ landing_predicted_temperature | float | round(3) }}°C{% else %}—{% endif %} |
| Predicted rise | {% if landing_predicted_rise is not none %}{{ landing_predicted_rise | float | round(3) }}°C{% else %}—{% endif %} |
| Target margin | {% if landing_target_margin is not none %}{{ landing_target_margin | float | round(3) }}°C{% else %}—{% endif %} |
| Coast required | {% if landing_coast_required %}yes{% else %}no{% endif %} |
| Release allowed | {% if landing_release_allowed %}yes{% else %}no{% endif %} |
| Temperature slope | {% if temperature_slope_h is not none %}{{ temperature_slope_h | float | round(3) }}°C/h{% else %}—{% endif %} |
| Time to target | {% if landing_time_to_target_min is not none %}{{ landing_time_to_target_min | float | round(2) }} min{% else %}—{% endif %} |
| Release blocked by slope | {% if landing_release_blocked_by_slope %}yes{% else %}no{% endif %} |
| `u_cmd` before cap | {% if landing_u_cmd_before_cap is not none %}{{ (landing_u_cmd_before_cap | float * 100) | round(2) }}%{% else %}—{% endif %} |
| `u_cmd` after cap | {% if landing_u_cmd_after_cap is not none %}{{ (landing_u_cmd_after_cap | float * 100) | round(2) }}%{% else %}—{% endif %} |

---

{% endif %}
### ⚡ Command

{% if has_debug %}
`{{ pwr_bar }}` **{{ (on_pct * 100) | round(1) }}%**
<small>█ Effective FF · ░ PI · · stop</small>
{% else %}
`{{ power_bar }}` **{{ (next_cycle * 100) | round(1) }}%**
<small>█ FF · ░ PI · · stop</small>
{% endif %}

| Signal | Value |
|---|---:|
| Current cycle | {{ (current_cycle * 100) | round(1) }}% |
| Next cycle | {{ (next_cycle * 100) | round(1) }}% |
| Feed-forward | {{ (ff_pct * 100) | round(1) }}% |
| PI | {{ (pi_pct * 100) | round(1) }}% |
| Hold | {{ (hold_pct * 100) | round(1) }}% |
| Hysteresis | `{{ hyst_state }}` |
| Restart | `{{ restart_reason }}` |
{%- if landing_u_cap is not none %}
| Landing cap | {{ (landing_u_cap | float * 100) | round(1) }}% |
{%- endif %}
{%- if valve_linearization_enabled %}
| SmartPI demand | {{ (linear_next_cycle * 100) | round(1) }}% |
| Adjusted valve command | {{ (next_cycle * 100) | round(1) }}% |
| Current cycle demand | {{ (linear_current_cycle * 100) | round(1) }}% |
| Current cycle adjusted | {{ (current_cycle * 100) | round(1) }}% |
{%- endif %}
{%- if has_debug %}
| `u_cmd` | {{ (u_cmd * 100) | round(1) }}% |
| `u_limited` | {{ (u_limited * 100) | round(1) }}% |
| `u_applied` | {{ (u_applied * 100) | round(1) }}% |
| `aw_du` | {{ (aw_du * 100) | round(2) }}%{% if db_active and aw_du != 0 %} ⚠️{% endif %} |
| `forced_by_timing` | {% if forced_tm %}yes{% else %}no{% endif %} |
| Detailed integral | `{{ i_mode }}` |
| Detailed I guard | `{{ integral_guard_mode }}` |
{%- endif %}

---

### 🌡️ Model

| Parameter | Value |
|---|---:|
| `a` | {% if a is not none %}{{ a | float | round(6) }}{% else %}—{% endif %} |
| `b` | {% if b is not none %}{{ b | float | round(6) }}{% else %}—{% endif %} |
| AB Confidence | {{ ab_label }} |
| `tau_reliable` | {% if tau_reliable %}✅{% else %}⏳{% endif %} |
| `deadtime_heat_s` | {% if dt_heat is not none %}{{ dt_heat }} s{% else %}—{% endif %} |
| `deadtime_cool_s` | {% if dt_cool is not none %}{{ dt_cool }} s{% else %}—{% endif %} |
| `Kp` | {% if kp is not none %}{{ kp | float | round(4) }}{% else %}—{% endif %} |
| `Ki` | {% if ki is not none %}{{ ki | float | round(5) }}{% else %}—{% endif %} |
{%- if has_debug %}
| `tau` | {% if tau_s is not none %}{{ tau_s | float | round(1) }} min{% else %}—{% endif %} |
| `kp_source` | `{{ kp_src }}` |
{%- endif %}

---

### 🧠 AB Learning

{{ stage_icon }} **{{ stage | upper }}**
{%- if bootstrap_progress is not none %} · {{ bootstrap_progress }}%{% endif %}

| Parameter | Value |
|---|---|
| Samples A/B | **{{ samples_a }} / {{ samples_b }}**{% if target_samples %} out of {{ target_samples }}{% endif %} |
| Drift A/B | `{{ a_drift }}` / `{{ b_drift }}` |
| Bootstrap | {% if bootstrap_status %}`{{ bootstrap_status }}`{% else %}—{% endif %} |
| Last reason | `{{ last_reason | truncate(80, true, '…') }}` |
{%- if has_debug %}
| Learn ok/skip | {{ learn_ok }} / {{ learn_skip }} |
| Learn progress | {% if learn_progress is not none %}{{ learn_progress }}%{% else %}—{% endif %} |
| Remaining time | {% if learn_time_remaining is not none %}{{ learn_time_remaining }} s{% else %}—{% endif %} |
| `u_avg / cv / std` | {% if learn_u_avg is not none %}{{ learn_u_avg }}{% else %}—{% endif %} / {% if learn_u_cv is not none %}{{ learn_u_cv }}{% else %}—{% endif %} / {% if learn_u_std is not none %}{{ learn_u_std }}{% else %}—{% endif %} |
{%- endif %}

---

### 🛡️ Governance

| Signal | Value |
|---|---|
| Regime | `{{ regime }}` |
| Thermal decision | `{{ thermal_decision }}` |
| Thermal reason | `{{ thermal_reason }}` |
| FF3 | `{{ ff3_status }}` |
| Twin usable | {% if ff3_twin_usable %}yes{% else %}no{% endif %} |
| Twin status | `{{ twin_status }}` |
| Deadband source | `{{ deadband_source }}` |

---

### 🔧 Calibration

| Signal | Value |
|---|---|
| AutoCalib | `{{ autocalib_state }}`{% if autocalib_degraded %} · ⚠️ degraded{% endif %} |
| Snapshot age | {% if autocalib_age is not none %}{{ autocalib_age }} h{% else %}—{% endif %} |
| Last trigger | {% if autocalib_last %}`{{ autocalib_last }}`{% else %}—{% endif %} |
| Next check | {% if autocalib_next %}`{{ autocalib_next }}`{% else %}—{% endif %} |
| Calibration | `{{ calibration_state }}` |
| Retries | {{ calibration_retry }} |
| Last calibration | {% if calibration_last %}`{{ calibration_last }}`{% else %}—{% endif %} |

{% if has_debug %}
---

### 🧷 Integral Protection

| Signal | Value |
|---|---|
| Guard active | {% if integral_guard_active %}yes{% else %}no{% endif %} |
| Guard source | `{{ debug_integral_guard_label }}` |
| Guard mode | `{{ integral_guard_mode }}` |
| Restart cycle | `{{ restart_reason_label }}` |
| Deadband | {% if db_active %}yes{% else %}no{% endif %} |
| Core deadband | {% if core_db_active %}yes{% else %}no{% endif %} |
| Near-band | {% if nb_active %}yes{% else %}no{% endif %} |
| Deadtime window | {% if in_dt %}yes{% else %}no{% endif %} |
| Saturation | {{ sat_icons.get(sat, sat) }} |
| Boost | {% if boost %}yes{% else %}no{% endif %} |

---

### 🔀 Detailed Feedforward

| Signal | Value |
|---|---:|
| `ff_raw` | {{ (ff_raw * 100) | round(1) }}% |
| `u_ff` | {{ (u_ff * 100) | round(1) }}% |
| `u_pi` | {{ (u_pi * 100) | round(1) }}% |
| Theoretical PI | {{ (u_pi_th * 100) | round(1) }}% |
| `u_ff1` | **{{ ff1_pct }}%** |
| `u_ff2` | **{{ ff2_pct }}%** |
| `u_ff_final` | **{{ ffinal_pct }}%** |
| `u_ff3` | **{{ ff3_pct }}%** |
| `u_ff_eff` | **{{ ffeff_pct }}%** |
| `u_db_nominal` | **{{ dbnom_pct }}%** |

| FF Status | Value |
|---|---|
| FF Reason | `{{ ff_reason }}` |
| FF2 authority | {% if ff2_authority is not none %}{{ (ff2_authority | float * 100) | round(1) }}%{% else %}—{% endif %} |
| FF2 frozen | {% if ff2_frozen %}🔒 yes{% else %}✅ no{% endif %} · `{{ ff2_freeze_reason }}` |
| FF2 trim signal | `{{ '%+.3f' | format(ff2_trim_delta * 100) }}%` |
| FFTrim admissible | {% if fftrim_cycle_admissible %}✅ yes{% else %}no{% endif %} |
| FFTrim update | `{{ fftrim_last_update_reason }}` |
| FFTrim reject | `{{ fftrim_last_reject_reason }}` |
| Cycles since update | {{ fftrim_cycles_since_update }} |
| FF warmup | {{ ff_ok }}/{{ ff_cyc }} |
| FF3 State | {% if ff3_enabled %}🔮 active{% else %}⚪ inactive{% endif %} |
| FF3 Reason | `{{ ff3_label }}` |
| FF3 Raw Reason | `{{ ff3_raw_reason }}` |
| Prediction quality | `{{ ff3_prediction_quality }}` |
| FF3 authority | {% if ff3_authority_factor is not none %}{{ (ff3_authority_factor | float * 100) | round(0) }}%{% else %}—{% endif %} |
| Disturbance context | {% if ff3_disturbance_active %}✅ active{% else %}no{% endif %} |
| Context reason | `{{ ff3_disturbance_reason_label }}` |
| Disturbance type | `{{ ff3_disturbance_kind_label }}` |
| Persistent residual | {% if ff3_residual_persistent %}yes{% else %}no{% endif %} |
| Dynamic coherence | {% if ff3_dynamic_coherent %}yes{% else %}no{% endif %} |
| FF3 Horizon | {{ ff3_horizon }} cycle · deadtime {{ ff3_deadtime_cycles }} · {% if ff3_horizon_capped %}capped{% else %}not capped{% endif %} |
| FF3 sensitivity | {% if ff3_action_sensitivity is not none %}{{ ff3_action_sensitivity | float | round(6) }} °C{% else %}—{% endif %} |
| FF3 Candidate | {% if ff3_selected_candidate is not none %}{{ (ff3_selected_candidate | float * 100) | round(1) }}%{% else %}—{% endif %} |

---

### 🎛️ Detailed Regulation

| Parameter | Value |
|---|---:|
| `Kp` | {{ kp_debug | round(4) }} |
| `Ki` | {{ ki_debug | round(5) }} |
| `ep` | {{ ep | round(4) }} |
| `integral` | {{ integral_error | float(0) | round(4) }} |
| Thermal | {{ '✅ active' if t_freeze == 'none' else '🔒 ' ~ (t_freeze | replace('_', ' ')) }} |
| Gains | {{ '✅ active' if g_freeze == 'none' else '🔒 ' ~ (g_freeze | replace('_', ' ')) }} |
| Thermal decision | `{{ g_dec_t }}` |
| Gains decision | `{{ g_dec_g }}` |

---

### ⏳ Deadtime

| Parameter | Value |
|---|---|
| `deadtime_heat_s` | {% if dt_heat is not none %}{{ dt_heat }} s{% else %}—{% endif %} · {% if deadtime_heat_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
| `deadtime_cool_s` | {% if dt_cool is not none %}{{ dt_cool }} s{% else %}—{% endif %} · {% if deadtime_cool_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
| `deadtime_state` | `{{ deadtime_state }}` |
| `deadtime_last_power` | {% if deadtime_last_power is not none %}{{ (deadtime_last_power | float * 100) | round(1) }}%{% else %}—{% endif %} |
| `heat_start_time` | {% if deadtime_heat_start_time is not none %}{{ deadtime_heat_start_time }}{% else %}—{% endif %} |
| `cool_start_time` | {% if deadtime_cool_start_time is not none %}{{ deadtime_cool_start_time }}{% else %}—{% endif %} |

---

### 🧠 Twin

{% if pred %}
| Signal | Value |
|---|---|
| Status | `{{ twin_status }}` |
| Reliable model | {% if twin_model_reliable %}yes{% else %}no{% endif %} |
| Warming up | {% if twin_warming_up %}yes{% else %}no{% endif %} |
| `T_hat / T_pred` | {% if twin_t_hat is not none %}{{ twin_t_hat }}°C{% else %}—{% endif %} / {% if twin_t_pred is not none %}{{ twin_t_pred }}°C{% else %}—{% endif %} |
| Innovation | {% if twin_innovation is not none %}{{ twin_innovation }}{% else %}—{% endif %} |
| RMSE 30 / pure | {% if twin_rmse_30 is not none %}{{ twin_rmse_30 }}{% else %}—{% endif %} / {% if twin_rmse_pure is not none %}{{ twin_rmse_pure }}{% else %}—{% endif %} |
| `T_hat_error` | {% if twin_t_hat_error is not none %}{{ twin_t_hat_error }}{% else %}—{% endif %} |
| Innovation bias | {% if twin_innovation_bias is not none %}{{ twin_innovation_bias }}{% else %}—{% endif %}{% if twin_bias_warning %} ⚠️{% endif %} |
| Perturbation dT/dt | {% if twin_perturbation is not none %}{{ twin_perturbation }}{% else %}—{% endif %} |
| CUSUM + / - | {% if twin_cusum_pos is not none %}{{ twin_cusum_pos }}{% else %}—{% endif %} / {% if twin_cusum_neg is not none %}{{ twin_cusum_neg }}{% else %}—{% endif %} |
| External gain / loss | {% if twin_external_gain %}gain{% else %}no{% endif %} / {% if twin_external_loss %}loss{% else %}no{% endif %} |
| `T_steady / T_steady_max` | {% if twin_t_steady is not none %}{{ twin_t_steady }}°C{% else %}—{% endif %} / {% if twin_t_steady_max is not none %}{{ twin_t_steady_max }}°C{% else %}—{% endif %} |
| `T_steady_reliable` | {% if twin_t_steady_reliable %}yes{% else %}no{% endif %} |
| Setpoint reachable | {% if twin_setpoint_reachable is not none %}{{ twin_setpoint_reachable }}{% else %}—{% endif %} |
| Setpoint reachable max | {% if twin_setpoint_reachable_max is not none %}{{ twin_setpoint_reachable_max }}{% else %}—{% endif %} |
| Emitter saturated | {% if twin_emitter_saturated %}yes{% else %}no{% endif %} |
| Cooling model | {% if twin_cooling_model_available %}yes{% else %}no{% endif %} |
| `u_eff` | {% if twin_u_eff is not none %}{{ (twin_u_eff | float * 100) | round(1) }}%{% else %}—{% endif %} |
| `deadtime_s / dead_steps` | {% if twin_deadtime_s is not none %}{{ twin_deadtime_s }} s{% else %}—{% endif %} / {% if twin_dead_steps is not none %}{{ twin_dead_steps }}{% else %}—{% endif %} |
| `d_hat / d_hat_fresh` | {% if twin_d_hat is not none %}{{ twin_d_hat }}{% else %}—{% endif %} / {% if twin_d_hat_fresh %}yes{% else %}no{% endif %} |
| ETA / reason | {% if eta_s is not none %}{{ eta_s }} s{% else %}—{% endif %} / `{{ eta_reason if eta_reason is not none else '—' }}` |
| ETA power | {% if eta_u is not none %}{{ (eta_u | float * 100) | round(1) }}%{% else %}—{% endif %} |
| Auto reset | {% if twin_auto_reset %}yes{% else %}no{% endif %} · {{ twin_reset_count }} |
{% else %}
<ha-alert alert-type="info">The thermal twin is not yet usable or does not expose a 'pred' block.</ha-alert>
{% endif %}
{% endif %}

{% endif %}
