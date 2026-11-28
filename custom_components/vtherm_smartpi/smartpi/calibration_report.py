"""End-of-calibration report: the bus event and the persistent notification.

The algorithm only holds the report of a calibration that ended (``LearningStart.take_report``, taken once); the
handler calls ``schedule_report`` after its calculation, which fires the event and the notification from a task of
its own, so that nothing here can delay or break the regulation. A report without interruption and with the readings in
agreement dismisses the notification of the thermostat; any other creates it, or replaces the previous one. Texts come
from the ``issues`` section of the translations.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.helpers import translation

from ..const import DOMAIN, EventType
from .calibration import RESULT_AGREED

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE = "calibration_ended"
_PREFIX = f"component.{DOMAIN}.issues"
_KEYS = {   # plain word -> translation key (entries of the ``issues`` section that carry a title only)
    "readings agree": "calibration_result_agreed", "pulses done": "calibration_result_pulses_done",
    "time limit": "calibration_result_time_limit", "cancelled": "calibration_result_cancelled",
    "setpoint change": "calibration_reason_setpoint_change", "thermostat stopped": "calibration_reason_thermostat_stopped",
    "mode change": "calibration_reason_mode_change", "learning paused": "calibration_reason_learning_paused",
}
_TITLE = "SmartPI calibration of {name}"
_MESSAGE = (
    "Calibration of {name}: {result}.\n\nPulse interruptions:\n{interruptions}\n\nA pulse lasts about {duration}: "
    "keep the setpoint unchanged meanwhile, preferably at night, or request the calibration again later."
)


def notification_id(thermostat: Any) -> str:
    """One notification per thermostat."""
    return f"{DOMAIN}_calibration_{getattr(thermostat, 'unique_id', None) or thermostat.entity_id}"


def needs_notification(report: dict[str, Any]) -> bool:
    """True when the user should read about the calibration: its readings did not agree or a pulse was interrupted."""
    return report["result"] != RESULT_AGREED or bool(report["interruptions"])


def _text(words: dict[str, str], plain: str) -> str:
    return words.get(f"{_PREFIX}.{_KEYS.get(plain, '')}.title", plain)


async def async_report_calibration(thermostat: Any, report: dict[str, Any]) -> None:
    """Fire the event and update the notification of the thermostat; a failure is logged, never raised."""
    hass = thermostat.hass
    try:
        hass.bus.async_fire(EventType.SMART_PI_EVENT.value, {
            "entity_id": thermostat.entity_id, "type": EVENT_TYPE, "result": report["result"],
            "automatic": report["automatic"], "interruptions": report["interruptions"],
        })
        ident = notification_id(thermostat)
        if not needs_notification(report):
            persistent_notification.async_dismiss(hass, ident)
            return
        words = await translation.async_get_translations(hass, hass.config.language, "issues", {DOMAIN})
        none = words.get(f"{_PREFIX}.calibration_none.title", "none")
        lines = [f"- {_text(words, i['reason'])}, {datetime.fromisoformat(i['at']):%Y-%m-%d %H:%M}"
                 for i in report["interruptions"]]
        name = getattr(thermostat, "name", None) or thermostat.entity_id
        message = words.get(f"{_PREFIX}.calibration_report.description", _MESSAGE).format(
            name=name, result=_text(words, report["result"]), interruptions="\n".join(lines) or none,
            duration=f"{round(report['duration_s'] / 3600.0)} h")
        title = words.get(f"{_PREFIX}.calibration_report.title", _TITLE).format(name=name)
        persistent_notification.async_create(hass, message, title=title, notification_id=ident)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("%s - Calibration report failed", getattr(thermostat, "entity_id", ""))


def schedule_report(thermostat: Any, algo: Any) -> None:
    """Called by the handler after each calculation: send the report of a calibration that has just ended, if any."""
    take = getattr(getattr(algo, "_start", None), "take_report", None)  # pylint: disable=protected-access
    report = take() if callable(take) else None
    if isinstance(report, dict):
        thermostat.hass.async_create_task(async_report_calibration(thermostat, report))
