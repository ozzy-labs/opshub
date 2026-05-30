"""Microsoft Graph fetcher for the MS365 connector (Phase 7 step B2).

Three endpoint groups (Phase 7 MVP):

1. Calendar events — ``GET /me/calendar/events?$filter=lastModifiedDateTime ge <iso>``
2. OneDrive changes — ``GET /me/drive/root/delta`` (uses Graph's delta link)
3. Outlook messages — ``GET /me/messages?$filter=receivedDateTime ge <iso>``

Each endpoint has its own cursor stored under a distinct ``cursor_key``
in the ``connector_cursors`` projection (``ms365:calendar``,
``ms365:onedrive``, ``ms365:outlook``). The B3 mapper / sync step wires
those cursors to :class:`opshub.connectors.context.ConnectorContext`;
B2 (this module) is concerned only with the wire format and yields
``(item, new_cursor)`` tuples so the caller can advance the cursor as
items are durably persisted.

Auth: bearer token from :class:`MS365Auth` (auto-refreshed). On a 401
we attempt one refresh via :meth:`MS365Auth.get_access_token` (which
itself re-issues an access token from the stored refresh token); on a
subsequent 401 we raise :class:`ConnectorFailedError` so the CLI driver
can record a ``ConnectorSyncFailed`` event (ADR-0010).

Rate-limit (HTTP 429): respect ``Retry-After`` header with exponential
backoff (1s / 2s / 4s, max 3 attempts). The Graph documentation
recommends honouring ``Retry-After`` whenever it is present; we fall
back to ``2 ** attempt`` when the header is missing so the connector
still backs off rather than hot-looping.

Cold-start guard (ADR-0001): ``httpx`` is imported lazily inside
:meth:`MS365Fetcher.__init__` so importing this module from the cold
path (e.g. ``opshub --help``) never pays the ``httpx`` cost when the
``[connectors-ms365]`` extras are not installed — mirrors the lazy
import pattern in :class:`opshub.llm.ollama_client.OllamaLLMClient`.

ADR-0005 External Content Minimization: the fetcher does NOT truncate
or sanitise bodies here — that responsibility lives in the B3 mapper.
The raw payload is preserved on the dataclass (``.raw``) so the mapper
can pick the exact fields it needs without re-issuing the request. The
mapper, not the fetcher, enforces the 200-char summary cap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.ms365.auth import MS365Auth


__all__ = [
    "CURSOR_CALENDAR",
    "CURSOR_ONEDRIVE",
    "CURSOR_OUTLOOK",
    "GRAPH_BASE",
    "MS365Fetcher",
    "RawCalendarEvent",
    "RawOneDriveItem",
    "RawOutlookMessage",
]


#: Microsoft Graph v1.0 base URL. The v1.0 surface is the GA endpoint
#: that returns stable schemas; the ``/beta`` surface is documented as
#: subject to change without notice and is intentionally avoided.
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: Cursor key for ``/me/calendar/events`` sync. Stored verbatim in the
#: ``connector_cursors`` projection alongside other connector cursors
#: (Phase 3 framework); the three MS365 endpoints each get their own
#: row so a partial failure in one stream cannot stall the others.
CURSOR_CALENDAR = "ms365:calendar"

#: Cursor key for ``/me/drive/root/delta``. The stored value is the
#: full ``@odata.deltaLink`` URL returned by Graph on the final page of
#: the previous sync — Microsoft documents this as opaque, so we treat
#: it as a black box and replay it as-is.
CURSOR_ONEDRIVE = "ms365:onedrive"

#: Cursor key for ``/me/messages`` sync.
CURSOR_OUTLOOK = "ms365:outlook"

#: HTTP timeout for Graph calls. 30s mirrors :class:`OllamaLLMClient`'s
#: default and accommodates Graph's tail-latency on large mailboxes
#: while still failing fast on a wedged connection.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`MS365Fetcher._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts is
#: the same budget the Phase 3 GitHub connector / Phase 7 plan §1 #8
#: rate-limit playbook uses (1s, 2s, 4s backoff between attempts).
_MAX_REQUEST_ATTEMPTS = 3

#: ``$top`` page size for paginated Graph queries. 50 balances per-call
#: latency against the round-trip count; the documented max for the
#: messages / events endpoints is 999, but values that high tend to
#: trip Graph's throttling layer before the response comes back.
_PAGE_SIZE = 50

#: Default since-cursor used on the very first sync. Graph's
#: ``$filter=... ge`` requires an ISO timestamp, and ``1970-01-01`` is
#: the conventional epoch sentinel — operators who want to truncate
#: their initial backfill can set the cursor manually via the
#: ``connector_cursors`` projection (Phase 7.x will add a CLI flag).
_EPOCH_ISO = "1970-01-01T00:00:00Z"

#: Outlook ``$select`` projection. Phase 10 (ADR-0020 Full Local Content
#: Retention) adds ``body`` so the mapper can retain the full message
#: body (``body.content``) alongside the ≤200-char ``bodyPreview``-derived
#: summary. Pinning the field list keeps the projection stable — any
#: future expansion stays visible in code review.
_OUTLOOK_SELECT = "id,subject,body,bodyPreview,sender,receivedDateTime,webLink"


@dataclass(frozen=True, slots=True)
class RawCalendarEvent:
    """Normalised view of a single calendar event.

    The B3 mapper consumes these tuples directly; ``.raw`` is preserved
    so the mapper can lift extra fields (e.g. ``location``) in future
    iterations without re-issuing the Graph call.
    """

    id: str
    subject: str
    start_iso: str
    end_iso: str
    attendees_count: int
    web_link: str
    last_modified_iso: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RawOneDriveItem:
    """Normalised view of a single OneDrive item.

    Deletions (items with a ``deleted`` facet) are filtered out in the
    fetcher and never reach the dataclass — Phase 7 MVP does not yet map
    them to a ``SourceDeleted`` event (Phase 7.x).
    """

    id: str
    name: str
    path: str
    web_url: str
    last_modified_iso: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RawOutlookMessage:
    """Normalised view of a single Outlook message.

    ``body_preview`` is whatever Graph returned in ``bodyPreview`` —
    Microsoft documents the field as being capped at ~255 chars, so the
    B3 mapper only needs to truncate to the project-wide 200-char limit
    (ADR-0005) rather than worrying about full message bodies leaking.
    """

    id: str
    subject: str
    body_preview: str
    sender: str
    received_iso: str
    web_link: str
    raw: dict[str, Any]


class MS365Fetcher:
    """Microsoft Graph fetcher for calendar / OneDrive / Outlook.

    Construction is intentionally lightweight so the connector wiring
    layer (B3) can hold one fetcher per sync run without paying a high
    setup cost. The ``httpx.Client`` is created here (rather than per
    call) so the connection pool is reused across all three endpoint
    groups within a single sync.

    The class is **not** thread-safe — Phase 7 syncs run sequentially
    inside ``opshub connector sync ms365`` (one connector at a time per
    process), so a per-call lock would be needless overhead.
    """

    def __init__(self, auth: MS365Auth) -> None:
        """Construct a fetcher bound to a configured ``MS365Auth`` helper.

        :param auth: An :class:`MS365Auth` whose
            :meth:`MS365Auth.get_access_token` returns a valid bearer
            token. The fetcher calls that method on every request so
            refresh-token rotation is observed automatically (B1
            stores the rotated value through :mod:`opshub.core.secrets`).

        :raises ConfigError: When the ``httpx`` extras are missing —
            same message shape as the auth module's MSAL guard so the
            operator gets one consistent install hint.
        """
        # Lazy import (ADR-0001 cold-start guard) — mirrors the Ollama
        # client / MS365 auth modules. The "import not at top" pattern is
        # intentional here: importing httpx from module scope would pay
        # the ``[connectors-ms365]`` install cost on every cold-start CLI
        # invocation, including the ``opshub --help`` path.
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "MS365 connector requires the [connectors-ms365] extras. "
                "Install with: uv sync --extra connectors-ms365"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)

    # ----- public API ------------------------------------------------------

    def fetch_calendar_events(
        self, *, since_iso: str | None
    ) -> Iterator[tuple[RawCalendarEvent, str]]:
        """Yield ``(event, new_cursor_iso)`` for events modified since the cursor.

        ``since_iso`` is the value stored under :data:`CURSOR_CALENDAR`
        in the ``connector_cursors`` projection (B3 wiring). ``None`` ->
        first sync; we substitute :data:`_EPOCH_ISO` so Graph's
        ``$filter`` expression remains well-formed.

        The ``new_cursor_iso`` value on each yield is monotonically
        non-decreasing — it advances to the maximum
        ``lastModifiedDateTime`` seen so far. The caller persists the
        cursor of the **last** successfully consumed event so a crash
        mid-iteration re-fetches only the unconsumed tail on the next
        sync (cursor idempotency per Phase 7 plan §3).
        """
        since = since_iso or _EPOCH_ISO
        # ``$filter`` + ``$orderby`` lets us advance the cursor to the
        # maximum seen modified time safely: Graph guarantees an
        # ordered stream so the last yield carries the correct cursor.
        params = {
            "$filter": f"lastModifiedDateTime ge {since}",
            "$orderby": "lastModifiedDateTime",
            "$top": str(_PAGE_SIZE),
        }
        url = f"{GRAPH_BASE}/me/calendar/events?{urlencode(params)}"
        max_modified = since
        for raw_event in self._paginate(url):
            event = _normalise_calendar_event(raw_event)
            if event.last_modified_iso > max_modified:
                max_modified = event.last_modified_iso
            yield event, max_modified

    def fetch_onedrive_changes(
        self, *, delta_link: str | None
    ) -> Iterator[tuple[RawOneDriveItem, str]]:
        """Yield ``(item, new_delta_link)`` using Graph's delta query.

        OneDrive uses ``/me/drive/root/delta`` which returns items
        changed since the last delta link. The first sync (``delta_link
        is None``) starts at the root; subsequent syncs replay the
        stored ``@odata.deltaLink`` URL verbatim.

        The new delta link arrives in the **final** page of the
        response as ``@odata.deltaLink``. We yield items as we walk
        the pages; until the final page is reached the cursor on each
        yield is the **incoming** ``delta_link`` (or the literal
        root URL on a first sync) so a mid-iteration crash does not
        advance the cursor past unconsumed items. On the final page we
        switch to the freshly-returned ``@odata.deltaLink``.

        Deletions (items carrying a ``deleted`` facet) are **skipped**
        in the Phase 7 MVP — they do not yield a tuple. Phase 7.x will
        map them to a ``SourceDeleted`` event (Phase 7 plan §1 #9
        deferral).
        """
        # ``cursor_in_flight`` is what the caller should persist for any
        # items yielded **before** we see the final ``@odata.deltaLink``.
        # On a first sync the safe-replay value is the root delta URL
        # itself, since re-issuing it just walks the world again and
        # Graph is idempotent on this endpoint.
        root_url = f"{GRAPH_BASE}/me/drive/root/delta"
        cursor_in_flight = delta_link or root_url
        url: str | None = delta_link or root_url

        while url is not None:
            body = self._request("GET", url)
            value_obj = body.get("value")
            if not isinstance(value_obj, list):
                raise ConnectorFailedError(
                    "MS365 OneDrive delta response is missing the 'value' list "
                    "(unexpected response shape)"
                )
            items = cast(list[dict[str, Any]], value_obj)

            next_link = body.get("@odata.nextLink")
            delta_link_out = body.get("@odata.deltaLink")
            # The final page is identified by the presence of
            # ``@odata.deltaLink`` and the absence of
            # ``@odata.nextLink``. When we are on that page the cursor
            # we hand out advances to the new delta link.
            page_cursor = (
                delta_link_out
                if isinstance(delta_link_out, str) and not isinstance(next_link, str)
                else cursor_in_flight
            )

            for raw_item in items:
                if _is_deleted_onedrive_item(raw_item):
                    # Phase 7 MVP skip — see docstring + plan §1 #9.
                    continue
                item = _normalise_onedrive_item(raw_item)
                yield item, page_cursor

            url = next_link if isinstance(next_link, str) else None

    def fetch_outlook_messages(
        self, *, since_iso: str | None
    ) -> Iterator[tuple[RawOutlookMessage, str]]:
        """Yield ``(message, new_cursor_iso)`` for messages since the cursor.

        Same monotonic-cursor semantics as :meth:`fetch_calendar_events`
        — see that method's docstring for the rationale.
        """
        since = since_iso or _EPOCH_ISO
        params = {
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime",
            "$top": str(_PAGE_SIZE),
            "$select": _OUTLOOK_SELECT,
        }
        url = f"{GRAPH_BASE}/me/messages?{urlencode(params)}"
        max_received = since
        for raw_message in self._paginate(url):
            message = _normalise_outlook_message(raw_message)
            if message.received_iso > max_received:
                max_received = message.received_iso
            yield message, max_received

    def close(self) -> None:
        """Release the underlying ``httpx.Client`` socket.

        Optional — the connection pool is GC-managed — but provided so
        a long-lived service process can clean up between sync runs.
        """
        self._client.close()

    # ----- internals -------------------------------------------------------

    def _paginate(self, url: str) -> Iterator[dict[str, Any]]:
        """Walk ``@odata.nextLink`` pages and yield each ``value`` item.

        Graph's pagination is link-based: every response that has more
        data carries an ``@odata.nextLink`` URL pointing at the next
        page. We follow those links verbatim because Microsoft documents
        them as opaque (they encode skip tokens / continuation state).
        """
        next_url: str | None = url
        while next_url is not None:
            body = self._request("GET", next_url)
            value_obj = body.get("value")
            if not isinstance(value_obj, list):
                raise ConnectorFailedError(
                    f"MS365 response from {next_url} is missing the 'value' list"
                )
            items = cast(list[dict[str, Any]], value_obj)
            yield from items
            link = body.get("@odata.nextLink")
            next_url = link if isinstance(link, str) else None

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Issue a Graph request with bearer auth, 401 retry, and 429 backoff.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **401** — happens when the access token expired between two
          requests of the same sync run. We force :class:`MS365Auth`
          to drop its cached token by setting ``_token = None`` and
          then call :meth:`MS365Auth.get_access_token` on the next
          loop turn, which re-issues an access token from the stored
          refresh token. Persistent 401 (refresh token also revoked)
          fails out as :class:`ConnectorFailedError`.
        * **429** — Graph's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **Other 4xx / 5xx** — fail-fast: :meth:`Response.raise_for_status`
          surfaces an :class:`httpx.HTTPStatusError` which we wrap into
          :class:`ConnectorFailedError` so the CLI driver always sees
          one error class.

        Tokens are NEVER logged or included in the raised message
        (ADR-0005). The error message identifies the offending HTTP
        verb / URL only.
        """
        last_status: int | None = None
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            token = self._auth.get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            extra_headers = kwargs.pop("headers", None)
            if extra_headers:
                headers.update(extra_headers)
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except self._httpx.HTTPError as exc:
                raise ConnectorFailedError(
                    f"MS365 request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 401 and attempt == 0:
                # Force the auth helper to discard its cached access
                # token; the next loop turn refreshes through
                # ``get_access_token`` and replays the request. We touch
                # the private ``_token`` attribute deliberately — there
                # is no public clear() helper, and growing the auth API
                # for a single caller would be premature. ``setattr``
                # routes around pyright's ``reportPrivateUsage`` check
                # while keeping the intent (cache invalidation) obvious.
                setattr(self._auth, "_token", None)  # noqa: B010
                continue
            if response.status_code == 429:
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if response.status_code >= 400:
                # Raise via httpx so we get a consistent error string,
                # but wrap into ConnectorFailedError immediately so the
                # CLI driver and tests can rely on one exception type.
                try:
                    response.raise_for_status()
                except self._httpx.HTTPStatusError as exc:
                    raise ConnectorFailedError(
                        f"MS365 request returned {response.status_code}: {method} {url}"
                    ) from exc

            # 2xx — parse the JSON body.
            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(f"MS365 response from {url} was not valid JSON") from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(f"MS365 response from {url} was not a JSON object")
            return cast(dict[str, Any], body)

        # Exhausted the retry budget — either persistent 401 or 429.
        raise ConnectorFailedError(
            f"MS365 request failed after {_MAX_REQUEST_ATTEMPTS} attempts: "
            f"{method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


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


def _get_dict(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``raw[key]`` when it is a dict, else an empty dict.

    Keeps the call sites in the normalisers concise and type-safe:
    every nested-object accessor turns into a single line without the
    ``isinstance`` ternary that confused pyright in the original draft.
    Returning a fresh dict on the miss path means downstream
    ``.get(...)`` calls cannot raise ``TypeError`` even on malformed
    Graph responses.
    """
    value = raw.get(key)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {}


def _get_list(raw: dict[str, Any], key: str) -> list[Any]:
    """Return ``raw[key]`` when it is a list, else an empty list.

    Same rationale as :func:`_get_dict` — keeps the normalisers free of
    inline ``isinstance`` plus ``cast`` patterns that pyright treats as
    only partially typed.
    """
    value = raw.get(key)
    if isinstance(value, list):
        # pyright requires the cast to make the inner element type
        # explicit (``list[Unknown]`` otherwise); mypy treats the cast
        # as redundant. The two checkers disagree, so we keep the cast
        # for pyright and suppress mypy's redundant-cast warning on
        # this one line.
        return cast(list[Any], value)  # type: ignore[redundant-cast]
    return []


def _normalise_calendar_event(raw: dict[str, Any]) -> RawCalendarEvent:
    """Lift the fields Phase 7 cares about out of a raw event payload.

    Graph nests start / end timestamps under
    ``{"start": {"dateTime": ..., "timeZone": ...}, ...}``. We keep the
    ISO datetime string verbatim and let the mapper format it; this
    avoids paying for ``datetime`` parsing on items the mapper might
    skip.
    """
    start_obj = _get_dict(raw, "start")
    end_obj = _get_dict(raw, "end")
    attendees_obj = _get_list(raw, "attendees")
    return RawCalendarEvent(
        id=str(raw.get("id", "")),
        subject=str(raw.get("subject", "")),
        start_iso=str(start_obj.get("dateTime", "")),
        end_iso=str(end_obj.get("dateTime", "")),
        attendees_count=len(attendees_obj),
        web_link=str(raw.get("webLink", "")),
        last_modified_iso=str(raw.get("lastModifiedDateTime", "")),
        raw=raw,
    )


def _is_deleted_onedrive_item(raw: dict[str, Any]) -> bool:
    """True iff the OneDrive delta item has a ``deleted`` facet.

    Graph's delta API surfaces deletions by attaching a ``deleted``
    object (with ``state: "deleted"``) instead of the usual file /
    folder facets. We skip these in Phase 7 MVP — see
    :meth:`MS365Fetcher.fetch_onedrive_changes` for the deferral
    rationale.
    """
    return raw.get("deleted") is not None


def _normalise_onedrive_item(raw: dict[str, Any]) -> RawOneDriveItem:
    """Lift the OneDrive fields Phase 7 cares about."""
    parent_ref = _get_dict(raw, "parentReference")
    parent_path = str(parent_ref.get("path", ""))
    name = str(raw.get("name", ""))
    # Graph's ``parentReference.path`` looks like ``/drive/root:/Folder/Sub``.
    # Joining ``parent_path`` + ``/`` + ``name`` reconstructs the
    # filesystem-style path the mapper exposes in the summary; empty
    # parent path (root items) falls back to a leading slash.
    path = f"{parent_path}/{name}" if parent_path else f"/{name}"
    return RawOneDriveItem(
        id=str(raw.get("id", "")),
        name=name,
        path=path,
        web_url=str(raw.get("webUrl", "")),
        last_modified_iso=str(raw.get("lastModifiedDateTime", "")),
        raw=raw,
    )


def _normalise_outlook_message(raw: dict[str, Any]) -> RawOutlookMessage:
    """Lift the Outlook fields Phase 7 cares about."""
    sender_obj = _get_dict(raw, "sender")
    sender_email = _get_dict(sender_obj, "emailAddress")
    sender_address = str(sender_email.get("address", ""))
    return RawOutlookMessage(
        id=str(raw.get("id", "")),
        subject=str(raw.get("subject", "")),
        body_preview=str(raw.get("bodyPreview", "")),
        sender=sender_address,
        received_iso=str(raw.get("receivedDateTime", "")),
        web_link=str(raw.get("webLink", "")),
        raw=raw,
    )
