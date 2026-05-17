"""Tests for :meth:`opshub.services.links.LinkService.expand` (Phase 8 step C2).

Approach mirrors :mod:`test_link_service_basic`: hand-create the
``links`` table on a fresh SQLite engine and seed rows directly,
bypassing the projector dispatch path. This keeps the C2 tests
independent of the B2 :class:`~opshub.projections.links.LinksProjector`
implementation.

Coverage map (per Phase 8 plan §2.3 C2 + ADR-0017 §決定 (e)):

* depth boundaries — 0 (root-only), default, exceeds-max, negative
* bidirectional walking — outgoing + incoming neighbours in both
  hops
* cycle detection — directed cycle, chain that loops back to root
* edge dedup — diamond shape (multiple paths to same node) yields
  one edge per distinct link.id
* link_type filtering — applied at every hop, not just the first
* sort stability — edges in chronological order by created_at
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.links import links_table
from opshub.services.links import GraphSubset, LinkService

# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite engine with only the ``links`` table created.

    We sidestep Alembic for the same reasons as ``test_link_service_basic``:
    expand only touches the ``links`` table, and ``links_table.create``
    keeps the fixture cheap.
    """
    db_path = tmp_path / "link_service_expand.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    links_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _seed_link(
    engine: Engine,
    *,
    from_entity_type: str,
    from_entity_id: str,
    to_entity_type: str,
    to_entity_id: str,
    link_type: str = "manual",
    created_at: datetime | None = None,
    source_event_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    """Insert one ``links`` row directly and return its id."""
    link_id = new_ulid()
    with engine.begin() as conn:
        conn.execute(
            insert(links_table).values(
                id=link_id,
                from_entity_type=from_entity_type,
                from_entity_id=from_entity_id,
                to_entity_type=to_entity_type,
                to_entity_id=to_entity_id,
                link_type=link_type,
                created_at=created_at or datetime.now(UTC),
                source_event_id=source_event_id,
                metadata=metadata,
            )
        )
    return link_id


_ROOT = ("task", "root")


# ---- depth boundaries -----------------------------------------------------


def test_expand_depth_zero_returns_root_only_no_edges(engine: Engine) -> None:
    """``depth=0`` yields ``nodes={root}`` and ``edges=()`` (no traversal)."""
    # Seed a link so the table isn't empty; expand must ignore it.
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="proposal",
        to_entity_id="P",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=0)

    assert result.root == _ROOT
    assert result.nodes == frozenset({_ROOT})
    assert result.edges == ()
    assert result.depth == 0


def test_expand_one_hop_returns_1hop_neighbours(engine: Engine) -> None:
    """``depth=1`` reaches direct neighbours in both directions."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # root → A (outgoing)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    # root → B (outgoing)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=1)

    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B")})
    assert len(result.edges) == 2


def test_expand_two_hop_returns_2hop_neighbours(engine: Engine) -> None:
    """``depth=2`` reaches transitive neighbours through a linear chain."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # root → A
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    # A → B
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="A",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=2)

    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B")})
    assert len(result.edges) == 2


def test_expand_bidirectional_includes_both_incoming_and_outgoing(
    engine: Engine,
) -> None:
    """Expansion follows both ``from_*`` and ``to_*`` edges from each node."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # root → A (outgoing from root)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    # B → root (incoming to root)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="B",
        to_entity_type="task",
        to_entity_id="root",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=1)

    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B")})
    assert len(result.edges) == 2


def test_expand_terminates_on_cycle(engine: Engine) -> None:
    """A directed cycle (root → A → root) terminates without infinite recursion."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="A",
        to_entity_type="task",
        to_entity_id="root",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    # depth=3 with a 2-node cycle would loop forever if visited
    # tracking were missing; the assertion is that this call
    # *returns* — the specific result shape is secondary.
    result = service.expand(*_ROOT, depth=3)

    assert result.nodes == frozenset({_ROOT, ("task", "A")})
    # Two distinct edges form the cycle.
    assert len(result.edges) == 2


def test_expand_deduplicates_edges_visited_via_multiple_paths(
    engine: Engine,
) -> None:
    """A diamond (root → A, root → B, A → C, B → C) yields exactly 4 edges."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="A",
        to_entity_type="task",
        to_entity_id="C",
        link_type="manual",
        created_at=base + timedelta(seconds=2),
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="B",
        to_entity_type="task",
        to_entity_id="C",
        link_type="manual",
        created_at=base + timedelta(seconds=3),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=2)

    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B"), ("task", "C")})
    # Exactly the 4 seeded edges — no dupes even though C is
    # reachable via both A and B.
    assert len(result.edges) == 4


def test_expand_filters_by_link_types(engine: Engine) -> None:
    """``link_types`` filter is applied at every hop, not just the first."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # root → A as applied_to (kept)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="proposal",
        to_entity_id="A",
        link_type="applied_to",
        created_at=base,
    )
    # root → B as manual (filtered out)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="proposal",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )
    # A → C as applied_to (kept — second hop must still apply filter)
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="A",
        to_entity_type="briefing",
        to_entity_id="C",
        link_type="applied_to",
        created_at=base + timedelta(seconds=2),
    )
    # A → D as manual (filtered out at second hop)
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="A",
        to_entity_type="briefing",
        to_entity_id="D",
        link_type="manual",
        created_at=base + timedelta(seconds=3),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=2, link_types=["applied_to"])

    assert result.nodes == frozenset({_ROOT, ("proposal", "A"), ("briefing", "C")})
    assert len(result.edges) == 2
    assert all(edge.link_type == "applied_to" for edge in result.edges)


def test_expand_respects_depth_limit_stops_at_boundary(engine: Engine) -> None:
    """In a chain root → A → B → C, ``depth=2`` excludes C from the result."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="A",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="B",
        to_entity_type="task",
        to_entity_id="C",
        link_type="manual",
        created_at=base + timedelta(seconds=2),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=2)

    assert ("task", "C") not in result.nodes
    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B")})


def test_expand_default_depth_is_2(engine: Engine) -> None:
    """When ``depth`` is omitted the effective expansion is 2 hops."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Build chain root → A → B → C. Default depth=2 should
    # surface root, A, B but NOT C.
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="A",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="B",
        to_entity_type="task",
        to_entity_id="C",
        link_type="manual",
        created_at=base + timedelta(seconds=2),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT)  # depth defaults to 2

    assert result.depth == 2
    assert result.nodes == frozenset({_ROOT, ("task", "A"), ("task", "B")})


def test_expand_max_depth_exceeded_raises_config_error(engine: Engine) -> None:
    """``depth > 5`` raises :class:`ConfigError` mentioning the max."""
    service = LinkService(engine)
    with pytest.raises(ConfigError) as excinfo:
        service.expand(*_ROOT, depth=6)
    assert "5" in str(excinfo.value)


def test_expand_negative_depth_raises_config_error(engine: Engine) -> None:
    """Negative depth is a programmer error and surfaces immediately."""
    service = LinkService(engine)
    with pytest.raises(ConfigError):
        service.expand(*_ROOT, depth=-1)


def test_expand_unreached_entity_returns_empty_graph(engine: Engine) -> None:
    """An entity with no links yields ``{root}`` and no edges."""
    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=2)

    assert result.root == _ROOT
    assert result.nodes == frozenset({_ROOT})
    assert result.edges == ()
    assert result.depth == 2


def test_expand_edges_sorted_by_created_at_ascending(engine: Engine) -> None:
    """Edges are returned in chronological order regardless of insert order."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Insert in reverse chronological order so an unsorted result
    # would expose the bug.
    third_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="C",
        link_type="manual",
        created_at=base + timedelta(hours=2),
    )
    first_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="A",
        link_type="manual",
        created_at=base,
    )
    second_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="root",
        to_entity_type="task",
        to_entity_id="B",
        link_type="manual",
        created_at=base + timedelta(hours=1),
    )

    service = LinkService(engine)
    result = service.expand(*_ROOT, depth=1)

    assert [edge.id for edge in result.edges] == [first_id, second_id, third_id]


# ---- dataclass smoke ------------------------------------------------------


def test_graph_subset_is_frozen_dataclass() -> None:
    """:class:`GraphSubset` is immutable.

    Frozen + slots is part of the contract: callers may put graph
    subsets into caches keyed by ``(root, depth)`` and rely on the
    instance not mutating beneath them.
    """
    subset = GraphSubset(
        root=("task", "root"),
        nodes=frozenset({("task", "root")}),
        edges=(),
        depth=0,
    )
    with pytest.raises((AttributeError, Exception)):
        # frozen dataclass: setattr fails. Accept either
        # FrozenInstanceError or AttributeError so we don't pin a
        # particular CPython implementation detail.
        subset.depth = 1  # type: ignore[misc]
