"""Regulation reference pin: a deterministic closed-loop run must give the same outputs bit for bit."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import regulation_sim as sim

REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "regulation_reference.json").read_text())
REPO = Path(__file__).resolve().parent.parent


def _sha(rows) -> str:
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def test_regulation_is_identical_to_the_reference() -> None:
    rows = sim.run(REFERENCE["hours"])
    assert len(rows) == REFERENCE["rows"]
    assert rows[:: 60] == REFERENCE["sample"]
    assert _sha(rows) == REFERENCE["sha256"]


def test_recording_the_room_history_leaves_the_regulation_identical() -> None:
    """The room history and the room model, nothing published: the regulation is bit-identical."""
    holder = {}

    def prepare(algo) -> None:
        algo.room_history.set_dependencies(True)
        holder["algo"] = algo

    rows = sim.run(REFERENCE["hours"], room_model=True, prepare=prepare)
    assert _sha(rows) == REFERENCE["sha256"]
    algo = holder["algo"]
    assert not algo.room_history.history.is_empty()
    published = algo.get_published_diagnostics()
    assert "tar_learning" not in published
    assert published["analysis"]["control"]["gain_delay_source"] == "dead time"


def test_scipy_is_not_imported_by_smartpi() -> None:
    """Neither the room model off nor on (before a computation) loads numpy or scipy."""
    code = (
        "import sys\n"
        "from unittest.mock import MagicMock\n"
        "from custom_components.vtherm_smartpi.algo import SmartPI\n"
        "import custom_components.vtherm_smartpi.handler, custom_components.vtherm_smartpi.config_flow\n"
        "for room in (False, True):\n"
        "    SmartPI(hass=MagicMock(), cycle_min=10, minimal_activation_delay=0,\n"
        "            minimal_deactivation_delay=0, name='x', room_model=room).get_published_diagnostics()\n"
        "print('scipy' in sys.modules, 'numpy' in sys.modules)\n"
    )
    paths = [str(REPO), str(REPO.parent / "vtherm_api" / "src")]
    env_path = ":".join(paths + [p for p in sys.path if p])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"}, cwd=str(REPO), check=True)
    assert out.stdout.split() == ["False", "False"]
