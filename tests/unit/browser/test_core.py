"""Unit tests for :mod:`opshub.browser.core` (Phase 21-B, ADR-0037).

These pin the browser-core contract **without a real Chromium** by
injecting a fake ``playwright.sync_api`` module into ``sys.modules`` so
the lazily-imported ``from playwright.sync_api import ...`` statements
inside :mod:`opshub.browser.core` resolve to the fake. The fake records
the launch kwargs / navigation args so we can assert:

* launch flag pinning — headless + opshub-dedicated ``user-data-dir`` +
  ``channel`` (ADR-0037 §決定 (c)),
* the ``connect_over_cdp`` attach path when ``[browser] cdp_endpoint``
  is set (ADR-0037 §決定 (b)),
* extraction = ``page.inner_text("body")`` + ``page.title()``
  (ADR-0037 §決定 (d), 21-B decision),
* the 500K char cap (head-truncation + marker, ADR-0025 §決定 (b-2)),
* timeout / navigation error → :class:`BrowserFetchError`,
* Chromium-binary-missing → :class:`ConfigError` pointing at
  ``playwright install chromium``.

The package-missing :class:`ConfigError` (``import playwright`` itself
failing) is covered by removing the fake module from ``sys.modules``.

The real-Chromium smoke test lives in
``tests/integration/test_browser_core.py`` behind the ``browser`` marker.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, TracebackType
from typing import TYPE_CHECKING, Any

import pytest

from opshub.browser.core import (
    DEFAULT_MAX_CHARS,
    BrowserFetchError,
    PageContent,
    fetch_page,
)
from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator


# --------------------------------------------------------------------- fakes


class _FakePlaywrightError(Exception):
    """Stand-in for ``playwright.sync_api.Error``."""


class _FakePlaywrightTimeoutError(_FakePlaywrightError):
    """Stand-in for ``playwright.sync_api.TimeoutError`` (subclass of Error)."""


class _FakePage:
    def __init__(self, *, title: str, body_text: str) -> None:
        self.title_text = title
        self.body_text = body_text
        self.goto_calls: list[dict[str, Any]] = []
        # Behaviour overrides set by individual tests.
        self.goto_raises: Exception | None = None
        self.inner_text_selector: str | None = None

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self.goto_raises is not None:
            raise self.goto_raises

    def title(self) -> str:
        return self.title_text

    def inner_text(self, selector: str) -> str:
        self.inner_text_selector = selector
        return self.body_text


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.contexts = [context]
        self.closed = False

    def new_context(self) -> _FakeContext:  # pragma: no cover - exercised when no contexts
        ctx = _FakeContext(self.contexts[0].page)
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, recorder: dict[str, Any], context: _FakeContext) -> None:
        self.recorder = recorder
        self.context = context
        self.launch_raises: Exception | None = None

    def launch_persistent_context(
        self,
        *,
        user_data_dir: str,
        headless: bool,
        channel: str | None,
    ) -> _FakeContext:
        self.recorder["launch"] = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            "channel": channel,
        }
        if self.launch_raises is not None:
            raise self.launch_raises
        return self.context

    def connect_over_cdp(self, endpoint: str) -> _FakeBrowser:
        self.recorder["connect_over_cdp"] = {"endpoint": endpoint}
        return _FakeBrowser(self.context)


class _FakePlaywrightHandle:
    def __init__(self, chromium: _FakeChromium, recorder: dict[str, Any]) -> None:
        self.chromium = chromium
        self._recorder = recorder

    def stop(self) -> None:
        self._recorder["stopped"] = True


class _FakeSyncPlaywrightCM:
    def __init__(self, handle: _FakePlaywrightHandle) -> None:
        self._handle = handle

    def start(self) -> _FakePlaywrightHandle:
        return self._handle

    # Support the ``with sync_playwright() as p`` form too, though the
    # core uses ``.start()`` / ``.stop()`` explicitly.
    def __enter__(self) -> _FakePlaywrightHandle:  # pragma: no cover - unused form
        return self._handle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:  # pragma: no cover - unused form
        self._handle.stop()


def _install_fake_playwright(
    *,
    title: str = "Example Domain",
    body_text: str = "Hello rendered world",
) -> tuple[dict[str, Any], _FakePage]:
    """Install a fake ``playwright.sync_api`` module into ``sys.modules``.

    Returns ``(recorder, page)`` so the test can assert on launch kwargs
    and mutate page behaviour (raise on ``goto`` etc.).
    """
    recorder: dict[str, Any] = {}
    page = _FakePage(title=title, body_text=body_text)
    context = _FakeContext(page)
    chromium = _FakeChromium(recorder, context)
    handle = _FakePlaywrightHandle(chromium, recorder)
    recorder["chromium"] = chromium

    module = ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _FakeSyncPlaywrightCM(handle)  # type: ignore[attr-defined]
    module.Error = _FakePlaywrightError  # type: ignore[attr-defined]
    module.TimeoutError = _FakePlaywrightTimeoutError  # type: ignore[attr-defined]

    parent = ModuleType("playwright")
    sys.modules["playwright"] = parent
    sys.modules["playwright.sync_api"] = module
    return recorder, page


@pytest.fixture
def fake_playwright() -> Iterator[tuple[dict[str, Any], _FakePage]]:
    saved = {name: sys.modules.get(name) for name in ("playwright", "playwright.sync_api")}
    recorder, page = _install_fake_playwright()
    try:
        yield recorder, page
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture
def settings(tmp_path: Path) -> OpsHubSettings:
    # Point data_dir at a tmp path so the browser user-data-dir resolves
    # under the test sandbox (the launch path mkdir's it).
    return OpsHubSettings(data_dir=tmp_path / "data")


# --------------------------------------------------------------------- launch


def test_launch_pins_headless_and_dedicated_user_data_dir(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    recorder, _page = fake_playwright

    fetch_page("https://example.com", settings=settings)

    launch = recorder["launch"]
    assert launch["headless"] is True
    assert launch["channel"] is None
    # Dedicated user-data-dir lives under <data_dir>/browser (ADR-0037 §(c)).
    assert launch["user_data_dir"] == str(settings.data_dir / "browser")
    assert Path(launch["user_data_dir"]).is_dir()
    # No CDP attach on the launch path.
    assert "connect_over_cdp" not in recorder


def test_launch_forwards_channel_and_headless_override(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    tmp_path: Path,
) -> None:
    recorder, _page = fake_playwright
    settings = OpsHubSettings(
        data_dir=tmp_path / "data",
        browser={"headless": False, "channel": "chrome"},
    )

    fetch_page("https://example.com", settings=settings)

    launch = recorder["launch"]
    assert launch["headless"] is False
    assert launch["channel"] == "chrome"


def test_cdp_endpoint_takes_attach_path(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    tmp_path: Path,
) -> None:
    recorder, _page = fake_playwright
    settings = OpsHubSettings(
        data_dir=tmp_path / "data",
        browser={"cdp_endpoint": "http://localhost:9222"},
    )

    result = fetch_page("https://example.com", settings=settings)

    assert recorder["connect_over_cdp"] == {"endpoint": "http://localhost:9222"}
    # The launch path is never taken when attaching.
    assert "launch" not in recorder
    assert result.text == "Hello rendered world"


# --------------------------------------------------------------------- fetch


def test_fetch_extracts_title_and_inner_text(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright

    result = fetch_page("https://example.com/path", settings=settings)

    assert isinstance(result, PageContent)
    assert result.url == "https://example.com/path"
    assert result.title == "Example Domain"
    assert result.text == "Hello rendered world"
    assert result.truncated is False
    # Extraction uses inner_text over the body selector (21-B decision).
    assert page.inner_text_selector == "body"
    # Navigation waits for the load event with the configured timeout.
    assert page.goto_calls == [
        {
            "url": "https://example.com/path",
            "wait_until": "load",
            "timeout": settings.browser.timeout,
        }
    ]


def test_fetch_respects_timeout_setting(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    tmp_path: Path,
) -> None:
    _recorder, page = fake_playwright
    settings = OpsHubSettings(data_dir=tmp_path / "data", browser={"timeout": 5000})

    fetch_page("https://example.com", settings=settings)

    assert page.goto_calls[0]["timeout"] == 5000


def test_fetch_caps_text_at_max_chars(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    page.body_text = "x" * (DEFAULT_MAX_CHARS + 100)

    result = fetch_page("https://example.com", settings=settings)

    assert result.truncated is True
    # Head-truncated to the cap + the appended marker (so strictly longer
    # than the kept prefix, but the kept prefix is exactly max_chars).
    assert result.text.startswith("x" * DEFAULT_MAX_CHARS)
    assert "browser body truncated" in result.text
    assert f"kept {DEFAULT_MAX_CHARS}" in result.text


def test_fetch_honours_explicit_max_chars(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    page.body_text = "abcdefghij"  # 10 chars

    result = fetch_page("https://example.com", settings=settings, max_chars=4)

    assert result.truncated is True
    assert result.text.startswith("abcd")
    assert "kept 4" in result.text


def test_fetch_empty_body_is_not_truncated(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    page.body_text = ""

    result = fetch_page("https://example.com", settings=settings)

    assert result.text == ""
    assert result.truncated is False


# --------------------------------------------------------------------- errors


def test_navigation_timeout_becomes_browser_fetch_error(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    page.goto_raises = _FakePlaywrightTimeoutError("Timeout 30000ms exceeded")

    with pytest.raises(BrowserFetchError) as excinfo:
        fetch_page("https://example.com", settings=settings)

    assert "timed out" in str(excinfo.value)
    # BrowserFetchError is a ConnectorFailedError so the connector's
    # existing fail-safe path catches it.
    assert isinstance(excinfo.value, ConnectorFailedError)


def test_navigation_error_becomes_browser_fetch_error(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    page.goto_raises = _FakePlaywrightError("net::ERR_NAME_NOT_RESOLVED")

    with pytest.raises(BrowserFetchError) as excinfo:
        fetch_page("https://nonexistent.invalid", settings=settings)

    assert "failed to fetch page" in str(excinfo.value)


def test_error_message_is_sanitised(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    _recorder, page = fake_playwright
    # An OpenAI/Anthropic-style secret key leaking into the navigation
    # error must be redacted by sanitise_error_message before it reaches
    # the BrowserFetchError message (and therefore the log / any body).
    leaked = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    page.goto_raises = _FakePlaywrightError(f"auth failed: {leaked}")

    with pytest.raises(BrowserFetchError) as excinfo:
        fetch_page("https://example.com", settings=settings)

    assert leaked not in str(excinfo.value)
    assert "sk-***" in str(excinfo.value)


def test_context_is_closed_even_on_error(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    recorder, page = fake_playwright
    page.goto_raises = _FakePlaywrightError("boom")

    with pytest.raises(BrowserFetchError):
        fetch_page("https://example.com", settings=settings)

    # The launch-path context (recorder["chromium"].context) was closed.
    assert recorder["chromium"].context.closed is True


def test_missing_chromium_binary_becomes_config_error(
    fake_playwright: tuple[dict[str, Any], _FakePage],
    settings: OpsHubSettings,
) -> None:
    recorder, _page = fake_playwright
    chromium: _FakeChromium = recorder["chromium"]
    chromium.launch_raises = _FakePlaywrightError(
        "Executable doesn't exist at /home/runner/.cache/ms-playwright/chromium/chrome"
    )

    with pytest.raises(ConfigError) as excinfo:
        fetch_page("https://example.com", settings=settings)

    assert "playwright install chromium" in str(excinfo.value)
    # The Playwright driver is stopped so we do not leak the subprocess.
    assert recorder.get("stopped") is True


def test_missing_playwright_package_becomes_config_error(
    settings: OpsHubSettings,
) -> None:
    # Ensure neither the real nor a fake playwright module is importable
    # so the lazy ``import playwright.sync_api`` raises ImportError.
    saved = {name: sys.modules.get(name) for name in ("playwright", "playwright.sync_api")}
    sys.modules["playwright"] = None  # type: ignore[assignment]
    sys.modules["playwright.sync_api"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigError) as excinfo:
            fetch_page("https://example.com", settings=settings)
        assert "browser" in str(excinfo.value)
        assert "playwright install chromium" in str(excinfo.value)
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_default_max_chars_matches_adr_0025_cap() -> None:
    # ADR-0037 §決定 (d): reuse the ADR-0025 500K cap verbatim (no
    # browser-specific default).
    assert DEFAULT_MAX_CHARS == 500_000
