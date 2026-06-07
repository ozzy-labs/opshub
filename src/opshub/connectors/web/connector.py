"""Web connector implementation (Phase 21-C, ADR-0037 / ADR-0010 §Phase 21 改訂).

Composes the :func:`opshub.connectors.web.fetcher.fetch_pages` browser
driver and the :func:`opshub.connectors.web.mapper.map_page_content` mapper
into the :class:`opshub.connectors.base.Connector` Protocol contract.
Driven by the ``opshub web sync`` CLI in :mod:`opshub.cli.web` (shared
driver: :mod:`opshub.cli._connector_common`).

Sync flow (ADR-0010 §Phase 21 改訂 (n)-(o), ADR-0019 §決定 (d) pattern)
----------------------------------------------------------------------

1. **Resolve ``pages``** from
   :class:`opshub.core.config.OpsHubSettings`'
   :class:`WebConnectorSettings`. An empty list is a no-op success (0
   observed) — a fresh install with ``enabled`` but no pages listed
   simply observes nothing, never an error.
2. **Hydrate ``prior_fingerprints``** by selecting
   ``(external_id, fingerprint)`` rows from the ``sources`` projection
   where ``connector_name = 'web'`` (ADR-0010 §Phase 21 改訂 (o) #2,
   identical shape to the box_drive hydration). The result is the dict the
   connector short-circuits against.
3. **Fetch + map + diff each URL**. :func:`fetch_pages` renders each URL
   and yields a :class:`~opshub.browser.core.PageContent` (a dead URL is
   logged at WARN and skipped, ADR-0019 §決定 fail-safe). Each page is
   mapped to a :class:`~opshub.domain.events.source.SourceObserved`; when
   its ``fingerprint`` (SHA-256 of the body) matches the prior value, the
   page is **unchanged** and we emit **no event** (ADR-0019 §決定 (d)
   change-detection). Otherwise the event is forwarded through
   :meth:`SourceService.observe` so the ``sources.fingerprint`` column
   advances for the next run.
4. **Return a ``SyncResult``** carrying the observed count and a
   ``new_cursor`` set to the current UTC ISO timestamp — informational
   only (the per-URL fingerprint diff is the actual change-detection
   mechanism, matching box_drive), persisted so the
   ``connector_cursors`` row's ``updated_at`` reflects the last sync.

Binary-missing handling (ADR-0037 §決定 (g))
--------------------------------------------

When ``playwright`` / Chromium is absent, the browser core raises
:class:`~opshub.core.errors.ConfigError` from the **first**
:func:`fetch_page` call. Because every URL would fail identically, that is
a whole-run misconfiguration, not a per-URL skip: it propagates out of
:meth:`sync` so the CLI driver maps it to a non-zero exit (the
``playwright install chromium`` pointer reaches the operator) **without**
appending a ``ConnectorSyncFailed`` event — config mistakes are not
connector failures (matches the box_drive ``root_path`` ``ConfigError``).

Cold-start budget (ADR-0001)
----------------------------

This module imports only the mapper + framework primitives at top level.
:class:`OpsHubSettings`, the ``sources`` projection table, and the
``playwright``-backed browser core are loaded lazily inside :meth:`sync`
(the browser core's ``playwright`` import is itself deferred, ADR-0037
§決定 (g)) so ``opshub --help`` cold start stays within the ~300ms budget
on installations that never run a web sync.

Atomicity (matches box_drive + the SaaS connectors)
---------------------------------------------------

Each observed page goes through a single :meth:`SourceService.observe`
call, which atomically appends one :class:`SourceObserved` + one
:class:`~opshub.domain.events.inbox.ItemEnqueued` in one UoW. The connector
loop is one-UoW-per-page so partial-success scenarios remain auditable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.web.mapper import map_page_content

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from opshub.browser.core import PageContent
    from opshub.connectors.context import ConnectorContext
    from opshub.core.config import OpsHubSettings

__all__ = ["WebConnector"]


class WebConnector:
    """Concrete :class:`~opshub.connectors.base.Connector` for browser-rendered Web pages.

    Parameters
    ----------
    settings_factory:
        Test seam. Defaults to ``None``, which causes :meth:`sync` to
        construct a fresh :class:`OpsHubSettings`. Unit tests supply a
        zero-arg callable returning a pre-built settings object so the
        connector path is exercised without env / TOML plumbing. The
        factory is a constructor argument (not a class attribute) so each
        instance carries its own override — tests can register a fresh
        connector under the global registry without leaking state.
    fetch_pages_factory:
        Test seam for the browser driver. Defaults to ``None``, which uses
        the real :func:`opshub.connectors.web.fetcher.fetch_pages`. Unit
        tests inject a stub that yields canned
        :class:`~opshub.browser.core.PageContent` records (or raises) so
        the connector logic — fingerprint diff, observe threading — is
        tested without a real Chromium launch. The signature mirrors
        ``fetch_pages(pages, *, settings, logger)``.

    The connector holds no network state at construction time — every
    resolve happens at the start of :meth:`sync`. That keeps the cold-start
    import cheap (pydantic-settings + the browser core only load when an
    operator actually runs ``opshub web sync``).
    """

    name = "web"

    def __init__(
        self,
        settings_factory: Callable[[], OpsHubSettings] | None = None,
        fetch_pages_factory: Callable[..., Iterable[PageContent]] | None = None,
    ) -> None:
        self._settings_factory = settings_factory
        self._fetch_pages_factory = fetch_pages_factory

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one web sync pass.

        See module docstring for the full flow. Propagates
        :class:`~opshub.core.errors.ConfigError` (binary missing) from the
        first fetch so the CLI driver surfaces the ``playwright install
        chromium`` pointer without an event log entry.
        """
        from datetime import UTC, datetime

        settings = self._resolve_settings()
        pages = list(settings.connectors.web.pages)
        prior_fingerprints = self._load_prior_fingerprints(context)
        fetch_pages = self._resolve_fetch_pages()

        observed_count = 0
        for page in fetch_pages(pages, settings=settings, logger=context.logger):
            event = map_page_content(page)
            # ADR-0019 §決定 (d) change-detection: an unchanged page (same
            # body → same SHA-256 fingerprint) emits no event so an
            # idempotent re-sync produces zero ``SourceObserved`` noise.
            if prior_fingerprints.get(event.external_id) == event.fingerprint:
                continue
            context.source_service.observe(
                connector_name=event.connector_name,
                external_id=event.external_id,
                source_type=event.source_type,
                title=event.title,
                url=event.url,
                summary=event.summary,
                fingerprint=event.fingerprint,
                body=event.body,
                provenance_origin=event.provenance_origin,
                provenance_trust=event.provenance_trust,
            )
            observed_count += 1

        # Informational-only cursor (the fingerprint diff is the real
        # change-detection mechanism), still persisted so the
        # ``connector_cursors`` row touches its ``updated_at`` column.
        new_cursor = datetime.now(tz=UTC).isoformat()
        return SyncResult(observed_count=observed_count, new_cursor=new_cursor)

    # ------------------------------------------------------------------ helpers

    def _resolve_settings(self) -> OpsHubSettings:
        """Build :class:`OpsHubSettings` (or use the test seam)."""
        if self._settings_factory is not None:
            return self._settings_factory()
        from opshub.core.config import OpsHubSettings

        return OpsHubSettings()

    def _resolve_fetch_pages(self) -> Callable[..., Iterable[PageContent]]:
        """Return the browser driver (real :func:`fetch_pages` or the test seam)."""
        if self._fetch_pages_factory is not None:
            return self._fetch_pages_factory
        from opshub.connectors.web.fetcher import fetch_pages

        return fetch_pages

    @staticmethod
    def _load_prior_fingerprints(context: ConnectorContext) -> dict[str, str]:
        """Build the ``{normalized_url: fingerprint}`` map from the ``sources`` projection.

        ADR-0010 §Phase 21 改訂 (o) #2: hydrate the prior map by selecting
        ``(external_id, fingerprint) FROM sources WHERE connector_name =
        'web'`` through the :class:`SourceService` engine handle — the
        identical shape to the box_drive hydration (ADR-0019 §決定 (d)).
        Rows with a ``NULL`` fingerprint are skipped (a ``None`` value
        cannot match a live SHA-256 hex digest, so re-emitting such a page
        is the right behaviour — the next observe populates the column).

        The lookup runs on a freshly-opened connection (not the service's
        UoW) because it is a *pre-sync* read.
        """
        from sqlalchemy import select

        from opshub.projections.sources import sources_table

        engine = getattr(context.source_service, "engine", None)
        if engine is None:
            engine = getattr(context.source_service, "_engine", None)
        if engine is None:
            # No engine (test stub) → first-sync semantics: every page is
            # treated as changed and gets observed.
            return {}

        statement = select(
            sources_table.c.external_id,
            sources_table.c.fingerprint,
        ).where(sources_table.c.connector_name == "web")

        prior: dict[str, str] = {}
        with engine.connect() as conn:
            for external_id, fingerprint in conn.execute(statement):
                if fingerprint is None:
                    continue
                prior[external_id] = fingerprint
        return prior
