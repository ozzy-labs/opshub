"""Tests for ``opshub.connectors.slack.conversations`` (#366).

:func:`list_conversations` walks Slack's ``users.conversations`` API
(default) — or ``conversations.list`` when called with ``all=True`` —
to yield :class:`SlackConversation` rows for the discovery CLI. The
behaviour worth pinning:

1. Default endpoint is ``users.conversations`` (joined-only view);
   ``all=True`` flips to ``conversations.list`` (workspace-wide).
2. ``types`` parameter is serialised as a comma-separated list of
   Slack API tokens (``public_channel,private_channel,im,mpim``) so
   the request shape is observable in tests.
3. Multi-page pagination via ``response_metadata.next_cursor`` stitches
   into one consistent stream.
4. ``limit`` stops the outer loop as soon as the post-filter count
   reaches the cap (the API call count is bounded too).
5. Archived channels are excluded by default; DM/MPIM rows are never
   gated by the archived flag (Slack does not archive DMs).
6. ``filter_substring`` matches case-insensitively against ``name`` or
   ``display_name`` so the operator can filter by either column.
7. DM (``im``) rows resolve a peer name via ``users.info`` lookup with
   per-call caching; MPIM (``mpim``) rows resolve participants via
   ``conversations.members`` + ``users.info``.
8. ``missing_scope`` failures raise :class:`ConnectorFailedError`
   with the scope name embedded.
9. HTTP 429 with ``Retry-After`` is honoured up to three retries
   before escalating.
10. An optional :class:`ProgressReporter` is advanced by the raw page
    size (pre-filter) so the spinner ticks for every Slack-returned
    row.

The :mod:`slack_sdk` extras (``[connectors-slack]``) may not be
installed in every environment, so the file-level
``pytest.importorskip`` gates the whole module.
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
from opshub.connectors.slack.conversations import (
    CONVERSATION_TYPES,
    SlackConversation,
    _as_response_dict,  # pyright: ignore[reportPrivateUsage]
    list_conversations,
)
from opshub.core.errors import ConnectorFailedError

# ----- shared fixtures ---------------------------------------------------


def _auth() -> SlackAuth:
    """Construct :class:`SlackAuth` with an explicit token (test-only)."""
    return SlackAuth(token="xoxb-test")


def _public_channel(
    channel_id: str = "C1",
    *,
    name: str = "general",
    is_archived: bool = False,
    purpose: str | None = "Company-wide announcements",
) -> dict[str, Any]:
    """Build a ``conversations.list``-shaped public-channel row."""
    row: dict[str, Any] = {
        "id": channel_id,
        "name": name,
        "is_channel": True,
        "is_private": False,
        "is_archived": is_archived,
    }
    if purpose is not None:
        row["purpose"] = {"value": purpose, "creator": "U-creator", "last_set": 0}
    return row


def _private_channel(
    channel_id: str = "G1",
    *,
    name: str = "leadership",
    is_archived: bool = False,
    purpose: str | None = "Leadership only",
) -> dict[str, Any]:
    """Build a ``conversations.list``-shaped private-channel row."""
    row: dict[str, Any] = {
        "id": channel_id,
        "name": name,
        "is_private": True,
        "is_archived": is_archived,
    }
    if purpose is not None:
        row["purpose"] = {"value": purpose, "creator": "U-creator", "last_set": 0}
    return row


def _im_row(
    channel_id: str = "D1",
    *,
    user: str = "U-alice",
) -> dict[str, Any]:
    """Build a ``users.conversations``-shaped ``im`` row.

    Slack returns ``user`` (the peer's id) on every ``im`` row but no
    ``name`` field — DMs have no Slack-assigned name.
    """
    return {
        "id": channel_id,
        "is_im": True,
        "is_private": True,
        "user": user,
    }


def _mpim_row(channel_id: str = "G-mpim-1") -> dict[str, Any]:
    """Build a ``users.conversations``-shaped ``mpim`` row.

    Participants are resolved via a separate ``conversations.members``
    call, so the row itself only carries flags + id.
    """
    return {
        "id": channel_id,
        "is_mpim": True,
        "is_private": True,
    }


def _list_response(
    channels: list[dict[str, Any]],
    *,
    next_cursor: str = "",
) -> dict[str, Any]:
    """Build a ``users.conversations`` / ``conversations.list`` response dict."""
    return {
        "ok": True,
        "channels": channels,
        "response_metadata": {"next_cursor": next_cursor},
    }


def _user_info_response(
    *,
    display_name: str = "",
    real_name: str = "",
    name: str = "",
) -> dict[str, Any]:
    """Build a ``users.info`` response with the documented profile shape."""
    return {
        "ok": True,
        "user": {
            "name": name,
            "real_name": real_name,
            "profile": {"display_name": display_name, "real_name": real_name},
        },
    }


def _members_response(
    members: list[str],
    *,
    next_cursor: str = "",
) -> dict[str, Any]:
    """Build a ``conversations.members`` response."""
    return {
        "ok": True,
        "members": members,
        "response_metadata": {"next_cursor": next_cursor},
    }


def _build_client(
    *,
    list_pages: list[dict[str, Any]] | None = None,
    list_side_effect: Any = None,
    users_info_responses: dict[str, dict[str, Any]] | None = None,
    members_responses: dict[str, dict[str, Any]] | None = None,
    use_conversations_list: bool = False,
) -> MagicMock:
    """Construct a :class:`MagicMock` WebClient with documented response shapes.

    ``list_pages`` / ``list_side_effect`` drive whichever endpoint the
    caller exercises (default = ``users.conversations``;
    ``use_conversations_list=True`` swaps to ``conversations.list``).

    ``users_info_responses`` is a ``{user_id: response_dict}`` map. The
    mock side-effect dispatches on the requested user id so a single
    test can mix multiple users.
    """
    client = MagicMock()
    pages = list_pages or [_list_response([])]

    if use_conversations_list:
        if list_side_effect is not None:
            client.conversations_list.side_effect = list_side_effect
        else:
            client.conversations_list.side_effect = list(pages)
    else:
        if list_side_effect is not None:
            client.users_conversations.side_effect = list_side_effect
        else:
            client.users_conversations.side_effect = list(pages)

    user_responses = users_info_responses or {}

    def _users_info(user: str, **_kwargs: Any) -> dict[str, Any]:
        return user_responses.get(user, _user_info_response(real_name=user))

    client.users_info.side_effect = _users_info

    member_responses = members_responses or {}

    def _members(channel: str, **_kwargs: Any) -> dict[str, Any]:
        return member_responses.get(channel, _members_response([]))

    client.conversations_members.side_effect = _members
    return client


def _patch_webclient(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> MagicMock:
    """Patch ``slack_sdk.WebClient`` to return ``client``."""
    import slack_sdk

    factory = MagicMock(return_value=client)
    monkeypatch.setattr(slack_sdk, "WebClient", factory)
    return factory


# ----- happy path -------------------------------------------------------


def test_default_types_set_includes_all_four() -> None:
    """The exported tuple pins the documented default accept-list."""
    assert CONVERSATION_TYPES == ("public", "private", "im", "mpim")


def test_list_conversations_uses_users_conversations_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default call dispatches to ``users.conversations`` (joined-only).

    The Slack legacy ``conversations.list`` returns everything the
    token's principal can *see*; ``users.conversations`` returns
    only conversations the token has *joined*. The default is the
    joined-only view because that matches what operators expect when
    they say "show me my Slack".
    """
    client = _build_client(list_pages=[_list_response([])])
    _patch_webclient(monkeypatch, client)

    list(list_conversations(_auth()))

    assert client.users_conversations.call_count == 1
    assert client.conversations_list.call_count == 0


def test_list_conversations_all_flag_switches_to_conversations_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``all=True`` flips the dispatch to ``conversations.list``.

    The workspace-wide path is opt-in because it requires the broader
    ``channels:read`` + ``groups:read`` scope set and surprises
    operators who expect "what I see in Slack".
    """
    client = _build_client(
        list_pages=[_list_response([])],
        use_conversations_list=True,
    )
    _patch_webclient(monkeypatch, client)

    list(list_conversations(_auth(), all=True))

    assert client.conversations_list.call_count == 1
    assert client.users_conversations.call_count == 0


def test_list_conversations_default_types_serialise_to_all_four_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``types`` → ``"public_channel,private_channel,im,mpim"``.

    The Slack API tokens differ from our short names; pinning the
    serialisation guards against a future rename that silently drops
    a type from the request.
    """
    client = _build_client(list_pages=[_list_response([])])
    _patch_webclient(monkeypatch, client)

    list(list_conversations(_auth()))

    call_kwargs = client.users_conversations.call_args.kwargs
    assert call_kwargs["types"] == "public_channel,private_channel,im,mpim"


def test_list_conversations_types_subset_filters_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``types=("public",)`` → ``"public_channel"`` only at the API."""
    client = _build_client(list_pages=[_list_response([])])
    _patch_webclient(monkeypatch, client)

    list(list_conversations(_auth(), types=("public",)))

    call_kwargs = client.users_conversations.call_args.kwargs
    assert call_kwargs["types"] == "public_channel"


def test_list_conversations_yields_public_channel_with_full_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public channel row → :class:`SlackConversation` with type='public'."""
    page = _list_response([_public_channel("C1", name="general", purpose="Hi")])
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert len(results) == 1
    row = results[0]
    assert row.id == "C1"
    assert row.type == "public"
    assert row.name == "general"
    assert row.display_name == "general"
    assert row.is_private is False
    assert row.is_archived is False
    assert row.purpose == "Hi"
    assert row.participants == ()


def test_list_conversations_yields_private_channel_with_private_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private channel row → ``type='private'`` and ``is_private=True``."""
    page = _list_response([_private_channel("G1", name="leadership")])
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert results[0].type == "private"
    assert results[0].is_private is True


def test_list_conversations_paginates_via_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pages: the second is fetched using ``next_cursor`` from the first."""
    page1 = _list_response(
        [_public_channel("C1", name="general"), _public_channel("C2", name="random")],
        next_cursor="page2",
    )
    page2 = _list_response([_public_channel("C3", name="eng")], next_cursor="")
    client = _build_client(list_pages=[page1, page2])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert [c.id for c in results] == ["C1", "C2", "C3"]
    assert client.users_conversations.call_count == 2
    second_kwargs = client.users_conversations.call_args_list[1].kwargs
    assert second_kwargs["cursor"] == "page2"


def test_list_conversations_excludes_archived_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_archived=True`` channel rows are dropped unless ``include_archived=True``."""
    page = _list_response(
        [
            _public_channel("C1", name="live"),
            _public_channel("C2", name="dead", is_archived=True),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    default = list(list_conversations(_auth()))
    assert [c.id for c in default] == ["C1"]

    client.users_conversations.side_effect = [page]
    with_archived = list(list_conversations(_auth(), include_archived=True))
    assert [c.id for c in with_archived] == ["C1", "C2"]


def test_list_conversations_filter_matches_name_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter_substring`` matches channel ``name`` regardless of case."""
    page = _list_response(
        [
            _public_channel("C1", name="general"),
            _public_channel("C2", name="eng-backend"),
            _public_channel("C3", name="design"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), filter_substring="ENG"))

    assert [c.id for c in results] == ["C2"]


def test_list_conversations_filter_matches_dm_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter_substring`` matches DM ``display_name`` so operators can find DMs by participant.

    DMs have no Slack-assigned ``name`` so the filter must fall
    through to ``display_name`` (the resolved peer name) — otherwise
    DM rows would be invisible to ``--filter alice``.
    """
    page = _list_response([_im_row("D1", user="U-alice")])
    client = _build_client(
        list_pages=[page],
        users_info_responses={
            "U-alice": _user_info_response(display_name="alice"),
        },
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), filter_substring="alice"))

    assert [c.id for c in results] == ["D1"]
    assert results[0].display_name == "alice"


def test_list_conversations_limit_stops_outer_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit=2`` halts iteration after two yields, even mid-page."""
    page1 = _list_response(
        [_public_channel("C1"), _public_channel("C2"), _public_channel("C3")],
        next_cursor="page2",
    )
    client = _build_client(list_pages=[page1])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), limit=2))

    assert [c.id for c in results] == ["C1", "C2"]
    assert client.users_conversations.call_count == 1


def test_list_conversations_skips_malformed_and_untyped_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows missing ``id`` or any type flag are dropped silently.

    A row carrying neither ``is_channel`` nor ``is_private`` nor
    ``is_im`` nor ``is_mpim`` is uncl assifiable — Slack should never
    emit one but a thin proxy might. The helper must drop it rather
    than crash, so a single bad payload does not poison the listing.
    """
    page = _list_response(
        [
            {"id": "", "is_channel": True},  # no id
            {"id": "X1"},  # no type flag
            _public_channel("C-ok", name="ok"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert [c.id for c in results] == ["C-ok"]


def test_list_conversations_handles_missing_purpose_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows without a ``purpose`` sub-object fall back to ``purpose=""``."""
    page = _list_response(
        [
            _public_channel("C1", name="general", purpose=None),
            _public_channel("C2", name="random", purpose=""),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert [c.purpose for c in results] == ["", ""]


def test_list_conversations_drops_types_not_in_requested_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose type is not in the requested set is dropped client-side.

    Slack honours the ``types`` API parameter, but some workspaces'
    response shapes leak adjacent types (a private-channel row arriving
    on a ``public_channel``-only request because both ``is_private``
    and ``is_channel`` are set). The client-side re-gate stops the
    operator-visible accept-list and the API request from drifting.
    """
    page = _list_response(
        [
            _public_channel("C1", name="general"),
            _private_channel("G1", name="leadership"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), types=("public",)))

    assert [c.id for c in results] == ["C1"]


# ----- DM / MPIM name resolution ----------------------------------------


def test_list_conversations_im_resolves_peer_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DM ``im`` row → ``display_name`` populated from ``users.info``."""
    page = _list_response([_im_row("D1", user="U-alice")])
    client = _build_client(
        list_pages=[page],
        users_info_responses={
            "U-alice": _user_info_response(display_name="alice"),
        },
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert len(results) == 1
    row = results[0]
    assert row.type == "im"
    assert row.name is None
    assert row.display_name == "alice"
    assert client.users_info.call_count == 1


def test_list_conversations_im_falls_back_to_real_name_when_display_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile with empty ``display_name`` falls back to ``real_name``."""
    page = _list_response([_im_row("D1", user="U-bob")])
    client = _build_client(
        list_pages=[page],
        users_info_responses={
            "U-bob": _user_info_response(display_name="", real_name="Bob Smith"),
        },
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert results[0].display_name == "Bob Smith"


def test_list_conversations_caches_user_info_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeat user ids → single ``users.info`` call per id.

    A multi-user MPIM listing can reference the same user across many
    rows; caching ensures one API hit per user_id rather than one per
    appearance.
    """
    page = _list_response(
        [
            _im_row("D1", user="U-alice"),
            _im_row("D2", user="U-alice"),
        ]
    )
    client = _build_client(
        list_pages=[page],
        users_info_responses={
            "U-alice": _user_info_response(display_name="alice"),
        },
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert [r.display_name for r in results] == ["alice", "alice"]
    assert client.users_info.call_count == 1


def test_list_conversations_im_falls_back_to_user_id_on_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``users.info`` API failure → ``display_name`` falls back to the user id.

    DM listing must not fail because one user is no longer resolvable
    (deactivated account, etc.). The fallback is the raw user id so
    the operator can still copy the conversation id and act on it.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response([_im_row("D1", user="U-ghost")])

    client = MagicMock()
    client.users_conversations.side_effect = [page]
    # Construct a response object the SDK exception expects.
    err_response = MagicMock()
    err_response.status_code = 404
    err_response.get.return_value = "user_not_found"
    err_response.headers = {}
    client.users_info.side_effect = SlackApiError(  # type: ignore[no-untyped-call]
        message="user_not_found", response=err_response
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert results[0].display_name == "U-ghost"


def test_list_conversations_mpim_resolves_participants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MPIM row → participants resolved via ``conversations.members`` + ``users.info``."""
    page = _list_response([_mpim_row("G-mpim-1")])
    client = _build_client(
        list_pages=[page],
        members_responses={
            "G-mpim-1": _members_response(["U-alice", "U-bob", "U-carol"]),
        },
        users_info_responses={
            "U-alice": _user_info_response(display_name="alice"),
            "U-bob": _user_info_response(display_name="bob"),
            "U-carol": _user_info_response(display_name="carol"),
        },
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert len(results) == 1
    row = results[0]
    assert row.type == "mpim"
    assert row.participants == ("alice", "bob", "carol")
    assert row.display_name == "alice, bob, carol"


def test_list_conversations_mpim_with_no_members_falls_back_to_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``conversations.members`` response → ``display_name`` = conversation id.

    A failed members lookup leaves the row visible (so the operator
    can still paste the id into ``opshub.toml``) with the id itself
    as the display name.
    """
    page = _list_response([_mpim_row("G-mpim-empty")])
    client = _build_client(
        list_pages=[page],
        members_responses={"G-mpim-empty": _members_response([])},
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert results[0].display_name == "G-mpim-empty"
    assert results[0].participants == ()


# ----- progress reporter -------------------------------------------------


def test_list_conversations_advances_reporter_by_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reporter.advance(len(page))`` is called for each raw page.

    The advance happens **before** client-side filtering so the
    operator sees the spinner tick for every Slack-returned row,
    not only the post-filter survivors — matching ``connector sync``'s
    "items observed at the API" semantics.
    """
    page1 = _list_response(
        [_public_channel("C1"), _public_channel("C2"), _public_channel("C3")],
        next_cursor="p2",
    )
    page2 = _list_response([_public_channel("C4", is_archived=True)], next_cursor="")
    client = _build_client(list_pages=[page1, page2])
    _patch_webclient(monkeypatch, client)

    reporter = MagicMock()

    results = list(list_conversations(_auth(), reporter=reporter))

    # Archived row from page 2 is filtered client-side, but the reporter
    # still saw it (3 + 1 advances).
    assert [c.id for c in results] == ["C1", "C2", "C3"]
    assert [call.args for call in reporter.advance.call_args_list] == [(3,), (1,)]


def test_list_conversations_none_reporter_does_not_call_progress_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reporter=None`` keeps the helper caller-agnostic (no AttributeError)."""
    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), reporter=None))

    assert [c.id for c in results] == ["C1"]


def test_list_conversations_does_not_advance_reporter_on_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty page → no ``reporter.advance`` call (no spurious 0-step ticks)."""
    page = _list_response([])
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    reporter = MagicMock()

    list(list_conversations(_auth(), reporter=reporter))

    assert reporter.advance.call_count == 0


# ----- rate limiting ----------------------------------------------------


def test_list_conversations_respects_retry_after_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with ``Retry-After`` is honoured, then the call succeeds."""
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 429
    bad_response.headers = {"Retry-After": "1"}
    bad_response.get.return_value = "rate_limited"
    success = _list_response([_public_channel("C1", name="recovered")])

    client = _build_client(
        list_side_effect=[
            SlackApiError(message="ratelimited", response=bad_response),  # type: ignore[no-untyped-call]
            success,
        ]
    )
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    results = list(list_conversations(_auth()))

    assert [c.name for c in results] == ["recovered"]
    sleep_mock.assert_called_once_with(1)


def test_list_conversations_exhausts_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive 429s → :class:`ConnectorFailedError` after the budget."""
    from slack_sdk.errors import SlackApiError

    def _make_429() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}
        resp.get.return_value = "rate_limited"
        return SlackApiError(message="ratelimited", response=resp)  # type: ignore[no-untyped-call]

    client = _build_client(list_side_effect=[_make_429(), _make_429(), _make_429()])
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth()))

    assert [call.args for call in sleep_mock.call_args_list] == [(1,), (2,), (4,)]
    message = str(excinfo.value)
    assert "rate_limited" in message
    assert "users.conversations" in message
    assert "xoxb-test" not in message


# ----- non-rate-limit API errors ----------------------------------------


def test_list_conversations_raises_on_invalid_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_auth`` → :class:`ConnectorFailedError` immediately."""
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 401
    bad_response.headers = {}
    bad_response.get.return_value = "invalid_auth"

    client = _build_client(
        list_side_effect=[
            SlackApiError(message="not_authed", response=bad_response)  # type: ignore[no-untyped-call]
        ]
    )
    _patch_webclient(monkeypatch, client)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth()))

    message = str(excinfo.value)
    assert "invalid_auth" in message
    assert "xoxb-test" not in message


def test_list_conversations_raises_with_scope_hint_on_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_scope`` → error message names the ``needed`` scope + ADR-0018."""
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 200

    def _response_get(key: str, default: object = None) -> object:
        return {"error": "missing_scope", "needed": "im:read"}.get(key, default)

    bad_response.get.side_effect = _response_get
    bad_response.headers = {}

    client = _build_client(
        list_side_effect=[
            SlackApiError(  # type: ignore[no-untyped-call]
                message="missing_scope",
                response=bad_response,
            )
        ]
    )
    _patch_webclient(monkeypatch, client)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth()))

    message = str(excinfo.value)
    assert "missing_scope" in message
    assert "im:read" in message
    assert "ADR-0018" in message
    assert "users.conversations" in message
    assert "xoxb-test" not in message


def test_list_conversations_all_path_names_conversations_list_in_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``all=True`` errors mention ``conversations.list`` (not ``users.conversations``).

    The operator sees the endpoint name so they know which scope's
    docs to consult — important because ``conversations.list`` needs
    ``channels:read`` / ``groups:read`` while ``users.conversations``
    additionally needs ``im:read`` / ``mpim:read``.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 401
    bad_response.headers = {}
    bad_response.get.return_value = "invalid_auth"

    client = _build_client(
        list_side_effect=[
            SlackApiError(message="not_authed", response=bad_response)  # type: ignore[no-untyped-call]
        ],
        use_conversations_list=True,
    )
    _patch_webclient(monkeypatch, client)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth(), all=True))

    assert "conversations.list" in str(excinfo.value)


# ----- cold-start guard --------------------------------------------------


def test_conversations_module_does_not_import_slack_sdk_eagerly() -> None:
    """``opshub.connectors.slack.conversations`` must not import the SDK at module level."""
    import ast
    from pathlib import Path

    module_path = Path(sys.modules["opshub.connectors.slack.conversations"].__file__ or "")
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] == "slack_sdk":
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module == "slack_sdk":
                offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        "opshub.connectors.slack.conversations imports slack_sdk at module level "
        "(must be lazy-loaded):\n  - " + "\n  - ".join(offenders)
    )


# ----- private helpers --------------------------------------------------


def test_as_response_dict_handles_slack_response_object() -> None:
    """:func:`_as_response_dict` unwraps SDK ``SlackResponse`` via ``.data``."""
    response_with_data = MagicMock(spec=["data"])
    response_with_data.data = {"ok": True, "channels": [{"id": "C1"}]}

    result = _as_response_dict(response_with_data)
    assert result == {"ok": True, "channels": [{"id": "C1"}]}

    class _DictLike:
        def keys(self) -> list[str]:
            return ["ok", "error"]

        def __getitem__(self, key: str) -> object:
            return {"ok": False, "error": "ratelimited"}[key]

    result_fallback = _as_response_dict(_DictLike())
    assert result_fallback == {"ok": False, "error": "ratelimited"}


def test_slack_conversation_dataclass_round_trip() -> None:
    """Smoke test the dataclass: tuple participants are frozen by ``slots``."""
    row = SlackConversation(
        id="C1",
        type="public",
        name="general",
        display_name="general",
        is_private=False,
        is_archived=False,
        purpose="hi",
        participants=(),
    )
    assert row.id == "C1"
    assert row.participants == ()
    assert row.last_activity_ts is None  # default, no activity probe attempted
    # frozen=True: assignment is rejected with FrozenInstanceError
    # (a subclass of AttributeError on Python 3.13).
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        row.id = "C2"  # type: ignore[misc]


# ----- activity filter (--since) ----------------------------------------


def _history_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a ``conversations.history`` response with the documented shape."""
    return {"ok": True, "messages": messages, "has_more": False}


def _since_dt(*, days_ago: float) -> Any:
    """Tz-aware UTC datetime ``days_ago`` days before now (helper for ``--since`` tests)."""
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(days=days_ago)


def test_list_conversations_since_calls_history_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``since`` set → one ``conversations.history`` call per surviving row.

    Each row that survives the type / archived / filter gates triggers
    one history probe with ``limit=1`` + the ``since`` timestamp.
    Rows whose latest message predates ``since`` are dropped.
    """
    page = _list_response(
        [_public_channel("C1", name="active"), _public_channel("C2", name="silent")]
    )
    client = _build_client(list_pages=[page])

    # ``C1`` returns a fresh message → kept; ``C2`` returns no messages
    # (i.e., no activity after ``since``) → dropped.
    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel == "C1":
            return _history_response([{"ts": "1717200000.123456", "text": "hi"}])
        return _history_response([])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))

    assert [c.id for c in results] == ["C1"]
    # ``last_activity_ts`` is a float parsed from the Slack ts string;
    # use an absolute tolerance (fractional microsecond drift is below
    # the precision the discovery command cares about).
    ts = results[0].last_activity_ts
    assert ts is not None
    assert abs(ts - 1717200000.123456) < 1e-3
    # One history call per row (2 rows ⇒ 2 calls, even though only 1 survived).
    assert client.conversations_history.call_count == 2


def test_list_conversations_no_since_skips_history_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``since=None`` → ``conversations.history`` is never called.

    The no-extra-API-call path of #366 is preserved when the operator
    does not opt into activity probing.
    """
    page = _list_response([_public_channel("C1"), _public_channel("C2")])
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth()))

    assert [c.id for c in results] == ["C1", "C2"]
    assert client.conversations_history.call_count == 0
    assert all(r.last_activity_ts is None for r in results)


def test_list_conversations_since_passes_oldest_to_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``oldest=`` parameter on ``conversations.history`` carries ``since`` as a unix ts."""
    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])
    client.conversations_history.return_value = _history_response([{"ts": "1717200000.000000"}])
    _patch_webclient(monkeypatch, client)

    from datetime import UTC, datetime

    since = datetime(2026, 5, 1, tzinfo=UTC)
    list(list_conversations(_auth(), since=since))

    call_kwargs = client.conversations_history.call_args.kwargs
    assert call_kwargs["channel"] == "C1"
    assert call_kwargs["limit"] == 1
    assert call_kwargs["inclusive"] is False
    # Slack expects a stringified unix ts; the helper formats with
    # microsecond precision so callers can use sub-second cutoffs.
    assert abs(float(call_kwargs["oldest"]) - since.timestamp()) < 1e-3


def test_list_conversations_since_missing_scope_disables_type_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_scope`` on history → that type is dropped + 1 warning appended.

    The warning is emitted once per type even when multiple rows of
    that type would have triggered the call. Other types' rows
    continue to flow normally.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C1", name="public-one"),
            _public_channel("C2", name="public-two"),
            _private_channel("G1", name="private-one"),
        ]
    )
    client = _build_client(list_pages=[page])

    def _make_missing_scope() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": "missing_scope", "needed": "channels:history"}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message="missing_scope", response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel.startswith("C"):
            raise _make_missing_scope()
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    assert [c.id for c in results] == ["G1"]
    # One warning per affected type (public), not per row (would be 2).
    assert len(warnings) == 1
    assert "public" in warnings[0]
    assert "channels:history" in warnings[0]
    # The token never leaks into the warning surface (ADR-0027).
    assert "xoxb-test" not in warnings[0]


def test_list_conversations_since_missing_scope_per_type_warnings_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two types failing with distinct ``needed`` scopes → two independent warnings.

    Pins the per-type semantics of the ``disabled_history_types`` set:
    a public-channel miss disables only the public bucket and surfaces
    one warning naming ``channels:history``; a concurrent mpim-bucket
    miss disables only mpim and surfaces a second warning naming
    ``mpim:history``. Other types' rows (private here) continue to flow.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C-pub", name="public-one"),
            _private_channel("G-priv", name="private-one"),
            _mpim_row("G-mpim-1"),
        ]
    )
    client = _build_client(
        list_pages=[page],
        members_responses={"G-mpim-1": _members_response(["U-alice", "U-bob"])},
        users_info_responses={
            "U-alice": _user_info_response(display_name="alice"),
            "U-bob": _user_info_response(display_name="bob"),
        },
    )

    def _make_miss(needed: str) -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": "missing_scope", "needed": needed}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message="missing_scope", response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel.startswith("C"):
            raise _make_miss("channels:history")
        if channel.startswith("G-mpim"):
            raise _make_miss("mpim:history")
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    # Only the private row (whose history call succeeded) survived.
    assert [c.id for c in results] == ["G-priv"]
    assert len(warnings) == 2
    public_warn = next(w for w in warnings if "public" in w)
    mpim_warn = next(w for w in warnings if "mpim" in w)
    assert "channels:history" in public_warn
    assert "mpim:history" in mpim_warn
    # Token never leaks through the warning surface.
    assert all("xoxb-test" not in w for w in warnings)


def test_list_conversations_since_warnings_none_drops_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``warnings=None`` is the non-CLI default — missing_scope still disables the type."""
    from slack_sdk.errors import SlackApiError

    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}

    def _get(key: str, default: object = None) -> object:
        return {"error": "missing_scope", "needed": "channels:history"}.get(key, default)

    resp.get.side_effect = _get
    client.conversations_history.side_effect = SlackApiError(  # type: ignore[no-untyped-call]
        message="missing_scope", response=resp
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))
    # No warnings collector ⇒ the row is still dropped, but no
    # exception escapes — the helper stays caller-agnostic.
    assert results == []


def test_list_conversations_since_history_429_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``conversations.history`` 429 → ``Retry-After`` honoured (mirrors listing retry)."""
    from slack_sdk.errors import SlackApiError

    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])

    bad_resp = MagicMock()
    bad_resp.status_code = 429
    bad_resp.headers = {"Retry-After": "1"}
    bad_resp.get.return_value = "ratelimited"
    rate_error = SlackApiError(  # type: ignore[no-untyped-call]
        message="ratelimited", response=bad_resp
    )
    client.conversations_history.side_effect = [
        rate_error,
        _history_response([{"ts": "1717200000.000000"}]),
    ]
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))

    assert len(results) == 1
    sleep_mock.assert_called_once_with(1)


def test_list_conversations_since_history_non_429_raises_connector_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other ``conversations.history`` API errors map to :class:`ConnectorFailedError`.

    The error message names ``conversations.history`` so the operator
    sees which scope's docs to consult — mirrors the listing error
    vocabulary so the discovery command has one consistent shape.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])

    bad_resp = MagicMock()
    bad_resp.status_code = 500
    bad_resp.headers = {}
    bad_resp.get.return_value = "internal_error"
    client.conversations_history.side_effect = SlackApiError(  # type: ignore[no-untyped-call]
        message="internal_error", response=bad_resp
    )
    _patch_webclient(monkeypatch, client)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth(), since=_since_dt(days_ago=7)))

    assert "conversations.history" in str(excinfo.value)
    assert "xoxb-test" not in str(excinfo.value)


def test_list_conversations_since_channel_not_found_skips_row_aggregates_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``channel_not_found`` on history → drop that row, keep the rest, 1 aggregate warning.

    Pins the row-scoped skip behaviour for ``conversations.history``
    errors that are not type-uniform (a Slack Connect external channel
    can list via ``users.conversations`` and then fail history with
    ``channel_not_found`` while sibling public channels on the same
    token's history scope continue to work). The warning counts the
    affected rows and names the error code so the operator can map
    back to the documented Slack causes (Slack Connect / deactivated
    DM peer / archived-between-list-and-probe) without re-reading the
    docs.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C1", name="readable"),
            _public_channel("C-extern", name="slack-connect"),
            _public_channel("C2", name="also-readable"),
        ]
    )
    client = _build_client(list_pages=[page])

    def _make_channel_not_found() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": "channel_not_found"}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message="channel_not_found", response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel == "C-extern":
            raise _make_channel_not_found()
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    assert [c.id for c in results] == ["C1", "C2"]
    # One aggregate warning naming the count + error code (not per-row).
    assert len(warnings) == 1
    assert "skipped 1 inaccessible channel" in warnings[0]
    assert "channel_not_found=1" in warnings[0]
    # Token never leaks through the warning surface (ADR-0027).
    assert "xoxb-test" not in warnings[0]


def test_list_conversations_since_not_in_channel_skips_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``not_in_channel`` on history → row-scoped skip + aggregate warning.

    Mirrors the ``channel_not_found`` arm but exercises the second
    documented per-row failure: the principal was removed from a
    private channel between the listing and the activity probe. The
    other rows must continue to flow.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C1", name="open"),
            _private_channel("G-locked", name="kicked-out"),
        ]
    )
    client = _build_client(list_pages=[page])

    def _make_not_in_channel() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": "not_in_channel"}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message="not_in_channel", response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel == "G-locked":
            raise _make_not_in_channel()
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    assert [c.id for c in results] == ["C1"]
    assert len(warnings) == 1
    assert "not_in_channel=1" in warnings[0]


def test_list_conversations_since_inaccessible_warning_aggregates_per_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple per-row history misses across two error codes → one aggregate warning.

    The summary groups by error code (``channel_not_found=2,
    not_in_channel=1``) so the operator sees one stderr line covering
    every dropped row regardless of cause. Pins the "one warning per
    listing call" invariant that protects operators with large Slack
    Connect / private-channel surface areas from stderr noise.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C1", name="open-one"),
            _public_channel("C-extern-1", name="connect-1"),
            _public_channel("C-extern-2", name="connect-2"),
            _private_channel("G-locked", name="kicked-out"),
            _public_channel("C2", name="open-two"),
        ]
    )
    client = _build_client(list_pages=[page])

    def _make_error(code: str) -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": code}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message=code, response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel.startswith("C-extern"):
            raise _make_error("channel_not_found")
        if channel == "G-locked":
            raise _make_error("not_in_channel")
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    assert [c.id for c in results] == ["C1", "C2"]
    assert len(warnings) == 1
    assert "skipped 3 inaccessible channels" in warnings[0]
    assert "channel_not_found=2" in warnings[0]
    assert "not_in_channel=1" in warnings[0]


def test_list_conversations_since_inaccessible_warning_omitted_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No per-row history misses → no aggregate warning is appended.

    The summary warning must not pollute the operator-visible output
    on a healthy run; the ``warnings`` list stays empty when every
    history probe succeeds.
    """
    page = _list_response(
        [_public_channel("C1", name="open-one"), _public_channel("C2", name="open-two")]
    )
    client = _build_client(list_pages=[page])
    client.conversations_history.return_value = _history_response([{"ts": "1717200000.000000"}])
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    list(list_conversations(_auth(), since=_since_dt(days_ago=7), warnings=warnings))

    assert warnings == []


def test_list_conversations_since_inaccessible_warning_emitted_under_limit_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate warning fires even when the ``limit`` cap exits the loop early.

    Pins that the summary emission is wired into both ``return`` arms
    of the listing loop — an operator who hits ``--limit 1`` after a
    Slack Connect skip still sees the dropped-channel count.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response(
        [
            _public_channel("C-extern", name="connect"),
            _public_channel("C1", name="open-one"),
            _public_channel("C2", name="open-two"),
        ]
    )
    client = _build_client(list_pages=[page])

    def _make_channel_not_found() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def _get(key: str, default: object = None) -> object:
            return {"error": "channel_not_found"}.get(key, default)

        resp.get.side_effect = _get
        return SlackApiError(  # type: ignore[no-untyped-call]
            message="channel_not_found", response=resp
        )

    def _history(*, channel: str, **_kwargs: Any) -> dict[str, Any]:
        if channel == "C-extern":
            raise _make_channel_not_found()
        return _history_response([{"ts": "1717200000.000000"}])

    client.conversations_history.side_effect = _history
    _patch_webclient(monkeypatch, client)

    warnings: list[str] = []
    results = list(
        list_conversations(_auth(), since=_since_dt(days_ago=7), limit=1, warnings=warnings)
    )

    # Limit 1 + the C-extern skip → only C1 surfaces.
    assert [c.id for c in results] == ["C1"]
    assert len(warnings) == 1
    assert "channel_not_found=1" in warnings[0]


def test_list_conversations_since_inaccessible_warnings_none_drops_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``warnings=None`` keeps the helper caller-agnostic on inaccessible rows.

    Mirrors the existing ``missing_scope`` "warnings=None" contract:
    the row is still dropped, no exception escapes, and the helper
    stays safe for non-CLI callers that only consume the row stream.
    """
    from slack_sdk.errors import SlackApiError

    page = _list_response([_public_channel("C-extern", name="connect")])
    client = _build_client(list_pages=[page])

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}

    def _get(key: str, default: object = None) -> object:
        return {"error": "channel_not_found"}.get(key, default)

    resp.get.side_effect = _get
    client.conversations_history.side_effect = SlackApiError(  # type: ignore[no-untyped-call]
        message="channel_not_found", response=resp
    )
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))
    assert results == []


# ----- audit followup: _call_list `all=True` + activity-probe defensive arms


def test_list_conversations_all_path_exhausts_retries_names_conversations_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``all=True`` + 3 x 429 → ``ConnectorFailedError`` names ``conversations.list``.

    Companion to ``test_list_conversations_exhausts_retries_then_raises`` (which
    only exercises the default ``users.conversations`` endpoint) and to
    ``test_list_conversations_all_path_names_conversations_list_in_errors``
    (which only covers non-429 errors). Pins the post-#380 path where
    the shared :func:`retry_on_rate_limit` helper re-raises the last
    429 and ``_call_list``'s outer ``except SlackApiError`` arm maps
    it via ``_to_connector_failed(exc, all=True)`` — so the endpoint
    name in the error message reflects the workspace-wide path.
    """
    from slack_sdk.errors import SlackApiError

    def _make_429() -> SlackApiError:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}
        resp.get.return_value = "rate_limited"
        return SlackApiError(message="ratelimited", response=resp)  # type: ignore[no-untyped-call]

    client = _build_client(
        list_side_effect=[_make_429(), _make_429(), _make_429()],
        use_conversations_list=True,
    )
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    monkeypatch.setattr(_stdlib_time, "sleep", MagicMock())

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_conversations(_auth(), all=True))

    message = str(excinfo.value)
    assert "conversations.list" in message
    assert "users.conversations" not in message
    assert "xoxb-test" not in message


def test_list_conversations_since_drops_row_when_history_ts_is_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric ``ts`` in a ``conversations.history`` row → ``None`` → row dropped.

    Defensive arm: a thin proxy or hostile mock could return a row
    whose ``ts`` is not a valid float string. The discovery path
    treats the row as if it had no activity (drops it from the
    activity-filtered output) rather than crashing the listing.
    """
    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])
    client.conversations_history.return_value = {
        "ok": True,
        "messages": [{"ts": "not-a-number"}],
        "has_more": False,
    }
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))

    assert results == []


def test_list_conversations_since_drops_row_when_messages_field_is_not_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``messages`` returned as a non-list (string / dict) → defensive drop.

    A buggy thin proxy could return ``messages`` as a dict or string
    instead of a list of message objects. The activity probe must
    silently drop the row in that case rather than raise an
    ``AttributeError`` or ``TypeError`` that would propagate up and
    break the entire listing.
    """
    page = _list_response([_public_channel("C1")])
    client = _build_client(list_pages=[page])
    client.conversations_history.return_value = {
        "ok": True,
        "messages": "this should be a list",  # malformed shape
        "has_more": False,
    }
    _patch_webclient(monkeypatch, client)

    results = list(list_conversations(_auth(), since=_since_dt(days_ago=7)))

    assert results == []
