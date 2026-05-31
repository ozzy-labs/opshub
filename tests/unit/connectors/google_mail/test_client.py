"""Tests for ``opshub.connectors.google_mail.client`` (Phase 14 G3).

:class:`GmailClient` is the ``httpx`` wrapper for Gmail API v1's
``users.getProfile`` / ``users.messages.list`` / ``users.messages.get``
/ ``users.history.list`` endpoints. Every test injects a
:class:`httpx.MockTransport` so the suite never reaches Google
(Phase 14 plan §7.5).

Coverage map:

* ``get_profile_history_id`` happy path.
* ``list_messages`` paginates and yields message ids; empty inbox
  (no ``messages`` field) yields nothing.
* ``list_messages`` forwards the ``q`` query parameter.
* ``get_message`` lifts the payload into :class:`RawGmailMessage` via
  the normaliser.
* ``fetch_history`` paginates and yields ``(message_id, cursor)``
  tuples, advancing to the response's ``historyId`` on the final
  page.
* ``fetch_history`` deduplication semantics (same message in
  ``messagesAdded`` + ``labelsAdded`` yields twice — connector dedups).
* HistoryId expiry (404 on ``/users/me/history``) raises
  :class:`HistoryIdExpiredError`.
* Rate-limit retry pin: 429 honours ``Retry-After``; 403
  ``userRateLimitExceeded`` is treated as 429.
* 5xx retry pin (transient).
* Persistent failure exhausts the retry budget and raises
  :class:`ConnectorFailedError`.
* Delegated mailbox guard (OQ12 / Phase 14 plan §8): every request
  URL is scoped under ``users/me`` — the wrapper does NOT expose a
  ``user_id`` / ``delegate`` parameter.
"""

from __future__ import annotations

import time
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
)
from opshub.core.errors import ConnectorFailedError


def _noop_sleep(seconds: float) -> None:
    """``time.sleep`` stand-in: forwards no-op to keep retry tests fast."""
    del seconds


class _StubAuth:
    """Minimal stand-in for :class:`GoogleWorkspaceAuth`.

    The client only calls ``get_access_token`` so we expose just that
    method. Using a real :class:`GoogleWorkspaceAuth` would force the
    OAuth round-trip mock on every test (overkill for client coverage
    where the auth surface is orthogonal). Phase 14 G2 (#294) helper
    lives at :mod:`opshub.connectors.google_auth.auth`.
    """

    def get_access_token(self) -> str:
        return "fake-access-token"


def _client_with_handler(handler: Any) -> GmailClient:
    """Construct a :class:`GmailClient` whose ``httpx.Client`` uses ``handler``."""
    auth = cast(GoogleWorkspaceAuth, _StubAuth())
    client = GmailClient(auth=auth)
    # Swap the ``httpx.Client`` for one backed by ``MockTransport``.
    transport = httpx.MockTransport(handler)
    client._client.close()  # pyright: ignore[reportPrivateUsage]
    client._client = httpx.Client(transport=transport, timeout=30.0)  # pyright: ignore[reportPrivateUsage]
    return client


# ----- get_profile_history_id --------------------------------------------


def test_get_profile_history_id_returns_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/gmail/v1/users/me/profile"
        assert request.headers["Authorization"] == "Bearer fake-access-token"
        return httpx.Response(200, json={"emailAddress": "me@x", "historyId": "123"})

    client = _client_with_handler(handler)
    assert client.get_profile_history_id() == "123"


def test_get_profile_history_id_raises_on_missing_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emailAddress": "me@x"})

    client = _client_with_handler(handler)
    with pytest.raises(ConnectorFailedError, match="historyId"):
        client.get_profile_history_id()


# ----- list_messages -----------------------------------------------------


def test_list_messages_yields_ids_across_pages() -> None:
    pages = [
        {
            "messages": [{"id": "M1"}, {"id": "M2"}],
            "nextPageToken": "PT2",
        },
        {
            "messages": [{"id": "M3"}],
        },
    ]
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/gmail/v1/users/me/messages"
        page = pages[state["i"]]
        state["i"] += 1
        return httpx.Response(200, json=page)

    client = _client_with_handler(handler)
    assert list(client.list_messages()) == ["M1", "M2", "M3"]
    assert state["i"] == 2


def test_list_messages_empty_inbox_yields_nothing() -> None:
    """Gmail omits ``messages`` entirely when the result set is empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultSizeEstimate": 0})

    client = _client_with_handler(handler)
    assert list(client.list_messages()) == []


def test_list_messages_forwards_query_param() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q", "")
        return httpx.Response(200, json={"messages": []})

    client = _client_with_handler(handler)
    list(client.list_messages(query="after:2026/01/01"))
    assert captured["q"] == "after:2026/01/01"


# ----- get_message -------------------------------------------------------


def test_get_message_lifts_normalised_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/gmail/v1/users/me/messages/M-Y"
        assert request.url.params.get("format") == "full"
        return httpx.Response(
            200,
            json={
                "id": "M-Y",
                "threadId": "T-Y",
                "historyId": "999",
                "snippet": "snip",
                "internalDate": "1735689600000",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Hi"},
                        {"name": "From", "value": "alice@x.com"},
                    ],
                    "body": {"data": "aGVsbG8="},
                },
            },
        )

    client = _client_with_handler(handler)
    raw = client.get_message(message_id="M-Y")
    assert raw.message_id == "M-Y"
    assert raw.thread_id == "T-Y"
    assert raw.subject == "Hi"
    assert raw.from_header == "alice@x.com"
    assert raw.label_ids == ("INBOX",)
    assert raw.body_text == "hello"


def test_get_message_rejects_empty_id() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="should not be called")

    client = _client_with_handler(_handler)
    with pytest.raises(ConnectorFailedError, match="empty message_id"):
        client.get_message(message_id="")


# ----- fetch_history -----------------------------------------------------


def test_fetch_history_yields_message_ids_with_cursor_advance() -> None:
    """Final-page cursor advances to the response's ``historyId``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/gmail/v1/users/me/history"
        assert request.url.params.get("startHistoryId") == "100"
        return httpx.Response(
            200,
            json={
                "historyId": "200",
                "history": [
                    {
                        "id": "150",
                        "messagesAdded": [{"message": {"id": "M1", "threadId": "T1"}}],
                    },
                    {
                        "id": "160",
                        "messagesAdded": [{"message": {"id": "M2", "threadId": "T2"}}],
                    },
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_history(start_history_id="100"))
    assert [mid for mid, _cursor in results] == ["M1", "M2"]
    # Final page advances cursor to the response's historyId.
    assert results[-1][1] == "200"


def test_fetch_history_advances_across_pages() -> None:
    pages = [
        {
            "historyId": "201",
            "history": [
                {
                    "id": "150",
                    "messagesAdded": [{"message": {"id": "M1", "threadId": "T1"}}],
                }
            ],
            "nextPageToken": "P2",
        },
        {
            "historyId": "202",
            "history": [
                {
                    "id": "151",
                    "messagesAdded": [{"message": {"id": "M2", "threadId": "T2"}}],
                }
            ],
        },
    ]
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[state["i"]]
        state["i"] += 1
        return httpx.Response(200, json=page)

    client = _client_with_handler(handler)
    results = list(client.fetch_history(start_history_id="100"))
    # Mid-iteration crash-safety: M1 carries the *incoming* cursor
    # since pagination is not on the final page yet.
    assert results[0][0] == "M1"
    assert results[0][1] == "100"
    # Final page advances to "202".
    assert results[1][0] == "M2"
    assert results[1][1] == "202"


def test_fetch_history_dedups_within_record_but_not_across_records() -> None:
    """A message in both messagesAdded + labelsAdded of the same record dedups (in-record);
    appearance in two separate records yields twice (connector dedups across records).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "historyId": "203",
                "history": [
                    {
                        "id": "150",
                        "messagesAdded": [{"message": {"id": "M1", "threadId": "T1"}}],
                        "labelsAdded": [
                            {
                                "message": {"id": "M1", "threadId": "T1"},
                                "labelIds": ["IMPORTANT"],
                            }
                        ],
                    },
                    {
                        "id": "151",
                        "labelsAdded": [
                            {
                                "message": {"id": "M1", "threadId": "T1"},
                                "labelIds": ["STARRED"],
                            }
                        ],
                    },
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_history(start_history_id="100"))
    # First record's in-record dedup keeps M1 once; the second record
    # carries M1 again. Connector layer is responsible for dedup across
    # records (so we see M1 twice here at the client layer).
    assert [mid for mid, _ in results] == ["M1", "M1"]


def test_fetch_history_no_changes_yields_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"historyId": "100"})

    client = _client_with_handler(handler)
    assert list(client.fetch_history(start_history_id="100")) == []


def test_fetch_history_404_raises_history_id_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404}})

    client = _client_with_handler(handler)
    with pytest.raises(HistoryIdExpiredError):
        list(client.fetch_history(start_history_id="42"))


def test_fetch_history_404_with_historynotfound_reason_raises_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": 404,
                    "message": "Requested entity was not found.",
                    "errors": [{"reason": "historyNotFound"}],
                }
            },
        )

    client = _client_with_handler(handler)
    with pytest.raises(HistoryIdExpiredError):
        list(client.fetch_history(start_history_id="42"))


# ----- retry semantics ---------------------------------------------------


def test_request_retries_on_429_with_retry_after() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"historyId": "X"})

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        assert client.get_profile_history_id() == "X"
    assert state["calls"] == 2


def test_request_retries_on_403_user_rate_limit_exceeded() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "errors": [{"reason": "userRateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(200, json={"historyId": "X"})

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        assert client.get_profile_history_id() == "X"
    assert state["calls"] == 2


def test_request_retries_on_5xx() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"historyId": "X"})

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        assert client.get_profile_history_id() == "X"
    assert state["calls"] == 2


def test_request_exhausts_retry_budget_and_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        with pytest.raises(ConnectorFailedError, match="failed after"):
            client.get_profile_history_id()


def test_request_fails_fast_on_non_retried_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    client = _client_with_handler(handler)
    with pytest.raises(ConnectorFailedError, match="returned 400"):
        client.get_profile_history_id()


# ----- delegated mailbox guard (OQ12) ------------------------------------


def test_no_delegate_or_user_id_in_request_path() -> None:
    """OQ12 / Phase 14 plan §8 — Gmail client targets ``users/me`` only.

    Every request URL must be scoped under ``/gmail/v1/users/me/`` and
    must NOT carry a ``delegate=`` or ``user_id=`` query parameter. A
    future regression that widens the surface to delegated mailboxes
    surfaces here.
    """
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "X"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []})
        if "/history" in request.url.path:
            return httpx.Response(200, json={"historyId": "X"})
        if "/messages/" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "M", "payload": {"mimeType": "text/plain", "headers": []}},
            )
        return httpx.Response(404)

    client = _client_with_handler(handler)
    client.get_profile_history_id()
    list(client.list_messages())
    list(client.fetch_history(start_history_id="100"))
    client.get_message(message_id="M")

    assert captured, "expected at least one captured request"
    for url in captured:
        assert "/users/me/" in url.path, f"request {url} is not scoped under users/me"
        params_keys = set(url.params.keys())
        assert "delegate" not in params_keys
        assert "user_id" not in params_keys
        assert "userId" not in params_keys


def test_gmail_api_base_pin() -> None:
    """The base URL pin keeps the v1 surface explicit."""
    assert GMAIL_API_BASE == "https://gmail.googleapis.com/gmail/v1"
