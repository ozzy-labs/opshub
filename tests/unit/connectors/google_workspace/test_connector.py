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
    fallback_window_days: int = 30,
    max_file_size_mb: int = 50,
    max_chars: int = 500_000,
    max_cells_per_sheet: int = 10_000,
    max_cells_per_workbook: int = 50_000,
) -> None:
    """Stub :class:`OpsHubSettings` for the connector's lazy import.

    Sets real numeric values on ``settings.office`` so the connector's
    ``cfg.max_file_size_mb * 1024 * 1024`` arithmetic produces an
    ``int`` rather than a ``MagicMock``. Defaults match
    :class:`OfficeSettings` so tests that do not override them see
    the production cap surface.
    """
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = client_id
    fake_settings.connectors.google_workspace.client_secret = client_secret
    fake_settings.connectors.google_workspace.redirect_uri = redirect_uri
    fake_settings.connectors.google_workspace.content_extraction = content_extraction
    fake_settings.connectors.google_workspace.fallback_window_days = fallback_window_days
    fake_settings.office.max_file_size_mb = max_file_size_mb
    fake_settings.office.max_chars = max_chars
    fake_settings.office.excel.max_cells_per_sheet = max_cells_per_sheet
    fake_settings.office.excel.max_cells_per_workbook = max_cells_per_workbook
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)


def _patch_excludes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str = "") -> None:
    """Write an ``excludes.yaml`` and point :func:`default_config_dir` at it."""
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _raw(
    file_id: str,
    *,
    name: str | None = None,
    mime_type: str = "application/vnd.google-apps.document",
    owner_email: str = "alice@example.com",
) -> RawDriveItem:
    return RawDriveItem(
        file_id=file_id,
        removed=False,
        trashed=False,
        name=name or f"Doc-{file_id}",
        mime_type=mime_type,
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owner_email=owner_email,
        owner_display_name="Alice",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )


def _patch_auth_and_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_page_token: str = "T0",
    pages: list[list[tuple[RawDriveItem, str]]] | None = None,
    raise_expired_first: bool = False,
    fallback_files: list[RawDriveItem] | None = None,
) -> MagicMock:
    """Stub :class:`GoogleWorkspaceAuth` + :class:`DriveClient`.

    ``pages`` is a list of "yield this list of ``(item, cursor)``
    tuples per call to ``fetch_changes``". When ``raise_expired_first``
    is set the first ``fetch_changes`` call raises
    :class:`PageTokenExpiredError` to exercise the TTL fallback.

    ``fallback_files`` is the list of items the TTL fallback's
    :meth:`DriveClient.list_files_modified_since` yields. Defaults to
    an empty list so existing tests do not need to opt in to the
    fallback shape.

    Returns the patched ``DriveClient`` instance so tests can assert
    against its method calls.
    """
    # Phase 14 G2 (#294): the shared OAuth helper moved to
    # ``opshub.connectors.google_auth.auth``; the connector resolves the
    # ``GoogleWorkspaceAuth`` name lazily inside ``sync()`` from this new
    # path, so the monkeypatch target tracks that source binding.
    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
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

    fallback_queue: list[RawDriveItem] = list(fallback_files or [])

    def list_files_modified_since(*, since: str) -> Iterator[RawDriveItem]:
        del since
        return iter(fallback_queue)

    fake_client.list_files_modified_since.side_effect = list_files_modified_since
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


def test_sync_falls_back_full_pass_on_page_token_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expired cursor → WARNING log → full-pass emit → cursor update.

    ADR-0010 §Phase 13 改訂 (g) 3-step TTL fallback pin (Phase 13
    audit cluster A, issue #286). Pre-#286 implementation only
    bootstrapped a fresh root token without backfilling the TTL gap,
    silently losing every change between the last successful sync and
    the recovery. This test pins the corrected 3-step round-trip:

    1. ``list_files_modified_since`` is invoked over the configured
       window and each surviving file is re-emitted as
       :class:`SourceObserved`.
    2. ``get_start_page_token`` bootstraps a fresh root for the next
       sync.
    3. The next sync resumes on the delta path from the fresh token.

    The projection's natural-key dedup on
    ``(connector_name, external_id)`` absorbs the steady-state
    overlap, so emitting the fallback set is safe.
    """
    _patch_settings(monkeypatch, fallback_window_days=30)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        start_page_token="fresh-T",
        # After the fallback bootstraps the fresh token the connector
        # immediately resumes a delta walk; the second ``fetch_changes``
        # call returns no further changes (the steady-state corpus is
        # caught up).
        pages=[[]],
        raise_expired_first=True,
        fallback_files=[_raw("F-from-fallback")],
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="expired-T"))

    # Step 1: ``fetch_changes`` was attempted with the expired cursor.
    assert fake_client.fetch_changes.call_args_list[0].kwargs["page_token"] == "expired-T"

    # Step 2: ``list_files_modified_since`` ran for the TTL window.
    assert fake_client.list_files_modified_since.call_count == 1
    fallback_kwargs = fake_client.list_files_modified_since.call_args.kwargs
    assert "since" in fallback_kwargs
    assert fallback_kwargs["since"].endswith("Z")  # ISO 8601 UTC

    # Step 3: bootstrap a fresh root token AFTER the full-pass.
    assert fake_client.get_start_page_token.call_count == 1

    # Fallback file emitted as SourceObserved.
    assert [c["external_id"] for c in service.calls] == ["F-from-fallback"]
    assert result.observed_count == 1

    # Cursor advanced to the freshly-bootstrapped root token (no
    # post-fallback delta pages in this fixture).
    assert result.new_cursor == "fresh-T"


def test_sync_fallback_emits_warning_log_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The TTL fallback emits ``connector.changes_list.expired`` WARNING.

    Teams ``connector.delta.expired`` 同型 (Phase 11 ADR-0010 §改訂 (c)
    observability pin); Phase 13 cluster A audit (#286) brought the
    Google Workspace shape into structural symmetry so dashboards can
    fan out on either connector with one rule.
    """
    from unittest.mock import patch as _patch_obj

    _patch_settings(monkeypatch, fallback_window_days=30)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        start_page_token="fresh-T",
        pages=[[]],
        raise_expired_first=True,
        fallback_files=[],
    )
    service = _RecordingSourceService()

    captured_logger = MagicMock()
    captured_logger.warning = MagicMock()
    with _patch_obj(
        "opshub.core.logging.get_logger",
        return_value=captured_logger,
    ):
        GoogleWorkspaceConnector().sync(_context(service, cursor="expired-T"))

    # WARNING log fired exactly once on the TTL-fallback path with the
    # Teams 同型 keys (event=connector.changes_list.expired, connector,
    # since, window_days).
    matching_calls = [
        call
        for call in captured_logger.warning.call_args_list
        if call.args and call.args[0] == "connector.changes_list.expired"
    ]
    assert len(matching_calls) == 1, captured_logger.warning.call_args_list
    kwargs = matching_calls[0].kwargs
    assert kwargs["connector"] == "google_workspace"
    assert kwargs["window_days"] == 30
    assert "since" in kwargs


def test_sync_fallback_list_files_failure_bubbles_to_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``list_files_modified_since`` failure aborts the sync.

    ADR-0010 §責務 4 fail-fast: if the recovery path itself fails the
    connector surfaces :class:`ConnectorFailedError` so the CLI driver
    appends a :class:`ConnectorSyncFailed` event. Squashing the failure
    silently would leave the connector stuck on the expired token.
    """
    from opshub.core.errors import ConnectorFailedError

    _patch_settings(monkeypatch, fallback_window_days=30)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        start_page_token="fresh-T",
        pages=[],
        raise_expired_first=True,
    )

    def _explode(*, since: str) -> Iterator[RawDriveItem]:
        del since
        raise ConnectorFailedError("fallback files.list failed")

    fake_client.list_files_modified_since.side_effect = _explode
    service = _RecordingSourceService()

    with pytest.raises(ConnectorFailedError, match=r"fallback files\.list failed"):
        GoogleWorkspaceConnector().sync(_context(service, cursor="expired-T"))

    # The fresh root token was NOT acquired — the recovery failed
    # before it could.
    fake_client.get_start_page_token.assert_not_called()


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
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
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


# ----- G4 content_extraction wiring (#278) -------------------------------


def test_sync_content_extraction_off_keeps_body_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``content_extraction = false`` (default) → body stays ``None``.

    Phase 13 G3 metadata-only invariant preserved bit-for-bit when
    the operator does not opt in. The connector MUST NOT call
    ``files.export`` on the default-off path — that would be a
    silent change in network surface for upgrading operators.
    """
    _patch_settings(monkeypatch, content_extraction=False)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T")]],
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    assert [c["body"] for c in service.calls] == [None]
    fake_client.export_file.assert_not_called()


def test_sync_content_extraction_on_calls_export_and_threads_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``content_extraction = true`` routes Google Docs through ``files.export`` + extract.

    Verifies the full G4 wiring round-trip:

    * ``files.export(fileId, mimeType=<docx mediatype>)`` is invoked.
    * The exported bytes flow into ``extract_workspace_export``.
    * The extractor's body lands on the resulting
      :class:`SourceObserved`.
    """
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T")]],
    )
    fake_client.export_file.return_value = b"fake-docx-bytes"

    from opshub.core.document_extract import ExtractResult

    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        MagicMock(
            return_value=ExtractResult(
                body="# extracted body",
                truncated=False,
                skip_reason=None,
                source_type="google_doc",
            )
        ),
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.export_file.assert_called_once_with(
        file_id="F1",
        mime_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert [c["body"] for c in service.calls] == ["# extracted body"]


def test_sync_content_extraction_routes_sheets_through_xlsx_mediatype(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Google Sheets → ``files.export(mimeType=<xlsx>)`` per ADR-0025 §決定 (j) Table 1."""
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[
            [
                (
                    _raw("S1", mime_type="application/vnd.google-apps.spreadsheet"),
                    "next-T",
                )
            ]
        ],
    )
    fake_client.export_file.return_value = b"fake-xlsx-bytes"

    from opshub.core.document_extract import ExtractResult

    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        MagicMock(
            return_value=ExtractResult(
                body="| col |",
                truncated=False,
                skip_reason=None,
                source_type="google_sheets",
            )
        ),
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.export_file.assert_called_once_with(
        file_id="S1",
        mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    )


def test_sync_content_extraction_routes_slides_through_pptx_mediatype(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Google Slides → ``files.export(mimeType=<pptx>)`` per ADR-0025 §決定 (j) Table 1."""
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[
            [
                (
                    _raw("P1", mime_type="application/vnd.google-apps.presentation"),
                    "next-T",
                )
            ]
        ],
    )
    fake_client.export_file.return_value = b"fake-pptx-bytes"

    from opshub.core.document_extract import ExtractResult

    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        MagicMock(
            return_value=ExtractResult(
                body="# slide 1",
                truncated=False,
                skip_reason=None,
                source_type="google_slides",
            )
        ),
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.export_file.assert_called_once_with(
        file_id="P1",
        mime_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )


def test_sync_content_extraction_skips_non_workspace_mimetypes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-native types (PDFs, uploads, folders) skip ``files.export`` entirely.

    Drive returns 403 ``fileNotExportable`` for these; the connector
    short-circuits on the canonical mimeType lookup before any HTTP
    round-trip, mirroring the box_drive scanner's "non-Office
    extension → skip" branch.
    """
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("PDF1", mime_type="application/pdf"), "next-T")]],
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.export_file.assert_not_called()
    assert [c["body"] for c in service.calls] == [None]
    # The source is still observed (ADR-0020 retain-everything).
    assert [c["external_id"] for c in service.calls] == ["PDF1"]


def test_sync_content_extraction_export_failure_falls_back_to_metadata_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed ``files.export`` collapses to ``body=None`` rather than aborting the sync.

    ADR-0025 §決定 (c) fail-safe contract: a single broken export
    must not block the rest of the sync. The connector logs the
    failure and continues; the projection still receives the metadata
    via the normal :class:`SourceObserved` path.
    """
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T"), (_raw("F2"), "final-T")]],
    )

    from opshub.core.errors import ConnectorFailedError

    fake_client.export_file.side_effect = [
        ConnectorFailedError("export failed for F1"),
        b"fake-docx-bytes",
    ]

    from opshub.core.document_extract import ExtractResult

    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        MagicMock(
            return_value=ExtractResult(
                body="# F2 body",
                truncated=False,
                skip_reason=None,
                source_type="google_doc",
            )
        ),
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["F1", "F2"]
    # F1 fell back to ``body=None``; F2 carried the extracted body.
    assert [c["body"] for c in service.calls] == [None, "# F2 body"]


def test_sync_content_extraction_skips_removed_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Removed (permanent-delete) items never reach ``files.export``.

    Drive cannot export a deleted file; the metadata-only path is the
    right shape for the ``[removed: <fileId>]`` projection row
    (ADR-0020 retain-everything via metadata).
    """
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    removed_item = RawDriveItem(
        file_id="DEL1",
        removed=True,
        trashed=False,
        name="",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link="",
        owner_email="",
        owner_display_name="",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(removed_item, "next-T")]],
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_client.export_file.assert_not_called()
    assert [c["body"] for c in service.calls] == [None]


def test_sync_content_extraction_skip_reason_does_not_block_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``extract_workspace_export`` returns ``skip_reason`` (size cap, corrupt),
    the connector keeps the event with ``body=None``.

    The mapper still emits :class:`SourceObserved` so the projection
    row + metadata are retained (ADR-0025 §決定 (c) fail-safe +
    ADR-0020 retain-everything).
    """
    _patch_settings(monkeypatch, content_extraction=True)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("BIG"), "next-T")]],
    )
    fake_client.export_file.return_value = b"x" * 200

    from opshub.core.document_extract import ExtractResult

    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        MagicMock(
            return_value=ExtractResult(
                body=None,
                truncated=False,
                skip_reason="file too large",
                source_type="google_doc",
            )
        ),
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    assert result.observed_count == 1
    assert [c["body"] for c in service.calls] == [None]


def test_sync_content_extraction_propagates_office_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``[office]`` overrides reach :func:`extract_workspace_export` as kwargs.

    Phase 11 audit Cluster B two-key composition (ADR-0025 §決定
    (g)): a single operator override governs Box Drive / OneDrive /
    Google Workspace bodies in lockstep. The pin asserts the
    cap arithmetic (MB → bytes) is correct and every cap is
    forwarded — silent drift here would defeat the unified-knob
    promise.
    """
    _patch_settings(
        monkeypatch,
        content_extraction=True,
        max_file_size_mb=100,
        max_chars=1_000_000,
        max_cells_per_sheet=20_000,
        max_cells_per_workbook=80_000,
    )
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T")]],
    )
    fake_client.export_file.return_value = b"fake-docx-bytes"

    from opshub.core.document_extract import ExtractResult

    fake_extract = MagicMock(
        return_value=ExtractResult(
            body="# body",
            truncated=False,
            skip_reason=None,
            source_type="google_doc",
        )
    )
    monkeypatch.setattr(
        "opshub.core.document_extract.extract_workspace_export",
        fake_extract,
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    fake_extract.assert_called_once()
    kwargs = fake_extract.call_args.kwargs
    assert kwargs["max_file_bytes"] == 100 * 1024 * 1024
    assert kwargs["max_chars"] == 1_000_000
    assert kwargs["max_cells_per_sheet"] == 20_000
    assert kwargs["max_cells_per_workbook"] == 80_000


# ----- Phase 13 audit Cluster C — C#21 lifecycle edge pin ----------------


def test_sync_handles_removed_and_trashed_simultaneously(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An item with both ``removed=True`` and ``trashed=True`` round-trips cleanly.

    Drive surfaces this shape when a file the operator had trashed
    (``trashed=true``) is then permanently deleted (``removed=true``).
    Both flags travel on the same change record because Drive's
    ``changes.list`` collapses the trash → permanent-delete transition
    into a single delta. ``client._normalise_change`` honours both
    booleans verbatim, and the mapper's marker chain picks
    ``[removed]`` (the more specific lifecycle state) per
    :func:`mapper._build_summary` — pinning the connector-level
    behaviour here guarantees the projection row stays valid
    end-to-end (no Pydantic crash, the summary marker is the
    operator-facing cue, and the cursor advances normally so the
    next sync resumes past this change). Issue #288 Cluster C C#21.
    """
    _patch_settings(monkeypatch, content_extraction=False)
    _patch_excludes(monkeypatch, tmp_path)

    # Construct the dual-flag shape directly — both ``removed`` and
    # ``trashed`` set, ``name`` empty (Drive strips the metadata on
    # permanent-delete). The mapper's removed-placeholder branch
    # synthesises the title.
    dual = RawDriveItem(
        file_id="F-dual",
        removed=True,
        trashed=True,
        name="",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link="",
        owner_email="",
        owner_display_name="",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )
    _patch_auth_and_client(
        monkeypatch,
        pages=[[(dual, "next-T")]],
    )
    service = _RecordingSourceService()

    result = GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    # Projection row emitted (ADR-0020 retain-everything: even
    # permanently-deleted items keep the row so downstream consumers
    # can answer "did this file ever exist?").
    assert result.observed_count == 1
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["external_id"] == "F-dual"
    # ``removed`` is more specific than ``trashed`` so the title
    # carries the removed-placeholder and the summary stamps
    # ``[removed]`` (not ``[trashed]``) — same precedence rule the
    # mapper's marker chain enforces in unit tests.
    assert call["title"] == "[removed: F-dual]"
    assert call["summary"] is not None
    assert "[removed]" in call["summary"]
    assert "[trashed]" not in call["summary"]
    # Cursor still advanced past the dual-flag change so the next
    # sync resumes correctly (no replay).
    assert result.new_cursor == "next-T"


def test_sync_existing_metadata_unchanged_when_content_extraction_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default-off path: every other observe-call kwarg matches the G3 shape.

    Regression guard for upgrading operators — flipping G4 on / off
    must only toggle ``body`` (and not silently change url / summary
    / provenance shape).
    """
    _patch_settings(monkeypatch, content_extraction=False)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        pages=[[(_raw("F1"), "next-T")]],
    )
    service = _RecordingSourceService()

    GoogleWorkspaceConnector().sync(_context(service, cursor="stored-T"))

    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["connector_name"] == "google_workspace"
    assert call["external_id"] == "F1"
    assert call["source_type"] == "google_doc"
    assert call["url"] == "https://drive.google.com/file/d/F1/view"
    assert call["provenance_origin"] == "external"
    assert call["provenance_trust"] == "untrusted"
    assert call["body"] is None
