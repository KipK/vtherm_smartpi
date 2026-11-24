# Field knowledge (anonymized)

What real installations look like, to calibrate judgement and benches. Generic orders of magnitude of typical installations; no individual installation or tester data.

## Rooms

- Typical living area heated by electric/PWM emitters with a precise 0.01 C sensor: fast response, regulation within about +-0.1 C most of the time.
- Rooms differ in insulation and coupling to their neighbours; a worse insulated or poorly coupled room loses heat faster and tracks a different air temperature.
- Mild-weather estimates: time constants of hundreds of hours (`tau = 1/b`, typically 190-365 h), `a` around 0.05 C per time unit at full power, `a/b` around 1000-1800 C (hold power 1-2 % at a 15 K gap). `b` is likely biased LOW in mild weather (small indoor-outdoor gaps; the 1R1C model has no free-gain term) and should rise in winter. Calibrate benches on winter values. Plants with tau of 1 h do not exist.
- Passive (OFF) slopes in such rooms are about 0.05 C/h: noise, quantization and window length dominate identification.
- Occupancy habits (bedtime, wake-up) vary by household: never hard-code any.

## Sensors

- Target: precise 0.01 C sensors, publish on change (rate-limited, tens of seconds) + heartbeat of 5-10 min. Users often set a reporting threshold of 0.05 C to save battery (resolution stays 0.01, reported change 0.05).
- Slow: 0.1 C resolution, publish on change, heartbeat 30-60 min depending on model. Not noisy; staircase-like. Best effort.
- A bench sensor model that samples every 300 s is pessimistic for precise sensors.
- Old exports: the published indoor value can stop refreshing while VT is off or during an overheat cut and keeps the stale value; with a dithered 0.01 C sensor, a run of one identical value for >= 30 min over >= 20 rows marks the data inactive. Do not apply this to slow sensors, which legitimately hold values.

## Emitters

- Electric/PWM panels and small radiators: dead time of minutes. Water radiators: tens of minutes. Floor heating: up to about an hour and large stored heat (overshoot after the setpoint is reached is the main defect, because power applied during the rise is still arriving).
- Rooms with massive walls: cold walls after a night setback keep absorbing heat after a setpoint rise (the room stays just short of the setpoint for hours); setbacks that are short with heavy walls leave the room model unpublished.
- After a setpoint rise the command at crossing is still 25-70 % (FF + integral grown during the rise); in massive slow-emitter rooms most of the overshoot is power applied above the setpoint.

## Observed behaviours worth knowing

- Without an integral guard, the post-rise temperature dip falls from 75-180 min to under 30 min on fast emitters, with overshoot <= 0.15 C; floor heating overshoots more.
- Slope anticipation (regulate on `T + slope * horizon`, faded in only near the setpoint, horizon about half the emitter inertia) reduced simulated overshoot by large factors in slow-emitter cells with a rise-time cost of 5-10 min; it is a candidate not yet adopted (see `decisions.md`).
- Gains of massive slow rooms already sit at the floors (`KP_MIN`/`KI_MIN`); counting emitter inertia in the gains alone does not fix floor heating in a massive room.
- A heating-balance estimate (heating rate at full power, emitter lag) is unusable in regulation at variable power, usable in bootstrap/calibration; it is biased low 25-60 % in massive rooms and good in massless rooms. It is published for observation only.
- Simulated field-like cells leave the bootstrap after about 36 h through the "full collection" path (31 measurements of a and b); with full histories SmartPI must never remain in bootstrap.

## Home Assistant access (for maintainers' agents)

- Live diagnostics: read sub-blocks of the diagnostics sensor attribute `live` with a template and `| tojson` (e.g. `model`, `tar_learning`, `analysis.learning.response_model`, `analysis.learning.heating_balance`, `calibration`). The full attribute dump is very large: never read it whole.
- History: 5-minute statistics for numeric sensors (power percent, smoothed temperature which lags the raw value by minutes).
- Never store endpoints, tokens or entity names of a real installation in this repository.
