"""Unit tests for :class:`opshub.projections.sources.SourcesProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``sources`` table is created via :meth:`Table.create` on a tmp-path
SQLite file so the test does not depend on migration ordering — the
migration smoke test covers that side separately.

The reducer's contract is upsert-by-``(connector_name, external_id)``:
re-observations of the same external item collapse onto a single row
while preserving ``id`` and ``observed_at`` from the first observation.
The tests below pin both halves of that contract.
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
    SourceObserved,
    SourceReferenced,
    TaskCreated,
)
from opshub.projections.sources import SourcesProjection, sources_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``sources`` table provisioned.

    We hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the integration test
    covers the migration path explicitly.
    """
    db_path = tmp_path / "sources.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    sources_table.create(db_engine)
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


# ---- SourceObserved: first observation ------------------------------------


def test_source_observed_inserts_new_row(engine: Engine) -> None:
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    source_id = new_ulid()
    event = SourceObserved(
        aggregate_id=source_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="Fix the thing",
        url="https://github.com/owner/repo/issues/42",
        summary="It is broken.",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(sources_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == source_id
    assert row["connector_name"] == "github"
    assert row["external_id"] == "owner/repo#42"
    assert row["source_type"] == "issue"
    assert row["title"] == "Fix the thing"
    assert row["url"] == "https://github.com/owner/repo/issues/42"
    assert row["summary"] == "It is broken."
    assert row["observed_at"] == _expected_storage(occurred)
    assert row["updated_at"] == _expected_storage(occurred)


def test_source_observed_allows_null_url_and_summary(engine: Engine) -> None:
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="bare",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["url"] is None
    assert row["summary"] is None


# ---- SourceObserved: re-observation upserts the row -----------------------


def test_source_observed_again_updates_in_place(engine: Engine) -> None:
    """Re-observation of the same natural key must UPDATE, not INSERT.

    Critically, the original ``id`` and ``observed_at`` must survive so
    references already minted against the first observation keep
    resolving; ``title`` / ``url`` / ``summary`` / ``updated_at`` track
    the latest observation.
    """
    projection = SourcesProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=2)
    original_id = new_ulid()
    later_id = new_ulid()
    assert original_id != later_id

    first = SourceObserved(
        aggregate_id=original_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="original title",
        url="https://example.com/v1",
        summary="v1",
    )
    second = SourceObserved(
        # Use a different aggregate_id to prove the projector keeps the
        # first-observation id even if the caller hands a fresh ULID.
        aggregate_id=later_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="updated title",
        url="https://example.com/v2",
        summary="v2",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        rows = conn.execute(select(sources_table)).mappings().all()
    assert len(rows) == 1, "re-observation must not insert a duplicate row"
    row = rows[0]
    # Identity columns preserved from the first observation.
    assert row["id"] == original_id
    assert row["observed_at"] == _expected_storage(t0)
    # Metadata columns reflect the latest observation.
    assert row["title"] == "updated title"
    assert row["url"] == "https://example.com/v2"
    assert row["summary"] == "v2"
    assert row["updated_at"] == _expected_storage(t1)


def test_source_observed_different_natural_key_inserts_separate_row(
    engine: Engine,
) -> None:
    """A different ``(connector_name, external_id)`` is a separate item."""
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    first = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="issue 42",
    )
    second_same_external_diff_connector = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="slack",
        external_id="owner/repo#42",
        source_type="message",
        title="unrelated slack message",
    )
    third_same_connector_diff_external = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        external_id="owner/repo#43",
        source_type="issue",
        title="issue 43",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second_same_external_diff_connector)
        projection.apply(conn, third_same_connector_diff_external)

    with engine.connect() as conn:
        rows = conn.execute(select(sources_table)).mappings().all()
    assert len(rows) == 3
    keys = {(r["connector_name"], r["external_id"]) for r in rows}
    assert keys == {
        ("github", "owner/repo#42"),
        ("slack", "owner/repo#42"),
        ("github", "owner/repo#43"),
    }


# ---- fingerprint (Phase 9 step A2, ADR-0019 §決定 (d)) -------------------


def test_source_observed_without_fingerprint_writes_null(engine: Engine) -> None:
    """The four pre-existing connectors omit ``fingerprint`` and stay byte-identical.

    ADR-0019 §決定 (d) Validation: the projector must persist
    ``None`` as ``NULL`` so the ``github`` / ``slack`` / ``ms365`` /
    ``box`` connectors — which never populate the field — round-trip
    bit-for-bit identical to the Phase 8 read model.
    """
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="legacy connector",
    )
    assert event.fingerprint is None, (
        "default fingerprint must remain None — backward-compat invariant"
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["fingerprint"] is None, "None must round-trip as SQL NULL"


def test_source_observed_with_fingerprint_writes_value(engine: Engine) -> None:
    """The ``box_drive`` connector's ``f"{size}:{mtime_ns}"`` must persist verbatim.

    Phase 9 step A2 pin: the projector copies ``event.fingerprint``
    onto the row both on INSERT and the UPDATE arm of the upsert.
    The string format itself (``"<size>:<mtime_ns>"``) is the
    connector's contract — the projection just stores opaque text.
    """
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    fingerprint = "100:1234567890"
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="connector:box_drive",
        connector_name="box_drive",
        external_id="docs/spec.md",
        source_type="box_drive_file",
        title="docs/spec.md",
        url="file:///mnt/b/docs/spec.md",
        summary="path: docs/spec.md",
        fingerprint=fingerprint,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["fingerprint"] == fingerprint


def test_source_observed_refresh_updates_fingerprint(engine: Engine) -> None:
    """Re-observation must refresh ``fingerprint`` (set_ arm of the upsert).

    The ``box_drive`` scanner relies on the latest stat() value being
    persisted so the *next* scan can compare against it and skip
    unchanged files (ADR-0019 §決定 (d) step 1). If the upsert kept
    the original fingerprint frozen on UPDATE, the scanner would
    never observe drift after the first sync.
    """
    projection = SourcesProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    first = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=t0,
        recorded_at=t0,
        actor="connector:box_drive",
        connector_name="box_drive",
        external_id="docs/spec.md",
        source_type="box_drive_file",
        title="docs/spec.md",
        fingerprint="100:1000000000",
    )
    second = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=t1,
        recorded_at=t1,
        actor="connector:box_drive",
        connector_name="box_drive",
        external_id="docs/spec.md",
        source_type="box_drive_file",
        title="docs/spec.md",
        fingerprint="200:2000000000",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["fingerprint"] == "200:2000000000"


# ---- SourceReferenced and unrelated events --------------------------------


def test_source_referenced_is_a_no_op(engine: Engine) -> None:
    """The reducer must ignore :class:`SourceReferenced`.

    Links will be materialised by a Phase 4 ``links`` projection;
    storing them on ``sources_table`` would duplicate that read model.
    """
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    referenced = SourceReferenced(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        entity_type="task",
        entity_id=new_ulid(),
    )

    with engine.begin() as conn:
        projection.apply(conn, referenced)

    with engine.connect() as conn:
        rows = conn.execute(select(sources_table)).all()
    assert rows == [], "SourceReferenced must not write to sources_table"


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = SourcesProjection()
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
        rows = conn.execute(select(sources_table)).all()
    assert rows == [], "task events must not produce sources rows"


# ---- reset ----------------------------------------------------------------


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = SourcesProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for i in range(3):
            event = SourceObserved(
                aggregate_id=new_ulid(),
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                connector_name="github",
                external_id=f"owner/repo#{i}",
                source_type="issue",
                title=f"row {i}",
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(sources_table)).all()
    assert remaining == []


# ---- Phase 10 (ADR-0020): body + provenance ------------------------------


def test_source_observed_persists_body_and_provenance(engine: Engine) -> None:
    """``body`` + provenance tags round-trip onto the row (ADR-0020 §(e))."""
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        external_id="owner/repo#7",
        source_type="issue",
        title="retained",
        summary="preview",
        body="the full untruncated issue body that ADR-0020 now retains",
        provenance_origin="external",
        provenance_trust="untrusted",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["body"] == "the full untruncated issue body that ADR-0020 now retains"
    assert row["provenance_origin"] == "external"
    assert row["provenance_trust"] == "untrusted"


def test_source_observed_body_defaults_to_null(engine: Engine) -> None:
    """A connector that omits ``body`` lands ``NULL`` (backward-compat, ADR-0020 §(d))."""
    projection = SourcesProjection()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="box_drive",
        external_id="projects/spec.md",
        source_type="box_drive_file",
        title="projects/spec.md",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["body"] is None
    assert row["provenance_origin"] is None
    assert row["provenance_trust"] is None


def test_reobservation_refreshes_body_and_provenance(engine: Engine) -> None:
    """Re-observing an edited item updates ``body`` while keeping the first ``id``."""
    projection = SourcesProjection()
    t0 = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    first_id = new_ulid()
    first = SourceObserved(
        aggregate_id=first_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        connector_name="slack",
        external_id="C1:1700000000.0001",
        source_type="slack_message",
        title="alice in #general",
        body="original message text",
        provenance_origin="external",
        provenance_trust="untrusted",
    )
    second = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=t0 + timedelta(minutes=5),
        recorded_at=t0 + timedelta(minutes=5),
        actor="test",
        connector_name="slack",
        external_id="C1:1700000000.0001",
        source_type="slack_message",
        title="alice in #general",
        body="edited message text",
        provenance_origin="external",
        provenance_trust="untrusted",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        row = conn.execute(select(sources_table)).mappings().one()
    assert row["id"] == first_id  # first-observation id preserved
    assert row["body"] == "edited message text"  # body refreshed on re-observe
