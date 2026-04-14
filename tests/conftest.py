"""Pytest configuration for repository-local imports."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = Path(__file__).resolve().parent
VTHERM_API_ROOT = Path("/workspaces/vtherm_api/src")

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

if str(VTHERM_API_ROOT) not in sys.path:
    sys.path.insert(0, str(VTHERM_API_ROOT))

from fakes.fake_thermostat_runtime import FakeThermostatRuntime


@pytest.fixture
def fake_runtime() -> FakeThermostatRuntime:
    """Return a baseline fake thermostat runtime."""
    return FakeThermostatRuntime()


@pytest.fixture
def fake_handler_runtime() -> FakeThermostatRuntime:
    """Return a fake thermostat runtime suitable for handler tests."""
    runtime = FakeThermostatRuntime()
    runtime.is_device_active = True
    return runtime
