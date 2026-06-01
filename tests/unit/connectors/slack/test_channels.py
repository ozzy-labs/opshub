"""Tests for ``opshub.connectors.slack.channels`` (#341 PR1).

:func:`list_channels` walks Slack's ``conversations.list`` API to
yield :class:`SlackChannel` rows for the channel-discovery CLI (PR2
on top of #341). The behaviour worth pinning:

1. Multi-page pagination via ``response_metadata.next_cursor``
   stitches into one consistent stream.
2. ``limit`` stops the outer loop as soon as the post-filter count
   reaches the cap (the API call count is bounded too — important
   for workspaces with thousands of channels).
3. Archived channels are excluded by default and included on
   opt-in; private channels likewise gate the ``types`` parameter
   sent to Slack so the request shape changes visibly.
4. ``filter_substring`` matches case-insensitively against
   ``channel.name``.
5. Empty / missing ``purpose`` and ``topic`` sub-objects do not
   crash the row builder.
6. ``missing_scope`` failures raise :class:`ConnectorFailedError`
   with the scope name embedded so the operator can extend their
   OAuth grant without round-tripping the docs.
7. HTTP 429 with ``Retry-After`` is honoured up to three retries
   before escalating to :class:`ConnectorFailedError`. The 1s /
   2s / 4s fallback applies when Slack omits the header.

The :mod:`slack_sdk` extras (``[connectors-slack]``) may not be
installed in every environment, so the file-level
``pytest.importorskip`` gates the whole module. Every Slack API
call is patched at the :class:`slack_sdk.WebClient` boundary so no
real request leaves CI (same pattern as ``test_fetcher.py``).
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
from opshub.connectors.slack.channels import (
    SlackChannel,
    list_channels,
)
from opshub.core.errors import ConnectorFailedError

# ----- shared fixtures ---------------------------------------------------


def _auth() -> SlackAuth:
    """Construct :class:`SlackAuth` with an explicit token.

    Bypasses the keyring / env-var path so the discovery tests stay
    focused on :func:`list_channels` behaviour (auth resolution is
    already pinned in ``test_auth.py``).
    """
    return SlackAuth(token="xoxb-test")


def _channel(
    channel_id: str = "C1",
    *,
    name: str = "general",
    is_private: bool = False,
    is_archived: bool = False,
    purpose: str | None = "Company-wide announcements",
) -> dict[str, Any]:
    """Build a :func:`conversations.list`-shaped channel row.

    Slack returns many more fields (``creator``, ``num_members``,
    ``topic``, ...) but the row builder only reads ``id`` / ``name``
    / ``is_private`` / ``is_archived`` / ``purpose.value``. Keeping
    the fixture minimal exposes any future field dependency
    immediately and makes the test surface obvious.
    """
    row: dict[str, Any] = {
        "id": channel_id,
        "name": name,
        "is_private": is_private,
        "is_archived": is_archived,
    }
    if purpose is not None:
        row["purpose"] = {"value": purpose, "creator": "U-creator", "last_set": 0}
    return row


def _list_response(
    channels: list[dict[str, Any]],
    *,
    next_cursor: str = "",
) -> dict[str, Any]:
    """Build a :func:`conversations.list`-shaped response dict.

    A blank ``next_cursor`` is Slack's documented "end of stream"
    signal — :func:`list_channels` stops paginating on it.
    """
    return {
        "ok": True,
        "channels": channels,
        "response_metadata": {"next_cursor": next_cursor},
    }


def _build_client(
    *,
    list_pages: list[dict[str, Any]] | None = None,
    list_side_effect: Any = None,
) -> MagicMock:
    """Construct a :class:`MagicMock` WebClient with documented response shapes.

    ``list_pages`` is the ordered list of ``conversations.list``
    responses the test wants the mock to return. Multi-page tests
    pass two responses; single-page tests pass one.

    ``list_side_effect`` overrides ``list_pages`` and lets a test
    inject :class:`SlackApiError` (for the 429 / missing_scope
    branches).
    """
    client = MagicMock()
    if list_side_effect is not None:
        client.conversations_list.side_effect = list_side_effect
    else:
        client.conversations_list.side_effect = list(list_pages or [_list_response([])])
    return client


def _patch_webclient(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> MagicMock:
    """Patch ``slack_sdk.WebClient`` to return ``client``.

    :func:`list_channels` imports :class:`WebClient` lazily inside
    the function body (cold-start guard), so we patch the attribute
    on the SDK module — not on the channels module — to intercept
    the lookup at the import site.
    """
    import slack_sdk

    factory = MagicMock(return_value=client)
    monkeypatch.setattr(slack_sdk, "WebClient", factory)
    return factory


# ----- happy path -------------------------------------------------------


def test_list_channels_yields_dataclass_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single page → one :class:`SlackChannel` per row, with all fields populated.

    Smoke test that confirms the row builder reads the documented
    Slack fields (``id`` / ``name`` / ``is_private`` / ``is_archived``
    / ``purpose.value``) into the dataclass without re-shaping. The
    CLI formatter (PR2) builds on this contract.
    """
    page = _list_response(
        [
            _channel("C1", name="general", purpose="Company-wide"),
            _channel("C2", name="eng-backend", purpose="Backend"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth()))

    assert [c.id for c in results] == ["C1", "C2"]
    assert [c.name for c in results] == ["general", "eng-backend"]
    assert all(isinstance(c, SlackChannel) for c in results)
    assert results[0].purpose == "Company-wide"
    assert results[0].is_private is False
    assert results[0].is_archived is False


def test_list_channels_paginates_via_next_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pages: the second is fetched using ``next_cursor`` from the first.

    Slack's ``conversations.list`` returns up to ``limit`` channels
    per page (we pass 200) and signals "more available" via a
    non-empty ``response_metadata.next_cursor``. :func:`list_channels`
    must thread the cursor into the follow-up call — otherwise
    workspaces with > 200 channels would silently truncate.
    """
    page1 = _list_response(
        [_channel("C1", name="general"), _channel("C2", name="random")],
        next_cursor="page2",
    )
    page2 = _list_response([_channel("C3", name="eng-backend")], next_cursor="")
    client = _build_client(list_pages=[page1, page2])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth()))

    assert [c.id for c in results] == ["C1", "C2", "C3"]
    assert client.conversations_list.call_count == 2
    second_kwargs = client.conversations_list.call_args_list[1].kwargs
    assert second_kwargs["cursor"] == "page2"


def test_list_channels_default_types_is_public_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default call → ``types="public_channel"`` (no private requested).

    Pinning the default keeps the operator on the minimum scope set
    documented in ADR-0018 — adding ``private_channel`` would require
    ``groups:read``, which is opt-in.
    """
    client = _build_client(list_pages=[_list_response([])])
    _patch_webclient(monkeypatch, client)

    list(list_channels(_auth()))

    call_kwargs = client.conversations_list.call_args.kwargs
    assert call_kwargs["types"] == "public_channel"


def test_list_channels_include_private_switches_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include_private=True`` → ``types="public_channel,private_channel"``.

    The request shape is observable to the operator (and to the
    Slack admin audit log), so we pin the exact serialisation. Slack
    enforces ``groups:read`` on the private-channel type — without
    the scope the call returns ``missing_scope``, exercised by the
    dedicated error-path test.
    """
    client = _build_client(list_pages=[_list_response([])])
    _patch_webclient(monkeypatch, client)

    list(list_channels(_auth(), include_private=True))

    call_kwargs = client.conversations_list.call_args.kwargs
    assert call_kwargs["types"] == "public_channel,private_channel"


def test_list_channels_excludes_archived_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_archived=True`` rows are dropped unless ``include_archived=True``.

    Operators almost never want archived channels in their sync
    list — the default-excluded behaviour matches that intuition.
    Pinning both directions (default vs. opt-in) guards against a
    regression that flips the gate.
    """
    page = _list_response(
        [
            _channel("C1", name="live"),
            _channel("C2", name="dead", is_archived=True),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    default = list(list_channels(_auth()))
    assert [c.id for c in default] == ["C1"]

    # Re-arm the mock for a fresh call.
    client.conversations_list.side_effect = [page]
    with_archived = list(list_channels(_auth(), include_archived=True))
    assert [c.id for c in with_archived] == ["C1", "C2"]
    assert with_archived[1].is_archived is True


def test_list_channels_filter_is_case_insensitive_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter_substring`` matches the channel name regardless of case.

    The CLI's ``--filter <text>`` knob lowercases its input before
    matching (the operator types ``--filter ENG`` and expects
    ``eng-backend`` to come back). Pin the case-insensitivity
    contract here so the CLI formatter does not need to repeat it.
    """
    page = _list_response(
        [
            _channel("C1", name="general"),
            _channel("C2", name="eng-backend"),
            _channel("C3", name="ENG-INFRA"),
            _channel("C4", name="design"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth(), filter_substring="ENG"))

    assert [c.id for c in results] == ["C2", "C3"]


def test_list_channels_limit_stops_outer_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``limit=2`` halts iteration after two yields, even mid-page.

    The limit caps the post-filter yield count, not the per-page
    size — important so workspaces with thousands of channels do
    not over-fetch when the operator asks for the first handful.
    On a single-page workspace the second API call must not happen.
    """
    page1 = _list_response(
        [_channel("C1"), _channel("C2"), _channel("C3")],
        next_cursor="page2",
    )
    # If the limit gate is broken the test will pull from page2 too,
    # which would expose the bug as either a third yield or a missing
    # second-page response (depending on which way the regression
    # leaks). ``MagicMock.side_effect`` raises ``StopIteration``
    # rather than returning a default — that surfaces the bug
    # loudly.
    client = _build_client(list_pages=[page1])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth(), limit=2))

    assert [c.id for c in results] == ["C1", "C2"]
    # Only the first page was fetched; the limit short-circuits the
    # loop before the ``next_cursor`` follow-up.
    assert client.conversations_list.call_count == 1


def test_list_channels_handles_missing_purpose_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows without a ``purpose`` sub-object fall back to ``purpose=""``.

    Slack's contract says ``purpose`` is always present, but
    thin-client proxies occasionally strip optional sub-objects. The
    row builder must not raise — the operator still wants the id /
    name pair for paste into ``opshub.toml``.
    """
    page = _list_response(
        [
            _channel("C1", name="general", purpose=None),
            # An empty-string purpose (``purpose.value = ""``) is
            # also documented behaviour for never-customised
            # channels; both shapes should land on ``purpose=""``.
            _channel("C2", name="random", purpose=""),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth()))

    assert [c.purpose for c in results] == ["", ""]


def test_list_channels_skips_malformed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows missing ``id`` or ``name`` are dropped silently.

    A single malformed row must not poison the discovery output —
    the operator should still get the valid rows so they can
    proceed. We do not raise because Slack's docs commit to ``id``
    / ``name`` presence; this branch is defensive against
    contract-violating proxies.
    """
    page = _list_response(
        [
            {"id": "", "name": "no-id"},
            {"id": "C2", "name": ""},
            _channel("C3", name="ok"),
        ]
    )
    client = _build_client(list_pages=[page])
    _patch_webclient(monkeypatch, client)

    results = list(list_channels(_auth()))

    assert [c.id for c in results] == ["C3"]


# ----- rate limiting ----------------------------------------------------


def test_list_channels_respects_retry_after_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with ``Retry-After`` is honoured exactly once, then the call succeeds.

    Mirrors the fetcher's 429 handling so operators see uniform
    behaviour across discovery and sync. Pinning the sleep duration
    to the exact header value catches a regression that defaults to
    the exponential schedule even when the header is present.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 429
    bad_response.headers = {"Retry-After": "1"}
    bad_response.get.return_value = "rate_limited"
    success = _list_response([_channel("C1", name="recovered")])

    client = _build_client(
        list_side_effect=[
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
    # assert the exact backoff value used. ``opshub.connectors.slack.channels``
    # imports ``time`` (not ``from time import sleep``), so the
    # attribute lookup ``time.sleep`` resolves to the stdlib module's
    # ``sleep`` at call time — patching it on the stdlib module is
    # the canonical surface.
    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    results = list(list_channels(_auth()))

    assert [c.name for c in results] == ["recovered"]
    sleep_mock.assert_called_once_with(1)


def test_list_channels_exhausts_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive 429s → :class:`ConnectorFailedError` after the budget.

    The 1s / 2s / 4s fallback fires when ``Retry-After`` is absent.
    We assert ``time.sleep`` is called for each of the three attempts
    and that the final error never echoes the token.
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

    client = _build_client(list_side_effect=[_make_429(), _make_429(), _make_429()])
    _patch_webclient(monkeypatch, client)

    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_channels(_auth()))

    # Exponential fallback: 1s, 2s, 4s for attempts 0 / 1 / 2.
    assert [call.args for call in sleep_mock.call_args_list] == [(1,), (2,), (4,)]
    message = str(excinfo.value)
    assert "rate_limited" in message
    # The token must never appear in the surfaced error, even when
    # the SDK exception's response carries it elsewhere.
    assert "xoxb-test" not in message


# ----- non-rate-limit API errors ----------------------------------------


def test_list_channels_raises_connector_failed_on_invalid_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_auth`` → :class:`ConnectorFailedError` immediately (no retry).

    Non-rate-limit Slack errors are permanent. The helper surfaces
    the API ``error`` code (a documented short string) so operators
    can map it back to the Slack docs without exposing the token.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 401
    bad_response.headers = {}
    bad_response.get.return_value = "invalid_auth"

    client = _build_client(
        list_side_effect=[
            # See the 429 test for the ``no-untyped-call`` rationale.
            SlackApiError(message="not_authed", response=bad_response)  # type: ignore[no-untyped-call]
        ]
    )
    _patch_webclient(monkeypatch, client)

    with pytest.raises(ConnectorFailedError) as excinfo:
        list(list_channels(_auth()))

    message = str(excinfo.value)
    assert "invalid_auth" in message
    assert "xoxb-test" not in message


def test_list_channels_raises_with_scope_hint_on_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_scope`` → :class:`ConnectorFailedError` with scope-extension hint.

    Without a dedicated branch the operator would see the generic
    short code (``missing_scope``) and have to guess which scope to
    add. Echoing Slack's ``needed`` field and linking to ADR-0018 +
    the scope catalogue resolves the diagnostic in one hop. The
    token-leak invariant is pinned with a substring assertion.
    """
    from slack_sdk.errors import SlackApiError

    bad_response = MagicMock()
    bad_response.status_code = 200  # Slack quirk: ``ok: false`` on 200

    def _response_get(key: str, default: object = None) -> object:
        return {"error": "missing_scope", "needed": "groups:read"}.get(key, default)

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
        list(list_channels(_auth(), include_private=True))

    message = str(excinfo.value)
    assert "missing_scope" in message
    # The dedicated branch surfaces ``needed`` so the operator
    # knows which scope to add for ``include_private``.
    assert "groups:read" in message
    # The remediation link is unconditional.
    assert "ADR-0018" in message
    assert "https://api.slack.com/scopes" in message
    # Token-leak invariant.
    assert "xoxb-test" not in message


# ----- cold-start guard --------------------------------------------------


def test_channels_module_does_not_import_slack_sdk_eagerly() -> None:
    """``opshub.connectors.slack.channels`` must not import the SDK at module level.

    ``slack_sdk`` is only needed inside :func:`list_channels` (and
    the private ``_call_list`` helper). Eager import would defeat
    the cold-start budget (ADR-0001) and force operators on the
    auth-only path to install the heavy extras. Verified by parsing
    the module source statically — same approach as the fetcher's
    cold-start guard test.
    """
    import ast
    from pathlib import Path

    channels_path = Path(sys.modules["opshub.connectors.slack.channels"].__file__ or "")
    source = channels_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(channels_path))

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
        "opshub.connectors.slack.channels imports slack_sdk at module level "
        "(must be lazy-loaded inside list_channels / _call_list):\n  - " + "\n  - ".join(offenders)
    )
