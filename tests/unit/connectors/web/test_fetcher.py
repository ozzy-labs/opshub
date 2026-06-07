"""Tests for :func:`opshub.connectors.web.fetcher.fetch_pages` (Phase 21-C).

The fetcher drives the browser core over a URL list. These tests stub
:func:`opshub.browser.core.fetch_page` (the connector's real fetch site is
the lazy browser core) so the per-URL failure posture is pinned without a
real Chromium launch:

1. Each URL → one :class:`PageContent` (success path).
2. A :class:`BrowserFetchError` on one URL is logged at WARN + skipped; the
   loop continues to the next URL.
3. A :class:`ConfigError` (binary missing) is **not** swallowed — it
   propagates (whole-run failure).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import opshub.connectors.web.fetcher as fetcher_mod
from opshub.browser.core import BrowserFetchError, PageContent
from opshub.connectors.web.fetcher import fetch_pages
from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError


def _settings() -> OpsHubSettings:
    return OpsHubSettings()


def _install_fake_fetch_page(
    monkeypatch: pytest.MonkeyPatch,
    behaviour: dict[str, PageContent | Exception],
) -> None:
    """Patch the module-level ``fetch_page`` the fetcher imported.

    ``behaviour`` maps a URL → either a :class:`PageContent` to return or an
    exception instance to raise.
    """

    def fake_fetch_page(url: str, *, settings: Any) -> PageContent:
        outcome = behaviour[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher_mod, "fetch_page", fake_fetch_page)


def test_fetch_pages_yields_one_per_success(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = ["https://a.com", "https://b.com"]
    _install_fake_fetch_page(
        monkeypatch,
        {
            "https://a.com": PageContent(url="https://a.com", title="A", text="a", truncated=False),
            "https://b.com": PageContent(url="https://b.com", title="B", text="b", truncated=False),
        },
    )

    out = list(fetch_pages(pages, settings=_settings(), logger=MagicMock()))

    assert [p.url for p in out] == pages
    assert [p.title for p in out] == ["A", "B"]


def test_fetch_pages_skips_browser_fetch_error_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = ["https://dead.com", "https://ok.com"]
    _install_fake_fetch_page(
        monkeypatch,
        {
            "https://dead.com": BrowserFetchError("timed out loading page after 30000ms"),
            "https://ok.com": PageContent(
                url="https://ok.com", title="OK", text="ok", truncated=False
            ),
        },
    )
    logger = MagicMock()

    out = list(fetch_pages(pages, settings=_settings(), logger=logger))

    # Only the good page is yielded; the dead one is skipped.
    assert [p.url for p in out] == ["https://ok.com"]
    # WARN logged once for the skipped URL, with the URL as a field.
    logger.warning.assert_called_once()
    _, kwargs = logger.warning.call_args
    assert kwargs["url"] == "https://dead.com"


def test_fetch_pages_propagates_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary-missing ``ConfigError`` is a whole-run failure, not a skip."""
    pages = ["https://x.com", "https://y.com"]
    _install_fake_fetch_page(
        monkeypatch,
        {
            "https://x.com": ConfigError(
                "Chromium is not installed; run 'playwright install chromium'"
            ),
        },
    )

    with pytest.raises(ConfigError, match="playwright install chromium"):
        list(fetch_pages(pages, settings=_settings(), logger=MagicMock()))


def test_fetch_pages_tolerates_logger_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logger lacking ``warning`` must not crash the skip path."""
    pages = ["https://dead.com"]
    _install_fake_fetch_page(
        monkeypatch,
        {"https://dead.com": BrowserFetchError("navigation failed")},
    )

    # ``object()`` has no ``warning`` attribute — the fetcher duck-types it.
    out = list(fetch_pages(pages, settings=_settings(), logger=object()))
    assert out == []


def test_fetch_pages_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_fetch_page(monkeypatch, {})
    out = list(fetch_pages([], settings=_settings(), logger=MagicMock()))
    assert out == []
