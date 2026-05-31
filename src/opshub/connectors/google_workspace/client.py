"""Drive API v3 client + raw item shape (Phase 13 G3).

A thin ``httpx``-backed wrapper over Google Drive's v3 REST endpoints.
The wrapper covers exactly the Phase 13 MVP needs:

* ``changes.getStartPageToken`` — fresh page-token bootstrap (cursor
  initialisation + TTL-expiry fallback per ADR-0010 §Phase 13 改訂 (g)).
* ``changes.list`` — delta walk over file metadata; the connector
  consumes the iterator and persists cursors as items land.
* ``files.export`` — *deferred to G4*; the Phase 13 G3 PR ships
  metadata-only (``body=None`` on every mapped event). The export call
  signature is documented here so G4 can extend without re-shaping the
  module.

SDK choice (OQ8 — decided at G3 start per plan §8)
--------------------------------------------------

``httpx`` + manual OAuth + manual JSON, not ``google-api-python-client``.
Rationale, captured here so the next reader can re-confirm:

1. **Cold-start budget (M6).** ``google-api-python-client`` does
   service-discovery on import (a ~5 MB JSON download cached on first
   call, plus ``httplib2`` + ``oauth2client`` + ``protobuf`` deps that
   are heavy by themselves). Maintaining the ADR-0001 ≤ 300 ms
   ``opshub --help`` budget under it would require gymnastics
   (sub-module lazy imports, discovery cache pre-warming) that
   ``httpx`` simply does not need.
2. **Sibling connectors.** Every prior connector (Phase 7 MS365 +
   Box, Phase 11 Teams) already uses ``httpx`` for the Graph / Drive /
   Box REST surface. Adding a second HTTP client shape would split
   the project's retry / pagination / error-mapping idioms in half.
3. **Auth surface.** The OAuth refresh-token flow Google requires
   (``access_type=offline`` + paste-code + refresh + rotation) is the
   MS365 / Box pattern. Re-implementing it on top of
   ``google-api-python-client`` would still need ``oauth2client`` /
   ``google-auth`` (the SDK's bundled auth helpers do roughly the
   same thing), so the cost is the same either way.
4. **Test ergonomics.** ``httpx.MockTransport`` is the project's
   standard mock seam (Teams / MS365 fetchers, Ollama LLM client all
   exercise it). The mock surface for ``google-api-python-client``
   is more involved (build a discovery doc fixture, intercept the
   batch HTTP layer) and would not reuse the existing fixture
   ergonomics.

Phase 13 plan §1 OQ8 + ADR-0010 §Phase 13 改訂 reference this decision.

Retry / rate-limit
------------------

Drive's documented throttling envelope is HTTP 403 ``rateLimitExceeded``
/ ``userRateLimitExceeded`` or HTTP 429 ``Too Many Requests``. We
honour ``Retry-After`` directly when present and otherwise back off
exponentially (1 s / 2 s / 4 s) for up to three attempts per request,
matching Phase 7 MS365 + Phase 11 Teams precedent. 5xx server errors
get the same backoff (Google documents them as transient). Persistent
failure escalates to :class:`~opshub.core.errors.ConnectorFailedError`.

Cursor invalidation
-------------------

Drive returns HTTP 404 with reason ``notFound`` (or 410 ``Gone`` on
some Google data-centre paths) when the stored ``startPageToken`` has
expired past the ~30-day vendor TTL. The client surfaces these to the
connector via a sentinel :class:`PageTokenExpiredError` so the
connector layer can bootstrap a fresh token via
``changes.getStartPageToken`` and resume — mirrors the Teams
``_DeltaLinkExpiredError`` control flow.

Shared Drives (OQ10)
--------------------

The Phase 13 MVP **includes** Shared Drives ("Team Drives") so the
secretary can see business-shared content. Drive requires two query
flags for Shared Drives to participate in ``changes.list``:
``supportsAllDrives=true`` and ``includeItemsFromAllDrives=true``.
We pin both in :data:`_CHANGES_LIST_PARAMS` so a future regression
that quietly drops them surfaces in tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.google_workspace.auth import GoogleWorkspaceAuth


__all__ = [
    "DRIVE_API_BASE",
    "DriveClient",
    "PageTokenExpiredError",
    "RawDriveItem",
]


#: Google Drive API v3 base URL. The v3 surface is the GA endpoint;
#: v2 is documented as deprecated and the connector deliberately stays
#: off it.
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

#: HTTP timeout for Drive calls. 30 s mirrors :class:`MS365Fetcher` /
#: :class:`TeamsFetcher` and accommodates Drive's tail-latency on the
#: larger ``changes.list`` pages without wedging.
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum number of attempts before :meth:`DriveClient._request`
#: gives up and raises :class:`ConnectorFailedError`. Three attempts
#: matches the Phase 7 MS365 + Phase 11 Teams retry budget.
_MAX_REQUEST_ATTEMPTS = 3

#: ``pageSize`` for ``changes.list`` pagination. 100 mirrors Drive's
#: documented sweet spot — small enough that 429s do not cost much
#: re-work, large enough that paging traffic stays low. Drive caps the
#: max at 1000 but values that high tend to trip the throttling layer
#: before the response comes back (same pattern Teams + MS365 exhibit
#: at 999).
_PAGE_SIZE = 100

#: ``$fields`` selector that pins the metadata columns Phase 13's
#: mapper actually reads. Adding fields here without updating the
#: mapper is a no-op; *removing* fields here without updating the
#: mapper would surface as :class:`KeyError` downstream so the explicit
#: pin keeps the contract bidirectional.
#:
#: Drive's ``changes.list`` returns each change as
#: ``{"file": {...}, "fileId": ..., "removed": bool, "time": ISO}`` so
#: the selector nests ``file/...`` to address file metadata fields.
_CHANGES_LIST_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,time,driveId,changeType,"
    "file(id,name,mimeType,modifiedTime,createdTime,webViewLink,"
    "iconLink,trashed,explicitlyTrashed,owners(emailAddress,displayName),"
    "lastModifyingUser(emailAddress,displayName),"
    "shared,sharedWithMeTime,size,parents,driveId))"
)

#: Pinned parameters for every ``changes.list`` call. Shared Drives
#: participation (OQ10) requires both
#: ``supportsAllDrives`` and ``includeItemsFromAllDrives``;
#: ``includeRemoved=true`` so Google's permanent-delete events surface
#: as ``removed=true`` (the connector retains them per ADR-0020
#: Full Local Content Retention).
_CHANGES_LIST_PARAMS: dict[str, str] = {
    "supportsAllDrives": "true",
    "includeItemsFromAllDrives": "true",
    "includeRemoved": "true",
    # ``allDrives`` (vs ``user``) widens the scope to My Drive + every
    # Shared Drive the user can see. ``user`` would only walk My Drive.
    "spaces": "drive",
    "fields": _CHANGES_LIST_FIELDS,
    "pageSize": str(_PAGE_SIZE),
}

#: ``$fields`` selector for ``files.list`` (TTL fallback full-pass).
#: Mirrors the ``file(...)`` projection inside
#: :data:`_CHANGES_LIST_FIELDS` so the same :class:`RawDriveItem`
#: shape can be lifted by :func:`_normalise_file` without re-wiring the
#: mapper. ``files.list`` returns the file objects directly (not nested
#: under ``changes(...)``) so the top-level shape is ``files(...)``.
_FILES_LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,modifiedTime,createdTime,webViewLink,"
    "iconLink,trashed,explicitlyTrashed,owners(emailAddress,displayName),"
    "lastModifyingUser(emailAddress,displayName),"
    "shared,sharedWithMeTime,size,parents,driveId)"
)

#: Pinned parameters for every ``files.list`` call used by the TTL
#: fallback full-pass. Same Shared Drives flags as
#: :data:`_CHANGES_LIST_PARAMS` so the recovery path covers the same
#: corpus as the steady-state delta path (ADR-0010 §Phase 13 改訂 (g)
#: full-pass = `files.list?modifiedTime>='...'&supportsAllDrives=true&
#: includeItemsFromAllDrives=true&spaces=drive` per Teams 同型).
_FILES_LIST_PARAMS_BASE: dict[str, str] = {
    "supportsAllDrives": "true",
    "includeItemsFromAllDrives": "true",
    "corpora": "allDrives",
    "spaces": "drive",
    "fields": _FILES_LIST_FIELDS,
    "pageSize": str(_PAGE_SIZE),
}


@dataclass(frozen=True, slots=True)
class RawDriveItem:
    """Normalised view of a single Drive change / file metadata.

    Attributes
    ----------
    file_id:
        The Drive file id (Google's stable opaque identifier). Pairs
        with the connector name to form the natural key the projection
        upserts on.
    removed:
        ``True`` iff Google reported the change as a permanent delete
        or a loss of visibility (ADR-0020 retains both as
        ``archived``-equivalent rather than emitting a SourceDeleted).
    trashed:
        ``True`` iff the file lives in the Drive trash. Retained per
        ADR-0020 §全保持; the mapper stamps a marker in the summary so
        downstream consumers can detect trashed items.
    name:
        Human-readable file name (Drive ``name`` field).
    mime_type:
        Google mimeType (e.g. ``application/vnd.google-apps.document``).
        Used by the mapper to pick a source_type and (in G4) to drive
        the ``files.export`` selection.
    modified_time_iso:
        ISO 8601 UTC timestamp from Drive ``modifiedTime``. Used as
        ``occurred_at`` for ``SourceObserved``.
    web_view_link:
        Stable URL to surface in ``sources.url`` (Drive ``webViewLink``).
        May be ``""`` for some change types (drive-level events,
        permanently-deleted files).
    owner_email:
        ``owners[0].emailAddress``. Used by the mapper for the
        ``actor``/``sender`` fields. ``""`` when Google did not return
        an owner (e.g. shared drives may not expose owner identity).
    owner_display_name:
        ``owners[0].displayName``. ``""`` when absent.
    is_shared_with_me:
        Whether the operator received this file via "Shared with me"
        rather than owning it. Surfaced in the summary so the secretary
        can distinguish own-content from received-content.
    shared:
        Whether the file is shared at all (``True`` if Drive's
        ``shared`` boolean is set, including outbound shares the
        operator owns). G4 (#278) surfaces this in the summary so
        downstream consumers can answer "is this private to me?".
    last_modifying_user_email:
        ``lastModifyingUser.emailAddress``. Empty when Drive does not
        return a last-modifying user (anonymous edits, system updates).
        G4 (#278) surfaces this so the secretary can attribute "who
        touched this last" without re-reading ``raw``.
    last_modifying_user_display_name:
        ``lastModifyingUser.displayName``. Empty when absent.
    drive_id:
        Drive id (``""`` for My Drive; populated for Shared Drives).
    raw:
        Verbatim ``changes.list`` payload, kept for forensic debugging
        (mapper fixtures, future backfill). The mapper does not
        persist this.
    """

    file_id: str
    removed: bool
    trashed: bool
    name: str
    mime_type: str
    modified_time_iso: str
    web_view_link: str
    owner_email: str
    owner_display_name: str
    is_shared_with_me: bool
    shared: bool
    last_modifying_user_email: str
    last_modifying_user_display_name: str
    drive_id: str
    raw: dict[str, Any]


class PageTokenExpiredError(Exception):
    """Internal signal: Drive returned 404/410 for the stored page token.

    Caught by :meth:`DriveClient.fetch_changes` callers so the
    connector layer can bootstrap a fresh ``startPageToken`` via
    :meth:`DriveClient.get_start_page_token` and resume. Never surfaced
    to upstream callers — the connector either completes via fallback
    or raises :class:`ConnectorFailedError` from inside the fallback
    path. Mirrors :class:`opshub.connectors.teams.fetcher._DeltaLinkExpiredError`.
    """


class DriveClient:
    """Drive API v3 client (``changes.list`` + ``changes.getStartPageToken``).

    Construction is intentionally lightweight so the connector wiring
    layer can hold one client per sync run without paying a high
    setup cost. The ``httpx.Client`` is created here (rather than per
    call) so the connection pool is reused across pages.

    The class is **not** thread-safe — Phase 13 syncs run sequentially
    inside ``opshub connector sync google_workspace`` (one connector
    at a time per process), so a per-call lock would be needless
    overhead.
    """

    def __init__(self, auth: GoogleWorkspaceAuth) -> None:
        """Construct a client bound to a configured :class:`GoogleWorkspaceAuth`.

        :param auth: An auth helper whose
            :meth:`GoogleWorkspaceAuth.get_access_token` returns a
            valid Drive bearer. The client calls that method on every
            request so refresh-token rotation is observed
            automatically (auth persists the rotated value through
            :mod:`opshub.core.secrets`).

        :raises ConfigError: When the ``[connectors-google-workspace]``
            extras are missing — same message shape as the auth
            module's ``httpx`` guard so the operator gets one
            consistent install hint.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Google Workspace connector requires the "
                "[connectors-google-workspace] extras. "
                "Install with: uv sync --extra connectors-google-workspace"
            ) from exc

        self._auth = auth
        # Keep the module on the instance so the request loop can refer
        # to ``httpx.HTTPError`` without re-importing on the hot path.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)

    # ----- public API ------------------------------------------------------

    def get_start_page_token(self) -> str:
        """Return a fresh root ``startPageToken``.

        Drive ``changes.getStartPageToken`` is the documented bootstrap
        endpoint for ``changes.list``. We pass the Shared Drives
        flags so the issued token covers every drive the user can see
        (matching :data:`_CHANGES_LIST_PARAMS`).

        Raises :class:`ConnectorFailedError` on any non-2xx or transport
        failure. Tokens never appear in the raised message.
        """
        body = self._request(
            "GET",
            f"{DRIVE_API_BASE}/changes/startPageToken",
            params={"supportsAllDrives": "true"},
        )
        token_obj = body.get("startPageToken")
        if not isinstance(token_obj, str) or not token_obj:
            raise ConnectorFailedError(
                "Google Workspace getStartPageToken returned no startPageToken "
                "(unexpected response shape)"
            )
        return token_obj

    def fetch_changes(self, *, page_token: str) -> Iterator[tuple[RawDriveItem, str]]:
        """Yield ``(item, cursor)`` for every change since ``page_token``.

        Walks Drive's ``changes.list`` endpoint forward across paginated
        responses; each page yields its items in Drive's natural order
        (Google does not guarantee ordering across pages but within a
        page the order is the SourceObserved-friendly time order).

        Cursor semantics (mirrors Teams + MS365 OneDrive delta walks):

        * Until the final page is reached, the yielded cursor is the
          **incoming** ``page_token`` so a mid-iteration crash does
          not advance the cursor past unconsumed items.
        * On the final page (the page that returns
          ``newStartPageToken`` and no ``nextPageToken``) the yielded
          cursor is the fresh ``newStartPageToken`` — the caller
          persists it and the next sync resumes there.

        Raises
        ------
        PageTokenExpiredError
            Drive rejected ``page_token`` as expired (HTTP 404
            ``notFound`` or HTTP 410 ``Gone``). The connector layer
            traps this, bootstraps a fresh token via
            :meth:`get_start_page_token`, and resumes.
        ConnectorFailedError
            On any other transport / API failure, or when the retry
            budget is exhausted. Tokens never appear in the error
            message.
        """
        url = f"{DRIVE_API_BASE}/changes"
        params: dict[str, str] = dict(_CHANGES_LIST_PARAMS)
        params["pageToken"] = page_token
        cursor_in_flight = page_token

        while True:
            # Drive does not echo the ``$fields`` / ``supportsAllDrives``
            # flags forward across pages so we keep ``params`` populated
            # on every call (only the ``pageToken`` value changes when
            # advancing to the next page). Skipping the params on
            # subsequent calls would silently drop the Shared Drives
            # flags and break OQ10 coverage.
            body = self._request("GET", url, params=params)
            changes_obj = body.get("changes")
            if not isinstance(changes_obj, list):
                raise ConnectorFailedError(
                    "Google Workspace changes.list response is missing the "
                    "'changes' list (unexpected response shape)"
                )
            changes = cast(list[dict[str, Any]], changes_obj)

            next_page_token = body.get("nextPageToken")
            new_start_page_token = body.get("newStartPageToken")
            # The final page is identified by the presence of
            # ``newStartPageToken`` and the absence of
            # ``nextPageToken``. When we are on that page the cursor
            # we hand out advances to the new start page token.
            page_cursor = (
                new_start_page_token
                if isinstance(new_start_page_token, str)
                and new_start_page_token
                and not isinstance(next_page_token, str)
                else cursor_in_flight
            )

            for raw_change in changes:
                item = _normalise_change(raw_change)
                if item is None:
                    continue
                yield item, page_cursor

            # Advance to the next page when Drive supplies one;
            # otherwise the loop exits and the caller persists
            # ``new_start_page_token`` (the value we already handed
            # out on the final yield above).
            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
                cursor_in_flight = page_cursor
            else:
                return

    def list_files_modified_since(
        self,
        *,
        since: str,
        page_size: int = _PAGE_SIZE,
    ) -> Iterator[RawDriveItem]:
        """Yield :class:`RawDriveItem` for every file modified at or after ``since``.

        Used by the connector's ADR-0010 §Phase 13 改訂 (g) TTL
        fallback path: when the stored ``startPageToken`` is rejected
        as expired, the connector walks ``files.list?q=modifiedTime
        >= '<since>'`` over the configured ``fallback_window_days``
        window so changes that happened during the TTL gap surface as
        :class:`SourceObserved` events. The projection's natural-key
        dedup on ``(connector_name, external_id)`` absorbs the
        steady-state overlap.

        Parameters
        ----------
        since:
            ISO 8601 / RFC 3339 UTC timestamp (e.g.
            ``"2026-04-30T00:00:00Z"``) used verbatim inside Drive's
            ``q=modifiedTime >= '...'`` selector. The caller computes
            this from ``now - fallback_window_days``.
        page_size:
            ``pageSize`` for the underlying ``files.list`` call.
            Defaults to :data:`_PAGE_SIZE` (100) — Drive's documented
            sweet spot, identical to the steady-state delta path.

        Yields
        ------
        RawDriveItem
            One per matched file. No cursor is yielded alongside the
            item because the caller is in fallback mode and does not
            persist an intermediate cursor (the in-flight
            ``startPageToken`` was already replaced by the freshly-
            bootstrapped one before the full-pass begins per
            ADR-0010 §Phase 13 改訂 (g) step 3 ordering).

        Raises
        ------
        ConnectorFailedError
            On any non-recoverable transport / API failure or when the
            retry budget is exhausted. The exception bubbles up to the
            connector's :class:`PageTokenExpiredError` handler which
            re-raises it as a hard sync failure (the CLI driver then
            appends ``ConnectorSyncFailed`` per ADR-0010 §責務 4).
            Drive's 429 / 5xx / rate-limit backoff is shared with the
            steady-state ``changes.list`` path through
            :meth:`_request`.
        """
        url = f"{DRIVE_API_BASE}/files"
        params: dict[str, str] = dict(_FILES_LIST_PARAMS_BASE)
        # Drive's ``q`` selector requires single-quoted RFC 3339
        # timestamps. ``modifiedTime`` is the documented field for
        # "last metadata or content modification" which is the same
        # semantics the steady-state ``changes.list`` delta surfaces.
        params["q"] = f"modifiedTime >= '{since}'"
        params["pageSize"] = str(page_size)

        while True:
            body = self._request("GET", url, params=params)
            files_obj = body.get("files")
            if not isinstance(files_obj, list):
                raise ConnectorFailedError(
                    "Google Workspace files.list response is missing the "
                    "'files' list (unexpected response shape)"
                )
            files = cast(list[dict[str, Any]], files_obj)

            for raw_file in files:
                item = _normalise_file(raw_file)
                if item is None:
                    continue
                yield item

            next_page_token = body.get("nextPageToken")
            if isinstance(next_page_token, str) and next_page_token:
                params["pageToken"] = next_page_token
            else:
                return

    def export_file(self, *, file_id: str, mime_type: str) -> bytes:
        """Fetch ``files.export(fileId, mimeType=<MS Office mediatype>)``.

        Returns the raw bytes of the exported MS Office document
        (``.docx`` / ``.xlsx`` / ``.pptx``) so the caller can hand
        them to :func:`opshub.core.document_extract.extract_workspace_export`.
        Only Google Workspace native types
        (``application/vnd.google-apps.document``,
        ``application/vnd.google-apps.spreadsheet``,
        ``application/vnd.google-apps.presentation``) have an
        ``export`` representation; non-native files (PDFs, uploads,
        folders) MUST be filtered by the caller — Drive returns 403
        ``fileNotExportable`` for those, which surfaces here as
        :class:`ConnectorFailedError`.

        Parameters
        ----------
        file_id:
            Drive file id (``raw.file_id``). The id is opaque; the
            method does not validate it beyond non-emptiness.
        mime_type:
            Export target mediatype (e.g.
            ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``).
            G4 (#278) connector wiring picks the target from
            :data:`opshub.core.document_extract.GoogleWorkspaceSourceType`
            so the choice stays in lockstep with
            :func:`extract_workspace_export`'s tempfile-suffix lookup.

        Returns
        -------
        bytes
            Raw response body. May be empty for a legitimately empty
            Doc; :func:`extract_workspace_export` short-circuits on
            ``b""``.

        Raises
        ------
        ConnectorFailedError
            On non-2xx (other than the retried 429/5xx) or transport
            failure. Tokens never appear in the raised message.
        PageTokenExpiredError
            Not raised here — ``files.export`` does not consume the
            ``startPageToken``. The exception type is mentioned so a
            future refactor that shares the request helper does not
            silently introduce the wrong fallback path.
        """
        if not file_id:
            raise ConnectorFailedError(
                "Google Workspace files.export was called with an empty file_id"
            )
        url = f"{DRIVE_API_BASE}/files/{file_id}/export"
        return self._request_bytes("GET", url, params={"mimeType": mime_type})

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
        """Issue a Drive request with bearer auth + 429 backoff + page-token-expired detection.

        Retry budget: up to :data:`_MAX_REQUEST_ATTEMPTS` attempts.

        * **404 / 410** — page-token expired (ADR-0010 §Phase 13
          改訂 (g)). Raised as :class:`PageTokenExpiredError` so the
          iterator can switch to the fallback path; not retried inline.
        * **429** — Drive's standard rate-limit. We sleep for
          ``Retry-After`` seconds (header), falling back to
          ``2 ** attempt`` when the header is missing or unparseable.
        * **403 with ``rateLimitExceeded`` / ``userRateLimitExceeded``** —
          Drive's documented user-quota signal, same handling as 429.
        * **5xx** — Drive documents these as transient; same backoff.
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
                    f"Google Workspace request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code in (404, 410):
                # Page token expired. Not retryable here: the connector
                # must restart in fallback mode after bootstrapping a
                # fresh token via :meth:`get_start_page_token`.
                raise PageTokenExpiredError
            if response.status_code == 429 or (
                response.status_code == 403 and _is_rate_limit_error(response)
            ):
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                # 5xx: Drive documents these as transient; back off.
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise ConnectorFailedError(
                    f"Google Workspace request returned {response.status_code}: {method} {url}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ConnectorFailedError(
                    f"Google Workspace response from {url} was not valid JSON"
                ) from exc
            if not isinstance(body, dict):
                raise ConnectorFailedError(
                    f"Google Workspace response from {url} was not a JSON object"
                )
            return cast(dict[str, Any], body)

        raise ConnectorFailedError(
            f"Google Workspace request failed after {_MAX_REQUEST_ATTEMPTS} "
            f"attempts: {method} {url} (last status {last_status})"
        )

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> bytes:
        """Same retry / sanitise contract as :meth:`_request` but returns raw bytes.

        Used by :meth:`export_file` because ``files.export`` returns
        the Office document body, not JSON. The retry budget, 429 /
        5xx backoff and "tokens never logged" invariants are identical
        — the only delta is that the success path returns
        ``response.content`` instead of ``response.json()``.

        Page-token-expired handling is omitted: ``files.export`` does
        not consume the ``startPageToken``, so a 404 / 410 here would
        mean the file id is unknown (the operator-visible state of
        Drive moved out from under us between ``changes.list`` and the
        export), which is a hard failure, not a cursor-fallback
        opportunity. We surface it as :class:`ConnectorFailedError`
        with the status code so the connector's fail-safe outer
        envelope logs and moves on.
        """
        last_status: int | None = None
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            headers = {
                "Authorization": f"Bearer {self._auth.get_access_token()}",
                "User-Agent": "opshub-connector/0.1",
            }
            try:
                response = self._client.request(method, url, headers=headers, params=params)
            except self._httpx.HTTPError as exc:
                raise ConnectorFailedError(
                    f"Google Workspace request failed: {method} {url} ({type(exc).__name__})"
                ) from exc

            last_status = response.status_code

            if response.status_code == 429 or (
                response.status_code == 403 and _is_rate_limit_error(response)
            ):
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), fallback=2**attempt
                )
                time.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                # Includes 403 ``fileNotExportable`` (caller routed a
                # non-Workspace file through the export path), 404
                # (file moved / deleted between changes.list and the
                # export call) and 410 (Drive's permanent-delete shape
                # for some routes). All three are fail-fast for the
                # caller's outer envelope, which logs and continues.
                raise ConnectorFailedError(
                    f"Google Workspace request returned {response.status_code}: {method} {url}"
                )

            return cast(bytes, response.content)

        raise ConnectorFailedError(
            f"Google Workspace request failed after {_MAX_REQUEST_ATTEMPTS} "
            f"attempts: {method} {url} (last status {last_status})"
        )


# ----- helpers -------------------------------------------------------------


def _is_rate_limit_error(response: Any) -> bool:
    """True iff a Drive 403 body carries a rate-limit reason code.

    Drive returns ``userRateLimitExceeded`` / ``rateLimitExceeded`` in
    the JSON body's ``error.errors[].reason`` field on 403s that are
    really rate limits rather than scope / permission denials. We must
    distinguish: scope denials would not benefit from backoff and would
    just retry-then-fail more loudly.
    """
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    # ``response.json()`` returns ``Any``; narrow each nested layer
    # before reading. The explicit casts let pyright (strict) follow
    # the dict shape without leaking ``Unknown`` types into the loop
    # body — same pattern :mod:`opshub.connectors.ms365.fetcher` uses
    # for its Graph payloads.
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

    Drive documents the header as an integer number of seconds; we
    still defend against the HTTP-date variant by falling back rather
    than raising — a connector that hot-loops because the server
    returned an exotic header would be worse than one that waits an
    extra few seconds (same defensive shape Teams uses).
    """
    if header_value is None:
        return fallback
    try:
        return int(header_value)
    except (TypeError, ValueError):
        return fallback


def _normalise_change(raw: dict[str, Any]) -> RawDriveItem | None:
    """Lift a Drive ``change`` payload into :class:`RawDriveItem`.

    Returns ``None`` for change records that do not pin a fileId — a
    handful of drive-level events (Shared Drive renames, permission
    changes) carry only a ``driveId`` and have no file metadata we can
    map to ``SourceObserved``. Surfacing them as observed sources
    would only add noise.

    The function tolerates Drive's nested-object shape: when ``file``
    is missing (the ``removed=true`` permanent-delete case) we still
    return a :class:`RawDriveItem` with the ``file_id`` from the
    top-level field so the projection can mark it ``removed``.
    """
    file_id_obj = raw.get("fileId")
    if not isinstance(file_id_obj, str) or not file_id_obj:
        return None
    removed = bool(raw.get("removed", False))
    drive_id = str(raw.get("driveId") or "")
    file_obj = raw.get("file")
    file_dict: dict[str, Any]
    if isinstance(file_obj, dict):
        file_dict = cast(dict[str, Any], file_obj)
    else:
        file_dict = {}

    owners_obj = file_dict.get("owners")
    owner_email = ""
    owner_display_name = ""
    if isinstance(owners_obj, list) and owners_obj:
        # Same cast shape as ``_is_rate_limit_error`` — Drive responses
        # arrive as ``dict[str, Any]`` so the inner element type needs
        # an explicit cast for pyright (strict) to follow.
        owners_list = cast(list[Any], owners_obj)  # type: ignore[redundant-cast]
        first = owners_list[0]
        if isinstance(first, dict):
            first_dict = cast(dict[str, Any], first)
            owner_email = str(first_dict.get("emailAddress") or "")
            owner_display_name = str(first_dict.get("displayName") or "")

    # G4 (#278): pull the ``lastModifyingUser`` block; Drive omits it
    # for anonymous/system edits (Workspace bot writes, drive-level
    # change events) so the defensive ``isinstance(..., dict)`` check
    # is load-bearing — surfacing ``""`` is the right behaviour for
    # the mapper's metadata summary path.
    last_user_obj = file_dict.get("lastModifyingUser")
    last_user_email = ""
    last_user_display_name = ""
    if isinstance(last_user_obj, dict):
        last_user_dict = cast(dict[str, Any], last_user_obj)
        last_user_email = str(last_user_dict.get("emailAddress") or "")
        last_user_display_name = str(last_user_dict.get("displayName") or "")

    return RawDriveItem(
        file_id=file_id_obj,
        removed=removed,
        trashed=bool(file_dict.get("trashed", False)),
        name=str(file_dict.get("name") or ""),
        mime_type=str(file_dict.get("mimeType") or ""),
        modified_time_iso=str(file_dict.get("modifiedTime") or raw.get("time") or ""),
        web_view_link=str(file_dict.get("webViewLink") or ""),
        owner_email=owner_email,
        owner_display_name=owner_display_name,
        is_shared_with_me=bool(file_dict.get("sharedWithMeTime")),
        # G4 (#278): ``shared`` is Drive's boolean for "this file has
        # at least one non-owner with access" — distinct from
        # ``sharedWithMeTime`` (which fires only when the operator is
        # on the receiving end). Both ``False`` means the file is
        # private to the operator (or to the owning Shared Drive).
        shared=bool(file_dict.get("shared", False)),
        last_modifying_user_email=last_user_email,
        last_modifying_user_display_name=last_user_display_name,
        drive_id=drive_id or str(file_dict.get("driveId") or ""),
        raw=raw,
    )


def _normalise_file(raw: dict[str, Any]) -> RawDriveItem | None:
    """Lift a Drive ``files.list`` file payload into :class:`RawDriveItem`.

    Used by the TTL fallback full-pass (ADR-0010 §Phase 13 改訂 (g))
    which walks ``files.list?q=modifiedTime>='...'`` directly. The
    ``files.list`` response wraps each file at the top level rather
    than inside a ``change`` envelope, so this helper extracts file
    metadata without the surrounding ``fileId`` / ``removed`` / ``time``
    fields that :func:`_normalise_change` reads from the change record.

    Returns ``None`` only when the file lacks an ``id`` — Drive
    documents this as an impossible case for ``files.list`` results
    but we defend against it to keep parity with :func:`_normalise_change`.

    ``removed`` is always ``False`` in the lift result: ``files.list``
    cannot return permanently-deleted files (they are gone from the
    user's drive), so the TTL fallback only re-surfaces present files.
    Permanent-delete events that occurred during the TTL gap are lost
    by design — there is no Drive API endpoint that returns them
    retroactively. The steady-state ``changes.list`` delta walk resumes
    immediately after the fallback (step 3 of the 3-step recovery), so
    going-forward permanent-deletes resume their normal flow.
    """
    file_id_obj = raw.get("id")
    if not isinstance(file_id_obj, str) or not file_id_obj:
        return None

    owners_obj = raw.get("owners")
    owner_email = ""
    owner_display_name = ""
    if isinstance(owners_obj, list) and owners_obj:
        owners_list = cast(list[Any], owners_obj)  # type: ignore[redundant-cast]
        first = owners_list[0]
        if isinstance(first, dict):
            first_dict = cast(dict[str, Any], first)
            owner_email = str(first_dict.get("emailAddress") or "")
            owner_display_name = str(first_dict.get("displayName") or "")

    last_user_obj = raw.get("lastModifyingUser")
    last_user_email = ""
    last_user_display_name = ""
    if isinstance(last_user_obj, dict):
        last_user_dict = cast(dict[str, Any], last_user_obj)
        last_user_email = str(last_user_dict.get("emailAddress") or "")
        last_user_display_name = str(last_user_dict.get("displayName") or "")

    return RawDriveItem(
        file_id=file_id_obj,
        # ``files.list`` cannot return permanently-deleted files; the
        # fallback re-emits present files only. ADR-0010 §Phase 13
        # 改訂 (g) acknowledges the permanent-delete gap as the cost
        # of the recovery path.
        removed=False,
        trashed=bool(raw.get("trashed", False)),
        name=str(raw.get("name") or ""),
        mime_type=str(raw.get("mimeType") or ""),
        modified_time_iso=str(raw.get("modifiedTime") or ""),
        web_view_link=str(raw.get("webViewLink") or ""),
        owner_email=owner_email,
        owner_display_name=owner_display_name,
        is_shared_with_me=bool(raw.get("sharedWithMeTime")),
        shared=bool(raw.get("shared", False)),
        last_modifying_user_email=last_user_email,
        last_modifying_user_display_name=last_user_display_name,
        drive_id=str(raw.get("driveId") or ""),
        raw=raw,
    )
