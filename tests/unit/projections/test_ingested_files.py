"""Unit tests for :class:`opshub.projections.ingested_files.IngestedFilesProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``ingested_files`` table is created via :meth:`Table.create` on a
tmp-path SQLite file so the test does not depend on migration
ordering — the migration smoke test covers that side separately.

The reducer's contract is upsert-by-``content_hash``: re-ingest of
identical content collapses onto a single row while preserving
``inbox_item_id`` from the first observation. ``file_path`` and
``ingested_at`` refresh because renames / re-touches are legitimate.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import (
    FileIngested,
    TaskCreated,
)
from opshub.projections.ingested_files import (
    IngestedFilesProjection,
    ingested_files_table,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``ingested_files`` table provisioned.

    We hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the integration test
    covers the migration path explicitly.
    """
    db_path = tmp_path / "ingested_files.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    ingested_files_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when
    the column is ``DateTime(timezone=True)``: the stored ISO string
    round-trips as a naive datetime whose components reflect UTC.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


_HASH_A = "a" * 64
_HASH_B = "b" * 64


# ---- FileIngested: first ingest -------------------------------------------


def test_file_ingested_inserts_new_row(engine: Engine) -> None:
    projection = IngestedFilesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    inbox_id = new_ulid()
    event = FileIngested(
        aggregate_id=_HASH_A,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/note.md",
        content_hash=_HASH_A,
        inbox_item_id=inbox_id,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(ingested_files_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["content_hash"] == _HASH_A
    assert row["file_path"] == "workspace/inbox/note.md"
    assert row["inbox_item_id"] == inbox_id
    assert row["ingested_at"] == _expected_storage(occurred)


# ---- FileIngested: re-ingest upserts the row -------------------------------


def test_file_ingested_again_updates_in_place(engine: Engine) -> None:
    """Re-ingest of the same content collapses onto a single row.

    Critically, ``inbox_item_id`` survives the upsert so external
    references to the first-observation inbox item stay valid;
    ``file_path`` and ``ingested_at`` track the latest observation
    (a rename / re-touch is the legitimate use case).
    """
    projection = IngestedFilesProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=2)
    first_inbox = new_ulid()
    second_inbox = new_ulid()
    assert first_inbox != second_inbox

    first = FileIngested(
        aggregate_id=_HASH_A,
        occurred_at=t0,
        recorded_at=t0,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/original.md",
        content_hash=_HASH_A,
        inbox_item_id=first_inbox,
    )
    second = FileIngested(
        # The aggregate_id (= content_hash) is identical so this is a
        # legitimate replay / rename. We pass a *different* inbox_item_id
        # to prove the reducer keeps the first one.
        aggregate_id=_HASH_A,
        occurred_at=t1,
        recorded_at=t1,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/renamed.md",
        content_hash=_HASH_A,
        inbox_item_id=second_inbox,
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        rows = conn.execute(select(ingested_files_table)).mappings().all()
    assert len(rows) == 1, "re-ingest of identical content must not duplicate the row"
    row = rows[0]
    # First-observation identity preserved.
    assert row["content_hash"] == _HASH_A
    assert row["inbox_item_id"] == first_inbox
    # Metadata columns refreshed.
    assert row["file_path"] == "workspace/inbox/renamed.md"
    assert row["ingested_at"] == _expected_storage(t1)


def test_file_ingested_different_hash_inserts_separate_row(engine: Engine) -> None:
    """A different ``content_hash`` is a separate file."""
    projection = IngestedFilesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    first = FileIngested(
        aggregate_id=_HASH_A,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/a.md",
        content_hash=_HASH_A,
        inbox_item_id=new_ulid(),
    )
    second = FileIngested(
        aggregate_id=_HASH_B,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/b.md",
        content_hash=_HASH_B,
        inbox_item_id=new_ulid(),
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        rows = conn.execute(select(ingested_files_table)).mappings().all()
    assert len(rows) == 2
    hashes = {r["content_hash"] for r in rows}
    assert hashes == {_HASH_A, _HASH_B}


# ---- unrelated events -----------------------------------------------------


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = IngestedFilesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    task_created = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated",
    )

    with engine.begin() as conn:
        projection.apply(conn, task_created)

    with engine.connect() as conn:
        rows = conn.execute(select(ingested_files_table)).all()
    assert rows == [], "task events must not produce ingested_files rows"


# ---- reset ----------------------------------------------------------------


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = IngestedFilesProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for i in range(3):
            # Build a distinct 64-char hex hash for each event (the
            # event model enforces exactly 64 chars).
            content_hash = (f"{i:02x}" * 32)[:64]
            event = FileIngested(
                aggregate_id=content_hash,
                occurred_at=t0,
                recorded_at=t0,
                actor="cli:workspace_ingest",
                file_path=f"workspace/inbox/{i}.md",
                content_hash=content_hash,
                inbox_item_id=new_ulid(),
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(ingested_files_table)).all()
    assert remaining == []
