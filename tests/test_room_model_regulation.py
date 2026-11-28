"""Room model and the regulation.

Running the room model leaves the regulation bit-identical to the reference, before and
after it publishes: it is observation only.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import room_model_sim as rsim
import regulation_sim as sim
from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams

REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "regulation_reference.json").read_text())
REPO = Path(__file__).resolve().parent.parent
EB_RATIO = 1.03 * sim.B_TRUE / sim.A_TRUE


def _sha(rows) -> str:
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _prefill(history, days: float = 5.0) -> None:
    """Recorded signals of the days before the run, so the daily identification fits real windows."""
    plant = dict(rsim.NO_MASS, a=sim.A_TRUE, alpha=sim.B_TRUE, tau_e=sim.TAU_EMITTER, L=0.0)
    start = sim.WALL0 - days * 86400.0
    data = rsim.simulate(plant, days, t0=start)
    history.temp_t, history.temp_v = list(data["pub_t"]), list(data["pub_v"])
    history.ext_t, history.ext_v = list(data["ext_t"]), list(data["ext_v"])
    history.on, history.off = list(data["on"]), list(data["off"])
    history.setpoints = [tuple(x) for x in data["sp"]]
    history.first_t, history.last_t = start, sim.WALL0 - 60.0


def _running(holder: dict, publish: bool = True):
    def prepare(algo) -> None:
        algo.room_history.set_dependencies(True)
        # The home position of the synthetic room: its setback nights are quiet periods
        algo.room_history.set_position(rsim.LAT, rsim.LON)
        _prefill(algo.room_history.history)
        if publish:
            params = RoomParams.reduced(sim.A_TRUE, sim.B_TRUE, sim.TAU_EMITTER, 0.0)
            algo.room_model.params = params
            algo.room_model.published_day = rsim.day_of(sim.WALL0) - 1
            algo.room_model.q = 0.1
            algo.room_model.observer.configure(params, 0.1)
            # Energy-balance ratio in force, 3 % above the fitted b_eq / a: the regulation ignores it
            algo.room_model.eb_slope = EB_RATIO
            algo.room_model.eb_day = rsim.day_of(sim.WALL0) - 1
            # ... and the balance of the finished days is already computed today
            algo.room_model.eb_run_day = rsim.day_of(sim.WALL0)
        holder["algo"] = algo

    return prepare


def test_published_room_model_leaves_the_regulation_unchanged() -> None:
    """A published simple model with an energy-balance ratio in force changes nothing in the command: the run is
    bit-identical to the reference, the holding power stays the learned one.

    Observer, forecasts, scoring, daily identification and diagnostics keep running.
    """
    holder: dict = {}
    rows = sim.run(REFERENCE["hours"], room_model=True, prepare=_running(holder))
    assert len(rows) == REFERENCE["rows"]
    assert _sha(rows) == REFERENCE["sha256"]
    algo = holder["algo"]
    room = algo.room_model
    # The module really ran: the windows of the closed quiet periods were identified, the observer tracked the
    # room
    assert len(room.records) == 3 and [r["id"] for r in room.records] == list(room.windows)
    assert all(w.get("reduced") is not None for w in room.windows.values())
    published = algo.get_published_diagnostics()
    diag = published["room_model"]
    assert diag["status"] == "simple model"
    assert diag["tracking"] == "ok"
    assert diag["used_by_regulation"] is False
    assert diag["forecast_c"] is not None
    assert diag["forecast_checks"]["1h"] > 0
    assert diag["wall_extra_power_percent"] is None
    assert published["feedforward"]["source"] == "learned model"
    assert "wall_power_percent" not in published["feedforward"]
    assert algo.dt_est.deadtime_heat_s == 480.0
    assert "room_model" not in algo.save_state()


def test_room_model_learning_is_identical_to_the_reference() -> None:
    """Nothing published (observer waiting, identification of the closed quiet periods running): the
    regulation is bit-identical."""
    holder: dict = {}
    rows = sim.run(REFERENCE["hours"], room_model=True, prepare=_running(holder, publish=False))
    assert _sha(rows) == REFERENCE["sha256"]
    assert holder["algo"].room_model.params is None
    assert len(holder["algo"].room_model.records) == 3
    published = holder["algo"].get_published_diagnostics()
    assert published["feedforward"]["source"] == "learned model"
    diag = published["room_model"]
    assert diag["status"] in ("learning", "not identified: the model does not follow the measured temperature "
                                          "closely enough")
    assert diag["forecast_c"] is None


def test_room_model_off_has_no_block() -> None:
    holder: dict = {}

    def prepare(algo) -> None:
        holder["algo"] = algo

    rows = sim.run(REFERENCE["hours"], prepare=prepare)
    assert _sha(rows) == REFERENCE["sha256"]
    assert holder["algo"].room_model is None and holder["algo"].room_history is None
    assert "room_model" not in holder["algo"].get_published_diagnostics()


def test_scipy_is_not_imported_by_the_room_model_on_the_event_loop() -> None:
    """Observing, applying commands and building the diagnostics never load numpy or scipy."""
    code = (
        "import sys\n"
        "from unittest.mock import MagicMock\n"
        "from custom_components.vtherm_smartpi.algo import SmartPI\n"
        "from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT\n"
        "from custom_components.vtherm_smartpi.smartpi.room_model.model import RoomParams\n"
        "a = SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0,\n"
        "            minimal_deactivation_delay=0, name='x', room_model=True)\n"
        "p = RoomParams('full', 0.001, 1e-4, 4e-6, 6e-5, 600.0, 60.0)\n"
        "a.room_model.params = p\n"
        "a.room_model.observer.configure(p, 0.01)\n"
        "for k in range(200):\n"
        "    a.room_model.on_applied(1e9 + 60 * k, 0.3, k % 10 == 0, 10.0, VThermHvacMode_HEAT)\n"
        "    a.room_model.observe(1e9 + 60 * k, hvac_mode=VThermHvacMode_HEAT, temp=19.0 + 0.01 * (k % 7),\n"
        "                         measure_id=k, ext=5.0, excluded=None, a_per_min=0.06, first_moment_s=660.0)\n"
        "a.get_published_diagnostics()\n"
        "print('scipy' in sys.modules, 'numpy' in sys.modules)\n"
    )
    paths = [str(REPO), str(REPO.parent / "vtherm_api" / "src")]
    env_path = ":".join(paths + [p for p in sys.path if p])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"}, cwd=str(REPO), check=True)
    assert out.stdout.split() == ["False", "False"]
