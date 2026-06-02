"""Tests for ``opshub.connectors.github.api`` (Phase 3 step B2).

Every test routes its HTTP traffic through :class:`httpx.MockTransport`
so the suite never reaches the real GitHub API. ``respx`` is
deliberately avoided -- ``MockTransport`` ships with httpx and adding
``respx`` would mean a new dev dependency.

The fixture pattern: every test builds a small ``routes`` table keyed
by ``(method, path)`` and feeds it to :func:`_client`. Any
unexpected request raises ``AssertionError`` from the mock handler,
so a regressing implementation that hits an unmocked endpoint fails
loudly instead of silently calling the real network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from opshub.connectors.github.api import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    GitHubAPIError,
    GitHubAuthError,
    GitHubItem,
    GitHubRateLimitError,
    _parse_iso_utc,  # pyright: ignore[reportPrivateUsage]
    _to_iso_utc,  # pyright: ignore[reportPrivateUsage]
    list_issues_since,
    list_notifications,
    list_pulls_since,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_TOKEN = "ghp_test_token"


def _client(routes: dict[tuple[str, str], httpx.Response]) -> httpx.Client:
    """Build an :class:`httpx.Client` whose every request is matched against ``routes``.

    Keys are ``(method, path)`` tuples; non-matching requests raise so
    a regressing implementation that hits an unmocked endpoint cannot
    silently call the real network.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return routes[key]

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )


def _recording_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """Variant of :func:`_client` that lets the caller inspect each request directly."""
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )


def _issue_payload(number: int, *, body: str | None = "hello world") -> dict[str, object]:
    return {
        "number": number,
        "title": f"issue #{number}",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "body": body,
        "updated_at": "2026-05-15T10:20:30Z",
    }


def _pr_payload(number: int, *, updated_at: str, body: str | None = "pr body") -> dict[str, object]:
    return {
        "number": number,
        "title": f"pr #{number}",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "body": body,
        "updated_at": updated_at,
    }


def _notification_payload(notification_id: str = "42") -> dict[str, object]:
    return {
        "id": notification_id,
        "reason": "mention",
        "subject": {
            "title": "notification subject",
            "url": "https://api.github.com/repos/owner/repo/issues/7",
        },
        "updated_at": "2026-05-15T11:00:00Z",
    }


# ---------------------------------------------------------------------------
# list_issues_since
# ---------------------------------------------------------------------------


def test_list_issues_since_filters_out_pull_requests() -> None:
    """Issues API mixes PRs into its response; only true issues are yielded."""
    pr_like = _issue_payload(1)
    pr_like["pull_request"] = {"url": "https://api.github.com/repos/owner/repo/pulls/1"}
    payload = [_issue_payload(2), pr_like, _issue_payload(3)]
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=payload),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 2
    assert {it.external_id for it in items} == {"owner/repo#2", "owner/repo#3"}
    assert all(it.source_type == "issue" for it in items)


def test_list_issues_passes_since_param_when_set() -> None:
    """The ``since`` argument is serialised to ISO 8601 with ``Z`` suffix."""
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    since = datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    with _recording_client(handler) as client:
        list(list_issues_since("owner/repo", since, token=_TOKEN, client=client))

    url = captured["url"]
    assert url.path == "/repos/owner/repo/issues"
    # ``since`` is the canonical ``...Z`` ISO 8601 form.
    assert url.params["since"] == "2026-05-17T00:00:00Z"
    assert url.params["state"] == "all"
    # The raw query string is URL-encoded -- the colons in ``since`` become %3A.
    assert "since=2026-05-17T00%3A00%3A00Z" in str(url)


def test_list_issues_omits_since_when_none() -> None:
    """A ``since=None`` call omits the query parameter entirely."""
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    with _recording_client(handler) as client:
        list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert "since" not in captured["url"].params


def test_list_issues_paginates_via_link_header() -> None:
    """Two pages -- page 1 carries ``rel="next"``, page 2 has no Link header."""
    request_count = {"n": 0}
    page1 = [_issue_payload(1), _issue_payload(2)]
    page2 = [_issue_payload(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        if request_count["n"] == 1:
            # First call: return Link header pointing at the *same* path
            # with page=2; the mock transport doesn't care about query strings.
            return httpx.Response(
                200,
                json=page1,
                headers={
                    "Link": (
                        "<https://api.github.com/repos/owner/repo/issues?page=2>; "
                        'rel="next", '
                        "<https://api.github.com/repos/owner/repo/issues?page=3>; "
                        'rel="last"'
                    )
                },
            )
        return httpx.Response(200, json=page2)

    with _recording_client(handler) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 3
    assert request_count["n"] == 2


# ---------------------------------------------------------------------------
# list_pulls_since
# ---------------------------------------------------------------------------


def test_list_pulls_stops_walking_when_item_older_than_since() -> None:
    """Walk-and-stop: once an item's ``updated_at`` falls before ``since``, halt."""
    request_count = {"n": 0}
    # Three PRs, sorted descending by updated_at.
    page1 = [
        _pr_payload(101, updated_at="2026-05-17T12:00:00Z"),
        _pr_payload(100, updated_at="2026-05-15T12:00:00Z"),
        _pr_payload(99, updated_at="2026-05-10T12:00:00Z"),
    ]
    next_link = '<https://api.github.com/repos/owner/repo/pulls?page=2>; rel="next"'

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        return httpx.Response(200, json=page1, headers={"Link": next_link})

    # Threshold between item 1 (2026-05-17) and item 2 (2026-05-15).
    since = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    with _recording_client(handler) as client:
        items = list(list_pulls_since("owner/repo", since, token=_TOKEN, client=client))

    assert len(items) == 1
    assert items[0].external_id == "owner/repo#101"
    # The generator must NOT have fetched page 2 -- the short-circuit
    # is what keeps incremental sync cheap.
    assert request_count["n"] == 1


def test_list_pulls_passes_sort_and_direction_params() -> None:
    """``sort=updated`` + ``direction=desc`` are required for walk-and-stop to be sound."""
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json=[])

    with _recording_client(handler) as client:
        list(list_pulls_since("owner/repo", None, token=_TOKEN, client=client))

    assert captured["url"].params["sort"] == "updated"
    assert captured["url"].params["direction"] == "desc"
    assert captured["url"].params["state"] == "all"


# ---------------------------------------------------------------------------
# list_notifications
# ---------------------------------------------------------------------------


def test_list_notifications_returns_subject_data() -> None:
    """``subject.title`` / ``subject.url`` / ``reason`` populate the GitHubItem."""
    payload = [_notification_payload("123")]
    routes = {
        ("GET", "/notifications"): httpx.Response(200, json=payload),
    }
    with _client(routes) as client:
        items = list(list_notifications(token=_TOKEN, client=client))

    assert len(items) == 1
    item = items[0]
    assert item.source_type == "notification"
    assert item.external_id == "123"
    assert item.title == "notification subject"
    assert item.url == "https://api.github.com/repos/owner/repo/issues/7"
    assert item.summary == "mention"


def test_list_notifications_normalises_whitespace_only_reason_to_none() -> None:
    """Issue #343: SSOT wiring — whitespace-only ``reason`` collapses to ``None``.

    GitHub's notification ``reason`` field is a closed enum
    (``subscribed`` / ``mention`` / ``assign`` / ``...``) so a
    whitespace-only value is not actually reachable in production —
    but pinning the helper wiring here keeps the
    ``GitHubItem.summary`` semantics SSOT-uniform with the four
    helper-based mappers (ms365 / google_workspace / google_mail /
    google_calendar) and Slack / Teams (#337). A future schema change
    that lets ``reason`` carry free-form strings cannot start leaking
    whitespace into ``sources.summary`` without this assertion
    failing first.
    """
    payload: list[dict[str, object]] = [
        {
            "id": "ws-1",
            "reason": "   \n\t ",
            "subject": {
                "title": "notification subject",
                "url": "https://api.github.com/repos/owner/repo/issues/7",
            },
            "updated_at": "2026-05-15T11:00:00Z",
        }
    ]
    routes = {
        ("GET", "/notifications"): httpx.Response(200, json=payload),
    }
    with _client(routes) as client:
        items = list(list_notifications(token=_TOKEN, client=client))

    assert len(items) == 1
    assert items[0].summary is None


# ---------------------------------------------------------------------------
# Optional-field normalisation
# ---------------------------------------------------------------------------


def test_normalise_handles_missing_optional_fields() -> None:
    """Missing body / subject collapse to safe defaults, not exceptions."""
    issue_without_body = _issue_payload(7, body=None)
    notification_without_subject: dict[str, object] = {
        "id": "999",
        "reason": "subscribed",
        "updated_at": "2026-05-15T11:00:00Z",
        # subject intentionally absent
    }
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[issue_without_body]),
        ("GET", "/notifications"): httpx.Response(200, json=[notification_without_subject]),
    }
    with _client(routes) as client:
        issues = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))
        notifications = list(list_notifications(token=_TOKEN, client=client))

    assert issues[0].summary is None
    assert notifications[0].title == "(no title)"
    assert notifications[0].url == ""


# ---------------------------------------------------------------------------
# Error branches
# ---------------------------------------------------------------------------


def test_raises_github_auth_error_on_401() -> None:
    """401 -> :class:`GitHubAuthError` (a :class:`GitHubAPIError` subclass)."""
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(401, text="Bad credentials"),
    }
    with _client(routes) as client:
        with pytest.raises(GitHubAuthError):
            next(iter(list_issues_since("owner/repo", None, token=_TOKEN, client=client)))


def test_raises_github_rate_limit_error_on_403_with_rate_limit_body() -> None:
    """403 + ``"rate limit"`` body -> :class:`GitHubRateLimitError`."""
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(
            403,
            text="API rate limit exceeded for user",
            headers={"X-RateLimit-Reset": "1747000000"},
        ),
    }
    with _client(routes) as client:
        with pytest.raises(GitHubRateLimitError) as excinfo:
            next(iter(list_issues_since("owner/repo", None, token=_TOKEN, client=client)))
    # The reset header surfaces in the message so the caller can log it.
    assert "1747000000" in str(excinfo.value)


def test_raises_github_api_error_on_unexpected_500() -> None:
    """A non-auth / non-rate-limit failure raises the generic :class:`GitHubAPIError`."""
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(500, text="boom"),
    }
    with _client(routes) as client:
        with pytest.raises(GitHubAPIError) as excinfo:
            next(iter(list_issues_since("owner/repo", None, token=_TOKEN, client=client)))
    # Auth / RateLimit are subclasses; assert the exact raised type to lock
    # the contract (callers may use ``except GitHubAPIError`` as a catch-all).
    assert type(excinfo.value) is GitHubAPIError


def test_unexpected_payload_shape_raises_api_error() -> None:
    """A dict in place of a list response triggers the shape guard."""
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(
            200,
            content=json.dumps({"message": "huh"}),
            headers={"content-type": "application/json"},
        ),
    }
    with _client(routes) as client:
        with pytest.raises(GitHubAPIError) as excinfo:
            next(iter(list_issues_since("owner/repo", None, token=_TOKEN, client=client)))
    assert "expected list" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Helpers: ISO 8601 round-trip + external_id format
# ---------------------------------------------------------------------------


def test_iso_helpers_roundtrip() -> None:
    """A UTC datetime survives ``_to_iso_utc`` -> ``_parse_iso_utc`` unchanged."""
    dt = datetime(2026, 5, 17, 9, 30, 45, tzinfo=UTC)
    assert _parse_iso_utc(_to_iso_utc(dt)) == dt


def test_external_id_format_for_issues_and_pulls() -> None:
    """Both issues and PRs use ``"<owner>/<repo>#<number>"`` -- the connector
    relies on this contract for ``SourceService.observe`` deduplication.
    """
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[_issue_payload(42)]),
        ("GET", "/repos/owner/repo/pulls"): httpx.Response(
            200, json=[_pr_payload(42, updated_at="2026-05-15T12:00:00Z")]
        ),
    }
    with _client(routes) as client:
        issues = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))
        pulls = list(list_pulls_since("owner/repo", None, token=_TOKEN, client=client))

    assert issues[0].external_id == "owner/repo#42"
    assert pulls[0].external_id == "owner/repo#42"


# ---------------------------------------------------------------------------
# GitHubItem is the only public payload shape
# ---------------------------------------------------------------------------


def test_github_item_is_frozen_and_minimal() -> None:
    """``GitHubItem`` is frozen + ``__slots__`` -- callers can rely on the
    absence of arbitrary attributes leaking from the raw payload
    (ADR-0005 External Content Minimization).
    """
    from dataclasses import FrozenInstanceError

    item = GitHubItem(
        source_type="issue",
        external_id="owner/repo#1",
        title="t",
        url="u",
        summary=None,
        updated_at=datetime(2026, 5, 15, 10, 20, 30, tzinfo=UTC),
    )
    # Frozen: mutation of an existing field raises FrozenInstanceError.
    with pytest.raises(FrozenInstanceError):
        item.title = "mutated"  # type: ignore[misc]
    # Slots: the class has __slots__ so no __dict__ leaks raw payload fields.
    assert not hasattr(item, "__dict__")
    assert set(GitHubItem.__slots__) == {
        "source_type",
        "external_id",
        "title",
        "url",
        "summary",
        "body",
        "updated_at",
    }


# ---------------------------------------------------------------------------
# ADR-0005 summary / title length caps (Phase 7 Validation §3 parity)
# ---------------------------------------------------------------------------


def test_summary_max_chars_constant_pins_phase7_convention() -> None:
    """The GitHub connector ships the same 200-char cap as Slack / MS365 / Box.

    ADR-0010 Phase 7 Validation §3 mandates ``summary ≤ 200 chars`` for
    every connector. Pinning the constant explicitly here keeps the
    cross-connector contract visible at review time: bumping the
    GitHub side in isolation would silently violate ADR-0005.
    """
    assert SUMMARY_MAX_CHARS == 200
    assert TITLE_MAX_CHARS == 500


def test_summary_truncated_to_200_chars_with_ellipsis() -> None:
    """An issue body whose first line exceeds 200 chars is clipped to exactly 200.

    The truncated summary ends with ``"…"`` (U+2026, one unicode
    character) so operators reading recall / brief output see at a
    glance that the preview was cut. The Phase 7 mappers
    (Slack / MS365 / Box) use the same shape — the cross-connector
    convention is pinned in :func:`test_summary_max_chars_constant_pins_phase7_convention`.
    """
    long_body = "a" * 250
    payload = _issue_payload(1, body=long_body)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 1
    summary = items[0].summary
    assert summary is not None
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")
    # The clipped prefix matches the input verbatim up to the ellipsis.
    assert summary[:-1] == "a" * (SUMMARY_MAX_CHARS - 1)


def test_summary_short_first_line_preserved_verbatim() -> None:
    """A first line shorter than the cap survives the mapper unchanged.

    Pins that the truncation helper is a no-op below the cap — long-form
    PR descriptions stay readable in recall output rather than being
    artificially clipped.
    """
    payload = _pr_payload(1, updated_at="2026-05-15T12:00:00Z", body="short body line")
    routes = {
        ("GET", "/repos/owner/repo/pulls"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_pulls_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 1
    assert items[0].summary == "short body line"


def test_summary_exactly_at_cap_preserved_verbatim() -> None:
    """A first line whose length is *exactly* :data:`SUMMARY_MAX_CHARS` is returned verbatim.

    Mirrors the Phase 7 boundary contract — appending the ellipsis at
    the boundary would push past the cap and defeat the truncation
    rule. Pinned here so a future refactor that flips the comparison
    from ``<=`` to ``<`` fails loudly instead of silently overshooting.
    """
    body = "x" * SUMMARY_MAX_CHARS
    payload = _issue_payload(1, body=body)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert items[0].summary == body
    assert len(items[0].summary or "") == SUMMARY_MAX_CHARS


def test_summary_empty_body_handled() -> None:
    """``body=""`` and ``body=None`` both yield ``summary=None`` (no exception)."""
    payload_empty = _issue_payload(1, body="")
    payload_none = _issue_payload(2, body=None)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(
            200, json=[payload_empty, payload_none]
        ),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 2
    assert all(it.summary is None for it in items)


def test_summary_truncates_long_first_line_of_multiline_body() -> None:
    """Truncation runs on the *first line*, not the whole body.

    The historical contract is that ``_first_line`` picks the first
    non-empty stripped line and clips it; trailing lines are
    discarded. This test pins both halves — only the first line
    reaches the mapper, and that line is then clamped to the cap.
    """
    long_first_line = "first " * 50  # ~300 chars
    body = f"{long_first_line}\nsecond line\nthird line"
    payload = _issue_payload(1, body=body)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    summary = items[0].summary
    assert summary is not None
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")
    # The discarded lines never appear in the mapped summary.
    assert "second line" not in summary
    assert "third line" not in summary


def test_summary_preserves_unicode_character_count() -> None:
    """The cap counts unicode characters, not UTF-8 bytes.

    A 250-char Japanese body becomes a 200-character summary (NOT a
    200-byte truncation that mangles a multi-byte glyph mid-codepoint).
    The Phase 7 connectors take the same stance — using
    :func:`len` on a ``str`` counts code points so emoji / CJK
    summaries are clipped on glyph boundaries.
    """
    japanese_body = "あ" * 250  # 250 chars, 750 UTF-8 bytes
    payload = _issue_payload(1, body=japanese_body)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    summary = items[0].summary
    assert summary is not None
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")
    # The non-ellipsis prefix is a contiguous run of the input character —
    # no mid-codepoint split.
    assert summary[:-1] == "あ" * (SUMMARY_MAX_CHARS - 1)


def test_notification_long_reason_truncated() -> None:
    """``notification.reason`` is also subject to the 200-char cap.

    The reason field is normally a short enum-like string (``"mention"``,
    ``"subscribed"``, ...) but GitHub does not contractually bound its
    length, so the mapper truncates defensively to keep the Pydantic
    ``SourceObserved.summary`` ``max_length=200`` happy.
    """
    long_reason = "r" * 250
    payload: dict[str, object] = {
        "id": "123",
        "reason": long_reason,
        "subject": {"title": "subject", "url": "https://example.invalid/x"},
        "updated_at": "2026-05-15T11:00:00Z",
    }
    routes = {
        ("GET", "/notifications"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_notifications(token=_TOKEN, client=client))

    summary = items[0].summary
    assert summary is not None
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_title_truncated_to_500_chars_with_ellipsis() -> None:
    """A title longer than :data:`TITLE_MAX_CHARS` is clamped, not propagated raw.

    Before this PR, the GitHub mapper forwarded the API ``title``
    verbatim — a 600-char title would crash downstream at the
    :class:`SourceObserved.title` ``max_length=500`` Pydantic bound.
    Truncating defensively here keeps the connector robust against
    operator-authored edge cases (Issue / PR titles have no
    GitHub-side cap).
    """
    long_title = "t" * 600
    payload = _issue_payload(1)
    payload["title"] = long_title
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    title = items[0].title
    assert len(title) == TITLE_MAX_CHARS
    assert title.endswith("…")
    assert title[:-1] == "t" * (TITLE_MAX_CHARS - 1)


def test_title_short_preserved_verbatim() -> None:
    """A title below the cap survives unchanged — no spurious ellipsis."""
    payload = _issue_payload(42)  # default title "issue #42"
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert items[0].title == "issue #42"
    assert not items[0].title.endswith("…")


# ---------------------------------------------------------------------------
# ADR-0020 (Full Local Content Retention): body field retained verbatim
# ---------------------------------------------------------------------------


def test_normalise_issue_retains_full_body() -> None:
    """ADR-0020: the issue body is retained verbatim in :attr:`GitHubItem.body`.

    The ≤200-char ``summary`` is the recognition preview; ``body``
    carries the full markdown so body-based search (Sub-issue B) and
    propose / reply-draft (Sub-issue E) have something to work from.
    A pathologically long body — well past the summary cap — must
    survive without truncation on the ``body`` field.
    """
    long_body = "First line preview\n\n" + ("paragraph body " * 50)
    assert len(long_body) > SUMMARY_MAX_CHARS  # > 200 chars (sanity)
    payload = _issue_payload(1, body=long_body)
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 1
    item = items[0]
    # ``body`` is the full markdown verbatim — no truncation, no ellipsis.
    assert item.body == long_body
    # ``summary`` is the first non-empty line clamped to the cap.
    assert item.summary is not None
    assert len(item.summary) <= SUMMARY_MAX_CHARS


def test_normalise_pull_retains_full_body() -> None:
    """ADR-0020: PR descriptions are likewise retained verbatim on ``body``."""
    long_body = "PR opener\n\n" + ("details paragraph " * 40)
    assert len(long_body) > SUMMARY_MAX_CHARS  # sanity
    payload = _pr_payload(7, updated_at="2026-05-15T12:00:00Z", body=long_body)
    routes = {
        ("GET", "/repos/owner/repo/pulls"): httpx.Response(200, json=[payload]),
    }
    with _client(routes) as client:
        items = list(list_pulls_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 1
    item = items[0]
    assert item.body == long_body
    assert item.summary is not None
    assert len(item.summary) <= SUMMARY_MAX_CHARS


def test_normalise_issue_body_none_when_missing() -> None:
    """A missing / empty body normalises to ``None`` on ``body`` (not ``""``).

    Mirrors the summary path: an unambiguous "no body" marker keeps
    downstream "has a body" / FTS index checks simple.
    """
    payload_none = _issue_payload(1, body=None)
    payload_empty = _issue_payload(2, body="")
    routes = {
        ("GET", "/repos/owner/repo/issues"): httpx.Response(
            200, json=[payload_none, payload_empty]
        ),
    }
    with _client(routes) as client:
        items = list(list_issues_since("owner/repo", None, token=_TOKEN, client=client))

    assert len(items) == 2
    assert all(it.body is None for it in items)
