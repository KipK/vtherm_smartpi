{% set climate_entity = 'climate.test_4_switchs' %}
{% set sensor_entity = 'sensor.smartpi_diagnostics' %}
{% set entity_name = 'Simu' %}

{% set sensor_state = states[sensor_entity] %}
{% set attrs = sensor_state.attributes if sensor_state is not none else {} %}
{% set spi = attrs.get('live') or {} %}

{% if not spi or spi.get('control') is none %}
<ha-alert alert-type="warning">No SmartPI data available for this entity. Check the sensor ID.</ha-alert>
{% else %}

{% set control = spi.get('control', {}) %}
{% set power = spi.get('power', {}) %}
{% set temp = spi.get('temperature', {}) %}
{% set model = spi.get('model', {}) %}
{% set learning = spi.get('learning', {}) %}
{% set gov = spi.get('governance', {}) %}
{% set ff = spi.get('feedforward', {}) %}
{% set setpoint = spi.get('setpoint', {}) %}
{% set calibration = spi.get('calibration', {}) %}
{% set analysis = spi.get('analysis', {}) %}
{% set analysis_control = analysis.get('control', {}) %}
{% set analysis_learning = analysis.get('learning', {}) %}
{% set analysis_deadtime = analysis.get('deadtime', {}) %}
{% set analysis_governance = analysis.get('governance', {}) %}
{% set analysis_ff = analysis.get('feedforward', {}) %}
{% set has_analysis = analysis | count > 0 %}

{% set phase = control.get('phase', 'unknown') %}
{% set mode = control.get('mode', 'unknown') %}
{% set hyst_state = control.get('hysteresis_state', '—') %}
{% set restart_reason = control.get('restart_reason', 'none') %}
{% set kp = control.get('kp') %}
{% set ki = control.get('ki') %}
{% set ti_min = control.get('ti_min') %}
{% set t_in = temp.get('indoor') %}
{% set t_set = state_attr(climate_entity, 'temperature') %}
{% set climate_state = states[climate_entity] %}
{% set c_attrs = climate_state.attributes if climate_state is not none else {} %}
{% set t_ext = c_attrs.get('ext_current_temperature', c_attrs.get('specific_states', {}).get('ext_current_temperature')) %}
{% set error = temp.get('error') %}
{% set integral_error = temp.get('integral_error') %}
{% set integral_mode = temp.get('integral_mode', temp.get('integral_hold_mode', 'none')) %}
{% set hold_mode = temp.get('integral_hold_mode', 'none') %}
{% set hold_source = temp.get('integral_hold_source', 'none') %}

{% set current_cycle = power.get('current_cycle_percent', 0) | float(0) %}
{% set next_cycle = power.get('next_cycle_percent', 0) | float(0) %}
{% set valve_linearization_enabled = power.get('valve_linearization_enabled', false) %}
{% set linear_current_cycle = power.get('linear_current_cycle_percent', current_cycle) | float(0) %}
{% set linear_next_cycle = power.get('linear_next_cycle_percent', next_cycle) | float(0) %}
{% set ff_pct = power.get('ff_percent', 0) | float(0) %}
{% set pi_pct = power.get('pi_percent', 0) | float(0) %}
{% set hold_pct = power.get('hold_percent', 0) | float(0) %}

{% set a = model.get('a') %}
{% set b = model.get('b') %}
{% set ab_conf = model.get('confidence', 'unknown') %}
{% set tau_reliable = model.get('tau_reliable', false) %}
{% set dt_heat = model.get('deadtime_heat_s') %}
{% set dt_cool = model.get('deadtime_cool_s') %}
{% set deadtime_heat_reliable = model.get('deadtime_heat_reliable', false) %}
{% set deadtime_cool_reliable = model.get('deadtime_cool_reliable', false) %}

{% set learning_enabled = learning.get('enabled') %}
{% set block_reason = learning.get('block_reason') %}
{% set stage = learning.get('stage', 'unknown') %}
{% set bootstrap_progress = learning.get('bootstrap_progress_percent') %}
{% set bootstrap_status = learning.get('bootstrap_status') %}
{% set emea_samples_a = learning.get('emea_samples_a', 0) | int(0) %}
{% set emea_samples_b = learning.get('emea_samples_b', 0) | int(0) %}
{% set bootstrap_target_a = learning.get('bootstrap_target_a', 0) | int(0) %}
{% set bootstrap_target_b = learning.get('bootstrap_target_b', 0) | int(0) %}
{% set history_target = learning.get('history_target', 0) | int(0) %}
{% set accepted_updates_a = learning.get('accepted_updates_a', 0) | int(0) %}
{% set accepted_updates_b = learning.get('accepted_updates_b', 0) | int(0) %}
{% set last_reason = learning.get('last_reason', '—') %}
{% set a_drift = learning.get('a_drift_state', '—') %}
{% set b_drift = learning.get('b_drift_state', '—') %}
{% set start = learning.get('start', {}) %}
{% set start_mode = start.get('mode') %}
{% set start_mode_label = {'bootstrap': 'bootstrap', 'direct': 'direct start'}.get(start_mode, '—') %}
{% set start_pulses = start.get('pulses_done', 0) | int(0) %}
{% set start_pulse = start.get('pulse', 'none') %}
{% set start_kept_a = start.get('kept_a') %}
{% set start_kept_b = start.get('kept_b') %}
{% set start_kept_lag = start.get('kept_lag_s') %}

{% set regime = gov.get('regime', 'unknown') %}
{% set thermal_decision = gov.get('thermal_update_decision', 'unknown') %}
{% set thermal_reason = gov.get('thermal_update_reason', 'none') %}

{% set deadband_source = ff.get('deadband_power_source', 'none') %}



{% set calibration_state = calibration.get('state', 'unknown') %}
{% set calibration_pulses = calibration.get('pulses_done', 0) %}
{% set calibration_result = calibration.get('result') %}
{% set calibration_last = calibration.get('last_time') %}
{% set calibration_dt_start = calibration.get('deadtime_start_s') %}
{% set calibration_dt_stop = calibration.get('deadtime_stop_s') %}
{% set calibration_a = calibration.get('reading_a') %}
{% set calibration_b = calibration.get('reading_b') %}
{% set calibration_lag = calibration.get('reading_lag_s') %}
{% set calibration_request = calibration.get('last_request') %}
{% set calibration_in_a = calibration.get('in_force_a') %}
{% set calibration_in_b = calibration.get('in_force_b') %}
{% set calibration_in_lag = calibration.get('in_force_lag_s') %}
{% set calibration_corrected = calibration.get('corrected') or [] %}

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
  'measurement pulses': '🔵',
  'learning': '🟡',
  'monitoring': '🟢',
  'degraded': '🔴'
}.get(stage, '⬜') %}

{% set ab_label = {
  'ab_bootstrap': '🔵 AB_BOOTSTRAP',
  'ab_learning': '🔵 AB_LEARNING',
  'ab_ok': '🟢 AB_OK',
  'ab_degraded': '🟡 AB_DEGRADED',
  'ab_bad': '🔴 AB_BAD'
}.get(ab_conf, ab_conf) %}

{% set hold_mode_label = {
  'none': 'none',
  'setpoint_change': 'setpoint change',
  'off_resume': 'OFF resume',
  'window_resume': 'window resume',
  'power_shedding_resume': 'power shedding resume',
  'disturbance_recovery': 'disturbance recovery'
}.get(hold_mode, hold_mode) %}

{% set current_ff_blk = ((ff_pct / 100) * 18) | int %}
{% set current_pi_blk = ((pi_pct / 100) * 18) | int %}
{% set current_off_blk = [18 - current_ff_blk - current_pi_blk, 0] | max %}
{% set power_bar = '█' * current_ff_blk ~ '░' * current_pi_blk ~ '·' * current_off_blk %}

{% if has_analysis %}
{% set cycle_min = analysis_control.get('cycle_min', '—') %}
{% set sat = control.get('saturation_state', 'NO_SAT') %}
{% set error_f = analysis_control.get('error_filtered') %}
{% set err_display = error_f | float(error | float(0)) %}
{% set ep = analysis_control.get('error_p', 0) | float %}
{% set u_pi = (pi_pct / 100) | float %}
{% set u_ff = (ff_pct / 100) | float %}
{% set on_pct = (next_cycle / 100) | float %}
{% set u_cmd = (power.get('command_percent', 0) / 100) | float %}
{% set u_limited = (power.get('limited_percent', 0) / 100) | float %}
{% set u_applied = (power.get('applied_percent', 0) / 100) | float %}
{% set aw_du = analysis_control.get('aw_du', 0) | float %}
{% set u_pi_th = (kp | float(0) * ep) + (ki | float(0) * (integral_error | float(0))) %}
{% set db_active = control.get('in_deadband', false) %}
{% set nb_active = control.get('in_near_band', false) %}
{% set in_dt = control.get('in_deadtime_window', false) %}
{% set forced_tm = analysis_control.get('forced_by_timing', false) %}
{% set boost = setpoint.get('boost_active', false) %}
{% set hyst_guard = false %}
{% set integral_hold_active = hold_mode != 'none' %}
{% set core_db_active = analysis_control.get('in_core_deadband', false) %}
{% set nb_src = analysis_control.get('near_band_source', 'unknown') %}
{% set tau_s = model.get('tau_min') %}
{% set learn_ok = analysis_learning.get('learn_ok_count', 0) | int %}
{% set learn_skip = analysis_learning.get('learn_skip_count', 0) | int %}
{% set ff_ok = analysis_learning.get('ff_warmup_ok_count', 0) | int %}
{% set ff_cyc = analysis_learning.get('ff_warmup_cycles', 0) | int %}
{% set temperature_slope_h = analysis_control.get('temperature_slope_h') %}
{% set anticipation_c = analysis_control.get('anticipation_c') %}
{% set anticipation_horizon_s = analysis_control.get('anticipation_horizon_s') %}
{% set anticipation_horizon_source = analysis_control.get('anticipation_horizon_source', 'none') %}
{% set anticipation_horizon_source_label = {
  'measurement pulses': 'measurement pulses',
  'heating balance': 'heating balance',
  'dead times': 'dead times',
  'none': 'none'
}.get(anticipation_horizon_source, anticipation_horizon_source) %}
{% set learn_u_avg = analysis_learning.get('window_mean_power') %}
{% set learn_u_cv = analysis_learning.get('window_power_cv') %}
{% set learn_u_std = analysis_learning.get('window_power_std') %}
{% set deadtime_state = analysis_deadtime.get('state', '—') %}
{% set deadtime_last_power = analysis_deadtime.get('last_power') %}
{% set t_freeze = gov.get('thermal_update_reason', 'none') %}
{% set g_freeze = analysis_governance.get('last_freeze_reason_gains', 'none') %}
{% set g_dec_g = analysis_governance.get('last_decision_gains', 'unknown') %}
{% set kp_src = analysis_governance.get('kp_source', 'heuristic') %}
{% set ff_reason = analysis_ff.get('reason', '—') %}
{% set u_ff1 = analysis_ff.get('u_ff1', 0) | float %}
{% set ff_raw = u_ff1 %}
{% set u_ff_final = analysis_ff.get('u_ff_final', 0) | float %}
{% set u_ff_eff = analysis_ff.get('u_ff_effective', u_ff_final) | float %}
{% set u_db_nominal = u_ff_final %}
{% set ff1_pct = (u_ff1 * 100) | round(1) %}
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
{% set nb_status = 'active' if nb_active else 'inactive' %}
{% set sat_icons = {
  'NO_SAT': '—',
  'SAT_HI': '🔴 high limit',
  'SAT_LO': '🔵 low limit'
} %}
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

{% endif %}

## 🏠 {{ entity_name }}

{{ regime_icon }} **{{ regime | replace('_', ' ') | upper }}** · `{{ phase | upper }}`
{%- if has_analysis %} · {{ cycle_min }} min{% endif %}
 · `{{ mode }}`
{%- if has_analysis and control.get('in_deadband', false) %} · 💤 DB{% elif has_analysis and control.get('in_near_band', false) %} · 〰️ NB{% endif %}

---

### 🌡️ Temperatures

| Metric | Value |
|---|---:|
| Room | {% if t_in is not none %}**{{ t_in | float | round(2) }}°C**{% else %}—{% endif %} |
| Setpoint | {% if t_set is not none %}{{ t_set | float | round(2) }}°C{% else %}—{% endif %} |
| Outdoor | {% if t_ext is not none %}{{ t_ext }}°C{% else %}—{% endif %} |
| Error | {% if error is not none %}`{{ '%+.2f' | format((error | float)) }}°C`{% else %}—{% endif %}{% if has_analysis and error_f is not none %} → `{{ '%+.2f' | format(err_display) }}°C`{% endif %} |
| Integral | {% if integral_error is not none %}{{ integral_error | float | round(4) }}{% else %}—{% endif %} |
| Mode I | `{{ integral_mode }}` |
| I hold source | `{{ hold_source }}` |
{% if has_analysis -%}
| Near-band | {{ nb_status }} · `{{ nb_src }}` |
{% endif %}

{% if has_analysis %}
`{{ bar_line }}`
<small>Error ±2°C</small>
{% endif %}

---

{% if has_analysis %}
### 🎯 Anticipation

| Signal | Value |
|---|---:|
| Correction | {% if anticipation_c is not none %}{{ '%+.3f' | format(anticipation_c | float) }}°C{% else %}—{% endif %} |
| Horizon | {% if anticipation_horizon_s is not none %}{{ anticipation_horizon_s | float | round(0) | int }} s{% else %}—{% endif %} |
| Horizon source | `{{ anticipation_horizon_source_label }}` |
| Temperature slope | {% if temperature_slope_h is not none %}{{ temperature_slope_h | float | round(3) }}°C/h{% else %}—{% endif %} |

---

{% endif %}

### ⚡ Command

{% if has_analysis %}
`{{ pwr_bar }}` **{{ (on_pct * 100) | round(1) }}%**
<small>█ Effective FF · ░ PI · · stop</small>
{% else %}
`{{ power_bar }}` **{{ next_cycle | round(1) }}%**
<small>█ FF · ░ PI · · stop</small>
{% endif %}

| Signal | Value |
|---|---:|
| Current cycle | {{ current_cycle | round(1) }}% |
| Next cycle | {{ next_cycle | round(1) }}% |
| Feed-forward | {{ ff_pct | round(1) }}% |
| PI | {{ pi_pct | round(1) }}% |
| Hold | {{ hold_pct | round(1) }}% |
| Hysteresis | `{{ hyst_state }}`{% if has_analysis and hyst_guard %} · guard active{% endif %} |
{% if not has_analysis -%}
| Restart | `{{ restart_reason }}` |
{% endif -%}
{% if valve_linearization_enabled -%}
| SmartPI demand | {{ linear_next_cycle | round(1) }}% |
| Adjusted valve command | {{ next_cycle | round(1) }}% |
| Current cycle demand | {{ linear_current_cycle | round(1) }}% |
| Current cycle adjusted | {{ current_cycle | round(1) }}% |
{% endif -%}
{% if has_analysis -%}
| `u_cmd` | {{ (u_cmd * 100) | round(1) }}% |
| `u_limited` | {{ (u_limited * 100) | round(1) }}% |
| `u_applied` | {{ (u_applied * 100) | round(1) }}% |
| `aw_du` | {{ (aw_du * 100) | round(2) }}%{% if db_active and aw_du != 0 %} ⚠️{% endif %} |
| `forced_by_timing` | {% if forced_tm %}yes{% else %}no{% endif %} |
{% endif %}

---

### 🌡️ Model

| Parameter | Value |
|---|---:|
| `a` | {% if a is not none %}{{ a | float | round(6) }}{% else %}—{% endif %} |
| `b` | {% if b is not none %}{{ b | float | round(6) }}{% else %}—{% endif %} |
| AB Confidence | {{ ab_label }} |
| `tau_reliable` | {% if tau_reliable %}✅{% else %}⏳{% endif %} |
{% if not has_analysis -%}
| `deadtime_heat_s` | {% if dt_heat is not none %}{{ dt_heat }} s{% else %}—{% endif %} · {% if deadtime_heat_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
| `deadtime_cool_s` | {% if dt_cool is not none %}{{ dt_cool }} s{% else %}—{% endif %} · {% if deadtime_cool_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
{% endif -%}
| `Kp` | {% if kp is not none %}{{ kp | float | round(4) }}{% else %}—{% endif %} |
| `Ki` | {% if ki is not none %}{{ ki | float | round(5) }}{% else %}—{% endif %} |
| `Ti` | {% if ti_min is not none %}{{ ti_min | float | round(0) | int }} min{% else %}—{% endif %} |
{% if has_analysis -%}
| `tau` | {% if tau_s is not none %}{{ tau_s | float | round(1) }} min{% else %}—{% endif %} |
| `kp_source` | `{{ kp_src }}` |
{% endif %}

---

### 🧠 AB Learning

{{ stage_icon }} **{{ stage | upper }}**
{%- if block_reason %} · ⏸️ blocked: {{ block_reason }}{% endif %}
{%- if bootstrap_progress is not none %} · {{ bootstrap_progress }}%{% endif %}

| Parameter | Value |
|---|---|
| Learning | {% if learning_enabled is sameas true %}enabled{% elif learning_enabled is sameas false %}paused{% else %}—{% endif %}{% if block_reason %} · blocked: **{{ block_reason }}**{% endif %} |
| EMEA Samples A/B | **{{ emea_samples_a }} / {{ emea_samples_b }}** (targets: {{ bootstrap_target_a }}/{{ bootstrap_target_b }} bootstrap, {{ history_target }} history) |
| Accepted updates A/B | **{{ accepted_updates_a }} / {{ accepted_updates_b }}** |
| Drift A/B | `{{ a_drift }}` / `{{ b_drift }}` |
| Bootstrap | {% if bootstrap_status %}`{{ bootstrap_status }}`{% else %}—{% endif %} |
| Learning start | {{ start_mode_label }}{% if start_mode == 'direct' %} · pulses **{{ start_pulses }}**{% if start_pulse != 'none' %} (`{{ start_pulse }}`){% endif %}{% endif %} |
| Kept values | {% if start.get('values_kept') %}a **{{ start_kept_a }}** °C/min{% if start_kept_b is not none %} · b **{{ start_kept_b }}** 1/min{% endif %}{% if start_kept_lag is not none %} · lag **{{ start_kept_lag }}** s{% endif %}{% else %}—{% endif %} |
| Last reason | `{{ last_reason | truncate(80, true, '…') }}` |
{% if has_analysis -%}
| Learn ok/skip | {{ learn_ok }} / {{ learn_skip }} |
| `u_avg / cv / std` | {% if learn_u_avg is not none %}{{ learn_u_avg }}{% else %}—{% endif %} / {% if learn_u_cv is not none %}{{ learn_u_cv }}{% else %}—{% endif %} / {% if learn_u_std is not none %}{{ learn_u_std }}{% else %}—{% endif %} |
{% endif %}

---

### 🛡️ Governance

| Signal | Value |
|---|---|
| Regime | `{{ regime }}` |
| Thermal decision | `{{ thermal_decision }}` |
| Thermal reason | `{{ thermal_reason }}` |
| Deadband source | `{{ deadband_source }}` |

---

### 🔧 Calibration

| Signal | Value |
|---|---|
| Calibration | `{{ calibration_state }}` · {{ calibration_pulses }} pulse(s) |
| Result | {% if calibration_result %}`{{ calibration_result }}`{% else %}—{% endif %} |
| Dead times (start / stop) | {% if calibration_dt_start is not none %}{{ calibration_dt_start }} s{% else %}—{% endif %} / {% if calibration_dt_stop is not none %}{{ calibration_dt_stop }} s{% else %}—{% endif %} |
| Reading a · b · lag | {% if calibration_a is not none %}{{ calibration_a }} °C/min · {{ calibration_b }} /min · {{ calibration_lag }} s{% else %}—{% endif %} |
| In use a · b · lag | {% if calibration_in_a is not none %}{{ calibration_in_a }} °C/min · {{ calibration_in_b }} /min · {{ calibration_in_lag }} s{% else %}—{% endif %} |
| Corrected | {% if calibration_corrected %}{{ calibration_corrected | join(', ') }}{% else %}—{% endif %} |
| Last request | {% if calibration_request %}`{{ calibration_request }}`{% else %}—{% endif %} |
| Last calibration | {% if calibration_last %}`{{ calibration_last }}`{% else %}—{% endif %} |

{% if has_analysis %}
---

### 🧷 Integral Protection

| Signal | Value |
|---|---|
| Restart cycle | `{{ restart_reason_label }}` |
| Deadband | {% if db_active %}yes{% else %}no{% endif %} |
| Core deadband | {% if core_db_active %}yes{% else %}no{% endif %} |
| Near-band | {% if nb_active %}yes{% else %}no{% endif %} |
| Dead time window | {% if in_dt %}yes{% else %}no{% endif %} |
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
| `u_ff_final` | **{{ ffinal_pct }}%** |
| `u_ff_eff` | **{{ ffeff_pct }}%** |
| `u_db_nominal` | **{{ dbnom_pct }}%** |

| FF Status | Value |
|---|---|
| FF Reason | `{{ ff_reason }}` |
| FF warmup | {{ ff_ok }}/{{ ff_cyc }} |

---

### 🎛️ Detailed Regulation

| Parameter | Value |
|---|---:|
| `ep` | {{ ep | round(4) }} |
| Thermal | {% if block_reason %}⏸️ learning blocked ({{ block_reason }}){% else %}{{ '✅ active' if t_freeze == 'none' else '🔒 ' ~ (t_freeze | replace('_', ' ')) }}{% endif %} |
| Gains | {{ '✅ active' if g_freeze == 'none' else '🔒 ' ~ (g_freeze | replace('_', ' ')) }} |
| Gains decision | `{{ g_dec_g }}` |

---

### ⏳ Dead time

| Parameter | Value |
|---|---|
| `deadtime_heat_s` | {% if dt_heat is not none %}{{ dt_heat }} s{% else %}—{% endif %} · {% if deadtime_heat_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
| `deadtime_cool_s` | {% if dt_cool is not none %}{{ dt_cool }} s{% else %}—{% endif %} · {% if deadtime_cool_reliable %}✅ reliable{% else %}⏳ learning{% endif %} |
| `deadtime_state` | `{{ deadtime_state }}` |
| `deadtime_last_power` | {% if deadtime_last_power is not none %}{{ (deadtime_last_power | float * 100) | round(1) }}%{% else %}—{% endif %} |

{% endif %}

{% endif %}
