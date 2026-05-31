"""Tests for ``opshub.connectors.google_workspace.client`` (Phase 13 G3).

:class:`DriveClient` is the ``httpx`` wrapper for Drive API v3's
``changes.list`` + ``changes.getStartPageToken`` endpoints. Every
test injects a :class:`httpx.MockTransport` so the suite never
reaches Google (Phase 13 plan §7.5).

Coverage map:

* ``get_start_page_token`` happy path.
* ``fetch_changes`` walks ``nextPageToken`` and yields
  ``newStartPageToken`` on the final page (mirrors MS365 OneDrive
  delta cursor handoff).
* Page-token expiry (404 / 410) raises :class:`PageTokenExpiredError`.
* Rate-limit retry pin: 429 honours ``Retry-After``; 403
  ``userRateLimitExceeded`` is treated as 429.
* 5xx retry pin (transient).
* Persistent failure exhausts the retry budget and raises
  :class:`ConnectorFailedError`.
* Shared Drives parameters are pinned on every ``changes.list`` call
  (OQ10).
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Workspace connector tests require the 'connectors-google-workspace' extras",
)

import httpx

from opshub.connectors.google_workspace.auth import GoogleWorkspaceAuth
from opshub.connectors.google_workspace.client import (
    DRIVE_API_BASE,
    DriveClient,
    PageTokenExpiredError,
    RawDriveItem,
)
from opshub.core.errors import ConnectorFailedError


def _noop_sleep(seconds: float) -> None:
    """``time.sleep`` stand-in: forwards no-op to keep retry tests fast.

    Pulled out as a named function (not an inline lambda) so pyright
    (strict) does not flag the patched argument as
    ``reportUnknownArgumentType`` — :meth:`unittest.mock.patch.object`'s
    typeshed annotation does not propagate the function signature when
    the replacement is an anonymous lambda, but a named function with
    an explicit ``float`` parameter satisfies the inference rule the
    project's pyright config expects.
    """
    del seconds  # explicit no-op — we just need the call to return


class _StubAuth:
    """Minimal stand-in for :class:`GoogleWorkspaceAuth` used by tests.

    The fetcher only calls ``get_access_token`` so we expose just that
    method. Using a real :class:`GoogleWorkspaceAuth` would force the
    OAuth round-trip mock on every test (overkill for client coverage
    where the auth surface is orthogonal).
    """

    def get_access_token(self) -> str:
        return "fake-access-token"


def _client_with_handler(
    handler: Any,
) -> DriveClient:
    """Build a :class:`DriveClient` whose underlying ``httpx`` uses ``handler``."""
    transport = httpx.MockTransport(handler)
    client = DriveClient(cast(GoogleWorkspaceAuth, _StubAuth()))
    # Replace the live client with one bound to the mock transport.
    # The DriveClient builds the httpx.Client in __init__; we swap it
    # out here rather than patching httpx.Client globally so each test
    # carries its own scoped transport.
    client._client.close()  # type: ignore[reportPrivateUsage]
    client._client = httpx.Client(transport=transport, timeout=5.0)  # type: ignore[reportPrivateUsage]
    return client


# ----- get_start_page_token ----------------------------------------------


def test_get_start_page_token_happy_path() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"startPageToken": "token-1"})

    client = _client_with_handler(handler)
    try:
        token = client.get_start_page_token()
    finally:
        client.close()
    assert token == "token-1"
    assert captured["params"].get("supportsAllDrives") == "true"
    assert captured["auth"] == "Bearer fake-access-token"


def test_get_start_page_token_raises_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="startPageToken"):
            client.get_start_page_token()
    finally:
        client.close()


# ----- fetch_changes -----------------------------------------------------


def _change(file_id: str, **overrides: Any) -> dict[str, Any]:
    """Build a Drive ``changes.list`` change item fixture."""
    file = {
        "id": file_id,
        "name": f"file-{file_id}",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-31T12:00:00Z",
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        "owners": [{"emailAddress": "alice@example.com", "displayName": "Alice"}],
        "trashed": False,
    }
    file.update(overrides.pop("file", {}))
    return {
        "fileId": file_id,
        "removed": False,
        "time": "2026-05-31T12:00:01Z",
        "file": file,
        **overrides,
    }


def test_fetch_changes_walks_pages_and_advances_cursor_on_final_page() -> None:
    """Cursor stays at the incoming page token until ``newStartPageToken``.

    Mirrors Teams' delta cursor pattern: incoming cursor is yielded
    until the final page, where the freshly-returned token replaces
    it. A mid-iteration crash never advances the cursor past unconsumed
    items.

    The test also pins the per-page param shape: the second page must
    carry the new ``pageToken`` AND the Shared Drives flags (OQ10) —
    without this assertion an earlier draft of ``fetch_changes`` silently
    dropped params on the second page (Drive does not echo them forward).
    """
    pages = iter(
        [
            httpx.Response(
                200,
                json={
                    "changes": [_change("F1"), _change("F2")],
                    "nextPageToken": "p2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "changes": [_change("F3")],
                    "newStartPageToken": "next-token",
                },
            ),
        ]
    )
    page_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_params.append(dict(request.url.params))
        return next(pages)

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()

    assert [(item.file_id, cursor) for item, cursor in items] == [
        ("F1", "p1"),
        ("F2", "p1"),
        ("F3", "next-token"),
    ]
    # Regression guard: every page request must carry the Shared
    # Drives flags + the correct pageToken (the bug the original draft
    # had was passing ``params=None`` on the 2nd page, which dropped
    # both).
    assert len(page_params) == 2
    assert page_params[0]["pageToken"] == "p1"
    assert page_params[0]["supportsAllDrives"] == "true"
    assert page_params[0]["includeItemsFromAllDrives"] == "true"
    assert page_params[1]["pageToken"] == "p2"
    assert page_params[1]["supportsAllDrives"] == "true"
    assert page_params[1]["includeItemsFromAllDrives"] == "true"


def test_fetch_changes_pins_shared_drives_params() -> None:
    """Shared Drives flags (OQ10) appear on every page request.

    Without these flags Google would only return My Drive content and
    the Shared Drive items would be silently dropped (a class of bug
    that is invisible until the operator notices a co-worker's doc is
    not in opshub).
    """
    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.append(dict(request.url.params))
        # Single page suffices to confirm the first call carries the
        # flags; the second page is exercised in the previous test.
        return httpx.Response(
            200,
            json={"changes": [_change("F1")], "newStartPageToken": "n"},
        )

    client = _client_with_handler(handler)
    try:
        list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()

    assert captured_params[0]["supportsAllDrives"] == "true"
    assert captured_params[0]["includeItemsFromAllDrives"] == "true"
    assert captured_params[0]["includeRemoved"] == "true"
    assert captured_params[0]["pageToken"] == "p1"


def test_fetch_changes_404_raises_page_token_expired() -> None:
    """Drive 404 on changes.list signals an expired page token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404}})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(PageTokenExpiredError):
            list(client.fetch_changes(page_token="expired"))
    finally:
        client.close()


def test_fetch_changes_410_raises_page_token_expired() -> None:
    """Drive 410 ``Gone`` also signals an expired page token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": {"code": 410}})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(PageTokenExpiredError):
            list(client.fetch_changes(page_token="expired"))
    finally:
        client.close()


def test_fetch_changes_skips_changes_with_no_file_id() -> None:
    """Drive-level events without ``fileId`` are silently dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "changes": [
                    {"removed": False, "time": "2026-01-01T00:00:00Z"},
                    _change("F1"),
                ],
                "newStartPageToken": "next",
            },
        )

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()
    assert [item.file_id for item, _ in items] == ["F1"]


def test_fetch_changes_handles_removed_items_without_file_metadata() -> None:
    """Permanent-delete events have no ``file`` object."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "changes": [
                    {
                        "fileId": "DELETED",
                        "removed": True,
                        "time": "2026-05-31T12:00:00Z",
                    }
                ],
                "newStartPageToken": "next",
            },
        )

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()
    assert len(items) == 1
    item, cursor = items[0]
    assert isinstance(item, RawDriveItem)
    assert item.file_id == "DELETED"
    assert item.removed is True
    assert item.name == ""  # no file metadata
    assert cursor == "next"


# ----- rate limit + retry ------------------------------------------------


def test_request_retries_on_429_with_retry_after_header() -> None:
    """429 with ``Retry-After`` triggers backoff + retry (rate-limit pin)."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"startPageToken": "t"})

    client = _client_with_handler(handler)
    sleep_calls: list[float] = []

    def record_sleep(seconds: float) -> None:
        sleep_calls.append(float(seconds))

    try:
        with patch.object(time, "sleep", record_sleep):
            token = client.get_start_page_token()
    finally:
        client.close()
    assert token == "t"
    assert call_count["n"] == 2
    assert sleep_calls == [1]


def test_request_retries_on_403_rate_limit_exceeded() -> None:
    """403 with ``userRateLimitExceeded`` reason is treated like 429.

    Drive's documented quota signal arrives as a 403 with a structured
    error body — distinguishing it from a scope / permission 403 is the
    whole point of the body parse in :func:`_is_rate_limit_error`.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "errors": [
                            {
                                "domain": "usageLimits",
                                "reason": "userRateLimitExceeded",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, json={"startPageToken": "t"})

    client = _client_with_handler(handler)
    try:
        with patch.object(time, "sleep", _noop_sleep):
            token = client.get_start_page_token()
    finally:
        client.close()
    assert token == "t"
    assert call_count["n"] == 2


def test_request_does_not_retry_on_plain_403() -> None:
    """403 without a rate-limit reason fails fast (scope / permission denial).

    Retrying a scope denial would only delay the inevitable failure
    by the backoff budget — the operator's consent is missing, not
    the quota.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "errors": [{"domain": "global", "reason": "insufficientPermissions"}],
                }
            },
        )

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="returned 403"):
            client.get_start_page_token()
    finally:
        client.close()


def test_request_retries_on_5xx() -> None:
    """5xx responses are transient per Drive's docs; we back off + retry."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, json={"error": {"code": 503}})
        return httpx.Response(200, json={"startPageToken": "t"})

    client = _client_with_handler(handler)
    try:
        with patch.object(time, "sleep", _noop_sleep):
            token = client.get_start_page_token()
    finally:
        client.close()
    assert token == "t"
    assert call_count["n"] == 2


def test_request_exhausts_budget_and_raises() -> None:
    """Persistent 429 after :data:`_MAX_REQUEST_ATTEMPTS` raises ConnectorFailedError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _client_with_handler(handler)
    try:
        with patch.object(time, "sleep", _noop_sleep):
            with pytest.raises(ConnectorFailedError, match="after 3 attempts"):
                client.get_start_page_token()
    finally:
        client.close()


# ----- contract pin ------------------------------------------------------


def test_drive_api_base_pinned() -> None:
    """The Drive v3 base URL is a regression-prone constant."""
    assert DRIVE_API_BASE == "https://www.googleapis.com/drive/v3"


# ----- G4 RawDriveItem extra fields --------------------------------------


def test_normalise_change_extracts_shared_and_last_modifying_user() -> None:
    """G4 (#278) lifts ``shared`` + ``lastModifyingUser`` off the Drive payload.

    The connector mapper uses these to attribute "edited by" and to
    distinguish private-from-shared in the summary; pinning the field
    surface here means a Drive API rename surfaces in tests rather
    than silently drifting the projection metadata.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "changes": [
                    {
                        "fileId": "F-shared",
                        "removed": False,
                        "time": "2026-05-31T12:00:00Z",
                        "file": {
                            "id": "F-shared",
                            "name": "Shared Doc",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-05-31T12:00:00Z",
                            "webViewLink": "https://example",
                            "trashed": False,
                            "owners": [
                                {
                                    "emailAddress": "alice@example.com",
                                    "displayName": "Alice",
                                },
                            ],
                            "shared": True,
                            "lastModifyingUser": {
                                "emailAddress": "bob@example.com",
                                "displayName": "Bob",
                            },
                        },
                    }
                ],
                "newStartPageToken": "next",
            },
        )

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()

    assert len(items) == 1
    item, _ = items[0]
    assert item.shared is True
    assert item.last_modifying_user_email == "bob@example.com"
    assert item.last_modifying_user_display_name == "Bob"


def test_normalise_change_defaults_when_optional_fields_missing() -> None:
    """Drive omits ``shared`` / ``lastModifyingUser`` on some change types.

    Anonymous edits + drive-level events do not carry these fields;
    the mapper still needs a well-typed value so we default to
    ``False`` / ``""``. Same defensive shape the rest of the
    normalisation uses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "changes": [
                    {
                        "fileId": "F-min",
                        "removed": False,
                        "time": "2026-05-31T12:00:00Z",
                        "file": {
                            "id": "F-min",
                            "name": "Plain",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-05-31T12:00:00Z",
                            "owners": [
                                {
                                    "emailAddress": "alice@example.com",
                                    "displayName": "Alice",
                                },
                            ],
                        },
                    }
                ],
                "newStartPageToken": "next",
            },
        )

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_changes(page_token="p1"))
    finally:
        client.close()

    item, _ = items[0]
    assert item.shared is False
    assert item.last_modifying_user_email == ""
    assert item.last_modifying_user_display_name == ""


# ----- G4 export_file ----------------------------------------------------


def test_export_file_happy_path() -> None:
    """``files.export`` returns the raw bytes and pins the URL + mime parameter."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url).split("?", 1)[0]
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"fake-docx-bytes")

    client = _client_with_handler(handler)
    try:
        content = client.export_file(
            file_id="F1",
            mime_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
    finally:
        client.close()

    assert content == b"fake-docx-bytes"
    assert captured["url"] == f"{DRIVE_API_BASE}/files/F1/export"
    assert captured["params"]["mimeType"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Bearer auth still required on the export endpoint.
    assert captured["auth"] == "Bearer fake-access-token"


def test_export_file_rejects_empty_file_id() -> None:
    """An empty file_id is a programmer error — fail fast rather than ask Drive."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500)

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="empty file_id"):
            client.export_file(file_id="", mime_type="any")
    finally:
        client.close()


def test_export_file_raises_on_403_file_not_exportable() -> None:
    """403 fileNotExportable surfaces as :class:`ConnectorFailedError` (not retried).

    A non-Workspace native (PDF upload, folder, binary) routed
    through ``files.export`` returns 403 with reason
    ``fileNotExportable``. Retrying would not help so we fail fast.
    The 403 carries no rate-limit reason code so it does NOT trigger
    the rate-limit backoff path.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "errors": [{"reason": "fileNotExportable"}],
                }
            },
        )

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="returned 403"):
            client.export_file(file_id="F1", mime_type="any")
    finally:
        client.close()


def test_export_file_retries_on_429() -> None:
    """``files.export`` shares the 429 + Retry-After backoff with ``changes.list``."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, content=b"ok-after-retry")

    client = _client_with_handler(handler)
    try:
        with patch.object(time, "sleep", _noop_sleep):
            content = client.export_file(file_id="F1", mime_type="any")
    finally:
        client.close()

    assert content == b"ok-after-retry"
    assert call_count["n"] == 2


def test_export_file_retries_on_5xx() -> None:
    """``files.export`` shares 5xx backoff with ``changes.list`` (transient)."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok-after-5xx")

    client = _client_with_handler(handler)
    try:
        with patch.object(time, "sleep", _noop_sleep):
            content = client.export_file(file_id="F1", mime_type="any")
    finally:
        client.close()

    assert content == b"ok-after-5xx"
    assert call_count["n"] == 2


def test_export_file_returns_empty_bytes_for_empty_doc() -> None:
    """An empty Doc legitimately exports to zero bytes — pass it through verbatim.

    :func:`opshub.core.document_extract.extract_workspace_export`
    short-circuits ``b""`` to ``body=""`` so the connector still
    emits a :class:`SourceObserved` (ADR-0020 retain-everything).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = _client_with_handler(handler)
    try:
        content = client.export_file(file_id="F-empty", mime_type="any")
    finally:
        client.close()

    assert content == b""
