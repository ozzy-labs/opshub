"""Workspace file ingest service (Phase 3 step C2, ADR-0002, ADR-0004).

:class:`FileIngestService` is the entry point for the workspace file
ingest pipeline. It scans ``workspace/inbox/*.md`` (immediate children
only — sub-directories are deliberately out of scope), parses each
file via :func:`opshub.markdown.ingest.parse_inbox_file`, and turns
**new** content into a pair of events committed atomically:

1. :class:`opshub.domain.events.inbox.ItemEnqueued` — the inbox row for
   the freshly captured note. This is the same event shape connector
   sync paths emit (see :class:`opshub.services.source_service.SourceService.observe`),
   so workspace-origin items show up in ``opshub inbox list`` next to
   connector-origin items with no special casing downstream.
2. :class:`opshub.domain.events.file_ingest.FileIngested` — records the
   ``content_hash`` so subsequent scans of the same directory skip the
   file when its body has not changed. ``inbox_item_id`` carries the
   ULID of the ItemEnqueued aggregate so a future "find the inbox row
   for this file" query can resolve in one hop.

Composition with :class:`InboxService`
--------------------------------------

The service holds an :class:`InboxService` reference for two reasons —
mirroring the rationale that :mod:`opshub.services.source_service`
documents for its own ``inbox_service`` field:

1. It makes the dependency explicit in the CLI wiring graph (workspace
   ingest is fundamentally an inbox producer).
2. Future enhancements (e.g. suppressing the inbox row for files
   destined to skip triage) will reuse :class:`InboxService` read
   primitives.

The :class:`ItemEnqueued` event is constructed **inline** in
:meth:`_commit_one`, *not* by calling :meth:`InboxService.enqueue`. The
two reasons are:

* :meth:`InboxService.enqueue` opens its own UoW (via the inbox
  service's ``uow_factory``), which would defeat the single-transaction
  atomicity contract this service inherits from PR #26.
* :class:`SourceService.observe` already uses the same inline-build
  pattern for the same reason (PR #47). Keeping both services on the
  same shape avoids cross-service signature changes and matches the
  reviewer expectations established in step A4.

Idempotency
-----------

:meth:`ingest_inbox_dir` consults the ``ingested_files`` projection
(via the supplied :class:`~sqlalchemy.engine.Engine`) for the set of
content hashes already ingested. Files whose ``content_hash`` is in
that set are skipped — no events are appended, the file is reported in
the result as "skipped". The first ingest of a file's content always
emits both events; replays / forced re-ingests are not the service's
concern.

Atomicity (matches :class:`SourceService.observe`)
--------------------------------------------------

When ``uow_factory`` is supplied, every commit opens a single Unit of
Work, threads the connection through both ``store.append`` and
``projector.apply`` for both events, and commits once both succeed. A
failure mid-batch rolls back both events. Without a factory (in-memory
test stack), the service falls back to the historical "append then
apply, no transaction" path — adequate for unit tests, never used in
production.

Engine requirement
------------------

Unlike the other Phase 3 services, the ``engine`` argument is
**required**: the service has to read the ``ingested_files`` projection
on every scan to know which files to skip. Service unit tests that
exercise just :meth:`ingest_inbox_dir` need a migrated SQLite engine;
the in-memory event-store path is reserved for the commit-side tests
that do not call :meth:`ingest_inbox_dir` (e.g. the failing-projector
atomicity test that drives the service via the lower-level
:meth:`_commit_one` helper).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.ids import new_ulid
from opshub.domain.events import FileIngested, ItemEnqueued
from opshub.markdown.ingest import InboxItemDraft, parse_inbox_file
from opshub.projections.ingested_files import ingested_files_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.services.inbox_service import InboxService


_DEFAULT_ACTOR = "cli:workspace_ingest"


__all__ = ["FileIngestResult", "FileIngestService"]


@dataclass(frozen=True, slots=True)
class FileIngestResult:
    """Outcome of one :meth:`FileIngestService.ingest_inbox_dir` invocation.

    Attributes
    ----------
    enqueued_count:
        Number of files whose content was new and was enqueued as a
        fresh inbox item (one ItemEnqueued + one FileIngested per
        file).
    skipped_count:
        Number of files whose ``content_hash`` matched a prior ingest;
        no events were appended for these.
    enqueued_paths:
        Paths of the files actually enqueued in the order they were
        processed (sorted lexicographically). The CLI surfaces this so
        the user sees exactly which notes were captured.
    """

    enqueued_count: int
    skipped_count: int
    enqueued_paths: list[Path]


class FileIngestService:
    """Scan ``workspace/inbox/*.md`` and ingest new files into the event log.

    Parameters
    ----------
    store:
        Append target. Only the :class:`EventStore` Protocol is required.
    projector:
        Read-model updater. Called with each event in append order on
        the same connection ``store.append`` was called with.
    inbox_service:
        Sibling service held for composition / wiring symmetry. The
        :class:`ItemEnqueued` event is built **inline** here, never via
        :meth:`InboxService.enqueue`, so the shared UoW is not broken
        (see module docstring).
    engine:
        Required SQLAlchemy :class:`~sqlalchemy.engine.Engine` used to
        read the ``ingested_files`` projection on every scan. Unlike
        the other Phase 3 services, there is no in-memory fallback —
        the service has to query the projection to know what to skip.
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`.
        When supplied, every commit runs ``store.append`` and
        ``projector.apply`` on the same connection inside a single
        transaction.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:workspace_ingest"`` — the workspace ingest path is
        always operator-driven, never connector-driven.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        inbox_service: InboxService,
        engine: Engine,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._store = store
        self._projector = projector
        self._inbox_service = inbox_service
        self._engine = engine
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ commands

    def ingest_inbox_dir(self, workspace_root: Path) -> FileIngestResult:
        """Scan ``<workspace_root>/inbox/`` for ``*.md`` files and ingest new ones.

        Only immediate children of ``<workspace_root>/inbox/`` with a
        ``.md`` extension are considered — sub-directories and
        non-markdown files are silently skipped (they are not part of
        the Phase 3 ingest contract; see phase-3-plan §1 確定済み事項
        #3). When ``<workspace_root>/inbox`` does not exist or is not
        a directory, the method returns an empty
        :class:`FileIngestResult` rather than raising — the caller can
        treat "no inbox" the same as "no new files".

        Returns a :class:`FileIngestResult` summarising enqueued vs
        skipped files.
        """
        inbox_dir = workspace_root / "inbox"
        if not inbox_dir.is_dir():
            return FileIngestResult(
                enqueued_count=0,
                skipped_count=0,
                enqueued_paths=[],
            )
        known_hashes = self._load_known_hashes()
        enqueued: list[Path] = []
        skipped = 0
        for path in sorted(inbox_dir.glob("*.md")):
            # ``glob("*.md")`` already filters by extension, but
            # ``is_file()`` guards against ``a-directory-named.md/``.
            if not path.is_file():
                continue
            draft = parse_inbox_file(path)
            if draft.content_hash in known_hashes:
                skipped += 1
                continue
            self._commit_one(draft)
            enqueued.append(path)
            # Track newly-ingested hashes so a duplicate file body
            # appearing twice in the same scan is only ingested once.
            known_hashes.add(draft.content_hash)
        return FileIngestResult(
            enqueued_count=len(enqueued),
            skipped_count=skipped,
            enqueued_paths=enqueued,
        )

    # ------------------------------------------------------------------ helpers

    def _load_known_hashes(self) -> set[str]:
        """Read the set of ``content_hash`` values already in the projection.

        Returns an empty set when the projection has no rows yet (first
        ingest on a fresh database). The query is keyed only on the PK
        so it is O(rows) regardless of file_path / ingested_at; for a
        typical inbox (a few hundred files) this is well below the
        cost of the subsequent file I/O.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(select(ingested_files_table.c.content_hash)).all()
        return {row[0] for row in rows}

    def _commit_one(self, draft: InboxItemDraft) -> None:
        """Append :class:`ItemEnqueued` + :class:`FileIngested` in one UoW.

        The pair commits atomically: a failure in either ``append`` or
        ``apply`` rolls both events back, so the ``ingested_files``
        projection never claims a content hash that does not also
        appear in the ``inbox_items`` projection.

        ``inbox_item_id`` on the :class:`FileIngested` event is set to
        the freshly-minted ULID on the :class:`ItemEnqueued` event so a
        future "where did this file go?" query resolves in one hop.

        The :class:`ItemEnqueued` event is built inline here (rather
        than via :meth:`InboxService.enqueue`) so the outer UoW stays
        intact — see module docstring for the rationale and the
        precedent set by :class:`SourceService.observe`.
        """
        inbox_event = ItemEnqueued(
            aggregate_id=new_ulid(),
            actor=self._actor,
            summary=draft.summary,
            source_ref=draft.source_ref,
        )
        file_event = FileIngested(
            aggregate_id=draft.content_hash,
            actor=self._actor,
            file_path=str(draft.path),
            content_hash=draft.content_hash,
            inbox_item_id=inbox_event.aggregate_id,
        )
        self._commit([inbox_event, file_event])

    def _commit(self, events: list[DomainEvent]) -> None:
        """Append and project a (possibly multi-event) batch atomically.

        Mirrors :meth:`SourceService._commit` and
        :meth:`InboxService._commit`: with a ``uow_factory`` every
        event in ``events`` is appended and projected on the same
        connection; a failure anywhere rolls back the whole batch.
        Without a factory each ``store.append`` / ``projector.apply``
        pair runs on whatever transaction the implementation opens
        internally — adequate for in-memory unit tests, never used in
        production.
        """
        with self._open_uow() as connection:
            for event in events:
                self._store.append(event, connection)
                self._projector.apply(event, connection)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Mirrors :meth:`SourceService._open_uow` — wrapping the optional
        factory in a context manager keeps :meth:`_commit` linear
        regardless of whether the caller passed a ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
