"""Gmail API v1 client + raw message shape (Phase 14 G3).

A thin ``httpx``-backed wrapper over Gmail's v1 REST endpoints. The
wrapper covers exactly the Phase 14 G3 MVP needs:

* ``users.getProfile`` — current mailbox ``historyId`` bootstrap
  (cursor initialisation + TTL-expiry fallback per ADR-0010 §Phase 14
  改訂 (j)).
* ``users.history.list`` — delta walk over message lifecycle events
  (``messagesAdded`` / ``labelsAdded`` / ...); the connector consumes
  the iterator and persists cursors as items land.
* ``users.messages.list`` — full-pass + initial-sync walker. Used on
  first sync (cursor is ``None``) and during TTL fallback when Gmail
  rejects the stored ``startHistoryId`` with HTTP 404
  ``historyNotFound``.
* ``users.messages.get(format='full')`` — full message payload (Gmail
  returns headers + the parsed MIME tree under ``payload.parts``).
  The mapper consumes this to produce body / labels / threadId /
  summary.

SDK choice (OQ14 — decided at G3 start per plan §8)
---------------------------------------------------

``httpx`` + manual OAuth + manual JSON, not ``google-api-python-client``.
This carries over the Phase 13 Drive client decision verbatim and is
re-confirmed here because Phase 14 plan §1 OQ14 calls for re-measuring
the payload-parsing cost of ``users.messages.get(format='full')``:

1. **Cold-start budget (M6).** ``google-api-python-client`` does
   service-discovery on import (~5 MB JSON download cached on first
   call, plus ``httplib2`` + ``oauth2client`` + ``protobuf`` deps that
   are heavy by themselves). Maintaining the ADR-0001 ≤ 300 ms
   ``opshub --help`` budget under it would require gymnastics
   (sub-module lazy imports, discovery cache pre-warming) that
   ``httpx`` simply does not need.
2. **Sibling connector parity.** Drive (Phase 13) + Calendar (Phase
   14 G4) use ``httpx`` as well. Mixing one ``google-api-python-client``
   connector into three ``httpx`` siblings would split the retry +
   pagination + error-mapping idioms across the Google family.
3. **Payload size envelope.** Gmail's ``users.messages.get(format='full')``
   payload is a recursive MIME tree (``payload.parts[*]``). The mapper
   does a fixed two-pass walk preferring text/plain → text/html;
   this is straightforward to write against the raw JSON. The SDK's
   advantage (typed result classes) would not materially shorten the
   mapper given the structural nature of the walk.

Phase 14 plan §1 OQ14 + ADR-0010 §Phase 14 改訂 (m) reference this
decision.

Retry / rate-limit
------------------

Gmail's documented throttling envelope is HTTP 403 ``rateLimitExceeded``
/ ``userRateLimitExceeded`` / ``quotaExceeded`` or HTTP 429
``Too Many Requests``. We honour ``Retry-After`` directly when present
and otherwise back off exponentially (1 s / 2 s / 4 s) for up to three
attempts per request, matching Phase 7 MS365 + Phase 11 Teams + Phase
13 Drive precedent. 5xx server errors get the same backoff (Google
documents them as transient). Persistent failure escalates to
:class:`~opshub.core.errors.ConnectorFailedError`.

Cursor invalidation
-------------------

Gmail returns HTTP 404 with reason ``historyNotFound`` (or a 404
without an error body on some routes) when the stored ``startHistoryId``
has expired past the documented ~7-day vendor TTL. The client surfaces
these to the connector via a sentinel :class:`HistoryIdExpiredError`
so the connector layer can run the full-pass fallback and bootstrap a
fresh ``historyId`` via ``users.getProfile``. The structural shape
mirrors Drive's :class:`PageTokenExpiredError` and Teams'
``_DeltaLinkExpiredError``.

Operator account (OQ12 — personal mailbox only, MVP)
----------------------------------------------------

Every request targets the implicit ``users/me`` mailbox identifier.
Phase 14 plan §1 OQ12 pins the MVP scope to the operator's own
personal mailbox; shared / delegated mailboxes are deferred to
Phase 15+ (they require additional consent + a different request
shape such as ``users/<email>/...``). The literal ``me`` is hard-coded
inside :data:`_USERS_BASE` so a future regression that tries to
inject a ``?delegate=...`` parameter is structurally impossible — the
mapper symmetry guard at
``tests/unit/connectors/google_mail/test_client.py::test_request_url_has_no_delegate_param``
pins this.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.connectors.google_auth.auth import GoogleAuthError
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    # Phase 14 G2 (#294): auth helper lives in the shared google_auth package.
    from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth


__all__ = [
    "GMAIL_API_BASE",
    "GmailClient",
    "HistoryIdExpiredError",
    "RawGmailMessage",
]


#: Gmail API v1 base URL. The v1 surface is the GA endpoint.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

#: Per Phase 14 plan §1 OQ12 the MVP scope is the operator's own
#: mailbox. Gmail's documented shorthand for "the authenticated user"
#: is ``users/me``; hard-coding it here keeps the connector
#: structurally incapable of accidentally targeting a delegated /
#: shared mailbox (which would require additional consent + Phase
#: 15+ scope expansion).
_USERS_BASE = f"{GMAIL_API_BASE}/users/me"

#: HTTP timeout for Gmail calls. 30 s mirrors :class:`DriveClient` /
#: :class:`MS365Fetcher` / :class:`TeamsFetcher` and accommodates
#: Gmail's tail latency on the larger ``messages.get(format='full')``
#: responses without wedging.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`GmailClient._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts
#: matches Drive / Teams / MS365 precedent.
_MAX_REQUEST_ATTEMPTS = 3

#: ``maxResults`` for ``users.messages.list`` / ``users.history.list``
#: pagination. 100 mirrors Gmail's documented sweet spot — small
#: enough that 429s do not cost much re-work, large enough that paging
#: traffic stays low.
_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class RawGmailMessage:
    """Normalised view of a single Gmail message payload.

    Attributes
    ----------
    message_id:
        Gmail's stable opaque message identifier (``message.id``).
        Pairs with the connector name to form the natural key the
        projection upserts on.
    thread_id:
        Gmail's thread identifier (``message.threadId``). Phase 14
        keeps this as a metadata field only; the
        ``replied_to``-style link projection is deferred to Phase
        15+ (Phase 14 plan §Phase 15+ outlook).
    label_ids:
        Gmail label identifiers stamped on the message (``INBOX`` /
        ``IMPORTANT`` / ``CATEGORY_PERSONAL`` / user labels). The
        mapper prepends these to the body as ``[Labels: ...]`` so
        the secretary skills can see priority cues without an extra
        column.
    history_id:
        The mailbox-level ``historyId`` Gmail reported when this
        message was fetched. Useful for forensic debugging only.
    internal_date_ms:
        Gmail's ``message.internalDate`` (UTC milliseconds since
        epoch, returned as a string by the API). Used as
        ``occurred_at`` for the mapped event.
    from_header:
        ``From:`` header verbatim (e.g. ``"Alice <alice@example.com>"``).
        Used in the summary line.
    subject_header:
        ``Subject:`` header verbatim. Used as ``title``.
    snippet:
        Gmail's pre-computed short preview (~200 chars in practice).
        Used as the recognition summary when present.
    body_text:
        Text/plain body content extracted from the MIME tree (the
        first ``text/plain`` part encountered, walking depth-first).
        Empty string when the message carries no text/plain part.
    body_html:
        Text/html body content extracted from the MIME tree (the
        first ``text/html`` part encountered, walking depth-first).
        Empty string when no text/html part exists. The mapper uses
        this as a fallback when ``body_text`` is empty — Outlook
        symmetric behaviour (Phase 14 plan §1 OQ4).
    raw:
        Verbatim ``users.messages.get`` payload, kept for forensic
        debugging (mapper fixtures, future backfill). The mapper
        does not persist this.
    """

    message_id: str
    thread_id: str
    label_ids: tuple[str, ...]
    history_id: str
    internal_date_ms: str
    from_header: str
    subject_header: str
    snippet: str
    body_text: str
    body_html: str
    raw: dict[str, Any]


class HistoryIdExpiredError(Exception):
    """Internal signal: Gmail returned 404 for the stored history id.

    Caught by :class:`GmailClient` callers so the connector layer can
    run the ADR-0010 §Phase 14 改訂 (j) full-pass fallback and
    bootstrap a fresh ``historyId`` via ``users.getProfile``. Never
    surfaced to upstream callers — the connector either completes via
    fallback or raises :class:`ConnectorFailedError` from inside the
    fallback path. Mirrors :class:`opshub.connectors.google_workspace.client.PageTokenExpiredError`.
    """


class GmailClient:
    """Gmail API v1 client (``users.history.list`` + ``users.messages.*``).

    Construction is intentionally lightweight so the connector wiring
    layer can hold one client per sync run without paying a high setup
    cost. The ``httpx.Client`` is created here (rather than per call)
    so the connection pool is reused across pages.

    The class is **not** thread-safe — Phase 14 syncs run sequentially
    inside ``opshub connector sync google_mail`` (one connector at a
    time per process), so a per-call lock would be needless overhead.
    """

    def __init__(self, auth: GoogleWorkspaceAuth) -> None:
        """Construct a client bound to a configured :class:`GoogleWorkspaceAuth`.

        :param auth: An auth helper whose
            :meth:`GoogleWorkspaceAuth.get_access_token` returns a
            valid Gmail bearer. The client calls that method on every
            request so refresh-token rotation is observed automatically
            (auth persists the rotated value through
            :mod:`opshub.core.secrets`).

        :raises ConfigError: When the ``[connectors-google-workspace]``
            extras are missing — same message shape as the Drive
            client + auth module's ``httpx`` guard so the operator
            gets one consistent install hint.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Gmail connector requires the "
                "[connectors-google-workspace] extras. "
                "Install with: uv sync --extra connectors-google-workspace"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)

    # ----- public API ------------------------------------------------------

    def get_profile_history_id(self) -> str:
        """Return the mailbox's current ``historyId`` via ``users.getProfile``.

        Used by the connector on first sync (cursor bootstrap) and
        after a TTL fallback (cursor refresh). Gmail's
        ``users.getProfile`` response carries the mailbox-level
        ``historyId`` at the top of the JSON object.

        Raises :class:`ConnectorFailedError` on any non-2xx or
        transport failure. Tokens never appear in the raised message.
        """
        body = self._request("GET", f"{_USERS_BASE}/profile", params=None)
        history_id_obj = body.get("historyId")
        if not isinstance(history_id_obj, str) or not history_id_obj:
            raise ConnectorFailedError(
                "Gmail users.getProfile returned no historyId (unexpected response shape)"
            )
        return history_id_obj

    def fetch_history(self, *, start_history_id: str) -> Iterator[tuple[str, str]]:
        """Yield ``(message_id, latest_history_id)`` since ``start_history_id``.

        Walks Gmail's ``users.history.list`` endpoint forward across
        paginated responses. Each page surfaces every
        ``message.id`` referenced by the page's ``history[*]``
        records via ``messagesAdded`` / ``labelsAdded`` / ...
        sub-arrays; the connector layer is responsible for de-duping
        if a single message appears in multiple sub-arrays (Gmail
        reports e.g. ``messagesAdded`` + ``labelsAdded`` for the same
        message id when both events happened during the sync window).

        Cursor semantics (mirrors Drive ``changes.list`` + MS365
        OneDrive delta walks):

        * Until the final page is reached, the yielded ``latest_history_id``
          is the **incoming** ``start_history_id`` so a mid-iteration
          crash does not advance the cursor past unconsumed messages.
        * On the final page (no ``nextPageToken``) the yielded
          ``latest_history_id`` is the page response's ``historyId``
          field — the caller persists it and the next sync resumes
          there.

        Raises
        ------
        HistoryIdExpiredError
            Gmail rejected ``start_history_id`` as expired (HTTP 404
            ``historyNotFound``). The connector layer traps this,
            runs the full-pass fallback, and bootstraps a fresh id.
        ConnectorFailedError
            On any other transport / API failure, or when the retry
            budget is exhausted. Tokens never appear in the error
            message.
        """
        url = f"{_USERS_BASE}/history"
        params: dict[str, str] = {
            "startHistoryId": start_history_id,
            "maxResults": str(_PAGE_SIZE),
        }
        cursor_in_flight = start_history_id

        while True:
            body = self._request("GET", url, params=params)
            history_obj = body.get("history")
            # Gmail omits ``history`` entirely when no new events have
            # happened since ``startHistoryId``; treat it as an empty
            # list rather than a failure (the projection-side dedup
            # absorbs the no-op).
            history = (
                cast(list[dict[str, Any]], history_obj) if isinstance(history_obj, list) else []
            )
            next_page_token = body.get("nextPageToken")
            new_history_id_obj = body.get("historyId")
            # The final page is identified by the absence of
            # ``nextPageToken``. When we are on that page the cursor
            # we hand out advances to the new history id; otherwise
            # callers see the incoming cursor until the walk completes.
            page_cursor = (
                new_history_id_obj
                if isinstance(new_history_id_obj, str)
                and new_history_id_obj
                and not isinstance(next_page_token, str)
                else cursor_in_flight
            )

            seen: set[str] = set()
            for record in history:
                for message_id in _iter_message_ids(record):
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    yield message_id, page_cursor

            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
                cursor_in_flight = page_cursor
            else:
                return

    def list_messages_since(
        self,
        *,
        since_epoch_seconds: int,
        page_size: int = _PAGE_SIZE,
    ) -> Iterator[str]:
        """Yield ``message_id`` for every message received at or after ``since_epoch_seconds``.

        Used by the connector's ADR-0010 §Phase 14 改訂 (j) TTL
        fallback path: when the stored ``startHistoryId`` is rejected
        as expired, the connector walks
        ``users.messages.list?q=after:<epoch>`` over the configured
        ``fallback_window_days`` window so messages that arrived
        during the TTL gap surface as :class:`SourceObserved` events.
        The projection's natural-key dedup on
        ``(connector_name, external_id)`` absorbs the steady-state
        overlap.

        Also used by first-sync bootstrap (cursor is ``None``): the
        connector uses a 1-day initial window by default so the
        operator's mailbox does not get re-emitted from the
        beginning of time.

        Parameters
        ----------
        since_epoch_seconds:
            Unix epoch seconds (UTC). Used verbatim inside Gmail's
            ``q=after:<epoch>`` selector. Gmail documents ``after:``
            as accepting integer Unix seconds in addition to
            ``YYYY/MM/DD`` form.
        page_size:
            ``maxResults`` for the underlying ``users.messages.list``
            call. Defaults to :data:`_PAGE_SIZE` (100) — Gmail's
            documented sweet spot.

        Yields
        ------
        str
            One ``message.id`` per matched message. No cursor is
            yielded alongside the id because the caller is in
            fallback / bootstrap mode and does not persist an
            intermediate cursor (the in-flight ``historyId`` is
            replaced by the freshly-bootstrapped one *after* the
            full-pass completes, per ADR-0010 §Phase 14 改訂 (j)).

        Raises
        ------
        ConnectorFailedError
            On any non-recoverable transport / API failure or when
            the retry budget is exhausted. Gmail's 429 / 5xx /
            rate-limit backoff is shared with the steady-state
            ``history.list`` path through :meth:`_request`.
        """
        url = f"{_USERS_BASE}/messages"
        params: dict[str, str] = {
            "q": f"after:{since_epoch_seconds}",
            "maxResults": str(page_size),
        }

        while True:
            body = self._request("GET", url, params=params)
            messages_obj = body.get("messages")
            # Gmail omits ``messages`` when the query yields zero
            # hits; treat the absence as "done" rather than as an
            # error.
            messages = (
                cast(list[dict[str, Any]], messages_obj) if isinstance(messages_obj, list) else []
            )

            for record in messages:
                message_id = record.get("id")
                if isinstance(message_id, str) and message_id:
                    yield message_id

            next_page_token = body.get("nextPageToken")
            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
            else:
                return

    def get_message(self, *, message_id: str) -> RawGmailMessage:
        """Fetch ``users.messages.get(id=<message_id>, format='full')``.

        Returns a :class:`RawGmailMessage` with body / labels /
        threadId / headers extracted from the response. The mapper
        consumes this verbatim.

        Parameters
        ----------
        message_id:
            Gmail message id. The id is opaque; the method does not
            validate it beyond non-emptiness.

        Raises
        ------
        ConnectorFailedError
            On non-2xx (other than the retried 429 / 5xx) or
            transport failure. Tokens never appear in the raised
            message.
        """
        if not message_id:
            raise ConnectorFailedError(
                "Gmail users.messages.get was called with an empty message_id"
            )
        url = f"{_USERS_BASE}/messages/{message_id}"
        body = self._request("GET", url, params={"format": "full"})
        return _normalise_message(body)

    def close(self) -> None:
        """Release the underlying ``httpx.Client`` socket.

        Optional — the connection pool is GC-managed — but provided so
        a long-lived service process can clean up between sync runs.
        """
        self._client.close()

    # ----- internals -------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Issue a Gmail request with bearer auth + 429 backoff + history-id-expired detection.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **404** on ``users.history.list`` — history id expired
          (ADR-0010 §Phase 14 改訂 (j)). Raised as
          :class:`HistoryIdExpiredError` so the connector layer can
          switch to the fallback path; not retried inline. Other 404s
          surface as :class:`ConnectorFailedError`.
        * **429** — Gmail's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **403 with ``rateLimitExceeded`` / ``userRateLimitExceeded``
          / ``quotaExceeded``** — Gmail's documented user-quota
          signals, same handling as 429.
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
                    f"Gmail request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 404 and _is_history_id_expired(url, response):
                # History id expired. Not retryable here: the
                # connector must restart in fallback mode after
                # bootstrapping a fresh id via
                # :meth:`get_profile_history_id`.
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
            if response.status_code == 401 and _is_insufficient_scope(response):
                # Re-consent signal: the stored refresh token was minted
                # against an older scope set that no longer covers
                # ``gmail.readonly``. Phase 14 G2 OQ6 scenario: operator
                # carrying forward a Phase 13 (drive-only) refresh token
                # into Phase 14 G3 without re-running the paste-code
                # flow. Raised as :class:`GoogleAuthError` (subclass of
                # :class:`ConfigError`) so the CLI surfaces an
                # actionable re-auth hint rather than a generic
                # connector failure.
                raise GoogleAuthError(
                    "Gmail request returned 401 insufficient_scope. "
                    "The stored refresh token does not grant gmail.readonly. "
                    "Re-run: opshub connector auth set google_workspace"
                )
            if response.status_code >= 400:
                raise ConnectorFailedError(
                    f"Gmail request returned {response.status_code}: {method} {url}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(f"Gmail response from {url} was not valid JSON") from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(f"Gmail response from {url} was not a JSON object")
            return cast(dict[str, Any], body)

        raise ConnectorFailedError(
            f"Gmail request failed after {_MAX_REQUEST_ATTEMPTS} "
            f"attempts: {method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


def _is_history_id_expired(url: str, response: Any) -> bool:
    """True iff a 404 on the history endpoint indicates ``historyNotFound``.

    Gmail returns a JSON error body shaped like
    ``{"error": {"code": 404, "errors": [{"reason": "notFound", ...}],
    "message": "Requested entity was not found."}}`` when the history
    id is no longer valid; the reason text is consistent enough that
    URL-anchored detection (``/history`` route → "history not found")
    is the simplest robust check. We also accept any 404 on the
    ``/history`` route as expiry because the API does not return a
    404 there for any other reason (a non-existent mailbox would be
    a 401 / 403 / 404 on ``/profile`` instead).
    """
    # URL-anchored detection is sufficient: Gmail's ``/history`` endpoint
    # has no other documented 404 path (a missing mailbox surfaces as
    # 401 / 403 on ``/profile`` instead). Body inspection is intentionally
    # skipped — Google's error message text is locale-sensitive and the
    # ``error.errors[].reason`` field carries the same "notFound" string
    # whether the cause is the documented TTL expiry or a transient
    # backend hiccup; either way the recovery path is the same.
    del response  # parameter retained for future body-aware inspection
    return "/history" in url


def _is_insufficient_scope(response: Any) -> bool:
    """True iff a 401 carries Google's ``insufficient_scope`` signal.

    Google's OAuth 2.0 protected-resource errors surface as either:

    * A ``WWW-Authenticate: Bearer error="insufficient_scope" ...``
      header (the documented OAuth 2.0 shape), or
    * A JSON body with ``error="invalid_token"`` plus
      ``error_subtype="insufficient_scope"`` on the API-gateway path.

    Either form indicates the stored access / refresh token was minted
    against an older scope set that no longer covers the requested
    endpoint (Phase 14 G2 OQ6 scenario: operator carrying forward a
    Phase 13 drive-only refresh token into Phase 14 G3). The recovery
    is a re-consent, not a retry — so we surface it as
    :class:`GoogleAuthError` (subclass of :class:`ConfigError`) with an
    actionable hint pointing at the paste-code flow command.

    Defensive on both axes: header parsing tolerates absence /
    case-insensitivity; body parsing tolerates missing / malformed
    JSON. A 401 without either signal falls through to the generic
    ``ConnectorFailedError`` path so unrelated auth failures stay
    distinguishable. Mirrors
    :func:`opshub.connectors.google_calendar.client._is_insufficient_scope`
    one-for-one (the two connectors share the recovery contract).
    """
    www_auth = ""
    headers_obj = getattr(response, "headers", None)
    if headers_obj is not None:
        try:
            www_auth = headers_obj.get("WWW-Authenticate", "") or ""
        except (AttributeError, TypeError):
            www_auth = ""
    if "insufficient_scope" in www_auth.lower():
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    body_dict = cast(dict[str, Any], body)
    error_obj = body_dict.get("error")
    error_subtype = body_dict.get("error_subtype")
    if isinstance(error_subtype, str) and error_subtype == "insufficient_scope":
        return True
    # Google's API-gateway sometimes nests the OAuth error under
    # ``error.status`` / ``error.message`` instead of the top-level
    # OAuth shape. We accept both forms.
    if isinstance(error_obj, dict):
        error_dict = cast(dict[str, Any], error_obj)
        status = error_dict.get("status")
        if isinstance(status, str) and status == "PERMISSION_DENIED":
            message_obj = error_dict.get("message")
            if isinstance(message_obj, str) and "insufficient" in message_obj.lower():
                return True
    return False


def _is_rate_limit_error(response: Any) -> bool:
    """True iff a Gmail 403 body carries a rate-limit reason code.

    Gmail returns ``userRateLimitExceeded`` / ``rateLimitExceeded`` /
    ``quotaExceeded`` in the JSON body's ``error.errors[].reason``
    field on 403s that are really rate limits rather than scope /
    permission denials. We must distinguish: scope denials would not
    benefit from backoff and would just retry-then-fail more loudly.
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
    extra few seconds (same defensive shape Drive / Teams use).
    """
    if header_value is None:
        return fallback
    try:
        return int(header_value)
    except (TypeError, ValueError):
        return fallback


def _iter_message_ids(record: dict[str, Any]) -> Iterator[str]:
    """Yield each ``message.id`` referenced by a single history record.

    Gmail's ``users.history.list`` response splits each history record
    into sub-arrays:

    * ``messagesAdded[*].message.id`` — new messages.
    * ``messagesDeleted[*].message.id`` — permanently deleted.
    * ``labelsAdded[*].message.id`` — label changes (e.g. STARRED).
    * ``labelsRemoved[*].message.id`` — label changes.

    Phase 14 G3 walks **three** of the four sub-arrays
    (``messagesAdded`` / ``labelsAdded`` / ``labelsRemoved``) and
    yields each referenced message id once per record. Re-emission of
    an existing message is absorbed by the projection's natural-key
    dedup; the mapper distinguishes "this run was a label change
    only" vs "new content" via the ``[Labels: ...]`` prepend so
    emitting a re-observation on label changes is the right shape
    for the secretary recall path.

    ``messagesDeleted`` is **deliberately skipped** — fetching those
    ids via ``users.messages.get`` would return 404 on every entry
    and waste a round-trip per deletion. The last-known projection
    row keeps the metadata-only state for retained recall
    (ADR-0020), and a future Phase 15+ extension can introduce a
    soft-delete event if the operator surface needs it. The
    connector layer's 404-tolerant ``_emit_message`` still handles
    the rarer "message disappeared between history.list and
    messages.get" race so transient races during the sync window
    do not abort the run.
    """
    for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
        sub = record.get(key)
        if not isinstance(sub, list):
            continue
        sub_list = cast(list[Any], sub)  # type: ignore[redundant-cast]
        for entry in sub_list:
            if not isinstance(entry, dict):
                continue
            entry_dict = cast(dict[str, Any], entry)
            message = entry_dict.get("message")
            if not isinstance(message, dict):
                continue
            message_dict = cast(dict[str, Any], message)
            mid = message_dict.get("id")
            if isinstance(mid, str) and mid:
                yield mid


def _normalise_message(raw: dict[str, Any]) -> RawGmailMessage:
    """Lift a Gmail ``users.messages.get`` payload into :class:`RawGmailMessage`.

    The function tolerates Gmail's nested-object shape:

    * Top-level fields (``id`` / ``threadId`` / ``labelIds`` /
      ``snippet`` / ``internalDate`` / ``historyId``).
    * ``payload.headers`` — flat list of ``{name, value}`` dicts; we
      pull ``Subject`` / ``From`` case-insensitively.
    * ``payload.parts`` — recursive MIME tree. We walk it depth-first
      preferring text/plain → text/html and decode the first match of
      each. Subsequent parts of the same mediatype are ignored
      (matches Outlook's "first body wins" behaviour and avoids
      concatenating quoted-reply chains into a giant blob).

    Empty / missing fields normalise to ``""`` (or empty tuple for
    label_ids) so the mapper does not need to defend against
    ``None``.
    """
    message_id = str(raw.get("id") or "")
    thread_id = str(raw.get("threadId") or "")
    history_id = str(raw.get("historyId") or "")
    internal_date_ms = str(raw.get("internalDate") or "")
    snippet = str(raw.get("snippet") or "")

    label_ids_obj = raw.get("labelIds")
    label_ids: tuple[str, ...] = ()
    if isinstance(label_ids_obj, list):
        label_ids_list = cast(list[Any], label_ids_obj)  # type: ignore[redundant-cast]
        label_ids = tuple(
            str(entry) for entry in label_ids_list if isinstance(entry, str) and entry
        )

    payload_obj = raw.get("payload")
    payload = cast(dict[str, Any], payload_obj) if isinstance(payload_obj, dict) else {}
    subject_header = _header_value(payload, "subject")
    from_header = _header_value(payload, "from")
    body_text, body_html = _extract_bodies(payload)

    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        label_ids=label_ids,
        history_id=history_id,
        internal_date_ms=internal_date_ms,
        from_header=from_header,
        subject_header=subject_header,
        snippet=snippet,
        body_text=body_text,
        body_html=body_html,
        raw=raw,
    )


def _header_value(payload: dict[str, Any], target: str) -> str:
    """Return the (first) header value for ``target`` (case-insensitive)."""
    headers_obj = payload.get("headers")
    if not isinstance(headers_obj, list):
        return ""
    headers_list = cast(list[Any], headers_obj)  # type: ignore[redundant-cast]
    lowered = target.lower()
    for header in headers_list:
        if not isinstance(header, dict):
            continue
        header_dict = cast(dict[str, Any], header)
        name_obj = header_dict.get("name")
        if not isinstance(name_obj, str):
            continue
        if name_obj.lower() != lowered:
            continue
        value_obj = header_dict.get("value")
        if isinstance(value_obj, str):
            return value_obj
        return ""
    return ""


def _extract_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    """Return ``(text_plain, text_html)`` for the first matches in the MIME tree.

    Walks the ``payload.parts`` tree depth-first. Returns the first
    text/plain and first text/html part body decoded from Gmail's
    URL-safe base64 (``body.data``). Subsequent parts of the same
    mediatype are ignored — matches Outlook's "first body wins"
    behaviour and avoids concatenating quoted-reply chains.

    Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k): the body is
    kept verbatim — no markitdown, no HTML stripping. Downstream
    consumers (recall / personal-brief / reply-draft) treat it as
    untrusted reference material via the do-not-follow preamble
    (ADR-0015 §決定 (f)).
    """
    text_plain = ""
    text_html = ""

    def visit(node: dict[str, Any]) -> None:
        nonlocal text_plain, text_html
        mime_type_obj = node.get("mimeType")
        mime_type = mime_type_obj if isinstance(mime_type_obj, str) else ""

        # Gmail represents a single-part message by putting the body
        # directly under ``payload.body`` (no ``parts`` array). The
        # mimeType then lives at the top level.
        body_obj = node.get("body")
        body_dict = cast(dict[str, Any], body_obj) if isinstance(body_obj, dict) else {}
        data_obj = body_dict.get("data")
        # Skip attachments (Gmail sets ``attachmentId`` and omits
        # ``data`` for them); Phase 14 plan §1 OQ4 explicitly excludes
        # attachment retention (Outlook symmetric).
        if isinstance(data_obj, str) and data_obj and "attachmentId" not in body_dict:
            decoded = _decode_gmail_body(data_obj)
            if mime_type == "text/plain" and not text_plain:
                text_plain = decoded
            elif mime_type == "text/html" and not text_html:
                text_html = decoded

        parts_obj = node.get("parts")
        if isinstance(parts_obj, list):
            parts_list = cast(list[Any], parts_obj)  # type: ignore[redundant-cast]
            for child in parts_list:
                if isinstance(child, dict):
                    visit(cast(dict[str, Any], child))
                if text_plain and text_html:
                    # Both bodies found; no need to keep walking.
                    return

    visit(payload)
    return text_plain, text_html


def _decode_gmail_body(data: str) -> str:
    """Decode Gmail's URL-safe base64 body data.

    Gmail returns body content as base64url-encoded bytes inside
    ``payload.body.data`` (and ``parts[*].body.data``). The Python
    stdlib's :func:`base64.urlsafe_b64decode` handles the URL-safe
    alphabet but requires correct padding; Gmail's encoder sometimes
    omits trailing ``=`` padding, so we re-pad defensively before
    decoding.

    Returns the decoded string (UTF-8 with ``replace`` error handling)
    or the empty string on any decode failure — a malformed body part
    should not block the rest of the sync.
    """
    import base64

    # Pad to a multiple of 4 (base64 alphabet quartets).
    padded = data + "=" * ((4 - len(data) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
