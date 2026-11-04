"""Orchestration of the experimental T_ar learning (event loop side, pure Python).

The manager records the room signals (history.py), detects the morning comfort
steps, schedules the computations (jobs.py, run in an executor), keeps their
results and gives the regulation the HEAT active dead time to use
(policy.applied_value). It never modifies the bootstrap/calibration dead time.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ...hvac_mode import VThermHvacMode_HEAT
from . import gates
from .clock import LocalClock
from .const import (
    ID_WIN_DAYS,
    ID_WINDOW_END_H,
    MORNING_EVAL_DELAY_S,
    PUB_LAST,
    R_SPAN_S,
    SOURCE_BOOTSTRAP,
    STEP_FROM_H,
    STEP_MERGE_S,
    STEP_MIN_RISE_C,
    STEP_TO_H,
    TAR_FAST_GATE,
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

STATE_VERSION = 1
_WINDOW_DELAY_S = 600.0
_PRUNE_EVERY_S = 3600.0


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


class TarLearningManager:
    """T_ar learning of one thermostat."""

    def __init__(self, name: str, time_zone: str | None, gate: str = TAR_FAST_GATE) -> None:
        self._name = name
        self._tz = time_zone
        self._clock = LocalClock(time_zone)
        self.gate = gate
        self.status = STATUS_CHECKING
        self.supported = True
        self.history = TarHistory()
        self._cycle_s = 600.0
        self._running = False
        self._last_prune = 0.0
        self.dirty = False
        self._reset_results()

    def _reset_results(self) -> None:
        self.pending: list[dict] = []
        self.nights: list[dict] = []
        self.mornings: list[dict] = []
        self.windows: dict[str, dict] = {}
        self.rnights: dict[str, dict] = {}
        self.evals: list[dict] = []
        self.fast_values: list[float] = []
        self.fast_count = 0
        self.fast_pub: dict | None = None
        self.te_pub: dict | None = None
        self.refined_cls = "unknown"
        self.refined_wall: dict | None = None
        self.last_error: str | None = None

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

    def _on_setpoint_change(self, t: float, old: float, new: float) -> None:
        if new - old < STEP_MIN_RISE_C:
            return
        if not STEP_FROM_H <= self._clock.tod_h(t) < STEP_TO_H:
            return
        if self.pending and t - self.pending[-1]["t_sp"] <= STEP_MERGE_S:
            self.pending[-1]["sp_to"] = max(self.pending[-1]["sp_to"], new)
            return
        self.pending.append({"t_sp": t, "eco": old, "sp_to": new})

    # ------------------------------------------------------------------
    # Value used by the regulation
    # ------------------------------------------------------------------
    def applied(self, bootstrap_s: float | None, bootstrap_reliable: bool) -> AppliedValue:
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
            "first_t": h.first_t, "last_t": h.last_t, "t_last": now,
            "cycle_s": self._cycle_s, "pw": h.power_levels(),
        }

    def _b_at(self, t: float) -> float | None:
        b = None
        for p in self.history.params:
            if p[0] <= t:
                b = p[1]
            else:
                break
        return b

    def _next_job(self, now: float):
        h = self.history
        if h.is_empty():
            return None
        # Morning comfort steps whose 2 h response is recorded
        for step in list(self.pending):
            if now < step["t_sp"] + MORNING_EVAL_DELAY_S:
                continue
            b_pub = self._b_at(step["t_sp"])
            if h.gap_overlaps(step["t_sp"] - R_SPAN_S, step["t_sp"] + MORNING_EVAL_DELAY_S - 300.0):
                self._skip_morning(step, "data gap around the step")
                continue
            if not b_pub or b_pub <= 0.0:
                self._skip_morning(step, "no published b")
                continue
            snap = self._snapshot_signals(now)
            snap.update(t_sp=step["t_sp"], eco=step["eco"], sp_to=step["sp_to"], b_pub=b_pub,
                        nights=list(self.nights), evals=list(self.evals))
            return "morning", snap, step
        # Identification window of today, once 08:00 has passed
        day = self._clock.day_of(now)
        t_end = self._clock.at(day, ID_WINDOW_END_H)
        key = str(day)
        if now >= t_end + _WINDOW_DELAY_S and key not in self.windows:
            t_start = self._clock.at(day - ID_WIN_DAYS, ID_WINDOW_END_H)
            nights = [n for n in range(day - ID_WIN_DAYS, day) if str(n) not in self.rnights]
            snap = self._snapshot_signals(now)
            snap["window_days"] = [day] if h.first_t is not None and t_start >= h.first_t else []
            snap["night_days"] = nights
            sps = self.history.setpoints
            snap["sp0"] = sps[0][1] if sps else 0.0
            snap["sp_events"] = [list(x) for x in sps[1:]]
            return "window", snap, {"day": day, "now": now}
        return None

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
                try:
                    result = await hass.async_add_executor_job(_run_job, kind, snap)
                except ImportError as err:
                    _LOGGER.warning("%s - T_ar learning: %s", self._name, err)
                    self.set_dependencies(False)
                    return
                except Exception as err:  # noqa: BLE001 - a failed fit must never stop the regulation
                    _LOGGER.exception("%s - T_ar learning: %s computation failed", self._name, kind)
                    self.last_error = f"{kind}: {err}"
                    self._mark_failed(kind, ctx)
                    continue
                await self._apply(hass, kind, ctx, result)
                self.dirty = True
        finally:
            self._running = False

    def _mark_failed(self, kind: str, ctx: dict) -> None:
        if kind == "morning":
            self._skip_morning(ctx, "computation failed")
        elif kind == "window":
            self.windows[str(ctx["day"])] = {"ok": False, "why": "computation failed"}

    async def _apply(self, hass, kind: str, ctx: dict, result: dict) -> None:
        if kind == "morning":
            self._apply_morning(ctx, result)
            return
        if kind != "window":
            return
        for k, v in result["nights"].items():
            self.rnights[k] = {"ok": bool(v.get("ok")), "why": v.get("why")}
        day = ctx["day"]
        self.windows[str(day)] = result["windows"].get(str(day)) or {"ok": False, "why": "window outside history"}
        self._trim()
        w1, w2 = self.windows.get(str(day - ID_WIN_DAYS)), self.windows.get(str(day))
        b_pub = self._b_at(ctx["now"])
        if not (w1 and w2 and "cls" in w1 and "cls" in w2 and b_pub and b_pub > 0.0):
            return
        nights = {k: v for k, v in self.rnights.items()}
        snap = {"day": day, "w1": w1, "w2": w2, "b_pub": b_pub, "nights": nights, "evals": list(self.evals)}
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
        self.evals.append(res["eval"])
        del self.evals[:-TAR_MAX_EVALUATIONS]
        self.te_pub = res["te_pub"] or self.te_pub
        self.refined_wall = res["wall"]
        self.refined_cls = res["cls"]

    def _apply_morning(self, step: dict, res: dict) -> None:
        if step in self.pending:
            self.pending.remove(step)
        rec = {k: res.get(k) for k in ("day", "t_sp", "t_s", "accepted", "T", "why", "path", "cls", "term", "ref",
                                        "night", "night_cls", "room_class", "sel", "b_fit_rel")}
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
                self.fast_values.append(float(res["T"]))
                del self.fast_values[:-PUB_LAST]
                self.fast_count += 1
                pub = fast_publication(self.fast_values)
                if pub is not None:
                    self.fast_pub = {"value_s": float(pub), "day": res.get("day")}
        self.mornings.append(rec)
        del self.mornings[:-TAR_MAX_MORNINGS]
        self.nights.append(res["night_summary"])
        del self.nights[:-TAR_MAX_NIGHTS]

    def _trim(self) -> None:
        for d in (self.windows, self.rnights):
            for k in sorted(d, key=int)[:-TAR_MAX_WINDOWS]:
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
            "gate": self.gate,
            "applied_s": _r(ap.value_s),
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
                "last_unconfirmed_reason": last_acc.get("unconfirmed_why") if last_acc else None,
                "last_morning": None if last is None else {
                    "day": last.get("day"), "accepted": last.get("accepted"), "reason": last.get("why"),
                    "path": last.get("path"), "room_class": last.get("cls"),
                },
            },
            "refined": {
                "tau_e_s": _r(self.te_pub["value"]) if self.te_pub else None,
                "class": self.refined_cls,
                "k_per_s": None if wall is None else float(f"{wall['k_per_s']:.4g}"),
                "tau_w_h": None if wall is None else _r(wall["tau_w_s"] / 3600.0, 2),
                "last_evaluation": None if last_eval is None else {
                    "day": last_eval.get("day"),
                    "regulated_nights": bool(last_eval.get("regulated")),
                    "class": last_eval.get("cls_raw", last_eval.get("cls")),
                    "tau_e_s": _r(last_eval.get("te_raw", last_eval.get("te"))),
                    # Identified a and b in the units of model.a and model.b (per minute)
                    "a": _per_min(last_eval.get("a_raw", last_eval.get("a"))),
                    "b": _per_min(last_eval.get("b_raw", last_eval.get("b"))),
                },
            },
            "night_room_class": last_room.get("room_class") if last_room else None,
            "pending_steps": len(self.pending),
            "data_invalid_reason": self.history.invalid_reason,
            "history_h": (_r((self.history.last_t - self.history.first_t) / 3600.0)
                          if self.history.first_t is not None and self.history.last_t is not None else 0.0),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_state(self) -> dict[str, Any]:
        self.dirty = False
        return {
            "version": STATE_VERSION,
            "history": self.history.to_dict(),
            "pending": self.pending,
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
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return
        try:
            self.history = TarHistory.from_dict(state.get("history"))
            self.pending = [dict(x) for x in state.get("pending") or []]
            self.nights = list(state.get("nights") or [])
            self.mornings = list(state.get("mornings") or [])
            self.windows = dict(state.get("windows") or {})
            self.rnights = dict(state.get("rnights") or {})
            self.evals = list(state.get("evals") or [])
            self.fast_values = [float(x) for x in state.get("fast_values") or []]
            self.fast_count = int(state.get("fast_count") or 0)
            self.fast_pub = state.get("fast_pub")
            self.te_pub = state.get("te_pub")
            self.refined_cls = state.get("refined_cls") or "unknown"
            self.refined_wall = state.get("refined_wall")
        except (TypeError, ValueError) as err:
            _LOGGER.warning("%s - T_ar learning state ignored: %s", self._name, err)
            self.history = TarHistory()
            self._reset_results()
