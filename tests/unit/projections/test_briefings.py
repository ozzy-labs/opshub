"""Unit tests for :class:`opshub.projections.briefings.BriefingsProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``briefings`` table is created via :meth:`Table.create` on a tmp-path
SQLite file so the test does not depend on migration ordering — the
migration smoke test (``tests/integration/test_phase5_migrations.py``)
covers that side separately.

Pinned contracts:

* :class:`~opshub.domain.events.BriefingGenerated` materialises one
  row per briefing ULID.
* Re-applying the same event on rebuild is a no-op (PK upsert).
* :class:`~opshub.domain.events.BriefingRequested` and
  :class:`~opshub.domain.events.BriefingFailed` are events-table-only
  (mirrors the Phase 2 lock-bracket handling) — they MUST NOT write
  to ``briefings``.
* ``source_refs`` round-trips through the JSON column as a list of
  two-element sequences.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import (
    BriefingFailed,
    BriefingGenerated,
    BriefingRequested,
    TaskCreated,
)
from opshub.projections.briefings import BriefingsProjection, briefings_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``briefings`` table provisioned.

    Hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the migration integration
    test covers the migration path explicitly.
    """
    db_path = tmp_path / "briefings.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    briefings_table.create(db_engine)
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


def _generated_event(
    *,
    briefing_id: str,
    occurred_at: datetime,
    topic: str = "ship phase 5",
    scope: str = "all",
    markdown: str = "# Briefing\n\nBody.",
    source_refs: list[tuple[str, str]] | None = None,
    model_id: str = "claude-haiku-4-5-20251001",
    model_version: str = "20251001",
    tokens_in: int = 1200,
    tokens_out: int = 350,
) -> BriefingGenerated:
    """Build a representative :class:`BriefingGenerated` event."""
    refs = source_refs if source_refs is not None else [("task", new_ulid())]
    return BriefingGenerated(
        aggregate_id=briefing_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="test",
        briefing_id=briefing_id,
        topic=topic,
        scope=scope,
        markdown=markdown,
        source_refs=refs,
        model_id=model_id,
        model_version=model_version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


# ---- BriefingGenerated materialises one row -------------------------------


def test_briefing_generated_applies_row(engine: Engine) -> None:
    """A single :class:`BriefingGenerated` writes one fully-populated row."""
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    source_refs = [("task", new_ulid()), ("decision", new_ulid())]
    event = _generated_event(
        briefing_id=briefing_id,
        occurred_at=occurred,
        topic="phase 5 status",
        scope="all",
        markdown="# Phase 5\n\nProgress so far...",
        source_refs=source_refs,
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=1500,
        tokens_out=420,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(briefings_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == briefing_id
    assert row["topic"] == "phase 5 status"
    assert row["scope"] == "all"
    assert row["markdown"] == "# Phase 5\n\nProgress so far..."
    # ``sa.JSON`` round-trips tuples as two-element lists; we compare
    # against that shape rather than the source ``list[tuple[...]]``.
    assert row["source_refs"] == [list(ref) for ref in source_refs]
    assert row["model_id"] == "claude-haiku-4-5-20251001"
    assert row["model_version"] == "20251001"
    assert row["tokens_in"] == 1500
    assert row["tokens_out"] == 420
    assert row["generated_at"] == _expected_storage(occurred)


# ---- Idempotency: replaying the same event must not duplicate -------------


def test_briefing_generated_is_idempotent(engine: Engine) -> None:
    """Re-applying the same event collapses onto the existing row.

    The rebuild driver replays from a freshly ``reset``-ed table, but
    the projection's upsert is what guarantees rebuild does not raise
    on the PK collision even when the same event is applied twice in
    one pass (test harness or future catch-up code).
    """
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = _generated_event(briefing_id=new_ulid(), occurred_at=occurred)

    with engine.begin() as conn:
        projection.apply(conn, event)
        projection.apply(conn, event)  # second apply must be a no-op

    with engine.connect() as conn:
        rows = conn.execute(select(briefings_table)).mappings().all()
    assert len(rows) == 1, "re-applying the same event must not duplicate the row"
    assert rows[0]["id"] == event.aggregate_id
    assert rows[0]["markdown"] == event.markdown


# ---- BriefingRequested / BriefingFailed are events-table-only -------------


def test_briefing_requested_does_not_write_to_briefings(engine: Engine) -> None:
    """:class:`BriefingRequested` is a bracket event — projection stays empty."""
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    requested = BriefingRequested(
        aggregate_id=briefing_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        briefing_id=briefing_id,
        topic="phase 5 status",
        scope="all",
        requested_by="cli:brief",
    )

    with engine.begin() as conn:
        projection.apply(conn, requested)

    with engine.connect() as conn:
        rows = conn.execute(select(briefings_table)).all()
    assert rows == [], "BriefingRequested must not write to briefings"


def test_briefing_failed_does_not_write_to_briefings(engine: Engine) -> None:
    """:class:`BriefingFailed` is diagnostic-only — projection stays empty.

    No markdown body was produced, so there is nothing to project;
    the failure record itself lives in the ``events`` table.
    """
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    failed = BriefingFailed(
        aggregate_id=briefing_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        briefing_id=briefing_id,
        topic="phase 5 status",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        error_message="rate limited",
    )

    with engine.begin() as conn:
        projection.apply(conn, failed)

    with engine.connect() as conn:
        rows = conn.execute(select(briefings_table)).all()
    assert rows == [], "BriefingFailed must not write to briefings"


# ---- source_refs round-trips through JSON ---------------------------------


def test_source_refs_round_trip(engine: Engine) -> None:
    """The list-of-tuples ``source_refs`` survives the JSON write/read cycle.

    SQLAlchemy's :class:`~sqlalchemy.JSON` type serialises tuples as
    two-element JSON arrays; reading them back yields lists. The
    projection layer treats the value as opaque, so consumers that
    care about tuple identity must materialise it themselves — pinned
    by comparing against ``[list(ref) for ref in source_refs]``.
    """
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    refs = [
        ("task", "01HA000000000000000000AAAA"),
        ("decision", "01HA000000000000000000AAAB"),
        ("source", "01HA000000000000000000AAAC"),
    ]
    event = _generated_event(
        briefing_id=new_ulid(),
        occurred_at=occurred,
        source_refs=refs,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(briefings_table)).mappings().one()
    assert row["source_refs"] == [list(ref) for ref in refs]


def test_source_refs_empty_list_round_trips(engine: Engine) -> None:
    """An empty ``source_refs`` list survives the JSON round-trip.

    A briefing built without any matched entity (e.g. ``recall``
    returned nothing) MUST still project — the projection contract
    does not require at least one source ref.
    """
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = _generated_event(
        briefing_id=new_ulid(),
        occurred_at=occurred,
        source_refs=[],
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(briefings_table)).mappings().one()
    assert row["source_refs"] == []


# ---- Unrelated events / reset ---------------------------------------------


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = BriefingsProjection()
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
        rows = conn.execute(select(briefings_table)).all()
    assert rows == [], "task events must not produce briefings rows"


def test_reset_clears_every_row(engine: Engine) -> None:
    """``reset`` is the rebuild driver's pre-replay hook; it must empty the table."""
    projection = BriefingsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for _ in range(3):
            event = _generated_event(briefing_id=new_ulid(), occurred_at=occurred)
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(briefings_table)).all()
    assert remaining == []
