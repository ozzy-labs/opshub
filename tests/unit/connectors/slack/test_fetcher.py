"""Tests for ``opshub.connectors.slack.fetcher`` (Phase 7 step A2).

:class:`SlackFetcher` paginates Slack's ``conversations.history`` API
for a configured set of channels and yields normalised
:class:`RawSlackMessage` rows along with a fresh cursor per message.
The behaviour worth pinning:

1. Construction fails fast on an empty channel list (operator
   misconfiguration check, not a silently-no-op sync).
2. ``conversations.history`` is called once per channel; messages
   are yielded oldest-first so the cursor (``ts``) advances
   monotonically as the caller commits each event.
3. The ``oldest`` cursor is passed straight through (with
   ``inclusive=False``) so the boundary message is never re-fetched.
4. ``has_more=True`` triggers a follow-up call using the API's own
   pagination cursor (``next_cursor``) — distinct from the
   per-channel resume cursor we own.
5. HTTP 429 with ``Retry-After`` is honoured with up to three
   retries before escalating to :class:`ConnectorFailedError`. The
   1s / 2s / 4s exponential fallback applies when Slack omits the
   header.
6. Non-rate-limit ``SlackApiError`` (e.g. ``invalid_auth``) is
   surfaced immediately as :class:`ConnectorFailedError`; the bot
   token never appears in the message.
7. Per-message ``users.info`` / ``conversations.info`` /
   ``chat.getPermalink`` lookups are cached for the fetcher
   lifetime so a long channel doesn't make N + 1 API calls.
8. Bot / system messages (no ``user`` field) fall back to
   ``"unknown"`` without an API call.

The :mod:`slack_sdk` extras (``[connectors-slack]``) may not be
installed in every environment, so the file-level
``pytest.importorskip`` gates the whole module. Every Slack API
call is patched at the :class:`slack_sdk.WebClient` boundary so no
real request leaves CI (Phase 7 plan §1 #6).
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.connectors.slack.auth import SlackAuth
from opshub.connectors.slack.fetcher import (
    RawSlackMessage,
    SlackFetcher,
)
from opshub.core.errors import ConnectorFailedError

# ----- shared fixtures ---------------------------------------------------


def _auth() -> SlackAuth:
    """Construct a :class:`SlackAuth` with an explicit token.

    Using an explicit token avoids the keyring / env-var path so the
    fetcher tests stay focused on the fetch logic rather than
    re-testing auth resolution (which is already pinned in
    ``test_auth.py``).
    """
    return SlackAuth(token="xoxb-test")


def _history_response(
    messages: list[dict[str, Any]],
    *,
    has_more: bool = False,
    next_cursor: str = "",
) -> dict[str, Any]:
    """Build a :func:`conversations.history`-shaped response dict.

    Keeping the shape in one helper means a future Slack response
    change (e.g. a new top-level field) only needs to be addressed in
    one place. The default ``has_more=False`` matches the common case
    — pagination tests opt into ``has_more=True`` explicitly.
    """
    return {
        "ok": True,
        "messages": messages,
        "has_more": has_more,
        "response_metadata": {"next_cursor": next_cursor},
    }


def _slack_message(
    ts: str = "1700000000.000100",
    *,
    text: str = "hello",
    user: str | None = "U1",
    subtype: str | None = None,
    bot_id: str | None = None,
    bot_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal Slack message dict.

    Slack's API returns many more fields (``team``, ``blocks``,
    ``reactions``, ...) but the fetcher only reads ``ts`` / ``text``
    / ``user`` / ``subtype`` / ``bot_id`` / ``bot_profile``. Keeping
    the fixture minimal exposes any future field dependency
    immediately.
    """
    msg: dict[str, Any] = {"ts": ts, "text": text}
    if user is not None:
        msg["user"] = user
    if subtype is not None:
        msg["subtype"] = subtype
    if bot_id is not None:
        msg["bot_id"] = bot_id
    if bot_profile is not None:
        msg["bot_profile"] = bot_profile
    return msg


def _users_info_response(
    user_id: str = "U1",
    *,
    display_name: str = "alice",
    real_name: str = "Alice Adams",
) -> dict[str, Any]:
    """Build a :func:`users.info`-shaped response dict."""
    return {
        "ok": True,
        "user": {
            "id": user_id,
            "name": user_id.lower(),
            "profile": {
                "display_name": display_name,
                "real_name": real_name,
            },
        },
    }


def _channel_info_response(channel_id: str = "C1", *, name: str = "general") -> dict[str, Any]:
    """Build a :func:`conversations.info`-shaped response dict."""
    return {"ok": True, "channel": {"id": channel_id, "name": name}}


def _permalink_response(channel_id: str = "C1", ts: str = "1700000000.000100") -> dict[str, Any]:
    """Build a :func:`chat.getPermalink`-shaped response dict."""
    return {
        "ok": True,
        "permalink": f"https://acme.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    }


def _build_client(
    *,
    history: list[dict[str, Any]] | None = None,
    history_side_effect: Any = None,
    users_info_side_effect: Any = None,
) -> MagicMock:
    """Construct a :class:`MagicMock` WebClient with the documented response shapes.

    ``history`` is the (ordered) list of ``conversations.history``
    responses the test wants the mock to return. Multi-page tests
    pass two responses; single-page tests pass one.

    ``history_side_effect`` overrides ``history`` and lets a test
    inject :class:`SlackApiError` (e.g. for the 429 / invalid_auth
    branches).

    ``users_info_side_effect`` overrides the default cached-name
    response — useful for the bot-message fallback test where
    ``users.info`` must never be called.
    """
    client = MagicMock()
    if history_side_effect is not None:
        client.conversations_history.side_effect = history_side_effect
    else:
        client.conversations_history.side_effect = list(history or [_history_response([])])
    client.conversations_info.return_value = _channel_info_response()
    if users_info_side_effect is not None:
        client.users_info.side_effect = users_info_side_effect
    else:
        client.users_info.return_value = _users_info_response()

    # ``chat.getPermalink`` returns a deterministic URL keyed by the
    # message_ts so multi-message tests can pin per-message URLs.
    def _permalink_fn(*, channel: str, message_ts: str) -> dict[str, Any]:
        return _permalink_response(channel_id=channel, ts=message_ts)

    client.chat_getPermalink.side_effect = _permalink_fn
    return client


def _patch_webclient(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> MagicMock:
    """Patch ``slack_sdk.WebClient`` to return ``client``.

    The fetcher imports :class:`WebClient` lazily inside
    :meth:`fetch_messages` (cold-start guard), so we patch the
    attribute on the SDK module — not on the fetcher module — to
    intercept the lookup at the import site.
    """
    import slack_sdk

    factory = MagicMock(return_value=client)
    monkeypatch.setattr(slack_sdk, "WebClient", factory)
    return factory


# ----- construction ------------------------------------------------------


def test_init_requires_at_least_one_channel() -> None:
    """Empty channel list → :class:`ValueError` at construction time.

    Constructing with an empty list is almost certainly an operator
    misconfiguration (``[connectors.slack] channels = []``). Failing
    fast at construction surfaces the error before the first sync
    attempt — otherwise the sync would silently observe zero messages
    and an operator might assume the connector is "working".
    """
    with pytest.raises(ValueError, match="at least one channel"):
        SlackFetcher(_auth(), channels=[])


def test_init_takes_defensive_copy_of_channels() -> None:
    """Mutating the supplied list after construction must not affect the fetcher.

    Pinning this lets callers reuse / mutate their config lists
    without surprising side effects on in-flight fetchers.
    """
    channels = ["C1"]
    fetcher = SlackFetcher(_auth(), channels=channels)
    channels.append("C2")
    # Access the snapshot through a fetch-like call rather than the
    # private attribute so we test the observable behaviour. Empty
    # cursor + a mocked client that returns no messages is enough.
    # (Exercised in test_fetch_messages_yields_each_message; here we
    # just assert the channels attribute does not include the late
    # append.)
    assert fetcher._channels == ["C1"]  # pyright: ignore[reportPrivateUsage]


# ----- fetch_messages: happy path ---------------------------------------


def test_fetch_messages_yields_each_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three messages → three yields, with the channel_id, ts, and text intact.

    This is the smoke test that confirms the fetcher composes the
    SDK methods correctly: ``conversations.info`` once (channel name
    lookup), ``conversations.history`` once (the page), and
    ``users.info`` once (cached for the same author).
    """
    msgs = [
        _slack_message(ts="1700000003.000300", text="msg-3"),
        _slack_message(ts="1700000002.000200", text="msg-2"),
        _slack_message(ts="1700000001.000100", text="msg-1"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Slack returns newest-first; the fetcher yields oldest-first so
    # the cursor advances monotonically as the caller commits each
    # event. Pin this ordering explicitly.
    assert [r[1].ts for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
    ]
    assert [r[1].text for r in results] == ["msg-1", "msg-2", "msg-3"]
    assert all(r[0] == "C1" for r in results)
    # The yielded cursor is the message's own ts (so the caller
    # writes ``ts`` back into ``connector_cursors`` after committing
    # the SourceObserved event for that message).
    assert [r[2] for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
    ]
    # The first message's channel + author / permalink fields are
    # populated end-to-end.
    first = results[0][1]
    assert first.channel_id == "C1"
    assert first.channel_name == "general"
    assert first.user_id == "U1"
    assert first.user_display_name == "alice"
    assert first.permalink.startswith("https://acme.slack.com/archives/C1/")
    assert isinstance(first, RawSlackMessage)


def test_fetch_messages_paginates_via_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pages: the second is fetched using ``next_cursor`` from the first.

    The fetcher consumes Slack's pagination cursor (``next_cursor``)
    to walk the in-call response chunks. This is distinct from our
    per-channel resume cursor (``oldest``), which is separately
    pinned in ``test_fetch_messages_skips_already_fetched_when_oldest_set``.

    Page ordering follows the Slack API contract: page 1 is the
    **newest** chunk and ``next_cursor`` walks toward **older**
    history (each subsequent page's messages have smaller ``ts``
    than the prior page's). The pre-#339 fixture had the page
    relationship reversed which masked the cursor-rewind bug —
    see the dedicated chronological-order test below for the
    cross-page invariant the fetcher now enforces.
    """
    page1 = _history_response(
        [
            _slack_message(ts="1700000003.000300", text="msg-3"),
            _slack_message(ts="1700000002.000200", text="msg-2"),
        ],
        has_more=True,
        next_cursor="page2",
    )
    page2 = _history_response(
        [_slack_message(ts="1700000001.000100", text="msg-1")],
        has_more=False,
    )
    client = _build_client(history=[page1, page2])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Yields are sorted ts-ascending across pages (issue #339 fix)
    # so the caller's cursor advances monotonically.
    assert [r[1].text for r in results] == ["msg-1", "msg-2", "msg-3"]
    # Two ``conversations.history`` calls — the second using the
    # ``next_cursor`` returned by the first.
    assert client.conversations_history.call_count == 2
    second_kwargs = client.conversations_history.call_args_list[1].kwargs
    assert second_kwargs["cursor"] == "page2"


def test_fetch_messages_yields_chronological_order_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three pages (newest→oldest) → yields ts-ascending across page boundaries.

    Regression guard for issue #339: ``conversations.history`` is
    documented as returning the **newest** chunk on page 1 and
    walking to **older** history via ``next_cursor``. Pre-fix the
    fetcher reversed each page individually but yielded pages in
    API order, so the persisted cursor ended up at the **oldest ts
    of the oldest page** — strictly less than messages already
    committed earlier in the loop. The next sync then re-fetched
    everything between the regressed cursor and the true latest ts,
    re-emitting ``SourceObserved`` / ``ItemEnqueued`` and inflating
    ``inbox_items`` on every run.

    Post-fix the fetcher buffers all pages and yields sorted
    ts-ascending, so the caller's ``cursor = ts`` write strictly
    advances. This test pins the cross-page invariant explicitly so
    a future refactor that reintroduces per-page yielding fails
    fast.
    """
    # Page 1: newest chunk (ts 5, 4 within page, newest-first per API).
    page1 = _history_response(
        [
            _slack_message(ts="1700000005.000500", text="msg-5"),
            _slack_message(ts="1700000004.000400", text="msg-4"),
        ],
        has_more=True,
        next_cursor="page2",
    )
    # Page 2: middle chunk (ts 3, 2).
    page2 = _history_response(
        [
            _slack_message(ts="1700000003.000300", text="msg-3"),
            _slack_message(ts="1700000002.000200", text="msg-2"),
        ],
        has_more=True,
        next_cursor="page3",
    )
    # Page 3: oldest chunk (ts 1).
    page3 = _history_response(
        [_slack_message(ts="1700000001.000100", text="msg-1")],
        has_more=False,
    )
    client = _build_client(history=[page1, page2, page3])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # All five messages yielded in strict ts-ascending order across
    # the three page boundaries.
    assert [r[1].ts for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
        "1700000004.000400",
        "1700000005.000500",
    ]
    assert [r[1].text for r in results] == ["msg-1", "msg-2", "msg-3", "msg-4", "msg-5"]
    # The yielded cursor is the message's own ts so the caller's
    # ``cursor = ts`` write monotonically advances.
    assert [r[2] for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
        "1700000004.000400",
        "1700000005.000500",
    ]
    # Three API calls walked via the documented ``next_cursor`` chain.
    assert client.conversations_history.call_count == 3
    assert client.conversations_history.call_args_list[1].kwargs["cursor"] == "page2"
    assert client.conversations_history.call_args_list[2].kwargs["cursor"] == "page3"


def test_fetch_messages_single_page_yields_chronological_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single page (no ``has_more``) → yields ts-ascending, no extra API call.

    Regression guard alongside the cross-page test: the single-page
    path is the common case and must keep its
    "newest-first-on-the-wire → oldest-first-out" contract under the
    new buffer-then-sort implementation. Without this, a refactor
    that mishandles the empty ``next_cursor`` arm could silently
    drop the single-page path back to API-order yielding.
    """
    msgs = [
        # Slack API returns newest-first within a page.
        _slack_message(ts="1700000003.000300", text="msg-3"),
        _slack_message(ts="1700000002.000200", text="msg-2"),
        _slack_message(ts="1700000001.000100", text="msg-1"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert [r[1].ts for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
    ]
    # Only one ``conversations.history`` call — pagination not
    # triggered (``has_more=False`` default).
    assert client.conversations_history.call_count == 1


def test_iter_channel_skips_malformed_ts_and_preserves_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed ``ts`` rows are sorted to position 0.0 then skipped, and the
    remaining well-formed rows still yield in ts-ascending order.

    Audit followup for #345 (PR 1 of #339). The buffer-then-sort
    rewrite added two malformed-ts guards in
    :meth:`SlackFetcher._iter_channel`:

    * ``_ts_key`` returns ``0.0`` for messages with missing /
      ``None`` / non-numeric ``ts`` so the sort key is total (and
      the malformed rows land at the head of the sorted buffer).
    * The yield loop checks ``if not ts: continue`` so the
      malformed-but-sortable rows are dropped before reaching the
      caller.

    The two pieces interlock: without ``_ts_key`` returning a
    sentinel the ``sorted()`` call would raise ``TypeError`` and
    abort the entire sync; without the skip arm a malformed row
    would still reach the mapper as a synthetic ``ts=""`` message.
    Pin both halves with a single fixture that interleaves a
    missing-``ts`` row, an empty-``ts`` row, and three well-formed
    rows in API-order (newest-first within the page).

    Pre-#345 the code reversed each page individually, so the
    malformed rows happened to fall through the per-page reverse
    without crashing — but the sort that #345 introduced is total
    only because of ``_ts_key``'s sentinel. A future refactor that
    drops the sentinel (or tightens the ``except`` to a single
    exception type) without restoring the skip arm would silently
    leak malformed ``RawSlackMessage`` rows to the mapper. Pinning
    the contract here catches that regression class.
    """
    # Page mixes well-formed and malformed rows. Slack's API normally
    # guarantees ``ts`` on every message but we model a contract
    # violation here — the fetcher's defensive arm is the only line
    # between the violation and a poisoned projection.
    msgs: list[dict[str, Any]] = [
        # Well-formed: newest in the page, should land last in yield.
        _slack_message(ts="1700000003.000300", text="msg-3"),
        # Malformed: ``ts`` key entirely absent (synthetic / unknown
        # subtype). ``_ts_key`` returns 0.0; skip arm drops it.
        {"text": "no-ts row", "user": "U1"},
        # Well-formed: middle ts.
        _slack_message(ts="1700000002.000200", text="msg-2"),
        # Malformed: ``ts`` present but empty string. ``float("")``
        # raises ``ValueError``; ``_ts_key`` returns 0.0; the
        # ``if not ts: continue`` arm drops it before the mapper.
        {"ts": "", "text": "empty-ts row", "user": "U1"},
        # Well-formed: oldest ts in the page.
        _slack_message(ts="1700000001.000100", text="msg-1"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Only the three well-formed messages survive, in strict
    # ts-ascending order. The malformed rows were sorted to position
    # 0.0 by ``_ts_key`` and then dropped by the ``if not ts:
    # continue`` skip arm — they never reach the caller. The well-
    # formed rows' relative order is preserved (the malformed rows
    # being sorted to 0.0 does not invert the well-formed sequence
    # because every well-formed ``ts`` is strictly greater than 0.0).
    assert [r[1].ts for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
    ]
    assert [r[1].text for r in results] == ["msg-1", "msg-2", "msg-3"]
    # The yielded cursor mirrors the ``ts`` field so the caller's
    # ``cursor[ch] = ts`` write advances monotonically. Critically
    # the malformed rows did not contribute a ``""`` cursor write
    # that would have stalled the connector's ``_max_ts`` guard.
    assert [r[2] for r in results] == [
        "1700000001.000100",
        "1700000002.000200",
        "1700000003.000300",
    ]


def test_fetch_messages_skips_already_fetched_when_oldest_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor → ``oldest`` + ``inclusive=False`` so the boundary message is not re-fetched.

    Without ``inclusive=False`` Slack would re-yield the message at
    ``oldest`` on every resume, causing the mapper to emit a
    duplicate ``SourceObserved`` for an event we already committed
    last run. Pinning this argument prevents that regression.
    """
    client = _build_client(history=[_history_response([])])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    list(fetcher.fetch_messages(cursor_per_channel={"C1": "1700000000.000000"}))

    call_kwargs = client.conversations_history.call_args.kwargs
    assert call_kwargs["channel"] == "C1"
    assert call_kwargs["oldest"] == "1700000000.000000"
    assert call_kwargs["inclusive"] is False


def test_fetch_messages_no_cursor_skips_oldest_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-sync (``cursor=None``) → no ``oldest`` / ``inclusive`` arg passed.

    Slack documents ``oldest`` as "0" by default which means "from
    the beginning of channel history" — but passing ``inclusive``
    without ``oldest`` is a no-op anyway. We omit both for clarity
    so request shapes for first-sync vs. resume are visibly distinct
    in network captures.
    """
    client = _build_client(history=[_history_response([])])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    list(fetcher.fetch_messages(cursor_per_channel={}))

    call_kwargs = client.conversations_history.call_args.kwargs
    assert "oldest" not in call_kwargs
    assert "inclusive" not in call_kwargs


# ----- rate limiting ----------------------------------------------------


def test_fetch_messages_respects_retry_after_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with ``Retry-After`` is honoured exactly once, then the call succeeds.

    The fetcher must read ``Retry-After`` from the exception's
    ``response.headers`` (not from the response body) — that's where
    ``slack_sdk`` puts it. Pinning the sleep duration to the exact
    header value also catches a regression that defaults to the
    exponential schedule when the header is present.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 429
    bad_response.headers = {"Retry-After": "1"}
    bad_response.get.return_value = "rate_limited"
    success = _history_response([_slack_message(ts="1700000001.000100", text="recovered")])

    client = _build_client(
        history_side_effect=[
            # ``SlackApiError.__init__`` has no type annotations in
            # the SDK (3.30 series); the mypy strict ``no-untyped-call``
            # rule fires on every construction. We suppress at the
            # call site rather than relaxing the project-wide setting.
            SlackApiError(message="ratelimited", response=bad_response),  # type: ignore[no-untyped-call]
            success,
        ]
    )
    _patch_webclient(monkeypatch, client)

    # Patch ``time.sleep`` so the test runs instantly and we can
    # assert the exact backoff value used. ``opshub.connectors.slack.fetcher``
    # imports ``time`` (not ``from time import sleep``), so the
    # attribute lookup ``time.sleep`` resolves to the stdlib module's
    # ``sleep`` at call time — patching it on the stdlib module is
    # the canonical surface.
    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert [r[1].text for r in results] == ["recovered"]
    sleep_mock.assert_called_once_with(1)


def test_fetch_messages_exhausts_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive 429s → :class:`ConnectorFailedError` after the budget.

    The 1s / 2s / 4s fallback fires when ``Retry-After`` is absent.
    We assert ``time.sleep`` is called for each of the three attempts
    and that the final error mentions the channel id (so an operator
    can map the failure back to config) without echoing the token.
    """
    from slack_sdk.errors import SlackApiError

    def _make_429() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 429
        # No Retry-After: forces the exponential fallback (1 / 2 / 4).
        resp.headers = {}
        resp.get.return_value = "rate_limited"
        # See comment on the 429-then-recover test for the
        # ``no-untyped-call`` suppression rationale.
        return SlackApiError(message="ratelimited", response=resp)  # type: ignore[no-untyped-call]

    client = _build_client(history_side_effect=[_make_429(), _make_429(), _make_429()])
    _patch_webclient(monkeypatch, client)

    # Same patching strategy as the single-retry test: target the
    # stdlib ``time`` module's ``sleep`` so the fetcher's
    # ``time.sleep`` call resolves to the mock at attribute-lookup
    # time. Keeps the test millisecond-fast regardless of the
    # 1s / 2s / 4s schedule.
    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    # Exponential fallback: 1s, 2s, 4s for attempts 0 / 1 / 2.
    assert [call.args for call in sleep_mock.call_args_list] == [(1,), (2,), (4,)]
    message = str(excinfo.value)
    assert "C1" in message
    assert "rate_limited" in message
    # The bot token must never appear in the surfaced error, even
    # when the SDK exception's response carries it elsewhere.
    assert "xoxb-test" not in message


# ----- non-rate-limit API errors ----------------------------------------


def test_fetch_messages_raises_connector_failed_on_invalid_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_auth`` → :class:`ConnectorFailedError` immediately (no retry).

    Non-rate-limit Slack errors are permanent: re-running the same
    request will produce the same failure. The fetcher must
    surface the API ``error`` code (a documented short string) so
    operators can map it back to the Slack docs without exposing
    the bot token.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 401
    bad_response.headers = {}
    bad_response.get.return_value = "invalid_auth"

    client = _build_client(
        history_side_effect=[
            # See test_fetch_messages_respects_retry_after_on_429 for
            # the ``no-untyped-call`` suppression rationale.
            SlackApiError(message="not_authed", response=bad_response)  # type: ignore[no-untyped-call]
        ]
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    message = str(excinfo.value)
    assert "invalid_auth" in message
    assert "C1" in message
    assert "xoxb-test" not in message


def test_fetch_messages_raises_connector_failed_on_missing_scope_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_scope`` → :class:`ConnectorFailedError` with scope-extension hint.

    The generic ``error_code`` arm only surfaces the documented short
    string (``missing_scope``), which leaves the operator to guess
    which scope to add — and User Token vs. Bot Token scope tabs
    diverge in the Slack admin UI. The dedicated ``missing_scope``
    branch echoes Slack's ``needed`` field (scope name(s), never the
    token) and links to ADR-0018 + the scope catalogue so the
    operator can remediate in one hop.

    Token-leak invariant: even when the SDK response carries the
    token elsewhere on the response proxy, the raised message must
    never contain it. We pin this with a substring assertion against
    the test token.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 200  # Slack quirk: ``ok: false`` on 200

    # Slack returns ``{"ok": false, "error": "missing_scope",
    # "needed": "<scope>", "provided": "<scope>"}`` — the SDK proxies
    # ``__getitem__`` / ``.get`` access to this dict. We model the
    # ``.get(<key>)`` interface explicitly so the fetcher's branch
    # on ``response.get("error")`` / ``response.get("needed")``
    # resolves to documented payload fields.
    def _response_get(key: str, default: object = None) -> object:
        return {"error": "missing_scope", "needed": "channels:history"}.get(key, default)

    bad_response.get.side_effect = _response_get
    bad_response.headers = {}

    client = _build_client(
        history_side_effect=[
            # See test_fetch_messages_respects_retry_after_on_429 for
            # the ``no-untyped-call`` suppression rationale.
            SlackApiError(  # type: ignore[no-untyped-call]
                message="missing_scope",
                response=bad_response,
            )
        ]
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    message = str(excinfo.value)
    # Channel id + error code: same operator-actionable invariants
    # as the other API-error tests.
    assert "C1" in message
    assert "missing_scope" in message
    # The dedicated branch surfaces the ``needed`` scope so the
    # operator knows exactly which scope to add.
    assert "channels:history" in message
    # The remediation link: ADR-0018 documents the User Token / Bot
    # Token principal split; the Slack scope catalogue is the source
    # of truth for scope names.
    assert "ADR-0018" in message
    assert "https://api.slack.com/scopes" in message
    # Token-leak invariant: the bot token must never appear in the
    # surfaced error, even when the SDK exception's response carries
    # it elsewhere. This is the load-bearing safety property — never
    # let scope diagnostics widen the leak surface.
    assert "xoxb-test" not in message


def test_fetch_messages_missing_scope_omits_needed_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_scope`` without a ``needed`` field still includes the docs link.

    Older Slack response shapes (and some thin-client proxies) omit
    ``needed`` even on ``missing_scope`` failures. The fetcher must
    still produce a useful error: it falls back to an empty
    ``needed`` and still links to ADR-0018 + the scope catalogue so
    the operator has a starting point.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 200
    # No ``needed`` key — Slack's response shape is incomplete.

    def _response_get(key: str, default: object = None) -> object:
        return {"error": "missing_scope"}.get(key, default)

    bad_response.get.side_effect = _response_get
    bad_response.headers = {}

    client = _build_client(
        history_side_effect=[
            SlackApiError(  # type: ignore[no-untyped-call]
                message="missing_scope",
                response=bad_response,
            )
        ]
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    message = str(excinfo.value)
    assert "missing_scope" in message
    # The remediation link is unconditional — even without ``needed``
    # the operator can navigate to the scope catalogue.
    assert "ADR-0018" in message
    assert "https://api.slack.com/scopes" in message
    assert "xoxb-test" not in message


def test_fetch_messages_raises_connector_failed_on_channel_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``channel_not_found`` mid-list still maps to :class:`ConnectorFailedError`.

    A multi-channel config can include a channel the bot has been
    removed from. The fetcher must surface this with the offending
    channel id so the operator can fix the config without affecting
    the rest of the sync.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 200  # Slack quirk: ``ok: false`` on 200
    bad_response.headers = {}
    bad_response.get.return_value = "channel_not_found"

    client = _build_client(
        history_side_effect=[
            # See test_fetch_messages_respects_retry_after_on_429 for
            # the ``no-untyped-call`` suppression rationale.
            SlackApiError(message="not_found", response=bad_response)  # type: ignore[no-untyped-call]
        ]
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C-gone"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    assert "channel_not_found" in str(excinfo.value)
    assert "C-gone" in str(excinfo.value)


# ----- caching ----------------------------------------------------------


def test_user_display_name_cached_across_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two messages from the same author → one :func:`users.info` call.

    Without caching the fetcher would make N + 1 API calls (one per
    message + one per channel-info). Pinning the cache keeps Slack's
    Tier-3 rate limit headroom intact on busy channels.
    """
    msgs = [
        _slack_message(ts="1700000002.000200", text="msg-2", user="U1"),
        _slack_message(ts="1700000001.000100", text="msg-1", user="U1"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    list(fetcher.fetch_messages(cursor_per_channel={}))

    assert client.users_info.call_count == 1
    assert client.users_info.call_args.kwargs == {"user": "U1"}


def test_user_resolution_falls_back_when_user_and_bot_profile_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``user`` + no ``bot_id`` + no ``bot_profile`` → final ``"unknown"`` fallback.

    Issue #367 narrowed the ``"unknown"`` arm to a true last resort
    so that bot / system messages no longer mask the real bot
    identity in the title. This test pins the leaf case: a message
    that lacks **every** author-shaped field (no ``user`` id, no
    ``bot_id``, no ``bot_profile``) still degrades gracefully — the
    fetcher must not blow up, must not waste a ``users.info`` call,
    and must hand the mapper the literal ``"unknown"`` constant so
    the projection never lands an empty author string.
    """
    msgs = [_slack_message(ts="1700000001.000100", text="malformed payload", user=None)]
    client = _build_client(
        history=[_history_response(msgs)],
        users_info_side_effect=AssertionError("users_info must not be called"),
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert len(results) == 1
    assert results[0][1].user_id == ""
    assert results[0][1].user_display_name == "unknown"
    # No subtype on this payload either.
    assert results[0][1].subtype is None


def test_user_display_name_falls_back_to_real_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``display_name`` → ``real_name`` fallback.

    Slack lets users leave ``display_name`` blank, in which case the
    UI shows ``real_name``. Pinning this fallback keeps the brief
    output legible even for users who never customised their handle.
    """
    msgs = [_slack_message(ts="1700000001.000100", user="U2")]
    client = _build_client(history=[_history_response(msgs)])
    # Override the default cached response: display_name blank,
    # real_name populated.
    client.users_info.return_value = {
        "ok": True,
        "user": {
            "id": "U2",
            "name": "u2",
            "profile": {"display_name": "", "real_name": "Bob Builder"},
        },
    }
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert results[0][1].user_display_name == "Bob Builder"


def test_fetch_channel_name_resolves_id_to_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``conversations.info`` resolves the channel id to its name and is cached.

    Pinning the API call shape catches a regression that forgets to
    pass ``channel=`` (the SDK requires kwargs). The cache assertion
    catches a regression that re-fetches the name on every message —
    which on a 1000-message channel would burn the Tier-3 budget.
    """
    msgs = [
        _slack_message(ts="1700000002.000200", text="msg-2"),
        _slack_message(ts="1700000001.000100", text="msg-1"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    client.conversations_info.return_value = _channel_info_response(
        channel_id="C1", name="ops-room"
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert [r[1].channel_name for r in results] == ["ops-room", "ops-room"]
    # One channel-info call regardless of message count.
    assert client.conversations_info.call_count == 1
    assert client.conversations_info.call_args.kwargs == {"channel": "C1"}


# ----- bot / system message author resolution (issue #367) ----------------


def test_bot_message_uses_bot_profile_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bot message → ``user_display_name`` resolves to ``bot_profile.name``.

    Slack populates ``bot_profile`` on every modern bot integration
    (Slack apps, incoming webhooks). The fetcher must prefer this
    human-readable label over the opaque ``bot_id`` so the mapper
    can compose ``"GitHub in #notifications: ..."`` rather than
    ``"unknown in #notifications: ..."`` (issue #367).
    """
    msgs = [
        _slack_message(
            ts="1700000001.000100",
            text="PR opened",
            user=None,
            subtype="bot_message",
            bot_id="B123",
            bot_profile={"name": "GitHub"},
        )
    ]
    client = _build_client(
        history=[_history_response(msgs)],
        users_info_side_effect=AssertionError("users_info must not be called for bot messages"),
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert len(results) == 1
    msg = results[0][1]
    assert msg.user_id == ""
    assert msg.user_display_name == "GitHub"
    assert msg.subtype == "bot_message"


def test_bot_message_without_bot_profile_falls_back_to_bot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bot_message`` without ``bot_profile.name`` → ``"bot:{bot_id}"``.

    Legacy bot integrations omit ``bot_profile`` entirely. The
    fetcher still recovers the bot identity via ``bot_id`` so the
    title carries an operator-traceable label instead of the
    literal ``"unknown"`` (issue #367).
    """
    msgs = [
        _slack_message(
            ts="1700000001.000100",
            text="legacy webhook",
            user=None,
            subtype="bot_message",
            bot_id="B999",
        )
    ]
    client = _build_client(
        history=[_history_response(msgs)],
        users_info_side_effect=AssertionError("users_info must not be called for bot messages"),
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert results[0][1].user_display_name == "bot:B999"
    assert results[0][1].subtype == "bot_message"


def test_bot_message_with_empty_bot_profile_name_falls_back_to_bot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bot_profile`` present but ``name`` blank → falls through to ``bot_id``.

    Some Slack workspaces have bot integrations whose profile is
    populated with metadata other than a name (icons, app ids). The
    fetcher must treat an empty / whitespace-only ``name`` the same
    as a missing field and fall through to ``bot_id``.
    """
    msgs = [
        _slack_message(
            ts="1700000001.000100",
            text="webhook with no display name",
            user=None,
            subtype="bot_message",
            bot_id="B321",
            bot_profile={"name": "  "},
        )
    ]
    client = _build_client(
        history=[_history_response(msgs)],
        users_info_side_effect=AssertionError("users_info must not be called for bot messages"),
    )
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert results[0][1].user_display_name == "bot:B321"


def test_real_user_message_prefers_users_info_over_bot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``user`` is set, ``users.info`` wins over any incidental ``bot_profile``.

    Slack occasionally attaches ``bot_profile`` to messages that
    originate from a real user (e.g. a user who is *also* the
    installer of a bot). The fetcher must keep the user-first
    contract so the title carries the human name, not the bot label.
    """
    msgs = [
        _slack_message(
            ts="1700000001.000100",
            text="real user message",
            user="U1",
            bot_profile={"name": "GitHub"},
        )
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    assert results[0][1].user_display_name == "alice"  # default ``_users_info_response``
    assert results[0][1].user_id == "U1"


def test_subtype_is_carried_as_first_class_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack ``subtype`` lands on :attr:`RawSlackMessage.subtype` (typed field).

    Issue #367 promoted ``subtype`` to a typed dataclass field so the
    mapper can dispatch on it without ``raw.get("subtype")`` typo
    risk. Pin the field round-trip for every realistic subtype the
    mapper handles + the ``None`` default.
    """
    msgs = [
        _slack_message(ts="1700000001.000100", text="user message", user="U1"),
        _slack_message(
            ts="1700000002.000200",
            text="bot message",
            user=None,
            subtype="bot_message",
            bot_id="B1",
            bot_profile={"name": "BotName"},
        ),
        _slack_message(
            ts="1700000003.000300",
            text="<@U1> has joined the channel",
            user="U1",
            subtype="channel_join",
        ),
        _slack_message(
            ts="1700000004.000400",
            text="celebrates the ship",
            user="U1",
            subtype="me_message",
        ),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Yielded ts-ascending; the subtype on each row matches the
    # source payload (with ``None`` for the ordinary user message).
    subtypes = [r[1].subtype for r in results]
    assert subtypes == [None, "bot_message", "channel_join", "me_message"]


# ----- thread reply ingestion (ADR-0030 / issue #466) ------------------------


def _replies_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a :func:`conversations.replies`-shaped response dict.

    ``conversations.replies`` returns the parent as ``messages[0]``
    followed by every child reply in ts-ascending order. The fetcher
    skips ``messages[0]`` on yield (ADR-0030 §(b)) so the dedup
    invariant on ``external_id`` is preserved.
    """
    return {"ok": True, "messages": messages, "has_more": False}


def _parent_with_latest_reply(
    *,
    ts: str = "1700000010.000100",
    text: str = "parent",
    latest_reply: str = "1700000020.000200",
) -> dict[str, Any]:
    """Build a parent message dict with the ``latest_reply`` signal set.

    Slack populates ``latest_reply`` on every parent that has at least
    one child reply (``reply_count > 0`` is the documented co-signal).
    The fetcher gates the follow-up ``conversations.replies`` call on
    ``latest_reply`` presence — see :func:`test_replies_not_called_when_latest_reply_absent`
    for the contrapositive.

    Slack also sets ``thread_ts`` equal to the parent's own ``ts`` on a
    parent with replies; we populate that field too so the
    parent-side ``thread_ts`` invariant
    (:func:`test_thread_ts_field_set_for_parent_and_reply`) is
    exercised end-to-end.
    """
    return {
        "ts": ts,
        "text": text,
        "user": "U1",
        "thread_ts": ts,
        "latest_reply": latest_reply,
        "reply_count": 1,
    }


def _reply_message(
    *,
    ts: str,
    thread_ts: str,
    text: str = "reply",
    user: str = "U2",
) -> dict[str, Any]:
    """Build a child reply message dict (parent ``thread_ts`` injected).

    Slack always populates ``thread_ts`` on reply payloads with the
    parent's ``ts``. The fetcher forwards this verbatim onto
    :attr:`RawSlackMessage.thread_ts` per ADR-0030 §(c).
    """
    return {"ts": ts, "text": text, "user": user, "thread_ts": thread_ts}


def test_replies_not_called_when_latest_reply_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent without ``latest_reply`` → ``conversations.replies`` is not called.

    Pins the API-budget guard from ADR-0030 §(b): standalone messages
    (the vast majority on most workspaces) must not pay the Tier-3
    ``conversations.replies`` round-trip. The fetcher gates on the
    presence of the documented ``latest_reply`` field — Slack only
    populates it when ``reply_count > 0`` — so a regression that
    forgets the gate would silently drain the rate-limit budget on
    every sync.
    """
    msgs = [
        _slack_message(ts="1700000001.000100", text="standalone-1"),
        _slack_message(ts="1700000002.000200", text="standalone-2"),
    ]
    client = _build_client(history=[_history_response(msgs)])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Two parents, zero replies → two yields and no
    # ``conversations.replies`` call. The yielded ``thread_ts`` is
    # ``None`` for both (ADR-0030 §(c) parent semantics).
    assert [r[1].text for r in results] == ["standalone-1", "standalone-2"]
    assert all(r[1].thread_ts is None for r in results)
    assert client.conversations_replies.call_count == 0


def test_replies_fetched_when_latest_reply_present_and_messages_zero_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``latest_reply`` → ``conversations.replies`` called; ``messages[0]`` skipped.

    ADR-0030 §(b) thread reply happy path. The parent is yielded
    once from ``conversations.history``; ``conversations.replies``
    is then called with ``channel=C1, ts=<parent_ts>`` and returns
    the parent + children. The fetcher must:

    * Yield the parent exactly once (via the history path) — the
      replies-response ``messages[0]`` (parent) is skipped to honour
      the natural-key invariant from ADR-0030 §不変条件 3 (the
      ``UNIQUE`` constraint on ``external_id = f"{channel_id}:{ts}"``
      catches a regression but skipping at source keeps the event
      log clean and saves one ``users.info`` / ``chat.getPermalink``
      budget per parent).
    * Yield every child reply (``messages[1:]``) in ts-ascending
      order with ``thread_ts`` set to the parent's ``ts``.
    """
    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )
    reply_1 = _reply_message(
        ts="1700000015.000150",
        thread_ts="1700000010.000100",
        text="reply-1",
    )
    reply_2 = _reply_message(
        ts="1700000020.000200",
        thread_ts="1700000010.000100",
        text="reply-2",
    )
    client = _build_client(history=[_history_response([parent])])
    # Replies endpoint returns parent + 2 replies (Slack contract).
    client.conversations_replies.return_value = _replies_response([parent, reply_1, reply_2])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Three yields: parent + 2 replies. Parent itself appears only
    # once (the replies-response head is skipped).
    assert [r[1].text for r in results] == ["parent", "reply-1", "reply-2"]
    assert [r[1].ts for r in results] == [
        "1700000010.000100",
        "1700000015.000150",
        "1700000020.000200",
    ]
    # ``conversations.replies`` called exactly once with the parent
    # ts. Pin the kwargs shape so a regression that changes the
    # arg name (Slack SDK occasionally renames) trips here.
    assert client.conversations_replies.call_count == 1
    replies_call = client.conversations_replies.call_args
    assert replies_call.kwargs == {"channel": "C1", "ts": "1700000010.000100"}


def test_thread_ts_field_set_for_parent_and_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RawSlackMessage.thread_ts`` round-trips parent + reply semantics.

    ADR-0030 §(c):

    * Parent message that already has replies → Slack sets
      ``thread_ts == ts`` on the parent's payload; the fetcher
      forwards that verbatim so downstream consumers (``reply-draft``)
      can identify a thread root without joining back to the channel
      history.
    * Standalone top-level message (no replies) → ``thread_ts`` is
      absent from the Slack payload; the fetcher normalises to
      ``None``.
    * Child reply → ``thread_ts`` points at the parent's ``ts``; the
      fetcher forwards that verbatim.
    """
    parent_with_replies = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent-with-replies",
        latest_reply="1700000020.000200",
    )
    standalone = _slack_message(ts="1700000005.000050", text="standalone")
    reply = _reply_message(
        ts="1700000020.000200",
        thread_ts="1700000010.000100",
        text="child",
    )

    client = _build_client(history=[_history_response([parent_with_replies, standalone])])
    client.conversations_replies.return_value = _replies_response([parent_with_replies, reply])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    by_text = {r[1].text: r[1] for r in results}
    # Parent with replies: thread_ts == ts (Slack convention).
    assert by_text["parent-with-replies"].thread_ts == "1700000010.000100"
    # Standalone top-level message: no thread_ts in payload → None.
    assert by_text["standalone"].thread_ts is None
    # Child reply: thread_ts == parent's ts.
    assert by_text["child"].thread_ts == "1700000010.000100"
    # Reply ts is the reply's own; only the cursor element is
    # parent-anchored (asserted separately below).
    assert by_text["child"].ts == "1700000020.000200"


def test_thread_reply_cursor_anchored_to_parent_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yielded ``new_cursor`` for a reply == parent's ``ts`` (ADR-0030 §(d)).

    The per-channel resume cursor stays anchored to the channel's
    parent timeline so a partial-progress sync that committed N
    replies but not the next parent re-resumes from the last parent's
    ts (not from a reply ts that may be greater than the next
    parent's ts). Without this anchor a later sync would skip a
    parent whose ts lies between two reply timestamps.
    """
    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )
    reply = _reply_message(
        ts="1700000020.000200",
        thread_ts="1700000010.000100",
        text="reply",
    )
    client = _build_client(history=[_history_response([parent])])
    client.conversations_replies.return_value = _replies_response([parent, reply])
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Parent's cursor element is its own ts; reply's cursor element
    # is the parent's ts (NOT the reply's own ts).
    assert [(r[1].text, r[2]) for r in results] == [
        ("parent", "1700000010.000100"),
        ("reply", "1700000010.000100"),
    ]


def test_thread_replies_retry_429_via_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``conversations.replies`` 429 → exponential backoff via the shared helper.

    ADR-0030 §(e) pins ``_call_replies`` as the 4th call site of
    :func:`opshub.connectors.slack._retry.retry_on_rate_limit`
    (alongside ``_call_history`` + the discovery probe + the listing
    path). The shared helper applies the 1s / 2s / 4s exponential
    fallback when ``Retry-After`` is missing, so a single 429 must
    sleep exactly once for 1s and then succeed.
    """
    from slack_sdk.errors import SlackApiError

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )
    reply = _reply_message(
        ts="1700000020.000200",
        thread_ts="1700000010.000100",
        text="reply",
    )

    rate_limited_response = MagicMock()
    rate_limited_response.status_code = 429
    # No ``Retry-After`` → exponential fallback (2**0 = 1s).
    rate_limited_response.headers = {}
    rate_limited_response.get.return_value = "rate_limited"

    client = _build_client(history=[_history_response([parent])])
    client.conversations_replies.side_effect = [
        # See test_fetch_messages_respects_retry_after_on_429 for the
        # ``no-untyped-call`` suppression rationale.
        SlackApiError(message="ratelimited", response=rate_limited_response),  # type: ignore[no-untyped-call]
        _replies_response([parent, reply]),
    ]
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # Parent + reply both yielded after the retry succeeded.
    assert [r[1].text for r in results] == ["parent", "reply"]
    # The shared retry helper slept exactly once for the exponential
    # fallback (no ``Retry-After`` header → ``2**0 == 1``).
    sleep_mock.assert_called_once_with(1)


def test_thread_replies_non_429_error_surfaces_connector_failed_with_thread_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thread_not_found`` → ``ConnectorFailedError`` with channel + thread_ts.

    The dedicated reply-path error message names the offending
    ``thread_ts`` so an operator can map the failure back to a
    specific thread (vs. the parent-path message which carries only
    the channel id). The bot token must never appear regardless of
    error code.
    """
    from slack_sdk.errors import SlackApiError

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )

    bad_response = MagicMock()
    bad_response.status_code = 200  # Slack quirk: ``ok: false`` on 200
    bad_response.headers = {}
    bad_response.get.return_value = "thread_not_found"

    client = _build_client(history=[_history_response([parent])])
    client.conversations_replies.side_effect = [
        # See test_fetch_messages_respects_retry_after_on_429 for the
        # ``no-untyped-call`` suppression rationale.
        SlackApiError(message="not_found", response=bad_response)  # type: ignore[no-untyped-call]
    ]
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    message = str(excinfo.value)
    # Channel id + thread_ts + error code: the reply-path message
    # must name both so an operator can trace to a specific thread.
    assert "C1" in message
    assert "1700000010.000100" in message
    assert "thread_not_found" in message
    # Token-leak invariant: same as the parent-path tests.
    assert "xoxb-test" not in message


def test_thread_replies_missing_scope_surfaces_scope_hint_with_thread_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``conversations.replies`` ``missing_scope`` → ADR-0018 hint with ``needed`` + ``thread_ts``.

    Shares the same hint pattern as the parent-side ``missing_scope``
    branch (link to ADR-0018, Slack scope catalogue, echo of the
    ``needed`` field). The reply-path message additionally names the
    offending thread_ts.
    """
    from slack_sdk.errors import SlackApiError

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )

    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.headers = {}

    def _response_get(key: str, default: object = None) -> object:
        return {"error": "missing_scope", "needed": "channels:history"}.get(key, default)

    bad_response.get.side_effect = _response_get

    client = _build_client(history=[_history_response([parent])])
    client.conversations_replies.side_effect = [
        SlackApiError(  # type: ignore[no-untyped-call]
            message="missing_scope",
            response=bad_response,
        )
    ]
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    with pytest.raises(ConnectorFailedError) as excinfo:
        list(fetcher.fetch_messages(cursor_per_channel={}))

    message = str(excinfo.value)
    assert "C1" in message
    assert "1700000010.000100" in message
    assert "missing_scope" in message
    assert "channels:history" in message
    # Same scope-catalogue hint shape as the parent-path missing_scope test.
    assert "ADR-0018" in message
    assert "https://api.slack.com/scopes" in message
    assert "xoxb-test" not in message


def test_thread_replies_skipped_when_parent_channel_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent in ``excludes.channels`` → no ``conversations.replies`` call.

    The connector applies ``excludes.channels`` per-yield (every
    yielded ``RawSlackMessage`` for an excluded channel is dropped
    before reaching ``SourceService.observe``). When the fetcher
    knows the parent will be dropped, the follow-up
    ``conversations.replies`` call is wasted budget — every reply
    would be dropped on the same per-yield filter. ADR-0030 §(b)
    gates the call site on the optional ``excludes`` kwarg so the
    fetcher can short-circuit the request entirely.

    Counterpart for sender-based excludes is pinned by
    :func:`test_thread_replies_skipped_when_parent_sender_excluded`.
    The unconditional case (no ``excludes`` passed) is pinned by
    :func:`test_replies_fetched_when_latest_reply_present_and_messages_zero_skipped`.
    """
    from opshub.core.excludes import ExcludeRules

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="excluded-parent",
        latest_reply="1700000020.000200",
    )
    client = _build_client(history=[_history_response([parent])])
    # If the fetcher mistakenly calls ``conversations.replies`` the
    # mock has no configured return value; ``MagicMock`` would still
    # respond but the assertion below pins ``call_count == 0`` so a
    # regression trips immediately.
    _patch_webclient(monkeypatch, client)

    excludes = ExcludeRules(channels=frozenset({"C1"}))
    fetcher = SlackFetcher(_auth(), channels=["C1"])
    # Parent still yielded — the connector applies excludes filter
    # on the yielded row; the fetcher's excludes short-circuit only
    # affects the follow-up ``conversations.replies`` call.
    results = list(fetcher.fetch_messages(cursor_per_channel={}, excludes=excludes))

    assert [r[1].text for r in results] == ["excluded-parent"]
    assert client.conversations_replies.call_count == 0


def test_thread_replies_skipped_when_parent_sender_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent author in ``excludes.senders`` → no ``conversations.replies`` call.

    Mirror of the channel-excluded test for the sender axis. ADR-0020
    §(b) excludes filter exposes both ``channels`` and ``senders`` as
    independent dimensions; either one triggering on the parent
    suffices to short-circuit the reply fetch (because both filters
    are applied per-yield by the connector, so every reply would be
    dropped regardless).

    Note that the per-message sender check uses the parent's
    ``user_id`` field; replies authored by *other* users in the same
    thread are still skipped, mirroring how the connector handles
    each row independently.
    """
    from opshub.core.excludes import ExcludeRules

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent-from-excluded-sender",
        latest_reply="1700000020.000200",
    )
    client = _build_client(history=[_history_response([parent])])
    _patch_webclient(monkeypatch, client)

    # ``_parent_with_latest_reply`` defaults ``user`` to ``"U1"``.
    excludes = ExcludeRules(senders=frozenset({"U1"}))
    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}, excludes=excludes))

    assert [r[1].text for r in results] == ["parent-from-excluded-sender"]
    assert client.conversations_replies.call_count == 0


def test_thread_replies_fetched_for_mixed_threads_only_when_latest_reply_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed history (A: 2 replies, B: 0 replies, C: 1 reply) → 2 replies calls.

    Integration-coverage hint at the unit level: the fetcher must
    gate per-parent, not per-channel. The expected call count is the
    number of parents with ``latest_reply`` set — a regression that
    eagerly fetches replies for every parent would burn the rate-limit
    budget on workspaces where most messages are standalone.
    """
    parent_a = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent-A",
        latest_reply="1700000020.000200",
    )
    parent_b = _slack_message(ts="1700000011.000110", text="parent-B-standalone")
    parent_c = _parent_with_latest_reply(
        ts="1700000012.000120",
        text="parent-C",
        latest_reply="1700000030.000300",
    )

    reply_a1 = _reply_message(
        ts="1700000015.000150",
        thread_ts="1700000010.000100",
        text="A-reply-1",
    )
    reply_a2 = _reply_message(
        ts="1700000020.000200",
        thread_ts="1700000010.000100",
        text="A-reply-2",
    )
    reply_c1 = _reply_message(
        ts="1700000030.000300",
        thread_ts="1700000012.000120",
        text="C-reply-1",
    )

    client = _build_client(history=[_history_response([parent_a, parent_b, parent_c])])

    def _replies_side_effect(*, channel: str, ts: str) -> dict[str, Any]:
        del channel
        if ts == "1700000010.000100":
            return _replies_response([parent_a, reply_a1, reply_a2])
        if ts == "1700000012.000120":
            return _replies_response([parent_c, reply_c1])
        raise AssertionError(f"unexpected replies call for thread_ts={ts!r}")

    client.conversations_replies.side_effect = _replies_side_effect
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    results = list(fetcher.fetch_messages(cursor_per_channel={}))

    # 3 parents + 3 replies = 6 yields total; parent-B contributes
    # zero replies and zero ``conversations.replies`` calls.
    assert [r[1].text for r in results] == [
        "parent-A",
        "A-reply-1",
        "A-reply-2",
        "parent-B-standalone",
        "parent-C",
        "C-reply-1",
    ]
    # Two ``conversations.replies`` calls — one per parent with
    # ``latest_reply``. Parent B (standalone) does not trigger a call.
    assert client.conversations_replies.call_count == 2


# ----- cold-start guard --------------------------------------------------


def test_fetcher_module_does_not_import_slack_sdk_eagerly() -> None:
    """``opshub.connectors.slack.fetcher`` must not import the SDK at module level.

    ``slack_sdk`` is only needed inside :meth:`fetch_messages`.
    Eager import would defeat the cold-start budget (ADR-0001) and
    force operators on the auth-only path to install the heavy
    extras. We verify by parsing the module source statically
    (mirrors the approach used by ``test_auth.py``).
    """
    import ast
    from pathlib import Path

    fetcher_path = Path(sys.modules["opshub.connectors.slack.fetcher"].__file__ or "")
    source = fetcher_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(fetcher_path))

    offenders: list[str] = []
    for node in tree.body:  # top-level only
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] == "slack_sdk":
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module == "slack_sdk":
                offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        "opshub.connectors.slack.fetcher imports slack_sdk at module level "
        "(must be lazy-loaded inside fetch_messages):\n  - " + "\n  - ".join(offenders)
    )


# ----- Phase 20-E: partial-progress on thread reply failure ----------------


def test_thread_reply_fetch_failure_yields_parent_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent is observable before ``conversations.replies`` failure (Phase 20-E).

    Audit followup for [#478](https://github.com/ozzy-labs/opshub/issues/478):
    the fetcher's :meth:`_iter_channel` yields the parent message
    BEFORE it requests ``conversations.replies`` for its replies, so
    a downstream consumer iterating the generator one row at a time
    can persist the parent + advance the cursor before the reply
    fetch ever fires. The connector relies on that ordering for its
    partial-progress checkpoint (see :meth:`SlackConnector.sync`'s
    ``finally`` arm): if the reply fetch raises
    :class:`ConnectorFailedError` after we've yielded the parent, the
    channels-axis cursor still advances on the next sync. A
    regression that re-ordered the parent yield to *after* the reply
    fetch would break the cascade documented in issue #339.

    Drive the generator manually (``next()`` calls) so the parent
    yield is observable BEFORE the reply call raises — ``list()``
    would discard partial yields when the iterator raises, hiding
    the contract under test.
    """
    from slack_sdk.errors import SlackApiError

    parent = _parent_with_latest_reply(
        ts="1700000010.000100",
        text="parent",
        latest_reply="1700000020.000200",
    )

    bad_response = MagicMock()
    bad_response.status_code = 200  # Slack quirk: ``ok: false`` on 200
    bad_response.headers = {}
    bad_response.get.return_value = "thread_not_found"

    client = _build_client(history=[_history_response([parent])])
    client.conversations_replies.side_effect = [
        SlackApiError(  # type: ignore[no-untyped-call]
            message="not_found",
            response=bad_response,
        )
    ]
    _patch_webclient(monkeypatch, client)

    fetcher = SlackFetcher(_auth(), channels=["C1"])
    iterator = fetcher.fetch_messages(cursor_per_channel={})

    # Manual ``next()`` so the parent yield is observable before the
    # reply fetch raises.
    parent_channel_id, parent_message, parent_cursor = next(iterator)
    assert parent_channel_id == "C1"
    assert parent_message.ts == "1700000010.000100"
    assert parent_message.text == "parent"
    # The cursor element for the parent is its own ts (Phase 1
    # ``conversations.history`` semantics).
    assert parent_cursor == "1700000010.000100"

    # The next pull triggers the reply fetch which raises after the
    # parent has already been yielded.
    with pytest.raises(ConnectorFailedError) as excinfo:
        next(iterator)

    message = str(excinfo.value)
    # Reply-path message names channel + thread_ts + error code (the
    # canonical reply-path error shape per ADR-0030 §(b)).
    assert "C1" in message
    assert "1700000010.000100" in message
    assert "thread_not_found" in message
    # Token-leak invariant — same as the sibling tests.
    assert "xoxb-test" not in message
