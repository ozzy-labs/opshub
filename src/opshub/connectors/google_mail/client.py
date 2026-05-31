"""Gmail API v1 client + raw message shape (Phase 14 G3, ADR-0010 §Phase 14 改訂).

A thin ``httpx``-backed wrapper over Google Gmail's v1 REST endpoints.
The wrapper covers exactly the Phase 14 MVP needs:

* ``users.getProfile`` — fresh ``historyId`` bootstrap (cursor
  initialisation + TTL-expiry fallback per ADR-0010 §Phase 14 改訂 (j)).
* ``users.messages.list`` — paginated message-id walk for the
  first-sync backfill and the TTL fallback full-pass. Supports an
  optional ``q`` filter so the fallback can scope to
  ``after:YYYY/MM/DD`` (the Gmail-flavoured equivalent of Drive's
  ``modifiedTime >= '...'`` query).
* ``users.messages.get(format='full')`` — fetches the full message
  payload (headers + body parts) the mapper needs to assemble
  :class:`SourceObserved`.
* ``users.history.list`` — delta walk over message + label additions
  since a stored ``startHistoryId``; the connector consumes the
  iterator and persists cursors as items land.

SDK choice (OQ14 — confirmed at G3 start per Phase 14 plan §8)
--------------------------------------------------------------

``httpx`` + manual OAuth + manual JSON, not ``google-api-python-client``.
Same rationale as the Phase 13 Google Workspace client; pinning the
choice here so a future reader can re-confirm without cross-module
archaeology:

1. **Cold-start budget (M6).** ``google-api-python-client`` does
   service-discovery on import (a ~5 MB JSON download cached on first
   call, plus ``httplib2`` + ``oauth2client`` + ``protobuf`` deps that
   are heavy by themselves). Maintaining the ADR-0001 ≤ 300 ms
   ``opshub --help`` budget under it would require gymnastics
   ``httpx`` simply does not need.
2. **Sibling connectors.** Drive / MS365 / Box / Teams all use
   ``httpx``; the Gmail surface (single OAuth principal, REST + JSON)
   maps cleanly onto the same idioms.
3. **Auth surface.** Shared with Drive via
   :mod:`opshub.connectors.google_auth.auth` (Phase 14 G2 #294); no
   per-connector auth re-implementation.
4. **Test ergonomics.** ``httpx.MockTransport`` is the project's
   standard mock seam — same fixture pattern as the Drive client
   tests.

Retry / rate-limit
------------------

Gmail's documented throttling envelope is HTTP 429 ``Too Many
Requests`` plus HTTP 403 with ``reason`` ``rateLimitExceeded`` /
``userRateLimitExceeded`` / ``quotaExceeded``. We honour
``Retry-After`` directly when present and otherwise back off
exponentially (1 s / 2 s / 4 s) for up to three attempts per request,
matching Phase 7 MS365 + Phase 11 Teams + Phase 13 Google Workspace
precedent. 5xx server errors get the same backoff (Google documents
them as transient). Persistent failure escalates to
:class:`~opshub.core.errors.ConnectorFailedError`.

Cursor invalidation
-------------------

Gmail returns HTTP 404 with ``error.errors[].reason == 'historyNotFound'``
(or sometimes the bare 404 without a body reason) when the stored
``startHistoryId`` has expired past the documented ~7-day TTL. The
client surfaces these to the connector via a sentinel
:class:`HistoryIdExpiredError` so the connector layer can bootstrap a
fresh ``historyId`` via :meth:`get_profile_history_id` (after a
``messages.list`` backfill) and resume — mirrors the Phase 13 Google
Workspace ``PageTokenExpiredError`` control flow.

Delegated mailbox guard (OQ12 — Phase 14 plan §8)
-------------------------------------------------

The MVP targets the **operator's personal mailbox only** (ADR-0018
operator-1 scaling root). All endpoints are scoped under
``users/me``; the client deliberately does not expose a
``user_id`` / ``delegate`` parameter so a future regression that
accidentally widens the request surface to a shared / delegated
mailbox (a Workspace organisation feature) surfaces in tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth


__all__ = [
    "GMAIL_API_BASE",
    "GmailClient",
    "HistoryIdExpiredError",
    "RawGmailMessage",
    "normalise_message",
]


#: Gmail API v1 base URL. The v1 surface is the GA endpoint; there is
#: no v2 today, but the constant keeps the version pin explicit so a
#: future ``v2`` migration is a one-line change.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

#: HTTP timeout for Gmail calls. 30 s mirrors :class:`DriveClient` /
#: :class:`MS365Fetcher` / :class:`TeamsFetcher` and accommodates
#: Gmail's tail-latency on the larger ``messages.get`` (full HTML body)
#: payloads without wedging.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`GmailClient._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts
#: matches the rest of the connector family's retry budget.
_MAX_REQUEST_ATTEMPTS = 3

#: ``maxResults`` for ``messages.list`` / ``history.list`` pagination.
#: 100 mirrors the Drive client's sweet spot — small enough that 429s
#: do not cost much re-work, large enough that paging traffic stays
#: low. Gmail's documented max is 500 but values that high tend to
#: trip the throttling layer (same pattern Drive + Graph exhibit at
#: their respective max-page sizes).
_PAGE_SIZE = 100

#: ``historyTypes`` filter for ``users.history.list``. We care about
#: new messages and label additions; ``messageDeleted`` / ``labelRemoved``
#: are skipped because ADR-0020 retain-everything means the projection
#: keeps the original SourceObserved row regardless of post-hoc state
#: changes (the deletion would surface as an empty payload anyway).
#: Pinning the filter here so a future regression that quietly widens
#: it surfaces in tests.
_HISTORY_TYPES = ("messageAdded", "labelAdded")


@dataclass(frozen=True, slots=True)
class RawGmailMessage:
    """Normalised view of a single Gmail message payload.

    Lifted from the ``users.messages.get(format='full')`` response by
    :func:`_normalise_message`. The mapper consumes this shape rather
    than the raw JSON so a future Gmail API field rename only touches
    the normaliser, not the mapper or the tests.

    Attributes
    ----------
    message_id:
        The Gmail message id (Google's stable opaque identifier).
        Pairs with the connector name to form the natural key the
        projection upserts on.
    thread_id:
        The Gmail thread id. Persisted on
        :class:`RawGmailMessage` so the mapper can include it in the
        body / future projection rows; replied-to link materialisation
        is Phase 15+ defer (Phase 14 plan §Phase 15+ outlook,
        ADR-0010 §Phase 14 改訂 (k) §不変条件 3).
    history_id:
        The Gmail historyId associated with this message. The connector
        uses the **maximum** historyId seen across a sync run as the
        next cursor when the steady-state ``history.list`` path
        terminates without yielding a fresh value (the first-sync
        bootstrap path).
    snippet:
        Gmail's pre-computed short preview (Google caps it around 200
        chars). Used as the ``SourceObserved.summary`` body when no
        ``Subject`` line is present (defensive fallback only — every
        live message should carry a subject header).
    subject:
        Lifted from ``payload.headers[name=='Subject'].value``. Empty
        string when the message has no subject header (rare but legal
        per RFC 5322).
    from_header:
        Lifted from ``payload.headers[name=='From'].value``. Empty
        string when absent (e.g. internal system messages).
    internal_date_ms:
        Gmail's ``internalDate`` field (string of UTC milliseconds
        since epoch). Used as ``occurred_at`` for ``SourceObserved``;
        empty string falls back to :func:`opshub.core.time.now_utc` in
        the mapper.
    label_ids:
        The list of label ids attached to this message (``INBOX`` /
        ``IMPORTANT`` / user-defined labels). The mapper prepends a
        ``[Labels: ...]`` line to the body so the secretary skill can
        condition on the label set without an extra structured field
        (Phase 14 plan §1 OQ7 — Outlook 流 body 埋め込みのみ).
    body_text:
        Decoded UTF-8 text/plain body, when the message has one. Empty
        string when not present.
    body_html:
        Decoded UTF-8 text/html body, when the message has one and
        text/plain was absent. Empty string when not present or when
        a text/plain alternative was already picked. The mapper
        prefers text/plain; this field carries the fallback.
    raw:
        Verbatim ``messages.get`` payload, kept for forensic debugging
        (mapper fixtures, future backfill). The mapper does not
        persist this.
    """

    message_id: str
    thread_id: str
    history_id: str
    snippet: str
    subject: str
    from_header: str
    internal_date_ms: str
    label_ids: tuple[str, ...]
    body_text: str
    body_html: str
    raw: dict[str, Any]


class HistoryIdExpiredError(Exception):
    """Internal signal: Gmail returned 404 for the stored historyId.

    Caught by :meth:`GmailClient.fetch_history` callers so the
    connector layer can bootstrap a fresh ``historyId`` via
    :meth:`GmailClient.get_profile_history_id` (after a backfill via
    :meth:`GmailClient.list_messages`) and resume. Never surfaced
    to upstream callers — the connector either completes via fallback
    or raises :class:`ConnectorFailedError` from inside the fallback
    path. Mirrors
    :class:`opshub.connectors.google_workspace.client.PageTokenExpiredError`.
    """


class GmailClient:
    """Gmail API v1 client (``messages.list`` + ``messages.get`` + ``history.list``).

    Construction is intentionally lightweight so the connector wiring
    layer can hold one client per sync run without paying a high
    setup cost. The ``httpx.Client`` is created here (rather than per
    call) so the connection pool is reused across pages.

    The class is **not** thread-safe — Phase 14 syncs run sequentially
    inside ``opshub connector sync google_mail`` (one connector at a
    time per process), so a per-call lock would be needless overhead.
    """

    def __init__(self, auth: GoogleWorkspaceAuth) -> None:
        """Construct a client bound to a configured shared :class:`GoogleWorkspaceAuth`.

        :param auth: An auth helper whose
            :meth:`GoogleWorkspaceAuth.get_access_token` returns a
            valid bearer covering the ``gmail.readonly`` scope (the
            Phase 14 G2 fixed-scope list grants this alongside
            ``drive.readonly`` + ``calendar.readonly`` so any
            properly-consented operator already has it).

        :raises ConfigError: When the ``[connectors-google-workspace]``
            extras are missing — same message shape as the auth
            module's ``httpx`` guard so the operator gets one
            consistent install hint (Phase 14 plan §Alternatives §9
            rejected per-vendor extras splits).
        """
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Google Mail connector requires the "
                "[connectors-google-workspace] extras "
                "(shared with Drive / Calendar per Phase 14 plan §Alternatives §9). "
                "Install with: uv sync --extra connectors-google-workspace"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)

    # ----- public API ------------------------------------------------------

    def get_profile_history_id(self) -> str:
        """Return the current ``historyId`` from ``users.getProfile``.

        Gmail ``users.getProfile`` is the documented bootstrap endpoint
        for the History API: the returned ``historyId`` reflects the
        current state of the inbox and can be used as ``startHistoryId``
        on the next ``users.history.list`` call.

        Raises :class:`ConnectorFailedError` on any non-2xx or
        transport failure. Tokens never appear in the raised message.
        """
        body = self._request_json("GET", f"{GMAIL_API_BASE}/users/me/profile")
        history_id_obj = body.get("historyId")
        if not isinstance(history_id_obj, str) or not history_id_obj:
            raise ConnectorFailedError(
                "Google Mail getProfile returned no historyId (unexpected response shape)"
            )
        return history_id_obj

    def list_messages(
        self,
        *,
        query: str | None = None,
        page_size: int = _PAGE_SIZE,
    ) -> Iterator[str]:
        """Yield message ids for ``users.messages.list``.

        Walks Gmail's ``messages.list`` endpoint forward across
        paginated responses; each page yields its message ids in
        Gmail's natural (descending ``internalDate``) order. The
        caller is responsible for following each id up with
        :meth:`get_message` to fetch the full payload — Gmail's
        ``messages.list`` returns only ``{"id", "threadId"}`` per
        message by design.

        Parameters
        ----------
        query:
            Optional Gmail search query (e.g.
            ``"after:2026/05/01"`` for the TTL fallback). Forwarded
            verbatim into the ``q`` parameter; Gmail's documented
            search-syntax page covers the available operators.
        page_size:
            ``maxResults`` for the underlying ``messages.list`` call.
            Defaults to :data:`_PAGE_SIZE` (100).

        Yields
        ------
        str
            Gmail message id per matched message.

        Raises
        ------
        ConnectorFailedError
            On any non-recoverable transport / API failure or when the
            retry budget is exhausted.
        """
        url = f"{GMAIL_API_BASE}/users/me/messages"
        params: dict[str, str] = {"maxResults": str(page_size)}
        if query:
            params["q"] = query

        while True:
            body = self._request_json("GET", url, params=params)
            messages_obj = body.get("messages")
            if messages_obj is None:
                # Gmail omits the ``messages`` key entirely when the
                # response carries zero results (e.g. an empty inbox
                # or a ``q=`` filter that matched nothing). Treat this
                # as "no messages" rather than an error.
                return
            if not isinstance(messages_obj, list):
                raise ConnectorFailedError(
                    "Google Mail messages.list response 'messages' field is "
                    "not a list (unexpected response shape)"
                )
            messages = cast(list[dict[str, Any]], messages_obj)
            for raw_message in messages:
                mid = raw_message.get("id")
                if isinstance(mid, str) and mid:
                    yield mid

            next_page_token = body.get("nextPageToken")
            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
            else:
                return

    def get_message(self, *, message_id: str) -> RawGmailMessage:
        """Fetch + normalise a single message via ``messages.get(format='full')``.

        Returns the :class:`RawGmailMessage` shape the mapper consumes.
        ``format='full'`` instructs Gmail to include the headers + the
        decoded body parts in the response — the alternative
        ``metadata`` format would omit the body which defeats Phase 10
        (ADR-0020) full-body retention. ``format='raw'`` would return
        an RFC 2822 blob the mapper would have to re-parse; the
        ``full`` shape Google already does that decoding for us.

        Raises
        ------
        ConnectorFailedError
            On non-2xx / transport failure. Tokens never appear in
            the raised message.
        """
        if not message_id:
            raise ConnectorFailedError(
                "Google Mail messages.get was called with an empty message_id"
            )
        url = f"{GMAIL_API_BASE}/users/me/messages/{message_id}"
        body = self._request_json("GET", url, params={"format": "full"})
        return _normalise_message(body)

    def fetch_history(
        self,
        *,
        start_history_id: str,
    ) -> Iterator[tuple[str, str]]:
        """Yield ``(message_id, latest_history_id)`` for every change since ``start_history_id``.

        Walks Gmail's ``users.history.list`` endpoint forward across
        paginated responses. Each yielded ``message_id`` is a candidate
        the caller should follow up with :meth:`get_message`; the
        ``latest_history_id`` is the in-flight cursor value that
        advances to the page's freshly-returned ``historyId`` once the
        final page is reached, mirroring the
        :class:`DriveClient.fetch_changes` cursor handoff.

        Parameters
        ----------
        start_history_id:
            The stored cursor value, replayed verbatim into Gmail's
            ``startHistoryId`` parameter.

        Yields
        ------
        tuple[str, str]
            ``(message_id, in_flight_cursor)`` per change record.
            Deduplication across pages is the caller's responsibility
            (a single message may appear in multiple history records
            when ``messageAdded`` and ``labelAdded`` happen in the
            same window).

        Raises
        ------
        HistoryIdExpiredError
            Gmail rejected ``start_history_id`` as expired (HTTP 404
            with ``error.errors[].reason == 'historyNotFound'``, or
            sometimes the bare 404 without a body reason). The
            connector layer traps this, bootstraps a fresh
            ``historyId`` via :meth:`get_profile_history_id`, and
            resumes.
        ConnectorFailedError
            On any other transport / API failure, or when the retry
            budget is exhausted. Tokens never appear in the error
            message.
        """
        url = f"{GMAIL_API_BASE}/users/me/history"
        params: dict[str, str] = {
            "startHistoryId": start_history_id,
            "maxResults": str(_PAGE_SIZE),
        }
        for history_type in _HISTORY_TYPES:
            params.setdefault("historyTypes", history_type)
        # Gmail's ``historyTypes`` parameter accepts a list-like form;
        # we pass it as a single repeated parameter via httpx's
        # ``params`` dict by passing a tuple. Build the params with
        # the tuple form here.
        request_params: list[tuple[str, str]] = [
            (key, value) for key, value in params.items() if key != "historyTypes"
        ]
        for history_type in _HISTORY_TYPES:
            request_params.append(("historyTypes", history_type))

        cursor_in_flight = start_history_id

        while True:
            body = self._request_json(
                "GET",
                url,
                params=request_params,
            )
            history_obj = body.get("history")
            # ``history`` is omitted entirely when no changes happened
            # since the stored cursor. The connector still needs to
            # observe the response's ``historyId`` so the next sync
            # picks up the latest watermark.
            response_history_id = body.get("historyId")
            page_cursor = (
                response_history_id
                if isinstance(response_history_id, str) and response_history_id
                else cursor_in_flight
            )
            next_page_token = body.get("nextPageToken")
            # On the final page (no nextPageToken) we advance the
            # cursor to the response's freshly-returned historyId so
            # the caller can persist it. Until then we keep handing
            # out the in-flight value so a mid-iteration crash does
            # not advance past unconsumed items.
            advanced_cursor = (
                page_cursor
                if not (isinstance(next_page_token, str) and next_page_token)
                else cursor_in_flight
            )

            if isinstance(history_obj, list):
                for raw_record in cast(list[dict[str, Any]], history_obj):
                    for message_id in _extract_message_ids_from_history_record(raw_record):
                        yield message_id, advanced_cursor

            if isinstance(next_page_token, str) and next_page_token:
                # Replace the pageToken slot on the params list.
                request_params = [
                    (key, value) for key, value in request_params if key != "pageToken"
                ]
                request_params.append(("pageToken", next_page_token))
                cursor_in_flight = page_cursor
            else:
                return

    def close(self) -> None:
        """Release the underlying ``httpx.Client`` socket.

        Optional — the connection pool is GC-managed — but provided so
        a long-lived service process can clean up between sync runs.
        """
        self._client.close()

    # ----- internals -------------------------------------------------------

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Issue a Gmail request with bearer auth + 429 backoff + historyId-expired detection.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **404 with ``historyNotFound`` reason** — historyId expired
          (ADR-0010 §Phase 14 改訂 (j)). Raised as
          :class:`HistoryIdExpiredError` so the iterator can switch to
          the fallback path; not retried inline. Bare 404 without a
          recognisable reason on the ``/users/me/history`` endpoint is
          treated the same way (Gmail's documented shape).
        * **429** — Gmail's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **403 with ``rateLimitExceeded`` / ``userRateLimitExceeded`` /
          ``quotaExceeded``** — Gmail's documented user-quota signal,
          same handling as 429.
        * **5xx** — Gmail documents these as transient; same backoff.
        * **Other 4xx** — fail-fast: wrap into
          :class:`ConnectorFailedError` so the CLI driver always sees
          one error class.

        Tokens are NEVER logged or included in the raised message
        (ADR-0005 / ADR-0020 §(e) provenance discipline). The error
        message identifies the offending HTTP verb / URL only.
        """
        last_status: int | None = None
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            headers = {
                "Authorization": f"Bearer {self._auth.get_access_token()}",
                "Accept": "application/json",
                "User-Agent": "opshub-connector/0.1",
            }
            try:
                response = self._client.request(method, url, headers=headers, params=params)
            except self._httpx.HTTPError as exc:
                raise ConnectorFailedError(
                    f"Google Mail request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 404 and _is_history_not_found(response, url):
                # historyId expired. Not retryable here: the connector
                # must restart in fallback mode after bootstrapping a
                # fresh historyId via :meth:`get_profile_history_id`.
                raise HistoryIdExpiredError
            if response.status_code == 429 or (
                response.status_code == 403 and _is_rate_limit_error(response)
            ):
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                # 5xx: Gmail documents these as transient; back off.
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise ConnectorFailedError(
                    f"Google Mail request returned {response.status_code}: {method} {url}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(
                    f"Google Mail response from {url} was not valid JSON"
                ) from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(f"Google Mail response from {url} was not a JSON object")
            return cast(dict[str, Any], body)

        raise ConnectorFailedError(
            f"Google Mail request failed after {_MAX_REQUEST_ATTEMPTS} "
            f"attempts: {method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


def _is_history_not_found(response: Any, url: str) -> bool:
    """True iff a 404 on the history endpoint indicates historyId expiry.

    Gmail returns ``error.errors[].reason == 'historyNotFound'`` when
    the stored historyId has been pruned past the documented ~7-day
    TTL. Some Google data centres return a bare 404 without a body
    reason; for the ``/users/me/history`` endpoint we treat any 404 as
    expiry because the only other reason a history lookup 404s is a
    typo in the path (which would also be caught and surfaced as a
    fresh bootstrap rather than a hard fail).
    """
    if "/users/me/history" in url:
        # Bare 404 on the history endpoint is treated as expiry — the
        # only other valid 404 here would mean the path itself is
        # wrong, which the fallback would surface immediately on the
        # subsequent ``getProfile`` call.
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    body_dict = cast(dict[str, Any], body)
    error_obj = body_dict.get("error")
    if not isinstance(error_obj, dict):
        return False
    error_dict = cast(dict[str, Any], error_obj)
    errors_obj = error_dict.get("errors")
    if not isinstance(errors_obj, list):
        return False
    errors_list = cast(list[Any], errors_obj)  # type: ignore[redundant-cast]
    for entry in errors_list:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(dict[str, Any], entry)
        reason = entry_dict.get("reason")
        if isinstance(reason, str) and reason == "historyNotFound":
            return True
    return False


def _is_rate_limit_error(response: Any) -> bool:
    """True iff a Gmail 403 body carries a rate-limit reason code.

    Same shape :class:`DriveClient` uses for Drive 403 detection;
    duplicating the helper here keeps the Gmail client free of
    cross-connector imports (the Drive helper is a private symbol).
    """
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    body_dict = cast(dict[str, Any], body)
    error_obj = body_dict.get("error")
    if not isinstance(error_obj, dict):
        return False
    error_dict = cast(dict[str, Any], error_obj)
    errors_obj = error_dict.get("errors")
    if not isinstance(errors_obj, list):
        return False
    errors_list = cast(list[Any], errors_obj)  # type: ignore[redundant-cast]
    for entry in errors_list:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(dict[str, Any], entry)
        reason = entry_dict.get("reason")
        if isinstance(reason, str) and reason in (
            "userRateLimitExceeded",
            "rateLimitExceeded",
            "quotaExceeded",
        ):
            return True
    return False


def _parse_retry_after(header_value: str | None, *, fallback: int) -> int:
    """Return the ``Retry-After`` delay in seconds, or ``fallback`` on parse failure.

    Gmail documents the header as an integer number of seconds; we
    still defend against the HTTP-date variant by falling back rather
    than raising — a connector that hot-loops because the server
    returned an exotic header would be worse than one that waits an
    extra few seconds (same defensive shape Drive uses).
    """
    if header_value is None:
        return fallback
    try:
        return int(header_value)
    except (TypeError, ValueError):
        return fallback


def _extract_message_ids_from_history_record(raw: dict[str, Any]) -> list[str]:
    """Lift the message ids touched by a single history record.

    A history record carries one or more of ``messagesAdded`` /
    ``labelsAdded`` / ``messagesDeleted`` / ``labelsRemoved`` sub-arrays
    (Gmail's documented shape). We only care about message-touching
    events that ADD context (additions / label additions) because
    ADR-0020 retain-everything means deletions / label removals do not
    cause us to re-emit (the original SourceObserved is preserved).

    Returns the de-duplicated list of message ids in the order they
    appear in the record so the caller's downstream observe call order
    is deterministic for fixture-based tests.
    """
    found: list[str] = []
    seen: set[str] = set()

    for key in ("messagesAdded", "labelsAdded"):
        sub_obj = raw.get(key)
        if not isinstance(sub_obj, list):
            continue
        sub_list = cast(list[Any], sub_obj)  # type: ignore[redundant-cast]
        for entry in sub_list:
            if not isinstance(entry, dict):
                continue
            entry_dict = cast(dict[str, Any], entry)
            message_obj = entry_dict.get("message")
            if not isinstance(message_obj, dict):
                continue
            message_dict = cast(dict[str, Any], message_obj)
            mid = message_dict.get("id")
            if isinstance(mid, str) and mid and mid not in seen:
                seen.add(mid)
                found.append(mid)

    return found


def normalise_message(raw: dict[str, Any]) -> RawGmailMessage:
    """Public wrapper around :func:`_normalise_message`.

    Exposed so fixture-driven mapper tests can hand raw Gmail JSON
    (read from ``tests/fixtures/google_mail/*.json``) through the same
    normaliser the production ``get_message`` path uses, without
    reaching into a leading-underscore symbol from outside the module.
    """
    return _normalise_message(raw)


def _normalise_message(raw: dict[str, Any]) -> RawGmailMessage:
    """Lift a Gmail ``messages.get(format='full')`` payload into :class:`RawGmailMessage`.

    Walks the ``payload`` MIME tree depth-first to find the first
    ``text/plain`` part (preferred) and the first ``text/html`` part
    (fallback). Multi-part / nested ``multipart/alternative`` shapes
    are handled by the recursive walk — same algorithm RFC 2046 §5
    documents (search children before falling back to the parent).

    The function tolerates Gmail's nested-object shape: when ``payload``
    is missing (defensive fallback for malformed payloads) we still
    return a :class:`RawGmailMessage` with the ``message_id`` lifted
    from the top-level field so the mapper can decide whether to keep
    or reject it.
    """
    message_id_obj = raw.get("id")
    message_id = message_id_obj if isinstance(message_id_obj, str) else ""

    thread_id_obj = raw.get("threadId")
    thread_id = thread_id_obj if isinstance(thread_id_obj, str) else ""

    history_id_obj = raw.get("historyId")
    history_id = history_id_obj if isinstance(history_id_obj, str) else ""

    snippet_obj = raw.get("snippet")
    snippet = snippet_obj if isinstance(snippet_obj, str) else ""

    internal_date_obj = raw.get("internalDate")
    internal_date_ms = internal_date_obj if isinstance(internal_date_obj, str) else ""

    label_ids_obj = raw.get("labelIds")
    label_ids: tuple[str, ...]
    if isinstance(label_ids_obj, list):
        label_ids_list = cast(list[Any], label_ids_obj)  # type: ignore[redundant-cast]
        label_ids = tuple(
            entry for entry in label_ids_list if isinstance(entry, str) and entry
        )
    else:
        label_ids = ()

    payload_obj = raw.get("payload")
    payload_dict: dict[str, Any] = (
        cast(dict[str, Any], payload_obj) if isinstance(payload_obj, dict) else {}
    )

    subject = _header_value(payload_dict, "Subject")
    from_header = _header_value(payload_dict, "From")

    body_text, body_html = _extract_body_parts(payload_dict)

    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        history_id=history_id,
        snippet=snippet,
        subject=subject,
        from_header=from_header,
        internal_date_ms=internal_date_ms,
        label_ids=label_ids,
        body_text=body_text,
        body_html=body_html,
        raw=raw,
    )


def _header_value(payload: dict[str, Any], name: str) -> str:
    """Return the first header whose ``name`` matches (case-insensitive).

    Gmail's ``payload.headers`` is an array of ``{"name": "...", "value": "..."}``
    pairs; the header names are case-insensitive per RFC 5322. We do
    one linear scan because the typical header set is < 50 entries.
    """
    headers_obj = payload.get("headers")
    if not isinstance(headers_obj, list):
        return ""
    needle = name.lower()
    headers_list = cast(list[Any], headers_obj)  # type: ignore[redundant-cast]
    for entry in headers_list:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(dict[str, Any], entry)
        entry_name = entry_dict.get("name")
        if not isinstance(entry_name, str):
            continue
        if entry_name.lower() == needle:
            value = entry_dict.get("value")
            return value if isinstance(value, str) else ""
    return ""


def _extract_body_parts(payload: dict[str, Any]) -> tuple[str, str]:
    """Walk the MIME tree and return the first ``(text/plain, text/html)`` pair.

    Algorithm:

    1. Recurse depth-first into ``payload.parts``.
    2. The first ``text/plain`` part encountered wins for
       ``body_text``.
    3. The first ``text/html`` part encountered wins for
       ``body_html`` — but only when ``body_text`` is still empty
       when the walk completes (i.e. the mapper's preference for
       text/plain is encoded here so the mapper does not have to
       re-derive it).
    4. Single-part payloads (``payload`` itself carries
       ``mimeType`` + ``body.data``) are treated as if they were a
       one-element ``parts`` list.

    Empty string returned for whichever side has no matching part.
    """
    text_body = ""
    html_body = ""

    def _walk(part: dict[str, Any]) -> None:
        nonlocal text_body, html_body
        mime_type_obj = part.get("mimeType")
        mime_type = mime_type_obj if isinstance(mime_type_obj, str) else ""
        parts_obj = part.get("parts")

        # Leaf nodes carry ``body.data`` (base64url-encoded payload).
        # Multipart nodes carry ``parts`` and their ``body`` is empty.
        if mime_type == "text/plain" and not text_body:
            decoded = _decode_body(part)
            if decoded:
                text_body = decoded
        elif mime_type == "text/html" and not html_body:
            decoded = _decode_body(part)
            if decoded:
                html_body = decoded

        if isinstance(parts_obj, list):
            parts_list = cast(list[Any], parts_obj)  # type: ignore[redundant-cast]
            for child in parts_list:
                if isinstance(child, dict):
                    _walk(cast(dict[str, Any], child))

    _walk(payload)
    return text_body, html_body


def _decode_body(part: dict[str, Any]) -> str:
    """Decode ``part.body.data`` (base64url) to UTF-8.

    Returns ``""`` when the part has no body or when decoding fails
    (defensive — Gmail occasionally returns parts with no payload for
    e.g. attachment placeholders). Attachment bodies are intentionally
    not retained (Phase 14 plan §1 OQ4: 添付 retain なし); the caller
    already filters by mimeType so the only way ``_decode_body`` sees
    an attachment is when the attachment carries an inline text/plain
    or text/html part, which is acceptable.
    """
    body_obj = part.get("body")
    if not isinstance(body_obj, dict):
        return ""
    body_dict = cast(dict[str, Any], body_obj)
    data_obj = body_dict.get("data")
    if not isinstance(data_obj, str) or not data_obj:
        return ""
    # Gmail uses base64url (RFC 4648 §5) which differs from standard
    # base64 in two characters (``-`` instead of ``+``, ``_`` instead
    # of ``/``) and omits the trailing ``=`` padding. ``base64.urlsafe_b64decode``
    # handles both differences once we re-pad the string to a multiple
    # of 4.
    import base64

    padded = data_obj + "=" * (-len(data_obj) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return ""
    try:
        return decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to UTF-8 replacement so a single mis-encoded
        # message does not block the entire sync. The downstream
        # provenance tags already mark the content as untrusted.
        return decoded_bytes.decode("utf-8", errors="replace")
