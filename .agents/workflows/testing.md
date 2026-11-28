# Testing

- Framework: pytest with `pytest-homeassistant-custom-component`; `asyncio_mode = "auto"` (see `pyproject.toml`). Python >= 3.13. Use the project virtualenv (`.venv` in the repo or the shared one in the workspace root).
- `tests/conftest.py` adds the repo, `../vtherm_api/src` and `../versatile_thermostat` to `sys.path`: the VT and vtherm_api sibling folders must exist (CI checks VT out inside the workspace, sets `PYTHONPATH`, installs `vtherm-api` from PyPI and runs Python 3.14; `conftest.py` adds the sibling folders only when they exist). If `vtherm_api` is not published yet, install the sibling editable: `pip install --no-deps -e ../vtherm_api`.
- Run from the repo root:
  - one file: `pytest -q tests/test_smartpi_feedback.py`
  - by keyword: `pytest -q tests -k "deadtime and not config"`
  - full suite: `pytest -q tests` (can be slow, e.g. on WSL/NTFS: use a generous timeout and run in the background).
- **Never poll** a running suite. Start it in the background and wait for the completion notification; read the output once. If a blocking wait is unavoidable: one call with a hard timeout on a PID/marker, never `pgrep -f`.
- Order: targeted files for the touched area first, then the neighbouring areas, full suite before proposing a commit when the change is wide.
- Finding the test for a module: `../context/architecture.md` (tests by area). Fakes: `tests/fakes/`; shared helpers: `tests/helpers.py`; simulators used by tests: `tests/room_model_sim.py`, `tests/regulation_sim.py`.
- Tests read diagnostics through `algo.get_published_diagnostics()` (nested `live` tree); tests call `calculate()` with keyword arguments. The `history` builder indexes live keys strictly, so test doubles must carry them.
- Published diagnostics key set is pinned by `tests/test_published_diagnostics_schema.py`; regenerate its fixture only for an intended schema change with `SMARTPI_UPDATE_DIAG_PATHS=1`, then adapt cards/docs and note the Equinox impact.
- Git worktrees: a detached worktree needs `versatile_thermostat` and `vtherm_api` symlinks next to the worktree folder. `tests/test_config_flow.py` and `tests/test_init_lifecycle.py` fail there ("Could not setup dependencies: versatile_thermostat"); run those two from the main checkout.
- A test proves a claim, it does not mirror the implementation. Time: patch `time.monotonic` on the module; do not sleep.
- Asserting "bit-identical when silent": compare full outputs of two runs (baseline vs feature silent), not a hand-picked field.

## Setup for a contributor without the maintainer's workspace

Clone `versatile_thermostat` and `vtherm_api` as sibling folders of this repo (same parent directory), create a Python >= 3.13 virtualenv and install `requirements_test.txt` (plus `pip install --no-deps -e ../vtherm_api` while the needed `vtherm_api` release is not on PyPI). Then run `pytest -q tests/<file>` from the repo root.
