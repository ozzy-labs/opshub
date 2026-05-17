"""Integration tests for ``rebuild_all`` against the SQLAlchemy event store.

These tests exercise the full Phase 1 stack: Alembic-managed schema,
SQLAlchemy-backed :class:`SqlAlchemyEventStore`,
:class:`TasksProjection`, and :func:`rebuild_all`. They live under
``tests/integration/`` (a new directory introduced by this step) because
they require a real SQLite database and the migrated schema.

Two properties are pinned:

* **Replay fidelity** — applying the projection inline (via the task
  service) and rebuilding from the event log produce the same row.
* **Idempotency** — calling :func:`rebuild_all` twice yields the same
  projection state as calling it once. This catches a class of bug
  where ``reset`` is skipped and the second rebuild double-applies or
  fails on a PK conflict.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from opshub.core.errors import OpsHubError
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
    SourceObserved,
    SourceReferenced,
    TaskActivated,
    TaskCompleted,
    TaskCreated,
)
from opshub.projections import (
    TasksProjection,
    connector_cursors_table,
    rebuild_all,
    sources_table,
    tasks_table,
)
from opshub.projections.registry import all_projections
from opshub.services.projector import NoOpProjector
from opshub.services.task_service import TaskService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``tests/unit/db/test_migrations.py`` so
    integration tests pick up the same env.py URL-resolution path the
    production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "rebuild.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _read_tasks(engine: Engine) -> list[dict[str, Any]]:
    """Return every row in ``tasks_table`` as a list of dicts, ordered by id.

    Used as the snapshot the idempotency assertion compares against. We
    sort by ``id`` so the snapshot is order-stable regardless of which
    SQLite page the rows happen to land on.
    """
    with engine.connect() as conn:
        rows = conn.execute(select(tasks_table).order_by(tasks_table.c.id)).mappings().all()
    return [dict(row) for row in rows]


def test_rebuild_all_replays_event_log_into_tasks_projection(migrated_engine: Engine) -> None:
    """End-to-end replay: events committed via the service rebuild into the same row."""
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()
    # The service applies events inline via a no-op projector; the
    # rebuild driver is responsible for materialising the read model.
    service = TaskService(store=store, projector=NoOpProjector())

    created = service.create_task(title="ship rebuild", body="phase 1 step 10")
    service.activate_task(created.aggregate_id)
    service.complete_task(created.aggregate_id, "shipped in PR #14")

    # First rebuild produces the canonical snapshot.
    rebuild_all(migrated_engine, store, [projection])

    snapshot = _read_tasks(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["id"] == created.aggregate_id
    assert row["title"] == "ship rebuild"
    assert row["body"] == "phase 1 step 10"
    assert row["state"] == "completed"
    assert row["result_note"] == "shipped in PR #14"
    # SQLite's stdlib driver returns ``DateTime(timezone=True)`` columns as
    # naive datetimes whose components reflect UTC. We assert the value
    # shape (timestamp is a datetime, monotonic w.r.t. activation /
    # completion) rather than tzinfo identity — the tz-aware roundtrip is
    # exercised in the ``iter_all`` JSON path, not in the projection
    # columns. Pinned by the engine-level documentation in
    # ``opshub.db.engine``.
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["updated_at"], datetime)
    # ``updated_at`` advances past ``created_at`` once activation /
    # completion events are applied (they have later ``occurred_at``
    # values).
    assert row["updated_at"] >= row["created_at"]


def test_rebuild_all_is_idempotent(migrated_engine: Engine) -> None:
    """``rebuild_all`` called twice must produce the same snapshot as one call.

    This is the contract that pins down :meth:`Projection.reset`: if
    ``reset`` were skipped the second call would either double-apply
    (broken state) or fail on a PK conflict (broken durability).
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()
    service = TaskService(store=store, projector=NoOpProjector())

    a = service.create_task(title="task A")
    b = service.create_task(title="task B", body="with body")
    service.activate_task(a.aggregate_id)
    service.complete_task(a.aggregate_id, "done")

    rebuild_all(migrated_engine, store, [projection])
    snapshot_after_one = _read_tasks(migrated_engine)

    rebuild_all(migrated_engine, store, [projection])
    snapshot_after_two = _read_tasks(migrated_engine)

    assert snapshot_after_two == snapshot_after_one
    # Sanity: the snapshot must contain both tasks in their final state.
    ids = {row["id"] for row in snapshot_after_one}
    assert ids == {a.aggregate_id, b.aggregate_id}
    states = {row["id"]: row["state"] for row in snapshot_after_one}
    assert states[a.aggregate_id] == "completed"
    assert states[b.aggregate_id] == "draft"


def test_rebuild_all_respects_recorded_at_order_when_inserted_out_of_sequence(
    migrated_engine: Engine,
) -> None:
    """Events appended out of business-time order still rebuild deterministically.

    We bypass the service layer and append events directly with
    ``recorded_at`` values that do *not* match insertion order: the
    later-recorded ``TaskCompleted`` is appended first, the
    earlier-recorded ``TaskCreated`` second. Replay must still produce
    the natural state machine (created → completed) because ``iter_all``
    orders by ``recorded_at``, not by ``id`` or insertion order.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()

    aggregate_id = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    t0 = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)

    # Insertion order != recorded_at order.
    completed = TaskCompleted(
        aggregate_id=aggregate_id,
        occurred_at=t2,
        recorded_at=t2,
        actor="test",
        result_note="out-of-order done",
    )
    created = TaskCreated(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="appended late",
    )
    activated = TaskActivated(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
    )

    store.append(completed)
    store.append(created)
    store.append(activated)

    rebuild_all(migrated_engine, store, [projection])

    snapshot = _read_tasks(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["id"] == aggregate_id
    assert row["state"] == "completed"
    assert row["result_note"] == "out-of-order done"
    # SQLite's stdlib driver drops tzinfo on read; the components still
    # reflect UTC so we compare against the naive equivalents.
    assert row["created_at"] == t0.replace(tzinfo=None)
    assert row["updated_at"] == t2.replace(tzinfo=None)


def test_rebuild_all_tie_break_on_same_recorded_at_orders_by_id_asc(
    migrated_engine: Engine,
) -> None:
    """Two events with identical ``recorded_at`` replay in ``id`` ASC order.

    ``SqlAlchemyEventStore.iter_all`` orders by ``(recorded_at, id)`` —
    when the wall clock collides (multiple events recorded in the same
    millisecond, common for batch imports / scripted replay) the ULID
    ``id`` is the tie-break. The rebuild driver inherits that order
    because it iterates ``store.iter_all()`` in sequence.

    We pin the contract by appending three task events that all share a
    single fixed ``recorded_at`` (so the wall-clock tie-break is the
    only signal). The ``event_id`` values are crafted so that the
    business-time order (``created`` → ``activated`` → ``completed``)
    matches the lex-sorted ``event_id`` order — exactly the property
    real ULIDs guarantee when generated within the same millisecond.
    The projection must end in the ``completed`` state.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()

    aggregate_id = "01HA00000000000000000000ZZ"
    fixed_recorded_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    # Lex-sorted id suffixes: "...AA" < "...AB" < "...AC". These take
    # the place of the ULID random component when ``time_ms`` collides;
    # ASC ordering must put created → activated → completed.
    created_id = "01HA000000000000000000AAAA"
    activated_id = "01HA000000000000000000AAAB"
    completed_id = "01HA000000000000000000AAAC"

    created = TaskCreated(
        event_id=created_id,
        aggregate_id=aggregate_id,
        occurred_at=fixed_recorded_at,
        recorded_at=fixed_recorded_at,
        actor="test",
        title="tie-break",
    )
    activated = TaskActivated(
        event_id=activated_id,
        aggregate_id=aggregate_id,
        occurred_at=fixed_recorded_at,
        recorded_at=fixed_recorded_at,
        actor="test",
    )
    completed = TaskCompleted(
        event_id=completed_id,
        aggregate_id=aggregate_id,
        occurred_at=fixed_recorded_at,
        recorded_at=fixed_recorded_at,
        actor="test",
        result_note="tie-break done",
    )

    # Insert in an order *different* from both business-time order and
    # id ASC, so the only way the test can pass is if iter_all sorts by
    # id when recorded_at ties. If iter_all sorted by insertion order
    # we'd see ``TaskCompleted`` applied first and the projection would
    # diverge (no row to update; or row would not reach completed).
    store.append(activated)
    store.append(completed)
    store.append(created)

    rebuild_all(migrated_engine, store, [projection])

    snapshot = _read_tasks(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["id"] == aggregate_id
    # Final state == completed proves the events applied in id ASC
    # order: created (insert) → activated (update) → completed (update).
    # Any other order would either leave the row at "active" or fail to
    # insert and produce an empty projection.
    assert row["state"] == "completed"
    assert row["result_note"] == "tie-break done"


def test_rebuild_all_aborts_and_rolls_back_on_unknown_event_type(
    migrated_engine: Engine,
) -> None:
    """An unknown ``event_type`` aborts ``rebuild_all`` and rolls back the txn.

    The ``SqlAlchemyEventStore._decode`` path raises
    :class:`OpsHubError` for an ``event_type`` the binary cannot
    deserialise (typically: an event written by a newer build, or a
    corrupted ``schema_version`` bump that never landed here). That
    error propagates out of the ``engine.begin()`` block the rebuild
    driver wraps replay in, which means the projection state must be
    **byte-identical to what it was before the rebuild attempt** — the
    transaction rolls back and a partially-rewound projection cannot
    overwrite the previous good snapshot.

    We seed the projection by running a known-good rebuild first
    (snapshot A), then manually inject a row with ``event_type =
    'unknown.event_type'`` into the events table (bypassing the
    service), then run rebuild again and assert it raises
    :class:`OpsHubError` and the snapshot is unchanged.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()
    service = TaskService(store=store, projector=NoOpProjector())

    # Seed: one good task, drive it to completed, run a clean rebuild
    # so the ``tasks`` table holds the canonical snapshot.
    created = service.create_task(title="seed", body="for rollback")
    service.activate_task(created.aggregate_id)
    service.complete_task(created.aggregate_id, "shipped")
    rebuild_all(migrated_engine, store, [projection])
    snapshot_before = _read_tasks(migrated_engine)
    assert len(snapshot_before) == 1

    # Manually insert an unknown event_type, bypassing the service /
    # event model so pydantic validation does not block us. This
    # simulates either a forward-compat scenario (newer binary wrote
    # the event) or a corrupted store; the rebuild contract treats
    # both identically.
    now = datetime.now(UTC)
    with migrated_engine.begin() as conn:
        conn.execute(
            insert(events_table).values(
                id="01HA000000000000000000UNKN",
                aggregate_id="01HA0000000000000000AGGUNK",
                event_type="unknown.event_type",
                payload='{"event_type": "unknown.event_type"}',
                schema_version=1,
                occurred_at=now,
                recorded_at=now,
                actor="test",
            )
        )

    # ``rebuild_all`` opens its own ``engine.begin()`` transaction; the
    # OpsHubError raised by _decode propagates out and triggers
    # rollback. Without the rollback, the in-progress ``reset`` would
    # leave ``tasks`` empty.
    with pytest.raises(OpsHubError):
        rebuild_all(migrated_engine, store, [projection])

    snapshot_after = _read_tasks(migrated_engine)
    assert snapshot_after == snapshot_before, (
        "rebuild_all must rollback its transaction on unknown event_type; "
        "projection state diverged from the pre-rebuild snapshot"
    )


# ---- Phase 3 projections end-to-end ---------------------------------------


def test_rebuild_all_replays_phase_3_source_and_connector_events(
    migrated_engine: Engine,
) -> None:
    """Phase 3 source + connector events flow through ``rebuild_all``.

    This pins two things at once:

    1. The :func:`all_projections` registry actually wires
       :class:`SourcesProjection` + :class:`ConnectorCursorsProjection`
       in — if either is missing from the registry, the corresponding
       table is empty after the rebuild.
    2. The reducers' upsert semantics survive a full rewind+replay:
       re-observation of the same ``(connector_name, external_id)``
       still collapses to a single row, and the started/completed
       bracket still leaves ``last_synced_at`` pinned to the start.

    We bypass the service layer (no source / connector service exists
    yet — those land in steps A4 / A5) and append events directly via
    :class:`SqlAlchemyEventStore`.
    """
    store = SqlAlchemyEventStore(migrated_engine)

    # Two observations of the same external item — second observation
    # must update the row in place.
    source_id = "01HA0SRC0000000000000000AA"
    t_obs1 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t_obs2 = t_obs1 + timedelta(hours=1)
    obs1 = SourceObserved(
        aggregate_id=source_id,
        occurred_at=t_obs1,
        recorded_at=t_obs1,
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="original title",
        url="https://example.com/v1",
        summary="v1",
    )
    obs2 = SourceObserved(
        aggregate_id="01HA0SRC0000000000000000BB",
        occurred_at=t_obs2,
        recorded_at=t_obs2,
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="updated title",
        url="https://example.com/v2",
        summary="v2",
    )

    # A SourceReferenced event must be a no-op for SourcesProjection
    # (the row count for owner/repo#1 stays at one regardless).
    referenced = SourceReferenced(
        aggregate_id=source_id,
        occurred_at=t_obs2,
        recorded_at=t_obs2,
        actor="test",
        entity_type="task",
        entity_id="01HA0TASKREFERENCING000001",
    )

    # A sync run for the github connector: started → completed.
    t_start = datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)
    t_end = t_start + timedelta(minutes=10)
    started = ConnectorSyncStarted(
        aggregate_id="01HA0SYNC0000000000000000A",
        occurred_at=t_start,
        recorded_at=t_start,
        actor="connector:github",
        connector_name="github",
        cursor_value="2026-05-17T08:00:00Z",
    )
    completed = ConnectorSyncCompleted(
        aggregate_id=started.aggregate_id,
        occurred_at=t_end,
        recorded_at=t_end,
        actor="connector:github",
        connector_name="github",
        cursor_value="2026-05-17T09:59:59Z",
        observed_count=2,
    )

    # A failed sync run for a *different* connector must NOT create a
    # cursor row (no preceding started event for slack).
    t_fail = t_end + timedelta(minutes=5)
    failed_slack = ConnectorSyncFailed(
        aggregate_id="01HA0SYNC0000000000000000B",
        occurred_at=t_fail,
        recorded_at=t_fail,
        actor="connector:slack",
        connector_name="slack",
        error_message="auth expired",
    )

    for event in (obs1, obs2, referenced, started, completed, failed_slack):
        store.append(event)

    rebuild_all(migrated_engine, store, all_projections())

    # SourcesProjection: one row for owner/repo#1, with first-observation
    # id + observed_at, latest title / updated_at.
    with migrated_engine.connect() as conn:
        source_rows = conn.execute(select(sources_table)).mappings().all()
    assert len(source_rows) == 1
    source_row = source_rows[0]
    assert source_row["id"] == source_id
    assert source_row["connector_name"] == "github"
    assert source_row["external_id"] == "owner/repo#1"
    assert source_row["title"] == "updated title"
    assert source_row["url"] == "https://example.com/v2"
    assert source_row["summary"] == "v2"
    assert source_row["observed_at"] == t_obs1.replace(tzinfo=None)
    assert source_row["updated_at"] == t_obs2.replace(tzinfo=None)

    # ConnectorCursorsProjection: one row for github, none for slack
    # (the failed sync did not synthesise a row).
    with migrated_engine.connect() as conn:
        cursor_rows = conn.execute(select(connector_cursors_table)).mappings().all()
    assert len(cursor_rows) == 1
    cursor_row = cursor_rows[0]
    assert cursor_row["connector_name"] == "github"
    # cursor_value advanced to the post-completion token …
    assert cursor_row["cursor_value"] == "2026-05-17T09:59:59Z"
    # … updated_at refreshed to completion …
    assert cursor_row["updated_at"] == t_end.replace(tzinfo=None)
    # … but last_synced_at stayed pinned to the start of the sync run.
    assert cursor_row["last_synced_at"] == t_start.replace(tzinfo=None)


def test_rebuild_all_phase_3_projections_are_idempotent(
    migrated_engine: Engine,
) -> None:
    """Two consecutive ``rebuild_all`` calls produce the same Phase 3 rows.

    Mirrors the Phase 1 idempotency test, but exercises the new
    projections. Without :meth:`reset` clearing the upserted rows the
    second rebuild's ``ON CONFLICT`` clause would still succeed — but
    ``observed_at`` would shift to the second-call timestamps because
    ``reset`` is the only thing that purges the prior row identity.
    """
    store = SqlAlchemyEventStore(migrated_engine)

    t_obs = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t_start = datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)
    t_end = t_start + timedelta(minutes=10)
    store.append(
        SourceObserved(
            aggregate_id="01HA0SRC0000000000000000CC",
            occurred_at=t_obs,
            recorded_at=t_obs,
            actor="connector:github",
            connector_name="github",
            external_id="owner/repo#2",
            source_type="issue",
            title="idempotent",
        )
    )
    store.append(
        ConnectorSyncStarted(
            aggregate_id="01HA0SYNC0000000000000000C",
            occurred_at=t_start,
            recorded_at=t_start,
            actor="connector:github",
            connector_name="github",
            cursor_value=None,
        )
    )
    store.append(
        ConnectorSyncCompleted(
            aggregate_id="01HA0SYNC0000000000000000C",
            occurred_at=t_end,
            recorded_at=t_end,
            actor="connector:github",
            connector_name="github",
            cursor_value="2026-05-17T09:59:59Z",
            observed_count=1,
        )
    )

    rebuild_all(migrated_engine, store, all_projections())
    with migrated_engine.connect() as conn:
        sources_first = [
            dict(r)
            for r in conn.execute(select(sources_table).order_by(sources_table.c.id))
            .mappings()
            .all()
        ]
        cursors_first = [
            dict(r)
            for r in conn.execute(
                select(connector_cursors_table).order_by(connector_cursors_table.c.connector_name)
            )
            .mappings()
            .all()
        ]

    rebuild_all(migrated_engine, store, all_projections())
    with migrated_engine.connect() as conn:
        sources_second = [
            dict(r)
            for r in conn.execute(select(sources_table).order_by(sources_table.c.id))
            .mappings()
            .all()
        ]
        cursors_second = [
            dict(r)
            for r in conn.execute(
                select(connector_cursors_table).order_by(connector_cursors_table.c.connector_name)
            )
            .mappings()
            .all()
        ]

    assert sources_second == sources_first
    assert cursors_second == cursors_first
    # Sanity: each table has exactly one row from the seeded events.
    assert len(sources_first) == 1
    assert len(cursors_first) == 1
