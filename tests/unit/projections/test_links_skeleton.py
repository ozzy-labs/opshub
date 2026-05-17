"""Unit tests for the Phase 8 ``LinksProjector`` schema + registry wiring.

These tests pin the structural contract for
:mod:`opshub.projections.links` (originally introduced as the A2
skeleton, kept stable through B2's dispatch implementation):

* The ``links`` table is registered on the shared
  :data:`opshub.db.schema.metadata` (so Alembic autogenerate /
  ``Table.create`` both see it without an extra import).
* The natural-key UNIQUE constraint and the two bidirectional
  traversal indexes are present at the SQLAlchemy declaration layer
  (the migration integration test covers the live DB schema
  separately).
* :func:`opshub.projections.registry.all_projections` lists the
  projector under the stable ``"links"`` name so the inline projector
  and rebuild driver both wire it in.
* :data:`opshub.projections.links.LINK_TYPES_MVP` matches the
  ADR-0017 §決定 (b) enum exactly so consumers (CLI warning, future
  graph rendering) can membership-test the literal set.

Per-event dispatch behaviour (the actual extraction logic) is covered
by ``tests/unit/projections/test_links_extractor.py`` from Phase 8 B2
onwards. The handful of dispatch-adjacent tests below (e.g. the
unrelated-event no-op) stay here because they are functionally
schema-level smoke tests that gate against the reducer wiring an
unrelated event family by accident.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Index, UniqueConstraint, select
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import metadata
from opshub.domain.events import TaskCreated
from opshub.projections.links import (
    LINK_TYPES_MVP,
    LinksProjector,
    links_table,
)
from opshub.projections.registry import all_projections


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``links`` table provisioned.

    Hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the migration integration
    test (``tests/integration/test_phase8_migrations.py``) covers the
    migration path explicitly.
    """
    db_path = tmp_path / "links.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    links_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


# ---- Schema registration -------------------------------------------------


def test_links_table_registered_on_metadata() -> None:
    """``links`` lands on the shared ``MetaData`` at import time.

    Alembic autogenerate reads :data:`opshub.db.schema.metadata` to
    diff the schema; if the table did not register itself on import
    (for example because the registry stopped importing the module)
    autogenerate would emit a spurious ``DROP TABLE links`` diff on
    every revision pass.
    """
    assert "links" in metadata.tables, (
        "links table missing from shared metadata; "
        "opshub.projections.links did not import or register"
    )
    table = metadata.tables["links"]
    column_names = {col.name for col in table.columns}
    expected_columns = {
        "id",
        "from_entity_type",
        "from_entity_id",
        "to_entity_type",
        "to_entity_id",
        "link_type",
        "created_at",
        "source_event_id",
        "metadata",
    }
    assert column_names == expected_columns, (
        f"links column set mismatch; got {sorted(column_names)}"
    )


def test_links_table_has_natural_key_unique_constraint() -> None:
    """ADR-0017 §決定 (a) natural key is enforced via UNIQUE.

    The UPSERT semantics Phase 8 B2 relies on for idempotent rebuild
    target this constraint name; the column order must match exactly
    so the conflict-target tuple lines up with the dispatch table.
    """
    natural_key_columns = (
        "from_entity_type",
        "from_entity_id",
        "to_entity_type",
        "to_entity_id",
        "link_type",
    )
    uniques = [
        constraint
        for constraint in links_table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    matching = [
        uc for uc in uniques if tuple(col.name for col in uc.columns) == natural_key_columns
    ]
    assert matching, (
        "links table missing UNIQUE constraint on the natural-key tuple "
        f"{natural_key_columns!r}; found UNIQUE columns: "
        f"{[tuple(col.name for col in uc.columns) for uc in uniques]!r}"
    )
    assert matching[0].name == "links_natural_key_uq", (
        f"natural-key UNIQUE constraint name drifted: {matching[0].name!r}"
    )


def test_links_table_has_from_and_to_indexes() -> None:
    """Both bidirectional traversal indexes are declared.

    Phase 8 C1's ``LinkService.related`` issues two ``SELECT`` paths
    (outgoing via ``from_entity_*``, incoming via ``to_entity_*``).
    Without these indexes the queries would fall back to full-table
    scans once the graph grows past trivial size — the unit test pins
    the index declarations here so a future refactor that drops one
    of them is caught immediately, not at runtime.
    """
    # ``Index.name`` is typed as ``quoted_name | None`` in SQLAlchemy's
    # stubs; coerce to ``str`` here so the dict keying / sorted output
    # stays simple and pyright-friendly.
    indexes_by_name: dict[str, Index] = {str(idx.name): idx for idx in links_table.indexes}
    assert "links_from_idx" in indexes_by_name, (
        f"links_from_idx missing; got {sorted(indexes_by_name)!r}"
    )
    assert "links_to_idx" in indexes_by_name, (
        f"links_to_idx missing; got {sorted(indexes_by_name)!r}"
    )

    from_columns = tuple(col.name for col in indexes_by_name["links_from_idx"].columns)
    to_columns = tuple(col.name for col in indexes_by_name["links_to_idx"].columns)
    assert from_columns == ("from_entity_type", "from_entity_id")
    assert to_columns == ("to_entity_type", "to_entity_id")

    # Index instances are declared via the ``Index`` constructor (vs
    # the ``index=True`` column shorthand) — pin that so the
    # migration's ``op.create_index`` calls and the table stub stay in
    # lock-step.
    assert isinstance(indexes_by_name["links_from_idx"], Index)
    assert isinstance(indexes_by_name["links_to_idx"], Index)


# ---- Unrelated event family: dispatch must be a silent no-op -------------


def test_linksprojector_apply_unrelated_event_is_noop(engine: Engine) -> None:
    """Events outside the 6-path dispatch table leave the table untouched.

    The rebuild driver fans every event to every projection (see
    :func:`opshub.projections.rebuild.rebuild_all`); reducers that
    only own a subset of the event-type space MUST silently drop
    everything else. Wiring a stray event family into the dispatch
    would either raise (breaking rebuild) or write spurious rows
    (breaking the projection invariant) — both regressions surface
    here.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated event smoke test",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)  # must not raise

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == [], "LinksProjector.apply must no-op on unrelated event families"


def test_linksprojector_reset_clears_table(engine: Engine) -> None:
    """``reset`` empties the table even on the A2 skeleton.

    The rebuild driver always calls ``reset`` before replay; Phase 8
    B2's idempotent-rebuild test depends on ``reset`` being functional
    from day one, so we pin it here against a hand-seeded row.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    with engine.begin() as conn:
        conn.execute(
            links_table.insert().values(
                id=new_ulid(),
                from_entity_type="task",
                from_entity_id=new_ulid(),
                to_entity_type="decision",
                to_entity_id=new_ulid(),
                link_type="manual",
                created_at=occurred,
                source_event_id=None,
                metadata=None,
            )
        )

    with engine.begin() as conn:
        projector.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(links_table)).all()
    assert remaining == [], "reset must empty the links table"


# ---- Registry wiring ------------------------------------------------------


def test_linksprojector_in_registry_all_projections() -> None:
    """:func:`all_projections` lists the projector under ``"links"``.

    The inline projector wiring (``opshub.cli._wiring``) and the
    rebuild driver (``opshub.cli.projections``) both read the registry
    list. A missing entry would mean ``opshub projections rebuild``
    leaves the table stale even though the migration ran — exactly
    the silent correctness bug
    :func:`opshub.projections.registry.all_projections` exists to
    prevent (see the registry module docstring).
    """
    projections = all_projections()
    names = [p.name for p in projections]
    assert "links" in names, f"all_projections() must include 'links'; got {names!r}"

    matching = [p for p in projections if p.name == "links"]
    assert len(matching) == 1, f"duplicate 'links' projector entries: {matching!r}"
    assert isinstance(matching[0], LinksProjector)


# ---- ADR-0017 §決定 (b) enum --------------------------------------------


def test_link_types_mvp_matches_adr_0017_decision_b() -> None:
    """The MVP enum matches ADR-0017 §決定 (b) exactly.

    Phase 8 D1's ``opshub link add`` CLI warns when ``--type`` is
    outside this set; Phase 8 B2's auto-extraction emits only these
    values. Pinning the literal set here keeps the ADR-0017 §決定 (b)
    Decision Record and the runtime contract in lock-step.
    """
    assert LINK_TYPES_MVP == frozenset(
        {
            "applied_to",
            "referenced_in_briefing",
            "generated_from_briefing",
            "references",
            "manual",
        }
    )
