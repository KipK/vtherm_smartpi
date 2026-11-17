# Debugging a user problem

For an agent launched by a user (or a developer) to find why SmartPI behaves badly on one thermostat. Read `../context/diagnostics.md` first (data access, `live` tree map, symptom table, status strings).

## Ground rules

- Read-only investigation. Do not change the user's configuration, do not call services that alter learning (`reset_smartpi_learning`, `reset_smartpi_integral`, `force_smartpi_calibration`) without the user's explicit consent: they destroy evidence.
- Privacy: never ask for or store credentials, tokens, URLs, addresses or names. Ask the user to redact. Never write user data into the repository; summarize qualitatively.
- Never dump the whole `live` tree or full history: extract the blocks you need (it is very large).
- Evidence before conclusions: each hypothesis lists the fields that support/contradict it. If the data cannot discriminate, say which extra measurement would.

## Procedure

1. **Frame the report.** What is observed, when, in which mode (HEAT/COOL), what was expected, since when (version change? setpoint change? restart?). Get the SmartPI version/commit and VT version (check for a diagnostics field of the expected feature, not the HACS version) and the HA log excerpts at the incident time.
2. **VT-side configuration.** Cycle length, valve vs switch, activation/deactivation delays (SmartPI's own minimum on/off options are in the SmartPI entry), sensor type and resolution/heartbeat, presets and setpoint schedule, overheat/safety settings, window/presence features, SmartPI options (global vs per-thermostat). Many "SmartPI" problems are cycle or actuator settings.
3. **Data quality.** Sensor resolution and publication cadence, gaps, restarts, VT off periods, setpoint changes, sun/occupancy influence.
4. **Model sanity.** `a`, `b`, `tau = 1/b`, hold power `b*dT/a`, dead times (heat/cool), learning state and window outcomes. Implausible values (tau of a few hours, a near zero, a/b far from 1000-2000 C scale) point to learning or data problems.
5. **Controller state.** Gains vs floors/caps, integral mode and hold source, deadband/near band, trajectory/boost flags, anti-windup output limit, FF terms (static, FF3, trim) and their ownership, valve curve.
6. **Subsystem.** Bootstrap/calibration status, T_ar/room model/quiet periods publication reasons, restart bridge decision, command ownership binding reasons.
7. **Timeline.** Align the history series (indoor, setpoint, commands, PI/FF split, a/b, kp/ki) with the events; locate the first instant the behaviour departs from the model.
8. **Conclude.** Root cause or ranked hypotheses; classify: configuration, data/sensor, VT behaviour, SmartPI bug, SmartPI design limit (known; see `../context/decisions.md`). For a bug: minimal reproduction (a test sketch with the fake runtime) and the module concerned.
9. **Escalate** a design flaw to `smartpi-architect`/`smartpi-control-researcher`; a code fix to `smartpi-developer` with a precise description.

## Report format

Use `../templates/issue-report.md`.
