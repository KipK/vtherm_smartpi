"""Start of the learning from a blank state: the hysteresis bootstrap or the direct start.

The learning stage (``SmartPIPhase.HYSTERESIS``: the first model is not published) and the source of the command are
separate. The learning keeps its stage and everything keyed on it. ``learning_start`` selects the command of the stage:

- ``"bootstrap"``: the on/off hysteresis is the command during the learning stage. Its heating phases are measurement
  events like the pulses' (source ``bootstrap``): they are recorded and read, and two agreeing readings give starting
  values once the stage has ended. The lag of those readings is kept and published, not used: the anticipation horizon
  does not read it;
- ``"direct"``: the PI is the command from the first calculation, on the fallback gains until a heating rate is kept.
  Its model readers see no published model during the stage, so the feed-forward is zero. Up to three measurement
  pulses (smartpi/measure_pulse.py) replace the PI command for a short sequence that gives the learning a dead time
  after a start and a measurement event, and the readings of those events (smartpi/event_reading.py) keep a heating
  rate, a loss coefficient and an emitter lag that stand in for the first model. The pulses go on while no values are
  kept in the mode or its response model is not published. The integral term of the regulator starts from zero
  at the calculation at which the learning stage ends.

The values kept from the readings are given to the a/b estimator as starting values (smartpi/starting_values.py),
once per mode: with the direct start at the keep, which ends the stage; with the bootstrap start at the later of the
keep and the end of the stage.

The start mode acts on a blank start only. ``SmartPI`` is given the configured mode; a thermostat whose learning has
begun (a saved state with measurements, starting values, pulses or readings) keeps the mode that learning began with,
which is saved with it (a state saved without it began with the bootstrap), whatever is configured now. A learning
reset takes the configured mode again.

``LearningStart`` holds the pulses, the record of the measurement events and their reader, the calibration that
drives the pulses once the first model is published and applies its corrections (smartpi/calibration.py), and the few
calls ``SmartPI`` makes around them. It also tells the phase of the algorithm and what drives the command. It reads
the state it needs (phase, regulator, dead-time estimator, estimator) from the ``SmartPI`` it belongs to, and gives
its response model to the learning window. Times are monotonic seconds, as ``SmartPI.calculate`` reads them.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..const import LEARNING_START_BOOTSTRAP, LEARNING_START_DIRECT, LEARNING_STARTS
from ..hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from .calibration import (
    REQUEST_STARTED,
    RESULT_AGREED,
    RESULT_CANCELLED,
    Calibration,
    corrections,
    own_measures_agree,
    pulse_limits,
)
from .const import AB_MIN_SAMPLES_A, AB_MIN_SAMPLES_B, SmartPIPhase
from .event_reading import EventReader, ModelReader
from .feedforward import compute_ff_gain
from .measure_event import MODE_COOL, SOURCE_BOOTSTRAP, SOURCE_CALIBRATION, SOURCE_PULSE, SOURCE_REGULATION, MeasureRecord
from .measure_pulse import PULSE_MAX_COUNT, STATE_HEAT, STATE_REST, MeasurePulse
from .response_model import ResponseModel, seed_from_saved
from .timestamp_utils import monotonic_to_wall_ts

_LOGGER = logging.getLogger(__name__)

_PULSE_STEPS = {"idle": "none", "rest": "rest", "heat": "full power", "stop": "stop"}   # plain words of the state


# Plain values published while the direct start's learning stage runs (the bootstrap start keeps its own).
LEARNING_PULSES_STATE = "learning_pulses"        # state of the diagnostics sensor
LEARNING_PULSES_PHASE = "Learning"               # control.phase
LEARNING_PULSES_STAGE = "measurement pulses"     # learning.stage
LEARNING_PULSES_CONFIDENCE = "ab_learning"       # model.confidence


def learning_by_pulses(algo: Any) -> bool:
    """True while the first model is not published and the learning started direct: what is published then names the
    learning by measurement pulses, never the bootstrap or the hysteresis."""
    start = getattr(algo, "_start", None)
    return algo.phase == SmartPIPhase.HYSTERESIS and getattr(start, "direct", False) is True


def command_is_on_off(algo: Any) -> bool:
    """True while the command is applied as an on/off state (bootstrap hysteresis, measurement pulse).

    The learning stage alone does not tell: with the PI as command during the stage the command is not an on/off
    one (the algorithm's own property says so; an object without it keeps the stage).
    """
    return algo.phase == SmartPIPhase.HYSTERESIS and algo.command_is_on_off is not False


class LearningStart:
    """Start mode of the learning, the measurement pulses and their readings."""

    def __init__(self, learning_start: str = LEARNING_START_BOOTSTRAP, owner: Any = None, config: Any = None) -> None:
        if learning_start not in LEARNING_STARTS:
            raise ValueError(f"learning_start must be one of {LEARNING_STARTS} (got {learning_start!r})")
        self.configured = learning_start   # the option: what a blank start uses
        self.mode = learning_start         # the start of the learning in progress
        self._owner = owner
        self.pulse = MeasurePulse()
        self.record = MeasureRecord()
        self.reading = EventReader(self.record)
        # The response model (smartpi/response_model.py), fed by its reader of every event; the a/b learning reads it.
        self.model = ResponseModel()
        self.model_reading = ModelReader(self.model)
        self.model_reading.set_position(getattr(config, "latitude", None), getattr(config, "longitude", None))
        self.calibration = Calibration()
        self._pi_stage_seen = False      # a learning stage driven by the PI has been seen
        self._integral_zeroed = False    # the integral term was set to zero for this blank start
        self.recovering = False          # after a calibration pulse, until the room reaches the setpoint
        self._calibration_pulse_seen = False
        if owner is not None:
            events = owner.learn_win.events
            events.source = self.event_source
            events.record, events.reader = self.record, self.reading
            events.model_reader = self.model_reading
            owner.learn_win.response_model = self.model

    @property
    def direct(self) -> bool:
        """True when the PI, not the hysteresis, is the command during the learning stage."""
        return self.mode == LEARNING_START_DIRECT

    def _use(self, mode: str) -> None:
        """Take ``mode`` as the start of the learning in progress."""
        self.mode = mode
        self._owner.dt_est.direct = self.direct

    def reset(self) -> None:
        """Blank start: the configured start, the pulses are available again, the kept values and the response model are
        dropped, a calibration in progress is cancelled."""
        self._use(self.configured)
        self.cancel_calibration()
        self.pulse.reset()
        self.reading.reset()
        self.model_reading.reset()
        self.model.reset()
        self._pi_stage_seen = False
        self._integral_zeroed = False
        self.recovering = self._calibration_pulse_seen = False

    # -- the learning stage ------------------------------------------------------------------------------------
    def b_waiting(self, estimator: Any) -> bool:
        """Whether the learning stage still waits for ``b``. With the direct start ``b`` is usable when a starting
        value stands or the learning's own time constant has been reliable; otherwise when ``b`` has its first
        measurements or a value corrected by a calibration stands."""
        if self.direct:
            return not estimator.usable_b
        return len(estimator.b_meas_hist) < AB_MIN_SAMPLES_B and estimator.start.standing("b") is not True

    def stage_waiting(self, estimator: Any) -> bool:
        """True while the first model is not published: ``a`` has no measurements nor starting value, ``b`` is awaited,
        or the first publication of ``a`` waits for the delivered power model."""
        no_a = not estimator.usable_a
        return (no_a and len(estimator.a_meas_hist) < AB_MIN_SAMPLES_A) or self.b_waiting(estimator) or (
            no_a and not estimator.a_model_ready)

    @property
    def phase(self) -> SmartPIPhase:
        """Phase of the algorithm: the learning stage until the first model is published, else the calibration while
        one of its pulses commands the output, else the stable phase."""
        if self.stage_waiting(self._owner.est):
            return SmartPIPhase.HYSTERESIS
        return SmartPIPhase.CALIBRATION if self.pulse.for_calibration else SmartPIPhase.STABLE

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
        """Emitter lag kept from the readings in ``hvac_mode`` (anticipation horizon while the response model of the mode
        is not published), None when none is kept and with the bootstrap start, whose kept lag is published only."""
        return self.reading.kept_lag_s(hvac_mode) if self.direct else None

    def _blocked(self) -> bool:
        """True while the learning is blocked (smartpi/learning_block.py)."""
        return getattr(getattr(self._owner, "learning_block", None), "blocked", False) is True

    def hand_over(self, estimator: Any, hvac_mode: object) -> None:
        """Give the kept values of ``hvac_mode`` to the estimator as starting values, once per mode and blank start: ``a``
        of the two agreeing readings, and ``b`` with the first hand-over. With the bootstrap start they wait for the end
        of the learning stage; they wait too while the learning is blocked. A state saved with kept values and no
        starting values hands them over at its load, unless it was saved with the learning paused."""
        kept = self.reading.kept_for(hvac_mode)
        if kept is None or estimator.start.handed_for(kept["mode"]) or (not self.direct and self.learning_stage) or (
                self._blocked()):
            return
        sign = -1.0 if kept["mode"] == MODE_COOL else 1.0
        estimator.give_starting_values(kept["a_per_min"], kept.get("b_per_min"), kept["mode"], sign)

    def measures_start(self) -> bool:
        """True while a start is made by a measurement pulse or by the hysteresis, not by the regulation."""
        return self.pulse.active or self._owner.command_is_hysteresis

    def event_source(self) -> str:
        """What drives the command at the start of a measurement event (read by the event detector)."""
        owner = self._owner
        if self.pulse.active:
            return SOURCE_CALIBRATION if self.pulse.for_calibration else SOURCE_PULSE
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
            for record in (self.record, self.model_reading.record):
                record.note_cycle(start, float(receipt.on_time_sec), float(receipt.cycle_duration_sec))

    # -- per calculation ---------------------------------------------------------------------------------------
    def begin(self, hvac_mode: object) -> None:
        """First call of a calculation: the change of mode."""
        self.reading.begin(hvac_mode)
        self.model_reading.begin(hvac_mode)
        self.zero_integral()

    def advance(self, now: float, target: float, temp: float, hvac_mode: object, previous_command: float) -> None:
        """Advance the calibration and the measurement pulse (command of the pulse before the PI: direct start or
        calibration), then the readings of the events. A calibration whose end has come during its pulse ends at the
        calculation where the pulse ends. While a calibration is in progress the estimator takes no ``a`` measure. While
        the learning is blocked no pulse is advanced (none starts)."""
        owner = self._owner
        learning_stage = self.learning_stage
        cal = self.calibration
        if cal.in_progress:
            self._follow_calibration(now, hvac_mode, learning_stage, temp)
        calibrating = cal.in_progress
        if (self.direct or calibrating) and not self._blocked():
            heated = self.pulse.calibration_heated
            self.pulse.update(
                now, target, temp, hvac_mode,
                cooling=hvac_mode == VThermHvacMode_COOL,
                heat_or_cool=hvac_mode in (VThermHvacMode_HEAT, VThermHvacMode_COOL),
                learning_stage=learning_stage,
                model_known=owner.learn_win.model(hvac_mode) is not None,
                dt_est=owner.dt_est,
                history=owner.learn_win.temperatures.raw,
                previous_command=previous_command,
                readings_pending=self.reading.wants_pulses(hvac_mode),
                calibration=calibrating,
                calibration_pulses_left=cal.pulses < PULSE_MAX_COUNT,
                event_stage=owner.learn_win.events.phase_stage(str(hvac_mode)),
                limits=pulse_limits(
                    owner.learn_win.model(hvac_mode), hvac_mode, owner.anticipation_horizon(hvac_mode).seconds,
                    owner.learn_win.temperatures.raw,
                ) if calibrating else None,
            )
            if calibrating and self.pulse.calibration_heated > heated:
                cal.pulses += 1   # a calibration pulse counts once it heats
            if calibrating and self._calibration_pulse_seen and not self.pulse.for_calibration:
                self._follow_calibration(now, hvac_mode, learning_stage, temp)   # its pulse ended: it may end now
        self._follow_recovery(target, temp, hvac_mode)
        owner.est.calibrating = cal.in_progress
        self.reading.step(now, owner.learn_win.events.phase_start)
        self._step_model(now, hvac_mode)
        self.hand_over(owner.est, hvac_mode)
        self.record.enabled = self.reading.wants_record(learning_stage, hvac_mode) or cal.in_progress
        if not self.record.enabled:
            self.record.clear()
        self.zero_integral()

    def _step_model(self, now: float, hvac_mode: object) -> None:
        """The readings of the response model: what tells whether SmartPI is in control, the published ``b``."""
        owner, est = self._owner, self._owner.est
        block = getattr(owner, "learning_block", None)
        excluded = getattr(owner.restart, "excluded_vt", None) or getattr(block, "exclusion", None) or (
            "safety mode" if getattr(owner, "vt_safety_override", False) is True else None)
        self.model_reading.note_control(now, excluded, float(est.b) if getattr(est, "usable_b", False) is True else None)
        self.model_reading.step(now, owner.learn_win.events.phase_start)

    # -- the calibration -----------------------------------------------------------------------------------------
    @property
    def _name(self) -> str:
        return str(getattr(self._owner, "_name", ""))

    def cancel_calibration(self) -> None:
        """Cancel a calibration in progress (learning reset or learning blocked)."""
        if self.calibration.in_progress:
            self.calibration.finish(RESULT_CANCELLED)

    def block(self, now: float, reason: str) -> None:
        """The learning becomes blocked (smartpi/learning_block.py): a calibration in progress is cancelled, the pulse in
        progress ends and the readings in progress end without result."""
        self.cancel_calibration()
        self.pulse.interrupt(now, reason)
        self.reading.block(now, reason)
        self.model_reading.block(now, reason)

    def request_calibration(self, phase: SmartPIPhase, learning_enabled: bool) -> bool:
        """A calibration is requested (smartpi/calibration.py ``Calibration.request``: refused, ignored or started;
        the thermostat is off when its last calculation saw it off by the user, not by a window). True when a
        calibration starts."""
        off = getattr(self._owner, "_last_restart_reason", None) == "off"
        reason = self.calibration.request(phase == SmartPIPhase.HYSTERESIS, learning_enabled, off)
        if reason != REQUEST_STARTED:
            _LOGGER.info("%s - Calibration request %s", self._name, reason)
            return False
        self.reading.start_calibration()
        self.model.start_calibration()
        _LOGGER.info("%s - Calibration started", self._name)
        return True

    def follow_calibration(self, now: float, hvac_mode: object, temp: float | None = None) -> None:
        """End check of a calibration in progress on a calculation that does not advance the pulses (guard cut)."""
        if self.calibration.in_progress:
            self._follow_calibration(now, hvac_mode, self.learning_stage, temp)

    def _follow_recovery(self, target: float, temp: float, hvac_mode: object) -> None:
        """After each calibration pulse the integral is held until the room reaches the setpoint (``recovering``)."""
        if self._calibration_pulse_seen and not self.pulse.for_calibration:
            self.recovering = True
        self._calibration_pulse_seen = self.pulse.for_calibration
        if self.recovering and (temp - target if hvac_mode == VThermHvacMode_COOL else target - temp) <= 0.0:
            self.recovering = False

    def _follow_calibration(self, now: float, hvac_mode: object, learning_stage: bool, temp: float | None) -> None:
        """Note the dead time measured by a calibration pulse, then end the calibration when its end has come. A
        cancellation ends its pulse at once; any other end first ends the pulse in progress cleanly: a pulse in its
        rest hands back at once, one heating goes to its stop step, one in its stop step goes on, and the calibration
        ends at the calculation where that pulse ends."""
        cal = self.calibration
        cal.note_deadtimes(self._owner.dt_est, hvac_mode, self.pulse.for_calibration)
        result = cal.check(
            now, hvac_mode, learning_stage, pulse_active=self.pulse.for_calibration,
            readings_waiting=self.reading.calibration_in_progress,
            agreed=self.reading.calibration_reading(cal.mode) is not None, max_pulses=PULSE_MAX_COUNT,
        )
        if result is None:
            return
        if self.pulse.for_calibration:
            if result == RESULT_CANCELLED or self.pulse.state == STATE_REST:
                self.pulse.interrupt(now, "calibration ended")
            else:
                if self.pulse.state == STATE_HEAT:
                    self.pulse.stop_heating(now, "calibration ended", temp)
                return
        self.reading.end_calibration(now)
        if result == RESULT_AGREED:
            self._correct(self.reading.calibration_reading(cal.mode), cal.mode)
        cal.finish(result)
        _LOGGER.info("%s - Calibration ended: %s (corrected: %s)", self._name, result, ", ".join(cal.corrected) or "none")

    def _correct(self, reading: dict[str, Any], mode: str) -> None:
        """Compare the agreeing reading with the values in force in ``mode`` and apply the corrections (rule in
        smartpi/calibration.py). The step of the learned holding power is moved into the integral at the next
        regulation calculation (a change of Kp is not moved into it), and the feed-forward ramp keeps its count."""
        owner, est, cal = self._owner, self._owner.est, self.calibration
        in_force = {"a": abs(est.a), "b": est.b, "lag_s": owner.anticipation_horizon(mode).seconds}
        own = {"a": own_measures_agree(est.a_meas_hist, est.a_drift.drift_buffer, in_force["a"]),
               "b": own_measures_agree(est.b_meas_hist, est.b_drift.drift_buffer, in_force["b"])}
        wanted = corrections(reading, in_force, own)
        cal.in_force = in_force
        if not wanted:
            return
        k_ff, ramp = compute_ff_gain(est, mode, owner._tau_reliable), est.ramp_count
        sign = -1.0 if mode == MODE_COOL else 1.0
        done = []
        if "b" in wanted and est.correct_b(float(reading["b_per_min"]), sign):
            done.append("b")
        if "a" in wanted:
            est.correct_a(mode, float(reading["a_per_min"]))
            done.append("a")
        est.start.latch(ramp)
        owner._ff_holding.note_learned_step(k_ff)
        cal.corrected = [q for q in ("a", "b") if q in done]

    def interrupt(self, now: float, reason: str) -> None:
        """The thermostat stops (off, window, shedding): the pulse and the readings in progress end."""
        self.pulse.interrupt(now, reason)
        self.reading.interrupt(now, reason)
        self.model_reading.interrupt(now, reason)

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
            "pulse": "none" if self.pulse.for_calibration else _PULSE_STEPS.get(self.pulse.state, "none"),
            "values_kept": kept is not None,
            "values_kept_at": None if at is None else datetime.fromtimestamp(at).isoformat(),
            "kept_a": None if kept is None else round(kept["a_per_min"], 5),
            "kept_b": None if b is None else round(b, 6),
            "kept_lag_s": None if kept is None else round(kept["lag_s"]),
        }

    def calibration_public(self) -> dict[str, Any]:
        """Plain summary of the calibration: its state (the step of its pulse in progress in words, "waiting" between
        pulses, "idle" without calibration), its pulses, result, dead time and agreeing reading."""
        cal = self.calibration
        step = _PULSE_STEPS.get(self.pulse.state, "none") if self.pulse.for_calibration else "waiting"
        mode = cal.mode or self._owner.est.model_hvac_mode
        return cal.public(step, self.reading.calibration_reading(mode))

    # -- persistence -------------------------------------------------------------------------------------------
    def save(self, now: float) -> dict[str, Any]:
        """The start of the learning when it is the direct one (a state without it began with the bootstrap), the
        pulses started (a pulse in progress is saved as ended by the restart), the calibration, the record of the events
        and the readings with their kept values, and the response model with its readings and record."""
        state: dict[str, Any] = {"learning_start": self.mode} if self.direct else {}   # no key: the bootstrap
        pulse = self.pulse.save(now)
        if pulse is not None:
            state["measure_pulse"] = pulse
        calibration = self.calibration.save()
        if calibration is not None:
            state["calibration"] = calibration
        reading = self.reading.save()
        if reading is not None:
            state["event_reading"] = reading
        record = self.record.save()
        if record is not None:
            state["measure_events"] = record
        # always saved: a state without it is one of an earlier version, whose model is seeded at its load
        state["response_state"] = {"model": self.model.save(), "reading": self.model_reading.save(),
                                   "record": self.model_reading.record.save()}
        return state

    def _begun(self, state: dict[str, Any]) -> bool:
        """True when the learning of a saved state has begun: measurements, starting values, pulses or readings."""
        est = self._owner.est
        return bool(
            est.a_meas_hist or est.b_meas_hist or est.learn_ok_count or est.start.handed
            or state.get("measure_pulse") or state.get("event_reading") or state.get("pulse_reading")
        )

    def load(self, state: dict[str, Any]) -> None:
        """Restore what ``save`` wrote. A learning that has begun goes on with the start it began with, a blank one takes
        the configured start. A calibration saved by the former format (``cal_state``) is not resumed."""
        saved = state.get("learning_start")
        if not self._begun(state):
            self._use(self.configured)
        else:
            self._use(saved if saved in LEARNING_STARTS else LEARNING_START_BOOTSTRAP)
        self._pi_stage_seen = False
        self._integral_zeroed = False
        self.pulse.load(state.get("measure_pulse"))
        self.calibration.load(state.get("calibration"), state.get("cal_state"))
        self.reading.load(state.get("event_reading") or state.get("pulse_reading"))
        self.record.load(state.get("measure_events"))
        self._load_model(state)
        if state.get("learning_enabled", True) is not False:
            self.hand_over(self._owner.est, self._owner.est.model_hvac_mode)

    def _load_model(self, state: dict[str, Any]) -> None:
        """The response model, its readings and record; a state saved without it seeds the model from the dead times
        and lags of the earlier estimators (smartpi/response_model.py)."""
        saved = state.get("response_state")
        if not isinstance(saved, dict):
            self.model_reading.reset()
            self.model.reset()
            for mode, (theta, tau) in seed_from_saved(state).items():
                self.model.seed(mode, theta, tau)
            return
        self.model.load(saved.get("model"))
        self.model_reading.load(saved.get("reading"))
        self.model_reading.record.load(saved.get("record"))
