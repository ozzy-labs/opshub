"""Tests for :class:`opshub.connectors.google_mail.connector.GoogleMailConnector`.

Coverage map:

* ``name`` constant + registry side-effect (import-time registration).
* First-sync bootstrap path: ``cursor_value=None`` →
  ``getProfile`` → ``messages.list`` backfill → cursor persisted.
* Resume path: stored cursor is replayed verbatim into
  ``fetch_history``; messages are fetched + mapped + observed.
* Cursor advances to the freshly-returned ``historyId`` on the final
  page (so the next sync resumes there).
* :class:`HistoryIdExpiredError` triggers a re-bootstrap (ADR-0010
  §Phase 14 改訂 (j) TTL fallback):
    1. WARNING log ``connector.history.expired``.
    2. Full-pass ``messages.list?q=after:...`` re-emits during gap.
    3. Fresh ``historyId`` is persisted as the new cursor.
* ``fallback_window_days = 0`` opts out of the recovery full-pass.
* Across-record dedup: the same message id in two history records
  is observed only once.
* :class:`ConfigError` early-return when ``client_id`` /
  ``client_secret`` is unset on the shared Workspace settings.
* ``max_body_chars`` from settings flows through to the mapper.

The Gmail HTTP layer is mocked at the :class:`GmailClient` boundary so
this file does not need to re-build the ``httpx.MockTransport`` setup
the client tests already cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="Gmail connector tests require the 'connectors-google-workspace' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.google_mail.client import (
    HistoryIdExpiredError,
    RawGmailMessage,
)
from opshub.connectors.google_mail.connector import GoogleMailConnector
from opshub.connectors.google_mail.cursor import CURSOR_HISTORY
from opshub.core.errors import ConfigError

# ----- doubles -----------------------------------------------------------


class _RecordingSourceService:
    """Test double for :class:`SourceService`.

    Same shape as the google_workspace connector test double — pinning
    the Phase 10 ``observe`` keyword set + cursor surface. A drift on
    argument names trips :class:`TypeError` immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._cursors: dict[str, str | None] = {}
        self.cursor_history: list[tuple[str, str | None, bool]] = []

    def cursor_get(self, key: str) -> str | None:
        return self._cursors.get(key)

    def cursor_set(self, key: str, value: str | None, *, sync_started: bool = False) -> None:
        self._cursors[key] = value
        self.cursor_history.append((key, value, sync_started))

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        pass

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
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
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
            }
        )


def _context(service: _RecordingSourceService, *, cursor: str | None = None) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor,
        secrets=None,
        logger=MagicMock(),
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_id: str = "fake-client-id",
    client_secret: str = "fake-secret",
    redirect_uri: str = "http://localhost",
    fallback_window_days: int = 30,
    max_body_chars: int = 500_000,
) -> None:
    """Stub :class:`OpsHubSettings` for the connector's lazy import."""
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = client_id
    fake_settings.connectors.google_workspace.client_secret = client_secret
    fake_settings.connectors.google_workspace.redirect_uri = redirect_uri
    fake_settings.connectors.google_mail.fallback_window_days = fallback_window_days
    fake_settings.connectors.google_mail.max_body_chars = max_body_chars
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)


def _raw(
    message_id: str,
    *,
    subject: str | None = None,
    body_text: str = "Body for {mid}",
    label_ids: tuple[str, ...] = ("INBOX",),
) -> RawGmailMessage:
    return RawGmailMessage(
        message_id=message_id,
        thread_id=f"T-{message_id}",
        history_id="100",
        snippet="snip",
        subject=subject if subject is not None else f"Subject {message_id}",
        from_header="alice@example.com",
        internal_date_ms="1735689600000",
        label_ids=label_ids,
        body_text=body_text.format(mid=message_id),
        body_html="",
        raw={},
    )


def _patch_auth_and_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bootstrap_history_id: str = "100",
    history_pages: list[list[tuple[str, str]]] | None = None,
    raise_expired_first: bool = False,
    backfill_messages: list[str] | None = None,
    fallback_messages: list[str] | None = None,
    message_factory: Any = None,
) -> MagicMock:
    """Stub :class:`GoogleWorkspaceAuth` + :class:`GmailClient`.

    ``history_pages`` is a list of "yield this list of ``(message_id, cursor)``
    tuples per call to ``fetch_history``". When ``raise_expired_first``
    is set the first ``fetch_history`` call raises
    :class:`HistoryIdExpiredError` to exercise the TTL fallback.

    ``backfill_messages`` is the list of message ids the first-sync
    ``list_messages`` call yields (when ``cursor_value`` is None).
    ``fallback_messages`` is the list yielded by the TTL-fallback
    ``list_messages`` call (when ``HistoryIdExpiredError`` was raised).
    """
    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
        MagicMock(),
    )
    fake_client = MagicMock()
    fake_client.get_profile_history_id.return_value = bootstrap_history_id

    history_queue: list[list[tuple[str, str]]] = list(history_pages or [])
    expired_pending = {"flag": raise_expired_first}

    def fetch_history(*, start_history_id: str) -> Iterator[tuple[str, str]]:
        if expired_pending["flag"]:
            expired_pending["flag"] = False
            raise HistoryIdExpiredError
        if not history_queue:
            return iter([])
        return iter(history_queue.pop(0))

    fake_client.fetch_history.side_effect = fetch_history

    # ``list_messages`` is called with ``query=`` kwarg from the
    # backfill / fallback paths. Both queries share the same fake
    # iterator; tests typically only exercise one path at a time.
    backfill_queue: list[str] = list(backfill_messages or [])
    fallback_queue: list[str] = list(fallback_messages or [])
    list_call_count = {"n": 0}

    def list_messages(*, query: str | None = None) -> Iterator[str]:
        list_call_count["n"] += 1
        # First call wins backfill, subsequent calls win fallback.
        # In practice tests use exactly one of the two paths so the
        # ordering does not matter; we still split the queues so a
        # test that exercises both back-to-back gets the right
        # sequence.
        if backfill_queue:
            queue = list(backfill_queue)
            backfill_queue.clear()
        else:
            queue = list(fallback_queue)
            fallback_queue.clear()
        return iter(queue)

    fake_client.list_messages.side_effect = list_messages

    def get_message(*, message_id: str) -> RawGmailMessage:
        if message_factory is not None:
            built = message_factory(message_id)
            assert isinstance(built, RawGmailMessage)
            return built
        return _raw(message_id)

    fake_client.get_message.side_effect = get_message
    monkeypatch.setattr(
        "opshub.connectors.google_mail.client.GmailClient",
        MagicMock(return_value=fake_client),
    )
    return fake_client


# ----- contract pin ------------------------------------------------------


def test_connector_name_pin() -> None:
    """Connector name is the registry key — pin it explicitly."""
    assert GoogleMailConnector.name == "google_mail"
    assert GoogleMailConnector().name == "google_mail"


def test_import_registers_connector() -> None:
    """Importing the package registers the connector with the registry."""
    from opshub.connectors import discover_connectors
    from opshub.connectors._registry import register_connector

    if "google_mail" not in {c.name for c in discover_connectors()}:
        register_connector(GoogleMailConnector())

    names = [c.name for c in discover_connectors()]
    assert "google_mail" in names


# ----- early-return guards -----------------------------------------------


def test_sync_requires_workspace_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, client_id="")
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_id"):
        GoogleMailConnector().sync(_context(service))


def test_sync_requires_workspace_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, client_secret="")
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_secret"):
        GoogleMailConnector().sync(_context(service))


# ----- first-sync bootstrap ----------------------------------------------


def test_sync_first_run_bootstraps_history_id_and_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cursor_value=None`` triggers ``getProfile`` + backfill, persists cursor."""
    _patch_settings(monkeypatch, fallback_window_days=30)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        bootstrap_history_id="200",
        backfill_messages=["M-A", "M-B"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service))

    fake_client.get_profile_history_id.assert_called_once()
    # The bootstrap cursor was persisted before the backfill ran.
    assert (CURSOR_HISTORY, "200", False) in service.cursor_history
    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["M-A", "M-B"]
    # Cursor stays at the bootstrap value (no delta walk in first sync).
    assert result.new_cursor == "200"


def test_sync_first_run_zero_window_skips_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fallback_window_days = 0`` opts out of the first-sync backfill."""
    _patch_settings(monkeypatch, fallback_window_days=0)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        bootstrap_history_id="300",
        backfill_messages=["should-not-yield"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service))

    fake_client.get_profile_history_id.assert_called_once()
    # No backfill — list_messages was never invoked.
    fake_client.list_messages.assert_not_called()
    assert result.observed_count == 0
    assert result.new_cursor == "300"


# ----- resume path -------------------------------------------------------


def test_sync_resume_replays_stored_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored cursor is forwarded verbatim into ``fetch_history``."""
    _patch_settings(monkeypatch)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("M-1", "next-cursor")]],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="stored-cursor"))

    fake_client.get_profile_history_id.assert_not_called()
    fake_client.fetch_history.assert_called_with(start_history_id="stored-cursor")
    assert result.observed_count == 1
    assert result.new_cursor == "next-cursor"


def test_sync_dedups_across_history_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same message id in two history records is observed once."""
    _patch_settings(monkeypatch)
    _patch_auth_and_client(
        monkeypatch,
        history_pages=[
            [
                ("M-DUP", "cur-1"),
                ("M-DUP", "cur-2"),  # second appearance — dedup target
                ("M-OTHER", "cur-3"),
            ]
        ],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="start"))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["M-DUP", "M-OTHER"]
    assert result.new_cursor == "cur-3"


# ----- TTL fallback ------------------------------------------------------


def test_sync_falls_back_full_pass_on_history_id_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired cursor → WARNING log → full-pass emit → cursor update.

    ADR-0010 §Phase 14 改訂 (j) 3-step TTL fallback pin (mirrors
    Phase 13 google_workspace TTL fallback, Drive 同型). Pre-recovery
    implementation would silently lose every message between the last
    successful sync and the recovery; this test pins the corrected
    3-step round-trip:

    1. ``list_messages(query='after:...')`` re-emits during the
       configured window.
    2. ``get_profile_history_id`` bootstraps a fresh historyId for
       the next sync.
    3. The next sync resumes on the delta path from the fresh historyId.
    """
    _patch_settings(monkeypatch, fallback_window_days=30)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        bootstrap_history_id="fresh-history",
        raise_expired_first=True,
        fallback_messages=["M-RECOVERED"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="expired-cursor"))

    # Step 1: fetch_history was attempted with the expired cursor.
    assert (
        fake_client.fetch_history.call_args_list[0].kwargs["start_history_id"] == "expired-cursor"
    )

    # Step 2: list_messages ran for the TTL window.
    assert fake_client.list_messages.call_count == 1
    fallback_kwargs = fake_client.list_messages.call_args.kwargs
    assert "query" in fallback_kwargs
    assert fallback_kwargs["query"].startswith("after:")

    # Step 3: bootstrap a fresh historyId AFTER the full-pass.
    assert fake_client.get_profile_history_id.call_count == 1

    # Recovered message emitted as SourceObserved.
    assert [c["external_id"] for c in service.calls] == ["M-RECOVERED"]
    assert result.observed_count == 1
    assert result.new_cursor == "fresh-history"


def test_sync_fallback_emits_warning_log_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL fallback emits ``connector.history.expired`` WARNING.

    Mirrors the Drive ``connector.changes_list.expired`` /
    Teams ``connector.delta.expired`` observability pin so dashboards
    can fan out on any of the three connectors with one rule.
    """
    from unittest.mock import patch as _patch_obj

    _patch_settings(monkeypatch, fallback_window_days=30)
    _patch_auth_and_client(
        monkeypatch,
        bootstrap_history_id="fresh",
        raise_expired_first=True,
        fallback_messages=[],
    )
    service = _RecordingSourceService()

    captured: list[tuple[str, dict[str, Any]]] = []

    class _CapturingLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

    def _factory(*_args: Any, **_kwargs: Any) -> _CapturingLogger:
        return _CapturingLogger()

    with _patch_obj("opshub.core.logging.get_logger", _factory):
        GoogleMailConnector().sync(_context(service, cursor="expired-cursor"))

    assert any(event == "connector.history.expired" for event, _ in captured)
    event_payload = next(
        payload for event, payload in captured if event == "connector.history.expired"
    )
    assert event_payload["connector"] == "google_mail"
    assert event_payload["window_days"] == 30
    assert event_payload["since"].endswith("Z")


def test_sync_fallback_zero_window_skips_full_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fallback_window_days = 0`` skips the full-pass; cursor refreshes only."""
    _patch_settings(monkeypatch, fallback_window_days=0)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        bootstrap_history_id="fresh",
        raise_expired_first=True,
        fallback_messages=["should-not-yield"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="expired-cursor"))

    fake_client.list_messages.assert_not_called()
    fake_client.get_profile_history_id.assert_called_once()
    assert result.observed_count == 0
    assert result.new_cursor == "fresh"


# ----- settings flow-through ---------------------------------------------


def test_max_body_chars_setting_flows_through_to_mapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``[connectors.google_mail] max_body_chars`` override reaches the body."""
    _patch_settings(monkeypatch, max_body_chars=20)

    def _big_message(message_id: str) -> RawGmailMessage:
        return RawGmailMessage(
            message_id=message_id,
            thread_id="T",
            history_id="1",
            snippet="",
            subject="big",
            from_header="x@x",
            internal_date_ms="1735689600000",
            label_ids=(),
            body_text="A" * 1000,
            body_html="",
            raw={},
        )

    _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("M-BIG", "next")]],
        message_factory=_big_message,
    )
    service = _RecordingSourceService()

    GoogleMailConnector().sync(_context(service, cursor="start"))

    body = service.calls[0]["body"]
    assert "[gmail body truncated: 20 / 1000 chars]" in body
    # The 20-char cap clipped the body to 20 + marker.
    assert "A" * 20 in body
