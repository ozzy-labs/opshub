"""Tests for :class:`opshub.connectors.google_workspace.connector.GoogleWorkspaceConnector`.

Coverage map:

* ``name`` constant + registry side-effect (import-time registration)
* First-sync bootstrap path: ``cursor_value=None`` →
  ``getStartPageToken`` → walks any pending changes
* Resume path: stored cursor is replayed verbatim into ``changes.list``
* Cursor advances to the freshly-returned ``newStartPageToken`` on the
  final page (so the next sync resumes there)
* :class:`PageTokenExpiredError` triggers a re-bootstrap (ADR-0010
  §Phase 13 改訂 (g) TTL fallback)
* Excluded items advance the cursor but never reach ``observe``
* ``ConfigError`` early-return when ``client_id`` / ``client_secret``
  is unset

The Drive HTTP layer is mocked at the :class:`DriveClient` boundary so
this file does not need to re-build the ``httpx.MockTransport`` setup
the client tests already cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Workspace connector tests require the 'connectors-google-workspace' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.google_workspace.client import (
    PageTokenExpiredError,
    RawDriveItem,
)
from opshub.connectors.google_workspace.connector import GoogleWorkspaceConnector
from opshub.connectors.google_workspace.cursor import CURSOR_CHANGES
from opshub.core.errors import ConfigError

# ----- doubles -----------------------------------------------------------


class _RecordingSourceService:
    """Test double for :class:`SourceService`.

    Same shape as the MS365 connector test double — implements the
    Phase 10 ``observe`` keyword set + cursor surface. A drift on
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
    content_extraction: bool = False,
) -> None:
    """Stub :class:`OpsHubSettings` for the connector's lazy import."""
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = client_id
    fake_settings.connectors.google_workspace.client_secret = client_secret
    fake_settings.connectors.google_workspace.redirect_uri = redirect_uri
    fake_settings.connectors.google_workspace.content_extraction = content_extraction
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)


def _patch_excludes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str = "") -> None:
    """Write an ``excludes.yaml`` and point :func:`default_config_dir` at it."""
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _raw(file_id: str, *, name: str | None = None) -> RawDriveItem:
    return RawDriveItem(
        file_id=file_id,
        removed=False,
        trashed=False,
        name=name or f"Doc-{file_id}",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owner_email="alice@example.com",
        owner_display_name="Alice",
        is_shared_with_me=False,
        drive_id="",
        raw={},
    )


def _patch_auth_and_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_page_token: str = "T0",
    pages: list[list[tuple[RawDriveItem, str]]] | None = None,
    raise_expired_first: bool = False,
) -> MagicMock:
    """Stub :class:`GoogleWorkspaceAuth` + :class:`DriveClient`.

    ``pages`` is a list of "yield this list of ``(item, cursor)``
    tuples per call to ``fetch_changes``". When ``raise_expired_first``
    is set the first ``fetch_changes`` call raises
    :class:`PageTokenExpiredError` to exercise the TTL fallback.

    Returns the patched ``DriveClient`` instance so tests can assert
    against its method calls.
    """
    monkeypatch.setattr(
        "opshub.connectors.google_workspace.auth.GoogleWorkspaceAuth",
        MagicMock(),
    )
    fake_client = MagicMock()
    fake_client.get_start_page_token.return_value = start_page_token

    iter_queue: list[list[tuple[RawDriveItem, str]]] = list(pages or [])
    expired_pending = {"flag": raise_expired_first}

    def fetch_changes(*, page_token: str) -> Iterator[tuple[RawDriveItem, str]]:
        if expired_pending["flag"]:
            expired_pending["flag"] = False
            raise PageTokenExpiredError
        if not iter_queue:
            return iter([])
        return iter(iter_queue.pop(0))

    fake_client.fetch_changes.side_effect = fetch_changes
    monkeypatch.setattr(
        "opshub.connectors.google_workspace.client.DriveClient",
        MagicMock(return_value=fake_client),
    )
    return fake_client


# ----- contract pin ------------------------------------------------------


def test_connector_name_pin() -> None:
    """Connector name is the registry key — pin it explicitly."""
    assert GoogleWorkspaceConnector.name == "google_workspace"
    assert GoogleWorkspaceConnector().name == "google_workspace"


def test_import_registers_connector() -> None:
    """Importing the package registers the connector with the registry.

    Other tests in the suite occasionally call ``unregister_all`` to
    isolate state; we re-register **only** when the slot is empty so
    we do not trip the registry's "different instance under existing
    name" guard (a per-import side-effect already registered the
    canonical instance, and re-registering a fresh one would raise).
    """
    from opshub.connectors import discover_connectors
    from opshub.connectors._registry import register_connector

    if "google_workspace" not in {c.name for c in discover_connectors()}:
        register_connector(GoogleWorkspaceConnector())

    names = [c.name for c in discover_connectors()]
    assert "google_workspace" in names


# ----- early-return guards -----------------------------------------------


def test_sync_requires_client_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_settings(monkeypatch, client_id="")
    _patch_excludes(monkeypatch, tmp_path)
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_id"):
        GoogleWorkspaceConnector().sync(_context(service))


def test_sync_requires_client_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_settings(monkeypatch, client_secret="")
    _patch_excludes(monkeypatch, tmp_path)
    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="client_secret"):
        GoogleWorkspaceConnector().sync(_context(service))


# ----- first-sync bootstrap ----------------------------------------------


def test_sync_first_run_bootstraps_start_page_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cursor_value=None`` triggers ``getStartPageToken`` and persists it.

    The eager cursor commit guards against the next-sync race where a
    crash mid-bootstrap would re-issue a new token and silently lose
    any changes between the two bootstraps.
    """
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        start_page_token="boot-T",
        pages=[[(_raw("F1"), "boot-T"), (_raw("F2"), "final-T")]],
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service))

    fake_client.get_start_page_token.assert_called_once()
    # The bootstrap cursor was persisted before the iterator ran.
    assert (CURSOR_CHANGES, "boot-T", False) in service.cursor_history
    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["F1", "F2"]
    # New cursor advanced to the final-page token.
    assert result.new_cursor == "final-T"


def test_sync_resume_replays_stored_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stored cursor is forwarded verbatim into ``fetch_changes``."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T")]],
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.get_start_page_token.assert_not_called()
    fake_client.fetch_changes.assert_called_with(page_token="stored-T")
    assert result.observed_count == 1
    assert result.new_cursor == "next-T"


# ----- TTL fallback ------------------------------------------------------


def test_sync_falls_back_on_page_token_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expired cursor → fresh ``getStartPageToken`` → resume.

    ADR-0010 §Phase 13 改訂 (g) TTL fallback pin. The fallback
    re-emits items the projection has already seen; the projection
    dedup absorbs that safely.
    """
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        start_page_token="fresh-T",
        pages=[[(_raw("F-after-fallback"), "post-fallback-T")]],
        raise_expired_first=True,
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="expired-T"))

    # First fetch raised; second fetch ran on the fresh token.
    assert fake_client.get_start_page_token.called
    assert fake_client.fetch_changes.call_count == 2
    second_call = fake_client.fetch_changes.call_args_list[1]
    assert second_call.kwargs["page_token"] == "fresh-T"
    assert result.observed_count == 1
    assert result.new_cursor == "post-fallback-T"


# ----- excludes ----------------------------------------------------------


def test_sync_excluded_items_advance_cursor_but_are_not_observed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An excluded item still advances the cursor (ADR-0020 §(b))."""
    _patch_settings(monkeypatch)
    _patch_excludes(
        monkeypatch,
        tmp_path,
        body="senders:\n  - bob@example.com\n",
    )
    excluded = RawDriveItem(
        file_id="F-exc",
        removed=False,
        trashed=False,
        name="Excluded",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link="",
        owner_email="bob@example.com",
        owner_display_name="Bob",
        is_shared_with_me=False,
        drive_id="",
        raw={},
    )
    _patch_auth_and_client(
        monkeypatch,
        pages=[[(excluded, "cursor-after"), (_raw("F-keep"), "final-T")]],
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    # Only the non-excluded item was observed.
    assert [c["external_id"] for c in service.calls] == ["F-keep"]
    # Cursor still advanced past the excluded item.
    assert result.new_cursor == "final-T"
    assert result.observed_count == 1
