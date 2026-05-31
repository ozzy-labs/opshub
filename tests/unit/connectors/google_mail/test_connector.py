"""Tests for :class:`opshub.connectors.google_mail.connector.GoogleMailConnector`.

Coverage map:

* ``name`` constant + registry side-effect (import-time registration).
* First-sync bootstrap: ``cursor_value=None`` → backfill + capture
  ``profile.historyId`` → persist as cursor.
* Resume path: stored cursor replayed verbatim into
  ``fetch_history``.
* :class:`HistoryIdExpiredError` triggers the 3-step TTL fallback
  (WARNING log → ``list_messages_since`` full-pass → fresh
  ``historyId`` via ``getProfile``).
* ``fallback_window_days = 0`` skips the full-pass but still
  refreshes the cursor.
* Excluded senders advance the cursor but never reach ``observe``.
* Deleted message (404 on ``get_message``) is skipped with a
  structlog warning rather than failing the sync.
* ``ConfigError`` early-return when the shared OAuth client_id /
  client_secret is unset.

The Gmail HTTP layer is mocked at the :class:`GmailClient` boundary
so this file does not need to re-build the ``httpx.MockTransport``
setup the client tests already cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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
from opshub.core.errors import ConfigError, ConnectorFailedError

# ----- doubles -----------------------------------------------------------


class _RecordingSourceService:
    """Test double for :class:`SourceService` (Gmail variant).

    Same shape as the google_workspace connector test double — drift
    on argument names trips :class:`TypeError` immediately.
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
    initial_window_days: int = 7,
    fallback_window_days: int = 30,
) -> None:
    """Stub :class:`OpsHubSettings` for the connector's lazy import.

    The Gmail connector reads ``client_id`` / ``client_secret`` /
    ``redirect_uri`` from the shared
    :class:`GoogleWorkspaceConnectorSettings` (Phase 14 plan §1 OQ6:
    one Google account = one principal) and ``initial_window_days``
    / ``fallback_window_days`` from its own settings class.
    """
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = client_id
    fake_settings.connectors.google_workspace.client_secret = client_secret
    fake_settings.connectors.google_workspace.redirect_uri = redirect_uri
    fake_settings.connectors.google_mail.initial_window_days = initial_window_days
    fake_settings.connectors.google_mail.fallback_window_days = fallback_window_days
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)


def _patch_excludes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str = "") -> None:
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _raw_message(
    message_id: str,
    *,
    from_header: str = "alice@example.com",
    subject: str = "Sample subject",
    body_text: str = "body",
    label_ids: tuple[str, ...] = ("INBOX",),
    thread_id: str = "t",
) -> RawGmailMessage:
    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        label_ids=label_ids,
        history_id="h",
        internal_date_ms="1735660800000",
        from_header=from_header,
        subject_header=subject,
        snippet="snip",
        body_text=body_text,
        body_html="",
        raw={},
    )


def _patch_auth_and_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile_history_id: str = "PROF-H",
    history_pages: list[list[tuple[str, str]]] | None = None,
    raise_expired_first: bool = False,
    fallback_ids: list[str] | None = None,
    initial_backfill_ids: list[str] | None = None,
    message_payloads: dict[str, RawGmailMessage] | None = None,
    not_found_ids: set[str] | None = None,
) -> MagicMock:
    """Stub :class:`GoogleWorkspaceAuth` + :class:`GmailClient`.

    ``history_pages`` yields tuples of ``(message_id, advanced_history_id)``
    per call to ``fetch_history``. ``raise_expired_first`` makes the
    first call raise :class:`HistoryIdExpiredError`. ``fallback_ids``
    is the list of message ids the TTL fallback's
    :meth:`GmailClient.list_messages_since` yields.
    ``initial_backfill_ids`` is the list for first-sync backfill —
    same method but distinct list so the test can assert against the
    two call paths separately.

    Returns the patched ``GmailClient`` instance so tests can assert
    against its method calls.
    """
    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
        MagicMock(),
    )
    fake_client = MagicMock()
    fake_client.get_profile_history_id.return_value = profile_history_id

    history_queue: list[list[tuple[str, str]]] = list(history_pages or [])
    expired_pending = {"flag": raise_expired_first}

    def fetch_history(*, start_history_id: str) -> Iterator[tuple[str, str]]:
        del start_history_id
        if expired_pending["flag"]:
            expired_pending["flag"] = False
            raise HistoryIdExpiredError
        if not history_queue:
            return iter([])
        return iter(history_queue.pop(0))

    fake_client.fetch_history.side_effect = fetch_history

    # Two separate queues so a test can distinguish backfill vs
    # fallback calls to ``list_messages_since`` (the method is
    # invoked twice when both paths fire in the same sync).
    initial_queue = list(initial_backfill_ids or [])
    fallback_queue = list(fallback_ids or [])
    list_calls = {"n": 0}

    def list_messages_since(*, since_epoch_seconds: int) -> Iterator[str]:
        del since_epoch_seconds
        list_calls["n"] += 1
        if list_calls["n"] == 1 and initial_queue:
            return iter(initial_queue)
        return iter(fallback_queue)

    fake_client.list_messages_since.side_effect = list_messages_since

    payloads: dict[str, RawGmailMessage] = dict(message_payloads or {})
    nf: set[str] = set(not_found_ids or set())

    def get_message(*, message_id: str) -> RawGmailMessage:
        if message_id in nf:
            raise ConnectorFailedError(f"Gmail request returned 404: GET .../messages/{message_id}")
        if message_id in payloads:
            return payloads[message_id]
        return _raw_message(message_id)

    fake_client.get_message.side_effect = get_message
    monkeypatch.setattr(
        "opshub.connectors.google_mail.client.GmailClient",
        MagicMock(return_value=fake_client),
    )
    return fake_client


# ----- contract pin ------------------------------------------------------


def test_connector_name_pin() -> None:
    assert GoogleMailConnector.name == "google_mail"
    assert GoogleMailConnector().name == "google_mail"


def test_import_registers_connector() -> None:
    from opshub.connectors import discover_connectors
    from opshub.connectors._registry import register_connector

    if "google_mail" not in {c.name for c in discover_connectors()}:
        register_connector(GoogleMailConnector())
    names = [c.name for c in discover_connectors()]
    assert "google_mail" in names


# ----- early-return guards ------------------------------------------------


def test_sync_requires_shared_client_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_settings(monkeypatch, client_id="")
    _patch_excludes(monkeypatch, tmp_path)
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_id"):
        GoogleMailConnector().sync(_context(service))


def test_sync_requires_shared_client_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_settings(monkeypatch, client_secret="")
    _patch_excludes(monkeypatch, tmp_path)
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_secret"):
        GoogleMailConnector().sync(_context(service))


# ----- first-sync bootstrap -----------------------------------------------


def test_sync_first_run_backfills_then_bootstraps_history_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cursor_value=None`` → backfill + capture ``profile.historyId``.

    The eager cursor commit after the bootstrap guards against the
    next-sync race where a crash mid-bootstrap would re-issue a new
    id and silently lose any messages between the two bootstraps.
    """
    _patch_settings(monkeypatch, initial_window_days=7)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        profile_history_id="boot-H",
        initial_backfill_ids=["M1", "M2"],
        # After backfill, ``fetch_history`` returns nothing new.
        history_pages=[[]],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service))

    # ``users.messages.list`` was called once during backfill; the
    # bootstrap cursor was persisted right after.
    assert fake_client.list_messages_since.called
    fake_client.get_profile_history_id.assert_called_once()
    assert (CURSOR_HISTORY, "boot-H", False) in service.cursor_history
    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["M1", "M2"]
    # Final cursor is the bootstrapped id (no further history pages).
    assert result.new_cursor == "boot-H"


def test_sync_first_run_initial_window_zero_skips_backfill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``initial_window_days = 0`` skips the backfill entirely.

    The connector still bootstraps a fresh id so subsequent syncs
    use the delta path; only the recent-inbox backfill is omitted.
    """
    _patch_settings(monkeypatch, initial_window_days=0)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        profile_history_id="boot-H",
        # Pre-load some ids that would have been returned IF the
        # backfill had run; we assert the connector ignores them.
        initial_backfill_ids=["IGNORE-1"],
        history_pages=[[]],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service))

    assert result.observed_count == 0
    fake_client.get_profile_history_id.assert_called_once()
    fake_client.list_messages_since.assert_not_called()
    assert result.new_cursor == "boot-H"


# ----- resume path --------------------------------------------------------


def test_sync_resume_replays_stored_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stored cursor is forwarded verbatim into ``fetch_history``."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("M1", "next-H")]],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="stored-H"))

    fake_client.get_profile_history_id.assert_not_called()
    fake_client.fetch_history.assert_called_with(start_history_id="stored-H")
    assert result.observed_count == 1
    assert result.new_cursor == "next-H"


# ----- TTL fallback (Phase 14 改訂 (j)) ----------------------------------


def test_sync_falls_back_full_pass_on_history_id_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expired cursor → WARNING log → full-pass emit → fresh id.

    Pins the ADR-0010 §Phase 14 改訂 (j) 3-step recovery shape:
    structurally identical to Drive §改訂 (g) + Teams ``_fallback_pass``.
    """
    _patch_settings(monkeypatch, fallback_window_days=14)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        profile_history_id="fresh-H",
        raise_expired_first=True,
        fallback_ids=["MA", "MB"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="stale-H"))

    # The full-pass emitted both fallback ids, then ``get_profile``
    # bootstrapped a fresh id and persisted it.
    assert [c["external_id"] for c in service.calls] == ["MA", "MB"]
    assert result.new_cursor == "fresh-H"
    fake_client.get_profile_history_id.assert_called_once()
    assert (CURSOR_HISTORY, "fresh-H", False) in service.cursor_history


def test_sync_fallback_emits_warning_log_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The TTL fallback emits ``connector.history_list.expired`` WARNING.

    Drive ``connector.changes_list.expired`` / Teams
    ``connector.delta.expired`` 同型 (Phase 11 / 13 / 14 unified
    delta-cursor observability pin per ADR-0010 §Phase 14 改訂 (j)).
    Dashboards can fan out on either connector with one rule.
    """
    from unittest.mock import patch as _patch_obj

    _patch_settings(monkeypatch, fallback_window_days=30)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        raise_expired_first=True,
        fallback_ids=[],
    )
    service = _RecordingSourceService()

    captured_logger = MagicMock()
    captured_logger.warning = MagicMock()
    with _patch_obj(
        "opshub.core.logging.get_logger",
        return_value=captured_logger,
    ):
        GoogleMailConnector().sync(_context(service, cursor="stale-H"))

    matching_calls = [
        call
        for call in captured_logger.warning.call_args_list
        if call.args and call.args[0] == "connector.history_list.expired"
    ]
    assert len(matching_calls) == 1, captured_logger.warning.call_args_list
    kwargs = matching_calls[0].kwargs
    assert kwargs["connector"] == "google_mail"
    assert kwargs["window_days"] == 30
    assert "since" in kwargs


def test_sync_fallback_window_zero_skips_full_pass_but_refreshes_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``fallback_window_days = 0`` opts out of the full-pass.

    The connector still calls ``getProfile`` to refresh the cursor so
    subsequent syncs do not re-hit the expired id; the operator
    accepts the loss of TTL-gap messages.
    """
    _patch_settings(monkeypatch, fallback_window_days=0)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        profile_history_id="fresh-H",
        raise_expired_first=True,
        fallback_ids=["WOULD-BE-EMITTED"],
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="stale-H"))

    assert result.observed_count == 0
    assert result.new_cursor == "fresh-H"
    fake_client.list_messages_since.assert_not_called()
    fake_client.get_profile_history_id.assert_called_once()


# ----- excludes + deleted-message handling -------------------------------


def test_excluded_sender_advances_cursor_but_is_not_observed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ingest-excluded sender is dropped before ``observe``.

    Excludes are a Phase 10 ADR-0020 §(b) facility; Gmail uses the
    ``senders`` selector against the bare address extracted from the
    ``From:`` header.
    """
    _patch_settings(monkeypatch)
    _patch_excludes(
        monkeypatch,
        tmp_path,
        body="senders:\n  - bad-sender@example.com\n",
    )
    payloads = {
        "GOOD": _raw_message("GOOD", from_header="alice@example.com"),
        "BAD": _raw_message("BAD", from_header="Bad Person <bad-sender@example.com>"),
    }
    _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("GOOD", "H1"), ("BAD", "H2")]],
        message_payloads=payloads,
    )
    service = _RecordingSourceService()

    result = GoogleMailConnector().sync(_context(service, cursor="start-H"))

    # Only GOOD was observed; BAD was filtered before ``observe``.
    assert [c["external_id"] for c in service.calls] == ["GOOD"]
    # Cursor still advanced through both (advancement is per-iteration,
    # not per-emit).
    assert result.new_cursor == "H2"


def test_deleted_message_404_is_skipped_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 404 on ``get_message`` for a referenced id is non-fatal.

    Gmail's ``users.history.list`` references deleted messages via
    ``messagesDeleted[*]``; ``get_message`` returns 404 for those.
    The connector swallows the 404 with a structlog warning rather
    than failing the whole sync (ADR-0020 retain-everything via the
    last-known projection row).
    """
    from unittest.mock import patch as _patch_obj

    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("OK", "H1"), ("DELETED", "H2")]],
        not_found_ids={"DELETED"},
    )
    service = _RecordingSourceService()

    captured_logger = MagicMock()
    captured_logger.warning = MagicMock()
    with _patch_obj(
        "opshub.core.logging.get_logger",
        return_value=captured_logger,
    ):
        result = GoogleMailConnector().sync(_context(service, cursor="start-H"))

    assert [c["external_id"] for c in service.calls] == ["OK"]
    assert result.new_cursor == "H2"
    not_found_calls = [
        call
        for call in captured_logger.warning.call_args_list
        if call.args and call.args[0] == "connector.message_not_found"
    ]
    assert len(not_found_calls) == 1
    assert not_found_calls[0].kwargs["message_id"] == "DELETED"
    assert not_found_calls[0].kwargs["connector"] == "google_mail"


def test_non_404_get_message_failure_bubbles_to_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-404 ``ConnectorFailedError`` from ``get_message`` re-raises.

    The CLI driver then records a sanitised ``ConnectorSyncFailed``
    event; we should NOT silently swallow 500s / 401s under the
    deleted-message guard.
    """
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        history_pages=[[("BAD", "H1")]],
    )
    fake_client.get_message.side_effect = ConnectorFailedError(
        "Gmail request returned 500: GET .../messages/BAD"
    )
    service = _RecordingSourceService()

    with pytest.raises(ConnectorFailedError, match="500"):
        GoogleMailConnector().sync(_context(service, cursor="start-H"))
