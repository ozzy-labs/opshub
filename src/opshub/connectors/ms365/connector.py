"""Microsoft 365 :class:`Connector` implementation (Phase 7 step B3).

Composes the B1 auth helper + B2 fetcher + B3 mapper into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by the ``opshub connector sync ms365`` CLI in
:mod:`opshub.cli.connector`.

Endpoint groups
---------------

Microsoft Graph splits the Phase 7 MVP surface across three endpoint
groups with **independent** cursors:

* Calendar — ``/me/calendar/events``, cursor :data:`CURSOR_CALENDAR`.
* OneDrive — ``/me/drive/root/delta``, cursor :data:`CURSOR_ONEDRIVE`.
* Outlook — ``/me/messages``, cursor :data:`CURSOR_OUTLOOK`.

A failure in one group must NOT stall the other two — operators
running ``opshub connector sync ms365`` expect a partial sync to
record what it could rather than block on the first 429. The
connector therefore catches :class:`ConnectorFailedError` per group
and records a :class:`ConnectorSyncFailed` event with the sanitised
exception type name, then continues to the next group.

Cursor wiring (the :class:`ConnectorContext` mismatch)
------------------------------------------------------

The Phase 3 framework's :class:`ConnectorContext` carries a **single**
``cursor_value`` string — sufficient for connectors that read one
endpoint (GitHub). MS365's three endpoints need three independent
cursors, so this connector ignores ``context.cursor_value`` and reads
each per-endpoint cursor directly through
:meth:`SourceService.cursor_get` using the per-endpoint cursor keys
defined in :mod:`opshub.connectors.ms365.fetcher`. On completion the
connector writes each cursor back via :meth:`SourceService.cursor_set`
(``sync_started=False``). The framework-level
``cursor_set(sync_started=True/False)`` brackets that the CLI driver
runs around :meth:`sync` continue to fire under the ``"ms365"`` key
for sync-run observability, but the **substantive** cursor state
lives under the three per-endpoint keys.

Per-endpoint enable flags
-------------------------

:class:`MS365ConnectorSettings` exposes ``calendar_enabled`` /
``onedrive_enabled`` / ``outlook_enabled`` (all defaulting to
``True``). Operators who only consented to a subset of Microsoft
Graph scopes (e.g. ``Calendars.Read`` alone) flip the unused flags
to ``False`` so :meth:`sync` skips the corresponding endpoint
entirely — without those flags the fetcher would raise
:class:`ConnectorFailedError` on the missing-scope 403 and pollute
the event log with a sync failure per run.

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
the mapper (:mod:`opshub.connectors.ms365.mapper`), which enforces
the 200-char summary cap. Tokens never enter the event payload —
the only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opshub.connectors.base import SyncResult
from opshub.connectors.ms365 import mapper as ms365_mapper
from opshub.connectors.ms365.fetcher import (
    CURSOR_CALENDAR,
    CURSOR_ONEDRIVE,
    CURSOR_OUTLOOK,
)
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.ms365.fetcher import MS365Fetcher
    from opshub.domain.events.source import SourceObserved

__all__ = ["MS365Connector"]


# Type aliases used by :meth:`MS365Connector._run_endpoint` to keep
# the signature short. ``IteratorFactory`` is the function that takes a
# stored cursor (``str | None``) and returns the fetcher iterator;
# ``MapperFn`` is the per-endpoint mapper. The ``Raw`` type parameter
# is erased to ``Any`` because the function body never inspects the
# raw shape — every consumed attribute lives on the :class:`SourceObserved`
# the mapper returns.
if TYPE_CHECKING:
    IteratorFactory = Callable[[str | None], Iterator[tuple[Any, str]]]
    MapperFn = Callable[[Any], SourceObserved]


class MS365Connector:
    """Concrete :class:`Connector` for Microsoft 365 (Graph)."""

    name = "ms365"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one sync pass across the three Graph endpoint groups.

        Returns a single :class:`SyncResult` whose ``observed_count``
        is the **sum** across all three endpoints; ``new_cursor``
        forwards ``context.cursor_value`` unchanged (per-endpoint
        cursors are written through :meth:`SourceService.cursor_set`
        rather than smuggled through the framework's single-cursor
        field — see module docstring).

        Failures in any single endpoint group are recorded as
        :class:`ConnectorSyncFailed` events via
        :meth:`SourceService.record_sync_failure` and the connector
        continues to the next group. If every endpoint fails the
        observed count is zero, but :meth:`sync` still returns
        normally — the CLI driver records a top-level
        ``ConnectorSyncCompleted`` event because per-group failures
        already left a paper trail in the event log.
        """
        # Lazy imports keep cold-start budget tight (ADR-0001). The
        # ``MS365Auth`` / ``MS365Fetcher`` constructors trigger ``msal`` /
        # ``httpx`` imports on first call, which is acceptable here
        # because :meth:`sync` is only reached from the CLI command
        # callback (``opshub connector sync ms365``), never the
        # ``opshub --help`` cold path.
        from opshub.connectors.ms365.auth import MS365Auth
        from opshub.connectors.ms365.fetcher import MS365Fetcher
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        ms365_settings = settings.connectors.ms365
        if not ms365_settings.client_id:
            # The auth helper raises a near-identical ConfigError but
            # waiting until the first Graph call to surface it would
            # also try to read a refresh token from keyring first —
            # failing here is faster and the message is identical.
            raise ConfigError(
                "MS365 connector requires `[connectors.ms365] client_id` "
                "in opshub.toml (or OPSHUB_CONNECTORS__MS365__CLIENT_ID)."
            )

        auth = MS365Auth(
            client_id=ms365_settings.client_id,
            authority=ms365_settings.authority,
        )
        fetcher = MS365Fetcher(auth)
        observed_count = 0
        try:
            if ms365_settings.calendar_enabled:
                observed_count += self._sync_calendar(context, fetcher)
            if ms365_settings.onedrive_enabled:
                observed_count += self._sync_onedrive(context, fetcher)
            if ms365_settings.outlook_enabled:
                observed_count += self._sync_outlook(context, fetcher)
        finally:
            # The fetcher owns an ``httpx.Client`` socket pool. Closing
            # it deterministically here (rather than relying on GC) is
            # cheap and keeps the CLI process tidy when many connectors
            # run in sequence.
            fetcher.close()
        return SyncResult(observed_count=observed_count, new_cursor=context.cursor_value)

    # ----- per-endpoint syncs --------------------------------------------

    def _sync_calendar(self, context: ConnectorContext, fetcher: MS365Fetcher) -> int:
        """Sync ``/me/calendar/events``. Returns the observed count."""

        def factory(since: str | None) -> Iterator[tuple[Any, str]]:
            # Pyright + mypy both treat the lambda branch as
            # partially-unknown because the per-endpoint Raw dataclass
            # type would leak through the function reference. A named
            # nested function with explicit annotations keeps the
            # _run_endpoint signature uniform across the three call
            # sites without introducing a generic helper class.
            return fetcher.fetch_calendar_events(since_iso=since)

        return self._run_endpoint(
            context,
            cursor_key=CURSOR_CALENDAR,
            iterator_factory=factory,
            mapper_fn=ms365_mapper.map_calendar_event,
        )

    def _sync_onedrive(self, context: ConnectorContext, fetcher: MS365Fetcher) -> int:
        """Sync ``/me/drive/root/delta``. Returns the observed count."""

        def factory(since: str | None) -> Iterator[tuple[Any, str]]:
            # OneDrive's fetcher takes a ``delta_link`` rather than an
            # ``since_iso`` cursor — the parameter name differs but the
            # semantics (opaque resume token) are equivalent for the
            # connector's purposes.
            return fetcher.fetch_onedrive_changes(delta_link=since)

        return self._run_endpoint(
            context,
            cursor_key=CURSOR_ONEDRIVE,
            iterator_factory=factory,
            mapper_fn=ms365_mapper.map_onedrive_item,
        )

    def _sync_outlook(self, context: ConnectorContext, fetcher: MS365Fetcher) -> int:
        """Sync ``/me/messages``. Returns the observed count."""

        def factory(since: str | None) -> Iterator[tuple[Any, str]]:
            return fetcher.fetch_outlook_messages(since_iso=since)

        return self._run_endpoint(
            context,
            cursor_key=CURSOR_OUTLOOK,
            iterator_factory=factory,
            mapper_fn=ms365_mapper.map_outlook_message,
        )

    # ----- shared driver --------------------------------------------------

    def _run_endpoint(
        self,
        context: ConnectorContext,
        *,
        cursor_key: str,
        iterator_factory: IteratorFactory,
        mapper_fn: MapperFn,
    ) -> int:
        """Drive one endpoint group: load cursor → iterate → observe → save cursor.

        ``iterator_factory`` / ``mapper_fn`` are typed via the
        :data:`IteratorFactory` / :data:`MapperFn` aliases at the top
        of this module. The raw item type is erased to :class:`Any`
        because the function body never inspects it — every consumed
        attribute lives on the :class:`SourceObserved` instance that
        ``mapper_fn`` returns.

        Per-endpoint :class:`ConnectorFailedError` is swallowed and
        recorded as a :class:`ConnectorSyncFailed` event via
        :meth:`SourceService.record_sync_failure`; the function
        returns the count of items that **had** been observed before
        the failure, so a fetcher that errors out mid-page still
        contributes its prior yields to the run total.
        """
        # ``SourceService.cursor_get`` is the read-side projection
        # lookup. Treated as ``Any`` at the ConnectorContext boundary
        # (see :class:`opshub.connectors.context.ConnectorContext`).
        source_service = context.source_service
        cursor = source_service.cursor_get(cursor_key)
        # Open the per-endpoint sync-run bracket. The
        # :class:`ConnectorCursorsProjection` reducer treats
        # :class:`ConnectorSyncStarted` as an upsert and
        # :class:`ConnectorSyncCompleted` as an update-only, so
        # emitting *started* first guarantees the projection row exists
        # by the time we advance the cursor at the end of the loop.
        # Without this bracket the very first sync for a fresh
        # endpoint would observe items but leave the projection
        # cursor blank (the update would be a silent no-op).
        source_service.cursor_set(cursor_key, cursor, sync_started=True)
        observed = 0
        try:
            iterator = iterator_factory(cursor)
            new_cursor = cursor
            for item, advanced_cursor in iterator:
                event = mapper_fn(item)
                source_service.observe(
                    connector_name="ms365",
                    external_id=event.external_id,
                    source_type=event.source_type,
                    title=event.title,
                    url=event.url,
                    summary=event.summary,
                )
                new_cursor = advanced_cursor
                observed += 1
        except ConnectorFailedError as exc:
            # Sanitise: only the exception type name reaches the event
            # log (ADR-0005). The fetcher already scrubs tokens from
            # its message, but funnelling the type name keeps the
            # contract identical across all four connectors. The
            # ``ConnectorSyncStarted`` event we already emitted is
            # left in place — the projection reducer documents
            # :class:`ConnectorSyncFailed` as a no-op so the cursor
            # row stays at its prior value (phase-3-plan §4 Q3).
            source_service.record_sync_failure(
                self.name, error_message=f"{cursor_key}: {type(exc).__name__}"
            )
            return observed

        # Always close the per-endpoint sync-run bracket on the success
        # path so observers see a matched started/completed pair even
        # when the loop yielded zero items. ``cursor_set`` appends an
        # event unconditionally (ADR-0002 immutability); the projection
        # reducer collapses the resulting update on the existing row.
        source_service.cursor_set(cursor_key, new_cursor, sync_started=False)
        return observed
