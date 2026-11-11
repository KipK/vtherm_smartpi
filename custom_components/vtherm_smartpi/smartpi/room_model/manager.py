"""Orchestration of the room model (event loop side, pure Python).

Once published, the room model gives the regulation its feed-forward holding
power in heating and the equilibrium of the braking prediction
(feedforward.py). The manager feeds the observer at each regulation tick and
with each applied command, schedules the computations (jobs.py, run in an
executor on the history recorded by the T_ar learning), publishes the
qualified parameters, and holds the persistence and the diagnostics.

Identification: the quiet periods come from the shared catalogue of the T_ar
learning (no sun, constant setpoint, valid data). Each closed period that
completes a window of three periods starts one identification of that window
(keyed by the period, never computed twice), compared with the window of the
three periods before it. The jobs run at most once per _JOB_MIN_GAP_S of
elapsed time, oldest window first; no local hour is involved. The observer
gives each closed period the mean of its disturbance, and the periods that
stand out are left out of the fits.

Energy balance: once a local day is over, a job computes the static model of
the whole days before it (energy_balance.py): the slope b_eq / a and the
constant free gain, whatever the quiet periods. A qualified model comes into
force; a refused day keeps the model in force and records why. The model in
force gives the static part of the feed-forward once a room model is
published, and its slope is the reference that the fits must agree with to be
published.

Emitter: the first moment of the emitter response is the HEAT dead time in
force (learned T_ar or bootstrap); its first-order part tau_e is the refined
T_ar value when published (else the whole first moment), the delay L the rest.
The identification refines that first moment within a bounded grid.

Restarts: the time of the last tick and of the save are kept with the state, so
that a bridged restart (restart_bridge.py) holds the command applied at the
stop over the downtime (observer.py).

Power shedding keeps the observer running: the heater is off at a known power.
From the first tick of a shedding period the command timeline is held at zero
until SmartPI is back in control, so the rest of an interrupted cycle is not
counted as heating.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import time
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from ..tar_learning.clock import LocalClock
from ..tar_learning.manager import STATUS_UNAVAILABLE
from . import gates, public, periods
from .const import (
    RM_EXCLUDED_SHEDDING,
    RM_EXCLUDED_WINDOW,
    RM_HORIZON_LABELS,
    RM_MAX_RECORDS,
    RM_MAX_WINDOWS,
    RM_PUB_LAST,
)
from .emitter import split_first_moment
from .model import RoomParams
from .observer import HEALTH_NO_MODEL, HEALTH_OK, HEALTH_PAUSED, HEALTH_WAITING, RoomObserver

_LOGGER = logging.getLogger(__name__)

# 2: identification windows keyed by quiet period, disturbance of the observer by period, separate energy
# balance schedule
STATE_VERSION = 2
# Shortest elapsed time between two identification jobs
_JOB_MIN_GAP_S = 3600.0
# The energy balance of a finished local day waits this long after its end, for the last recorded data
_EB_DELAY_S = 1200.0

KIND_IDENTIFY = "identify"
KIND_BALANCE = "balance"


def _run_job(kind: str, snap: dict) -> dict:
    """Executor entry point: the numerical modules are imported here, never on the event loop."""
    from . import jobs  # pylint: disable=import-outside-toplevel

    if kind == KIND_BALANCE:
        return jobs.balance_job(snap)
    return jobs.identify_job(snap)


def _per_min(x: float | None) -> float | None:
    """A per-second parameter in per-minute units, rounded like model.a and model.b."""
    return None if x is None else round(float(x) * 60.0, 6)


def _r(x: float | None, digits: int) -> float | None:
    return None if x is None else round(float(x), digits)


def _date(day: int | None) -> str | None:
    return None if day is None else _dt.date.fromordinal(int(day)).isoformat()


def _measure_key(meas_id: Any) -> tuple[Any, float | None]:
    """Identity and epoch time of a temperature measurement id (a datetime in the integration)."""
    if meas_id is None:
        return None, None
    if hasattr(meas_id, "timestamp"):
        try:
            return meas_id.isoformat(), float(meas_id.timestamp())
        except (TypeError, ValueError, OverflowError):
            return str(meas_id), None
    return str(meas_id), None


class RoomModelManager:
    """Room model of one thermostat."""

    def __init__(self, name: str, time_zone: str | None, tar) -> None:
        self._name = name
        self._tz = time_zone
        self._clock = LocalClock(time_zone)
        self._tar = tar
        self._running = False
        self.dirty = False
        self._a_per_min: float | None = None
        self._emitter: tuple[float, float] | None = None
        self._shedding = False
        # Last tick of this session; the saved last tick and save time of a restart to decide
        self._last_t: float | None = None
        self._restart_saved: dict[str, float | None] | None = None
        self._restart_pending = False
        self._reset_results()

    def _reset_results(self) -> None:
        self.observer = RoomObserver()
        self._restart_saved = None
        self._restart_pending = False
        self.params: RoomParams | None = None
        self.q: float | None = None
        self.windows: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.records: list[dict] = []
        self.published_day: int | None = None
        self.wall_code: str | None = None
        self.last_error: str | None = None
        # Static model in force from the energy balance: slope (b_eq / a units), constant free gain (share of
        # full power), number of days and the day it was computed; None before a first qualified value. Why
        # the last computation gave no new value, None when it qualified.
        self.eb_slope: float | None = None
        self.eb_gain: float | None = None
        self.eb_days: int | None = None
        self.eb_day: int | None = None
        self.eb_reason: str | None = None
        # Last day whose energy balance was computed (None: none yet); time of the last identification job
        self.eb_run_day: int | None = None
        self.last_job_t: float | None = None

    def reset(self) -> None:
        """Forget the identified parameters and the observer state."""
        self._reset_results()
        self.dirty = True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """The identification can run (numerical dependencies found, heater supported)."""
        return self._tar is not None and self._tar.active

    def _blocked_reason(self) -> str | None:
        if self._tar is None:
            return public.REASON_DEPENDENCIES
        if not self._tar.supported:
            return public.REASON_VALVE
        if self._tar.status == STATUS_UNAVAILABLE:
            return public.REASON_DEPENDENCIES
        return None

    # ------------------------------------------------------------------
    # Inputs (called from the regulation, must stay cheap)
    # ------------------------------------------------------------------
    def observe(self, now: float, *, hvac_mode, temp: float | None, measure_id: Any, ext: float | None,
                excluded: str | None, a_per_min: float | None, first_moment_s: float | None) -> None:
        """Record one regulation tick.

        excluded: why SmartPI is not in control of the heater, None when it is. An open window is
        followed with the measured air; power shedding is tracked with the heater off; any other
        reason pauses the observer.
        first_moment_s: HEAT dead time in force, the first moment of the emitter response.
        """
        self._settle_restart()
        self._last_t = now
        self._a_per_min = a_per_min if a_per_min and a_per_min > 0.0 else None
        self._emitter = self._emitter_split(first_moment_s)
        key, meas_t = _measure_key(measure_id)
        heating = hvac_mode == VThermHvacMode_HEAT
        shedding = heating and excluded == RM_EXCLUDED_SHEDDING
        if shedding and not self._shedding:
            self.observer.timeline.stop(now)
        self._shedding = shedding
        active = heating and (not excluded or shedding)
        self.observer.observe(now, temp=temp, meas_key=key, meas_t=meas_t, ext=ext, active=active,
                              window=excluded == RM_EXCLUDED_WINDOW)
        self.dirty = True

    def on_applied(self, now: float, power: float, cycle_start: bool, cycle_min: float, hvac_mode,
                   valve: bool = False) -> None:
        """Record an applied command: a PWM cycle (power x cycle on) or a valve level.

        During power shedding the heater stays off whatever the command.
        """
        self._settle_restart()
        tl = self.observer.timeline
        if hvac_mode != VThermHvacMode_HEAT or self._shedding:
            tl.stop(now)
        elif valve:
            tl.applied_level(now, power)
        else:
            tl.applied_cycle(now, power, max(float(cycle_min) * 60.0, 60.0), cycle_start)
        tl.prune(now)

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    def restart_context(self) -> tuple[float | None, bool] | None:
        """(last tick, command saved) of a restart to decide, None without a saved state to resume.

        A state saved without its last tick (older format) gives (None, False): the
        command over the downtime is unknown.
        """
        if not self._restart_pending:
            return None
        saved = self._restart_saved
        if saved is None:
            obs = self.observer
            if obs.t is None and not obs.timeline.t:
                return None
            return None, False
        return saved["last_t"], True

    def on_restart(self, bridged: bool, t_resume: float) -> None:
        """Apply the restart decision (restart_bridge.py) before the first tick after it."""
        if not self._restart_pending:
            return
        self._restart_pending = False
        saved = self._restart_saved
        stop_t = None
        if bridged and saved is not None and saved["last_t"] is not None:
            last_t = saved["last_t"]
            t = saved["saved_t"] if saved["saved_t"] is not None else last_t
            stop_t = min(max(t, last_t), max(t_resume, last_t))
        self.observer.resolve_restart(bridged and stop_t is not None, stop_t)

    def _settle_restart(self) -> None:
        if self._restart_pending:
            self.on_restart(False, 0.0)

    def _te_pub(self) -> float | None:
        te_pub = getattr(self._tar, "te_pub", None) if self._tar is not None else None
        return float(te_pub["value"]) if te_pub and te_pub.get("value") is not None else None

    def _emitter_split(self, first_moment_s: float | None) -> tuple[float, float] | None:
        if first_moment_s is None or first_moment_s < 0.0:
            return None
        return split_first_moment(float(first_moment_s), self._te_pub())

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _periods(self, now: float) -> list[dict]:
        """Closed quiet periods known at now, oldest first."""
        tar = self._tar
        get = getattr(tar, "quiet_periods", None)
        if get is None:
            return []
        return [p for p in get(now) if periods.interval(p)[1] <= now]

    def _snapshot(self, now: float) -> dict:
        tar = self._tar
        snap = tar.signals_snapshot(now)
        snap["sp"] = [list(x) for x in tar.history.setpoints]
        snap["a_per_min"] = self._a_per_min
        return snap

    def _balance_job(self, now: float):
        day = self._clock.day_of(now)
        if self.eb_run_day is not None and day <= self.eb_run_day:
            return None
        if now < self._clock.at(day, 0.0) + _EB_DELAY_S:
            return None
        snap = self._snapshot(now)
        snap["day"] = day
        return KIND_BALANCE, snap

    def _identify_job(self, now: float):
        pers = self._periods(now)
        self.observer.attach_periods([(p["id"], *periods.interval(p)) for p in pers], now)
        if self.last_job_t is not None and now - self.last_job_t < _JOB_MIN_GAP_S:
            return None
        h = self._tar.history
        done_t = max((float(w.get("t_end") or 0.0) for w in self.windows.values()), default=None)
        for w in periods.windows(pers):
            if w["id"] in self.windows or (done_t is not None and w["t_end"] <= done_t):
                continue
            if h.first_t is None or min(x[0] for x in w["intervals"]) < h.first_t:
                # Its data are no longer recorded
                continue
            snap = self._snapshot(now)
            snap["window"] = w
            snap["tau_e"], snap["L"] = self._emitter if self._emitter else (None, None)
            snap["te_pub"] = self._te_pub()
            snap["excluded_nights"] = self.observer.disturbed_nights()
            snap["prev"] = self.windows.get(w["prev"]) if w["prev"] else None
            snap["eb"] = self.eb_slope
            snap["eb_gain"] = self.eb_gain
            return KIND_IDENTIFY, snap
        return None

    def _next_job(self, now: float):
        tar = self._tar
        if tar is None or tar.history.is_empty():
            return None
        return self._balance_job(now) or self._identify_job(now)

    async def async_run(self, hass, now: float | None = None) -> None:
        """Run the due computations in the executor, one at a time."""
        if self._running or not self.active:
            return
        self._running = True
        try:
            while True:
                t = time.time() if now is None else now
                job = self._next_job(t)
                if job is None:
                    return
                kind, snap = job
                if kind == KIND_IDENTIFY:
                    self.last_job_t = t
                try:
                    result = await hass.async_add_executor_job(_run_job, kind, snap)
                except ImportError as err:
                    _LOGGER.warning("%s - room model: %s", self._name, err)
                    self._tar.set_dependencies(False)
                    return
                except Exception as err:  # noqa: BLE001 - a failed fit must never stop the regulation
                    _LOGGER.exception("%s - room model: %s computation failed", self._name, kind)
                    self.last_error = str(err)
                    result = self._failed(kind, snap)
                if kind == KIND_BALANCE:
                    self.eb_run_day = int(snap["day"])
                    self.apply_energy_balance(int(snap["day"]), result.get("eb"))
                else:
                    self._apply(result)
                self.dirty = True
        finally:
            self._running = False

    def _failed(self, kind: str, snap: dict) -> dict:
        if kind == KIND_BALANCE:
            return {"day": snap["day"], "eb": None}
        w = snap["window"]
        return {"id": w["id"], "day": self._clock.day_of(float(w["t_end"])), "t_end": w["t_end"],
                "nights": list(w["nights"]), "prev_id": w.get("prev"), "why": gates.WHY_FAILED,
                "decision": {"why": gates.WHY_FAILED, "wall": None}}

    def apply_energy_balance(self, day: int, eb: dict | None) -> None:
        """Take the energy balance of a day: a qualified static model comes into force, a refused one keeps the
        model in force (eb None: the computation failed)."""
        if not eb:
            self.eb_reason = gates.WHY_FAILED
            return
        try:
            value = None if eb.get("value") is None else float(eb["value"])
            gain = float(eb.get("gain") or 0.0)
            days = None if eb.get("days") is None else int(eb["days"])
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value > 0.0 and math.isfinite(gain):
            self.eb_slope, self.eb_gain, self.eb_days, self.eb_day, self.eb_reason = value, gain, days, int(day), None
            return
        self.eb_reason = eb.get("reason") or gates.WHY_FAILED

    def _apply(self, rec: dict) -> None:
        """Take the result of an identification job."""
        key = str(rec["id"])
        day = int(rec["day"])
        self.windows[key] = dict({k: rec.get(k) for k in ("day", "why", "q", "precise", "full", "reduced")},
                                 t_end=rec.get("t_end"), nights=rec.get("nights"), prev=rec.get("prev_id"))
        for k in sorted(self.windows, key=lambda x: float(self.windows[x].get("t_end") or 0.0))[:-RM_MAX_WINDOWS]:
            del self.windows[k]
        dec = rec.get("decision") or {}
        self.records.append({"day": day, "id": key, "why": dec.get("why"), "struct": dec.get("struct"),
                             "wall": dec.get("wall"), "eb": rec.get("eb"), "hours": rec.get("hours"),
                             "held": rec.get("held"), "excluded_nights": rec.get("excluded_nights")})
        del self.records[:-RM_MAX_RECORDS]
        if dec.get("candidate") is None:
            if self.params is None:
                self.wall_code = dec.get("wall")
            return
        self.candidates.append(dict(dec["candidate"], struct=dec["struct"], day=day, id=key))
        del self.candidates[:-RM_PUB_LAST * 3]
        params = RoomParams.from_dict(gates.published(self.candidates))
        if params is None:
            return
        self.params = params
        self.published_day = day
        self.wall_code = dec.get("wall")
        self.q = rec.get("q")
        self.observer.configure(params, self.q)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        p = self.params
        obs = self.observer
        last = self.records[-1] if self.records else None
        blocked = self._blocked_reason()
        last_why = blocked or (last.get("why") if last else None)
        health = obs.health()
        fc = obs.forecast() if health not in (HEALTH_NO_MODEL, HEALTH_WAITING, HEALTH_PAUSED) else None
        wx = obs.wall_extra() if health == HEALTH_OK else None
        score = obs.score.summary()
        full = p is not None and p.full
        return {
            "status": public.status(p.struct if p else None, last_why, bool(self.records)),
            "tracking": public.tracking(health),
            "walls": public.walls(p.struct if p else None, self.wall_code),
            "a": _per_min(p.a) if p else None,
            "b_eq": _per_min(p.b_eq) if p else None,
            "b_air": _per_min(p.alpha) if full else None,
            "b_wall": _per_min(p.c) if full else None,
            "forecast_c": None if fc is None else {k: round(v, 2) for k, v in zip(RM_HORIZON_LABELS, fc)},
            "forecast_error_c": {k: _r(v, 3) for k, v in zip(RM_HORIZON_LABELS, score["rmse"])},
            "forecast_checks": dict(zip(RM_HORIZON_LABELS, score["count"])),
            "unchanged_temperature_error_c": {k: _r(v, 3) for k, v in zip(RM_HORIZON_LABELS,
                                                                            score["unchanged_rmse"])},
            "wall_extra_power_percent": None if wx is None else round(100.0 * wx, 1),
            "identified_day": _date(self.published_day) if p else None,
            "last_identification_day": _date(last["day"]) if last else None,
            "last_refusal_reason": public.reason(last_why),
            "energy_balance_loss_per_c": _r(self.eb_slope, 6),
            "energy_balance_free_gain_percent": None if self.eb_gain is None else round(100.0 * self.eb_gain, 1),
            "energy_balance_days": self.eb_days,
            "energy_balance_day": _date(self.eb_day),
            "energy_balance_refusal_reason": public.energy_balance_reason(self.eb_reason),
            "sensor_resolution_c": _r(self.q, 3),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_state(self) -> dict[str, Any]:
        self.dirty = False
        return {
            "version": STATE_VERSION,
            "params": self.params.to_dict() if self.params else None,
            "q": self.q,
            "published_day": self.published_day,
            "wall_code": self.wall_code,
            "windows": self.windows,
            "candidates": self.candidates,
            "records": self.records,
            "observer": self.observer.to_dict(),
            "restart": self._restart_save(),
            "energy_balance": {"slope": self.eb_slope, "gain": self.eb_gain, "days": self.eb_days,
                               "day": self.eb_day, "reason": self.eb_reason, "run_day": self.eb_run_day},
            "last_job_t": self.last_job_t,
        }

    def _restart_save(self) -> dict[str, float | None] | None:
        """Last tick and time of the save (the command applied at a stop)."""
        if self._restart_pending:
            # Saved again before the first tick after a restart: the stop is still the first one.
            return dict(self._restart_saved) if self._restart_saved is not None else None
        if self._last_t is None:
            return None
        return {"last_t": self._last_t, "saved_t": time.time()}

    @staticmethod
    def _load_restart(data: Any) -> dict[str, float | None] | None:
        if not isinstance(data, dict):
            return None
        try:
            last_t = float(data["last_t"])
            saved_t = None if data.get("saved_t") is None else float(data["saved_t"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(last_t) or (saved_t is not None and not math.isfinite(saved_t)):
            return None
        return {"last_t": last_t, "saved_t": saved_t}

    def _load_energy_balance(self, data: Any) -> None:
        """Restore the static model in force; an invalid one is dropped (a state without it has none)."""
        self.eb_slope = self.eb_gain = self.eb_days = self.eb_day = self.eb_reason = None
        if not isinstance(data, dict):
            return
        reason = data.get("reason")
        self.eb_reason = reason if isinstance(reason, str) else None
        try:
            slope, gain, day = data.get("slope"), data.get("gain"), data.get("day")
            if slope is None or day is None:
                return
            slope, gain, day = float(slope), float(gain or 0.0), int(day)
            days = None if data.get("days") is None else int(data["days"])
        except (TypeError, ValueError):
            return
        if math.isfinite(slope) and slope > 0.0 and math.isfinite(gain):
            self.eb_slope, self.eb_gain, self.eb_days, self.eb_day = slope, gain, days, day

    def load_state(self, state: dict[str, Any] | None) -> None:
        """Load a saved state; the earlier format keeps its model and observer state, a later one is ignored."""
        if not isinstance(state, dict) or state.get("version") not in (1, STATE_VERSION):
            return
        old = state.get("version") == 1
        try:
            self.params = RoomParams.from_dict(state.get("params"))
            q = state.get("q")
            self.q = None if q is None else float(q)
            pd = state.get("published_day")
            self.published_day = None if pd is None else int(pd)
            self.wall_code = state.get("wall_code")
            self.records = [dict(r) for r in state.get("records") or []]
            self._load_energy_balance(state.get("energy_balance"))
            self.observer = RoomObserver()
            self.observer.configure(self.params, self.q)
            self.observer.load(state.get("observer"))
            self._restart_saved = self._load_restart(state.get("restart"))
            self._restart_pending = True
            if old:
                self._drop_v1_evidence()
                return
            self.windows = {str(k): dict(v) for k, v in (state.get("windows") or {}).items()}
            self.candidates = [dict(c) for c in state.get("candidates") or []]
            run_day = (state.get("energy_balance") or {}).get("run_day")
            self.eb_run_day = None if run_day is None else int(run_day)
            t = state.get("last_job_t")
            self.last_job_t = float(t) if isinstance(t, (int, float)) and math.isfinite(t) else None
        except (TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning("%s - room model state ignored: %s", self._name, err)
            self._reset_results()

    def _drop_v1_evidence(self) -> None:
        """State of the fixed-hour nights: the published model, its observer state, the energy balance in force
        and the records shown in the diagnostics are kept.

        Its windows and candidates were selected by local hours and keyed by day, and the observer's night
        disturbances were summed over local hours: they are no evidence for the quiet periods and are dropped
        (the disturbance of a period cannot be rebuilt without its steps). The next windows are computed from
        the quiet periods of the recorded history.
        """
        self.windows = {}
        self.candidates = []
        self.observer.drop_periods()
        self.eb_run_day = None
        self.last_job_t = None
        self.dirty = True
        _LOGGER.info("%s - room model: earlier state format, its identification windows are computed again from "
                     "the quiet periods", self._name)
