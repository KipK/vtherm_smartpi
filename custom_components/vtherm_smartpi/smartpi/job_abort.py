"""Abort of the numerical jobs run in an executor thread.

A job cannot be cancelled once started; the objective functions of its fits call
``check`` so that a stop (removal or reload of the thermostat) ends it quickly.
The event is attached to the executing thread, so jobs of several thermostats
never interfere.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

_local = threading.local()


class JobAborted(Exception):
    """The job was stopped before its end."""


def check() -> None:
    """Raise JobAborted when the job running in this thread was asked to stop."""
    event = getattr(_local, "event", None)
    if event is not None and event.is_set():
        raise JobAborted


def run(event: threading.Event, fn: Callable[..., Any], *args: Any) -> Any:
    """Run fn(*args) in this thread, abortable through event."""
    previous = getattr(_local, "event", None)
    _local.event = event
    try:
        check()
        return fn(*args)
    finally:
        _local.event = previous
