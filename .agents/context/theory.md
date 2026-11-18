# Control theory and thermal physics used by SmartPI

Reference notes for design discussions. They are not the implementation: verify against code (`algorithm-core.md`). Separate in every debate: what the literature says, what is a SmartPI hypothesis, what simulation or field data validated.

## Plant

- Room as first order plus dead time (FOPDT). 1R1C thermal model: `C dT/dt = P_heater*u - (T_in - T_out)/R`, in the code's normalized form `dT/dt = a*u - b*(T_in - T_out)`, with `u` in [0,1].
  - `a` = heating rate at full power (C per unit time), `b` = loss coefficient (1/time); time constant `tau = 1/b`; hold power at a gap `dT` is `b*dT/a` (this is the static feed-forward FF1 gain `b/a`).
  - Sign contract (mono-mode): HEAT `a > 0`, COOL `a < 0`, `b > 0` in both.
- Dead time `L`: delay between a power change and the first temperature response (emitter warm-up, transport, sensor). Distinct for heat (rise) and cool (fall) channels. Emitter inertia (radiator water, floor slab) acts as an additional lag; massive walls add a slow second time constant (hence 2R2C/room model refinements).
- Typical scales: the time constant is hundreds of hours in real homes (hold power at 15 K gap of only a few percent); dead times from minutes (electric, thin radiators) to an hour (floor heating). Disturbances: sun, occupants, appliances, doors/windows, wall heat storage after setback.
- Actuation is PWM on/off inside a fixed cycle (average power = duty), or a valve position. Quantization: sensors 0.01 C or 0.1 C; PWM resolution in seconds.

## Controller concepts from Astrom & Hagglund (2nd ed.)

- PI form with integral state `I`; proportional gain `Kp`, integral gain `Ki = Kp/Ti`. SmartPI keeps the raw setpoint (no setpoint weighting, no trajectory) and anticipates on the measurement side instead: see the derivative-like anticipation below.
- Anti-windup: back-calculation / tracking on actuator saturation (SmartPI: per-tick tracking of the output limit in integrator-running modes). Conditional integration when the total command (FF + PI) saturates. Integral action must never be frozen for hours below the setpoint (this was the failure of a former integral guard).
- Derivative on measurement (never on setpoint) avoids kicks; with large emitter lag a derivative-like anticipation `T + slope * delay` is the textbook remedy (series PID form). SIMC prescribes derivative time of the order of the whole lag; half the lag is the usual simulation-preferred horizon. SmartPI applies it to the P and I errors as `Tin + clamp(slope * horizon, 1 C) * fade`, same rule in HEAT and COOL (`smartpi/anticipation.py`); the horizon is the measured emitter lag (HEAT) or the dead times (see `algorithm-core.md`).
- Tuning for FOPDT: lambda/IMC and SIMC rules give `Kp = tau / (K (tau_c + L))`, `Ti = min(tau, 4(tau_c + L))`-type results; dead-time dominant processes (L > tau) need prediction or very conservative gains. Half rule (Skogestad): count half of the neglected lag as extra dead time. Floors/caps on gains protect against bad identification.
- Discrete-time effects: sampling period, ZOH, PWM as time-proportional actuation (mean-ZOH approximation), self-triggered/event-based variants and deadbands (hysteresis shell around the setpoint) with their stability caveats.
- Feed-forward: static (b/a times measured outdoor gap), predictive (model-based horizon correction), slow trim learned from stationary windows; a feed-forward error must not be hidden by the integral for long (bumpless transfers move power between FF trim and integral without changing the command).
- Dead-time and delay estimation: relay/step response, intercept of the first-response slope, energy balance during heating phases. Identification needs excitation; passive (OFF) windows give `b`, active windows give `a`.
- Estimation under quantization: with a coarse staircase sensor, an OLS slope over held values is biased by roughly `(1 - 1/m^2)` over `m` levels and a threshold-triggered episode biases the held values up to half a step: use change instants, not periodic samples.
- Model selection (Bacher & Madsen 2011): choose model order by data support (likelihood-ratio tests, cross-validation); 1R1C is the default, higher orders only with evidence.
- Predictive PI/PID for dead-time processes (Peterle, Rampazzo, Beghi 2018) and event-based/dead-band self-triggered PI are background references for the dead-time handling; adopt a technique only with a bench showing the gain.

## Practical reading rules

- A theory rule is validated for SmartPI only if it holds with: discrete cycles, PWM or valve, long dead times, huge inertia, saturations (deadband, near band, overrides, safety limits).
- Quote the book by chapter/section; do not cite from memory what you cannot source. State conjecture as conjecture.
- Local copies of reference notes are not versioned; the PDFs and books are the primary sources.
