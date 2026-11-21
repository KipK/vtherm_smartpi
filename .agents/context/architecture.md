# SmartPI code map

Home Assistant custom integration `vtherm_smartpi`: registers the SmartPI
self-adapting PI algorithm as a proportional-function plugin of Versatile
Thermostat (VT) through `vtherm_api`. Code is the source of truth; refer to
symbols, not line numbers (`path::symbol`).

## 1. Repo layout (versioned)

```
custom_components/vtherm_smartpi/   integration (root modules + smartpi/ package)
  smartpi/                          algorithm modules (pure Python, no HA import unless noted)
    room_model/  tar_learning/  quiet_periods/
  translations/{en,fr}.json  services.yaml  manifest.json  brand/
tests/                              pytest suite (+ fakes/, fixtures/)
documentation/{en,fr}/              diagnostics.md, technical_doc.md, vtherm_smartpi.md
cards/                              Lovelace diagnostics card docs (smartpi_diags-{en,fr}.md)
container                           dev container helper script
.devcontainer/                      dev container (HA config, pytest.ini, lovelace)
.github/workflows/                  pytest.yml, hacs.yml, release.yml
README.md README.fr.md CHANGELOG.md hacs.json pyproject.toml requirements_*.txt
```
`plans/`, `dev_tests/` and `tools/` are gitignored (local only): not available to other readers.

## 2. Root modules

| File | Role |
|---|---|
| `__init__.py` | `async_setup`: registers the factory with `VThermAPI` (`_register_factory`, kept alive by `_keep_factory_registered`), registers services (`_register_services`), reloads affected VTherms on options change; setup/unload/remove of entries |
| `config_flow.py` | `SmartPIConfigFlow` / `SmartPIOptionsFlow`: global defaults entry and per-thermostat entries; schema builders (`build_main_options_schema`, `build_valve_curve_schema`), valve linearization validation |
| `const.py` | Domain, `CONF_*` keys, `DEFAULT_*` values, `DEFAULT_*` option dict, service names, dispatcher signals |
| `factory.py` | `SmartPIHandlerFactory` (VT plugin factory); builds `SmartPIHandler` |
| `handler.py` | `SmartPIHandler`: VT runtime bridge. Merges config (`_resolve_effective_config`), builds `SmartPI` (`init_algorithm`), loads/saves state, drives `control_heating`, scheduler hooks (`on_scheduler_ready`, receipts), learning-job exclusions, diagnostics publication, `service_*` methods |
| `algo.py` | `SmartPI`: orchestrator that owns all `smartpi/` components; `calculate`, `update_learning`, cycle callbacks, `save_state`/`load_state`, `get_published_diagnostics` |
| `sensor.py` | Diagnostic sensor platform: `SmartPIDiagnosticSensor` and `SmartPIRecordedDiagnosticSensor` (debug profile); attributes via `smartpi/diagnostic_history.py` |
| `state_store.py` | HA `Store` wrappers: `SmartPIStateStore` (main + legacy key migration), `SmartPITarStore`, `SmartPIRoomModelStore` (delayed saves) |
| `valve_config_check.py` | Read-only inspection of VT valve filters; HA Repairs issues and valve diagnostics |
| `hvac_mode.py` | `VThermHvacMode` helpers and `VThermHvacMode_*` constants |
| `commons.py` | `write_event_log` helper |
| `services.yaml` | Service descriptions: `reset_smartpi_learning`, `set_smartpi_learning`, `force_smartpi_calibration`, `reset_smartpi_integral` |
| `translations/` | `en.json`, `fr.json`: config/options steps, errors, services, exceptions, repair issues |
| `manifest.json` | HA manifest (version, requirements: vtherm_api, numpy, scipy) |

## 3. `smartpi/` modules by function

**Control core**
- `controller.py`: `SmartPIController`: integral state, hysteresis output, PWM calculation, anti-windup (`track_output_limit`), bounded integral-power transfer.
- `gains.py`: `GainScheduler`: Kp/Ki from the model (IMC rule), near-band adjustment.
- `governance.py`: `SmartPIGovernance`: regime (warm-up, stable, perturbed...) and whether learning/gains may adapt.
- `guards.py`: `SmartPIGuards`: guard cut / guard kick safety actions.
- `resume_hold.py`: `ResumeDeadtimeHold`: integral hold after window or shedding resume; `recovery_error_floor_c`.
- `anticipation.py`: slope anticipation (`correction_c`, `select_horizon`, `anticipated_error`): the P and I errors read the measured temperature plus the expected travel over the horizon, in HEAT and COOL.
- `const.py`: algorithm constants, enums, `clamp`, governance matrix.

**Setpoint / deadband**
- `setpoint.py`: `SmartPISetpointManager`: setpoint boost state only (the regulation works on the raw setpoint).
- `deadband.py`: `DeadbandManager`: deadband / near-band state with hysteresis and auto near-band.
- `deadband_output.py`: error shaping inside the deadband.
- `valve_curve.py`: static valve linearization of the command.

**Thermal learning A/B**
- `ab_estimator.py`: `ABEstimator`: robust online estimate of a and b (1R1C).
- `ab_aggregator.py`: published a_hat / b_hat aggregation.
- `ab_drift.py`: persistent drift helpers.
- `ab_window_balance.py`: integral-balance pools for windows without a robust slope.
- `heating_balance.py`: heating-balance estimator (its median kept emitter lag is the HEAT horizon of the anticipation; the rest is observation only).
- `learning_window.py`: `LearningWindowManager`: multi-cycle learning windows, acceptance.
- `learning_outcomes.py`: bounded counters of window outcomes.
- `tint_filter.py`: adaptive filter on indoor temperature (`t_int_lp`, `t_int_clean`).

**Dead time and emitter lag**
- `deadtime_estimator.py`: heating/cooling dead time by state machine on power transitions.
- `deadtime_trend.py`: free-running trend before a power start.
- `response_lag.py`: emitter lag estimator over the constant-power heating phases of the learning windows.

**Calibration**
- `calibration.py`: `CalibrationManager`: forced calibration state machine.
- `autocalib.py`: `AutoCalibTrigger`: triggers calibration on parameter stagnation.

**Feed-forward**
- `feedforward.py`: FF1 holding power (learned b / a, or the published room model) and its warm-up factor.
- `ff_ab_confidence.py`: reliability policy of a, b (OK / degraded / bad).

**Restart bridge**
- `restart_bridge.py`: one decision on whether a stop (reload/restart) can be bridged; `save`/`load`.

**Diagnostics / history**
- `diagnostics.py`: `build_published_diagnostics`: live tree, one `_build_*` per section.
- `diagnostic_history.py`: `build_diagnostic_attributes` / `build_history_diagnostics`: HA attribute envelope.

**VT glue / misc**
- `device_link.py`: device-registry helpers for config entries.
- `compatibility.py`: VT version guard before registration.
- `timestamp_utils.py`: monotonic <-> wall-clock conversion for persistence.
- `job_abort.py`: stop signal for executor jobs.

**`room_model/`** (air-wall-emitter model: forecasts, FF holding power)
- `manager.py` orchestration (event loop), `jobs.py` executor jobs (numpy/scipy), `identification.py` output-error fit, `energy_balance.py` daily static model, `gates.py` publication rules, `periods.py` fit periods, `observer.py` Kalman observer, `model.py` discretisation, `linalg.py` small matrices, `emitter.py` applied-command timeline, `feedforward.py` holding power, `scoring.py` forecast errors, `public.py` user-facing values, `const.py` rule constants.

**`tar_learning/`** (emitter lag T_ar; always enabled by the handler, which builds `SmartPI(tar_learning=True, room_model=True)`; the constructor flag defaults to False and tests use it; numpy/scipy lazily imported)
- `manager.py` orchestration, `jobs.py` executor jobs, `history.py` bounded signal history, `signals.py` signal primitives, `identification.py` multi-day night fit, `morning.py` comfort-step estimator, `response.py` step-response fit, `room_mass.py` room-mass indicator, `gates.py` acceptance gates, `policy.py` which value regulation uses, `clock.py` local days/hours, `deps.py` lazy numerical deps, `public.py` user-facing values, `const.py` rule constants.

**`quiet_periods/`** (stretches without sun, constant setpoint, valid data; pure Python + astral)
- `selection.py` period selection, `solar.py` sun position / darkness intervals, `const.py` constants.

## 4. Tests (`tests/`, pytest, `asyncio_mode = "auto"`)

Find a test by module name: `tests/test_<module>.py` or `tests/test_smartpi_<topic>.py`.

| Area | Files (glob) |
|---|---|
| Root modules | `test_config_flow`, `test_factory`, `test_init_lifecycle`, `test_services`, `test_sensor`, `test_valve_config_check`, `test_device_link`, `test_vtherm_compatibility`, `test_handler_*`, `test_smartpi_handler_*`, `test_smartpi_vt_recalculate_contract` |
| Control core | `test_smartpi_{antiwindup,bumpless_transfer,feedback,gain_transfer,governance,guards,guard_cut_*,integral_hold,max_on_percent,pause,shedding,robustness*}`, `test_algo_{deadband_hysteresis,off_mode}` |
| Setpoint / deadband | `test_smartpi_setpoint*`, `test_algo_setpoint_boost`, `test_smartpi_deadband_output`, `test_smartpi_near_band_unwind`, `test_smartpi_nearband_model`, `test_smartpi_valve_curve`, `test_smartpi_valve_runtime_sync` |
| Learning A/B | `test_ab_window_balance`, `test_smartpi_ab_*`, `test_smartpi_learning_*`, `test_learning_outcomes`, `test_smartpi_heating_balance`, `test_smartpi_b_heat_learning_guard`, `test_smartpi_hysteresis_learning`, `test_smartpi_tin_history_coverage` |
| Dead time / emitter lag / calibration | `test_smartpi_deadtime_*`, `test_smartpi_response_lag*`, `test_smartpi_calibration*` |
| Feed-forward | `test_smartpi_feedforward`, `test_room_model_feedforward` |
| Restart | `test_restart_bridge*`, `test_smartpi_restart_*` |
| Persistence / diagnostics | `test_algo_persistence`, `test_handler_persistence`, `test_diagnostic_history`, `test_published_diagnostics_schema` |
| Room model | `test_room_model_*` (helper `room_model_sim.py`) |
| Emitter lag learning | `test_tar_learning_*`, `test_quiet_periods` (helper `tar_learning_sim.py`) |

Support files:
- `conftest.py`: adds repo, `tests/`, sibling `vtherm_api/src` and `versatile_thermostat` to `sys.path`; loads `pytest_homeassistant_custom_component`; fixtures `fake_runtime`, `fake_handler_runtime`; autouse custom-integration enabling.
- `fakes/fake_thermostat_runtime.py` (`FakeThermostatRuntime`), `fakes/fake_cycle_scheduler.py`.
- `helpers.py`: e.g. `force_smartpi_stable_mode(algo)`.
- `fixtures/published_diagnostics_paths.json`: expected key paths of the published live tree, checked by `test_published_diagnostics_schema.py`. After an intentional tree change regenerate with `SMARTPI_UPDATE_DIAG_PATHS=1 python -m pytest tests/test_published_diagnostics_schema.py`.
- `fixtures/tar_learning/`: recorded histories (`*.json`, `*.npz`) and `regulation_reference.json`.

## 5. Where do I change X?

| Task | Files |
|---|---|
| Add a published diagnostic field | builder in `smartpi/diagnostics.py` (`_build_*`); regenerate `tests/fixtures/published_diagnostics_paths.json`; document in `documentation/{en,fr}/diagnostics.md` and `cards/` if shown; keep user-facing values plain (see `room_model/public.py`, `tar_learning/public.py`) |
| Change what the sensor attributes contain | `smartpi/diagnostic_history.py`, `sensor.py`, `test_diagnostic_history.py`, `test_sensor.py` |
| Add a config option | key and default in `const.py`; form field in `config_flow.py`; read in `handler.py::_resolve_effective_config` / `init_algorithm`, pass to `SmartPI`; labels in `translations/en.json` and `fr.json`; docs `documentation/{en,fr}/vtherm_smartpi.md`; tests `test_config_flow.py`, `test_handler_effective_config.py` |
| Change a gain rule | `smartpi/gains.py` (and constants in `smartpi/const.py`); tests `test_smartpi_gain_transfer.py` |
| Change learning acceptance / governance | `smartpi/learning_window.py`, `smartpi/governance.py` (matrix in `smartpi/const.py`) |
| Change a/b estimation | `smartpi/ab_estimator.py`, `ab_window_balance.py`, `ab_aggregator.py` |
| Change feed-forward | `smartpi/feedforward.py`, `ff_ab_confidence.py`, `room_model/feedforward.py` |
| Add a persisted state field | `save_state`/`load_state` of the owning component (new key optional with a neutral default on load); wire it in `algo.py::save_state`/`load_state` only if the component is new; tests `test_algo_persistence.py` / `test_handler_persistence.py`; storage keys live in `state_store.py` |
| Add a persisted store (separate file) | new class in `state_store.py`, load/save wiring in `handler.py` (see `_tar_store`) |
| Add a service | name in `const.py`; registration in `__init__.py::_register_services`; method `service_*` in `handler.py`; `services.yaml`; `translations/{en,fr}.json` `services`; `test_services.py`; docs |
| Add a Repairs issue | `valve_config_check.py` or `smartpi/compatibility.py`; `translations` `issues`; tests |
| Change the landing on the setpoint (anticipation) / boost | `smartpi/anticipation.py` (rule), `algo.py::SmartPI.calculate` (wiring), `smartpi/setpoint.py` (boost) |
| Change restart behavior | `smartpi/restart_bridge.py`, `handler.py` (`_restart_exclusion`) |
| Add a numerical learning job | new module in `smartpi/<pkg>/jobs.py` style (snapshot in, plain data out, executor, `job_abort.check`); numpy/scipy imported lazily in the job only |
| Release / CI | `.github/workflows/*.yml`, `manifest.json`, `CHANGELOG.md` |

Rules for any user-visible change: update `documentation/en` and `documentation/fr` together; docs describe the current behavior only.

## 6. Size and modularization rule

`algo.py` and `handler.py` must not grow. New logic goes into a new or existing module under `smartpi/`; `algo.py` only wires it (construct, call, persist, publish), and `handler.py` only bridges VT. Extract before adding.

Current sizes (lines): `algo.py` 2312, `handler.py` 812, all of `custom_components/vtherm_smartpi/smartpi/` (including subpackages) 19885. Largest `smartpi/` files: `learning_window.py` 1161, `ab_estimator.py` 1031, `response_lag.py` 842.

## 7. Names that do not exist (do not look for them)

Mentioned by stale notes, absent from the code:
`reference_governor.py`, `reference_governor_authority.py`, `reference_governor_runtime.py`, `governor_falsification.py`, `braking_release.py`, `integral_guard.py` (its resume hold lives in `smartpi/resume_hold.py`), `eris_tuning.py`, `ff_trim.py`, `ff3.py`, `thermal_twin_1r1c.py`, `command_ownership.py`, `gain_observation.py`, `trajectory.py`, `recovery_plateau.py` (the setpoint trajectory and its plateau detector; the landing is the anticipation).
