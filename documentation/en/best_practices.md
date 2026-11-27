# SmartPI: best practices for good learning

SmartPI learns your room on its own: how fast it heats up, how much heat it loses, and how long the radiator takes to react. The cleaner its measurements, the more accurate the regulation. This guide sums up what helps at each step. The details are in the [user documentation](vtherm_smartpi.md).

## Before anything else

- **A good sensor.** The best choice is a sensor with 0.01 °C resolution that reports at least every 5 minutes. A sensor with 0.1 °C resolution that reports on every change also works. Place it near the middle of the room, away from the radiator, direct sunlight and drafts.
- **The outdoor temperature** must be available in Versatile Thermostat.
- **The home location** must be set in Home Assistant (Settings > System > General). SmartPI uses it to know whether it is day or night and to ignore measurements distorted by the sun.
- **Window detection** in Versatile Thermostat must be enabled (sensor or automatic detection).
- **A cycle suited to your heater.** See the table [PWM cycle by installation](vtherm_smartpi.md#pwm-cycle-by-installation).
- **Do not change the settings** while SmartPI is learning. A setting changed halfway through blurs the measurements.

## Choosing how to start

| | Bootstrap (default) | Direct start |
| --- | --- | --- |
| How it works | Heats on/off around the setpoint until the model is learned | Normal regulation from the first minute, with at most three light measurement pulses |
| Duration | From a few hours to two days | First model in about 8 to 24 hours |
| Comfort while learning | The temperature swings between the two thresholds | The room stays close to the setpoint |
| Sensor | Any sensor, with suitable thresholds | A precise sensor, preferably 0.01 °C |
| Schedule | Doesn't matter | At least one scheduled setpoint change every day |

If in doubt, keep the bootstrap.

## During the bootstrap

- **Let it run without interruption** for one or two days: no stopping the thermostat, no reset.
- **Keep a stable setpoint**, clearly above the outdoor temperature. Avoid big schedule changes until it is finished.
- **Adjust the hysteresis thresholds to your sensor**: see [Bootstrap hysteresis by sensor](vtherm_smartpi.md#bootstrap-hysteresis-by-sensor). With a coarse or slow sensor, thresholds that are too tight make the bootstrap last longer, or prevent it from finishing.
- **Oscillations are normal**: this is how SmartPI measures the room. Follow the progress in the diagnostics card.
- **Report disturbances** (see below): many guests, a fireplace, a boiler that is switched off.

## During the direct start

- **Schedule at least one setback per day**, for example 1 °C lower at night. A fixed setpoint day and night does not give SmartPI the measurements it needs.
- **Let the pulses complete.** A pulse usually lasts two to three hours: a short rest, a full-power heating phase, then a stop. Changing the setpoint, stopping the thermostat or pausing the learning during that time wastes it. Avoid changing the setpoint by hand during the first day.
- Two pulses are at least 6 hours apart. Two matching pulses are needed.

## In everyday regulation

- **Keep a regular schedule.** Each setpoint increase after a period without heating (in the morning, for example) is an opportunity for a measurement that refines the model.
- **Report what SmartPI cannot see:**
  - the **disturbance entity**, set to "on" during an unusual heat gain or loss (guests, fireplace, strong sun, auxiliary heater);
  - the **unavailable heating entity**, set to "on" when the radiator cannot heat (boiler switched off, hot water priority, defrost, load shedding);
  - the **`set_smartpi_learning` service** to pause the learning by hand (works, unusual absence).

  See [Pausing the learning](vtherm_smartpi.md#pausing-the-learning).
- **Small overshoots are normal**: one or two tenths of a degree after a rise, especially with a slow radiator. The model keeps refining itself over the following days.

## When to ask for a calibration

The calibration (`force_smartpi_calibration` service) runs a new measurement of the room with at most three pulses near the setpoint, and corrects the model if it is clearly wrong. It is useful:

- **after an update from a previous version** of SmartPI, as long as the diagnostics show "previous version" as the source of the response model. SmartPI starts one by itself after 3 days, on a night when the setpoint stays unchanged according to the two previous nights (the home location must be set); `calibration.automatic_status` tells what it is waiting for. You can also request it yourself without waiting;
- after a change of radiator, sensor, or major work;
- if the room repeatedly overshoots or stays too slow to reach the setpoint.

For it to succeed:

- choose a time when **the setpoint will not change for several hours**; at night, at the night setpoint, works well;
- **do not turn off the thermostat** and do not pause the learning during that time;
- the result appears in the diagnostics card (`calibration.result`). If a pulse is interrupted or the calibration fails, a Home Assistant notification gives the reason; you can request a new calibration right away.

## When to reset the learning

Reset (`reset_smartpi_learning` service) only if the room has really changed: new radiator, moved sensor, thermostat assigned to another room. A bad day is not a reason to relearn everything: the model corrects itself, and a calibration is usually enough.
