"""Box Events fetcher (Phase 7 step C2).

Uses the Box Events API (``GET /events?stream_position=<pos>&stream_type=all``)
for incremental sync. ``stream_position`` is Box's monotonic cursor —
each event has a position and the API returns events with position
*greater than* the supplied value. The next call passes the result's
``next_stream_position`` to advance the window.

The cursor is persisted by the caller under :data:`CURSOR_BOX_STREAM_POSITION`
in the ``connector_cursors`` projection. On the first sync (no
cursor) the fetcher requests the **current** stream position
(``stream_position="now"``) and yields nothing — Phase 7 MVP does
not backfill historical events. Operators who want history can run
multiple syncs to catch up; that decision is documented in
``docs/phase-7-plan.md`` §2.3 C2.

Auth: bearer token sourced from :class:`BoxAuth` (auto-refreshed via
the rotating-refresh-token ``store_tokens`` callback registered in
:mod:`opshub.connectors.box.auth`). On a Box 401 we drop the cached
access token (forcing a refresh on the next attempt) and retry once;
on subsequent 401 we raise :class:`ConnectorFailedError` because the
refresh-token rotation has failed downstream and the operator must
re-auth.

Rate limit (HTTP 429): respect the ``Retry-After`` header when
present and otherwise back off ``1s / 2s / 4s`` (max 3 attempts) —
matches the strategy Phase 3 GitHub used and Phase 7 plan §1 #8
mandates.

Event filtering: Box's ``stream_type=all`` returns ~20 event types
(login, comment, item_*, …). Phase 7 MVP only ingests **file /
folder events** (``ITEM_*``). User / admin events are out of scope
for now and will be added in Phase 7.x once we have a concrete need
— this keeps the projection focussed on artefacts the agent can
actually navigate from.

This module is lazy-imported by the connector wiring; the top-level
``import opshub.connectors.box.fetcher`` does NOT pull in ``boxsdk``
itself (Cold-start guard, Phase 7 plan §1.1). The SDK is only loaded
the first time :class:`BoxFetcher` is instantiated, at which point
the optional extras must be installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opshub.connectors.box.auth import BoxAuth


__all__ = [
    "CURSOR_BOX_STREAM_POSITION",
    "ITEM_EVENT_TYPES",
    "BoxFetcher",
    "RawBoxEvent",
]


#: Cursor key under which the Box Events stream_position is persisted
#: in the ``connector_cursors`` projection. Public constant so the
#: connector wiring (C3) and tests can reference one source of truth.
CURSOR_BOX_STREAM_POSITION = "box:stream_position"


#: Set of Box event types that represent file / folder activity.
#:
#: Phase 7 MVP scope: only these events become :class:`RawBoxEvent`
#: yields. The remaining ~15 Box event types (LOGIN, COMMENT_CREATE,
#: GROUP_ADD_USER, …) are returned by the API on ``stream_type=all``
#: but skipped here. Phase 7.x can widen the filter once we add user
#: / admin-event mappers.
ITEM_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ITEM_CREATE",
        "ITEM_RENAME",
        "ITEM_MODIFY",
        "ITEM_MOVE",
        "ITEM_COPY",
        "ITEM_TRASH",
        "ITEM_UNDELETE_VIA_TRASH",
    }
)

# Per-attempt backoff (seconds) when a 429 response does not carry a
# ``Retry-After`` header. Mirrors Phase 7 plan §1 #8.
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Page size requested from the events endpoint. Box defaults to 100;
# we make it explicit so a future plan tweak shows up in code review.
_DEFAULT_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class RawBoxEvent:
    """Normalised view of one Box event before mapping to ``SourceObserved``.

    The C3 mapper consumes only these fields and discards the raw
    SDK payload, in line with ADR-0005 (External Content Min): the
    body of the underlying file is never read; we just record
    metadata about that the file/folder changed.

    Attributes
    ----------
    event_id:
        Box's globally-unique event id (``event_id`` field on the
        API response). Used as the ``external_id`` for deduplication
        — Box sometimes redelivers an event after a transient
        upstream failure and we want the projection idempotent.
    event_type:
        One of :data:`ITEM_EVENT_TYPES`. Other event types are
        filtered out before construction.
    item_id, item_type, item_name:
        The Box file / folder this event is about. ``item_type`` is
        ``"file"`` or ``"folder"``.
    item_path:
        Human-readable path joined from ``source.path_collection`` —
        e.g. ``"/Documents/Reports/Q3.pdf"``. We skip the synthetic
        "All Files" root Box always inserts at index 0. May be just
        the item name when ``path_collection`` is empty.
    created_iso:
        Event creation time as the raw ISO 8601 string Box returned.
        Kept as a string here — the C3 mapper parses it into a
        ``datetime`` at the projection boundary so this module
        avoids the datetime-conversion choices.
    actor_id, actor_name:
        Box user who triggered the event. ``actor_name`` is the
        display name; ``actor_id`` is the numeric Box user id.
    web_url:
        Browser-accessible URL for the item. ``None`` for some
        event types where Box does not expose a deep link (e.g.
        ``ITEM_TRASH`` of a now-deleted item — the item endpoint
        returns 404).
    raw:
        The full SDK-translated event dict. Retained for debugging
        / observability and so future mappers can pull additional
        fields without changing this contract. NOT serialised into
        the projection (ADR-0005).
    """

    event_id: str
    event_type: str
    item_id: str
    item_type: str
    item_name: str
    item_path: str
    created_iso: str
    actor_id: str
    actor_name: str
    web_url: str | None
    raw: dict[str, Any]


class BoxFetcher:
    """Pull file / folder events from Box via the Events API.

    Single-threaded paginator. Yields normalised :class:`RawBoxEvent`
    tuples alongside the **current** ``next_stream_position`` so the
    caller can persist the cursor after each individual event is
    committed — crash safety: even if the projection commits half a
    page before the process dies, the next sync resumes from the
    correct position rather than re-emitting committed events.

    Parameters
    ----------
    auth:
        The Phase 7 C1 :class:`BoxAuth` instance. Must already be in a
        usable state (operator has completed ``opshub connector auth
        set connector:box``). The fetcher does NOT drive the OAuth
        consent flow — only token refresh on demand.
    sleep:
        Test seam for backoff. Defaults to :func:`time.sleep`; tests
        substitute a recorder so they do not actually sleep on the
        429-retry paths.
    client_factory:
        Test seam for constructing the SDK client. Defaults to
        :meth:`BoxAuth.build_authenticated_client`; tests substitute
        a callable returning a fake client whose ``events()`` returns
        a programmable double.
    """

    def __init__(
        self,
        auth: BoxAuth,
        *,
        sleep: Callable[[float], None] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        # Lazy SDK presence probe: keeps the connectors-box extras out
        # of the cold-start path (mirrors :class:`BoxAuth` rationale)
        # and turns the missing-extras case into one actionable error
        # pointing at the extras name. We use :func:`importlib.import_module`
        # rather than ``import boxsdk.exception`` so the binding is not
        # left dangling at module scope (which pyright flags as unused).
        # The exception class is re-imported inside :meth:`fetch_events`
        # where it is actually used in the ``except`` clause.
        try:
            import importlib

            importlib.import_module("boxsdk.client.client")
            importlib.import_module("boxsdk.exception")
        except ImportError as exc:
            raise ConfigError(
                "Box connector requires the [connectors-box] extras. "
                "Install with: uv sync --extra connectors-box"
            ) from exc

        self._auth = auth
        self._sleep = sleep if sleep is not None else time.sleep
        self._client_factory = (
            client_factory if client_factory is not None else self._auth.build_authenticated_client
        )

    def fetch_events(self, *, stream_position: str | None) -> Iterator[tuple[RawBoxEvent, str]]:
        """Yield ``(event, current_stream_position)`` for each new item event.

        Parameters
        ----------
        stream_position:
            The cursor from the previous sync (Box returns it as
            ``next_stream_position`` on every page). ``None`` means
            "first sync" — the fetcher passes ``"now"`` to Box so the
            initial call returns the current marker and no historical
            events. The next sync will see real events.

        Yields
        ------
        tuple[RawBoxEvent, str]
            The normalised event and the ``next_stream_position`` that
            should be persisted **after** the event's own writes commit.
            All events on a single page share the same trailing
            stream_position, by Box's API contract (the position
            advances once per page, not once per event).

        Raises
        ------
        ConnectorFailedError
            On repeated 401 (refresh failed → operator must re-auth),
            exhausted 429 retries, or any other Box API exception.
        """
        from boxsdk.exception import BoxAPIException

        stream_pos: str | int = stream_position if stream_position is not None else "now"

        # Single page per call (Phase 7 MVP — caller invokes us once
        # per sync; the Events API is incremental so one page is what
        # we get per sync run). The retry loop wraps the single API
        # call: each attempt re-fetches the client because a 401
        # invalidates the cached token and the next attempt must rebuild
        # the OAuth wrapper with the fresh access token.
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(len(_BACKOFF_SECONDS)):
            client = self._client_factory()
            try:
                response: Any = client.events().get_events(
                    limit=_DEFAULT_PAGE_LIMIT,
                    stream_position=stream_pos,
                )
                # Narrow to ``dict[str, Any]`` at the SDK boundary.
                # boxsdk's translator returns either a plain dict or
                # its dict-like ``BaseObject`` subclass; both implement
                # the mapping protocol but their typings are ``Any``
                # which propagates "partially unknown" through the
                # downstream code unless we coerce here.
                if isinstance(response, dict):
                    result = cast("dict[str, Any]", response)
                else:
                    result = cast("dict[str, Any]", dict(response))
                break
            except BoxAPIException as exc:
                last_error = exc
                if exc.status == 401 and attempt == 0:
                    # First 401: cached access token is stale (Box
                    # rotated it on the server side without us knowing,
                    # e.g. operator hit "log out everywhere"). Force the
                    # next attempt through the refresh path.
                    self._auth.invalidate_cached_token()
                    continue
                if exc.status == 401:
                    # Refreshed once already and Box still 401s → the
                    # refresh token itself is dead. Operator action
                    # required; no point in further retries.
                    raise ConnectorFailedError(
                        "Box events API returned 401 after token refresh; "
                        "the stored refresh token may be revoked — run "
                        "`opshub connector auth set connector:box` to re-auth"
                    ) from exc
                if exc.status == 429:
                    delay = _retry_after_seconds(exc, attempt=attempt)
                    self._sleep(delay)
                    continue
                # Any other status (500s, 4xx other than 401/429) is
                # surfaced immediately — these are not transient retries
                # we want to spin on.
                raise ConnectorFailedError(
                    f"Box events API failed with status {exc.status}"
                ) from exc
        else:
            # Loop exhausted without ``break``: every attempt raised a
            # retryable status (429 or first-attempt 401-then-still-429).
            # ``last_error`` is guaranteed non-None because we only enter
            # the for-loop with ``len(_BACKOFF_SECONDS) > 0``.
            assert last_error is not None
            raise ConnectorFailedError(
                f"Box events API exhausted retries ({len(_BACKOFF_SECONDS)} attempts)"
            ) from last_error

        assert result is not None  # the break branch always assigns it
        new_position = str(result.get("next_stream_position", stream_pos))
        entries: list[Any] = list(result.get("entries") or [])
        for raw_event in entries:
            event_type = _safe_get(raw_event, "event_type")
            if event_type not in ITEM_EVENT_TYPES:
                # Phase 7 MVP filter: drop non-item events silently.
                continue
            normalised = _map_to_raw_box_event(raw_event)
            if normalised is None:
                # Defensive: a malformed event (missing source / id)
                # is skipped rather than crashing the whole sync. The
                # cursor still advances past it because Box's
                # ``next_stream_position`` is page-wide.
                continue
            yield normalised, new_position


# ---------------------------------------------------------------------------
# helpers (module-private)
# ---------------------------------------------------------------------------


def _retry_after_seconds(exc: Any, *, attempt: int) -> float:
    """Return the seconds to wait before retrying a 429.

    Honours the ``Retry-After`` header if Box sent one (RFC 6585 says
    it MAY be present on 429), otherwise falls back to the
    :data:`_BACKOFF_SECONDS` schedule. The header value is documented
    as either a delta-seconds integer or an HTTP-date; Box always uses
    the integer form so we only parse that — anything else falls back
    to the schedule.
    """
    headers: Any = getattr(exc, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is not None:
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            # Fall through to the static schedule below.
            pass
    return _BACKOFF_SECONDS[attempt]


def _safe_get(obj: Any, key: str) -> Any:
    """Read ``obj[key]`` or ``obj.key``, returning ``None`` on miss.

    Box SDK returns event entries that behave as both mappings and
    attribute bags (``BaseObject`` overrides ``__getattr__``). Tests
    typically use plain dicts. This helper papers over the difference
    so the mapping code is testable without a real ``Event`` object.

    Typed as ``Any -> Any`` because every Box-SDK access point in this
    module is ``Any``-shaped (boxsdk ships no strict type stubs); the
    helper purposefully widens to ``Any`` so callers can ``str(...)``
    fields without pyright complaining about partial unknowns.
    """
    if isinstance(obj, dict):
        return cast("dict[str, Any]", obj).get(key)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except (KeyError, AttributeError):
            return None
    return getattr(obj, key, None)


def _map_to_raw_box_event(raw: Any) -> RawBoxEvent | None:
    """Translate one SDK event into a :class:`RawBoxEvent` value object.

    Returns ``None`` if the event is missing fields we consider
    mandatory (id, source). Box has occasionally been observed to
    return events whose ``source`` is ``None`` (e.g. ITEM_TRASH events
    after the underlying item has been purged) — skipping them is
    safer than failing the whole sync.
    """
    event_id = _safe_get(raw, "event_id")
    event_type = _safe_get(raw, "event_type")
    source = _safe_get(raw, "source")
    if not event_id or not event_type or source is None:
        return None

    item_id = _safe_get(source, "id")
    item_type = _safe_get(source, "type")
    item_name = _safe_get(source, "name")
    if not item_id or not item_type or not item_name:
        return None

    actor: Any = _safe_get(raw, "created_by")
    actor_id = _safe_get(actor, "id") or "" if actor is not None else ""
    actor_name = _safe_get(actor, "name") or "" if actor is not None else ""

    return RawBoxEvent(
        event_id=str(event_id),
        event_type=str(event_type),
        item_id=str(item_id),
        item_type=str(item_type),
        item_name=str(item_name),
        item_path=_resolve_path(source),
        created_iso=str(_safe_get(raw, "created_at") or ""),
        actor_id=str(actor_id),
        actor_name=str(actor_name),
        web_url=_safe_optional_str(_safe_get(source, "web_link")),
        raw=_as_dict(raw),
    )


def _resolve_path(source: Any) -> str:
    """Build a human-readable path from ``source.path_collection``.

    Box's ``path_collection.entries`` always starts with the synthetic
    "All Files" root (the operator's Box root folder). We skip that
    entry so the rendered path matches what the operator sees in the
    Box web UI:

        path_collection: [All Files, Documents, Reports]
        source.name:     "Q3.pdf"
        →  "/Documents/Reports/Q3.pdf"

    Falls back to ``"/<name>"`` when the path_collection is missing or
    contains only the root, so the returned string is always non-empty
    when ``source.name`` exists.
    """
    name = _safe_get(source, "name") or ""
    pc = _safe_get(source, "path_collection")
    entries = _safe_get(pc, "entries") if pc is not None else None
    if not entries:
        return f"/{name}" if name else "/"
    # Skip the leading "All Files" root — Box always inserts it at
    # index 0 and surfacing it to the user is noise.
    parent_segments = [str(_safe_get(entry, "name") or "") for entry in entries[1:]]
    parent_path = "/".join(seg for seg in parent_segments if seg)
    if parent_path and name:
        return f"/{parent_path}/{name}"
    if parent_path:
        return f"/{parent_path}"
    return f"/{name}" if name else "/"


def _safe_optional_str(value: Any) -> str | None:
    """Coerce SDK ``Any`` to ``str | None`` without false ``""`` matches."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _as_dict(raw: Any) -> dict[str, Any]:
    """Best-effort capture of the SDK event payload as a plain dict.

    Used for the :attr:`RawBoxEvent.raw` debug field only — never
    persisted. Box ``Event`` objects expose ``response_object`` (the
    raw JSON they were translated from) and ``items()`` (the
    iterable-mapping interface). Tests pass plain dicts directly. If
    neither shape applies we fall back to an empty dict rather than
    risking a serialisation failure during a sync run.
    """
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw).copy()
    response = getattr(raw, "response_object", None)
    if isinstance(response, dict):
        return cast("dict[str, Any]", response).copy()
    items_fn = getattr(raw, "items", None)
    if callable(items_fn):
        try:
            items_iter: Any = items_fn()
        except (TypeError, ValueError):
            return {}
        result: dict[str, Any] = {}
        for pair in cast("list[tuple[Any, Any]]", list(items_iter)):
            # Defensive: ``items()`` should yield 2-tuples; anything
            # else is a malformed SDK return and we drop it silently
            # (this dict is debug-only — never persisted).
            try:
                key, value = pair
            except (TypeError, ValueError):
                continue
            result[str(key)] = value
        return result
    return {}
