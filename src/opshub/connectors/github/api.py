"""GitHub REST API thin wrappers (Phase 3 step B2).

Pure I/O primitives for fetching Issues / Pull Requests / Notifications.
B3 (GitHubConnector) composes these into the sync workflow:

    cursor (= last updated_at ISO 8601) -> list_*_since(repo, cursor)
        -> for each item: source_service.observe(...)
        -> new cursor = max(updated_at across items)

Why httpx over PyGithub (per docs/phase-3-plan.md Open Q #1):

- ~5x lighter import footprint (relevant to the cold-start budget,
  even though this module is only imported when ``opshub connector
  sync github`` runs -- connectors/github/api.py never appears on the
  ``opshub --help`` cold path per the M6 import-whitelist test)
- Direct control over the ``If-Modified-Since`` / ``ETag`` semantics
  we need for incremental sync
- Static type integrity: httpx has clean type hints; PyGithub's
  ``GithubObject`` lazy attribute system trips pyright strict

This module is intentionally narrow -- Phase 3 MVP fetches 3 entity
types. Slack / MS365 / Box will be entirely separate modules and
will NOT inherit from this. Resist the urge to factor a shared
``BaseHttpConnector``; each SaaS has its own auth / pagination /
rate-limit shape, and a premature base would lock in GitHub's
quirks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

from opshub.core.errors import OpsHubError
from opshub.core.text_limits import normalise_optional_text

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "SUMMARY_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "GitHubAPIError",
    "GitHubAuthError",
    "GitHubItem",
    "GitHubRateLimitError",
    "list_issues_since",
    "list_notifications",
    "list_pulls_since",
]

_DEFAULT_BASE_URL = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_PER_PAGE = 100
_USER_AGENT = "opshub-connector/0.1"

#: Hard cap on :attr:`GitHubItem.summary` (in unicode characters) enforced
#: by :func:`_normalise_issue` / :func:`_normalise_pull` /
#: :func:`_normalise_notification`. The cap matches the Phase 7
#: connectors' ``SUMMARY_MAX_CHARS = 200`` convention (Slack / MS365 /
#: Box mappers) so every ``SourceObserved`` row in the event log obeys
#: ADR-0005 (External Content Minimization) uniformly. ADR-0010 Phase 7
#: Validation §3 pins the rule ("全 connector で summary ≤ 200 chars
#: enforce") — this constant is the GitHub-side enforcement point.
#:
#: The :class:`opshub.domain.events.SourceObserved` Pydantic model also
#: carries ``max_length=200`` on its ``summary`` field; the mapper-side
#: truncation here lets the GitHub connector ship long-body issues
#: gracefully instead of raising :class:`pydantic.ValidationError`.
SUMMARY_MAX_CHARS = 200

#: Hard cap on :attr:`GitHubItem.title` (in unicode characters). Mirrors
#: :class:`SourceObserved`'s ``title`` ``max_length=500`` Pydantic
#: constraint so the mapper clamps long GitHub titles defensively
#: rather than relying on a downstream ``ValidationError`` to surface
#: the problem. The GitHub REST API rarely emits titles longer than a
#: few hundred chars, but Issue / PR titles are user-input and have no
#: server-side cap, so a defensive clamp here keeps the connector
#: robust against operator-authored edge cases.
TITLE_MAX_CHARS = 500


class GitHubAPIError(OpsHubError):
    """Generic GitHub API failure (non-success status outside the auth / rate-limit branches)."""


class GitHubAuthError(GitHubAPIError):
    """Raised on 401 -- the PAT is missing or revoked. Caller should surface to the user."""


class GitHubRateLimitError(GitHubAPIError):
    """Raised on 403 with a primary / secondary rate-limit response.

    Caller should fail-fast (Phase 3 Section 4 Q3).
    """


@dataclass(frozen=True, slots=True)
class GitHubItem:
    """Normalised view of one fetched GitHub item.

    Connector code reads only these fields. The raw API response is
    discarded after normalisation so the connector layer never sees
    payloads we did not intend to retain (ADR-0005 External Content
    Minimization).

    Attributes:
        source_type: ``"issue"`` / ``"pull_request"`` / ``"notification"``
        external_id: the canonical reference within ``connector_name="github"``
            -- for issues / PRs: ``"<owner>/<repo>#<number>"``; for
            notifications: the notification ``id`` (stringified)
        title: 1-line title clamped to :data:`TITLE_MAX_CHARS` (500)
            unicode characters by the normaliser. The GitHub REST API
            rarely exceeds that, but Issue / PR titles are user-input
            without a server-side cap so the mapper truncates
            defensively to satisfy :class:`SourceObserved.title`'s
            ``max_length=500`` Pydantic bound. A clipped title gains a
            trailing ``"…"`` (U+2026) so operators see it was cut.
        url: the canonical web URL (``html_url`` for issues / PRs,
            constructed from ``subject.url`` for notifications). The
            notification path funnels its candidate through
            :func:`opshub.core.text_limits.normalise_optional_text` so
            empty *and* whitespace-only inputs collapse to ``""``
            (issue #343) — SSOT-uniform with the four helper-based
            mappers' ``SourceObserved.url`` handling. The empty-string
            fallback (rather than ``None``) preserves the typed-``str``
            contract on this field; downstream
            :class:`SourceObserved.url` is a ``str | None`` Pydantic
            field, so a future widening here would be straightforward.
        summary: a 1-2 sentence summary derived from the API payload
            (e.g. issue ``body`` first line, or notification reason),
            clamped to :data:`SUMMARY_MAX_CHARS` (200) unicode
            characters with a trailing ``"…"`` when truncated. ``None``
            if the API gave nothing usable. The cap honours ADR-0005
            (External Content Minimization) and matches Phase 7
            connectors (Slack / MS365 / Box) so every connector ships
            uniform summary shape — see ADR-0010 Phase 7 Validation §3.
        updated_at: the API-reported ``updated_at`` (tz-aware UTC), used
            by the connector to advance its cursor.
        author_handle: the GitHub login of the item's author (Phase 25-A,
            ADR-0010 §改訂) — ``user.login`` for issues / PRs, ``None``
            for notifications (the ``/notifications`` payload is
            user-scoped and carries no per-item author). The connector
            threads this onto :attr:`SourceObserved.author_handle` so the
            Phase 25 person-axis resolver (25-B) can group GitHub logins
            with the operator's other identities. GitHub does not return
            a separate display name on the issue / PR list payloads, so
            :attr:`SourceObserved.author_display` is left ``None``.
    """

    source_type: str
    external_id: str
    title: str
    url: str
    summary: str | None
    updated_at: datetime
    body: str | None = None
    author_handle: str | None = None


def list_issues_since(
    repo: str,
    since: datetime | None,
    *,
    token: str,
    client: httpx.Client | None = None,
) -> Iterator[GitHubItem]:
    """Yield issues updated at or after ``since``.

    Uses ``GET /repos/{owner}/{repo}/issues?since=...&state=all&per_page=100``
    and paginates via the ``Link: <...>; rel="next"`` header. The
    GitHub Issues API returns *both* issues and pull requests; we
    filter out pull requests here (an issue payload that carries a
    ``pull_request`` key is actually a PR -- those are fetched by
    ``list_pulls_since``).
    """
    params: dict[str, str] = {"state": "all", "per_page": str(_DEFAULT_PER_PAGE)}
    if since is not None:
        params["since"] = _to_iso_utc(since)
    path = f"/repos/{repo}/issues"
    for item in _paginate(path, params, token=token, client=client):
        if item.get("pull_request") is not None:
            continue
        yield _normalise_issue(repo, item)


def list_pulls_since(
    repo: str,
    since: datetime | None,
    *,
    token: str,
    client: httpx.Client | None = None,
) -> Iterator[GitHubItem]:
    """Yield pull requests updated at or after ``since``.

    Uses ``GET /repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100``
    and short-circuits pagination once an item's ``updated_at`` is
    older than ``since`` (the pulls endpoint does not honour
    ``?since=`` directly, unlike issues; sort + walk-and-stop is the
    documented workaround in GitHub's API guide).
    """
    params: dict[str, str] = {
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": str(_DEFAULT_PER_PAGE),
    }
    path = f"/repos/{repo}/pulls"
    for item in _paginate(path, params, token=token, client=client):
        updated_at = _parse_iso_utc(item["updated_at"])
        if since is not None and updated_at < since:
            break  # subsequent pages are even older
        yield _normalise_pull(repo, item, updated_at=updated_at)


def list_notifications(
    *,
    token: str,
    since: datetime | None = None,
    client: httpx.Client | None = None,
) -> Iterator[GitHubItem]:
    """Yield notifications for the authenticated user.

    Uses ``GET /notifications?per_page=100`` and (when ``since`` is
    set) the ``since`` query param for cache friendliness. Unlike
    issues / pulls there is no per-repo scope here -- notifications
    are user-scoped.
    """
    params: dict[str, str] = {"per_page": str(_DEFAULT_PER_PAGE)}
    if since is not None:
        params["since"] = _to_iso_utc(since)
    for item in _paginate("/notifications", params, token=token, client=client):
        yield _normalise_notification(item)


# ---------- helpers (private) ----------


def _paginate(
    path: str,
    params: dict[str, str],
    *,
    token: str,
    client: httpx.Client | None,
) -> Iterator[dict[str, Any]]:
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=_DEFAULT_BASE_URL,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {token}",
            },
        )
    # Caller-supplied client: assume the auth header is already set.
    try:
        url: str | None = path
        request_params: dict[str, str] | None = params
        while url is not None:
            response = client.get(url, params=request_params)
            _raise_for_status(response)
            payload: object = response.json()
            if not isinstance(payload, list):
                raise GitHubAPIError(
                    f"unexpected GitHub response shape at {url}: "
                    f"expected list, got {type(payload).__name__}"
                )
            # The isinstance narrow above gives us ``list`` but pyright
            # widens its element type to Unknown -- cast to the documented
            # GitHub contract (JSON objects keyed by string).
            items = cast(list[dict[str, Any]], payload)
            yield from items
            # On subsequent pages the Link header already encodes ``per_page`` etc.
            request_params = None
            url = _next_link(response.headers.get("link"))
    finally:
        if owns_client:
            client.close()


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise GitHubAuthError("GitHub returned 401 -- token missing or revoked")
    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise GitHubRateLimitError(
            f"GitHub rate limit hit (status 403); retry after "
            f"{response.headers.get('X-RateLimit-Reset', 'unknown')}"
        )
    if response.status_code >= 400:
        raise GitHubAPIError(
            f"GitHub returned {response.status_code} for "
            f"{response.request.url}: {response.text[:200]}"
        )


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if section.endswith('rel="next"'):
            # Section format: ``<https://...>; rel="next"``
            url_part = section.split(";", 1)[0].strip()
            if url_part.startswith("<") and url_part.endswith(">"):
                return url_part[1:-1]
    return None


def _to_iso_utc(dt: datetime) -> str:
    """GitHub expects ISO 8601 in UTC ending with ``Z``.

    Naive datetimes are assumed to be UTC (matching ``opshub.core.time``
    conventions); tz-aware values are converted to UTC before formatting.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(s: str) -> datetime:
    """Parse the ``...Z``-suffixed ISO 8601 GitHub returns."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _author_login(item: dict[str, Any]) -> str | None:
    """Return the GitHub login of an issue / PR author (Phase 25-A).

    GitHub nests the author under ``user.login`` on the issue / PR list
    payloads. A deleted / ghost account (rare) serialises ``user`` as
    ``null``; we normalise that — and any whitespace-only login — to
    ``None`` so :attr:`SourceObserved.author_handle` stores ``NULL``
    rather than an empty string (mirrors the connector family's
    empty→``None`` discipline). The login is the connector-native join
    key the Phase 25 person-axis resolver (25-B) groups on.
    """
    user = item.get("user")
    if not isinstance(user, dict):
        return None
    login = cast(dict[str, Any], user).get("login")
    if not isinstance(login, str) or not login.strip():
        return None
    return login


def _normalise_issue(repo: str, item: dict[str, Any]) -> GitHubItem:
    return GitHubItem(
        source_type="issue",
        external_id=f"{repo}#{item['number']}",
        title=_truncate(item["title"], TITLE_MAX_CHARS),
        url=item["html_url"],
        summary=_first_line(item.get("body")),
        updated_at=_parse_iso_utc(item["updated_at"]),
        # Phase 10 (ADR-0020): retain the full issue body. The summary
        # above stays the ≤200-char preview; ``body`` carries the
        # untruncated markdown for body-based search (Sub-issue B).
        body=_body_text(item.get("body")),
        # Phase 25-A (ADR-0010 §改訂): the issue author's GitHub login.
        author_handle=_author_login(item),
    )


def _normalise_pull(repo: str, item: dict[str, Any], *, updated_at: datetime) -> GitHubItem:
    return GitHubItem(
        source_type="pull_request",
        external_id=f"{repo}#{item['number']}",
        title=_truncate(item["title"], TITLE_MAX_CHARS),
        url=item["html_url"],
        summary=_first_line(item.get("body")),
        updated_at=updated_at,
        # Phase 10 (ADR-0020): retain the full PR description body.
        body=_body_text(item.get("body")),
        # Phase 25-A (ADR-0010 §改訂): the PR author's GitHub login.
        author_handle=_author_login(item),
    )


def _normalise_notification(item: dict[str, Any]) -> GitHubItem:
    raw_subject = item.get("subject")
    subject: dict[str, Any] = (
        cast(dict[str, Any], raw_subject) if isinstance(raw_subject, dict) else {}
    )
    subject_url = subject.get("url")
    item_url = item.get("url")
    # Issue #343 (PR #355 followup): the optional ``url`` candidates
    # are funnelled through :func:`normalise_optional_text` for the
    # same SSOT reason PR #355 routed ``summary`` through it — a
    # whitespace-only ``subject.url`` / ``item.url`` would otherwise
    # land on ``GitHubItem.url`` (and from there on
    # ``SourceObserved.url`` via :mod:`connector`) as a visually-empty
    # link. The trailing ``or ""`` keeps the closed-string contract on
    # ``GitHubItem.url`` (typed ``str``, not ``str | None``) intact:
    # widening the type would ripple through ``connector.py`` + every
    # downstream ``GitHubItem.url`` consumer and is intentionally out
    # of scope for this audit followup. The historical
    # "url=='' when neither candidate is present" semantics
    # (asserted by :func:`test_normalise_handles_missing_optional_fields`)
    # are preserved — whitespace-only candidates now collapse to the
    # same ``""`` sentinel rather than leaking the whitespace through.
    url: str = normalise_optional_text(subject_url or item_url) or ""
    subject_title = subject.get("title")
    title: str = subject_title if isinstance(subject_title, str) and subject_title else "(no title)"
    reason = item.get("reason")
    raw_summary: str | None = reason if isinstance(reason, str) else None
    return GitHubItem(
        source_type="notification",
        external_id=str(item["id"]),
        title=_truncate(title, TITLE_MAX_CHARS),
        url=url,
        # Issue #343: GitHub's notification ``reason`` field is a
        # closed enum (``subscribed`` / ``mention`` / ...) so
        # whitespace-only input is not actually reachable here, but
        # routing the result through
        # :func:`opshub.core.text_limits.normalise_optional_text`
        # keeps the "summary is missing" semantics SSOT-uniform with
        # the four helper-based mappers (ms365 / google_workspace /
        # google_mail / google_calendar) and the Slack / Teams
        # mappers (#337). The wrap (rather than mutating
        # :func:`_truncate_optional` itself) localises the whitespace
        # normalisation to the one caller that needs it; the
        # general-purpose truncation primitive stays semantics-free
        # for any future caller that genuinely wants to preserve
        # whitespace.
        summary=normalise_optional_text(_truncate_optional(raw_summary, SUMMARY_MAX_CHARS)),
        updated_at=_parse_iso_utc(item["updated_at"]),
    )


def _body_text(text: str | None) -> str | None:
    """Return the full body text (Phase 10, ADR-0020), or ``None`` if empty.

    Unlike :func:`_first_line` this performs **no** truncation — ADR-0020
    Full Local Content Retention keeps the whole body. An empty / missing
    body normalises to ``None`` at the helper level; ``github/connector.py``
    line 135 then resolves ``item.body or item.summary or item.title`` to
    satisfy the post-#470 ``SourceObserved.body`` non-empty contract
    (projection persists a non-empty string per
    `ADR-0010 §不変条件 6 <../../docs/adr/0010-connector-contract.md>`_).
    """
    if not text or not text.strip():
        return None
    return text


def _first_line(text: str | None) -> str | None:
    """Return the first non-empty line of ``text`` clamped to :data:`SUMMARY_MAX_CHARS`.

    Phase 3 originally returned the whole first line verbatim, which
    could exceed :class:`SourceObserved.summary`'s 200-char Pydantic
    cap and trigger a :class:`pydantic.ValidationError` on long-issue
    bodies. The truncation here enforces ADR-0005 (External Content
    Minimization) at the mapper layer — matching Phase 7 connectors
    (Slack / MS365 / Box) which carry their own
    ``SUMMARY_MAX_CHARS = 200`` constant. ADR-0010 Phase 7 Validation
    §3 pins the rule across every connector.

    A clipped line gains a trailing ``"…"`` (U+2026) so operators see
    at a glance that the preview was cut; the ellipsis is one unicode
    *character* (not three ASCII dots) so the cap counts in
    character-terms rather than byte-terms, matching the Phase 7
    precedent.
    """
    if not text:
        return None
    head = text.strip().splitlines()
    if not head:
        return None
    return _truncate(head[0], SUMMARY_MAX_CHARS)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` unicode characters with a ``"…"`` tail.

    Mirrors the Phase 7 connector convention (see
    :mod:`opshub.connectors.slack.mapper._truncate` and
    :mod:`opshub.connectors.box.mapper._build_summary`): a value that
    is **exactly** ``max_chars`` characters is returned verbatim, and
    a longer value is clipped to ``max_chars - 1`` characters with a
    single ``"…"`` appended so the final string is exactly
    ``max_chars`` characters long. The ellipsis is one unicode
    character so the cap counts in character-terms rather than
    byte-terms — emoji / Japanese / CJK summaries are counted by
    glyph, not by UTF-8 byte width.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _truncate_optional(text: str | None, max_chars: int) -> str | None:
    """``None``-tolerant wrapper around :func:`_truncate` for nullable summary fields."""
    if text is None:
        return None
    return _truncate(text, max_chars)
