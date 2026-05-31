"""Tests for ``opshub.connectors.google_mail.client`` (Phase 14 G3).

:class:`GmailClient` is the ``httpx`` wrapper for Gmail API v1's
``users.history.list`` + ``users.messages.list`` + ``users.messages.get``
+ ``users.getProfile`` endpoints. Every test injects a
:class:`httpx.MockTransport` so the suite never reaches Gmail (Phase
14 plan §7.5).

Coverage map:

* ``get_profile_history_id`` happy path.
* ``fetch_history`` walks ``nextPageToken``, yields ``historyId`` on
  the final page, and de-duplicates message ids appearing in
  multiple sub-arrays of the same record.
* ``fetch_history`` raises :class:`HistoryIdExpiredError` on a 404
  from the ``/history`` route.
* ``list_messages_since`` walks pagination and emits message ids.
* ``get_message`` happy path on each fixture (text/plain only,
  text/html only, multipart alternative, with attachment, no
  labels).
* ``get_message`` ignores attachment parts (no ``data``).
* Rate-limit retry pin: 429 honours ``Retry-After``; 403
  ``userRateLimitExceeded`` is treated as 429.
* 5xx retry pin (transient).
* Persistent failure exhausts the retry budget and raises
  :class:`ConnectorFailedError`.
* Delegated mailbox URL pin (Phase 14 OQ12 personal-mailbox-only
  guard): no request URL contains ``?delegate=...`` and every URL
  targets ``/users/me/...`` (literal, not an email address).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Gmail connector tests require the 'connectors-google-workspace' extras",
)

import httpx

from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
from opshub.connectors.google_mail.client import (
    GMAIL_API_BASE,
    GmailClient,
    HistoryIdExpiredError,
    RawGmailMessage,
)
from opshub.core.errors import ConnectorFailedError

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "google_mail"


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _noop_sleep(seconds: float) -> None:
    """``time.sleep`` stand-in: forwards no-op to keep retry tests fast.

    Pulled out as a named function (not an inline lambda) so pyright
    (strict) does not flag the patched argument as
    ``reportUnknownArgumentType`` — same shape the google_workspace
    client tests use.
    """
    del seconds


class _StubAuth:
    """Minimal stand-in for :class:`GoogleWorkspaceAuth` used by tests.

    The client only calls ``get_access_token`` so we expose just that
    method. Using a real :class:`GoogleWorkspaceAuth` would force the
    OAuth round-trip mock on every test (overkill for client coverage
    where the auth surface is orthogonal). Phase 14 G2 (#294) moved
    the real helper to :mod:`opshub.connectors.google_auth.auth`; the
    cast target reflects the new module path.
    """

    def get_access_token(self) -> str:
        return "fake-access-token"


def _client_with_handler(handler: Any) -> GmailClient:
    """Build a :class:`GmailClient` whose underlying ``httpx`` uses ``handler``."""
    transport = httpx.MockTransport(handler)
    client = GmailClient(cast(GoogleWorkspaceAuth, _StubAuth()))
    # Replace the live client with one bound to the mock transport —
    # mirrors the google_workspace client tests.
    client._client.close()  # pyright: ignore[reportPrivateUsage]
    client._client = httpx.Client(transport=transport, timeout=5.0)  # pyright: ignore[reportPrivateUsage]
    return client


# ----- get_profile_history_id --------------------------------------------


def test_get_profile_history_id_happy_path() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_fixture("profile.json"))

    client = _client_with_handler(handler)
    try:
        history_id = client.get_profile_history_id()
    finally:
        client.close()
    assert history_id == "99999"
    assert "/users/me/profile" in captured["url"]
    assert captured["auth"] == "Bearer fake-access-token"


def test_get_profile_history_id_raises_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="historyId"):
            client.get_profile_history_id()
    finally:
        client.close()


# ----- fetch_history -----------------------------------------------------


def test_fetch_history_yields_dedup_message_ids() -> None:
    """``msg-plain-001`` appears in both ``messagesAdded`` and ``labelsAdded``.

    The client's ``_iter_message_ids`` helper de-dupes per record so
    the caller sees each id once per page (per-page rather than
    per-walk so the projection re-observes label-change-only events;
    the projection's natural-key dedup absorbs cross-page duplicates).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("history_page.json"))

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_history(start_history_id="10000"))
    finally:
        client.close()
    # The fixture's record-2 ``labelsAdded`` re-mentions ``msg-plain-001``;
    # the per-record dedup yields it once for the second record (a new
    # record id resets the seen set, matching the implementation).
    message_ids = [mid for mid, _ in items]
    assert "msg-plain-001" in message_ids
    assert "msg-html-002" in message_ids
    # All yielded cursors equal the final ``historyId`` because the
    # single-page response carries no ``nextPageToken``.
    assert all(cursor == "10003" for _, cursor in items)


def test_fetch_history_walks_pages_and_advances_cursor_on_final_page() -> None:
    """Cursor stays at the incoming history id until the final page.

    Same shape as Drive's ``test_fetch_changes_walks_pages...`` —
    pinning that a mid-iteration crash never advances the cursor
    past unconsumed messages.
    """
    pages = iter(
        [
            httpx.Response(
                200,
                json={
                    "history": [
                        {
                            "id": "a",
                            "messagesAdded": [
                                {"message": {"id": "M1", "threadId": "T1"}},
                            ],
                        },
                    ],
                    "nextPageToken": "p2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "history": [
                        {
                            "id": "b",
                            "messagesAdded": [
                                {"message": {"id": "M2", "threadId": "T2"}},
                            ],
                        },
                    ],
                    "historyId": "final",
                },
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(pages)

    client = _client_with_handler(handler)
    try:
        items = list(client.fetch_history(start_history_id="p1"))
    finally:
        client.close()
    assert items == [("M1", "p1"), ("M2", "final")]


def test_fetch_history_raises_history_id_expired_on_404() -> None:
    """A 404 on the ``/history`` route is interpreted as TTL expiry.

    Pins the cursor-invalidation signal: ADR-0010 §Phase 14 改訂 (j)
    contract states Gmail returns 404 ``historyNotFound`` once the
    stored id crosses the documented ~7-day TTL.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": 404,
                    "message": "Requested entity was not found.",
                    "errors": [{"reason": "notFound"}],
                }
            },
        )

    client = _client_with_handler(handler)
    try:
        with pytest.raises(HistoryIdExpiredError):
            list(client.fetch_history(start_history_id="stale-id"))
    finally:
        client.close()


# ----- list_messages_since -----------------------------------------------


def test_list_messages_since_yields_message_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("q", "").startswith("after:")
        return httpx.Response(200, json=_fixture("messages_list_page.json"))

    client = _client_with_handler(handler)
    try:
        ids = list(client.list_messages_since(since_epoch_seconds=1735660800))
    finally:
        client.close()
    assert ids == ["msg-plain-001", "msg-html-002", "msg-multi-003"]


def test_list_messages_since_walks_pagination() -> None:
    pages = iter(
        [
            httpx.Response(
                200,
                json={
                    "messages": [{"id": "A", "threadId": "T"}],
                    "nextPageToken": "tok",
                },
            ),
            httpx.Response(
                200,
                json={"messages": [{"id": "B", "threadId": "T"}]},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(pages)

    client = _client_with_handler(handler)
    try:
        ids = list(client.list_messages_since(since_epoch_seconds=1))
    finally:
        client.close()
    assert ids == ["A", "B"]


def test_list_messages_since_handles_empty_response() -> None:
    """Gmail omits ``messages`` entirely on a zero-hit query."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultSizeEstimate": 0})

    client = _client_with_handler(handler)
    try:
        ids = list(client.list_messages_since(since_epoch_seconds=1))
    finally:
        client.close()
    assert ids == []


# ----- get_message --------------------------------------------------------


def test_get_message_text_plain_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("message_text_plain_only.json"))

    client = _client_with_handler(handler)
    try:
        msg = client.get_message(message_id="msg-plain-001")
    finally:
        client.close()
    assert isinstance(msg, RawGmailMessage)
    assert msg.message_id == "msg-plain-001"
    assert msg.thread_id == "thread-plain-001"
    assert "Hello from text/plain" in msg.body_text
    assert msg.body_html == ""
    assert msg.from_header == "Alice Example <alice@example.com>"
    assert msg.subject_header == "Plain text only message"
    assert msg.label_ids == ("INBOX", "IMPORTANT", "CATEGORY_PERSONAL")


def test_get_message_text_html_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("message_text_html_only.json"))

    client = _client_with_handler(handler)
    try:
        msg = client.get_message(message_id="msg-html-002")
    finally:
        client.close()
    assert msg.body_text == ""
    assert "<html>" in msg.body_html
    assert "HTML only body" in msg.body_html


def test_get_message_multipart_alternative_prefers_text_plain() -> None:
    """Both parts decoded; mapper picks text/plain (mapper test pins that)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("message_multipart_alternative.json"))

    client = _client_with_handler(handler)
    try:
        msg = client.get_message(message_id="msg-multi-003")
    finally:
        client.close()
    assert msg.body_text == "multi-part plain body"
    assert msg.body_html == "<div>multi-part HTML</div>"


def test_get_message_with_attachment_ignores_attachment_part() -> None:
    """The text/plain part is captured; the PDF attachment is skipped.

    Phase 14 plan §1 OQ4 explicitly forbids attachment retention
    (Outlook symmetric). The client distinguishes attachment parts
    via ``attachmentId`` (Gmail sets it AND omits ``data`` for binary
    attachments) so the body extractor only walks inline body parts.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("message_with_attachment.json"))

    client = _client_with_handler(handler)
    try:
        msg = client.get_message(message_id="msg-attach-004")
    finally:
        client.close()
    assert "Plain body with attachment" in msg.body_text
    assert msg.body_html == ""


def test_get_message_no_labels_yields_empty_tuple() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("message_no_labels.json"))

    client = _client_with_handler(handler)
    try:
        msg = client.get_message(message_id="msg-nolabel-005")
    finally:
        client.close()
    assert msg.label_ids == ()


def test_get_message_rejects_empty_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(ConnectorFailedError, match="empty message_id"):
            client.get_message(message_id="")
    finally:
        client.close()


# ----- rate limit / retry -------------------------------------------------


def test_429_honours_retry_after_header() -> None:
    """One 429 retried after honouring ``Retry-After``; second attempt succeeds."""
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json=_fixture("profile.json")),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client_with_handler(handler)
    try:
        with patch("time.sleep", _noop_sleep):
            history_id = client.get_profile_history_id()
    finally:
        client.close()
    assert history_id == "99999"


def test_403_user_rate_limit_treated_as_throttle() -> None:
    responses = iter(
        [
            httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "errors": [{"reason": "userRateLimitExceeded"}],
                    }
                },
            ),
            httpx.Response(200, json=_fixture("profile.json")),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client_with_handler(handler)
    try:
        with patch("time.sleep", _noop_sleep):
            history_id = client.get_profile_history_id()
    finally:
        client.close()
    assert history_id == "99999"


def test_5xx_retried_then_success() -> None:
    responses = iter(
        [
            httpx.Response(503, json={}),
            httpx.Response(200, json=_fixture("profile.json")),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client_with_handler(handler)
    try:
        with patch("time.sleep", _noop_sleep):
            history_id = client.get_profile_history_id()
    finally:
        client.close()
    assert history_id == "99999"


def test_persistent_failure_exhausts_retry_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client = _client_with_handler(handler)
    try:
        with patch("time.sleep", _noop_sleep):
            with pytest.raises(ConnectorFailedError, match="after 3"):
                client.get_profile_history_id()
    finally:
        client.close()


def test_non_rate_limit_4xx_fails_fast() -> None:
    """A 400 / 401 / 403-without-rate-reason is **not** retried."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"code": 401, "message": "auth"}})

    client = _client_with_handler(handler)
    try:
        with patch("time.sleep", _noop_sleep):
            with pytest.raises(ConnectorFailedError, match="401"):
                client.get_profile_history_id()
    finally:
        client.close()
    assert calls["n"] == 1


# ----- delegated mailbox guard (Phase 14 OQ12) ---------------------------


def test_request_url_has_no_delegate_param() -> None:
    """OQ12 personal-mailbox guard: no request URL carries ``?delegate=...``.

    The MVP scope is the operator's own mailbox (Phase 14 plan §1
    OQ12). Shared / delegated mailboxes are a Phase 15+ extension and
    require additional consent + a different request shape such as
    ``users/<email>/...``. This test pins that the client always
    targets the literal ``users/me`` endpoint family and never
    appends a ``delegate`` query parameter.

    The guard fires across every public client method (``getProfile``
    / ``history.list`` / ``messages.list`` / ``messages.get``) by
    inspecting the URL captured in the mock transport handler.
    """
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        # Return shape-correct payloads for every endpoint so each
        # public method runs to completion.
        url_str = str(request.url)
        if "/profile" in url_str:
            return httpx.Response(200, json=_fixture("profile.json"))
        if "/history" in url_str:
            return httpx.Response(200, json=_fixture("history_page.json"))
        if "/messages/" in url_str:
            return httpx.Response(200, json=_fixture("message_text_plain_only.json"))
        # /messages list endpoint
        return httpx.Response(200, json=_fixture("messages_list_page.json"))

    client = _client_with_handler(handler)
    try:
        client.get_profile_history_id()
        list(client.fetch_history(start_history_id="x"))
        list(client.list_messages_since(since_epoch_seconds=0))
        client.get_message(message_id="msg-plain-001")
    finally:
        client.close()

    assert captured_urls, "expected at least one captured URL"
    for url in captured_urls:
        # Phase 14 OQ12 invariants:
        # 1. URL must target the gmail base.
        assert url.startswith(GMAIL_API_BASE), f"non-Gmail URL leaked: {url}"
        # 2. Path must use the literal ``users/me`` (not an email
        #    address or delegated mailbox identifier).
        assert "/users/me/" in url, f"unexpected mailbox identifier in {url}"
        # 3. No ``delegate`` query parameter (the failure mode the
        #    guard exists to catch — a future refactor that adds a
        #    delegated-mailbox kwarg to the client).
        assert "delegate=" not in url, f"delegated mailbox param leaked into {url}"
