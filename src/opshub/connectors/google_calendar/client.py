"""Calendar API v3 client + raw item shape (Phase 14 G4).

A thin ``httpx``-backed wrapper over Google Calendar's v3 REST
endpoints. The wrapper covers exactly the Phase 14 G4 MVP needs:

* ``calendars/<id>/events`` (a.k.a. ``events.list``) — both the delta
  variant (``syncToken=...``) and the bootstrap / fallback variant
  (``timeMin`` / ``timeMax`` window). ``singleEvents=false`` is pinned
  on every call so recurring masters and their overrides arrive as
  distinct entries (the connector emits each as a separate
  :class:`SourceObserved`; Phase 14 plan OQ3 / ADR-0010 §Phase 14
  改訂 (l) §不変条件 3).

SDK choice
----------

``httpx`` + manual OAuth + manual JSON, not
``google-api-python-client``. The rationale is identical to the Phase
13 ``google_workspace`` client — kept here only as a pointer so future
readers do not have to cross-reference:

1. Cold-start budget (ADR-0001 — ``opshub --help`` ≤ 300ms).
2. Sibling-connector consistency (``ms365`` / ``box`` / ``teams`` /
   ``google_workspace`` all use ``httpx``).
3. Auth surface (Phase 14 G2 ``google_auth`` is httpx-native).
4. Test ergonomics (``httpx.MockTransport`` is the project's standard
   mock seam).

Phase 14 plan §8 OQ14 confirms this decision; the Calendar event
payload parsing is lightweight enough that the cold-start cost stays
in budget.

Retry / rate-limit
------------------

Calendar's throttling envelope is HTTP 403 ``rateLimitExceeded`` /
``userRateLimitExceeded`` / ``quotaExceeded`` or HTTP 429 ``Too Many
Requests``. We honour ``Retry-After`` directly when present and
otherwise back off exponentially (1 s / 2 s / 4 s) for up to three
attempts per request, matching Phase 13 google_workspace +
ms365 / teams precedent. 5xx server errors get the same backoff.
Persistent failure escalates to
:class:`~opshub.core.errors.ConnectorFailedError`.

Sync token invalidation
-----------------------

Calendar returns HTTP 410 ``Gone`` when the stored ``syncToken`` is
no longer valid (Google can invalidate at any time; the canonical
trigger is "too much time elapsed since the last sync"). The client
surfaces this to the connector via a sentinel
:class:`SyncTokenExpiredError` so the connector layer can fall back
to a ``timeMin``/``timeMax`` full-pass and bootstrap a fresh sync
token — structurally identical to Drive's
:class:`opshub.connectors.google_workspace.client.PageTokenExpiredError`
shape.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.connectors.google_auth.auth import GoogleAuthError
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth


__all__ = [
    "CALENDAR_API_BASE",
    "CalendarClient",
    "RawCalendarEvent",
    "SyncTokenExpiredError",
]


#: Google Calendar API v3 base URL. v3 is the GA endpoint; there is no
#: documented stable v2 path for opshub to fall back on.
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

#: HTTP timeout for Calendar calls. 30 s mirrors
#: :class:`opshub.connectors.google_workspace.client.DriveClient` /
#: :class:`opshub.connectors.ms365.fetcher.MS365Fetcher` and is
#: comfortably above Calendar's tail latency on the larger
#: ``events.list`` page sizes.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`CalendarClient._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts
#: matches the Phase 7 MS365 + Phase 11 Teams + Phase 13 Drive retry
#: budgets.
_MAX_REQUEST_ATTEMPTS = 3

#: ``maxResults`` for ``events.list`` pagination. 250 is Calendar's
#: documented default and stays under the 2500 hard cap. Smaller
#: pages would multiply request count for no measurable wall-clock
#: benefit; larger pages tend to trip 429 throttling on busy
#: calendars per Calendar's own guidance.
_PAGE_SIZE = 250

#: Pinned parameters for every ``events.list`` call. ``singleEvents``
#: is the load-bearing pin — leaving it at the default ``true`` would
#: cause Calendar to *expand* recurring events into instances, which
#: would (a) explode the event log with derived state (Phase 14 plan
#: §Alternatives §4 explicitly rejected this) and (b) lose the
#: connector's ability to distinguish "master event" from "override".
#: With ``singleEvents=false`` Calendar returns masters and overrides
#: as distinct entries (overrides carry ``recurringEventId`` +
#: ``originalStartTime``), which the mapper consumes one-for-one.
#:
#: ``showDeleted=true`` so cancelled events surface as a status
#: change (Phase 14 OQ7 / ADR-0010 §Phase 14 改訂 (l) §不変条件 4 —
#: same retain-everything posture as Drive's ``includeRemoved`` flag).
_EVENTS_LIST_PARAMS_BASE: dict[str, str] = {
    "singleEvents": "false",
    "showDeleted": "true",
    "maxResults": str(_PAGE_SIZE),
}


@dataclass(frozen=True, slots=True)
class RawCalendarEvent:
    """Normalised view of a single Calendar event.

    Mirrors :class:`opshub.connectors.ms365.fetcher.RawCalendarEvent`
    field-for-field where the underlying APIs overlap (mapper symmetry
    relies on this — see ``tests/unit/connectors/test_mapper_symmetry.py``).
    The few Google-specific fields (``recurring_event_id``,
    ``original_start_iso``, ``status``, ``location``, ``description``,
    ``organizer_email``, ``attendees``, ``recurrence``) are appended in
    the order the mapper consumes them so reading them in source order
    matches the order they appear in the summary / body assembly.

    Attributes
    ----------
    id:
        Calendar event id (Google's stable opaque identifier). Pairs
        with the connector name to form the natural key the projection
        upserts on.
    subject:
        Event ``summary`` (Google calls the title-bar string the
        "summary"; the mapper renames it to ``subject`` for symmetry
        with the Microsoft 365 mapper which uses Graph's ``subject``).
    start_iso:
        ISO 8601 start timestamp. For timed events this is the
        ``start.dateTime`` field (with timezone offset preserved); for
        all-day events Google returns ``start.date`` (``YYYY-MM-DD``,
        no time component) and the connector forwards it verbatim so
        the mapper can render the all-day shape distinctly.
    end_iso:
        ISO 8601 end timestamp (same shape as ``start_iso``).
    attendees_count:
        ``len(attendees)`` — symmetric with the Microsoft 365 mapper
        which counts Graph's attendee array length the same way.
    web_link:
        Stable URL to surface in ``sources.url``. Google calls this
        field ``htmlLink``; the dataclass renames it to ``web_link``
        for symmetry with the Microsoft 365 mapper.
    last_modified_iso:
        ISO 8601 ``updated`` timestamp (Google's documented
        "last-modified" field). Used as ``occurred_at`` for
        :class:`SourceObserved`.
    status:
        Event status (``confirmed`` / ``tentative`` / ``cancelled``).
        Mapper surfaces a ``[cancelled]`` marker in the summary so
        downstream consumers can detect status changes; ADR-0020
        retain-everything keeps the event in the projection.
    description:
        Event description body (Google's free-text description field).
        Forwarded to the mapper's body assembly for "agenda" lines.
    location:
        Location string (Google's free-text location field, typically
        the room / address). Forwarded to body assembly.
    organizer_email:
        Organiser email. Used by the connector's excludes filter
        (sender-style filter) and by the mapper body assembly.
    attendees:
        Tuple of attendee email strings (``""`` entries are dropped at
        normalisation time). Forwarded to body assembly as a
        newline-separated list.
    recurrence:
        Tuple of RRULE / RDATE / EXDATE / EXRULE strings exactly as
        Google returns them. Empty for non-recurring events.
        Forwarded to body assembly so downstream consumers can read
        the RRULE without re-fetching the event.
    recurring_event_id:
        ``recurringEventId`` field — non-empty iff this entry is an
        **override** of a recurring master (Google API returns
        overrides as independent events with this pointer back to the
        master). The mapper preserves it on the body so projection
        consumers can join overrides back to their master series.
    original_start_iso:
        ``originalStartTime`` field — non-empty iff this entry is an
        override (mirrors ``recurring_event_id`` presence). Identifies
        which occurrence in the series this override replaces.
    raw:
        Verbatim ``events.list`` payload, kept for forensic debugging
        (mapper fixtures, future backfill). The mapper does not
        persist this.
    """

    id: str
    subject: str
    start_iso: str
    end_iso: str
    attendees_count: int
    web_link: str
    last_modified_iso: str
    status: str
    description: str
    location: str
    organizer_email: str
    attendees: tuple[str, ...]
    recurrence: tuple[str, ...]
    recurring_event_id: str
    original_start_iso: str
    raw: dict[str, Any]


class SyncTokenExpiredError(Exception):
    """Internal signal: Calendar returned 410 for the stored sync token.

    Caught by :meth:`CalendarClient.fetch_events_delta` callers so the
    connector layer can fall back to a ``timeMin`` / ``timeMax``
    window walk and bootstrap a fresh ``nextSyncToken``. Never
    surfaced to upstream callers — the connector either completes via
    fallback or raises :class:`ConnectorFailedError` from inside the
    fallback path. Mirrors
    :class:`opshub.connectors.google_workspace.client.PageTokenExpiredError`
    one-for-one.
    """


class CalendarClient:
    """Calendar API v3 client (``events.list`` with / without ``syncToken``).

    Construction is intentionally lightweight so the connector wiring
    layer can hold one client per sync run without paying a high
    setup cost. The ``httpx.Client`` is created here (rather than per
    call) so the connection pool is reused across pages.

    The class is **not** thread-safe — Phase 14 G4 syncs run
    sequentially inside ``opshub connector sync google_calendar`` (one
    connector at a time per process), so a per-call lock would be
    needless overhead.
    """

    def __init__(self, auth: GoogleWorkspaceAuth) -> None:
        """Construct a client bound to a configured :class:`GoogleWorkspaceAuth`.

        :param auth: An auth helper whose
            :meth:`GoogleWorkspaceAuth.get_access_token` returns a
            valid Calendar bearer. The client calls that method on
            every request so refresh-token rotation is observed
            automatically (auth persists the rotated value through
            :mod:`opshub.core.secrets`). Phase 14 G2 (#294) made the
            shared auth helper carry all three Google read scopes so
            this Calendar client can call ``events.list`` without
            re-running the OAuth dance.

        :raises ConfigError: When the ``[connectors-google-workspace]``
            extras are missing — same message shape as the auth
            module's ``httpx`` guard so the operator gets one
            consistent install hint.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Google Calendar connector requires the "
                "[connectors-google-workspace] extras. "
                "Install with: uv sync --extra connectors-google-workspace"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)

    # ----- public API ------------------------------------------------------

    def fetch_events_delta(
        self,
        *,
        calendar_id: str = "primary",
        sync_token: str,
    ) -> Iterator[tuple[RawCalendarEvent | None, str]]:
        """Yield ``(event, cursor)`` for every change since ``sync_token``.

        Uses the delta variant of ``events.list``: Calendar returns
        only the events that were created / modified / cancelled
        since the stored ``syncToken`` was minted. Pagination is via
        ``nextPageToken`` until the final page, which carries
        ``nextSyncToken`` instead — that value is the cursor for the
        next sync.

        Cursor semantics (mirrors the Drive ``fetch_changes`` walker
        per Phase 14 plan symmetry intent):

        * Until the final page is reached, the yielded cursor is the
          **incoming** ``sync_token`` so a mid-iteration crash does
          not advance the cursor past unconsumed events.
        * On the final page (the page that returns ``nextSyncToken``
          and no ``nextPageToken``) the yielded cursor is the fresh
          ``nextSyncToken`` — the caller persists it and the next
          sync resumes there.
        * **Final sentinel** (no-changes case): after the last events
          page is drained the iterator emits one ``(None, cursor)``
          tuple carrying the freshly-minted sync token. Without the
          sentinel a "no changes since last sync" run would never
          yield, the caller would keep replaying the old token, and
          we would miss any new sync token Calendar issued. The
          connector's ``_consume_delta`` consumes the sentinel by
          skipping ``observe`` when ``event is None`` and capturing
          the cursor verbatim.

        Raises
        ------
        SyncTokenExpiredError
            Calendar rejected ``sync_token`` as expired (HTTP 410
            ``Gone``). The connector layer traps this, falls back to
            a ``timeMin`` / ``timeMax`` window walk, and resumes.
        ConnectorFailedError
            On any other transport / API failure, or when the retry
            budget is exhausted. Tokens never appear in the error
            message (ADR-0005 / ADR-0020 §(e)).
        """
        url = f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events"
        params: dict[str, str] = dict(_EVENTS_LIST_PARAMS_BASE)
        params["syncToken"] = sync_token
        cursor_in_flight = sync_token

        while True:
            body = self._request("GET", url, params=params)
            items_obj = body.get("items")
            if not isinstance(items_obj, list):
                raise ConnectorFailedError(
                    "Google Calendar events.list response is missing the "
                    "'items' list (unexpected response shape)"
                )
            items = cast(list[dict[str, Any]], items_obj)

            next_page_token = body.get("nextPageToken")
            next_sync_token = body.get("nextSyncToken")
            # The final page is identified by the presence of
            # ``nextSyncToken`` and the absence of
            # ``nextPageToken``. When we are on that page the cursor
            # we hand out advances to the new sync token.
            is_final_page = (
                isinstance(next_sync_token, str)
                and next_sync_token
                and not isinstance(next_page_token, str)
            )
            page_cursor: str = cast(str, next_sync_token) if is_final_page else cursor_in_flight

            for raw_event in items:
                event = _normalise_event(raw_event)
                if event is None:
                    continue
                yield event, page_cursor

            # Advance to the next page when Calendar supplies one;
            # otherwise the loop exits and the caller persists the
            # ``next_sync_token`` (the value we already handed out on
            # the final yield above). ``params`` carries ``syncToken``
            # on first call but Calendar refuses to combine
            # ``syncToken`` with ``pageToken``, so we swap the keys
            # when advancing pages (Calendar's own documented
            # pagination contract).
            if isinstance(next_page_token, str) and next_page_token:
                params.pop("syncToken", None)
                params["pageToken"] = next_page_token
                cursor_in_flight = page_cursor
            else:
                # Emit a final sentinel so the caller observes the new
                # sync token even when no events changed in this delta
                # (the common "nothing happened" case).
                if is_final_page:
                    yield None, page_cursor
                return

    def fetch_events_window(
        self,
        *,
        calendar_id: str = "primary",
        time_min: str,
        time_max: str,
    ) -> Iterator[tuple[RawCalendarEvent | None, str | None]]:
        """Yield ``(event, cursor)`` for every event in ``[time_min, time_max]``.

        Used both for first-sync bootstrap (when the stored cursor is
        ``None``) and for the 410-GONE TTL fallback (when the stored
        ``syncToken`` was rejected). Calendar returns ``nextSyncToken``
        at the end of the window walk so the caller can persist it as
        the cursor for the next sync (the steady-state delta path
        resumes from there).

        Parameters
        ----------
        calendar_id:
            Calendar to query. Phase 14 G4 MVP defaults to ``"primary"``
            (operator's own primary calendar); secondary calendars
            are a Phase 15+ extension per OQ13.
        time_min:
            RFC 3339 timestamp (e.g. ``"2026-02-28T00:00:00Z"``)
            forwarded verbatim. The connector computes this from
            ``now - time_min_days``.
        time_max:
            RFC 3339 timestamp forwarded verbatim. The connector
            computes this from ``now + time_max_days`` so future
            events the operator already accepted appear in the
            recall projection.

        Yields
        ------
        tuple[RawCalendarEvent | None, str | None]
            Each tuple carries the event plus the latest cursor
            value the iterator has observed (``None`` on every page
            except the final one; on the final page the cursor is
            the freshly-minted ``nextSyncToken``). The connector
            persists the final cursor so subsequent syncs resume on
            the delta path.

            **Final sentinel**: after the last events page is drained
            the iterator yields one extra tuple
            ``(None, next_sync_token)`` carrying the freshly-minted
            sync token even when the window contained zero events.
            This is the load-bearing path for empty calendars (and
            for TTL-fallback recovery on calendars with no events in
            the configured window) — without the sentinel the
            connector would never observe the new ``nextSyncToken``
            and would re-trigger the full-pass on every subsequent
            sync. The connector's ``_consume_window`` consumes the
            sentinel by skipping ``observe`` when ``event is None``
            and capturing the cursor verbatim.

        Raises
        ------
        ConnectorFailedError
            On any non-recoverable transport / API failure or when
            the retry budget is exhausted. Tokens never appear in
            the raised message.
        """
        url = f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events"
        params: dict[str, str] = dict(_EVENTS_LIST_PARAMS_BASE)
        params["timeMin"] = time_min
        params["timeMax"] = time_max

        last_cursor: str | None = None
        while True:
            body = self._request("GET", url, params=params)
            items_obj = body.get("items")
            if not isinstance(items_obj, list):
                raise ConnectorFailedError(
                    "Google Calendar events.list response is missing the "
                    "'items' list (unexpected response shape)"
                )
            items = cast(list[dict[str, Any]], items_obj)

            next_page_token = body.get("nextPageToken")
            next_sync_token = body.get("nextSyncToken")
            is_final_page = (
                isinstance(next_sync_token, str)
                and next_sync_token
                and not isinstance(next_page_token, str)
            )
            page_cursor: str | None = next_sync_token if is_final_page else last_cursor

            for raw_event in items:
                event = _normalise_event(raw_event)
                if event is None:
                    continue
                yield event, page_cursor

            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
                last_cursor = page_cursor
            else:
                # Emit a final sentinel so the caller observes the new
                # sync token even on an empty calendar / empty fallback
                # window. ``event=None`` is the documented marker.
                if is_final_page:
                    yield None, page_cursor
                return

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
        """Issue a Calendar request with bearer auth + 429 backoff + sync-token-expired detection.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **410** — sync token expired (ADR-0010 §Phase 14 改訂 (j)).
          Raised as :class:`SyncTokenExpiredError` so the iterator can
          switch to the fallback path; not retried inline.
        * **429** — Calendar's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **403 with ``rateLimitExceeded`` / ``userRateLimitExceeded``
          / ``quotaExceeded``** — Calendar's documented user-quota
          signal, same handling as 429.
        * **5xx** — Calendar documents these as transient; same backoff.
        * **Other 4xx** — fail-fast: wrap into
          :class:`ConnectorFailedError` so the CLI driver always sees
          one error class.

        Tokens are NEVER logged or included in the raised message
        (ADR-0005 / ADR-0020 §(e) provenance discipline).
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
                    f"Google Calendar request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 410:
                # Sync token expired. Not retryable here: the connector
                # must restart in fallback mode using a ``timeMin`` /
                # ``timeMax`` window walk.
                raise SyncTokenExpiredError
            if response.status_code == 429 or (
                response.status_code == 403 and _is_rate_limit_error(response)
            ):
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                # 5xx: Calendar documents these as transient; back off.
                time.sleep(2**attempt)
                continue
            if response.status_code == 401 and _is_insufficient_scope(response):
                # Re-consent signal: the stored refresh token was minted
                # against an older scope set that no longer covers
                # ``calendar.readonly``. Phase 14 G2 OQ6 scenario:
                # operator carrying forward a Phase 13 (drive-only)
                # refresh token into Phase 14 G4 without re-running the
                # paste-code flow. Raised as :class:`GoogleAuthError`
                # (subclass of :class:`ConfigError`) so the CLI surfaces
                # an actionable re-auth hint rather than a generic
                # connector failure.
                raise GoogleAuthError(
                    "Google Calendar request returned 401 insufficient_scope. "
                    "The stored refresh token does not grant calendar.readonly. "
                    "Re-run: opshub connector auth set google_workspace"
                )
            if response.status_code >= 400:
                raise ConnectorFailedError(
                    f"Google Calendar request returned {response.status_code}: {method} {url}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(
                    f"Google Calendar response from {url} was not valid JSON"
                ) from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(
                    f"Google Calendar response from {url} was not a JSON object"
                )
            return cast(dict[str, Any], body)

        raise ConnectorFailedError(
            f"Google Calendar request failed after {_MAX_REQUEST_ATTEMPTS} "
            f"attempts: {method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


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
    Phase 13 drive-only refresh token into Phase 14 G4). The recovery
    is a re-consent, not a retry — so we surface it as
    :class:`GoogleAuthError` (subclass of :class:`ConfigError`) with an
    actionable hint pointing at the paste-code flow command.

    Defensive on both axes: header parsing tolerates absence /
    case-insensitivity; body parsing tolerates missing / malformed
    JSON. A 401 without either signal falls through to the generic
    ``ConnectorFailedError`` path so unrelated auth failures stay
    distinguishable.
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
    """True iff a Calendar 403 body carries a rate-limit reason code.

    Calendar returns ``userRateLimitExceeded`` / ``rateLimitExceeded``
    / ``quotaExceeded`` in the JSON body's ``error.errors[].reason``
    field on 403s that are really rate limits rather than scope /
    permission denials. We must distinguish: scope denials would not
    benefit from backoff and would just retry-then-fail more loudly.
    Mirrors :func:`opshub.connectors.google_workspace.client._is_rate_limit_error`
    one-for-one.
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

    Calendar documents the header as an integer number of seconds; we
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


def _normalise_event(raw: dict[str, Any]) -> RawCalendarEvent | None:
    """Lift a Calendar ``events.list`` event payload into :class:`RawCalendarEvent`.

    Returns ``None`` for entries that lack an ``id`` field — Google
    documents this as an impossible case for ``events.list`` results
    but we defend against it to keep the iterator robust against
    malformed responses.

    All-day events return ``start.date`` / ``end.date`` (``YYYY-MM-DD``
    only) rather than ``start.dateTime`` / ``end.dateTime``. The
    normaliser picks whichever Google supplied and forwards it
    verbatim so the mapper can render the all-day shape distinctly
    (the mapper builds the summary from these strings, so timed and
    all-day events render as ``"<iso> - <iso> (N attendees)"`` with
    only the time-component shape differing).
    """
    event_id_obj = raw.get("id")
    if not isinstance(event_id_obj, str) or not event_id_obj:
        return None

    start_obj = raw.get("start")
    start_dict: dict[str, Any] = (
        cast(dict[str, Any], start_obj) if isinstance(start_obj, dict) else {}
    )
    end_obj = raw.get("end")
    end_dict: dict[str, Any] = cast(dict[str, Any], end_obj) if isinstance(end_obj, dict) else {}

    # Prefer ``dateTime`` (timed event); fall back to ``date`` (all-day).
    # Both come back as plain strings; the mapper does not need to
    # parse them — they are forwarded verbatim into the summary.
    start_iso = str(start_dict.get("dateTime") or start_dict.get("date") or "")
    end_iso = str(end_dict.get("dateTime") or end_dict.get("date") or "")

    # Original start time for override events — same shape (dateTime
    # or date) as ``start``. Empty for non-overrides.
    original_start_obj = raw.get("originalStartTime")
    original_start_dict: dict[str, Any] = (
        cast(dict[str, Any], original_start_obj) if isinstance(original_start_obj, dict) else {}
    )
    original_start_iso = str(
        original_start_dict.get("dateTime") or original_start_dict.get("date") or ""
    )

    organizer_obj = raw.get("organizer")
    organizer_email = ""
    if isinstance(organizer_obj, dict):
        organizer_dict = cast(dict[str, Any], organizer_obj)
        organizer_email = str(organizer_dict.get("email") or "")

    attendees_obj = raw.get("attendees")
    attendees_emails: list[str] = []
    attendees_count = 0
    if isinstance(attendees_obj, list):
        attendees_list = cast(list[Any], attendees_obj)  # type: ignore[redundant-cast]
        attendees_count = len(attendees_list)
        for entry in attendees_list:
            if not isinstance(entry, dict):
                continue
            entry_dict = cast(dict[str, Any], entry)
            email = entry_dict.get("email")
            if isinstance(email, str) and email:
                attendees_emails.append(email)

    recurrence_obj = raw.get("recurrence")
    recurrence_strings: list[str] = []
    if isinstance(recurrence_obj, list):
        recurrence_list = cast(list[Any], recurrence_obj)  # type: ignore[redundant-cast]
        for entry in recurrence_list:
            if isinstance(entry, str) and entry:
                recurrence_strings.append(entry)

    return RawCalendarEvent(
        id=event_id_obj,
        # Google uses ``summary`` for the event title; the mapper
        # consumes the field as ``subject`` for symmetry with the
        # Microsoft 365 Calendar mapper which reads Graph's ``subject``.
        subject=str(raw.get("summary") or ""),
        start_iso=start_iso,
        end_iso=end_iso,
        attendees_count=attendees_count,
        # Google's ``htmlLink`` is the stable web URL for the event.
        web_link=str(raw.get("htmlLink") or ""),
        last_modified_iso=str(raw.get("updated") or ""),
        status=str(raw.get("status") or ""),
        description=str(raw.get("description") or ""),
        location=str(raw.get("location") or ""),
        organizer_email=organizer_email,
        attendees=tuple(attendees_emails),
        recurrence=tuple(recurrence_strings),
        recurring_event_id=str(raw.get("recurringEventId") or ""),
        original_start_iso=original_start_iso,
        raw=raw,
    )
