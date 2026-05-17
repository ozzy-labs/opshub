"""Tests for ``opshub.connectors.box.fetcher`` (Phase 7 step C2).

The fetcher wraps Box's Events API (``client.events().get_events(...)``)
for incremental sync. Tests here exercise:

* Lazy SDK import: a missing ``boxsdk`` install must raise
  :class:`ConfigError` pointing at the extras name.
* Event filtering: only ``ITEM_*`` events are yielded; user / admin
  events (LOGIN, COMMENT_CREATE, …) are dropped.
* Cursor handling: the first sync (cursor=None) passes ``"now"`` to
  Box; subsequent syncs advance via the API-returned
  ``next_stream_position``.
* Path resolution from ``source.path_collection`` — Box's synthetic
  "All Files" root is skipped.
* Retry / backoff: a single 401 forces a token refresh and one retry;
  repeated 401 raises :class:`ConnectorFailedError`. 429 honours
  ``Retry-After`` and falls back to exponential backoff after that.
* No real Box API calls — every test substitutes a programmable fake
  Client via the ``client_factory`` seam.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "boxsdk",
    reason="Box connector tests require the 'connectors-box' extras",
)

from boxsdk.exception import BoxAPIException

from opshub.connectors.box.fetcher import (
    CURSOR_BOX_STREAM_POSITION,
    BoxFetcher,
    RawBoxEvent,
)
from opshub.core.errors import ConfigError, ConnectorFailedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str = "ev-1",
    event_type: str = "ITEM_CREATE",
    item_id: str = "12345",
    item_type: str = "file",
    item_name: str = "report.pdf",
    path_entries: list[str] | None = None,
    actor_id: str = "u-1",
    actor_name: str = "Alice",
    web_link: str | None = "https://app.box.com/file/12345",
    created_at: str = "2026-05-17T10:00:00Z",
) -> dict[str, Any]:
    """Build one Box-event-shaped dict with sensible defaults.

    Mirrors the JSON the real ``GET /events`` returns — every test
    overrides only the fields it cares about. ``path_entries`` defaults
    to ``["All Files", "Documents", "Reports"]`` so the path-resolution
    branch is exercised by the bulk of tests.
    """
    if path_entries is None:
        path_entries = ["All Files", "Documents", "Reports"]
    return {
        "event_id": event_id,
        "event_type": event_type,
        "created_at": created_at,
        "created_by": {"id": actor_id, "name": actor_name},
        "source": {
            "id": item_id,
            "type": item_type,
            "name": item_name,
            "web_link": web_link,
            "path_collection": {
                "entries": [{"name": entry} for entry in path_entries],
            },
        },
    }


def _make_response(
    entries: list[dict[str, Any]], *, next_stream_position: str = "1000"
) -> dict[str, Any]:
    return {
        "chunk_size": len(entries),
        "next_stream_position": next_stream_position,
        "entries": entries,
    }


class _FakeEvents:
    """Programmable ``client.events()`` double.

    ``responses`` and ``exceptions`` are consumed in order — each
    ``get_events`` call pops the head. Tests interleave them by index
    (a ``None`` in ``exceptions`` means "use the next response").
    """

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        exceptions: list[BoxAPIException | None] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.exceptions = list(exceptions or [])
        self.calls: list[dict[str, Any]] = []

    def get_events(
        self,
        *,
        limit: int,
        stream_position: str | int,
    ) -> dict[str, Any]:
        self.calls.append({"limit": limit, "stream_position": stream_position})
        if self.exceptions:
            exc = self.exceptions.pop(0)
            if exc is not None:
                raise exc
        if not self.responses:
            raise AssertionError("FakeEvents exhausted — test wanted more calls")
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, events: _FakeEvents) -> None:
        self._events = events

    def events(self) -> _FakeEvents:
        return self._events


def _build_fetcher(
    *,
    events: _FakeEvents,
    auth: Any = None,
    sleep: Any = None,
) -> BoxFetcher:
    """Construct a :class:`BoxFetcher` wired to a programmable fake client.

    The real ``BoxAuth`` is replaced with a :class:`MagicMock` carrying
    just the attributes the fetcher touches: ``invalidate_cached_token``
    (called on 401) and the ``build_authenticated_client`` factory
    (overridden via the ``client_factory`` constructor seam, so the
    mock is mostly there to absorb the 401 invalidation call).
    """
    if auth is None:
        auth = MagicMock(spec=["invalidate_cached_token", "build_authenticated_client"])
    client = _FakeClient(events)
    return BoxFetcher(
        auth,
        sleep=sleep if sleep is not None else (lambda _: None),
        client_factory=lambda: client,
    )


# ---------------------------------------------------------------------------
# Construction / lazy import
# ---------------------------------------------------------------------------


def test_cursor_constant_value() -> None:
    """Pin the public cursor key so C3 mapper + sync use the same name."""
    assert CURSOR_BOX_STREAM_POSITION == "box:stream_position"


def test_init_raises_when_boxsdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``boxsdk`` install raises :class:`ConfigError`.

    Same simulation pattern as ``test_auth.test_init_raises_when_boxsdk_missing``:
    install a meta-path finder that fails to resolve every ``boxsdk*``
    name, evict the cached modules, and assert the fetcher's lazy
    import surfaces the actionable error.
    """
    import sys

    class _BrokenFinder:
        def find_spec(self, name: str, _path: object, _target: object = None) -> None:
            if name == "boxsdk" or name.startswith("boxsdk."):
                raise ImportError(f"simulated missing {name}")
            return None

    for cached in [m for m in sys.modules if m == "boxsdk" or m.startswith("boxsdk.")]:
        monkeypatch.delitem(sys.modules, cached, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BrokenFinder(), *sys.meta_path])

    with pytest.raises(ConfigError) as excinfo:
        BoxFetcher(MagicMock())

    assert "connectors-box" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_events_yields_each_event() -> None:
    """Three item events on one page → three yields, all sharing the cursor."""
    events = _FakeEvents(
        responses=[
            _make_response(
                [
                    _make_event(event_id="e1"),
                    _make_event(event_id="e2"),
                    _make_event(event_id="e3"),
                ],
                next_stream_position="42",
            )
        ]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="10"))

    assert [e.event_id for e, _ in yielded] == ["e1", "e2", "e3"]
    # Box's API contract: the stream_position advances once per page,
    # not once per event — every yield on this page carries the same
    # ``next_stream_position`` so the caller can persist after each
    # commit and not lose progress across crashes.
    assert {pos for _, pos in yielded} == {"42"}


def test_fetch_events_uses_now_when_no_cursor() -> None:
    """First sync (cursor=None) requests ``stream_position="now"``.

    Phase 7 plan §2.3 C2: the MVP does NOT backfill historical events;
    operators run repeated syncs to catch up. The ``"now"`` marker is
    Box's documented way to ask for the current cursor without events.
    """
    events = _FakeEvents(responses=[_make_response([], next_stream_position="now-marker-1")])
    fetcher = _build_fetcher(events=events)

    list(fetcher.fetch_events(stream_position=None))

    assert events.calls == [{"limit": 100, "stream_position": "now"}]


def test_fetch_events_passes_provided_cursor() -> None:
    """A non-None cursor is forwarded verbatim to Box."""
    events = _FakeEvents(responses=[_make_response([])])
    fetcher = _build_fetcher(events=events)

    list(fetcher.fetch_events(stream_position="prev-cursor-9999"))

    assert events.calls[0]["stream_position"] == "prev-cursor-9999"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_fetch_events_skips_non_item_events() -> None:
    """LOGIN / COMMENT_CREATE / etc. are dropped silently.

    Phase 7 MVP only ingests file & folder activity. Other event types
    are returned by ``stream_type=all`` and must be skipped here — they
    are tracked for Phase 7.x (user / admin event mappers).
    """
    events = _FakeEvents(
        responses=[
            _make_response(
                [
                    _make_event(event_id="kept-1", event_type="ITEM_CREATE"),
                    _make_event(event_id="dropped-login", event_type="LOGIN"),
                    _make_event(event_id="dropped-comment", event_type="COMMENT_CREATE"),
                    _make_event(event_id="kept-2", event_type="ITEM_RENAME"),
                ]
            )
        ]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert [e.event_id for e, _ in yielded] == ["kept-1", "kept-2"]


def test_fetch_events_drops_events_with_missing_source() -> None:
    """Defensive: events without a ``source`` are skipped, not raised.

    Box occasionally returns ITEM_TRASH events whose underlying item
    is already purged — ``source`` comes back as ``None``. We skip the
    event so the rest of the page still flows through.
    """
    events = _FakeEvents(
        responses=[
            _make_response(
                [
                    {
                        "event_id": "broken",
                        "event_type": "ITEM_TRASH",
                        "source": None,
                        "created_at": "2026-05-17T10:00:00Z",
                        "created_by": {"id": "u", "name": "U"},
                    },
                    _make_event(event_id="ok"),
                ]
            )
        ]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert [e.event_id for e, _ in yielded] == ["ok"]


# ---------------------------------------------------------------------------
# Cursor advancement
# ---------------------------------------------------------------------------


def test_fetch_events_advances_stream_position() -> None:
    """The final yield carries the API-returned ``next_stream_position``."""
    events = _FakeEvents(
        responses=[
            _make_response([_make_event(event_id="solo")], next_stream_position="advanced-9999")
        ]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="prev"))

    assert yielded[-1][1] == "advanced-9999"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_fetch_events_resolves_path_from_path_collection() -> None:
    """``path_collection`` → ``/Documents/Reports/foo.pdf``.

    The "All Files" root segment is skipped because it is a Box
    UI convention rather than part of the human-meaningful path the
    operator sees in the web UI.
    """
    events = _FakeEvents(
        responses=[
            _make_response(
                [
                    _make_event(
                        item_name="foo.pdf",
                        path_entries=["All Files", "Documents", "Reports"],
                    )
                ]
            )
        ]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert yielded[0][0].item_path == "/Documents/Reports/foo.pdf"


def test_fetch_events_resolves_root_level_item() -> None:
    """An item directly under the root → ``/<name>``."""
    events = _FakeEvents(
        responses=[_make_response([_make_event(item_name="top.txt", path_entries=["All Files"])])]
    )
    fetcher = _build_fetcher(events=events)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert yielded[0][0].item_path == "/top.txt"


# ---------------------------------------------------------------------------
# 401 retry / fail-fast
# ---------------------------------------------------------------------------


def test_fetch_events_retries_once_on_401() -> None:
    """First 401 → invalidate token + retry → success.

    The auth helper's :meth:`invalidate_cached_token` must be called
    so the next ``build_authenticated_client`` round trip refreshes
    via the stored refresh token rather than reusing the dead one.
    """
    auth = MagicMock(spec=["invalidate_cached_token", "build_authenticated_client"])
    events = _FakeEvents(
        responses=[_make_response([_make_event(event_id="post-refresh")])],
        exceptions=[BoxAPIException(status=401)],
    )
    fetcher = _build_fetcher(events=events, auth=auth)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert [e.event_id for e, _ in yielded] == ["post-refresh"]
    assert auth.invalidate_cached_token.called


def test_fetch_events_raises_on_repeated_401() -> None:
    """401 → invalidate + retry → 401 again → :class:`ConnectorFailedError`.

    The refresh token itself is dead; no further retries make sense.
    The error message must steer the operator at the re-auth command.
    """
    auth = MagicMock(spec=["invalidate_cached_token", "build_authenticated_client"])
    events = _FakeEvents(
        exceptions=[
            BoxAPIException(status=401),
            BoxAPIException(status=401),
        ]
    )
    fetcher = _build_fetcher(events=events, auth=auth)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_events(stream_position="x"))

    assert "401" in str(excinfo.value)
    assert "re-auth" in str(excinfo.value) or "auth set" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 429 backoff
# ---------------------------------------------------------------------------


def test_fetch_events_respects_retry_after_on_429() -> None:
    """A 429 with ``Retry-After: 5`` sleeps 5s then retries successfully."""
    sleeps: list[float] = []
    events = _FakeEvents(
        responses=[_make_response([_make_event(event_id="after-429")])],
        exceptions=[BoxAPIException(status=429, headers={"Retry-After": "5"})],
    )
    fetcher = _build_fetcher(events=events, sleep=sleeps.append)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert [e.event_id for e, _ in yielded] == ["after-429"]
    assert sleeps == [5.0]


def test_fetch_events_uses_backoff_schedule_when_no_retry_after() -> None:
    """No ``Retry-After`` → fall back to 1s/2s/4s schedule.

    Two transient 429s exhaust schedule slots ``[0]`` and ``[1]``;
    the third attempt succeeds without sleeping further.
    """
    sleeps: list[float] = []
    events = _FakeEvents(
        responses=[_make_response([_make_event(event_id="third-time")])],
        exceptions=[
            BoxAPIException(status=429),
            BoxAPIException(status=429),
        ],
    )
    fetcher = _build_fetcher(events=events, sleep=sleeps.append)

    yielded = list(fetcher.fetch_events(stream_position="x"))

    assert [e.event_id for e, _ in yielded] == ["third-time"]
    assert sleeps == [1.0, 2.0]


def test_fetch_events_exhausts_retries_on_persistent_429() -> None:
    """Three consecutive 429s → :class:`ConnectorFailedError`.

    Three is the max attempt count (one per :data:`_BACKOFF_SECONDS`
    slot). The error message names "retries" so operators can grep for
    it in ``opshub events`` output without needing to know the exact
    Box-side cause.
    """
    sleeps: list[float] = []
    events = _FakeEvents(
        exceptions=[
            BoxAPIException(status=429),
            BoxAPIException(status=429),
            BoxAPIException(status=429),
        ]
    )
    fetcher = _build_fetcher(events=events, sleep=sleeps.append)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_events(stream_position="x"))

    assert "retries" in str(excinfo.value)
    # Three attempts → two sleeps in between (no sleep before attempt 0).
    # Implementation sleeps after each retryable failure, so all three
    # attempts each schedule one sleep before the next loop iteration —
    # the loop just falls through to the exhausted-retries branch.
    assert len(sleeps) == 3


# ---------------------------------------------------------------------------
# Other failure modes
# ---------------------------------------------------------------------------


def test_fetch_events_raises_on_500() -> None:
    """A non-retryable 5xx is surfaced as :class:`ConnectorFailedError` immediately.

    We deliberately do NOT retry 500s in this MVP: a real Box outage
    is better surfaced to the operator than masked by silent retries
    that bloat the sync time. Phase 7.x can revisit if Box's 500 rate
    proves disruptive in practice.
    """
    events = _FakeEvents(exceptions=[BoxAPIException(status=500)])
    fetcher = _build_fetcher(events=events)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_events(stream_position="x"))

    assert "500" in str(excinfo.value)


# ---------------------------------------------------------------------------
# RawBoxEvent shape
# ---------------------------------------------------------------------------


def test_raw_box_event_carries_actor_and_url() -> None:
    """The yielded :class:`RawBoxEvent` carries the actor + web URL.

    The C3 mapper consumes these directly into ``SourceObserved``
    fields, so a regression here would silently drop attribution
    metadata from the projection.
    """
    events = _FakeEvents(
        responses=[
            _make_response(
                [
                    _make_event(
                        actor_id="u-42",
                        actor_name="Bob",
                        web_link="https://app.box.com/folder/9",
                    )
                ]
            )
        ]
    )
    fetcher = _build_fetcher(events=events)

    (event, _cursor) = next(iter(fetcher.fetch_events(stream_position="x")))

    assert isinstance(event, RawBoxEvent)
    assert event.actor_id == "u-42"
    assert event.actor_name == "Bob"
    assert event.web_url == "https://app.box.com/folder/9"
