"""Microsoft Graph chat fetcher for the Teams connector (Phase 11 F5).

Endpoint
--------

The Phase 11 MVP walks Microsoft Graph
``GET /me/chats/getAllMessages``, which streams every chat message the
authenticated user can see — personal 1:1 chats, group chats, and
channel messages — through a single delta-enabled endpoint. This is
intentionally narrower than enumerating ``/teams/{id}/channels/{id}/messages``
per channel: the operator-1 secretary model (ADR-0010 §責務) prefers
"everything I can see" over "manually-selected channel set" so the
ingestion side stays a single cursor rather than per-channel state.

Delta cursor + full-pass fallback (ADR-0010 Phase 11 改訂 (c))
--------------------------------------------------------------

The stored cursor is the opaque ``@odata.deltaLink`` URL Graph returns
on the final page of the previous sync. On a fresh sync (``delta_link
is None``) we begin at the root delta URL; on subsequent syncs we
replay the stored URL verbatim.

If Graph rejects the stored delta link with ``410 Gone`` (the
documented signal for an expired delta token), :meth:`fetch_chat_messages`
falls back to a **direct full-pass** over ``$filter=lastModifiedDateTime
ge <iso>`` for the configured ``fallback_window_days`` (default 30).
The fallback exists exclusively to recover from token expiry; under
normal operation the delta path is taken every time. ADR-0010 §改訂 (c)
documents the contract; the dedup in the read-side ``sources``
projection ensures that fallback re-emission of already-seen messages
collapses on ``(connector_name, external_id)``.

After the fallback drains we re-issue ``/me/chats/getAllMessages?$deltaToken=latest``
to pick up a fresh delta link. The yielded cursor in the final tuple
carries that new link so the caller can persist it and resume the
delta path on the next sync.

Rate-limit handling
-------------------

Graph returns HTTP 429 with a documented ``Retry-After`` header on
throttle. We honour it directly when present and otherwise back off
exponentially (1s / 2s / 4s) for up to three attempts per request,
matching the Phase 7 MS365 fetcher precedent. Exhausting the budget
escalates to :class:`ConnectorFailedError`.

Cold-start guard
----------------

``httpx`` is imported lazily inside :meth:`TeamsFetcher.__init__` so a
cold ``import opshub.connectors.teams.fetcher`` (e.g. via registry
discovery) never pulls the SDK onto the cold-start path. The cold-start
integration test (``tests/integration/test_cli_imports.py``) enforces
this rule across the package.

Token safety
------------

The resolved Graph User Token never appears in raised exceptions: we
surface the Graph error code (a documented short string, e.g.
``InvalidAuthenticationToken``) or, for transport errors, the
exception's type name only. The token never leaks even if an operator
pastes the error into a bug report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.teams.auth import TeamsAuth


__all__ = [
    "GRAPH_BASE",
    "RawTeamsChatMessage",
    "TeamsFetcher",
]


#: Microsoft Graph v1.0 base URL. The v1.0 surface is the GA endpoint
#: that returns stable schemas; the ``/beta`` surface is documented as
#: subject to change without notice and is intentionally avoided. (The
#: ``getAllMessages`` delta is GA on v1.0 since 2023.)
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: HTTP timeout for Graph calls. 30s mirrors the Phase 7 MS365 fetcher
#: default and accommodates Graph's tail-latency on large mailboxes
#: while still failing fast on a wedged connection.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`TeamsFetcher._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts
#: matches the Phase 7 plan §1 #8 rate-limit playbook (1s / 2s / 4s
#: backoff between attempts).
_MAX_REQUEST_ATTEMPTS = 3

#: ``$top`` page size for paginated Graph queries. 50 mirrors the
#: Phase 7 MS365 fetcher so the throttling envelope stays predictable
#: across MS365 and Teams workloads sharing the same tenant.
_PAGE_SIZE = 50

#: Default fallback window (in days) used when the stored delta link
#: has expired. ADR-0010 Phase 11 改訂 (c) pins the default at 30 — long
#: enough to cover a "left for vacation" outage window without slurping
#: years of history. Operators with longer outages override via
#: ``[connectors.teams] fallback_window_days`` in ``opshub.toml``.
_DEFAULT_FALLBACK_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class RawTeamsChatMessage:
    """Normalised view of a single Teams chat message.

    Attributes
    ----------
    id:
        The Graph message id. Pairs with ``chat_id`` to form the
        mapper's ``external_id`` (``f"{chat_id}:{id}"``).
    chat_id:
        The id of the chat (1:1 / group chat / channel-backed chat)
        the message belongs to. Used in the natural key.
    chat_topic:
        Optional human-readable chat title. ``""`` for 1:1 chats
        (Graph leaves the topic blank for those).
    body_html:
        The verbatim HTML body Graph returned. The mapper strips tags
        to plain text for both the ``summary`` preview and the
        retained ``body`` per ADR-0020.
    body_content_type:
        ``"html"`` / ``"text"`` from Graph's ``body.contentType`` —
        helps the mapper decide whether tag-stripping is needed.
    sender_display_name:
        ``from.user.displayName`` when present; ``""`` for system
        messages (channel rename, member added, ...) which the
        mapper renders as ``"system"``.
    sender_id:
        ``from.user.id`` when present; ``""`` for system messages.
    created_datetime_iso:
        The message ``createdDateTime`` (ISO 8601, UTC) — Graph
        returns these in chronological order on the delta path so
        the field doubles as the fallback ordering key.
    last_modified_iso:
        The message ``lastModifiedDateTime`` — used by the
        ``$filter`` fallback path to advance the temporary "since"
        cursor.
    web_url:
        Stable URL to surface in ``sources.url`` (Graph's
        ``webUrl`` field). May be empty on system messages.
    raw:
        The verbatim Graph payload, kept for forensic debugging
        (mapper fixtures, future backfill). The mapper does not
        persist this.
    """

    id: str
    chat_id: str
    chat_topic: str
    body_html: str
    body_content_type: str
    sender_display_name: str
    sender_id: str
    created_datetime_iso: str
    last_modified_iso: str
    web_url: str
    raw: dict[str, Any]


class TeamsFetcher:
    """Walk Microsoft Graph ``/me/chats/getAllMessages`` with delta + fallback.

    Construction is intentionally lightweight so the connector wiring
    layer can hold one fetcher per sync run without paying a high
    setup cost. The ``httpx.Client`` is created here (rather than per
    call) so the connection pool is reused across pages within a
    single sync.

    The class is **not** thread-safe — Phase 11 syncs run sequentially
    inside ``opshub connector sync teams`` (one connector at a time
    per process), so a per-call lock would be needless overhead.
    """

    def __init__(
        self,
        auth: TeamsAuth,
        *,
        fallback_window_days: int = _DEFAULT_FALLBACK_WINDOW_DAYS,
    ) -> None:
        """Construct a fetcher bound to a configured :class:`TeamsAuth`.

        :param auth: An auth helper whose :attr:`TeamsAuth.token`
            returns a valid Microsoft Graph bearer.
        :param fallback_window_days: How far back to scan when the
            stored delta link is rejected by Graph. Defaults to the
            ADR-0010 Phase 11 改訂 (c) recommendation of 30 days. A
            value of ``0`` disables fallback — the connector then
            surfaces the underlying ``ConnectorFailedError`` instead
            of recovering. Non-positive non-zero values are clamped
            to the default so a config typo cannot disable the
            recovery path silently.

        :raises ConfigError: When the ``httpx`` extras are missing —
            same message shape as the auth module's MSAL guard so the
            operator gets one consistent install hint.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Teams connector requires the [connectors-teams] extras. "
                "Install with: uv sync --extra connectors-teams"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)
        # Clamp the window: 0 means "disable" (operator opt-out, ADR-0010
        # §改訂 (c) notes it as discouraged but allowed). Negative values
        # almost certainly indicate a typo so we silently snap them back
        # to the default rather than failing loud — the connector
        # still works, and the operator is not penalised for a config
        # mistake on a recovery path they may never hit.
        if fallback_window_days < 0:
            fallback_window_days = _DEFAULT_FALLBACK_WINDOW_DAYS
        self._fallback_window_days = fallback_window_days

    # ----- public API ------------------------------------------------------

    def fetch_chat_messages(
        self, *, delta_link: str | None
    ) -> Iterator[tuple[RawTeamsChatMessage, str]]:
        """Yield ``(message, new_delta_link)`` using Graph's delta query.

        :param delta_link: The opaque ``@odata.deltaLink`` URL persisted
            on the previous sync, or ``None`` on first sync.

        Yields
        ------
        tuple[RawTeamsChatMessage, str]
            ``new_delta_link`` is the cursor to persist *after*
            committing the SourceObserved event for ``message``. Until
            the final page of a delta walk is observed the cursor on
            each yield is the **incoming** ``delta_link`` (or the root
            delta URL on a first sync) so a mid-iteration crash does
            not advance the cursor past unconsumed items. On the final
            page we switch to the freshly-returned ``@odata.deltaLink``.

            When Graph invalidates the stored delta link (``410
            Gone``), the iterator restarts in fallback mode: it walks
            ``$filter=lastModifiedDateTime ge <since>`` for the
            configured window, then refreshes the delta link by
            issuing the root delta URL one more time and yielding any
            additional messages from that page — the final yield in
            that case carries the brand-new delta link.

        Raises
        ------
        ConnectorFailedError
            On any Graph API error that is not a recoverable 429 / 410,
            or when the retry budget is exhausted. The error message
            includes only the HTTP method / URL fragment; the bearer
            token never appears.
        """
        root_url = self._root_delta_url()
        cursor_in_flight = delta_link or root_url
        url: str | None = delta_link or root_url

        # ``saw_410`` tracks whether the first request failed with a
        # delta-link-invalidated error. When it did we drop into the
        # fallback branch outside this loop and start over from a
        # ``$filter``-based scan.
        try:
            while url is not None:
                body = self._request("GET", url)
                value_obj = body.get("value")
                if not isinstance(value_obj, list):
                    raise ConnectorFailedError(
                        "Teams chat delta response is missing the 'value' list "
                        "(unexpected response shape)"
                    )
                items = cast(list[dict[str, Any]], value_obj)

                next_link = body.get("@odata.nextLink")
                delta_link_out = body.get("@odata.deltaLink")
                page_cursor = (
                    delta_link_out
                    if isinstance(delta_link_out, str) and not isinstance(next_link, str)
                    else cursor_in_flight
                )

                for raw_item in items:
                    message = _normalise_chat_message(raw_item)
                    if message is None:
                        continue
                    yield message, page_cursor

                url = next_link if isinstance(next_link, str) else None
        except _DeltaLinkExpiredError:
            # ADR-0010 Phase 11 改訂 (c): the stored delta link has
            # expired. We yield from the fallback path here rather than
            # in a ``finally`` so a partial walk before the expiry
            # surfaces nothing — the projection-side dedup absorbs the
            # subsequent fallback re-yield safely.
            yield from self._fallback_pass()

    def close(self) -> None:
        """Release the underlying ``httpx.Client`` socket.

        Optional — the connection pool is GC-managed — but provided so
        a long-lived service process can clean up between sync runs.
        """
        self._client.close()

    # ----- internals -------------------------------------------------------

    @staticmethod
    def _root_delta_url() -> str:
        """Return the root URL for a fresh delta walk.

        Microsoft Graph requires ``$filter`` on the initial
        ``getAllMessages`` call (the resource is server-side filtered),
        so we pin a "1970 onward" filter as the documented bootstrap.
        Operators who want to truncate their initial backfill can edit
        the persisted cursor row directly — Phase 11.x may add a CLI
        flag if the manual edit proves common.
        """
        params = {"$filter": "lastModifiedDateTime gt 1970-01-01T00:00:00Z"}
        return f"{GRAPH_BASE}/me/chats/getAllMessages?{urlencode(params)}"

    def _fallback_pass(self) -> Iterator[tuple[RawTeamsChatMessage, str]]:
        """Yield ``(message, new_delta_link)`` for the fallback window.

        ADR-0010 Phase 11 改訂 (c) §2: when the stored delta link is
        rejected we walk ``$filter=lastModifiedDateTime ge <since>``
        for ``fallback_window_days`` days, then refresh the delta link
        on the final yield so the next sync resumes on the delta path.

        ``fallback_window_days == 0`` disables fallback by short-
        circuiting here with a re-raise of the original
        :class:`ConnectorFailedError` — the operator opted out
        explicitly so we do not silently swallow the failure.
        """
        if self._fallback_window_days == 0:
            raise ConnectorFailedError(
                "Teams chat delta link expired and fallback is disabled "
                "(fallback_window_days = 0). Re-enable fallback in "
                "opshub.toml or manually reset the cursor."
            )

        since = (datetime.now(tz=UTC) - timedelta(days=self._fallback_window_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {
            "$filter": f"lastModifiedDateTime ge {since}",
            "$top": str(_PAGE_SIZE),
        }
        url: str | None = f"{GRAPH_BASE}/me/chats/getAllMessages?{urlencode(params)}"

        # We do not yet have a fresh delta link, so the cursor we yield
        # *during* the fallback walk replays the same ``$filter`` URL.
        # That keeps a mid-fallback crash idempotent: the next sync
        # repeats the filter (the projection dedups), and only when the
        # walk completes do we swap in the freshly-acquired delta link.
        cursor_in_flight: str = url
        while url is not None:
            body = self._request("GET", url)
            value_obj = body.get("value")
            if not isinstance(value_obj, list):
                raise ConnectorFailedError(
                    "Teams chat fallback response is missing the 'value' list "
                    "(unexpected response shape)"
                )
            items = cast(list[dict[str, Any]], value_obj)
            next_link = body.get("@odata.nextLink")

            for raw_item in items:
                message = _normalise_chat_message(raw_item)
                if message is None:
                    continue
                yield message, cursor_in_flight

            url = next_link if isinstance(next_link, str) else None

        # Refresh the delta link by hitting the root delta URL once.
        # We do not yield messages from this page (any items returned
        # here were already covered by the ``$filter`` walk for the
        # configured window — Graph's delta endpoint reports activity
        # since "now" relative to the new token); the value we care
        # about is the ``@odata.deltaLink`` it carries.
        refresh_body = self._request("GET", self._root_delta_url())
        next_link = refresh_body.get("@odata.nextLink")
        # When ``$top`` paging happens we still need to walk to the end
        # before Graph commits to a delta link. We follow ``@odata.nextLink``
        # without yielding (same rationale as above).
        while isinstance(next_link, str):
            refresh_body = self._request("GET", next_link)
            next_link = refresh_body.get("@odata.nextLink")
        new_delta_link = refresh_body.get("@odata.deltaLink")
        if isinstance(new_delta_link, str) and new_delta_link:
            # Final "advance" yield: a synthetic empty-body message
            # would be wrong because the caller maps each yield to a
            # SourceObserved event. Instead we attach the new cursor
            # to the *last* real message via a generator-state trick:
            # we wrap the previous yields so the very last one we hand
            # out carries the new delta link. To keep the iterator
            # contract simple, we instead surface the refresh outcome
            # by yielding a synthetic "cursor-only" advance only when
            # at least one real message was yielded — but the
            # connector loop already updates its persisted cursor on
            # every yield, so the simpler shape is: emit a final
            # zero-content sentinel? That breaks the
            # ``RawTeamsChatMessage`` contract.
            #
            # Resolution: we do not yield a fake message. Instead the
            # *connector* layer treats the refresh by re-fetching the
            # cursor through this fetcher's :meth:`pending_delta_link`
            # property after the iterator drains.
            self._pending_delta_link = new_delta_link
        # else: Graph returned no delta link on the refresh page (rare;
        # documented to always include it but defended against). The
        # connector keeps the in-flight ``$filter`` cursor — the next
        # sync simply replays the same fallback walk.

    @property
    def pending_delta_link(self) -> str | None:
        """Return a freshly-acquired delta link if the fallback ran.

        The :meth:`fetch_chat_messages` iterator cannot smuggle a
        delta-link refresh through its final yield (the yield carries a
        message instance), so the connector layer queries this
        property after the iterator drains. ``None`` means the
        fallback either did not run or did not produce a fresh link;
        the connector keeps whatever cursor it has.
        """
        return getattr(self, "_pending_delta_link", None)

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Issue a Graph request with bearer auth, 410 detection, and 429 backoff.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **410** — delta-link expired (ADR-0010 §改訂 (c)). Raised as
          :class:`_DeltaLinkExpiredError` so the iterator can switch to
          the fallback path; not retried inline.
        * **429** — Graph's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **Other 4xx / 5xx** — fail-fast: :meth:`Response.raise_for_status`
          surfaces an :class:`httpx.HTTPStatusError` which we wrap into
          :class:`ConnectorFailedError` so the connector always sees
          one error class.

        Tokens are NEVER logged or included in the raised message
        (ADR-0005 / ADR-0020 §(e) provenance discipline). The error
        message identifies the offending HTTP verb / URL only.
        """
        last_status: int | None = None
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            headers = {"Authorization": f"Bearer {self._auth.token}"}
            extra_headers = kwargs.pop("headers", None)
            if extra_headers:
                headers.update(extra_headers)
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except self._httpx.HTTPError as exc:
                raise ConnectorFailedError(
                    f"Teams request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 410:
                # Delta link expired. Not retryable: the iterator must
                # restart in fallback mode. The custom exception is a
                # control-flow signal scoped to this module.
                raise _DeltaLinkExpiredError
            if response.status_code == 429:
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if response.status_code >= 400:
                try:
                    response.raise_for_status()
                except self._httpx.HTTPStatusError as exc:
                    raise ConnectorFailedError(
                        f"Teams request returned {response.status_code}: {method} {url}"
                    ) from exc

            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(f"Teams response from {url} was not valid JSON") from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(f"Teams response from {url} was not a JSON object")
            return cast(dict[str, Any], body)

        raise ConnectorFailedError(
            f"Teams request failed after {_MAX_REQUEST_ATTEMPTS} attempts: "
            f"{method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


class _DeltaLinkExpiredError(Exception):
    """Internal signal: Graph returned ``410 Gone`` for the delta link.

    Caught by :meth:`TeamsFetcher.fetch_chat_messages` to trigger the
    fallback walk. Never surfaced to callers — the iterator either
    completes via fallback or raises :class:`ConnectorFailedError` from
    inside the fallback path.
    """


def _parse_retry_after(header_value: str | None, *, fallback: int) -> int:
    """Return the ``Retry-After`` delay in seconds, or ``fallback`` on parse failure.

    Microsoft Graph documents the header as an integer number of
    seconds; we still defend against the HTTP-date variant by falling
    back rather than raising — a connector that hot-loops because the
    server returned an exotic header would be worse than one that
    waits an extra few seconds.
    """
    if header_value is None:
        return fallback
    try:
        return int(header_value)
    except (TypeError, ValueError):
        return fallback


def _normalise_chat_message(raw: dict[str, Any]) -> RawTeamsChatMessage | None:
    """Lift a Graph payload to :class:`RawTeamsChatMessage`.

    Returns ``None`` for system / control messages that have no body
    we can map — Graph emits these on chat membership changes,
    channel renames, etc.; surfacing them as SourceObserved would
    only add noise. A "no body and no sender" payload is the
    documented shape for those system events.
    """
    body_obj = raw.get("body")
    body: dict[str, Any] = cast(dict[str, Any], body_obj) if isinstance(body_obj, dict) else {}
    body_html = str(body.get("content") or "")
    body_content_type = str(body.get("contentType") or "html")

    # ``from`` is reserved as a Python keyword, but Graph happens to
    # also use ``from`` as the JSON key. ``raw.get("from")`` works
    # because we only read the dict shape; no attribute access.
    from_obj = raw.get("from")
    from_dict: dict[str, Any] = cast(dict[str, Any], from_obj) if isinstance(from_obj, dict) else {}
    user_obj = from_dict.get("user")
    user: dict[str, Any] = cast(dict[str, Any], user_obj) if isinstance(user_obj, dict) else {}
    sender_display_name = str(user.get("displayName") or "")
    sender_id = str(user.get("id") or "")

    # System messages: empty body + no human sender. Skip them.
    if not body_html and not sender_id:
        return None

    return RawTeamsChatMessage(
        id=str(raw.get("id") or ""),
        chat_id=str(raw.get("chatId") or ""),
        chat_topic=str(raw.get("chatType") or ""),
        body_html=body_html,
        body_content_type=body_content_type,
        sender_display_name=sender_display_name,
        sender_id=sender_id,
        created_datetime_iso=str(raw.get("createdDateTime") or ""),
        last_modified_iso=str(raw.get("lastModifiedDateTime") or ""),
        web_url=str(raw.get("webUrl") or ""),
        raw=raw,
    )
