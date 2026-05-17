"""Tests for ``opshub.connectors.ms365.fetcher`` (Phase 7 step B2).

Every test routes Microsoft Graph traffic through
:class:`httpx.MockTransport` so the suite never reaches a real
``graph.microsoft.com`` endpoint — mirrors the Phase 6 Ollama client
test pattern (``tests/unit/llm/test_ollama_client.py``). The
``connectors-ms365`` extras include both ``msal`` and ``httpx``; the
fetcher module itself only needs ``httpx``, so the importorskip below
gates on that.

The MS365Auth dependency is stubbed with a tiny dataclass-like helper
that records token reads — the real auth surface is covered by Phase 7
step B1's test module. We exercise the fetcher's interaction with
``MS365Auth`` (token retrieval + 401 cache-bust) by counting calls to
the stub.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="MS365 fetcher tests require the 'connectors-ms365' extras",
)

import httpx

from opshub.connectors.ms365.fetcher import (
    CURSOR_CALENDAR,
    CURSOR_ONEDRIVE,
    CURSOR_OUTLOOK,
    GRAPH_BASE,
    MS365Fetcher,
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)
from opshub.core.errors import ConnectorFailedError

# ----- helpers -------------------------------------------------------------


class _StubAuth:
    """Minimal stand-in for :class:`MS365Auth`.

    ``get_access_token`` returns a sentinel string and counts calls;
    the fetcher uses ``self._token = None`` to bust the access-token
    cache on 401, so we expose the same attribute and assert tests
    re-program it as appropriate.
    """

    def __init__(self, tokens: list[str] | None = None) -> None:
        self._tokens: list[str] = list(tokens) if tokens else ["bearer-1"]
        self._index = 0
        self.calls = 0
        # Mirrors the real auth helper's in-memory cache attribute so
        # the fetcher's ``self._auth._token = None`` cache-bust resolves.
        self._token: object | None = "sentinel"

    def get_access_token(self) -> str:
        self.calls += 1
        token = self._tokens[min(self._index, len(self._tokens) - 1)]
        self._index += 1
        return token


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Patch :class:`httpx.Client` so the fetcher's client uses ``handler``.

    The fetcher constructs its own ``httpx.Client`` inside ``__init__``
    (matching the Ollama client pattern), so we monkeypatch the class
    factory before constructing the fetcher. Every request is recorded
    into the returned list for downstream assertions.
    """
    requests: list[httpx.Request] = []
    real_client_cls = httpx.Client

    def _recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

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
    auth: _StubAuth | None = None,
) -> tuple[MS365Fetcher, _StubAuth, list[httpx.Request]]:
    """Build a fetcher whose HTTP client uses the given handler."""
    requests = _patch_httpx_client(monkeypatch, handler)
    if auth is None:
        auth = _StubAuth()
    fetcher = MS365Fetcher(auth=auth)  # type: ignore[arg-type]
    return fetcher, auth, requests


def _event_payload(
    *,
    event_id: str,
    subject: str,
    last_modified: str,
    attendees: int = 0,
) -> dict[str, Any]:
    """Build a typical ``/me/calendar/events`` value entry."""
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": "2026-05-17T10:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-17T11:00:00.0000000", "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": f"a{i}@x"}} for i in range(attendees)],
        "webLink": f"https://outlook.office365.com/owa/?itemid={event_id}",
        "lastModifiedDateTime": last_modified,
    }


def _onedrive_payload(
    *,
    item_id: str,
    name: str,
    last_modified: str,
    parent_path: str = "/drive/root:",
    deleted: bool = False,
) -> dict[str, Any]:
    """Build a typical ``/me/drive/root/delta`` value entry."""
    payload: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "parentReference": {"path": parent_path},
        "webUrl": f"https://onedrive.live.com/?id={item_id}",
        "lastModifiedDateTime": last_modified,
    }
    if deleted:
        payload["deleted"] = {"state": "deleted"}
    return payload


def _outlook_payload(
    *,
    message_id: str,
    subject: str,
    received: str,
    sender_address: str = "alice@example.com",
    body_preview: str = "hello",
) -> dict[str, Any]:
    """Build a typical ``/me/messages`` value entry."""
    return {
        "id": message_id,
        "subject": subject,
        "bodyPreview": body_preview,
        "sender": {"emailAddress": {"address": sender_address}},
        "receivedDateTime": received,
        "webLink": f"https://outlook.office365.com/owa/?itemid={message_id}",
    }


# ----- module surface ------------------------------------------------------


def test_cursor_keys_are_stable() -> None:
    """Cursor keys are the contract between B2 fetcher + B3 mapper / wiring.

    The three keys land in the ``connector_cursors`` projection at
    sync time; renaming them silently would orphan previously stored
    cursors. The test pins the values to make any drift visible.
    """
    assert CURSOR_CALENDAR == "ms365:calendar"
    assert CURSOR_ONEDRIVE == "ms365:onedrive"
    assert CURSOR_OUTLOOK == "ms365:outlook"


def test_module_does_not_import_httpx_at_top_level() -> None:
    """The fetcher module imports httpx lazily inside ``__init__``.

    Mirrors :func:`tests.unit.llm.test_ollama_client.test_module_imports_without_extras_marker`
    — the cold-start guard (``tests/integration/test_cli_imports.py``)
    only inspects ``opshub.cli.*`` directly, but pinning the lazy-import
    discipline at the connector layer too keeps the rule applied
    uniformly across the codebase.
    """
    import opshub.connectors.ms365.fetcher as fetcher_module

    assert "httpx" not in vars(fetcher_module), (
        "MS365 fetcher exposes 'httpx' at module level; lazy import broken"
    )


# ----- calendar endpoint ---------------------------------------------------


def test_fetch_calendar_events_yields_each_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-page response of 3 events yields 3 tuples."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1.0/me/calendar/events"
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="a", last_modified="2026-05-10T01:00:00Z"),
                    _event_payload(event_id="2", subject="b", last_modified="2026-05-11T01:00:00Z"),
                    _event_payload(event_id="3", subject="c", last_modified="2026-05-12T01:00:00Z"),
                ]
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_calendar_events(since_iso=None))
    assert len(yields) == 3
    events = [y[0] for y in yields]
    assert all(isinstance(e, RawCalendarEvent) for e in events)
    assert [e.id for e in events] == ["1", "2", "3"]
    # Final cursor is the max lastModifiedDateTime seen.
    assert yields[-1][1] == "2026-05-12T01:00:00Z"


def test_fetch_calendar_events_paginates_via_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """First page returns 2 + ``@odata.nextLink``, second returns 1."""
    page_two_url = "https://graph.microsoft.com/v1.0/me/calendar/events?$skiptoken=PAGE2&$top=50"

    def _handler(request: httpx.Request) -> httpx.Response:
        if "skiptoken=PAGE2" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        _event_payload(
                            event_id="3", subject="c", last_modified="2026-05-12T01:00:00Z"
                        )
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="a", last_modified="2026-05-10T01:00:00Z"),
                    _event_payload(event_id="2", subject="b", last_modified="2026-05-11T01:00:00Z"),
                ],
                "@odata.nextLink": page_two_url,
            },
        )

    fetcher, _auth, requests = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_calendar_events(since_iso=None))
    assert [y[0].id for y in yields] == ["1", "2", "3"]
    assert len(requests) == 2  # two pages walked


def test_fetch_calendar_events_advances_cursor_to_max_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``new_cursor_iso`` on the final yield is the maximum modified time."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="a", last_modified="2026-05-10T01:00:00Z"),
                    _event_payload(event_id="2", subject="b", last_modified="2026-05-15T01:00:00Z"),
                    _event_payload(event_id="3", subject="c", last_modified="2026-05-12T01:00:00Z"),
                ]
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_calendar_events(since_iso="2026-05-01T00:00:00Z"))
    cursors = [y[1] for y in yields]
    # Monotonic non-decreasing; final value is the highest seen.
    assert cursors == [
        "2026-05-10T01:00:00Z",
        "2026-05-15T01:00:00Z",
        "2026-05-15T01:00:00Z",
    ]


def test_fetch_calendar_events_uses_since_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``$filter`` query param echoes the supplied ``since_iso``."""
    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        # ``request.url.params`` preserves order but exposes a multidict
        # interface; for assertion we coerce to a plain dict.
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"value": []})

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    list(fetcher.fetch_calendar_events(since_iso="2026-05-15T00:00:00Z"))
    assert "lastModifiedDateTime ge 2026-05-15T00:00:00Z" in captured["$filter"]
    assert captured["$orderby"] == "lastModifiedDateTime"


# ----- onedrive endpoint ---------------------------------------------------


def test_fetch_onedrive_changes_uses_delta_link_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``delta_link`` is supplied the first call hits that URL directly."""
    stored_delta = "https://graph.microsoft.com/v1.0/me/drive/root/delta?token=PREV"

    def _handler(request: httpx.Request) -> httpx.Response:
        # First call MUST use the stored delta URL verbatim, not the
        # root URL — that is the whole point of the delta-link replay.
        assert "token=PREV" in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [
                    _onedrive_payload(
                        item_id="f1", name="doc.txt", last_modified="2026-05-10T01:00:00Z"
                    ),
                ],
                "@odata.deltaLink": (
                    "https://graph.microsoft.com/v1.0/me/drive/root/delta?token=NEW"
                ),
            },
        )

    fetcher, _auth, requests = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_onedrive_changes(delta_link=stored_delta))
    assert len(yields) == 1
    assert yields[0][0].id == "f1"
    # The cursor on the final page advances to the new delta link.
    assert yields[0][1] == "https://graph.microsoft.com/v1.0/me/drive/root/delta?token=NEW"
    assert len(requests) == 1


def test_fetch_onedrive_changes_starts_at_root_on_first_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``delta_link`` is None the first call hits ``/me/drive/root/delta``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1.0/me/drive/root/delta"
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": f"{GRAPH_BASE}/me/drive/root/delta?token=FIRST",
            },
        )

    fetcher, _auth, requests = _make_fetcher(monkeypatch, _handler)
    list(fetcher.fetch_onedrive_changes(delta_link=None))
    assert len(requests) == 1


def test_fetch_onedrive_changes_skips_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Items with a ``deleted`` facet are filtered out (Phase 7 MVP)."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _onedrive_payload(
                        item_id="alive", name="ok.txt", last_modified="2026-05-10T01:00:00Z"
                    ),
                    _onedrive_payload(
                        item_id="gone",
                        name="tombstone.txt",
                        last_modified="2026-05-10T02:00:00Z",
                        deleted=True,
                    ),
                ],
                "@odata.deltaLink": f"{GRAPH_BASE}/me/drive/root/delta?token=NEW",
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_onedrive_changes(delta_link=None))
    assert [y[0].id for y in yields] == ["alive"]


def test_fetch_onedrive_changes_returns_new_delta_link_on_final_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor on the final-page yield is the freshly-returned delta link."""
    new_delta = f"{GRAPH_BASE}/me/drive/root/delta?token=NEW"

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _onedrive_payload(
                        item_id="f1", name="a.txt", last_modified="2026-05-10T01:00:00Z"
                    )
                ],
                "@odata.deltaLink": new_delta,
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_onedrive_changes(delta_link=None))
    assert yields[-1][1] == new_delta


def test_fetch_onedrive_changes_paginates_via_next_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walks ``@odata.nextLink`` then picks up ``@odata.deltaLink`` on final page."""
    page_two = f"{GRAPH_BASE}/me/drive/root/delta?token=PAGE2"

    def _handler(request: httpx.Request) -> httpx.Response:
        if "token=PAGE2" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        _onedrive_payload(
                            item_id="f2", name="b.txt", last_modified="2026-05-11T01:00:00Z"
                        )
                    ],
                    "@odata.deltaLink": f"{GRAPH_BASE}/me/drive/root/delta?token=FINAL",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    _onedrive_payload(
                        item_id="f1", name="a.txt", last_modified="2026-05-10T01:00:00Z"
                    )
                ],
                "@odata.nextLink": page_two,
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_onedrive_changes(delta_link=None))
    assert [y[0].id for y in yields] == ["f1", "f2"]
    # The first yield uses the in-flight cursor (root URL) because we
    # haven't reached the final page yet; the second yield (on the
    # final page) advances to the new delta link.
    assert yields[0][1].endswith("/me/drive/root/delta")
    assert yields[1][1].endswith("token=FINAL")


# ----- outlook endpoint ----------------------------------------------------


def test_fetch_outlook_messages_yields_each_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-page response of 2 messages yields 2 tuples."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _outlook_payload(
                        message_id="m1", subject="hi", received="2026-05-10T01:00:00Z"
                    ),
                    _outlook_payload(
                        message_id="m2", subject="bye", received="2026-05-11T01:00:00Z"
                    ),
                ]
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_outlook_messages(since_iso="2026-05-01T00:00:00Z"))
    messages = [y[0] for y in yields]
    assert all(isinstance(m, RawOutlookMessage) for m in messages)
    assert [m.id for m in messages] == ["m1", "m2"]
    assert yields[-1][1] == "2026-05-11T01:00:00Z"


def test_fetch_outlook_messages_uses_select_for_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``$select`` query param pins the projection list (ADR-0005)."""
    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"value": []})

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    list(fetcher.fetch_outlook_messages(since_iso="2026-05-15T00:00:00Z"))
    # The exact set of fields the mapper consumes — every name must
    # be present or the mapper would silently see empty strings.
    select = captured["$select"]
    for field in ("id", "subject", "bodyPreview", "sender", "receivedDateTime", "webLink"):
        assert field in select, f"{field!r} missing from $select={select!r}"
    assert "lastModifiedDateTime ge" not in captured["$filter"]
    assert captured["$filter"] == "receivedDateTime ge 2026-05-15T00:00:00Z"


# ----- auth + retry plumbing ----------------------------------------------


def test_request_retries_once_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 on the first call triggers cache-bust + retry; second call succeeds."""
    call_count = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="x", last_modified="2026-05-10T01:00:00Z")
                ]
            },
        )

    auth = _StubAuth(tokens=["stale", "fresh"])
    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler, auth=auth)
    # ``_auth._token`` is initially the sentinel — the 401 path sets
    # it to ``None`` to force the auth helper to re-issue.
    yields = list(fetcher.fetch_calendar_events(since_iso=None))
    assert len(yields) == 1
    assert call_count["n"] == 2  # one 401 + one 200
    # The fetcher set ``_token`` to None before the retry; the next
    # ``get_access_token`` call returned ``"fresh"``, so the second
    # call's bearer was the refreshed token.
    assert auth.calls == 2
    # ``_token`` lives on the stub; reading it from outside the class
    # would normally trip pyright's reportPrivateUsage, but the stub
    # *is* a test scaffold built for exactly this assertion. Using
    # ``getattr`` keeps the privacy check silent without weakening the
    # behavioural check.
    assert getattr(auth, "_token") is None  # noqa: B009


def test_request_raises_on_repeated_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive 401s fail out as ``ConnectorFailedError``."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_calendar_events(since_iso=None))
    # The message identifies the verb / URL but never echoes the token.
    msg = str(excinfo.value)
    assert "MS365" in msg
    assert "bearer" not in msg.lower()


def test_request_respects_retry_after_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 with ``Retry-After: 1`` sleeps for that many seconds then retries."""
    call_count = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "1"},
                json={"error": {"code": "TooManyRequests"}},
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="x", last_modified="2026-05-10T01:00:00Z")
                ]
            },
        )

    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    sleep_mock = MagicMock(side_effect=_record_sleep)
    monkeypatch.setattr("opshub.connectors.ms365.fetcher.time.sleep", sleep_mock)

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_calendar_events(since_iso=None))

    assert len(yields) == 1
    assert call_count["n"] == 2
    assert sleeps == [1]


def test_request_falls_back_when_retry_after_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 without ``Retry-After`` uses exponential backoff (``2 ** attempt``)."""
    call_count = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, json={"error": {"code": "TooManyRequests"}})
        return httpx.Response(
            200,
            json={
                "value": [
                    _event_payload(event_id="1", subject="x", last_modified="2026-05-10T01:00:00Z")
                ]
            },
        )

    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("opshub.connectors.ms365.fetcher.time.sleep", _record_sleep)

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    list(fetcher.fetch_calendar_events(since_iso=None))
    # ``2 ** 0`` on the first failed attempt → 1 second fallback.
    assert sleeps == [1]


def test_request_exhausts_retries_on_persistent_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three consecutive 429s fail out as ``ConnectorFailedError``."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"error": {"code": "TooManyRequests"}},
        )

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("opshub.connectors.ms365.fetcher.time.sleep", _no_sleep)
    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_calendar_events(since_iso=None))
    assert "after 3 attempts" in str(excinfo.value)


def test_request_wraps_other_http_errors_into_connector_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx with no retry path surfaces as ``ConnectorFailedError``."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "InternalServerError"}})

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_calendar_events(since_iso=None))
    assert "500" in str(excinfo.value)


def test_request_attaches_bearer_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request carries the ``Authorization: Bearer <token>`` header.

    Pins the auth wiring — without this header the Graph API rejects
    every request as 401, which would mask any other regression.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        # The header must be present and well-formed; we assert via the
        # request the handler receives so a missing header surfaces as
        # an assertion error here, not a downstream 401.
        assert request.headers["Authorization"] == "Bearer bearer-1"
        return httpx.Response(200, json={"value": []})

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    list(fetcher.fetch_calendar_events(since_iso=None))


def test_onedrive_helpers_expose_normalised_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """``RawOneDriveItem.path`` joins parent reference + name with ``/``."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _onedrive_payload(
                        item_id="f1",
                        name="report.docx",
                        last_modified="2026-05-10T01:00:00Z",
                        parent_path="/drive/root:/Projects",
                    )
                ],
                "@odata.deltaLink": f"{GRAPH_BASE}/me/drive/root/delta?token=NEW",
            },
        )

    fetcher, _auth, _ = _make_fetcher(monkeypatch, _handler)
    yields = list(fetcher.fetch_onedrive_changes(delta_link=None))
    item = yields[0][0]
    assert isinstance(item, RawOneDriveItem)
    assert item.path == "/drive/root:/Projects/report.docx"
