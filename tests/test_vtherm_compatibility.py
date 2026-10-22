"""Tests for the VT minimum version required during SmartPI setup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from awesomeversion import AwesomeVersion
import pytest
from homeassistant.components import persistent_notification
from homeassistant.loader import LoaderError

from custom_components.vtherm_smartpi import async_setup
from custom_components.vtherm_smartpi.const import DOMAIN
from custom_components.vtherm_smartpi.smartpi import compatibility


@pytest.fixture
def integration_loader(monkeypatch):
    """Provide the VT integration metadata without loading VT itself."""
    loader = AsyncMock(
        return_value=SimpleNamespace(version=AwesomeVersion("10.6.2"))
    )
    monkeypatch.setattr(compatibility, "async_get_integration", loader)
    return loader


@pytest.fixture
def translations(monkeypatch):
    """Expose the actual translated notification text through the HA loader."""
    translations_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / DOMAIN
        / "translations"
    )
    messages = {
        language: json.loads((translations_dir / f"{language}.json").read_text())[
            "issues"
        ]["vtherm_version"]
        for language in ("en", "fr")
    }

    async def _load(hass, language, category, integrations):
        assert category == "issues"
        assert integrations == {DOMAIN}
        return {
            f"component.{DOMAIN}.issues.vtherm_version.{key}": value
            for key, value in messages[language].items()
        }

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(compatibility.translation, "async_get_translations", loader)
    return loader, messages


@pytest.mark.parametrize(
    ("version", "compatible"),
    [
        ("10.6.1", False),
        ("10.6.2b1", False),
        ("10.6.2", True),
        ("10.6.3b1", True),
        ("10.7.0", True),
        ("11.0.0", True),
        (None, False),
        ("not-a-version", False),
    ],
)
async def test_version_requirement(
    hass, integration_loader, translations, version, compatible
) -> None:
    """Accept the minimum release and reject older or unusable metadata."""
    integration_loader.return_value.version = (
        AwesomeVersion(version) if version is not None else None
    )

    assert await compatibility.async_check_vtherm_version(hass) is compatible

    integration_loader.assert_awaited_once_with(hass, "versatile_thermostat")
    notifications = hass.data.get(persistent_notification.DOMAIN, {})
    loader, _ = translations
    if compatible:
        assert notifications == {}
        loader.assert_not_awaited()
    else:
        assert list(notifications) == [compatibility.NOTIFICATION_ID]
        message = notifications[compatibility.NOTIFICATION_ID]["message"]
        assert compatibility.MIN_VTHERM_VERSION in message
        assert (version if version is not None else "?") in message


@pytest.mark.parametrize("language", ["en", "fr"])
async def test_notification_uses_configured_language(
    hass, integration_loader, translations, language
) -> None:
    """Render the maintained FR/EN text with both relevant VT versions."""
    hass.config.language = language
    integration_loader.return_value.version = AwesomeVersion("10.6.1")

    assert await compatibility.async_check_vtherm_version(hass) is False

    _, messages = translations
    placeholders = {"minimum_version": "10.6.2", "installed_version": "10.6.1"}
    notification = hass.data[persistent_notification.DOMAIN][
        compatibility.NOTIFICATION_ID
    ]
    assert notification["title"] == messages[language]["title"].format(**placeholders)
    assert notification["message"] == messages[language]["description"].format(
        **placeholders
    )


async def test_missing_integration_is_reported(
    hass, integration_loader, translations
) -> None:
    """A loader failure blocks startup and reports an unknown installed version."""
    integration_loader.side_effect = LoaderError("VT is unavailable")

    assert await compatibility.async_check_vtherm_version(hass) is False

    notification = hass.data[persistent_notification.DOMAIN][
        compatibility.NOTIFICATION_ID
    ]
    assert "?" in notification["message"]


async def test_notification_is_reused_and_dismissed_after_upgrade(
    hass, integration_loader, translations
) -> None:
    """Repeated refusals keep one notification, removed by a compatible version."""
    integration_loader.return_value.version = AwesomeVersion("10.6.1")
    for _ in range(2):
        assert await compatibility.async_check_vtherm_version(hass) is False

    assert list(hass.data[persistent_notification.DOMAIN]) == [
        compatibility.NOTIFICATION_ID
    ]

    integration_loader.return_value.version = AwesomeVersion("10.6.2")
    assert await compatibility.async_check_vtherm_version(hass) is True
    assert hass.data[persistent_notification.DOMAIN] == {}


@pytest.mark.parametrize("version", ["10.6.1", None])
async def test_incompatible_vtherm_prevents_factory_and_service_registration(
    hass, monkeypatch, integration_loader, translations, version
) -> None:
    """Refuse SmartPI setup before publishing its factory or services."""
    integration_loader.return_value.version = (
        AwesomeVersion(version) if version is not None else None
    )
    register_factory = Mock()
    register_services = Mock()
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi._register_factory", register_factory
    )
    monkeypatch.setattr(
        "custom_components.vtherm_smartpi._register_services", register_services
    )

    assert await async_setup(hass, {}) is False

    register_factory.assert_not_called()
    register_services.assert_not_called()
    assert compatibility.NOTIFICATION_ID in hass.data[persistent_notification.DOMAIN]
