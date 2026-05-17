"""Tests for the writer-side extension of :class:`LinkService` (Phase 8 D1).

Phase 8 step C1 shipped read-only ``related`` / ``trace`` /
``find_link_id`` methods. Step D1 added writer methods (``create_link``,
``delete_link``, ``list_links``) wired against the events table +
:class:`LinksProjector`. These tests pin the writer-side contract:

* ``create_link`` mints a fresh ULID, emits :class:`LinkCreated`, and
  applies the projector in one UoW.
* ``delete_link`` emits :class:`LinkDeleted`, applies the projector
  (hard-DELETE), and reports whether a row was actually deleted.
* ``list_links`` honours the ``--from`` / ``--to`` / ``--type`` /
  ``--limit`` filter set surfaced by the CLI.
* Constructing the service without writer dependencies leaves the
  C1 read-only contract intact and raises :class:`ConfigError` when
  a writer method is invoked.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.db import SqlAlchemyEventStore, events_table
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import DomainEvent
from opshub.projections.links import LinksProjector, links_table
from opshub.services.links import LinkService

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class _LinksProjectorAdapter:
    """Adapter exposing :class:`LinksProjector` as a :class:`Projector`.

    The projection-layer ``Projection`` Protocol uses ``apply(conn,
    event)`` while the service-layer ``Projector`` Protocol uses
    ``apply(event, connection)``. The CLI wiring crosses this seam via
    the private ``_PersistingProjector`` in :mod:`opshub.cli._wiring`;
    the tests use this slim adapter so the assertion suite stays
    decoupled from the CLI wiring's private class.
    """

    def __init__(self) -> None:
        self._inner = LinksProjector()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        assert connection is not None
        self._inner.apply(connection, event)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite engine with the events + links tables."""
    db_path = tmp_path / "link_writer.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    events_table.create(db_engine)
    links_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _build_service(engine: Engine, *, actor: str = "test:writer") -> LinkService:
    """Construct a writer-capable LinkService backed by ``engine``.

    Uses the :class:`_LinksProjectorAdapter` defined above to bridge
    the :class:`LinksProjector` `(conn, event)` signature into the
    :class:`Projector` `(event, connection)` signature the service
    expects — mirrors what
    :func:`opshub.cli._wiring.build_link_service` does for the live
    CLI path.
    """
    return LinkService(
        engine=engine,
        store=SqlAlchemyEventStore(engine),
        projector=_LinksProjectorAdapter(),
        uow_factory=engine.begin,
        actor=actor,
    )


# ---- create_link ----------------------------------------------------------


def test_create_link_emits_event_and_writes_row(engine: Engine) -> None:
    """``create_link`` appends LinkCreated and inserts a links row."""
    service = _build_service(engine)
    link_id = service.create_link(
        from_entity_type="task",
        from_entity_id="task-1",
        to_entity_type="decision",
        to_entity_id="decision-1",
        link_type="manual",
    )

    # Events table has the LinkCreated row.
    with engine.connect() as conn:
        events = conn.execute(select(events_table.c.event_type, events_table.c.aggregate_id)).all()
    # ``Row`` compares element-wise to a tuple; cast to tuples so the
    # mypy strict checker sees a comparable shape (Sequence[Row[Any]]
    # vs. list[tuple[...]] is not overlapping under the strict mode).
    assert [tuple(row) for row in events] == [("link.created", link_id)]

    # Projection row matches.
    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == link_id
    assert row["from_entity_type"] == "task"
    assert row["from_entity_id"] == "task-1"
    assert row["link_type"] == "manual"


def test_create_link_with_metadata_persists(engine: Engine) -> None:
    """Metadata dict is written to the projection."""
    service = _build_service(engine)
    link_id = service.create_link(
        from_entity_type="task",
        from_entity_id="task-2",
        to_entity_type="proposal",
        to_entity_id="proposal-2",
        metadata={"k": "v"},
    )
    with engine.connect() as conn:
        rows = conn.execute(select(links_table).where(links_table.c.id == link_id)).mappings().all()
    assert rows[0]["metadata"] == {"k": "v"}


# ---- delete_link ----------------------------------------------------------


def test_delete_link_removes_row_and_returns_true(engine: Engine) -> None:
    """``delete_link`` deletes the row and reports True."""
    service = _build_service(engine)
    link_id = service.create_link(
        from_entity_type="task",
        from_entity_id="task-3",
        to_entity_type="decision",
        to_entity_id="decision-3",
    )
    result = service.delete_link(link_id, reason="cleanup")
    assert result is True
    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == []


def test_delete_link_missing_id_returns_false(engine: Engine) -> None:
    """Removing a non-existent id reports False but still appends the event."""
    service = _build_service(engine)
    result = service.delete_link("01J6MISSING000000000000004")
    assert result is False
    # The LinkDeleted event is still appended for audit purposes.
    with engine.connect() as conn:
        events = conn.execute(
            select(events_table.c.event_type).where(events_table.c.event_type == "link.deleted")
        ).all()
    assert len(events) == 1


def test_delete_link_sanitises_reason(engine: Engine) -> None:
    """Reasons containing obvious secret shapes are scrubbed before persistence."""
    service = _build_service(engine)
    link_id = service.create_link(
        from_entity_type="task",
        from_entity_id="task-4",
        to_entity_type="decision",
        to_entity_id="decision-4",
    )
    service.delete_link(
        link_id,
        reason="found sk-abcdefghijklmnopqrstuvwxyz in body",
    )
    import json

    with engine.connect() as conn:
        rows = conn.execute(
            select(events_table.c.payload).where(events_table.c.event_type == "link.deleted")
        ).all()
    payload = json.loads(rows[0][0])
    assert "sk-***" in payload["reason"]
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["reason"]


# ---- list_links -----------------------------------------------------------


def test_list_links_no_filters_returns_all(engine: Engine) -> None:
    """An unfiltered call returns every link."""
    service = _build_service(engine)
    service.create_link(
        from_entity_type="task",
        from_entity_id="task-5",
        to_entity_type="decision",
        to_entity_id="decision-5",
    )
    service.create_link(
        from_entity_type="briefing",
        from_entity_id="briefing-5",
        to_entity_type="task",
        to_entity_id="task-5",
    )
    links = service.list_links()
    assert len(links) == 2


def test_list_links_filter_by_from(engine: Engine) -> None:
    service = _build_service(engine)
    service.create_link(
        from_entity_type="task",
        from_entity_id="task-6",
        to_entity_type="decision",
        to_entity_id="decision-6",
    )
    service.create_link(
        from_entity_type="briefing",
        from_entity_id="briefing-6",
        to_entity_type="task",
        to_entity_id="task-7",
    )
    links = service.list_links(from_entity_type="task", from_entity_id="task-6")
    assert len(links) == 1
    assert links[0].from_entity_id == "task-6"


def test_list_links_filter_by_type(engine: Engine) -> None:
    service = _build_service(engine)
    service.create_link(
        from_entity_type="task",
        from_entity_id="task-8",
        to_entity_type="decision",
        to_entity_id="decision-8",
        link_type="manual",
    )
    service.create_link(
        from_entity_type="briefing",
        from_entity_id="briefing-8",
        to_entity_type="task",
        to_entity_id="task-9",
        link_type="referenced_in_briefing",
    )
    links = service.list_links(link_type="manual")
    assert len(links) == 1
    assert links[0].link_type == "manual"


def test_list_links_respects_limit(engine: Engine) -> None:
    service = _build_service(engine)
    for index in range(5):
        service.create_link(
            from_entity_type="task",
            from_entity_id=f"task-limit-{index}",
            to_entity_type="decision",
            to_entity_id=f"decision-limit-{index}",
        )
    links = service.list_links(limit=2)
    assert len(links) == 2


# ---- read-only construction still works -----------------------------------


def test_read_only_construction_blocks_writer_methods(engine: Engine) -> None:
    """Constructing without writer deps raises ConfigError on create_link."""
    service = LinkService(engine)
    with pytest.raises(ConfigError):
        service.create_link(
            from_entity_type="task",
            from_entity_id="task-X",
            to_entity_type="decision",
            to_entity_id="decision-X",
        )
    with pytest.raises(ConfigError):
        service.delete_link("01J6LINK000000000000000ABC")


def test_read_only_construction_still_supports_list_links(engine: Engine) -> None:
    """``list_links`` is engine-only, available on the read-only construction."""
    # Seed via writer service, then query via read-only.
    writer = _build_service(engine)
    writer.create_link(
        from_entity_type="task",
        from_entity_id="task-list-readonly",
        to_entity_type="decision",
        to_entity_id="decision-list-readonly",
    )
    reader = LinkService(engine)
    links = reader.list_links()
    assert len(links) == 1
