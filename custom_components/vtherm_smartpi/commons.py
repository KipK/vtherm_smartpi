"""Shared helpers for vtherm_smartpi."""

from __future__ import annotations

import logging


def write_event_log(logger: logging.Logger, thermostat, message: str) -> None:
    """Write a formatted SmartPI event log entry."""
    logger.info(
        "%s - ---------------------> NEW EVENT: %s --------------------------------------------------------------",
        thermostat,
        message,
    )
