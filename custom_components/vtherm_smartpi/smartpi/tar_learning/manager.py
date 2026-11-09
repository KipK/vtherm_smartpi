"""Orchestration of the T_ar learning (event loop side, pure Python).

The manager records the room signals (history.py), keeps the catalogue of the
quiet periods of that history (quiet_periods: no sun, constant setpoint, valid
data), detects the comfort steps, schedules the computations (jobs.py, run in
an executor), keeps their results and gives the regulation the HEAT active
dead time to use (policy.applied_value). It never modifies the
bootstrap/calibration dead time.

The catalogue holds the closed and complete quiet periods with their keys and
origins, so a period keeps its key when the history is pruned. Every piece of
evidence (night of the room class, regulated night, identification window,
confirmed fast value) is keyed by its period: a period counts once. Without the
position of the home no period is found and the methods stay silent.
"""
from __future__ import annotations

import bisect
import logging
import math
import time
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from ..quiet_periods import (
    WHY_NO_POSITION,
    Position,
    astral_available,
    build_episodes,
    dark_intervals,
    episode_of_rise,
    local_iso,
    quiet_intervals,
    regulated_span,
    step_component,
    summary,
    validate_position,
)
from ..quiet_periods.const import BASELINE_HOLD_S
from . import gates, public
from .clock import LocalClock
from .const import (
    ID_WIN_PERIODS,
    MORNING_EVAL_DELAY_S,
    PUB_LAST,
    R_SPAN_S,
    SOURCE_BOOTSTRAP,
    STEP_MERGE_S,
    STEP_MIN_RISE_C,
    STEP_RESTART_GUARD_S,
    TAR_FAST_GATE,
    TAR_GAIN_DEADTIME_FACTOR,
    TAR_MAX_EVALUATIONS,
    TAR_MAX_MORNINGS,
    TAR_MAX_NIGHTS,
    TAR_MAX_WINDOWS,
    TAR_VALVE_MODE_SUPPORTED,
)
from .history import TarHistory
from .policy import AppliedValue, RegulationDeadTime, applied_value, fast_publication

_LOGGER = logging.getLogger(__name__)

STATUS_CHECKING = "checking_dependencies"
STATUS_ACTIVE = "active"
STATUS_UNAVAILABLE = "dependencies_unavailable"
STATUS_UNSUPPORTED = "valve_mode_not_supported"

# 2: quiet periods (keys by period, catalogue of periods, position of the evidence)
STATE_VERSION = 2
_PRUNE_EVERY_S = 3600.0
# The catalogue of the quiet periods is rebuilt at most this often
_CATALOGUE_EVERY_S = 600.0
# A position closer than this (degrees) to the one of the evidence is the same home
_POSITION_TOL_DEG = 1e-3
# Shortest response left before sunrise for a comfort step
_RESPONSE_MIN_S = 600.0


def _run_job(kind: str, snap: dict) -> dict:
    """Executor entry point: the numerical modules are imported here, never on the event loop."""
    from . import jobs  # pylint: disable=import-outside-toplevel

    if kind == "morning":
        return jobs.morning_job(snap)
    if kind == "window":
        return jobs.window_job(snap)
    return jobs.evaluate_job(snap)


def _r(x: float | None, digits: int = 1) -> float | None:
    return None if x is None else round(float(x), digits)


def _per_min(x: float | None) -> float | None:
    """A per-second identified parameter in per-minute units, rounded like model.a and model.b."""
    return None if x is None else round(float(x) * 60.0, 6)


def _moved(a: Position, b: Position) -> bool:
    return (abs(a.latitude - b.latitude) > _POSITION_TOL_DEG
            or abs(a.longitude - b.longitude) > _POSITION_TOL_DEG)


class TarLearningManager:
    """T_ar learning of one thermostat."""

    def __init__(self, name: str, time_zone: str | None, gate: str = TAR_FAST_GATE) -> None:
        self._name = name
        self._tz = time_zone
        # Local dates of the diagnostics only: no selection uses the local time
        self._clock = LocalClock(time_zone)
        self.gate = gate
        self.status = STATUS_CHECKING
        self.supported = True
        self.history = TarHistory()
        self._cycle_s = 600.0
        self._running = False
        self._last_prune = 0.0
        self.dirty = False
        # Position of the home (None: unknown, no quiet period) and the one the evidence was found with
        self.position: Position | None = None
        self._evidence_position: Position | None = None
        # Last reliable bootstrap HEAT dead time seen (reference of the steps cut by sunrise)
        self.bootstrap_heat_s: float | None = None
        # Results of a computation started before a reset or a change of position are dropped
        self._generation = 0
        self._reset_results()

    def _reset_results(self) -> None:
        self.pending: list[dict] = []
        # Closed and complete quiet periods (oldest first), keyed by "id"
        self.episodes: list[dict] = []
        # Night indicator of each evaluated step, one per period ("night_id")
        self.nights: list[dict] = []
        self.mornings: list[dict] = []
        # Identification windows and regulated-night decisions, keyed by period
        self.windows: dict[str, dict] = {}
        self.rnights: dict[str, dict] = {}
        self.evals: list[dict] = []
        # Confirmed fast values [period key, T], one per period
        self.fast_values: list[list] = []
        self.fast_ids: list[str] = []
        self.fast_count = 0
        self.fast_pub: dict | None = None
        self.te_pub: dict | None = None
        self.refined_cls = "unknown"
        self.refined_wall: dict | None = None
        self.last_error: str | None = None
        self._open: list[dict] = []
        self._cat_t: float | None = None
        self._generation += 1

    def _forget_periods(self) -> None:
        """Forget the evidence found with the quiet periods; the history and the pending steps stay."""
        pending, mornings = self.pending, self.mornings
        self._reset_results()
        self.pending, self.mornings = pending, mornings
        self._evidence_position = None
        self.dirty = True

    def set_position(self, latitude, longitude) -> None:
        """Position of the home (Home Assistant configuration); a value that is not valid means unknown."""
        pos = validate_position(latitude, longitude)
        if pos is not None and not astral_available():
            _LOGGER.warning("%s - T_ar learning: the solar position is unavailable (astral)", self._name)
            pos = None
        if pos is None and self.position is None and latitude is not None:
            _LOGGER.debug("%s - T_ar learning: home position unknown, no quiet period", self._name)
        self.position = pos
        self._check_position()
        self._cat_t = None

    def _check_position(self) -> None:
        ev = self._evidence_position
        if self.position is not None and ev is not None and _moved(ev, self.position):
            _LOGGER.info("%s - T_ar learning: the home position changed, the learned periods are forgotten",
                         self._name)
            self._forget_periods()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_dependencies(self, available: bool) -> None:
        """Result of the numpy/scipy check: without them the bootstrap dead time stays in use."""
        if available:
            self.status = STATUS_ACTIVE
            return
        if self.status != STATUS_UNAVAILABLE:
            _LOGGER.warning(
                "%s - T_ar learning is enabled but numpy/scipy are unavailable: "
                "the regulation uses the bootstrap dead time",
                self._name,
            )
        self.status = STATUS_UNAVAILABLE
        self.history = TarHistory()

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE and self.supported

    @property
    def public_status(self) -> str:
        return self.status if self.supported else STATUS_UNSUPPORTED

    def reset(self) -> None:
        """Forget the history and every learned value."""
        self.history = TarHistory()
        self._reset_results()
        self._evidence_position = None
        self.dirty = True

    # ------------------------------------------------------------------
    # Recording (called from the regulation, must stay cheap)
    # ------------------------------------------------------------------
    def observe(
        self,
        now: float,
        *,
        hvac_mode,
        temp: float | None,
        ext: float | None,
        setpoint: float | None,
        phase: str | None,
        b: float | None,
        dtc: float | None,
        dtc_reliable: bool,
        lag: float | None,
        cycle_min: float,
        valve: bool = False,
        excluded: str | None = None,
    ) -> None:
        """Record one regulation tick.

        valve: the heater is a valve (applied power recorded as a level), not a PWM switch.
        excluded: why SmartPI is not in control of the heater (window open, power
        shedding, safety mode, command overridden...), None when it is; the
        tick is then recorded as invalid.
        """
        if valve and not TAR_VALVE_MODE_SUPPORTED:
            if self.supported:
                self.supported = False
                self.history = TarHistory()
            return
        self.supported = True
        if self.status == STATUS_UNAVAILABLE:
            return
        if bool(valve) != self.history.valve:
            # The recorded heater signal changes kind: the history restarts.
            self.history = TarHistory(valve=bool(valve))
        if hvac_mode != VThermHvacMode_HEAT and not excluded:
            excluded = "hvac not heating"
        self._cycle_s = max(float(cycle_min) * 60.0, 60.0)
        prev_sp = self.history.record(
            now, temp=temp, ext=ext, setpoint=setpoint, phase=phase.upper() if phase else None,
            b=b, dtc=dtc if dtc_reliable else None, lag=lag, excluded=excluded,
        )
        if prev_sp is not None and setpoint is not None:
            self._on_setpoint_change(now, prev_sp, float(setpoint))
        if now - self._last_prune > _PRUNE_EVERY_S:
            self.history.prune(now)
            self._last_prune = now
        self.dirty = True

    def on_applied(self, now: float, power: float, cycle_start: bool, cycle_min: float, hvac_mode,
                   valve: bool = False) -> None:
        """Record an applied command: a PWM cycle (power x cycle on) or a valve level (heat-equivalent power)."""
        if self.status == STATUS_UNAVAILABLE or not self.supported or bool(valve) != self.history.valve:
            return
        if valve:
            # A valve outside HEAT delivers no heating power.
            self.history.applied_level(now, power if hvac_mode == VThermHvacMode_HEAT else 0.0, cycle_start)
            return
        if hvac_mode != VThermHvacMode_HEAT:
            return
        self.history.applied_cycle(now, power, max(float(cycle_min) * 60.0, 60.0), cycle_start)

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    def restart_context(self) -> tuple[float, bool] | None:
        """(last observation, heater state saved) of a restart to decide, None without history."""
        h = self.history
        if not h.restart_pending:
            return None
        return float(h.last_t), h.heater_saved

    def on_restart(self, bridged: bool, t_resume: float) -> None:
        """Apply the restart decision (restart_bridge.py) before the first observation after it."""
        self.history.resolve_restart(bridged, t_resume)

    def _after_restart(self, t: float) -> bool:
        """t lies within the restoration delay after the first observation following a restart."""
        return any(0.0 <= t - r[1] <= STEP_RESTART_GUARD_S for r in self.history.restarts)

    def _on_setpoint_change(self, t: float, old: float, new: float) -> None:
        """A rise of at least STEP_MIN_RISE_C after a low setpoint held for the baseline is a comfort step."""
        if new - old < STEP_MIN_RISE_C:
            return
        t = round(float(t), 1)
        if self._after_restart(t):
            return
        if self.pending and t - self.pending[-1]["t_sp"] <= STEP_MERGE_S:
            self.pending[-1]["sp_to"] = max(self.pending[-1]["sp_to"], new)
            return
        sps = self.history.setpoints
        i = bisect.bisect_left([x[0] for x in sps], t - 0.05) - 1
        if i < 0 or abs(sps[i][1] - old) > 1e-6 or t - sps[i][0] < BASELINE_HOLD_S:
            return
        self.pending.append({"t_sp": t, "eco": old, "sp_to": new})

    # ------------------------------------------------------------------
    # Value used by the regulation
    # ------------------------------------------------------------------
    def applied(self, bootstrap_s: float | None, bootstrap_reliable: bool) -> AppliedValue:
        if bootstrap_reliable and bootstrap_s is not None and math.isfinite(bootstrap_s) and bootstrap_s > 0.0:
            self.bootstrap_heat_s = float(bootstrap_s)
        if not self.active:
            return AppliedValue(bootstrap_s, SOURCE_BOOTSTRAP, None, None, False)
        return applied_value(
            bootstrap_s,
            bootstrap_reliable,
            self.fast_pub["value_s"] if self.fast_pub else None,
            self.te_pub["value"] if self.te_pub else None,
        )

    def regulation_dead_time(self, dt_est, hvac_mode):
        """Dead-time source of the regulation: dt_est itself unless a learned HEAT value applies."""
        if hvac_mode != VThermHvacMode_HEAT or not self.active:
            return dt_est
        ap = self.applied(dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable)
        if ap.source == SOURCE_BOOTSTRAP or ap.value_s is None:
            return dt_est
        return RegulationDeadTime(dt_est, ap.value_s)

    def gain_dead_time(self, dt_est, hvac_mode):
        """Dead-time source of the gain rule: the learned value as an effective dead time."""
        view = self.regulation_dead_time(dt_est, hvac_mode)
        if view is dt_est:
            return dt_est
        return RegulationDeadTime(dt_est, TAR_GAIN_DEADTIME_FACTOR * view.deadtime_heat_s)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _snapshot_signals(self, now: float) -> dict:
        h = self.history
        h.flush(now)
        return {
            "tz": self._tz,
            "pub_t": list(h.temp_t), "pub_v": list(h.temp_v),
            "ext_t": list(h.ext_t), "ext_v": list(h.ext_v),
            "on": list(h.on), "off": list(h.off),
            "phases": list(h.phases), "params": list(h.params), "gaps": h.invalid_intervals(now),
            "restarts": [list(x) for x in h.restarts],
            "first_t": h.first_t, "last_t": h.last_t, "t_last": now,
            "cycle_s": self._cycle_s, "pw": h.power_levels(),
        }

    def signals_snapshot(self, now: float) -> dict:
        """Recorded signals as plain data, for a computation run in an executor."""
        return self._snapshot_signals(now)

    def _b_at(self, t: float) -> float | None:
        b = None
        for p in self.history.params:
            if p[0] <= t:
                b = p[1]
            else:
                break
        return b

    # ------------------------------------------------------------------
    # Quiet periods
    # ------------------------------------------------------------------
    def _refresh(self, now: float, force: bool = False) -> None:
        """Rebuild the catalogue of the quiet periods (at most every _CATALOGUE_EVERY_S)."""
        if self.position is None:
            self._open = []
            return
        if not force and self._cat_t is not None and now - self._cat_t < _CATALOGUE_EVERY_S:
            return
        self._cat_t = now
        h = self.history
        h.flush(now)
        eps = build_episodes(self.position, h.setpoints, h.valid_spans(now), h.first_t, h.last_t, h.on,
                             (h.temp_t, h.temp_v), h.restarts)
        known = {e["id"] for e in self.episodes}
        last_end = self.episodes[-1]["end"] if self.episodes else -math.inf
        self._open = []
        for e in eps:
            if not e["settled"]:
                # Not stored yet; a closed one already serves the comfort step that ends it
                self._open.append(e)
                continue
            # The catalogue only grows forward: a closed period is never selected again differently
            if not e["complete"] or e["id"] in known or e["start"] < last_end - 1e-6:
                continue
            rec = summary(e)
            rec["day"] = self._clock.day_of(e["end"])
            self.episodes.append(rec)
            last_end = e["end"]
            self._evidence_position = self.position
        del self.episodes[:-TAR_MAX_NIGHTS]

    def _is_dark(self, t: float) -> bool:
        return self.position is not None and bool(dark_intervals(self.position, t - 1.0, t + 1.0))

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _next_job(self, now: float):
        h = self.history
        if h.is_empty():
            return None
        self._refresh(now, force=any(now >= s["t_sp"] + MORNING_EVAL_DELAY_S for s in self.pending))
        # Comfort steps whose 2 h response is recorded
        for step in list(self.pending):
            if now < step["t_sp"] + MORNING_EVAL_DELAY_S:
                continue
            job = self._step_job(step, now)
            if job is not None:
                return job
        # Regulated-night gate and identification window of the next closed period
        for k, ep in enumerate(self.episodes):
            if ep["id"] not in self.rnights:
                return self._window_job(k, now)
        return None

    def _step_job(self, step: dict, now: float):
        h = self.history
        t_sp = step["t_sp"]
        if self._after_restart(t_sp):
            self._skip_morning(step, "setpoint restored after a restart")
            return None
        ep = None
        if self.position is not None:
            ep = episode_of_rise(self.episodes, t_sp) or episode_of_rise(
                [e for e in self._open if e["closed"] and e["complete"]], t_sp)
        t_hi = t_sp + MORNING_EVAL_DELAY_S - 300.0
        if ep is not None:
            t_hi = min(t_hi, ep["dark_end"])
        if h.gap_overlaps(t_sp - R_SPAN_S, t_hi):
            self._skip_morning(step, "data gap around the step")
            return None
        b_pub = self._b_at(t_sp)
        if not b_pub or b_pub <= 0.0:
            self._skip_morning(step, "no published b")
            return None
        if self.position is None:
            self._skip_morning(step, WHY_NO_POSITION)
            return None
        if ep is None:
            self._skip_morning(step, "no quiet period before the step" if self._is_dark(t_sp)
                               else "comfort step in daylight")
            return None
        comp = step_component(ep, t_sp)
        if comp is None:
            self._skip_morning(step, "low setpoint not held in the dark before the step")
            return None
        if ep["dark_end"] - t_sp < _RESPONSE_MIN_S:
            self._skip_morning(step, "sunrise too close to the comfort step")
            return None
        snap = self._snapshot_signals(now)
        snap.update(t_sp=t_sp, eco=step["eco"], sp_to=step["sp_to"], b_pub=b_pub, nights=list(self.nights),
                    evals=list(self.evals), night_id=ep["id"], quiet=[list(x) for x in quiet_intervals(ep)],
                    fit_end=ep["dark_end"], regulated_start=comp[4],
                    fast_s=self.fast_pub["value_s"] if self.fast_pub else None, bootstrap_s=self.bootstrap_heat_s)
        return "morning", snap, step

    def _window_job(self, k: int, now: float):
        ep = self.episodes[k]
        a, b, why = regulated_span(ep)
        snap = self._snapshot_signals(now)
        snap["gate"] = {"id": ep["id"], "a": a, "b": b, "why": why, "sp": ep["sp"]}
        window = None
        if k >= ID_WIN_PERIODS - 1:
            three = self.episodes[k - ID_WIN_PERIODS + 1: k + 1]
            prev = self.episodes[k - ID_WIN_PERIODS]["id"] if k >= ID_WIN_PERIODS else None
            window = {"id": ep["id"], "nights": [e["id"] for e in three],
                      "intervals": [e.get("ident") or e["quiet"] for e in three],
                      "t_end": ep["end"], "day": ep["day"], "prev": prev}
        snap["window"] = window
        return "window", snap, {"episode": ep, "window": window}

    def _skip_morning(self, step: dict, why: str) -> None:
        self.pending.remove(step)
        self.mornings.append({"day": self._clock.day_of(step["t_sp"]), "t_sp": step["t_sp"], "accepted": False,
                              "why": why, "T": None, "confirmed": None})
        del self.mornings[:-TAR_MAX_MORNINGS]

    async def async_run(self, hass, now: float | None = None) -> None:
        """Run the due computations in the executor, one at a time."""
        if self._running or not self.active:
            return
        self._running = True
        try:
            while True:
                job = self._next_job(time.time() if now is None else now)
                if job is None:
                    break
                kind, snap, ctx = job
                gen = self._generation
                try:
                    result = await hass.async_add_executor_job(_run_job, kind, snap)
                except ImportError as err:
                    _LOGGER.warning("%s - T_ar learning: %s", self._name, err)
                    self.set_dependencies(False)
                    return
                except Exception as err:  # noqa: BLE001 - a failed fit must never stop the regulation
                    _LOGGER.exception("%s - T_ar learning: %s computation failed", self._name, kind)
                    if gen != self._generation:
                        continue
                    self.last_error = f"{kind}: {err}"
                    self._mark_failed(kind, ctx)
                    continue
                if gen != self._generation:
                    # Reset or new position while it ran: the result belongs to forgotten evidence
                    continue
                await self._apply(hass, kind, ctx, result)
                self.dirty = True
        finally:
            self._running = False

    def _mark_failed(self, kind: str, ctx: dict) -> None:
        if kind == "morning":
            if ctx in self.pending:
                self._skip_morning(ctx, "computation failed")
        elif kind == "window":
            ep = ctx["episode"]
            self.rnights[ep["id"]] = {"ok": False, "why": "computation failed", "t_end": ep["end"]}
            if ctx["window"] is not None:
                self.windows[ep["id"]] = dict(self._window_meta(ctx["window"]), ok=False, why="computation failed")
            self._trim()

    @staticmethod
    def _window_meta(w: dict) -> dict:
        return {"nights": list(w["nights"]), "prev": w["prev"], "t_end": w["t_end"], "day": w["day"]}

    async def _apply(self, hass, kind: str, ctx: dict, result: dict) -> None:
        if kind == "morning":
            self._apply_morning(ctx, result)
            return
        if kind != "window":
            return
        ep, w = ctx["episode"], ctx["window"]
        night = result.get("night") or {"ok": False, "why": "computation failed"}
        self.rnights[ep["id"]] = {"ok": bool(night.get("ok")), "why": night.get("why"), "t_end": ep["end"]}
        if w is None:
            self._trim()
            return
        fit = result.get("window") or {"ok": False, "why": "window outside history"}
        w2 = dict(fit, **self._window_meta(w))
        self.windows[w["id"]] = w2
        self._trim()
        w1 = self.windows.get(w["prev"]) if w["prev"] else None
        b_pub = self._b_at(w["t_end"])
        if not (w1 and "cls" in w1 and "cls" in w2 and b_pub and b_pub > 0.0):
            return
        if set(w1["nights"]) & set(w2["nights"]):
            return
        ids = list(w1["nights"]) + list(w2["nights"])
        snap = {"day": w["day"], "t": w["t_end"], "window_id": w["id"], "w1": w1, "w2": w2, "b_pub": b_pub,
                "nights": {n: self.rnights.get(n) for n in ids}, "evals": list(self.evals)}
        gen = self._generation
        try:
            res = await hass.async_add_executor_job(_run_job, "evaluate", snap)
        except ImportError as err:
            _LOGGER.warning("%s - T_ar learning: %s", self._name, err)
            self.set_dependencies(False)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("%s - T_ar learning: evaluation failed", self._name)
            self.last_error = f"evaluate: {err}"
            return
        if gen != self._generation:
            return
        self.evals.append(res["eval"])
        del self.evals[:-TAR_MAX_EVALUATIONS]
        self.te_pub = res["te_pub"] or self.te_pub
        self.refined_wall = res["wall"]
        self.refined_cls = res["cls"]

    def _apply_morning(self, step: dict, res: dict) -> None:
        if step in self.pending:
            self.pending.remove(step)
        nid = res.get("night_id")
        rec = {k: res.get(k) for k in ("t_sp", "t_s", "accepted", "T", "why", "path", "cls", "term", "ref",
                                        "night", "night_cls", "room_class", "sel", "b_fit_rel", "night_id")}
        rec["day"] = self._clock.day_of(step["t_sp"])
        learned = res.get("learned")
        rec["k_per_s"] = learned.get("k_per_s") if learned else None
        rec["tau_w_s"] = learned.get("tau_w_s") if learned else None
        rec["gate_obs"] = res.get("gate_obs")
        rec["confirmed"] = None
        if res.get("accepted"):
            why = gates.unconfirmed(self.gate, res.get("gate_obs") or {}, res.get("T"), res.get("b_fit_rel"))
            rec["confirmed"] = why is None
            rec["unconfirmed_why"] = why
            if why is None:
                # One confirmed value per period: a second evaluation of a period replaces its value
                self.fast_values = [v for v in self.fast_values if v[0] != nid] + [[nid, float(res["T"])]]
                del self.fast_values[:-PUB_LAST]
                if nid not in self.fast_ids:
                    self.fast_ids.append(nid)
                    del self.fast_ids[:-TAR_MAX_MORNINGS]
                    self.fast_count += 1
                pub = fast_publication([v[1] for v in self.fast_values])
                if pub is not None:
                    self.fast_pub = {"value_s": float(pub), "day": rec["day"]}
        self.mornings.append(rec)
        del self.mornings[:-TAR_MAX_MORNINGS]
        self.nights = [n for n in self.nights if n.get("night_id") != nid] + [res["night_summary"]]
        del self.nights[:-TAR_MAX_NIGHTS]

    def _trim(self) -> None:
        for d in (self.windows, self.rnights):
            keep = TAR_MAX_WINDOWS if d is self.windows else TAR_MAX_NIGHTS
            for k in sorted(d, key=lambda x: d[x].get("t_end") or 0.0)[:-keep]:
                del d[k]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self, bootstrap_s: float | None, bootstrap_reliable: bool) -> dict[str, Any]:
        ap = self.applied(bootstrap_s, bootstrap_reliable)
        last = self.mornings[-1] if self.mornings else None
        last_acc = next((m for m in reversed(self.mornings) if m.get("accepted")), None)
        if last_acc is None:
            fast_status = "none"
        else:
            fast_status = "confirmed" if last_acc.get("confirmed") else "unconfirmed"
        last_eval = self.evals[-1] if self.evals else None
        wall = self.refined_wall
        last_room = next((m for m in reversed(self.mornings) if m.get("room_class")), None)
        return {
            "status": self.public_status,
            "applied_s": _r(ap.value_s),
            "gains_s": _r(ap.value_s if ap.source == SOURCE_BOOTSTRAP or ap.value_s is None
                          else TAR_GAIN_DEADTIME_FACTOR * ap.value_s),
            "source": ap.source,
            "bootstrap_s": _r(bootstrap_s),
            "in_force_s": _r(ap.in_force_s),
            "in_force_source": ap.in_force_source,
            "out_of_bound": ap.out_of_bound,
            "fast": {
                "value_s": _r(self.fast_pub["value_s"]) if self.fast_pub else None,
                "confirmed_mornings": self.fast_count,
                "last_value_s": _r(last_acc.get("T")) if last_acc else None,
                "last_status": fast_status,
                "last_unconfirmed_reason": public.unconfirmed_reason(last_acc.get("unconfirmed_why")) if last_acc else None,
                "last_morning": None if last is None else {
                    "day": last.get("day"), "accepted": last.get("accepted"),
                    "reason": public.morning_reason(last.get("why")), "room_class": public.room_class(last.get("cls")),
                },
            },
            "refined": {
                "tau_e_s": _r(self.te_pub["value"]) if self.te_pub else None,
                "class": public.room_class(self.refined_cls),
                "k_per_s": None if wall is None else float(f"{wall['k_per_s']:.4g}"),
                "tau_w_h": None if wall is None else _r(wall["tau_w_s"] / 3600.0, 2),
                "last_evaluation": None if last_eval is None else {
                    "day": last_eval.get("day"),
                    "regulated_nights": bool(last_eval.get("regulated")),
                    "class": public.room_class(last_eval.get("cls_raw", last_eval.get("cls"))),
                    "tau_e_s": _r(last_eval.get("te_raw", last_eval.get("te"))),
                    # Identified a and b in the units of model.a and model.b (per minute)
                    "a": _per_min(last_eval.get("a_raw", last_eval.get("a"))),
                    "b": _per_min(last_eval.get("b_raw", last_eval.get("b"))),
                },
            },
            "night_room_class": public.room_class(last_room.get("room_class")) if last_room else None,
            "quiet_periods": self._periods_diagnostics(),
            "pending_steps": len(self.pending),
            "data_invalid_reason": self.history.invalid_reason,
            "history_h": (_r((self.history.last_t - self.history.first_t) / 3600.0)
                          if self.history.first_t is not None and self.history.last_t is not None else 0.0),
            "last_error": self.last_error,
        }

    def _periods_diagnostics(self) -> dict[str, Any]:
        last = self.episodes[-1] if self.episodes else None
        tz = self._clock._tz  # pylint: disable=protected-access
        out: dict[str, Any] = {
            "reason": public.period_reason(WHY_NO_POSITION) if self.position is None else None,
            "count": len(self.episodes),
            "last": None,
        }
        if last is not None:
            sel = last["comps"][last["sel"]]
            out["last"] = {"start": local_iso(last["quiet"][0], tz), "end": local_iso(last["quiet"][1], tz),
                           "regulated_from": local_iso(sel[4], tz)}
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_state(self) -> dict[str, Any]:
        self.dirty = False
        return {
            "version": STATE_VERSION,
            "position": self._evidence_position.to_list() if self._evidence_position else None,
            "bootstrap_heat_s": self.bootstrap_heat_s,
            "history": self.history.to_dict(time.time()),
            "pending": self.pending,
            "episodes": self.episodes,
            "fast_ids": self.fast_ids,
            "nights": self.nights,
            "mornings": self.mornings,
            "windows": self.windows,
            "rnights": self.rnights,
            "evals": self.evals,
            "fast_values": self.fast_values,
            "fast_count": self.fast_count,
            "fast_pub": self.fast_pub,
            "te_pub": self.te_pub,
            "refined_cls": self.refined_cls,
            "refined_wall": self.refined_wall,
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        """Load a saved state; an earlier format keeps only its raw data, a later one is ignored."""
        if not isinstance(state, dict):
            return
        version = state.get("version")
        if version == 1:
            self._load_v1(state)
            return
        if version != STATE_VERSION:
            return
        try:
            self.history = TarHistory.from_dict(state.get("history"))
            self.pending = [dict(x) for x in state.get("pending") or []]
            self.episodes = [dict(x) for x in state.get("episodes") or []]
            self.nights = list(state.get("nights") or [])
            self.mornings = list(state.get("mornings") or [])
            self.windows = dict(state.get("windows") or {})
            self.rnights = dict(state.get("rnights") or {})
            self.evals = list(state.get("evals") or [])
            self.fast_values = [[str(k), float(v)] for k, v in state.get("fast_values") or []]
            if not all(math.isfinite(v) and v > 0.0 for _, v in self.fast_values):
                raise ValueError("fast value not finite")
            self.fast_ids = [str(x) for x in state.get("fast_ids") or []]
            self.fast_count = int(state.get("fast_count") or 0)
            self.fast_pub = state.get("fast_pub")
            self.te_pub = state.get("te_pub")
            self.refined_cls = state.get("refined_cls") or "unknown"
            self.refined_wall = state.get("refined_wall")
            bh = state.get("bootstrap_heat_s")
            self.bootstrap_heat_s = float(bh) if isinstance(bh, (int, float)) and math.isfinite(bh) and bh > 0 else None
            pos = state.get("position")
            self._evidence_position = validate_position(*pos) if isinstance(pos, list) and len(pos) == 2 else None
            for e in self.episodes:
                if not (isinstance(e.get("id"), str) and isinstance(e.get("comps"), list)
                        and isinstance(e.get("sel"), int) and isinstance(e.get("end"), (int, float))):
                    raise ValueError("malformed quiet period")
        except (TypeError, ValueError) as err:
            _LOGGER.warning("%s - T_ar learning state ignored: %s", self._name, err)
            self.history = TarHistory()
            self._reset_results()
            self._evidence_position = None
            return
        self._cat_t = None
        self._check_position()

    def _load_v1(self, state: dict[str, Any]) -> None:
        """State of the fixed-hour selection: the recorded signals and the pending rises are kept.

        Its nights, windows, evaluations, confirmations and published values were
        selected by local hours, without a solar check: they are not evidence
        for the quiet periods and are dropped. The quiet periods are found again
        from the recorded signals.
        """
        try:
            history = TarHistory.from_dict(state.get("history"))
            pending = [{"t_sp": round(float(x["t_sp"]), 1), "eco": float(x["eco"]), "sp_to": float(x["sp_to"])}
                       for x in state.get("pending") or []]
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.warning("%s - T_ar learning state ignored: %s", self._name, err)
            return
        self._reset_results()
        self.history = history
        self.pending = pending
        self._evidence_position = None
        self.dirty = True
        _LOGGER.info("%s - T_ar learning: earlier state format, its learned values are learned again from the "
                     "quiet periods", self._name)
