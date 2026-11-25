"""T_ar learning and the regulation: the regulation never reads the T_ar values, option on or off."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tar_learning_sim as sim
from custom_components.vtherm_smartpi.algo import SmartPI

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


@pytest.mark.parametrize("fast_s", [600.0, 1500.0, 480.0 * 4.5])
def test_a_learned_value_does_not_change_the_regulation(fast_s: float) -> None:
    """The gains read the dead time after a start, the holds the response model; a published T_ar value changes
    nothing."""
    holder = {}

    def prepare(algo):
        _active(fast_s)(algo)
        holder["algo"] = algo

    rows = sim.run(REFERENCE["hours"], tar_learning=True, prepare=prepare)
    assert _sha(rows) == REFERENCE["sha256"]
    diag = holder["algo"].get_published_diagnostics()
    assert diag["tar_learning"]["fast"]["value_s"] == fast_s
    assert diag["tar_learning"]["bootstrap_s"] == 480.0
    for key in ("applied_s", "gains_s", "source", "reference_s", "in_force_s", "out_of_bound", "limit_reason"):
        assert key not in diag["tar_learning"]
    assert diag["analysis"]["control"]["gain_delay_source"] == "dead time"


def _algo(tar: bool) -> SmartPI:
    algo = SmartPI(hass=MagicMock(), cycle_min=10.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
                   name="TarToggle", tar_learning=tar)
    sim.seed_stable(algo)
    return algo


def test_turning_the_option_off_keeps_the_regulation_state() -> None:
    on = _algo(True)
    on.tar_learning.set_dependencies(True)
    on.tar_learning.fast_pub = {"value_s": 1500.0, "day": 0}
    state = on.save_state()
    assert "tar_learning" not in state  # the learner keeps its own store
    off = _algo(False)
    off.load_state(state)
    assert off.tar_learning is None
    assert off.dt_est.deadtime_heat_s == 480.0
    assert "tar_learning" not in off.get_published_diagnostics()


def test_unavailable_dependencies_are_published() -> None:
    algo = _algo(True)
    algo.tar_learning.fast_pub = {"value_s": 300.0, "day": 0}
    algo.tar_learning.set_dependencies(False)
    diag = algo.get_published_diagnostics()["tar_learning"]
    assert diag["status"] == "dependencies_unavailable"


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
