"""T_ar learning and the regulation: option off is unchanged, the learned value is bounded and reversible."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock


import tar_learning_sim as sim
from custom_components.vtherm_smartpi.algo import SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT

REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "tar_learning" / "regulation_reference.json").read_text())
REPO = Path(__file__).resolve().parent.parent


def _sha(rows) -> str:
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _active(fast_s: float | None):
    def prepare(algo: SmartPI) -> None:
        algo.tar_learning.set_dependencies(True)
        if fast_s is not None:
            algo.tar_learning.fast_pub = {"value_s": fast_s, "day": 0}

    return prepare


def test_option_off_is_identical_to_the_reference() -> None:
    """The reference rows were produced by the code before the T_ar learning existed."""
    rows = sim.run(REFERENCE["hours"])
    assert len(rows) == REFERENCE["rows"]
    assert rows[:: 60] == REFERENCE["sample"]
    assert _sha(rows) == REFERENCE["sha256"]


def test_option_on_without_learned_value_is_identical() -> None:
    rows = sim.run(REFERENCE["hours"], tar_learning=True, prepare=_active(None))
    assert _sha(rows) == REFERENCE["sha256"]


def test_short_learned_value_keeps_the_reference() -> None:
    """A learned value whose half is below the bootstrap dead time never shortens the gain dead time."""
    algo_ref = {}

    def prepare(algo):
        _active(600.0)(algo)
        algo_ref["algo"] = algo

    rows = sim.run(REFERENCE["hours"], tar_learning=True, prepare=prepare)
    assert _sha(rows) == REFERENCE["sha256"]
    algo = algo_ref["algo"]
    assert algo.regulation_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    assert algo.gain_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    diag = algo.get_published_diagnostics()["tar_learning"]
    assert diag["out_of_bound"] is True
    assert diag["source"] == "bootstrap"
    assert diag["in_force_s"] == 600.0
    assert diag["gains_s"] == diag["applied_s"] == diag["bootstrap_s"] == diag["reference_s"] == 480.0
    assert diag["limit_reason"] == "learned delay shorter than the reference: the gains keep the reference dead time"


def test_long_unconfirmed_value_is_capped_at_twice_the_reference() -> None:
    """Above twice the reference without a second method, the gains use twice the reference; the other
    consumers keep the bootstrap dead time."""
    holder = {}

    def prepare(algo):
        _active(480.0 * 4.5)(algo)
        holder["algo"] = algo

    rows = sim.run(REFERENCE["hours"], tar_learning=True, prepare=prepare)
    assert _sha(rows) != REFERENCE["sha256"]
    algo = holder["algo"]
    assert algo.regulation_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    assert algo.gain_dead_time(VThermHvacMode_HEAT).deadtime_heat_s == 960.0
    diag = algo.get_published_diagnostics()["tar_learning"]
    assert (diag["source"], diag["applied_s"], diag["gains_s"], diag["out_of_bound"]) == ("bootstrap", 480.0, 960.0,
                                                                                         True)
    assert diag["limit_reason"].startswith("learned delay above twice the reference")


def test_learned_value_drives_the_regulation_without_touching_the_bootstrap() -> None:
    holder = {}

    def prepare(algo):
        _active(1500.0)(algo)
        holder["algo"] = algo
        calculate = algo.gain_scheduler.calculate

        def spy(**kwargs):
            holder.setdefault("gain_dead_times", set()).add(kwargs["dt_est"].deadtime_heat_s)
            return calculate(**kwargs)

        algo.gain_scheduler.calculate = spy

    rows = sim.run(REFERENCE["hours"], tar_learning=True, prepare=prepare)
    assert _sha(rows) != REFERENCE["sha256"]
    ref_kp = {r[3] for r in REFERENCE["sample"]}
    assert {r[3] for r in rows[::60]} != ref_kp
    algo = holder["algo"]
    assert algo.dt_est.deadtime_heat_s == 480.0
    view = algo.regulation_dead_time(VThermHvacMode_HEAT)
    assert view.deadtime_heat_s == 1500.0
    assert algo.regulation_dead_time(VThermHvacMode_COOL) is algo.dt_est
    # The gain rule gets the learned value as an effective dead time, half the learned one
    assert algo.gain_dead_time(VThermHvacMode_HEAT).deadtime_heat_s == 750.0
    assert algo.gain_dead_time(VThermHvacMode_COOL) is algo.dt_est
    assert holder["gain_dead_times"] == {750.0}
    diag = algo.get_published_diagnostics()
    assert diag["model"]["deadtime_heat_s"] == 480.0
    assert diag["tar_learning"]["source"] == "fast"
    assert diag["tar_learning"]["applied_s"] == 1500.0
    assert diag["tar_learning"]["gains_s"] == 750.0
    assert diag["tar_learning"]["reference_s"] == 480.0
    assert diag["tar_learning"]["limit_reason"] is None


def _algo(tar: bool) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="TarToggle", tar_learning=tar)
    sim.seed_stable(algo)
    return algo


def test_turning_the_option_off_reverts_at_once() -> None:
    on = _algo(True)
    on.tar_learning.set_dependencies(True)
    on.tar_learning.fast_pub = {"value_s": 1500.0, "day": 0}
    on._last_hvac_mode = VThermHvacMode_HEAT
    assert on.regulation_dead_time().deadtime_heat_s == 1500.0
    assert on.gain_dead_time().deadtime_heat_s == 750.0
    state = on.save_state()
    assert "tar_learning" not in state  # the learner keeps its own store
    off = _algo(False)
    off.load_state(state)
    assert off.tar_learning is None
    assert off.regulation_dead_time(VThermHvacMode_HEAT) is off.dt_est
    assert off.gain_dead_time(VThermHvacMode_HEAT) is off.dt_est
    assert off.dt_est.deadtime_heat_s == 480.0
    assert "tar_learning" not in off.get_published_diagnostics()


def test_unavailable_dependencies_fall_back_to_bootstrap() -> None:
    algo = _algo(True)
    algo.tar_learning.fast_pub = {"value_s": 300.0, "day": 0}
    algo.tar_learning.set_dependencies(False)
    assert algo.regulation_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    assert algo.gain_dead_time(VThermHvacMode_HEAT) is algo.dt_est
    diag = algo.get_published_diagnostics()["tar_learning"]
    assert diag["status"] == "dependencies_unavailable"
    assert diag["source"] == "bootstrap"


def test_scipy_is_not_imported_by_smartpi() -> None:
    """Neither the option off nor the option on (before a computation) loads numpy or scipy."""
    code = (
        "import sys\n"
        "from unittest.mock import MagicMock\n"
        "from custom_components.vtherm_smartpi.algo import SmartPI\n"
        "import custom_components.vtherm_smartpi.handler, custom_components.vtherm_smartpi.config_flow\n"
        "for tar in (False, True):\n"
        "    SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0,\n"
        "            minimal_deactivation_delay=0, name='x', tar_learning=tar).get_published_diagnostics()\n"
        "print('scipy' in sys.modules, 'numpy' in sys.modules)\n"
    )
    paths = [str(REPO), str(REPO.parent / "vtherm_api" / "src")]
    env_path = ":".join(paths + [p for p in sys.path if p])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"}, cwd=str(REPO), check=True)
    assert out.stdout.split() == ["False", "False"]
