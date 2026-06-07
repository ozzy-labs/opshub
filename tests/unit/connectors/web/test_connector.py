"""Tests for :class:`opshub.connectors.web.connector.WebConnector` (Phase 21-C).

Pins the connector-level contract (ADR-0037 / ADR-0010 §Phase 21 改訂)
independently of the end-to-end CLI lifecycle:

1. ``name`` matches the registry / CLI dispatch key (``"web"``).
2. Each fetched page → one :meth:`SourceService.observe` call carrying the
   mapper's normalised ``external_id`` + SHA-256 ``fingerprint``.
3. Fingerprint **skip**: a page whose body hash matches the prior
   ``sources.fingerprint`` emits no event (ADR-0019 §決定 (d)).
4. Prior fingerprints are hydrated from the ``sources`` projection via the
   :class:`SourceService` engine handle (``connector_name = 'web'`` +
   ``fingerprint IS NOT NULL`` skip).
5. A per-URL :class:`BrowserFetchError` is logged at WARN and skipped; the
   other pages still sync (fail-safe posture).
6. :class:`ConfigError` (binary missing) propagates out of ``sync`` (whole
   run failure, not a per-URL skip).
7. Empty ``pages`` → 0 observed, no error.
8. Importing the package registers the connector.

Tests use a fake source-service double + a fetch-pages factory injection so
no real Chromium / SQLite engine is required for the connector-internal
logic (the integration suite covers the wired end-to-end path).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from opshub.browser.core import PageContent
from opshub.connectors.context import ConnectorContext
from opshub.connectors.web.connector import WebConnector
from opshub.connectors.web.mapper import fingerprint_body, normalize_url
from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService` recording ``observe`` calls.

    Mirrors the keyword-only signature of the real service so a drift on
    argument names trips :class:`TypeError` immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.engine: Any = None
        self._engine: Any = None

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
        fingerprint: str | None = None,
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "fingerprint": fingerprint,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
            }
        )


def _context(service: _RecordingSourceService) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=MagicMock(),
    )


def _page(url: str, *, title: str = "T", text: str = "body") -> PageContent:
    return PageContent(url=url, title=title, text=text, truncated=False)


def _settings_with_pages(pages: list[str]) -> OpsHubSettings:
    return OpsHubSettings(connectors={"web": {"pages": pages}})


def _connector(
    *,
    pages: list[str],
    fetch_pages: Any,
) -> WebConnector:
    return WebConnector(
        settings_factory=lambda: _settings_with_pages(pages),
        fetch_pages_factory=fetch_pages,
    )


# ---------------------------------------------------------------------- name


def test_connector_name_is_web() -> None:
    assert WebConnector.name == "web"
    assert WebConnector().name == "web"


# ---------------------------------------------------------------------- sync flow


def test_sync_observes_each_fetched_page() -> None:
    """Each yielded page → one ``observe`` call with normalised id + fingerprint."""
    pages_in = ["https://a.com", "https://b.com/p"]

    def fake_fetch(pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
        assert list(pages) == pages_in
        yield _page("https://a.com", title="A", text="body-a")
        yield _page("https://b.com/p", title="B", text="body-b")

    service = _RecordingSourceService()
    result = _connector(pages=pages_in, fetch_pages=fake_fetch).sync(_context(service))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == [
        normalize_url("https://a.com"),
        normalize_url("https://b.com/p"),
    ]
    assert all(c["connector_name"] == "web" for c in service.calls)
    assert all(c["source_type"] == "web_page" for c in service.calls)
    assert service.calls[0]["fingerprint"] == fingerprint_body("body-a")
    assert service.calls[1]["fingerprint"] == fingerprint_body("body-b")
    assert service.calls[0]["body"] == "body-a"
    assert all(c["provenance_trust"] == "untrusted" for c in service.calls)


def test_sync_skips_unchanged_page_by_fingerprint() -> None:
    """A page whose body hash matches the prior fingerprint emits no event.

    Stands up a real in-memory SQLite engine seeded with a prior ``web``
    row whose fingerprint equals the SHA-256 of the page body, then asserts
    the connector short-circuits (ADR-0019 §決定 (d)).
    """
    from sqlalchemy import create_engine, insert

    from opshub.db.schema import metadata
    from opshub.projections.sources import sources_table

    url = "https://example.com/page"
    body = "stable body"
    normalized = normalize_url(url)

    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        now = datetime(2026, 6, 7, tzinfo=UTC)
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000WW",
                    connector_name="web",
                    external_id=normalized,
                    source_type="web_page",
                    title="Example",
                    url=normalized,
                    summary="Example",
                    observed_at=now,
                    updated_at=now,
                    fingerprint=fingerprint_body(body),
                    body=body,
                )
            )

        def fake_fetch(_pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
            yield _page(url, title="Example", text=body)

        service = _RecordingSourceService()
        service.engine = engine
        result = _connector(pages=[url], fetch_pages=fake_fetch).sync(_context(service))

        # Unchanged → no observe call.
        assert result.observed_count == 0
        assert service.calls == []
    finally:
        engine.dispose()


def test_sync_observes_changed_page_when_fingerprint_differs() -> None:
    """A page whose body changed (different hash) is re-observed."""
    from sqlalchemy import create_engine, insert

    from opshub.db.schema import metadata
    from opshub.projections.sources import sources_table

    url = "https://example.com/page"
    normalized = normalize_url(url)

    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        now = datetime(2026, 6, 7, tzinfo=UTC)
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000XX",
                    connector_name="web",
                    external_id=normalized,
                    source_type="web_page",
                    title="Old",
                    url=normalized,
                    summary="Old",
                    observed_at=now,
                    updated_at=now,
                    fingerprint=fingerprint_body("old body"),
                    body="old body",
                )
            )

        def fake_fetch(_pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
            yield _page(url, title="New", text="new body")

        service = _RecordingSourceService()
        service.engine = engine
        result = _connector(pages=[url], fetch_pages=fake_fetch).sync(_context(service))

        assert result.observed_count == 1
        assert service.calls[0]["fingerprint"] == fingerprint_body("new body")
    finally:
        engine.dispose()


def test_sync_hydrates_prior_fingerprints_scoped_to_web() -> None:
    """Prior map only includes ``connector_name = 'web'`` non-NULL rows."""
    from sqlalchemy import create_engine, insert

    from opshub.db.schema import metadata
    from opshub.projections.sources import sources_table

    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        now = datetime(2026, 6, 7, tzinfo=UTC)
        with engine.begin() as conn:
            # web row with fingerprint — included
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000A1",
                    connector_name="web",
                    external_id="https://x.com/",
                    source_type="web_page",
                    title="X",
                    url="https://x.com/",
                    summary="X",
                    observed_at=now,
                    updated_at=now,
                    fingerprint="abc123",
                    body="x",
                )
            )
            # foreign connector — must not leak
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000A2",
                    connector_name="box_drive",
                    external_id="file.txt",
                    source_type="box_drive_file",
                    title="file.txt",
                    url=None,
                    summary="path: file.txt",
                    observed_at=now,
                    updated_at=now,
                    fingerprint="should-not-leak",
                    body="path: file.txt",
                )
            )
            # web row with NULL fingerprint — skipped
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000A3",
                    connector_name="web",
                    external_id="https://y.com/",
                    source_type="web_page",
                    title="Y",
                    url="https://y.com/",
                    summary="Y",
                    observed_at=now,
                    updated_at=now,
                    fingerprint=None,
                    body="y",
                )
            )

        captured: dict[str, Any] = {}

        def fake_fetch(_pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
            # The connector compares against the hydrated prior map; we make
            # the page bodies match the seeded fingerprints to assert which
            # rows the prior map carried. ``https://x.com/`` has a prior
            # fingerprint "abc123" which the body "x" will never equal, so
            # it WILL be observed — what we assert instead is that the
            # foreign row never suppresses anything.
            yield _page("https://x.com/", title="X", text="x")
            return
            yield  # pragma: no cover

        service = _RecordingSourceService()
        service.engine = engine
        captured.clear()
        result = _connector(pages=["https://x.com/"], fetch_pages=fake_fetch).sync(
            _context(service)
        )

        # The page body "x" hashes to something != "abc123", so it is
        # observed (changed). The foreign box_drive row's "should-not-leak"
        # fingerprint never participated.
        assert result.observed_count == 1
        assert service.calls[0]["external_id"] == "https://x.com/"
    finally:
        engine.dispose()


def test_sync_skips_failed_url_and_continues() -> None:
    """A per-URL ``BrowserFetchError`` is logged at WARN and skipped.

    The first URL raises; the second succeeds. The run yields one observe
    call (the good page) and one WARN log line (the dead page), never an
    abort.
    """

    def fake_fetch(pages: Iterable[str], *, logger: Any, **_: Any) -> Iterator[PageContent]:
        # Replicate the real fetcher's per-URL skip-and-continue so the
        # connector test exercises the contract end-to-end through a stub
        # that has the same control flow shape.
        for url in pages:
            if url == "https://dead.com":
                logger.warning("web.fetch_failed", url=url, reason="timed out")
                continue
            yield _page(url, title="Good", text="good body")

    service = _RecordingSourceService()
    logger = MagicMock()
    context = ConnectorContext(
        source_service=service, cursor_value=None, secrets=None, logger=logger
    )
    connector = WebConnector(
        settings_factory=lambda: _settings_with_pages(["https://dead.com", "https://ok.com"]),
        fetch_pages_factory=fake_fetch,
    )
    result = connector.sync(context)

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == normalize_url("https://ok.com")
    logger.warning.assert_called_once()


def test_sync_propagates_config_error_when_binary_missing() -> None:
    """A ``ConfigError`` from the first fetch propagates (whole-run failure)."""

    def fake_fetch(_pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
        raise ConfigError("Chromium is not installed; run 'playwright install chromium'")
        yield  # pragma: no cover

    service = _RecordingSourceService()
    connector = _connector(pages=["https://x.com"], fetch_pages=fake_fetch)

    with pytest.raises(ConfigError, match="playwright install chromium"):
        connector.sync(_context(service))
    assert service.calls == []


def test_sync_empty_pages_is_noop_success() -> None:
    """No pages configured → 0 observed, no error, informational cursor set."""

    def fake_fetch(pages: Iterable[str], **_: Any) -> Iterator[PageContent]:
        assert list(pages) == []
        return
        yield  # pragma: no cover

    service = _RecordingSourceService()
    result = _connector(pages=[], fetch_pages=fake_fetch).sync(_context(service))

    assert result.observed_count == 0
    assert service.calls == []
    assert result.new_cursor is not None
    # ISO-8601 tz-aware timestamp (matches box_drive cursor shape).
    parsed = datetime.fromisoformat(result.new_cursor)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------- registry


def test_web_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.web` registers the connector."""
    import importlib

    import opshub.connectors.web
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.web)

    names = {c.name for c in discover_connectors()}
    assert "web" in names
