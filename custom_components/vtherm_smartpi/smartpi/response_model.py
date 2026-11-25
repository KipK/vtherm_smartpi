"""Dynamic model of the response of the room to the power: dead time and response time constant, per mode.

The model of the chain is ``u(t) -> delay theta -> 1/(tau_e s + 1) -> dT/dt = a q - b (T - T_outdoor)``. ``theta``
(dead time: sensor, transport, actuator) and ``tau_e`` (response time constant: the emitter, the air and the nearby
mass together) are fitted on the measurement events by
``smartpi/event_fit.py`` (``fit_response``); the reader of the model (``ModelReader``, smartpi/event_reading.py)
admits the events and hands each fit here. ``a`` and ``b`` stay owned by the a/b estimator.

Per mode the last ``RESPONSE_MODEL_HISTORY`` accepted events are kept (theta, tau_e, S = theta + tau_e, whether the
fit tells theta apart, source, daylight, time). An event in daylight (sun on the room) is shown, not kept, unless the
mode has no event yet: it is then kept until the first event in darkness replaces it. Published values:

- S: the median S of the events; theta: the median theta of the events that tell it apart (at most S), else S / 2
  (the middle of [0, S]: effective dead time 3 S / 4); tau_e = S - theta;
- confidence: ``dead time assumed`` without an event that tells theta apart, ``measured`` when two events agree on S
  within ``READING_AGREE_REL``, else ``provisional`` (one event is enough to publish);
- the events of a calibration enter the history; when two of them agree on S and their mean S is off the S published
  before them by more than ``CALIBRATION_CORRECT_REL``, those two replace the history of the mode.

Outliers need a confirming event. The reference of a mode is its published S (the median of its events, or the seed
while it has none). Any other event whose S lies outside [S_ref / ``OUTLIER_RATIO``, ``OUTLIER_RATIO`` S_ref] is kept
pending (at most ``OUTLIERS_KEPT`` per mode, the oldest dropped) and changes nothing. A later event outside the band
on the same side that agrees with a pending one on S within ``READING_AGREE_REL`` confirms it: both replace the
history of the mode (a durable change of the room, such as a new emitter, still moves the model) and the pending
events of the mode are dropped. A mode without a reference feeds its first event.

Derived values: effective dead time theta + tau_e / 2 (gains, holds), emitter lag theta + tau_e (horizon).

The a/b learning (smartpi/learning_window.py, smartpi/power_steps.py) reads the model of its mode: no window of the
learning stage before it is published; the power delivered to the room is the command delayed by theta, then a first
order of tau_e (``delivered_fraction``); a window starts the emitter lag theta + tau_e = S after the power step or
the cut (``settle_s``; the split does not change it, assumed or not): the delivered power, counted in ``a`` and in
``b``, corrects the rest of the transient.

A state saved without this model is seeded once (``seed_from_saved``) so that the effective dead time equals the saved
active dead time L: with an emitter lag S of the earlier estimators theta = clamp(2L - S, 0, L), with L only
theta = tau_e = 2 L / 3 (the split of S / 2 each). The seed is published as ``previous version``,
``provisional``, until the first event fed to the mode. A model saved without pending events (before the outlier
rule) is repaired at load (``repair``): a mode whose seed can be computed again from the saved state and was dropped
gets it back as its reference, and its saved events are read again in order through the rule.

Prediction: each fed event carries the rms of the model in force before it on its window (theta and tau_e fixed,
``a``, ``b`` and the start temperature solved); the median of the last ``PREDICTIONS_KEPT`` is published. Shown only.

Peak samples: each confirmed peak after a cut gives tau_e = (t_peak - theta) / ln(1 + s_cut / l), with l the loss rate
b (T_cut - T_outdoor) and s_cut the slope before the cut. They are published only.

The anticipation horizon reads the emitter lag theta + tau_e (smartpi/anticipation.py); the near band reads the
effective dead time above the setpoint and the emitter lag below it (smartpi/deadband.py); the calibration pulses size
their limits from both (smartpi/calibration.py); the learning pulses of the direct start go on while the model of the
mode is not published (smartpi/measure_pulse.py). When the split is assumed (theta = S / 2) those values are the
cautious ends: emitter lag S, effective dead time 3 S / 4. The integral holds read the effective dead time
(smartpi/gains.py, smartpi/resume_hold.py); the gains keep the dead time after a start.
Internal codes become plain words here.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from .calibration import CALIBRATION_CORRECT_REL
from .event_fit import READING_AGREE_REL
from .measure_event import (
    MODE_COOL,
    MODE_HEAT,
    SOURCE_BOOTSTRAP,
    SOURCE_CALIBRATION,
    SOURCE_PULSE,
    SOURCE_REGULATION,
    _number,
)
from .timestamp_utils import monotonic_to_wall_ts

RESPONSE_MODEL_HISTORY = 7
PEAK_SAMPLES_KEPT = 7
PREDICTIONS_KEPT = 7
PEAK_MIN_S = 600.0             # time from the dead time to the peak of a usable peak sample (two peak bins)
PEAK_SLOPE_MAX_S = 1800.0      # longest span of the slope before the cut
OUTLIER_RATIO = 2.0            # an event with S off the published S by this factor needs a confirming event
OUTLIERS_KEPT = 3              # pending events kept per mode

SOURCE_PREVIOUS = "previous"
_SOURCES = {
    SOURCE_PULSE: "measurement pulses", SOURCE_BOOTSTRAP: "bootstrap", SOURCE_CALIBRATION: "calibration",
    SOURCE_REGULATION: "regulation", SOURCE_PREVIOUS: "previous version",
}
CONFIDENCE_MEASURED = "measured"
CONFIDENCE_PROVISIONAL = "provisional"
CONFIDENCE_ASSUMED = "dead time assumed"

OUTCOME_ACCEPTED = "accepted"
OUTCOME_DAYTIME = "not used in daylight"
OUTCOME_DAYTIME_FIRST = "used in daylight until an event in darkness"
OUTCOME_LOWER_LIMIT = "accepted, response time constant at its lower limit"
OUTCOME_OUTLIER = "not used: far from the model in force, waiting for a confirming event"
OUTCOME_CONFIRMED = "accepted with an earlier event far from the model, both replace it"

_EVENT_KEYS = ("theta_s", "tau_e_s", "s_s")


def plain_source(source: str | None) -> str | None:
    return None if source is None else _SOURCES.get(source, source)


def _agree(s1: float, s2: float) -> bool:
    return abs(s1 - s2) <= READING_AGREE_REL * max(s1, s2) + 1e-9


class ResponseModel:
    """Events, published values and diagnostics of the response model, per mode (``MODE_HEAT``, ``MODE_COOL``)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Blank start: no event, no seed, no diagnostics."""
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._seed: dict[str, dict[str, float]] = {}
        self._pending: dict[str, list[dict[str, Any]]] = {}       # outliers waiting for a confirming event
        self._cal: dict[str, list[dict[str, Any]] | None] = {}   # accepted events of the calibration in progress
        self.last: dict[str, Any] | None = None
        self.last_reason: str | None = None
        self._peaks: list[tuple[str, float]] = []
        self._predictions: list[tuple[str, float]] = []

    # -- published values ----------------------------------------------------------------------------------------
    def values(self, mode: object) -> dict[str, Any] | None:
        """theta_s, tau_e_s, source and confidence of ``mode``; None without event nor seed."""
        mode = None if mode is None else str(mode)
        events = self._events.get(mode) or []
        if not events:
            seed = self._seed.get(mode)
            if seed is None:
                return None
            return dict(seed, source=SOURCE_PREVIOUS, confidence=CONFIDENCE_PROVISIONAL)
        s_pub = statistics.median(e["s_s"] for e in events)
        split = [e["theta_s"] for e in events if e["split_ok"]]
        theta = min(statistics.median(split), s_pub) if split else s_pub / 2.0
        agreed = any(_agree(a["s_s"], b["s_s"]) for i, a in enumerate(events) for b in events[i + 1:])
        confidence = CONFIDENCE_ASSUMED if not split else CONFIDENCE_MEASURED if agreed else CONFIDENCE_PROVISIONAL
        return {"theta_s": theta, "tau_e_s": s_pub - theta, "source": events[-1]["source"], "confidence": confidence}

    def effective_dead_time_s(self, mode: object) -> float | None:
        """theta + tau_e / 2."""
        v = self.values(mode)
        return None if v is None else v["theta_s"] + 0.5 * v["tau_e_s"]

    def emitter_lag_s(self, mode: object) -> float | None:
        """theta + tau_e."""
        v = self.values(mode)
        return None if v is None else v["theta_s"] + v["tau_e_s"]

    def event_count(self, mode: object) -> int:
        return len(self._events.get(None if mode is None else str(mode)) or [])

    def pending_count(self, mode: object) -> int:
        return len(self._pending.get(None if mode is None else str(mode)) or [])

    # -- events --------------------------------------------------------------------------------------------------
    def start_calibration(self) -> None:
        """A calibration starts: its events are compared among themselves."""
        self._cal = {}

    def refuse(self, reason: str) -> None:
        """An event gives no fit (plain reason)."""
        self.last_reason = reason

    def add(self, mode: str, source: str, fit: dict[str, Any], now: float, daytime: bool = False) -> str:
        """A fitted event of ``mode``: accepted unless the fit rejects it or it ran in daylight while the mode has an
        event. Returns the outcome in plain words."""
        events = self._events.setdefault(mode, [])
        outcome = fit.get("reject") or (
            (OUTCOME_LOWER_LIMIT if fit.get("tau_e_lower_limit") else OUTCOME_ACCEPTED) if not daytime
            else OUTCOME_DAYTIME if events else OUTCOME_DAYTIME_FIRST)
        self.last = {
            "source": plain_source(source),
            "dead_time_s": _round(fit.get("theta_s")), "response_time_constant_s": _round(fit.get("tau_e_s")),
            "rms_c": None if fit.get("rms_c") is None else round(fit["rms_c"], 4), "outcome": outcome,
            "daytime": bool(daytime), "prediction_rms_c": None,
        }
        self.last_reason = outcome
        if outcome not in (OUTCOME_ACCEPTED, OUTCOME_LOWER_LIMIT, OUTCOME_DAYTIME_FIRST):
            return outcome
        prediction = _number(fit.get("prediction_rms_c"))
        if prediction is not None:
            self.last["prediction_rms_c"] = round(prediction, 4)
        event = {key: float(fit[key]) for key in _EVENT_KEYS}
        event.update(split_ok=bool(fit["split_ok"]), source=source, daytime=bool(daytime), at_wall=monotonic_to_wall_ts(now))
        placed = self._place(mode, event)
        if placed is not None:
            outcome = self.last["outcome"] = self.last_reason = placed
        if placed != OUTCOME_OUTLIER and prediction is not None:
            self._predictions.append((mode, prediction))
            del self._predictions[:-PREDICTIONS_KEPT]
        return outcome

    def _side(self, mode: str, s_s: float) -> int:
        """+1 (above) or -1 (below) when ``s_s`` lies outside the band of the published S of ``mode``, 0 inside it or
        without a published S."""
        ref = self.emitter_lag_s(mode)
        if ref is None or ref <= 0.0:
            return 0
        return 1 if s_s > OUTLIER_RATIO * ref + 1e-9 else -1 if s_s < ref / OUTLIER_RATIO - 1e-9 else 0

    def _place(self, mode: str, event: dict[str, Any], replay: bool = False) -> str | None:
        """Feed ``event`` to ``mode`` or keep it pending. Returns ``OUTCOME_OUTLIER`` (pending), ``OUTCOME_CONFIRMED``
        (fed with the pending event it confirms) or None (fed). A saved calibration event read again (``replay``)
        is fed as it was."""
        events = self._events.setdefault(mode, [])
        calibration = event["source"] == SOURCE_CALIBRATION
        side = 0 if calibration else self._side(mode, event["s_s"])
        pending = self._pending.setdefault(mode, [])
        match = next((p for p in reversed(pending) if p["side"] == side and _agree(p["s_s"], event["s_s"])), None)
        if side and match is None:
            pending.append(dict(event, side=side))
            del pending[:-OUTLIERS_KEPT]
            return OUTCOME_OUTLIER
        if side:
            pending.clear()                # the model moves: the other pending events were read against the former one
            events[:] = [{k: v for k, v in match.items() if k != "side"}]
        if not event["daytime"]:
            events[:] = [e for e in events if not e["daytime"]]
        if calibration and not replay:
            self._add_calibration(mode, event, events)
        else:
            events.append(event)
        del events[:-RESPONSE_MODEL_HISTORY]
        self._seed.pop(mode, None)
        return OUTCOME_CONFIRMED if side else None

    def _add_calibration(self, mode: str, event: dict[str, Any], events: list[dict[str, Any]]) -> None:
        cal = self._cal.setdefault(mode, [])
        if cal is None:                   # the pair of this calibration was already compared
            events.append(event)
            return
        before = [e for e in events if e not in cal]
        events.append(event)
        cal.append(event)
        if len(cal) < 2 or not _agree(cal[-2]["s_s"], cal[-1]["s_s"]):
            return
        self._cal[mode] = None
        reference = statistics.median(e["s_s"] for e in before) if before else None
        s_cal = 0.5 * (cal[-2]["s_s"] + cal[-1]["s_s"])
        if reference is not None and abs(s_cal - reference) > CALIBRATION_CORRECT_REL * reference:
            events[:] = cal[-2:]

    def add_peak(self, mode: str, tau_e_s: float) -> None:
        self._peaks.append((mode, float(tau_e_s)))
        del self._peaks[:-PEAK_SAMPLES_KEPT]

    def seed(self, mode: str, theta_s: float, tau_e_s: float) -> None:
        """Values of the previous estimators, published until the first event fed to ``mode``."""
        if not self._events.get(mode):
            self._seed[mode] = {"theta_s": float(theta_s), "tau_e_s": float(tau_e_s)}

    def repair(self, seeds: dict[str, tuple[float, float]]) -> None:
        """A model saved before the outlier rule: each mode of ``seeds`` (theta_s, tau_e_s computed again from the
        saved state) that has events and no seed gets the seed back as its reference; its saved events are read again
        in order through the rule, those it would not feed become pending. A mode without a seed keeps its events."""
        for mode, (theta, tau) in seeds.items():
            saved = self._events.get(mode)
            if not saved or mode in self._seed or self._pending.get(mode):
                continue
            self._events[mode] = []
            self._seed[mode] = {"theta_s": float(theta), "tau_e_s": float(tau)}
            for event in saved:
                self._place(mode, event, replay=True)

    # -- published diagnostics -----------------------------------------------------------------------------------
    def public_effective_dead_time_s(self, mode: object) -> int | None:
        """``analysis.control.effective_dead_time_s`` of ``mode`` (whole seconds)."""
        return _round(self.effective_dead_time_s(mode))

    def public_model(self, mode: object) -> dict[str, Any]:
        """``model.*`` values of ``mode``."""
        v = self.values(mode)
        return {
            "dead_time_s": None if v is None else _round(v["theta_s"]),
            "response_time_constant_s": None if v is None else _round(v["tau_e_s"]),
            "response_source": None if v is None else plain_source(v["source"]),
            "response_confidence": None if v is None else v["confidence"],
        }

    def public_learning(self, mode: object) -> dict[str, Any]:
        """``analysis.learning.response_model`` of ``mode``."""
        mode = None if mode is None else str(mode)
        return {
            "events": self.event_count(mode),
            "pending_outliers": self.pending_count(mode),
            "last": None if self.last is None else dict(self.last),
            "last_reason": self.last_reason,
            "peak_samples": [round(tau) for m, tau in self._peaks if m == mode],
            "prediction_rms_c": _median_rms([p for m, p in self._predictions if m == mode]),
        }

    # -- persistence ---------------------------------------------------------------------------------------------
    def save(self) -> dict[str, Any]:
        return {
            "events": {mode: [dict(e) for e in events] for mode, events in self._events.items() if events},
            "seed": {mode: dict(seed) for mode, seed in self._seed.items()},
            "pending": {mode: [dict(e) for e in pending] for mode, pending in self._pending.items() if pending},
            "last": self.last, "last_reason": self.last_reason, "peaks": [list(p) for p in self._peaks],
            "predictions": [list(p) for p in self._predictions],
        }

    def load(self, saved: object) -> None:
        """Restore what ``save`` wrote; anything malformed is skipped."""
        self.reset()
        if not isinstance(saved, dict):
            return
        for mode, events in _items(saved.get("events")):
            for e in events if isinstance(events, list) and mode in (MODE_HEAT, MODE_COOL) else []:
                if (event := _load_event(e)) is not None:
                    self._events.setdefault(mode, []).append(event)
            if mode in self._events:
                del self._events[mode][:-RESPONSE_MODEL_HISTORY]
        for mode, pending in _items(saved.get("pending")):
            for e in pending if isinstance(pending, list) and mode in (MODE_HEAT, MODE_COOL) else []:
                if (event := _load_event(e)) is not None and e.get("side") in (1, -1):
                    self._pending.setdefault(mode, []).append(dict(event, side=e["side"]))
            if mode in self._pending:
                del self._pending[mode][:-OUTLIERS_KEPT]
        for mode, seed in _items(saved.get("seed")):
            if mode in (MODE_HEAT, MODE_COOL) and isinstance(seed, dict) and all(
                    _number(seed.get(k)) is not None for k in ("theta_s", "tau_e_s")):
                self.seed(mode, seed["theta_s"], seed["tau_e_s"])
        self.last = dict(saved["last"]) if isinstance(saved.get("last"), dict) else None
        self.last_reason = saved["last_reason"] if isinstance(saved.get("last_reason"), str) else None
        for p in saved.get("peaks") if isinstance(saved.get("peaks"), list) else []:
            if isinstance(p, list) and len(p) == 2 and p[0] in (MODE_HEAT, MODE_COOL) and _number(p[1]) is not None:
                self._peaks.append((p[0], float(p[1])))
        for p in saved.get("predictions") if isinstance(saved.get("predictions"), list) else []:
            if isinstance(p, list) and len(p) == 2 and p[0] in (MODE_HEAT, MODE_COOL) and _number(p[1]) is not None:
                self._predictions.append((p[0], float(p[1])))


def _load_event(e: object) -> dict[str, Any] | None:
    """A saved event, None when malformed."""
    if not isinstance(e, dict) or not all(_number(e.get(k)) is not None for k in _EVENT_KEYS):
        return None
    return {
        **{k: float(e[k]) for k in _EVENT_KEYS}, "split_ok": e.get("split_ok") is True,
        "daytime": e.get("daytime") is True,
        "source": e["source"] if isinstance(e.get("source"), str) else SOURCE_REGULATION,
        "at_wall": _number(e.get("at_wall")),
    }


def _round(value: Any) -> int | None:
    return None if value is None else int(round(value))


def _median_rms(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _items(value: object):
    return value.items() if isinstance(value, dict) else ()


def peak_sample(theta_s: float, peak_delay_s: float, slope_c_min: float | None, loss_c_min: float) -> float | None:
    """tau_e from the time to the peak after a cut (``peak_delay_s``), the slope of the room before the cut and its
    loss rate there (both in C/min, in the direction of the heating); None when the sample is not usable (the slope
    under the loss rate, or the peak less than ``PEAK_MIN_S`` after the dead time)."""
    if slope_c_min is None or loss_c_min <= 0.0 or slope_c_min < loss_c_min or peak_delay_s - theta_s < PEAK_MIN_S:
        return None
    return (peak_delay_s - theta_s) / math.log1p(slope_c_min / loss_c_min)


# -- a/b learning -----------------------------------------------------------------------------------------------------
def settle_s(values: dict[str, Any] | None) -> float | None:
    """Wait of a window after a power step (``a``) or a cut (``b``): the emitter lag theta + tau_e = S, the same
    whether the split is measured or assumed (the delivered power corrects the rest of the transient); None without a
    model."""
    if values is None:
        return None
    return values["theta_s"] + values["tau_e_s"]


def delivered_fraction(start_s: float, duration_s: float, values: dict[str, Any] | None) -> float:
    """Mean fraction of a power step from rest delivered over a window of ``duration_s`` starting ``start_s`` after the
    step: delay theta, then a first order of tau_e (the published values). 1 without a model."""
    if values is None or duration_s <= 0.0:
        return 1.0
    delay, tau = values["theta_s"], values["tau_e_s"]
    end = start_s + duration_s
    on = max(start_s, delay)
    if end <= on:
        return 0.0
    if tau <= 0.0:
        return (end - on) / duration_s
    return ((end - on) - tau * (math.exp(-(on - delay) / tau) - math.exp(-(end - delay) / tau))) / duration_s


# -- migration -------------------------------------------------------------------------------------------------------
def _reliable(value: object, reliable: object) -> float | None:
    v = _number(value)
    return v if reliable is True and v is not None and v > 0.0 else None


def _saved_dead_time(dt_state: dict, mode: str) -> float | None:
    """Active dead time L of ``mode`` in a saved dead-time state (the active slot: heat in HEAT, cool in COOL)."""
    slot = "heat" if mode == MODE_HEAT else "cool"
    snap = dt_state if dt_state.get("model_hvac_mode") == mode else (dt_state.get("mode_stash") or {}).get(mode)
    if not isinstance(snap, dict):
        return None
    return _reliable(snap.get(f"deadtime_{slot}_s"), snap.get(f"deadtime_{slot}_reliable"))


def _saved_lag(state: dict, mode: str) -> float | None:
    """Emitter lag S of the earlier estimators: the kept lag of the readings, the kept lag of a saved heating balance
    (HEAT), the response lag."""
    reading = state.get("event_reading") or state.get("pulse_reading")
    kept = reading.get("kept") if isinstance(reading, dict) else None
    if isinstance(kept, dict) and isinstance(kept.get("mode"), str):
        kept = {kept["mode"]: kept}
    lag = _number((kept.get(mode) or {}).get("lag_s")) if isinstance(kept, dict) and isinstance(kept.get(mode), dict) else None
    if lag is not None and lag > 0.0:
        return lag
    lw = state.get("lw_state") if isinstance(state.get("lw_state"), dict) else {}
    balance = lw.get("heating_balance")
    if mode == MODE_HEAT and isinstance(balance, dict):
        lags = [v for v in map(_number, balance.get("lags") or []) if v is not None and v > 0.0]
        if not lags:
            lags = [v for v in (_number(r.get("lag_s")) for r in balance.get("results") or [] if isinstance(r, dict))
                    if v is not None and v > 0.0]
        if lags:
            return statistics.median(lags)
    lag_state = lw.get("response_lag")
    if isinstance(lag_state, dict):
        snap = lag_state if lag_state.get("hvac_mode") == mode else (lag_state.get("mode_stash") or {}).get(mode)
        lag = _number(snap.get("published")) if isinstance(snap, dict) else None
        if lag is not None and lag > 0.0:
            return lag
    return None


def seed_from_saved(state: dict) -> dict[str, tuple[float, float]]:
    """Seed (theta_s, tau_e_s) by mode from a state saved without the response model; a mode without a saved active
    dead time has none."""
    dt_state = state.get("dt_est_state") if isinstance(state.get("dt_est_state"), dict) else {}
    seeds = {}
    for mode in (MODE_HEAT, MODE_COOL):
        dead = _saved_dead_time(dt_state, mode)
        if dead is None:
            continue
        lag = _saved_lag(state, mode)
        theta = 2.0 * dead / 3.0 if lag is None else min(max(2.0 * dead - lag, 0.0), dead)
        seeds[mode] = (theta, 2.0 * (dead - theta))
    return seeds
