"""Tests for ``opshub.connectors.teams.fetcher`` (Phase 11 F5).

Every test routes Microsoft Graph traffic through
:class:`httpx.MockTransport` so the suite never reaches a real
``graph.microsoft.com`` endpoint — mirrors the Phase 7 MS365 fetcher
test pattern (``tests/unit/connectors/ms365/test_fetcher.py``). The
``connectors-teams`` extras include both ``msal`` (reserved for the
out-of-process OAuth helper) and ``httpx``; the fetcher itself only
needs ``httpx``, so the importorskip below gates on that.

Pinned behaviour
----------------

1. Delta-token URL is the bootstrap when ``delta_link is None``; the
   stored delta link is replayed verbatim on subsequent syncs.
2. ``@odata.nextLink`` pagination is followed transparently and the
   yielded cursor stays on the in-flight value until the final page.
3. On the final page (no ``nextLink`` + presence of ``@odata.deltaLink``)
   the cursor advances to the freshly-returned delta link.
4. ``410 Gone`` on the stored delta link triggers the full-pass
   fallback per ADR-0010 §改訂 (c); a fresh delta link is acquired
   afterwards and surfaced via :attr:`pending_delta_link`.
5. ``fallback_window_days = 0`` disables fallback explicitly — the
   underlying :class:`ConnectorFailedError` propagates.
6. ``429`` is honoured with the documented backoff (asserts retry
   happens; ``time.sleep`` is patched to avoid wall-clock waits).
7. Tokens never appear in raised exception messages (ADR-0005 / 0020).
8. System messages (no body + no sender) are dropped by the
   normaliser so the connector loop never sees them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Teams fetcher tests require the 'connectors-teams' extras",
)

import httpx

from opshub.connectors.teams.fetcher import (
    GRAPH_BASE,
    RawTeamsChatMessage,
    TeamsFetcher,
)
from opshub.core.errors import ConnectorFailedError

# ----- helpers -----------------------------------------------------------


class _StubAuth:
    """Minimal stand-in for :class:`TeamsAuth`.

    The fetcher reads ``self._auth.token`` to populate the
    ``Authorization: Bearer`` header. We return a sentinel and never
    rotate (Teams' Graph User Token lifetime is long enough that the
    Phase 11 MVP fetcher does not auto-refresh inline).
    """

    def __init__(self, token: str = "bearer-fake") -> None:
        self.token = token


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Patch :class:`httpx.Client` so the fetcher uses ``handler``.

    Mirrors the Phase 7 MS365 fetcher test helper exactly.
    """
    requests: list[httpx.Request] = []
    real_client_cls = httpx.Client

    def _recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def _factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_recorded),
            **client_kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)
    return requests


def _make_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    fallback_window_days: int | None = None,
) -> tuple[TeamsFetcher, _StubAuth, list[httpx.Request]]:
    """Build a fetcher whose HTTP client uses the given handler."""
    requests = _patch_httpx_client(monkeypatch, handler)
    auth = _StubAuth()
    # ``_StubAuth`` is structurally compatible with :class:`TeamsAuth`
    # (both expose ``.token: str``); :class:`TeamsAuth` is not declared
    # as a ``Protocol`` so the assignment needs an explicit cast for
    # pyright's strict mode rather than a duck-typing nicety.
    from typing import cast

    from opshub.connectors.teams.auth import TeamsAuth

    typed_auth = cast(TeamsAuth, auth)
    if fallback_window_days is None:
        fetcher = TeamsFetcher(auth=typed_auth)
    else:
        fetcher = TeamsFetcher(
            auth=typed_auth,
            fallback_window_days=fallback_window_days,
        )
    return fetcher, auth, requests


def _message_payload(
    *,
    msg_id: str = "1700000000001",
    chat_id: str = "19:abc@thread.v2",
    body_html: str = "<p>hello</p>",
    sender_name: str = "Alice",
    sender_id: str = "user-alice",
    created_iso: str = "2026-01-01T00:00:00Z",
    last_modified_iso: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "chatId": chat_id,
        "chatType": "groupChat",
        "body": {"content": body_html, "contentType": "html"},
        "from": {
            "user": {"displayName": sender_name, "id": sender_id},
        },
        "createdDateTime": created_iso,
        "lastModifiedDateTime": last_modified_iso,
        "webUrl": f"https://teams.microsoft.com/l/message/{msg_id}",
    }


# ----- delta path: happy paths ------------------------------------------


def test_first_sync_starts_at_root_delta_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delta_link=None`` → root delta URL with the bootstrap filter."""
    payload = {
        "value": [_message_payload()],
        "@odata.deltaLink": f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=NEW",
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        # Root call uses the bootstrap ``$filter`` and goes to
        # ``getAllMessages``.
        assert "getAllMessages" in str(request.url)
        return httpx.Response(200, json=payload)

    fetcher, _auth, requests = _make_fetcher(monkeypatch, _handler)
    try:
        yields = list(fetcher.fetch_chat_messages(delta_link=None))
    finally:
        fetcher.close()

    assert len(requests) == 1
    # Authorization header is set with the bearer (token never echoes
    # into the request body).
    assert requests[0].headers.get("Authorization") == "Bearer bearer-fake"
    # Single yield with the freshly-returned delta link as cursor.
    assert len(yields) == 1
    msg, cursor = yields[0]
    assert isinstance(msg, RawTeamsChatMessage)
    assert msg.id == "1700000000001"
    assert cursor == f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=NEW"


def test_subsequent_sync_replays_stored_delta_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted delta link is replayed verbatim on the next sync.

    This is the core delta-resume contract — the link is opaque, so
    we must not modify it before re-issuing.
    """
    stored = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=PREVIOUS"
    new_link = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=NEXT"
    payload = {
        "value": [_message_payload(msg_id="m-2")],
        "@odata.deltaLink": new_link,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        # The request URL must match the stored delta link exactly.
        assert str(request.url) == stored
        return httpx.Response(200, json=payload)

    fetcher, _auth, requests = _make_fetcher(monkeypatch, _handler)
    try:
        yields = list(fetcher.fetch_chat_messages(delta_link=stored))
    finally:
        fetcher.close()

    assert len(requests) == 1
    assert len(yields) == 1
    _msg, cursor = yields[0]
    assert cursor == new_link


def test_pagination_keeps_cursor_in_flight_until_final_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-pagination yields carry the *incoming* cursor.

    Only the final page (no ``nextLink``, has ``deltaLink``) advances
    the cursor to the new delta link. This guarantees mid-iteration
    crash idempotency: the next sync replays the prior cursor, the
    projection dedups, and no event is lost.
    """
    stored = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=START"
    page2_url = f"{GRAPH_BASE}/me/chats/getAllMessages?$skiptoken=PAGE2"
    final_delta = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=FINAL"

    pages = {
        stored: {
            "value": [_message_payload(msg_id="m-1")],
            "@odata.nextLink": page2_url,
        },
        page2_url: {
            "value": [_message_payload(msg_id="m-2")],
            "@odata.deltaLink": final_delta,
        },
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[str(request.url)])

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    try:
        yields = list(fetcher.fetch_chat_messages(delta_link=stored))
    finally:
        fetcher.close()

    assert len(yields) == 2
    # First yield carries the in-flight (incoming) cursor.
    assert yields[0][1] == stored
    # Final yield carries the freshly-returned delta link.
    assert yields[1][1] == final_delta


def test_system_messages_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages with no body and no sender (chat membership / rename
    events) are dropped by the normaliser so the connector loop never
    sees them."""
    payload = {
        "value": [
            {
                "id": "sys-1",
                "chatId": "19:abc@thread.v2",
                "body": {"content": "", "contentType": "html"},
                # ``from`` absent — Graph signals system events this way.
                "createdDateTime": "2026-01-01T00:00:00Z",
                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            },
            _message_payload(msg_id="m-real"),
        ],
        "@odata.deltaLink": f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=X",
    }

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    try:
        yields = list(fetcher.fetch_chat_messages(delta_link=None))
    finally:
        fetcher.close()

    # System message dropped; the real one survives.
    assert [m.id for m, _ in yields] == ["m-real"]


# ----- fallback path (ADR-0010 §改訂 (c)) -------------------------------


def test_410_triggers_full_pass_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``410 Gone`` on the stored delta link triggers the full-pass
    fallback; a fresh delta link is acquired afterwards and surfaced
    via :attr:`pending_delta_link`.

    The fallback walk uses ``$filter=lastModifiedDateTime ge <since>``
    over the configured window (here 30 days, the default).
    """
    stored = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=EXPIRED"
    refresh_delta = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=FRESH"

    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        call_log.append(url)
        # Stored delta link is rejected.
        if url == stored:
            return httpx.Response(
                410,
                json={"error": {"code": "syncStateInvalid"}},
            )
        # Fallback window scan: the fetcher builds it with ``$filter=
        # lastModifiedDateTime ge <since>``. The literal space is
        # encoded by ``urlencode`` (default ``+``) so we match on the
        # operator + the encoded marker rather than relying on a
        # specific encoding.
        if "lastModifiedDateTime+ge" in url or "lastModifiedDateTime%20ge" in url:
            return httpx.Response(
                200,
                json={"value": [_message_payload(msg_id="m-recovered")]},
            )
        # Refresh delta link acquisition (root delta URL uses
        # ``lastModifiedDateTime gt 1970...``).
        return httpx.Response(
            200,
            json={"value": [], "@odata.deltaLink": refresh_delta},
        )

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    try:
        yields = list(fetcher.fetch_chat_messages(delta_link=stored))
    finally:
        fetcher.close()

    # Recovered message surfaced from the fallback window.
    assert [m.id for m, _ in yields] == ["m-recovered"], f"call_log: {call_log}"
    # Pending delta link carries the freshly-acquired URL so the
    # connector layer can prefer it as the new cursor.
    assert fetcher.pending_delta_link == refresh_delta
    # Three requests fired: original (410) → fallback filter → refresh.
    assert len(call_log) == 3
    assert call_log[0] == stored


def test_fallback_disabled_when_window_zero_raises_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fallback_window_days=0`` opts out of recovery — the
    :class:`ConnectorFailedError` propagates to the caller instead of
    silently swallowing the failure."""
    stored = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=EXPIRED"

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": {"code": "syncStateInvalid"}})

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler, fallback_window_days=0)
    try:
        with pytest.raises(ConnectorFailedError, match="fallback is disabled"):
            list(fetcher.fetch_chat_messages(delta_link=stored))
    finally:
        fetcher.close()


def test_negative_fallback_window_snaps_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative ``fallback_window_days`` (likely a typo) is clamped to
    the default rather than silently disabling fallback."""
    stored = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=EXPIRED"
    refresh_delta = f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=FRESH"

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stored:
            return httpx.Response(410, json={"error": {"code": "syncStateInvalid"}})
        if "lastModifiedDateTime+ge" in url or "lastModifiedDateTime%20ge" in url:
            return httpx.Response(200, json={"value": []})
        # Refresh delta link page.
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": refresh_delta})

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler, fallback_window_days=-5)
    try:
        # Drains without raising — fallback ran and refresh succeeded.
        yields = list(fetcher.fetch_chat_messages(delta_link=stored))
    finally:
        fetcher.close()

    assert yields == []
    assert fetcher.pending_delta_link == refresh_delta


# ----- rate-limit + error paths ----------------------------------------


def test_429_is_retried_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``429`` triggers exponential backoff up to the documented budget."""
    payload = {
        "value": [_message_payload()],
        "@odata.deltaLink": f"{GRAPH_BASE}/me/chats/getAllMessages?$deltaToken=NEW",
    }
    state = {"calls": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=payload)

    with patch("opshub.connectors.teams.fetcher.time.sleep") as fake_sleep:
        fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
        try:
            yields = list(fetcher.fetch_chat_messages(delta_link=None))
        finally:
            fetcher.close()

    assert len(yields) == 1
    # ``time.sleep`` honoured Retry-After.
    fake_sleep.assert_called_once_with(1)


def test_unrecoverable_4xx_raises_connector_failed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx other than 410 / 429 surfaces as :class:`ConnectorFailedError`
    immediately — fail-fast posture per ADR-0010 §責務 4."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "Forbidden"}})

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    try:
        with pytest.raises(ConnectorFailedError) as excinfo:
            list(fetcher.fetch_chat_messages(delta_link=None))
    finally:
        fetcher.close()

    # Token must never appear in the surfaced error message.
    assert "bearer-fake" not in str(excinfo.value)
    # The status code is surfaced for operator debugging.
    assert "403" in str(excinfo.value)


def test_token_never_appears_in_transport_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport errors surface only the exception type, never the token."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport failure")

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    try:
        with pytest.raises(ConnectorFailedError) as excinfo:
            list(fetcher.fetch_chat_messages(delta_link=None))
    finally:
        fetcher.close()

    assert "bearer-fake" not in str(excinfo.value)
    # Type name appears so operators can map back to documentation.
    assert "ConnectError" in str(excinfo.value)


def test_close_releases_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``close()`` shuts the underlying ``httpx.Client`` deterministically.

    Long-lived service processes call this between sync runs; we pin
    that the method exists and does not raise so a future refactor
    that re-shapes the client lifecycle catches this contract.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    fetcher, _auth, _requests = _make_fetcher(monkeypatch, _handler)
    fetcher.close()
    # Idempotent close is acceptable but not required; we simply
    # assert the method completes without raising.
