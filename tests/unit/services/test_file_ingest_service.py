"""Tests for :class:`opshub.services.file_ingest_service.FileIngestService`.

The unit suite drives the service through a migrated SQLite engine
(the same ``migrated_engine`` fixture pattern used by
:mod:`tests.unit.services.test_source_service`) because the service
needs to read the ``ingested_files`` projection on every scan. A
recording / failing projector wraps the real
:class:`IngestedFilesProjection` so the atomicity contract is observed
end-to-end without going through the CLI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import (
    DomainEvent,
    FileIngested,
    ItemEnqueued,
)
from opshub.projections.ingested_files import (
    IngestedFilesProjection,
    ingested_files_table,
)
from opshub.services.file_ingest_service import (
    FileIngestResult,
    FileIngestService,
)
from opshub.services.inbox_service import InboxService
from opshub.services.projector import NoOpProjector

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "file_ingest.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _IngestedFilesProjector:
    """Projector that applies the ingested_files projection on the shared UoW.

    The service's atomicity tests need a projector that actually
    writes to the ``ingested_files`` table — otherwise the
    "skip-by-hash" path on the next scan would not see anything to
    skip. This double mirrors the ``_CursorOnlyProjector`` pattern in
    :mod:`tests.unit.services.test_source_service`.
    """

    def __init__(self) -> None:
        self._projection = IngestedFilesProjection()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("connection is required for atomic apply")
        self._projection.apply(connection, event)


class _AtSecondCallFailingProjector:
    """Projector that succeeds on the first ``_commit_one`` then fails.

    Used to verify that a mid-scan failure rolls back **only** the
    second file's events (the first file's commit already completed).
    Each :meth:`FileIngestService._commit_one` call produces a pair of
    events (ItemEnqueued + FileIngested) — so this projector counts
    events and trips on the third one, i.e. the first event of the
    second file's commit batch.
    """

    def __init__(self) -> None:
        self._projection = IngestedFilesProjection()
        self.applied = 0

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("connection is required for atomic apply")
        self.applied += 1
        if self.applied > 2:
            raise RuntimeError("simulated mid-scan projector failure")
        # First file's pair (events 1+2) project normally so the
        # ingested_files row materialises.
        self._projection.apply(connection, event)


def _make_service(
    migrated_engine: Engine,
    *,
    projector: object | None = None,
) -> FileIngestService:
    """Build a :class:`FileIngestService` wired against the migrated DB."""
    store = SqlAlchemyEventStore(migrated_engine)
    projector = projector if projector is not None else _IngestedFilesProjector()
    inbox = InboxService(
        store=store,
        projector=NoOpProjector(),
        uow_factory=migrated_engine.begin,
    )
    return FileIngestService(
        store=store,
        projector=projector,  # type: ignore[arg-type]
        inbox_service=inbox,
        engine=migrated_engine,
        uow_factory=migrated_engine.begin,
    )


def _write_md(path: Path, body: str) -> None:
    """Write ``body`` to ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---- ingest_inbox_dir: happy path -----------------------------------------


def test_ingest_inbox_dir_enqueues_new_files(migrated_engine: Engine, tmp_path: Path) -> None:
    """Two .md files → 2 enqueued / 0 skipped + 4 events in the store."""
    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "one.md", "---\nsummary: First\n---\nbody one\n")
    _write_md(inbox_dir / "two.md", "# Second heading\n\nbody two\n")

    result = service.ingest_inbox_dir(tmp_path / "workspace")

    assert isinstance(result, FileIngestResult)
    assert result.enqueued_count == 2
    assert result.skipped_count == 0
    assert {p.name for p in result.enqueued_paths} == {"one.md", "two.md"}

    # Event log: 2 files x (ItemEnqueued + FileIngested) = 4 events.
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(events_table.c.event_type)).all()
    event_types = [row[0] for row in rows]
    assert event_types.count("inbox.enqueued") == 2
    assert event_types.count("workspace.file_ingested") == 2


def test_ingest_inbox_dir_returns_paths_sorted_lexicographically(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """``enqueued_paths`` follows the deterministic glob-then-sort order."""
    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "c.md", "# c\n")
    _write_md(inbox_dir / "a.md", "# a\n")
    _write_md(inbox_dir / "b.md", "# b\n")

    result = service.ingest_inbox_dir(tmp_path / "workspace")

    assert [p.name for p in result.enqueued_paths] == ["a.md", "b.md", "c.md"]


# ---- ingest_inbox_dir: idempotency / skip -----------------------------------


def test_ingest_inbox_dir_skips_known_content_hashes(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """A second ingest of the same files is a complete no-op."""
    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "one.md", "---\nsummary: Stable\n---\nstable body\n")
    _write_md(inbox_dir / "two.md", "# Second\n")

    first = service.ingest_inbox_dir(tmp_path / "workspace")
    assert first.enqueued_count == 2

    second = service.ingest_inbox_dir(tmp_path / "workspace")
    assert second.enqueued_count == 0
    assert second.skipped_count == 2
    assert second.enqueued_paths == []

    # Event log still holds only the first scan's 4 events.
    with migrated_engine.connect() as conn:
        total = conn.execute(select(events_table)).all()
    assert len(total) == 4


def test_ingest_inbox_dir_skips_files_already_in_projection(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """Pre-populating the projection short-circuits the file ingest.

    We seed the ``ingested_files`` projection directly (bypassing the
    event log) with a row matching the SHA-256 of the file body. The
    service must read that projection and skip — no events emitted.
    """
    import hashlib
    from datetime import UTC, datetime

    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    body = "---\nsummary: Pre-known\n---\nseeded body\n"
    _write_md(inbox_dir / "seeded.md", body)

    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with migrated_engine.begin() as conn:
        conn.execute(
            insert(ingested_files_table).values(
                content_hash=content_hash,
                file_path="any/old/path.md",
                inbox_item_id="01HA00000000000000000000AA",
                ingested_at=datetime.now(UTC),
            )
        )

    result = service.ingest_inbox_dir(tmp_path / "workspace")

    assert result.enqueued_count == 0
    assert result.skipped_count == 1
    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
    assert events == [], "no events should be appended when the hash is pre-known"


# ---- ingest_inbox_dir: edge cases ------------------------------------------


def test_ingest_inbox_dir_handles_missing_directory(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """``workspace/inbox`` missing → returns 0/0/[] without raising."""
    service = _make_service(migrated_engine)
    # tmp_path has no ``workspace/inbox`` subdirectory.
    result = service.ingest_inbox_dir(tmp_path / "workspace")

    assert result.enqueued_count == 0
    assert result.skipped_count == 0
    assert result.enqueued_paths == []


def test_ingest_inbox_dir_handles_inbox_as_a_file(migrated_engine: Engine, tmp_path: Path) -> None:
    """``workspace/inbox`` is a file (not a dir) → graceful no-op."""
    service = _make_service(migrated_engine)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inbox").write_text("not a dir", encoding="utf-8")

    result = service.ingest_inbox_dir(workspace)

    assert result.enqueued_count == 0
    assert result.skipped_count == 0
    assert result.enqueued_paths == []


def test_ingest_inbox_dir_ignores_non_markdown_files(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """Only ``*.md`` immediate children are considered."""
    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "kept.md", "# kept\n")
    _write_md(inbox_dir / "ignored.txt", "ignored")
    _write_md(inbox_dir / "ignored.log", "ignored")
    # Subdirectories are also out of scope.
    _write_md(inbox_dir / "nested" / "deep.md", "# deep\n")

    result = service.ingest_inbox_dir(tmp_path / "workspace")

    assert result.enqueued_count == 1
    assert [p.name for p in result.enqueued_paths] == ["kept.md"]


# ---- atomicity ------------------------------------------------------------


def test_ingest_inbox_dir_atomicity_rolls_back_failing_file(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """A mid-scan projector failure rolls back ONLY the failing file.

    The first file's pair (ItemEnqueued + FileIngested) commits
    normally. The second file's pair fails inside the same UoW the
    service opened for it, so neither of its events lands in the
    store — but the first file's events stay committed.
    """
    projector = _AtSecondCallFailingProjector()
    service = _make_service(migrated_engine, projector=projector)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "first.md", "# First\n")
    _write_md(inbox_dir / "second.md", "# Second\n")

    with pytest.raises(RuntimeError, match="simulated mid-scan projector failure"):
        service.ingest_inbox_dir(tmp_path / "workspace")

    # Store holds exactly the first file's pair — 2 events, both with
    # the expected event types.
    with migrated_engine.connect() as conn:
        types = [row[0] for row in conn.execute(select(events_table.c.event_type)).all()]
    assert sorted(types) == ["inbox.enqueued", "workspace.file_ingested"]

    # ingested_files projection mirrors that single commit (one row).
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(ingested_files_table)).all()
    assert len(rows) == 1


# ---- inbox_item_id link contract ------------------------------------------


def test_file_ingested_event_records_inbox_item_id(migrated_engine: Engine, tmp_path: Path) -> None:
    """``FileIngested.inbox_item_id`` equals the ItemEnqueued aggregate id.

    This is the file → inbox link contract: a future "find the inbox
    row that this file produced" query joins through
    ``ingested_files.inbox_item_id`` straight to
    ``inbox_items.id``. The test reads back the events from the
    event store and asserts the pair shares the right id.
    """
    from opshub.db.event_store import SqlAlchemyEventStore as _EventStore

    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "linked.md", "---\nsummary: Linked\n---\nbody\n")

    service.ingest_inbox_dir(tmp_path / "workspace")

    # Walk the event log and pair up the two events written for this file.
    store = _EventStore(migrated_engine)
    appended = list(store.iter_all())
    item_events = [e for e in appended if isinstance(e, ItemEnqueued)]
    file_events = [e for e in appended if isinstance(e, FileIngested)]
    assert len(item_events) == 1
    assert len(file_events) == 1
    assert file_events[0].inbox_item_id == item_events[0].aggregate_id
    # And the projection row carries the same link.
    with migrated_engine.connect() as conn:
        row = conn.execute(select(ingested_files_table)).mappings().one()
    assert row["inbox_item_id"] == item_events[0].aggregate_id


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_cli_workspace_ingest(migrated_engine: Engine, tmp_path: Path) -> None:
    """Default actor matches the wiring helper string."""
    service = _make_service(migrated_engine)
    inbox_dir = tmp_path / "workspace" / "inbox"
    _write_md(inbox_dir / "actor.md", "# A\n")

    service.ingest_inbox_dir(tmp_path / "workspace")

    store = SqlAlchemyEventStore(migrated_engine)
    for event in store.iter_all():
        assert event.actor == "cli:workspace_ingest"
