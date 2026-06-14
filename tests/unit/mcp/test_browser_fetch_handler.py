"""Unit tests for the ``browser.fetch`` MCP write handler (Phase 21-D).

ADR-0037 §決定 (e) + ADR-0022 改訂 add ``browser.fetch`` as a
write-category ad-hoc Web read: it renders a URL with Chromium and
returns the extracted text + title **without persisting anything**.
These tests fake the browser core (:func:`opshub.browser.core.fetch_page`)
so they run on any machine — no Chromium binary, no network. The
``browser`` marker'd real-Chromium path lives in
``tests/integration/test_browser_fetch_mcp.py``.

Pins:

* the handler bridges the async MCP boundary to the sync browser core
  (the fake records that it was invoked, and the handler awaits it);
* the returned snippet is **truncated** at the context-frugal cap
  (ADR-0022 §(d)) while the full ``text_chars`` length + ``truncated``
  hint are surfaced verbatim;
* nothing is persisted (``persisted: false`` envelope flag);
* non-http(s) schemes (``file`` / ``data`` / ``javascript`` / bare /
  host-less) are rejected with an ``OpsHubError`` so the headless
  browser is never a local-file exfiltration primitive;
* a ``BrowserFetchError`` from the core propagates (and through the
  server dispatch wrapper its message is run through the redactor — the
  dispatch-level redaction is covered separately in
  ``test_server_dispatch`` / ``test_redact``; here we assert the raw
  propagation and the no-token-leak shape).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from opshub.browser.core import BrowserFetchError, PageContent
from opshub.core.errors import OpsHubError
from opshub.mcp._writes import (
    _BROWSER_FETCH_SNIPPET_MAX_CHARS,  # pyright: ignore[reportPrivateUsage]
    build_browser_fetch_handler,
)

# Every tool name the registry materialises — ``build_tool_specs``
# indexes each one out of the ``handlers`` mapping, so the
# dispatch-redaction test below must supply a handler (real or stub)
# for all of them. Kept in lockstep with
# ``tests/unit/mcp/test_registry_policy._TOOL_NAMES`` (27 tools after
# Phase 25-D).
_ALL_TOOL_NAMES: tuple[str, ...] = (
    "recall.search",
    "task.list",
    "inbox.list",
    "decision.list",
    "task.create",
    "inbox.add",
    "connector.sync",
    "brief",
    "graph.related",
    "graph.trace",
    "graph.expand",
    "source.list",
    "source.get",
    "embeddings.find_duplicates",
    "propose.generate",
    "search",
    "propose.apply",
    "slack.demand.list",
    "browser.fetch",
    # Phase 25-D (epic #566, ADR-0042 / ADR-0043): 秘書化 v1 surface.
    "commitment.list",
    "person.list",
    "catchup",
    "commitment.scan",
    "commitment.resolve",
    "commitment.dismiss",
    "person.merge",
    "person.split",
)


class _FakeFetch:
    """Records the ``fetch_page`` call and returns a canned ``PageContent``.

    The handler imports ``fetch_page`` lazily from
    :mod:`opshub.browser.core` inside its body, so monkeypatching that
    module attribute swaps the real Playwright render for this fake.
    """

    def __init__(self, result: PageContent) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> PageContent:
        self.calls.append((url, kwargs))
        return self.result


def _install_fake_fetch(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    monkeypatch.setattr("opshub.browser.core.fetch_page", fake)


async def _invoke(handler: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    raw = await handler(arguments)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


@pytest.fixture
def engine() -> object:
    # The handler ignores ``engine`` (it resolves OpsHubSettings itself);
    # a sentinel object documents that it is accepted for symmetry.
    return object()


@pytest.mark.asyncio
async def test_browser_fetch_returns_title_and_truncated_snippet(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: title verbatim + truncated text + length / persist hints."""
    long_text = "A" * 500  # well over the 200-char preview cap
    fake = _FakeFetch(
        PageContent(
            url="https://example.com/page",
            title="Example Page",
            text=long_text,
            truncated=False,
        )
    )
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    payload = await _invoke(handler, {"url": "https://example.com/page"})

    assert payload["ok"] is True
    assert payload["url"] == "https://example.com/page"
    assert payload["title"] == "Example Page"
    # Snippet is truncated to the preview cap with the ellipsis marker.
    assert payload["text"].endswith("…")
    assert len(payload["text"]) == _BROWSER_FETCH_SNIPPET_MAX_CHARS
    # The full rendered length is surfaced verbatim so the agent knows
    # how much it is NOT seeing in the preview snippet.
    assert payload["text_chars"] == 500
    assert payload["truncated"] is False
    # Ad-hoc read: nothing persisted.
    assert payload["persisted"] is False
    # The handler actually bridged to the (faked) sync core with the URL
    # and the resolved ``settings`` (the only kwarg it forwards).
    assert len(fake.calls) == 1
    call_url, call_kwargs = fake.calls[0]
    assert call_url == "https://example.com/page"
    assert "settings" in call_kwargs


@pytest.mark.asyncio
async def test_browser_fetch_short_body_is_returned_whole(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body under the preview cap is returned unclipped (no ellipsis)."""
    fake = _FakeFetch(
        PageContent(
            url="http://127.0.0.1:8000/",
            title="local",
            text="short body",
            truncated=False,
        )
    )
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    payload = await _invoke(handler, {"url": "http://127.0.0.1:8000/"})

    assert payload["text"] == "short body"
    assert "…" not in payload["text"]
    assert payload["text_chars"] == len("short body")


@pytest.mark.asyncio
async def test_browser_fetch_surfaces_core_truncated_flag(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the browser core hit its 500K cap, ``truncated`` is propagated."""
    fake = _FakeFetch(
        PageContent(
            url="https://example.com/big",
            title="big",
            text="capped body",
            truncated=True,
        )
    )
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    payload = await _invoke(handler, {"url": "https://example.com/big"})

    assert payload["truncated"] is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "javascript:alert(1)",
        "ftp://example.com/file",
        "ws://example.com/socket",
    ],
)
@pytest.mark.asyncio
async def test_browser_fetch_rejects_non_http_schemes(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Only http/https are accepted; other schemes raise before any fetch.

    The headless browser must never be turned into a local-file / data-URI
    exfiltration primitive, so the scheme gate runs *before* the core is
    invoked. We install a fake that would record a call so we can assert
    the core was never reached.
    """
    fake = _FakeFetch(PageContent(url=url, title="x", text="x", truncated=False))
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    with pytest.raises(OpsHubError, match="http"):
        await handler({"url": url})
    assert fake.calls == [], "the browser core must not run for a rejected scheme"


@pytest.mark.asyncio
async def test_browser_fetch_rejects_hostless_http_url(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An http URL without a host (e.g. ``http:///path``) is rejected."""
    fake = _FakeFetch(PageContent(url="x", title="x", text="x", truncated=False))
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    with pytest.raises(OpsHubError, match="host"):
        await handler({"url": "http:///just/a/path"})
    assert fake.calls == []


@pytest.mark.asyncio
async def test_browser_fetch_rejects_empty_url(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``url`` is rejected before scheme parsing."""
    fake = _FakeFetch(PageContent(url="x", title="x", text="x", truncated=False))
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    with pytest.raises(OpsHubError, match="non-empty"):
        await handler({"url": "   "})
    assert fake.calls == []


@pytest.mark.asyncio
async def test_browser_fetch_propagates_core_fetch_error(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``BrowserFetchError`` from the core propagates (server wrapper redacts).

    ``BrowserFetchError`` subclasses ``OpsHubError`` (via
    ``ConnectorFailedError``), so it surfaces as a clean MCP ``isError``
    at the server boundary. The message is already sanitised at the core
    raise site (no token leak); we assert the propagation here.
    """

    def _boom(url: str, **kwargs: Any) -> PageContent:
        _ = (url, kwargs)
        raise BrowserFetchError("failed to fetch page: navigation error")

    monkeypatch.setattr("opshub.browser.core.fetch_page", _boom)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]
    with pytest.raises(BrowserFetchError, match="failed to fetch page"):
        await handler({"url": "https://example.com/dead"})


@pytest.mark.asyncio
async def test_browser_fetch_output_is_redacted_through_dispatch(
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token in fetched page text is scrubbed by the dispatch redactor.

    ``browser.fetch`` reads arbitrary Web content, so a malicious / leaky
    page could embed a token-shaped string in its body. The server's
    :func:`opshub.mcp.server.dispatch_tool_call` runs every handler's
    output through :func:`opshub.mcp._redact.redact_secrets`; this test
    drives the real registry spec through that dispatch path and asserts
    the GitHub-PAT-shaped run never reaches the ``TextContent`` block.
    The snippet cap (200 chars) keeps the token within the preview, so
    the redaction is the only line of defence that removes it.
    """
    from opshub.mcp._registry import build_tool_specs
    from opshub.mcp.server import dispatch_tool_call

    leaked = "ghp_abcdef0123456789ABCDEF0123456789abcd"
    fake = _FakeFetch(
        PageContent(
            url="https://evil.example/leak",
            title="leak",
            text=f"page body containing a {leaked} token",
            truncated=False,
        )
    )
    _install_fake_fetch(monkeypatch, fake)

    handler = build_browser_fetch_handler(engine)  # type: ignore[arg-type]

    # ``build_tool_specs`` indexes every tool name out of ``handlers``;
    # stub the rest so we can materialise the real ``browser.fetch``
    # spec (with its policy) and dispatch through it.
    async def _stub(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return "{}"

    handlers: dict[str, Any] = dict.fromkeys(_ALL_TOOL_NAMES, _stub)
    handlers["browser.fetch"] = handler
    specs = build_tool_specs(handlers=handlers)
    specs_by_name = {s.name: s for s in specs}

    content = await dispatch_tool_call(
        specs_by_name, "browser.fetch", {"url": "https://evil.example/leak"}
    )
    assert len(content) == 1
    text = content[0].text
    assert leaked not in text, "dispatch must redact a token in fetched page text"
    # The non-secret context survives so the agent still sees the page.
    assert "page body containing a" in text
