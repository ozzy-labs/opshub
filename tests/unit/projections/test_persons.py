"""Unit tests for the person-axis projections (Phase 25-B, ADR-0043).

Exercises :class:`opshub.projections.persons.PersonsProjection` and
:class:`opshub.projections.person_identities.PersonIdentitiesProjection`
directly against a live SQLite connection. Both tables are provisioned
via ``metadata.create_all`` (the migration shape is pinned separately in
``tests/integration/test_phase25b_migrations.py``).
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
    IdentityLinked,
    IdentityMerged,
    IdentitySplit,
    PersonIdentified,
    TaskCreated,
)
from opshub.projections.person_identities import (
    PersonIdentitiesProjection,
    person_identities_table,
)
from opshub.projections.persons import PersonsProjection, persons_table

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Engine with both person-axis tables provisioned (FK enforcement on)."""
    db_path = tmp_path / "persons.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    persons_table.create(db_engine)
    person_identities_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _identify(person_id: str, name: str, *, is_operator: bool = False) -> PersonIdentified:
    return PersonIdentified(
        aggregate_id=person_id,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="cli:person",
        display_name=name,
        is_operator=is_operator,
    )


def _link(person_id: str, connector: str, handle: str, **kw: object) -> IdentityLinked:
    return IdentityLinked(
        aggregate_id=person_id,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="cli:person",
        connector=connector,
        handle=handle,
        **kw,  # type: ignore[arg-type]
    )


def _apply(engine: Engine, *events: object) -> None:
    persons = PersonsProjection()
    idents = PersonIdentitiesProjection()
    with engine.begin() as conn:
        for ev in events:
            persons.apply(conn, ev)  # type: ignore[arg-type]
            idents.apply(conn, ev)  # type: ignore[arg-type]


# ---- PersonIdentified -----------------------------------------------------


def test_person_identified_inserts_row(engine: Engine) -> None:
    pid = new_ulid()
    _apply(engine, _identify(pid, "Alice", is_operator=True))

    with engine.connect() as conn:
        row = conn.execute(select(persons_table)).mappings().one()
    assert row["id"] == pid
    assert row["display_name"] == "Alice"
    assert row["is_operator"] == 1


def test_person_identified_conflict_is_noop(engine: Engine) -> None:
    """Re-applying the same PersonIdentified (rebuild) does not duplicate."""
    pid = new_ulid()
    ev = _identify(pid, "Alice")
    _apply(engine, ev, ev)

    with engine.connect() as conn:
        rows = conn.execute(select(persons_table)).all()
    assert len(rows) == 1


# ---- IdentityLinked -------------------------------------------------------


def test_identity_linked_inserts_and_upserts(engine: Engine) -> None:
    pid = new_ulid()
    _apply(
        engine,
        _identify(pid, "Alice"),
        _link(pid, "slack", "U0123", display="Alice S"),
    )

    with engine.connect() as conn:
        row = conn.execute(select(person_identities_table)).mappings().one()
    assert row["connector"] == "slack"
    assert row["handle"] == "U0123"
    assert row["person_id"] == pid
    assert row["display"] == "Alice S"
    assert row["confidence"] == "exact"

    # Re-link the same (connector, handle) under a new person → person_id
    # is refreshed (UPSERT), still one row.
    pid2 = new_ulid()
    _apply(engine, _identify(pid2, "Alice2"), _link(pid2, "slack", "U0123"))
    with engine.connect() as conn:
        rows = conn.execute(select(person_identities_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["person_id"] == pid2


# ---- IdentityMerged -------------------------------------------------------


def test_identity_merged_reparents_and_tombstones(engine: Engine) -> None:
    survivor = "01AAAAAAAAAAAAAAAAAAAAAAAA"
    merged = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    _apply(
        engine,
        _identify(survivor, "Alice"),
        _link(survivor, "slack", "U_alice"),
        _identify(merged, "Alice (gh)"),
        _link(merged, "github", "alice"),
    )

    _apply(
        engine,
        IdentityMerged(
            aggregate_id=survivor,
            occurred_at=_T0,
            recorded_at=_T0,
            actor="cli:person_merge",
            merged_person_id=merged,
            reason="manual",
        ),
    )

    with engine.connect() as conn:
        persons = conn.execute(select(persons_table.c.id)).scalars().all()
        idents = (
            conn.execute(
                select(person_identities_table.c.handle, person_identities_table.c.person_id)
            )
            .mappings()
            .all()
        )
    # Merged person tombstoned, survivor remains.
    assert set(persons) == {survivor}
    # Both identities now parented on the survivor.
    assert {i["person_id"] for i in idents} == {survivor}
    assert {i["handle"] for i in idents} == {"U_alice", "alice"}


def test_identity_merged_is_idempotent(engine: Engine) -> None:
    survivor = "01AAAAAAAAAAAAAAAAAAAAAAAA"
    merged = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    merge_ev = IdentityMerged(
        aggregate_id=survivor,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="cli:person_merge",
        merged_person_id=merged,
        reason="manual",
    )
    _apply(
        engine,
        _identify(survivor, "Alice"),
        _link(survivor, "slack", "U_alice"),
        _identify(merged, "Alice (gh)"),
        _link(merged, "github", "alice"),
        merge_ev,
        merge_ev,  # second apply (rebuild) is a no-op
    )

    with engine.connect() as conn:
        persons = conn.execute(select(persons_table.c.id)).scalars().all()
        idents = conn.execute(select(person_identities_table.c.person_id)).scalars().all()
    assert set(persons) == {survivor}
    assert set(idents) == {survivor}


# ---- IdentitySplit --------------------------------------------------------


def test_identity_split_detaches_into_new_person(engine: Engine) -> None:
    pid = new_ulid()
    new_pid = new_ulid()
    _apply(
        engine,
        _identify(pid, "Alice"),
        _link(pid, "slack", "U_alice", display="Alice S"),
        _link(pid, "github", "alice"),
        IdentitySplit(
            aggregate_id=pid,
            occurred_at=_T0,
            recorded_at=_T0,
            actor="cli:person_split",
            new_person_id=new_pid,
            identity_connector="github",
            identity_handle="alice",
        ),
    )

    with engine.connect() as conn:
        idents = (
            conn.execute(
                select(person_identities_table.c.connector, person_identities_table.c.person_id)
            )
            .mappings()
            .all()
        )
        new_person = (
            conn.execute(select(persons_table).where(persons_table.c.id == new_pid))
            .mappings()
            .one()
        )
    by_conn = {i["connector"]: i["person_id"] for i in idents}
    assert by_conn["slack"] == pid
    assert by_conn["github"] == new_pid
    # New person exists; display falls back to the handle (github had no display).
    assert new_person["display_name"] == "alice"
    assert new_person["is_operator"] == 0


# ---- generic contract -----------------------------------------------------


def test_unrelated_event_is_noop(engine: Engine) -> None:
    _apply(
        engine,
        TaskCreated(
            aggregate_id=new_ulid(),
            occurred_at=_T0,
            recorded_at=_T0,
            actor="cli:test",
            title="not a person",
        ),
    )
    with engine.connect() as conn:
        assert conn.execute(select(persons_table)).all() == []
        assert conn.execute(select(person_identities_table)).all() == []


def test_reset_clears_both_tables(engine: Engine) -> None:
    pid = new_ulid()
    _apply(engine, _identify(pid, "Alice"), _link(pid, "slack", "U0123"))

    persons = PersonsProjection()
    idents = PersonIdentitiesProjection()
    with engine.begin() as conn:
        # Identities first so the FK does not block the persons delete.
        idents.reset(conn)
        persons.reset(conn)

    with engine.connect() as conn:
        assert conn.execute(select(persons_table)).all() == []
        assert conn.execute(select(person_identities_table)).all() == []


def test_projection_names() -> None:
    assert PersonsProjection().name == "persons"
    assert PersonIdentitiesProjection().name == "person_identities"
