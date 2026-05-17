"""CLI-driven embedding orchestration (Phase 4 step B2, ADR-0012).

Phase 4 MVP is CLI-driven: the operator runs ``opshub embeddings rebuild``
(PR B3) which calls :meth:`EmbeddingService.embed_pending`. The service
walks every supported entity type (task / decision / inbox_item /
source), reads each row's summary text, checks whether the ``embeddings``
projection already has a current ``(model_id, model_version)`` row for
that entity, and embeds only the missing ones. Per-entity failures emit
:class:`~opshub.domain.events.embedding.EmbeddingFailed` and continue —
a single bad row should not block the rest of the batch.

Atomicity
---------

Each successful embed appends one
:class:`~opshub.domain.events.embedding.TextEmbedded` event + one
:meth:`VectorStore.upsert` call. The event append + projector apply
run in one UoW (matching the PR #26 / Phase 3 contract); the
:meth:`VectorStore.upsert` is sequenced **after** the UoW closes
because the Phase 1 :class:`~opshub.vectors.store.VectorStore`
Protocol does not accept an external connection and nesting two
``engine.begin()`` blocks on SQLite would deadlock. The
inconsistency window — event committed, upsert failed — is bounded:
the next :meth:`EmbeddingService.embed_pending` retries the entity
because the ``embeddings`` metadata table has no row (the
``NOT EXISTS`` filter sees it as pending). See
:meth:`EmbeddingService._commit_embedding` for the full rationale.
Each failure path (Embedder raises) appends a single
:class:`EmbeddingFailed` event in its own UoW so the diagnostic is
still recorded even when the embed itself failed.

Idempotency
-----------

Re-running ``embed_pending`` on the same data is a no-op because the
service consults the ``embeddings`` projection before each embed. Switching
backends in config causes ``model_id`` to change; the next rebuild
re-embeds everything (the new ``model_id`` has zero prior rows in the
``embeddings`` projection).

Phase 4.x scope: event-driven auto-embed (projector hook + queue),
debounce, throttling, parallel multi-backend retention.

Error sanitisation
------------------

:meth:`_sanitise_error` is a defensive net for the common token
shapes (``sk-...``, ``ghp_...``, ``Bearer ...``) — the more important
guarantee is that the concrete embedders (PR #65 OpenAI / Voyage,
PR #66 local) NEVER include the API key in their exception messages.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Table, select, text

from opshub.core.ids import new_ulid
from opshub.core.sanitise import sanitise_error_message
from opshub.core.time import now_utc
from opshub.domain.events.embedding import (
    EmbeddingFailed,
    EmbeddingRebuildRequested,
    TextEmbedded,
)
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector
from opshub.vectors.store import StoredEmbedding, VectorStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from opshub.vectors.embedder import Embedder, EmbeddingResult


__all__ = ["EmbedResult", "EmbeddingService", "EntitySource"]


_DEFAULT_ACTOR = "cli:embeddings_rebuild"


# Maximum length of the sanitised error_message we attach to an
# :class:`EmbeddingFailed` event. The event's Pydantic ``Field`` caps at
# 2000; we truncate first so a giant traceback never trips validation.
_MAX_ERROR_MESSAGE_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class EntitySource:
    """Mapping from entity type → text-extraction column.

    Each instance says "for ``entity_type=X``, embed the value in
    ``text_column`` from the projection ``Table``, keyed by ``id_column``".
    The service iterates :data:`_SOURCES` so adding a new embeddable
    entity in Phase 4.x is one entry there.
    """

    entity_type: str
    table_name: str  # diagnostic / log breadcrumb
    id_column: str
    text_column: str


# Phase 4 entity → text column mapping. The columns match the projection
# tables registered in :mod:`opshub.projections` (verified against the
# table declarations in PR #45 / PR #4 / PR #11 / PR #5).
_SOURCES: tuple[EntitySource, ...] = (
    EntitySource("task", "tasks", "id", "title"),
    EntitySource("decision", "decisions", "id", "text"),
    EntitySource("inbox_item", "inbox_items", "id", "summary"),
    EntitySource("source", "sources", "id", "summary"),
)


# Table lookup by ``entity_type`` so :meth:`EmbeddingService._iter_pending`
# can compose the SELECT statement from the Core ``Table`` object. The
# ``embeddings`` table is NOT in this mapping — it is not registered on
# :data:`opshub.db.schema.metadata` and the service joins against it via
# raw SQL (matching the SqliteVecStore precedent in PR #67).
_TABLE_BY_ENTITY_TYPE: dict[str, Table] = {
    "task": tasks_table,
    "decision": decisions_table,
    "inbox_item": inbox_items_table,
    "source": sources_table,
}


@dataclass(frozen=True, slots=True)
class EmbedResult:
    """Outcome of one :meth:`EmbeddingService.embed_pending` invocation.

    Attributes
    ----------
    embedded_count:
        Number of entities newly embedded.
    skipped_count:
        Number of entities that already had a current
        ``(model_id, model_version)`` row in the projection, or whose
        ``text`` column was empty / NULL.
    failed_count:
        Number of entities whose embed call raised; an
        :class:`EmbeddingFailed` event was appended for each.
    rebuild_run_id:
        ULID of the :class:`EmbeddingRebuildRequested` event that
        bracketed this rebuild — surfaced for audit / log correlation
        (the CLI in PR B3 prints it on success).
    """

    embedded_count: int
    skipped_count: int
    failed_count: int
    rebuild_run_id: str


class EmbeddingService:
    """Compose :class:`Embedder` + :class:`VectorStore` + :class:`EventStore`
    into a CLI-driven rebuild flow.

    Constructor mirrors the PR #26 contract: ``store`` / ``projector`` /
    ``uow_factory`` / ``actor``. ``embedder`` and ``vector_store`` come
    from :mod:`opshub.vectors.factory` (PR #68); the CLI wiring helper
    (:func:`opshub.cli._wiring.build_embedding_service`) resolves them
    per invocation so config changes (backend switch) take effect on
    the next ``opshub embeddings rebuild``.

    Parameters
    ----------
    store:
        Append target. Only the :class:`EventStore` Protocol is required.
    projector:
        Read-model updater. Called with each event in append order on
        the same connection ``store.append`` was called with.
    embedder:
        Concrete :class:`~opshub.vectors.embedder.Embedder` resolved by
        :func:`opshub.vectors.factory.build_embedder` from the configured
        backend.
    vector_store:
        Concrete :class:`~opshub.vectors.store.VectorStore` resolved by
        :func:`opshub.vectors.factory.build_vector_store`. Phase 4 MVP
        always returns a :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore`.
    engine:
        Required SQLAlchemy :class:`~sqlalchemy.engine.Engine`. The
        service reads two projections on every rebuild — the per-entity
        table (``tasks`` / ``decisions`` / ``inbox_items`` / ``sources``)
        and the ``embeddings`` metadata table — and there is no
        in-memory fallback.
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`.
        When supplied, every commit runs ``store.append`` and
        ``projector.apply`` on the same connection inside a single
        transaction. The :class:`VectorStore` upsert runs *after* the
        UoW closes (the Protocol does not accept an external
        connection); see :meth:`_commit_embedding` for the sequencing
        rationale.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:embeddings_rebuild"`` — the rebuild path is always
        operator-driven (CLI), never connector-driven.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        embedder: Embedder,
        vector_store: VectorStore,
        engine: Engine,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._store = store
        self._projector = projector
        self._embedder = embedder
        self._vector_store = vector_store
        self._engine = engine
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ commands

    def embed_one_if_pending(self, entity_type: str, entity_id: str) -> bool:
        """Embed a single entity if it has no current-backend embedding.

        Single-row variant of :meth:`embed_pending` intended for the
        Phase 5 step C1 auto-embed projector hook
        (:class:`opshub.services.auto_embed_hook.AutoEmbedHook`). The
        method reads the entity's text from the matching projection,
        runs the same ``NOT EXISTS`` predicate that
        :meth:`_iter_pending` uses, embeds when pending, and records
        :class:`EmbeddingFailed` on embedder errors.

        Crucially, this method **does not raise**. The auto-embed hook
        runs **after** the originating event has committed (so the
        embed cannot unwind it); propagating an exception from here
        would surface a confusing post-commit traceback to the
        operator. Failures land on the event log as
        :class:`EmbeddingFailed` (same shape :meth:`embed_pending`
        produces) so the next ``opshub embeddings drain`` /
        ``opshub embeddings rebuild`` retries the entity via the
        existing ``NOT EXISTS`` retry mechanism.

        Parameters
        ----------
        entity_type:
            One of the supported entity types (``"task"`` /
            ``"decision"`` / ``"inbox_item"`` / ``"source"``).
            Unknown values return ``False`` without raising — the
            hook is allowed to dispatch on event types it does not
            recognise without the service objecting.
        entity_id:
            The aggregate id of the entity to embed. When the row does
            not exist in the projection (e.g. the projector did not
            yet apply, or the entity was deleted) the method returns
            ``False`` silently.

        Returns
        -------
        bool
            ``True`` when a new embedding was written. ``False`` when
            the entity was already embedded by the current backend,
            had empty / NULL text, or could not be embedded (failure
            already recorded as :class:`EmbeddingFailed`).
        """
        sources = self._sources(entity_type)
        if not sources:
            return False
        source = sources[0]
        try:
            raw_text = self._fetch_pending_text(source, entity_id)
            if raw_text is None:
                return False
            outcome = self._embed_one(source, entity_id, raw_text)
        except Exception as exc:  # post-commit safety net
            # The originating event has already committed by the time
            # the hook runs (see :mod:`opshub.services.auto_embed_hook`).
            # We must never re-raise; the entity stays in the "pending"
            # state for the next rebuild / drain to retry.
            try:
                self._record_failure(source.entity_type, entity_id, str(exc))
            except Exception:  # pragma: no cover - last-resort guard
                # Even the failure-event append went wrong (e.g. DB
                # locked). Swallow so the hook never raises; the
                # entity will simply be retried on the next rebuild.
                pass
            return False
        return outcome == "embedded"

    def embed_pending(
        self,
        *,
        entity_type: str | None = None,
        limit: int | None = None,
    ) -> EmbedResult:
        """Embed every entity that lacks a current ``(model_id, model_version)`` row.

        The bracketing :class:`EmbeddingRebuildRequested` event is
        always appended — even when the rebuild ends up processing
        zero rows — so a later operator can audit "when was the last
        rebuild attempted, and what scope".

        Parameters
        ----------
        entity_type:
            If set, restrict to this entity type (one of ``"task"`` /
            ``"decision"`` / ``"inbox_item"`` / ``"source"``). Unknown
            values produce an empty result with no work done (the
            ``EmbeddingRebuildRequested`` event still records the
            scope the operator asked for). ``None`` = process every
            supported entity type.
        limit:
            Cap the number of rows processed (across entity types when
            ``entity_type`` is ``None``). ``None`` = unbounded. Useful
            for operators who want a paced rebuild on a large store.
            Skipped rows (empty text) do **not** count against the
            limit; only embedded + failed rows do.

        Returns
        -------
        EmbedResult
            Counts + the rebuild_run_id of the bracketing event.
        """
        rebuild_run_id = self._record_rebuild_request(scope=entity_type or "all")
        embedded = 0
        skipped = 0
        failed = 0
        remaining = limit
        for source in self._sources(entity_type):
            if remaining is not None and remaining <= 0:
                break
            for entity_id, raw_text in self._iter_pending(source):
                if remaining is not None and remaining <= 0:
                    break
                outcome = self._embed_one(source, entity_id, raw_text)
                if outcome == "embedded":
                    embedded += 1
                    if remaining is not None:
                        remaining -= 1
                elif outcome == "skipped":
                    skipped += 1
                else:  # "failed"
                    failed += 1
                    if remaining is not None:
                        remaining -= 1
        return EmbedResult(
            embedded_count=embedded,
            skipped_count=skipped,
            failed_count=failed,
            rebuild_run_id=rebuild_run_id,
        )

    # ------------------------------------------------------------------ helpers

    def _sources(self, entity_type: str | None) -> tuple[EntitySource, ...]:
        """Return the :data:`_SOURCES` entries matching ``entity_type``.

        ``None`` returns every supported entity. An unknown literal
        returns an empty tuple — the rebuild becomes a no-op but the
        bracketing :class:`EmbeddingRebuildRequested` event still records
        the scope the operator asked for (audit trail).
        """
        if entity_type is None:
            return _SOURCES
        return tuple(s for s in _SOURCES if s.entity_type == entity_type)

    def _iter_pending(self, source: EntitySource) -> Iterator[tuple[str, str | None]]:
        """Yield ``(entity_id, text)`` pairs that lack a current embedding.

        Approach: SELECT ``(id, text)`` from the source projection
        table and filter with a correlated ``NOT EXISTS`` against the
        ``embeddings`` metadata table keyed on
        ``(entity_type, entity_id, model_id, model_version)``. The
        result is exactly the rows that the current backend has not
        yet embedded.

        The ``embeddings`` table is **not** registered on
        :data:`opshub.db.schema.metadata` (matching the SqliteVecStore
        precedent in PR #67) so the subquery is expressed via raw
        :func:`sqlalchemy.text`. The source table is accessed through
        its Core :class:`Table` declaration so the column names on
        the outer SELECT stay statically checked.
        """
        source_table = _TABLE_BY_ENTITY_TYPE[source.entity_type]
        id_col = source_table.c[source.id_column]
        text_col = source_table.c[source.text_column]
        # ``NOT EXISTS`` keeps the predicate well-formed even when the
        # ``embeddings`` row count is zero (the first rebuild on a
        # fresh database). Bind params keep the SQL value-safe.
        not_exists_sql = text(
            "NOT EXISTS (SELECT 1 FROM embeddings "
            "WHERE embeddings.entity_type = :__et "
            f"  AND embeddings.entity_id = {source.table_name}.{source.id_column} "
            "  AND embeddings.model_id = :__mid "
            "  AND embeddings.model_version = :__mv)"
        ).bindparams(
            __et=source.entity_type,
            __mid=self._embedder.model_id,
            __mv=self._embedder.model_version,
        )
        stmt = select(id_col, text_col).where(not_exists_sql)
        with self._engine.connect() as conn:
            for row in conn.execute(stmt):
                # SQLAlchemy returns ``Any`` for column values; the
                # projection schemas constrain id_column to ``str`` and
                # text_column to ``str | None``.
                yield str(row[0]), (None if row[1] is None else str(row[1]))

    def _fetch_pending_text(self, source: EntitySource, entity_id: str) -> str | None:
        """Return the entity's text iff a current-backend embedding is missing.

        Single-row counterpart to :meth:`_iter_pending`: reads the
        projection row for ``entity_id`` and applies the same
        ``NOT EXISTS`` predicate against the ``embeddings`` metadata
        table keyed on the current ``(model_id, model_version)`` pair.

        Returns
        -------
        str | None
            The text value when the entity is pending (no current
            embedding yet). ``None`` when the entity does not exist in
            the projection, when the text column is NULL, or when the
            current backend already has an embedding recorded.

        Notes
        -----
        Reading ``model_id`` / ``model_version`` off the bound
        ``_embedder`` matches :meth:`_iter_pending` so a backend
        switch via config takes effect on the next hook invocation
        without re-deriving the descriptors here.
        """
        source_table = _TABLE_BY_ENTITY_TYPE[source.entity_type]
        id_col = source_table.c[source.id_column]
        text_col = source_table.c[source.text_column]
        not_exists_sql = text(
            "NOT EXISTS (SELECT 1 FROM embeddings "
            "WHERE embeddings.entity_type = :__et "
            f"  AND embeddings.entity_id = {source.table_name}.{source.id_column} "
            "  AND embeddings.model_id = :__mid "
            "  AND embeddings.model_version = :__mv)"
        ).bindparams(
            __et=source.entity_type,
            __mid=self._embedder.model_id,
            __mv=self._embedder.model_version,
        )
        stmt = select(text_col).where(id_col == entity_id).where(not_exists_sql)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        value = row[0]
        if value is None:
            return None
        return str(value)

    def _embed_one(self, source: EntitySource, entity_id: str, raw_text: str | None) -> str:
        """Embed one row. Returns one of ``'embedded'`` / ``'skipped'`` / ``'failed'``.

        Empty / whitespace-only text is treated as ``skipped`` — there
        is no useful vector to compute and no event is appended. This
        keeps the embed projection consistent with "current state has
        no recall surface" for entities without summaries.
        """
        if raw_text is None or not raw_text.strip():
            return "skipped"
        try:
            embedding_result = self._embedder.embed_one(raw_text)
        except Exception as exc:  # service-level failure boundary
            self._record_failure(source.entity_type, entity_id, str(exc))
            return "failed"
        self._commit_embedding(source.entity_type, entity_id, embedding_result)
        return "embedded"

    def _commit_embedding(
        self,
        entity_type: str,
        entity_id: str,
        result: EmbeddingResult,
    ) -> None:
        """Append :class:`TextEmbedded` then upsert the vector.

        Order rationale
        ---------------

        The :class:`VectorStore` Protocol does not accept an external
        :class:`~sqlalchemy.engine.Connection` (Phase 1 design), so
        :meth:`VectorStore.upsert` opens its own ``engine.begin()``
        block (see :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore`
        in PR #67). Nesting that inside the EventStore's UoW would
        deadlock on SQLite (the outer UoW already holds a write lock).

        We therefore commit the :class:`TextEmbedded` event + the
        projector apply **first** (one UoW), then call
        :meth:`VectorStore.upsert` after the UoW closes. The two
        operations are sequenced rather than transactionally atomic;
        the inconsistency window is bounded as follows:

        * Event-UoW failure (projector raises): the event is rolled
          back, the upsert never runs. Consistent state.
        * Event-UoW success, upsert failure: the event log says
          "embedded" but the ``embeddings`` metadata table has no
          matching row. The next :meth:`embed_pending` sees the
          entity as "not yet embedded" by the
          :meth:`_iter_pending` ``NOT EXISTS`` filter and retries —
          producing a duplicate event but ultimately writing the
          vector. The natural-key UNIQUE constraint on
          ``embeddings`` keeps the projection clean.

        Phase 4.x can collapse the two when multi-store atomicity
        becomes necessary; the trade-off documented here is
        acceptable for the CLI-driven rebuild path.
        """
        timestamp = now_utc()
        event = TextEmbedded(
            aggregate_id=entity_id,
            actor=self._actor,
            entity_type=entity_type,  # type: ignore[arg-type]
            entity_id=entity_id,
            model_id=result.model_id,
            model_version=result.model_version,
            dim=result.dim,
            occurred_at=timestamp,
            recorded_at=timestamp,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)
        # UoW has now committed (or rolled back + raised). Run the
        # vector upsert outside the event-store transaction so the
        # vec0 INSERT can grab its own write lock without contending
        # with the just-released event-store lock.
        stored = StoredEmbedding(
            entity_type=entity_type,
            entity_id=entity_id,
            vector=result.vector,
            model_id=result.model_id,
            model_version=result.model_version,
            created_at=timestamp,
        )
        self._vector_store.upsert([stored])

    def _record_failure(self, entity_type: str, entity_id: str, error_message: str) -> None:
        """Append an :class:`EmbeddingFailed` event with a sanitised message.

        Runs in its own UoW (the calling :meth:`_embed_one` has already
        unwound the failing embed call). The event is what surfaces
        the failure to ``opshub embeddings status`` (PR B3); the caller
        decides whether to continue (the service does, by default) or
        bail.
        """
        sanitised = self._sanitise_error(error_message)
        event = EmbeddingFailed(
            aggregate_id=entity_id,
            actor=self._actor,
            entity_type=entity_type,  # type: ignore[arg-type]
            entity_id=entity_id,
            model_id=self._embedder.model_id,
            error_message=sanitised,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)

    def _record_rebuild_request(self, *, scope: str) -> str:
        """Append the bracketing :class:`EmbeddingRebuildRequested` event.

        Minted with a fresh ULID for ``aggregate_id`` so concurrent
        rebuild requests (e.g. operator-triggered + scheduled in
        Phase 4.x) are independently addressable. The ULID is surfaced
        in :class:`EmbedResult.rebuild_run_id` so the CLI can echo it.
        """
        run_id = new_ulid()
        event = EmbeddingRebuildRequested(
            aggregate_id=run_id,
            actor=self._actor,
            scope=scope,
            model_id=self._embedder.model_id,
            model_version=self._embedder.model_version,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)
        return run_id

    def _sanitise_error(self, message: str) -> str:
        """Truncate to the event-schema cap and redact obvious token shapes.

        The B2 contract is: the embedder layer must NOT include the
        API key in its exception messages (PR #65 / #66 honour this).
        This pass is a coarse net for the common shapes (``sk-...``,
        ``ghp_...``, ``Bearer ...``) and not a comprehensive PII
        scrubber. The redaction logic itself lives in
        :func:`opshub.core.sanitise.sanitise_error_message` so the
        Phase 5 :class:`~opshub.services.briefing_service.BriefingService`
        (step B3) can share the exact same regex set.

        Truncation happens **before** the regex pass so a giant log
        body cannot trip the :class:`EmbeddingFailed.error_message`
        2000-char :class:`pydantic.Field` cap before redaction runs.
        """
        truncated = message[:_MAX_ERROR_MESSAGE_LENGTH]
        return sanitise_error_message(truncated)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Mirrors :meth:`SourceService._open_uow` / :meth:`InboxService._open_uow`
        — wrapping the optional factory in a context manager keeps the
        commit helpers linear regardless of whether the caller passed
        a ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
