"""Start of the learning from a blank state: the hysteresis bootstrap or the direct start.

The learning stage (``SmartPIPhase.HYSTERESIS``: the first model is not published) and the source of the command are
separate. The learning keeps its stage and everything keyed on it. ``learning_start`` selects the command of the stage:

- ``"bootstrap"``: the on/off hysteresis is the command during the learning stage. Its heating phases are measurement
  events like the pulses' (source ``bootstrap``): they are recorded and read, and two agreeing readings give starting
  values once the stage has ended. The lag of those readings is kept and published, not used: the anticipation horizon
  and the response lag do not read it;
- ``"direct"``: the PI is the command from the first calculation, on the fallback gains until a heating rate is kept.
  Its model readers see no published model during the stage, so the feed-forward is zero. Up to three measurement
  pulses (smartpi/measure_pulse.py) replace the PI command for a short sequence that gives the learning a dead time
  after a start and after a stop, and the readings of the measurement events they cause (smartpi/event_reading.py)
  keep a heating rate, a loss coefficient and an emitter lag that stand in for the first model. The dead time after
  a stop is read on the temperature peak (smartpi/peak_search.py). The integral term of the regulator starts from zero
  at the calculation at which the learning stage ends.

The values kept from the readings are given to the a/b estimator as starting values (smartpi/starting_values.py),
once per mode: with the direct start at the keep, which ends the stage; with the bootstrap start at the later of the
keep and the end of the stage.

The start mode acts on a blank start only. ``SmartPI`` is given the configured mode; a thermostat whose learning has
begun (a saved state with measurements, starting values, pulses or readings) keeps the mode that learning began with,
which is saved with it (a state saved without it began with the bootstrap), whatever is configured now. A learning
reset takes the configured mode again.

``LearningStart`` holds the pulses, the record of the measurement events and their reader, and the few calls
``SmartPI`` makes around them. It also tells the phase of the algorithm and what drives the command. It reads the
state it needs (phase, regulator, dead-time estimator, response lag, estimator, calibration) from the ``SmartPI`` it
belongs to. Times are monotonic seconds, as ``SmartPI.calculate`` reads them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..const import LEARNING_START_BOOTSTRAP, LEARNING_START_DIRECT, LEARNING_STARTS
from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .const import AB_MIN_SAMPLES_A, AB_MIN_SAMPLES_B, SmartPICalibrationPhase, SmartPIPhase
from .event_reading import EventReader
from .measure_event import MODE_COOL, SOURCE_BOOTSTRAP, SOURCE_CALIBRATION, SOURCE_PULSE, SOURCE_REGULATION, MeasureRecord
from .measure_pulse import MeasurePulse
from .timestamp_utils import monotonic_to_wall_ts

_PULSE_STEPS = {"idle": "none", "rest": "rest", "heat": "full power", "stop": "stop"}   # plain words of the state


def command_is_on_off(algo: Any) -> bool:
    """True while the command is applied as an on/off state (bootstrap hysteresis, measurement pulse).

    The learning stage alone does not tell: with the PI as command during the stage the command is not an on/off
    one (the algorithm's own property says so; an object without it keeps the stage).
    """
    return algo.phase == SmartPIPhase.HYSTERESIS and algo.command_is_on_off is not False


class LearningStart:
    """Start mode of the learning, the measurement pulses and their readings."""

    def __init__(self, learning_start: str = LEARNING_START_BOOTSTRAP, owner: Any = None) -> None:
        if learning_start not in LEARNING_STARTS:
            raise ValueError(f"learning_start must be one of {LEARNING_STARTS} (got {learning_start!r})")
        self.configured = learning_start   # the option: what a blank start uses
        self.mode = learning_start         # the start of the learning in progress
        self._owner = owner
        self.pulse = MeasurePulse()
        self.record = MeasureRecord()
        self.reading = EventReader(self.record)
        self._pi_stage_seen = False      # a learning stage driven by the PI has been seen
        self._integral_zeroed = False    # the integral term was set to zero for this blank start
        if owner is not None:
            events = owner.learn_win.events
            events.source = self.event_source
            events.record, events.reader = self.record, self.reading

    @property
    def direct(self) -> bool:
        """True when the PI, not the hysteresis, is the command during the learning stage."""
        return self.mode == LEARNING_START_DIRECT

    def _use(self, mode: str) -> None:
        """Take ``mode`` as the start of the learning in progress."""
        self.mode = mode
        self._owner.dt_est.passive_by_peak = self.direct

    def reset(self) -> None:
        """Blank start: the configured start, the pulses are available again, the kept values and the lag given to the
        estimator are dropped."""
        self._use(self.configured)
        self.pulse.reset()
        self.reading.reset()
        self._pi_stage_seen = False
        self._integral_zeroed = False
        self._owner.learn_win.response_lag.give_lag(None)

    # -- the learning stage ------------------------------------------------------------------------------------
    def b_waiting(self, estimator: Any) -> bool:
        """Whether the learning stage still waits for ``b``. With the direct start ``b`` is usable when a starting
        value stands or the learning's own time constant has been reliable; otherwise when ``b`` has its first
        measurements."""
        if self.direct:
            return not estimator.usable_b
        return len(estimator.b_meas_hist) < AB_MIN_SAMPLES_B

    def stage_waiting(self, estimator: Any) -> bool:
        """True while the first model is not published: ``a`` has no measurements nor starting value, ``b`` is awaited,
        or the first publication of ``a`` waits for the delivered power model."""
        no_a = not estimator.usable_a
        return (no_a and len(estimator.a_meas_hist) < AB_MIN_SAMPLES_A) or self.b_waiting(estimator) or (
            no_a and not estimator.a_model_ready)

    @property
    def phase(self) -> SmartPIPhase:
        """Phase of the algorithm: the calibration, else the learning stage until the first model is published, else
        the stable phase."""
        owner = self._owner
        if owner.calibration_mgr.state != SmartPICalibrationPhase.IDLE:
            return SmartPIPhase.CALIBRATION
        return SmartPIPhase.HYSTERESIS if self.stage_waiting(owner.est) else SmartPIPhase.STABLE

    @property
    def learning_stage(self) -> bool:
        """True while the first model is not published."""
        return self._owner.phase == SmartPIPhase.HYSTERESIS

    @property
    def command_is_hysteresis(self) -> bool:
        """True when the command is the on/off hysteresis (the learning stage with the bootstrap start)."""
        return self.learning_stage and not self.direct

    @property
    def command_is_on_off(self) -> bool:
        """True when the command is applied as an on/off state: the hysteresis or a measurement pulse."""
        return self.command_is_hysteresis or self.pulse.active

    def pre_model_a(self, hvac_mode: object) -> float | None:
        """Heating rate (C/min at full power) of the gains of the learning stage: the kept value of the readings,
        None before values are kept and with the bootstrap start."""
        return self.reading.kept_a_per_min(hvac_mode) if self.direct and self.learning_stage else None

    def kept_lag_s(self, hvac_mode: object) -> float | None:
        """Emitter lag kept from the readings in ``hvac_mode`` (anticipation horizon), None when none is kept and with
        the bootstrap start, whose kept lag is published only."""
        return self.reading.kept_lag_s(hvac_mode) if self.direct else None

    def hand_over(self, estimator: Any, hvac_mode: object) -> None:
        """Give the kept values of ``hvac_mode`` to the estimator as starting values, once per mode and blank start: ``a``
        of the two agreeing readings, and ``b`` with the first hand-over. With the bootstrap start they wait for the end
        of the learning stage. A state saved with kept values and no starting values hands them over at its load."""
        kept = self.reading.kept_for(hvac_mode)
        if kept is None or estimator.start.handed_for(kept["mode"]) or (not self.direct and self.learning_stage):
            return
        sign = -1.0 if kept["mode"] == MODE_COOL else 1.0
        estimator.give_starting_values(kept["a_per_min"], kept.get("b_per_min"), kept["mode"], sign)

    def measures_start(self) -> bool:
        """True while a start is made by a measurement pulse or by the hysteresis, not by the regulation."""
        return self.pulse.active or self._owner.command_is_hysteresis

    def event_source(self) -> str:
        """What drives the command at the start of a measurement event (read by the event detector)."""
        owner = self._owner
        if owner.calibration_mgr.is_calibrating:
            return SOURCE_CALIBRATION
        if self.pulse.active:
            return SOURCE_PULSE
        if owner.command_is_hysteresis:
            return SOURCE_BOOTSTRAP
        return SOURCE_REGULATION

    @property
    def output(self) -> float | None:
        """Command of the measurement pulse in progress (0 or 1), None outside a pulse."""
        return self.pulse.output

    def note_receipt(self, receipt: Any, valve: bool) -> None:
        """An applied-command receipt: for a switch, the cycle it starts goes to the record, which then holds the real on
        and off edges of the emitter instead of the cycle fraction held over the cycle. A valve keeps the applied
        fraction, and so does a scheduler without receipts (the record then gets the power the detector observed)."""
        start = getattr(receipt, "cycle_start_monotonic", None)
        if not valve and start is not None:
            self.record.note_cycle(start, float(receipt.on_time_sec), float(receipt.cycle_duration_sec))

    # -- per calculation ---------------------------------------------------------------------------------------
    def begin(self, hvac_mode: object) -> None:
        """First call of a calculation: the change of mode."""
        self.reading.begin(hvac_mode)
        self.zero_integral()

    def advance(self, now: float, target: float, temp: float, hvac_mode: object, previous_command: float) -> None:
        """Advance the measurement pulse (command of the pulse before the PI, direct start), then the readings of the
        events."""
        owner = self._owner
        calibrating = owner.calibration_mgr.is_calibrating
        learning_stage = self.learning_stage
        if self.direct:
            dt_est = owner.dt_est
            self.pulse.update(
                now, target, temp, hvac_mode,
                cooling=hvac_mode == VThermHvacMode_COOL,
                heat_or_cool=hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL),
                learning_stage=learning_stage,
                calibrating=calibrating,
                active_deadtime_known=dt_est.active_deadtime(hvac_mode)[1],
                passive_deadtime_known=dt_est.passive_deadtime(hvac_mode)[1],
                dt_est=dt_est,
                previous_command=previous_command,
                readings_pending=self.reading.wants_pulses(hvac_mode),
            )
        self.reading.step(
            now, hvac_mode, calibrating=calibrating, response_lag=owner.learn_win.response_lag if self.direct else None,
            phase_start=owner.learn_win.events.phase_start,
        )
        self.hand_over(owner.est, hvac_mode)
        self.record.enabled = self.reading.wants_record(learning_stage, hvac_mode)
        if not self.record.enabled:
            self.record.clear()
        self.zero_integral()

    def interrupt(self, now: float, reason: str) -> None:
        """The thermostat stops (off, window, shedding): the pulse and the readings in progress end."""
        self.pulse.interrupt(now, reason)
        self.reading.interrupt(now, reason)

    def zero_integral(self) -> None:
        """Set the integral term to zero, once per blank start, at the calculation at which a learning stage driven
        by the PI ends. Never with the bootstrap start, after a restart of a learned model or on later gain
        changes; a learning reset makes it available again. Called at every point of a calculation where the stage
        may end, so that the zero is applied at the calculation of the end."""
        if not self.direct or self._integral_zeroed:
            return
        if self.learning_stage:
            self._pi_stage_seen = True
        elif self._pi_stage_seen:
            self._pi_stage_seen = False
            self._integral_zeroed = True
            self._owner.ctl.integral = 0.0

    # -- published summary -------------------------------------------------------------------------------------
    def public(self) -> dict[str, Any]:
        """Plain summary of the start for the published diagnostics, for the mode the model is in: the start of the
        learning in progress, the pulses done and the state of the one in progress, and the values kept from the
        readings. Internal codes become plain words here."""
        mode = self._owner.est.model_hvac_mode
        kept = self.reading.kept_for(mode)
        at = None if kept is None or kept["at_s"] is None else monotonic_to_wall_ts(kept["at_s"])
        b = None if kept is None else kept.get("b_per_min")
        return {
            "mode": self.mode,
            "pulses_done": self.pulse.count_for(mode) if mode is not None else self.pulse.count,
            "pulse": _PULSE_STEPS.get(self.pulse.state, "none"),
            "values_kept": kept is not None,
            "values_kept_at": None if at is None else datetime.fromtimestamp(at).isoformat(),
            "kept_a": None if kept is None else round(kept["a_per_min"], 5),
            "kept_b": None if b is None else round(b, 6),
            "kept_lag_s": None if kept is None else round(kept["lag_s"]),
        }

    # -- persistence -------------------------------------------------------------------------------------------
    def save(self, now: float) -> dict[str, Any]:
        """The start of the learning when it is the direct one (a state without it began with the bootstrap), the
        pulses started (a pulse in progress is saved as ended by the restart), the record of the events and the readings
        with their kept values."""
        state: dict[str, Any] = {"learning_start": self.mode} if self.direct else {}   # no key: the bootstrap
        pulse = self.pulse.save(now)
        if pulse is not None:
            state["measure_pulse"] = pulse
        reading = self.reading.save()
        if reading is not None:
            state["event_reading"] = reading
        record = self.record.save()
        if record is not None:
            state["measure_events"] = record
        return state

    def _begun(self, state: dict[str, Any]) -> bool:
        """True when the learning of a saved state has begun: measurements, starting values, pulses or readings."""
        est = self._owner.est
        return bool(
            est.a_meas_hist or est.b_meas_hist or est.learn_ok_count or est.start.handed
            or state.get("measure_pulse") or state.get("event_reading") or state.get("pulse_reading")
        )

    def load(self, state: dict[str, Any]) -> None:
        """Restore what ``save`` wrote; the pulses only with the direct start. A learning that has begun goes on with
        the start it began with, a blank one takes the configured start."""
        saved = state.get("learning_start")
        if not self._begun(state):
            self._use(self.configured)
        else:
            self._use(saved if saved in LEARNING_STARTS else LEARNING_START_BOOTSTRAP)
        self._pi_stage_seen = False
        self._integral_zeroed = False
        self.pulse.load(state.get("measure_pulse") if self.direct else None)
        self.reading.load(state.get("event_reading") or state.get("pulse_reading"))
        self.record.load(state.get("measure_events"))
        self.hand_over(self._owner.est, self._owner.est.model_hvac_mode)
