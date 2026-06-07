"""Tests for the ``[browser]`` settings section (Phase 21-B, ADR-0037).

Pins the default shape, env-var override path (``OPSHUB_BROWSER__*``),
and the dedicated user-data-dir helper
(:func:`opshub.core.platform.browser_user_data_dir`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import BrowserSettings, OpsHubSettings
from opshub.core.platform import BROWSER_PROFILE_DIRNAME, browser_user_data_dir


def test_browser_defaults() -> None:
    browser = BrowserSettings()
    assert browser.headless is True
    assert browser.channel is None
    assert browser.timeout == 30_000
    assert browser.cdp_endpoint is None


def test_browser_section_default_on_root_settings(tmp_path: Path) -> None:
    settings = OpsHubSettings(data_dir=tmp_path)
    assert isinstance(settings.browser, BrowserSettings)
    assert settings.browser.headless is True


def test_browser_explicit_overrides() -> None:
    browser = BrowserSettings(
        headless=False,
        channel="msedge",
        timeout=5000,
        cdp_endpoint="http://localhost:9222",
    )
    assert browser.headless is False
    assert browser.channel == "msedge"
    assert browser.timeout == 5000
    assert browser.cdp_endpoint == "http://localhost:9222"


def test_browser_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nested env-var override pattern (OPSHUB_ prefix + __ delimiter).
    monkeypatch.setenv("OPSHUB_BROWSER__HEADLESS", "false")
    monkeypatch.setenv("OPSHUB_BROWSER__CHANNEL", "chrome")
    monkeypatch.setenv("OPSHUB_BROWSER__TIMEOUT", "12000")
    monkeypatch.setenv("OPSHUB_BROWSER__CDP_ENDPOINT", "http://localhost:9333")

    settings = OpsHubSettings()

    assert settings.browser.headless is False
    assert settings.browser.channel == "chrome"
    assert settings.browser.timeout == 12000
    assert settings.browser.cdp_endpoint == "http://localhost:9333"


def test_browser_user_data_dir_under_data_dir() -> None:
    data_dir = Path("/var/lib/opshub")
    assert browser_user_data_dir(data_dir) == data_dir / "browser"
    assert BROWSER_PROFILE_DIRNAME == "browser"
