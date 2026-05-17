"""Integration tests for the Phase 8 step B2 ``LinksExtractor`` rebuild path.

These tests pin the rebuild contract Phase 8 plan §1.1 calls out: the
``links`` projection must be **reconstructible end-to-end from the
event log** via ``opshub projections rebuild``, and running the rebuild
twice must produce byte-identical projection state (idempotency).

Why a dedicated integration test (vs. the unit tests in
``tests/unit/projections/test_links_extractor.py``):

* The unit tests exercise the reducer directly with a hand-created
  ``links`` table. They do not catch a regression where the registry
  forgets to include :class:`LinksProjector`, where the migration
  drops a column the reducer writes to, or where the rebuild driver
  itself fans events incorrectly to the new projector.
* The integration test runs the real Alembic-managed schema, the real
  ``SqlAlchemyEventStore``, the real ``all_projections()`` registry,
  and the real ``rebuild_all`` driver — i.e. the same surface area
  ``opshub projections rebuild`` covers in production.

We seed one event from each of the 5 emitting families (the 4
auto-extracted paths + the ``LinkCreated`` manual path) so the test
fails on a regression in any single dispatch path. A separate test
asserts the byte-identical equality between two consecutive rebuild
runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import (
    BriefingGenerated,
    LinkCreated,
    ProposalApplied,
    ProposalRequested,
    SourceReferenced,
)
from opshub.projections import links_table, rebuild_all
from opshub.projections.registry import all_projections

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper in :mod:`tests.integration.test_phase8_migrations`
    so the rebuild test picks up the same env.py URL-resolution path
    the production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    ``head`` advances past migration 0016 so the ``links`` table is
    present and the registry's ``LinksProjector`` has a target table
    to write into.
    """
    db_path = tmp_path / "phase8_rebuild.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_link_emitting_events(
    store: SqlAlchemyEventStore,
) -> dict[str, Any]:
    """Append one event from each link-emitting family.

    Returns a dict of the ids referenced by the seeded events so the
    assertion can construct the expected link tuples (rather than
    rebuilding them from string concatenation in the test body).
    """
    base = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    proposal_id = "01HA0PROPOSAL0000000000001"
    applied_entity_id = "01HA0TASK0000000000000001A"
    briefing_id = "01HA0BRIEFING000000000001B"
    proposal_from_briefing_id = "01HA0PROPOSAL000000000002B"
    source_id = "01HA0SOURCE000000000000001"
    referenced_entity_id = "01HA0TASK0000000000000002R"
    briefing_ref_task_id = "01HA0TASK0000000000000003B"
    briefing_ref_decision_id = "01HA0DECISION000000000004B"
    manual_link_id = "01HA0LINK0000000000000005M"
    manual_from_id = "01HA0TASK0000000000000006M"
    manual_to_id = "01HA0DECISION000000000007M"

    # Path 1: ProposalApplied → applied_to
    store.append(
        ProposalApplied(
            aggregate_id=proposal_id,
            occurred_at=base,
            recorded_at=base,
            actor="cli:propose",
            candidate_index=0,
            applied_entity_type="task",
            applied_entity_id=applied_entity_id,
            applied_by="cli:propose",
        )
    )

    # Path 2: BriefingGenerated -> referenced_in_briefing (2 source_refs)
    store.append(
        BriefingGenerated(
            aggregate_id=briefing_id,
            occurred_at=base + timedelta(minutes=1),
            recorded_at=base + timedelta(minutes=1),
            actor="cli:brief",
            briefing_id=briefing_id,
            topic="rebuild test",
            scope="all",
            markdown="# Briefing\n\nBody.",
            source_refs=[("task", briefing_ref_task_id), ("decision", briefing_ref_decision_id)],
            model_id="claude-haiku-4-5-20251001",
            model_version="20251001",
            tokens_in=100,
            tokens_out=50,
        )
    )

    # Path 3: ProposalRequested(briefing_id=non-None) → generated_from_briefing
    store.append(
        ProposalRequested(
            aggregate_id=proposal_from_briefing_id,
            occurred_at=base + timedelta(minutes=2),
            recorded_at=base + timedelta(minutes=2),
            actor="cli:propose",
            topic="rebuild test",
            scope="all",
            briefing_id=briefing_id,
            requested_by="cli:propose",
        )
    )

    # Path 4: SourceReferenced → references
    store.append(
        SourceReferenced(
            aggregate_id=source_id,
            occurred_at=base + timedelta(minutes=3),
            recorded_at=base + timedelta(minutes=3),
            actor="connector:github",
            entity_type="task",
            entity_id=referenced_entity_id,
        )
    )

    # Path 5: LinkCreated → direct INSERT
    store.append(
        LinkCreated(
            aggregate_id=manual_link_id,
            occurred_at=base + timedelta(minutes=4),
            recorded_at=base + timedelta(minutes=4),
            actor="cli:link",
            from_entity_type="task",
            from_entity_id=manual_from_id,
            to_entity_type="decision",
            to_entity_id=manual_to_id,
            link_type="manual",
            created_by="cli:link",
        )
    )

    return {
        "proposal_id": proposal_id,
        "applied_entity_id": applied_entity_id,
        "briefing_id": briefing_id,
        "proposal_from_briefing_id": proposal_from_briefing_id,
        "source_id": source_id,
        "referenced_entity_id": referenced_entity_id,
        "briefing_ref_task_id": briefing_ref_task_id,
        "briefing_ref_decision_id": briefing_ref_decision_id,
        "manual_link_id": manual_link_id,
        "manual_from_id": manual_from_id,
        "manual_to_id": manual_to_id,
    }


def _link_signatures(engine: Engine) -> list[tuple[str, str, str, str, str]]:
    """Return ``(from_type, from_id, to_type, to_id, link_type)`` tuples.

    Identity-comparing the natural-key tuples (rather than the full
    rows) keeps the assertion stable across SQLite timestamp
    formatting quirks while still pinning every link the rebuild
    materialised.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    links_table.c.from_entity_type,
                    links_table.c.from_entity_id,
                    links_table.c.to_entity_type,
                    links_table.c.to_entity_id,
                    links_table.c.link_type,
                ).order_by(
                    links_table.c.link_type,
                    links_table.c.from_entity_id,
                    links_table.c.to_entity_id,
                )
            )
            .mappings()
            .all()
        )
    return [
        (
            r["from_entity_type"],
            r["from_entity_id"],
            r["to_entity_type"],
            r["to_entity_id"],
            r["link_type"],
        )
        for r in rows
    ]


def _all_rows_ordered(engine: Engine) -> list[dict[str, Any]]:
    """Return every ``links`` row as a list of dicts sorted by ``id``.

    Used as the byte-identical snapshot the idempotency assertion
    compares against. Sorting by ``id`` keeps the comparison stable
    regardless of which SQLite page each row landed on.
    """
    with engine.connect() as conn:
        rows = conn.execute(select(links_table).order_by(links_table.c.id)).mappings().all()
    return [dict(row) for row in rows]


def test_projections_rebuild_reconstructs_full_links_graph(
    migrated_engine: Engine,
) -> None:
    """``rebuild_all`` materialises every link from the seeded events.

    Seeds one event from each emitting family (4 auto-extracted paths
    + the manual ``LinkCreated`` path) then runs the rebuild driver.
    Each path's expected natural-key tuple is asserted explicitly so a
    regression in any single dispatch surfaces here.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    seeded = _seed_link_emitting_events(store)

    rebuild_all(migrated_engine, store, all_projections())

    signatures = _link_signatures(migrated_engine)

    expected = sorted(
        [
            (
                "proposal",
                seeded["proposal_id"],
                "task",
                seeded["applied_entity_id"],
                "applied_to",
            ),
            (
                "proposal",
                seeded["proposal_from_briefing_id"],
                "briefing",
                seeded["briefing_id"],
                "generated_from_briefing",
            ),
            (
                "briefing",
                seeded["briefing_id"],
                "task",
                seeded["briefing_ref_task_id"],
                "referenced_in_briefing",
            ),
            (
                "briefing",
                seeded["briefing_id"],
                "decision",
                seeded["briefing_ref_decision_id"],
                "referenced_in_briefing",
            ),
            (
                "source",
                seeded["source_id"],
                "task",
                seeded["referenced_entity_id"],
                "references",
            ),
            (
                "task",
                seeded["manual_from_id"],
                "decision",
                seeded["manual_to_id"],
                "manual",
            ),
        ],
        key=lambda t: (t[4], t[1], t[3]),
    )

    assert signatures == expected, (
        "rebuild_all must materialise every link-emitting event into the projection"
    )


def test_projections_rebuild_is_idempotent(migrated_engine: Engine) -> None:
    """Two consecutive ``rebuild_all`` calls produce identical projection state.

    This is the rebuild idempotency contract Phase 8 plan §1.1 pins
    for the ``LinksExtractor``: ``reset`` + replay must be a pure
    function of the event log. Without idempotency, the operational
    "fix a projection bug via rebuild" escape hatch (ADR-0002) breaks
    — a second rebuild would drift from the first, masking the
    correctness regression.

    The auto-extracted link ids are also pinned by this test: because
    they are deterministic hashes of the natural-key tuple
    (Phase 8 B2 ``_stable_link_id``), two rebuilds must produce the
    same id for the same logical link. A non-deterministic id
    derivation would fail the dict-level equality assertion below.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    _seed_link_emitting_events(store)

    rebuild_all(migrated_engine, store, all_projections())
    snapshot_first = _all_rows_ordered(migrated_engine)

    rebuild_all(migrated_engine, store, all_projections())
    snapshot_second = _all_rows_ordered(migrated_engine)

    assert snapshot_second == snapshot_first, (
        "two consecutive rebuild_all calls must produce byte-identical links state"
    )
    # Sanity: the snapshot contains every expected row.
    assert len(snapshot_first) == 6, (
        f"expected 6 link rows from the seeded events; got {len(snapshot_first)}"
    )
