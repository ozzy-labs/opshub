"""Tests for :class:`opshub.connectors.ms365.connector.MS365Connector`.

Focused on the Phase 10 audit Cluster 3 §A2 contract: the shared
``excludes.yaml`` ``senders`` selector (Calendar organiser / Outlook
``sender``) and ``paths`` selector (OneDrive item path) cause the
connector to skip the matched item before its body reaches the source
service. The matched item's cursor still advances so the connector
does not re-scan it forever (mirrors the slack-channel-exclude
contract).

Each test stubs the per-endpoint fetcher iterator and a stand-in
``SourceService`` double so the suite never reaches Microsoft Graph
or the keyring. The ``[connectors-ms365]`` extras are gated with
:func:`pytest.importorskip` (mirrors the rest of the MS365 test files)
so the file is skipped cleanly on a slim install.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="MS365 connector tests share the [connectors-ms365] extras with the fetcher",
)
pytest.importorskip(
    "msal",
    reason="MS365 connector tests share the [connectors-ms365] extras with the auth helper",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.ms365.connector import MS365Connector
from opshub.connectors.ms365.fetcher import (
    CURSOR_CALENDAR,
    CURSOR_ONEDRIVE,
    CURSOR_OUTLOOK,
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService`.

    Implements the Phase 10 ``observe`` keyword set (``body`` /
    ``provenance_origin`` / ``provenance_trust``) plus the per-endpoint
    cursor surface the MS365 connector uses (``cursor_get`` /
    ``cursor_set`` / ``record_sync_failure``). A drift on argument names
    trips :class:`TypeError` immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._cursors: dict[str, str | None] = {}
        self.cursor_history: list[tuple[str, str | None, bool]] = []
        self.failures: list[tuple[str, str]] = []

    def cursor_get(self, key: str) -> str | None:
        return self._cursors.get(key)

    def cursor_set(self, key: str, value: str | None, *, sync_started: bool = False) -> None:
        self._cursors[key] = value
        self.cursor_history.append((key, value, sync_started))

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        self.failures.append((name, error_message))

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
        author_handle: str | None = None,
        author_display: str | None = None,
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
                "author_handle": author_handle,
                "author_display": author_display,
            }
        )


def _context(service: _RecordingSourceService) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=MagicMock(),
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calendar_enabled: bool = True,
    onedrive_enabled: bool = True,
    outlook_enabled: bool = True,
) -> None:
    """Stub :class:`OpsHubSettings` so the connector reaches the fetchers.

    Mirrors the slack test pattern: a MagicMock ``OpsHubSettings`` whose
    ``connectors.ms365`` exposes the per-endpoint flags + a non-empty
    ``client_id`` (so the early-return ConfigError guard does not fire).
    """
    fake_settings = MagicMock()
    fake_settings.connectors.ms365.client_id = "fake-client-id"
    fake_settings.connectors.ms365.authority = "https://login.microsoftonline.com/common"
    fake_settings.connectors.ms365.calendar_enabled = calendar_enabled
    fake_settings.connectors.ms365.onedrive_enabled = onedrive_enabled
    fake_settings.connectors.ms365.outlook_enabled = outlook_enabled
    monkeypatch.setattr(
        "opshub.core.config.OpsHubSettings",
        lambda: fake_settings,
    )


def _patch_auth_and_fetcher(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch :class:`MS365Auth` + :class:`MS365Fetcher` to no-op constructors.

    Returns the fetcher instance MagicMock so the test can stub its
    per-endpoint iterator methods. The connector instantiates both
    classes lazily inside :meth:`sync`; we lazy-import them in the
    helper so the patch targets resolve through the *same* import
    statement the connector uses.
    """
    monkeypatch.setattr("opshub.connectors.ms365.auth.MS365Auth", MagicMock())
    fake_fetcher = MagicMock()
    fake_fetcher_cls = MagicMock(return_value=fake_fetcher)
    monkeypatch.setattr("opshub.connectors.ms365.fetcher.MS365Fetcher", fake_fetcher_cls)
    return fake_fetcher


def _patch_excludes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str) -> None:
    """Write an ``excludes.yaml`` and point :func:`default_config_dir` at it.

    Same pattern as the slack / github / box_drive excludes tests:
    redirect the *module-level* import of :func:`default_config_dir`
    inside :mod:`opshub.core.excludes` so :func:`load_excludes` (called
    without arguments by the connector) resolves to the test's tmp dir.
    The no-arg call is mandatory — the ``OpsHubSettings.config_dir``
    threading path would yield a :class:`MagicMock`-typed value that
    sends :func:`yaml.safe_load` into an infinite loop (audit Cluster 3
    rationale on the source side).
    """
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _calendar(
    *,
    event_id: str = "evt-1",
    subject: str = "team sync",
    organizer_email: str | None = None,
) -> RawCalendarEvent:
    raw: dict[str, Any] = {"id": event_id}
    if organizer_email is not None:
        raw["organizer"] = {"emailAddress": {"address": organizer_email}}
    return RawCalendarEvent(
        id=event_id,
        subject=subject,
        start_iso="2026-05-17T09:00:00Z",
        end_iso="2026-05-17T10:00:00Z",
        attendees_count=2,
        web_link="https://outlook.office.com/calendar/abc",
        last_modified_iso="2026-05-17T08:30:00Z",
        raw=raw,
    )


def _outlook(
    *,
    message_id: str = "msg-1",
    sender: str = "alice@example.com",
) -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject="hello",
        body_preview="preview",
        sender=sender,
        received_iso="2026-05-17T08:00:00Z",
        web_link="https://outlook.office.com/mail/abc",
        raw={"id": message_id},
    )


def _onedrive(
    *, item_id: str = "file-1", path: str = "/drive/root:/Projects/x.md"
) -> RawOneDriveItem:
    return RawOneDriveItem(
        id=item_id,
        name="x.md",
        path=path,
        web_url="https://onedrive.live.com/?id=abc",
        last_modified_iso="2026-05-15T12:00:00Z",
        raw={"id": item_id},
    )


# ---------------------------------------------------------------------- name


def test_connector_name_is_ms365() -> None:
    """The registry / CLI dispatch key must be exactly ``"ms365"``."""
    assert MS365Connector.name == "ms365"
    assert MS365Connector().name == "ms365"


# ---------------------------------------------------------------------- excludes


def test_sync_skips_excluded_outlook_sender_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): an Outlook message from an excluded sender is dropped.

    The per-endpoint cursor still advances past the dropped message
    (mirrors the slack channel-exclude contract — skip but advance so
    we don't re-scan forever).
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - bot@example.com\n")
    _patch_settings(monkeypatch, calendar_enabled=False, onedrive_enabled=False)
    fetcher = _patch_auth_and_fetcher(monkeypatch)

    bot_msg = _outlook(message_id="msg-bot", sender="bot@example.com")
    human_msg = _outlook(message_id="msg-human", sender="alice@example.com")

    def _outlook_iter(*, since_iso: str | None) -> Iterator[tuple[RawOutlookMessage, str]]:
        del since_iso
        yield bot_msg, "2026-05-17T08:00:00Z"
        yield human_msg, "2026-05-17T09:00:00Z"

    fetcher.fetch_outlook_messages.side_effect = _outlook_iter

    service = _RecordingSourceService()
    MS365Connector().sync(_context(service))

    # Only the human's message reached the service; the bot was filtered.
    assert [c["external_id"] for c in service.calls] == ["msg-human"]
    # The persisted cursor advanced to the latest seen value across
    # both observed and skipped items.
    final_cursor = service.cursor_history[-1]
    assert final_cursor[0] == CURSOR_OUTLOOK
    assert final_cursor[1] == "2026-05-17T09:00:00Z"
    assert final_cursor[2] is False  # the closing sync_started=False bracket


def test_sync_skips_excluded_calendar_organizer_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a calendar event organised by an excluded sender is dropped.

    The organiser email is nested at
    ``raw.organizer.emailAddress.address`` on the Graph payload; the
    connector lifts it from the preserved ``raw`` dict.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - bot@example.com\n")
    _patch_settings(monkeypatch, onedrive_enabled=False, outlook_enabled=False)
    fetcher = _patch_auth_and_fetcher(monkeypatch)

    bot_evt = _calendar(event_id="evt-bot", organizer_email="bot@example.com")
    human_evt = _calendar(event_id="evt-human", organizer_email="alice@example.com")

    def _calendar_iter(*, since_iso: str | None) -> Iterator[tuple[RawCalendarEvent, str]]:
        del since_iso
        yield bot_evt, "2026-05-17T08:30:00Z"
        yield human_evt, "2026-05-17T09:30:00Z"

    fetcher.fetch_calendar_events.side_effect = _calendar_iter

    service = _RecordingSourceService()
    MS365Connector().sync(_context(service))

    assert [c["external_id"] for c in service.calls] == ["evt-human"]
    final_cursor = service.cursor_history[-1]
    assert final_cursor[0] == CURSOR_CALENDAR
    assert final_cursor[1] == "2026-05-17T09:30:00Z"


def test_sync_skips_excluded_onedrive_path_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a OneDrive item under an excluded path glob is dropped.

    Mirrors the ``box_drive`` path-glob semantics so a single shared
    ``paths`` rule covers both filesystem-scanning and Graph-API
    connectors uniformly.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="paths:\n  - '**/secrets/**'\n")
    _patch_settings(monkeypatch, calendar_enabled=False, outlook_enabled=False)
    fetcher = _patch_auth_and_fetcher(monkeypatch)

    secret_item = _onedrive(item_id="file-secret", path="/drive/root:/secrets/api-key.txt")
    safe_item = _onedrive(item_id="file-safe", path="/drive/root:/Projects/report.md")

    def _onedrive_iter(*, delta_link: str | None) -> Iterator[tuple[RawOneDriveItem, str]]:
        del delta_link
        yield secret_item, "delta-after-secret"
        yield safe_item, "delta-after-safe"

    fetcher.fetch_onedrive_changes.side_effect = _onedrive_iter

    service = _RecordingSourceService()
    MS365Connector().sync(_context(service))

    assert [c["external_id"] for c in service.calls] == ["file-safe"]
    final_cursor = service.cursor_history[-1]
    assert final_cursor[0] == CURSOR_ONEDRIVE
    assert final_cursor[1] == "delta-after-safe"
