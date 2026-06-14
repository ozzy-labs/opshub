"""Integration tests for the Phase 25-B person axis (ADR-0043).

End-to-end across the Alembic-migrated schema:

* **resolve → rebuild idempotency** — resolving author handles into a
  person graph, then ``rebuild_all`` from the event log, reproduces the
  same ``persons`` / ``person_identities`` rows (the determinism
  contract: the fuzzy / exact *decision* lives in the service, the
  projections are pure functions of the recorded events).

* **graph reachability** — a ``person:<id>`` → ``source:<id>`` link with
  the ``identifies`` link type is reachable via ``LinkService.related``
  and ``trace``, and ``identifies`` is in :data:`LINK_TYPES_MVP` so the
  manual ``link add`` path does not flag it as outside the recommended
  enum (ADR-0017 §改訂).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import DomainEvent, SourceObserved
from opshub.projections import (
    LINK_TYPES_MVP,
    person_identities_table,
    persons_table,
    rebuild_all,
)
from opshub.projections.registry import all_projections
from opshub.projections.sources import SourcesProjection
from opshub.services.links import LinkService
from opshub.services.persons import PersonResolutionService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"
_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "phase25b.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _AllProjectionsAdapter:
    def __init__(self) -> None:
        self._projections = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        assert connection is not None
        for projection in self._projections:
            projection.apply(connection, event)


def _seed_source(engine: Engine, *, connector: str, external_id: str, handle: str) -> str:
    src_id = new_ulid()
    event = SourceObserved(
        aggregate_id=src_id,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="connector:test",
        connector_name=connector,
        external_id=external_id,
        source_type="message",
        title="msg",
        body="hello",
        author_handle=handle,
        author_display="Alice",
    )
    store = SqlAlchemyEventStore(engine)
    proj = SourcesProjection()
    with engine.begin() as conn:
        store.append(event, conn)
        proj.apply(conn, event)
    return src_id


def _snapshot(engine: Engine) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    with engine.connect() as conn:
        persons = [
            dict(r)
            for r in conn.execute(select(persons_table).order_by(persons_table.c.id))
            .mappings()
            .all()
        ]
        idents = [
            dict(r)
            for r in conn.execute(
                select(person_identities_table).order_by(
                    person_identities_table.c.connector,
                    person_identities_table.c.handle,
                )
            )
            .mappings()
            .all()
        ]
    return persons, idents


def test_resolve_then_rebuild_is_idempotent(migrated_engine: Engine) -> None:
    _seed_source(migrated_engine, connector="slack", external_id="T1:C1:1", handle="U_a")
    _seed_source(migrated_engine, connector="github", external_id="42", handle="bob")

    svc = PersonResolutionService(
        engine=migrated_engine,
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=migrated_engine.begin,
    )
    svc.resolve()
    before = _snapshot(migrated_engine)
    assert len(before[0]) == 2  # two persons
    assert len(before[1]) == 2  # two identities

    # Replay the whole event log (SourceObserved + PersonIdentified +
    # IdentityLinked) through every projection.
    store = SqlAlchemyEventStore(migrated_engine)
    rebuild_all(migrated_engine, store, all_projections())

    after = _snapshot(migrated_engine)
    assert after == before

    # A second rebuild is also stable.
    rebuild_all(migrated_engine, store, all_projections())
    assert _snapshot(migrated_engine) == before


def test_person_source_identifies_link_is_reachable(migrated_engine: Engine) -> None:
    # ``identifies`` is recognised, so the manual link add path does not
    # warn (ADR-0017 §改訂 / ADR-0043).
    assert "identifies" in LINK_TYPES_MVP

    src_id = _seed_source(migrated_engine, connector="slack", external_id="T1:C1:1", handle="U_a")
    svc = PersonResolutionService(
        engine=migrated_engine,
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=migrated_engine.begin,
    )
    svc.resolve()
    person_id = svc.list_persons()[0].id

    link_service = LinkService(
        engine=migrated_engine,
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=migrated_engine.begin,
        actor="cli:link_add",
    )
    link_service.create_link(
        from_entity_type="person",
        from_entity_id=person_id,
        to_entity_type="source",
        to_entity_id=src_id,
        link_type="identifies",
    )

    # related() from the person finds the source it identifies.
    out = link_service.related("person", person_id, direction="outgoing")
    assert len(out) == 1
    assert out[0].to_entity_type == "source"
    assert out[0].to_entity_id == src_id
    assert out[0].link_type == "identifies"

    # trace() from the source reaches back to the person node.
    paths = link_service.trace("source", src_id, depth=2)
    reached = {
        (link.from_entity_type, link.from_entity_id) for path in paths for link in path.links
    }
    assert ("person", person_id) in reached
