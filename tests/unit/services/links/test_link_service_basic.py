"""Tests for :class:`opshub.services.links.LinkService`.

Approach: hand-create the ``links`` table via SQLAlchemy
``links_table.create`` on a fresh SQLite engine and seed rows with
direct SQL inserts (bypassing the Phase 8 B2
:class:`~opshub.projections.links.LinksProjector` dispatch, which may
not yet be implemented in parallel branches). This keeps the C1
service tests independent of B2 — the only shared contract is the
``links_table`` schema pinned by Phase 8 A2.

The tests cover the three public methods of
:class:`~opshub.services.links.LinkService`:

* :meth:`related` — 1-hop neighbour queries with direction /
  link_type filters + limit + ``created_at`` ordering
* :meth:`trace` — recursive backward provenance traversal with
  depth limits + cycle detection (ADR-0017 §決定 (e))
* :meth:`find_link_id` — natural-key lookup helper used by
  Phase 8 D1's ``opshub link remove`` CLI path

Cycle detection is exercised in three shapes:

1. ``test_trace_detects_cycle_and_does_not_loop`` — bidirectional
   ``A → B → A`` cycle.
2. ``test_trace_self_loop_does_not_infinite_recurse`` — a self-loop
   on a single entity.
3. ``test_trace_multiple_paths_share_visited_state_per_call`` —
   visited tracking is per-invocation (does not leak between
   ``trace()`` calls).
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
from opshub.services.links import Link, LinkPath, LinkService

# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite engine with only the ``links`` table created.

    We sidestep Alembic on purpose: the LinkService test surface only
    touches ``links``, and ``links_table.create`` keeps the fixture
    cheap and independent of every other migration. The migration
    integration test (``tests/integration/test_phase8_migrations.py``)
    covers the live DB schema separately.
    """
    db_path = tmp_path / "link_service.sqlite"
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
    """Insert one ``links`` row directly and return its id.

    Bypasses :class:`~opshub.projections.links.LinksProjector` so
    these tests don't depend on Phase 8 B2's dispatch logic being
    complete.
    """
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


# Convenience identities used across multiple seeds. ULIDs are not
# required here (the service treats them as opaque strings) so we use
# legible names that make the seeded graph readable in the assertions.
_TASK_A = ("task", "task-A")
_TASK_B = ("task", "task-B")
_PROPOSAL_P = ("proposal", "proposal-P")
_PROPOSAL_Q = ("proposal", "proposal-Q")
_BRIEFING_X = ("briefing", "briefing-X")
_DECISION_D = ("decision", "decision-D")


# ---- related --------------------------------------------------------------


def test_related_outgoing_returns_outgoing_links_only(engine: Engine) -> None:
    """``direction="outgoing"`` filters to ``from_*`` matches only."""
    # task-A → proposal-P (outgoing from task-A)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )
    # task-A → proposal-Q (outgoing from task-A)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-Q",
        link_type="manual",
    )
    # task-B → task-A (incoming to task-A)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-B",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.related(*_TASK_A, direction="outgoing")

    assert len(result) == 2
    assert all(link.from_entity_id == "task-A" for link in result)
    assert {link.to_entity_id for link in result} == {"proposal-P", "proposal-Q"}


def test_related_incoming_returns_incoming_links_only(engine: Engine) -> None:
    """``direction="incoming"`` filters to ``to_*`` matches only."""
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-Q",
        link_type="manual",
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-B",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.related(*_TASK_A, direction="incoming")

    assert len(result) == 1
    assert result[0].to_entity_id == "task-A"
    assert result[0].from_entity_id == "task-B"


def test_related_both_returns_union(engine: Engine) -> None:
    """``direction="both"`` (default) returns outgoing + incoming."""
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-Q",
        link_type="manual",
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-B",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.related(*_TASK_A)  # direction="both" default

    assert len(result) == 3


def test_related_filters_by_link_types(engine: Engine) -> None:
    """``link_types`` restricts the result to the requested types."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="applied_to",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="briefing",
        to_entity_id="briefing-X",
        link_type="references",
        created_at=base + timedelta(seconds=1),
    )
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="decision",
        to_entity_id="decision-D",
        link_type="manual",
        created_at=base + timedelta(seconds=2),
    )

    service = LinkService(engine)
    result = service.related(*_TASK_A, link_types=["applied_to", "references"])

    assert len(result) == 2
    assert {link.link_type for link in result} == {"applied_to", "references"}


def test_related_respects_limit(engine: Engine) -> None:
    """``limit`` caps the number of returned rows at the SQL layer."""
    for i in range(5):
        _seed_link(
            engine,
            from_entity_type="task",
            from_entity_id="task-A",
            to_entity_type="proposal",
            to_entity_id=f"proposal-{i}",
            link_type="manual",
        )

    service = LinkService(engine)
    result = service.related(*_TASK_A, limit=2)

    assert len(result) == 2


def test_related_orders_by_created_at_ascending(engine: Engine) -> None:
    """Results are sorted by ``created_at`` ascending (oldest first)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Seed in reverse chronological order so an unordered query
    # would surface the bug.
    third_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-third",
        link_type="manual",
        created_at=base + timedelta(hours=2),
    )
    first_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-first",
        link_type="manual",
        created_at=base,
    )
    second_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-second",
        link_type="manual",
        created_at=base + timedelta(hours=1),
    )

    service = LinkService(engine)
    result = service.related(*_TASK_A, direction="outgoing")

    assert [link.id for link in result] == [first_id, second_id, third_id]


def test_related_returns_empty_when_no_links(engine: Engine) -> None:
    """An entity with no links produces an empty list (not an error)."""
    service = LinkService(engine)
    result = service.related("task", "non-existent-task")
    assert result == []


# ---- trace ----------------------------------------------------------------


def test_trace_returns_empty_when_no_incoming_links(engine: Engine) -> None:
    """An entity with no incoming links produces an empty path list."""
    # Seed an *outgoing* link so the table isn't empty (trace must
    # only follow incoming edges).
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.trace(*_TASK_A)

    assert result == []


def test_trace_single_hop_chain(engine: Engine) -> None:
    """A single incoming edge produces one ``LinkPath`` of depth 1."""
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-P",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="applied_to",
    )

    service = LinkService(engine)
    result = service.trace(*_TASK_A)

    assert len(result) == 1
    path = result[0]
    assert path.depth == 1
    assert len(path.links) == 1
    assert path.links[0].from_entity_id == "proposal-P"
    assert path.links[0].to_entity_id == "task-A"
    assert path.links[0].link_type == "applied_to"


def test_trace_multi_hop_chain(engine: Engine) -> None:
    """A chain ``briefing → proposal → task`` produces one depth-2 path."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # briefing-X → proposal-P (proposal-P is to_* side)
    _seed_link(
        engine,
        from_entity_type="briefing",
        from_entity_id="briefing-X",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="generated_from_briefing",
        created_at=base,
    )
    # proposal-P → task-A (task-A is to_* side)
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-P",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="applied_to",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.trace(*_TASK_A, depth=3)

    assert len(result) == 1
    path = result[0]
    assert path.depth == 2
    # path.links is ordered nearest-to-root first: proposal→task is
    # the direct incoming edge to task-A, then briefing→proposal is
    # the upstream edge.
    assert path.links[0].from_entity_id == "proposal-P"
    assert path.links[0].to_entity_id == "task-A"
    assert path.links[1].from_entity_id == "briefing-X"
    assert path.links[1].to_entity_id == "proposal-P"


def test_trace_respects_depth_limit(engine: Engine) -> None:
    """``depth=1`` truncates a multi-hop chain to one edge."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="briefing",
        from_entity_id="briefing-X",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="generated_from_briefing",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-P",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="applied_to",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.trace(*_TASK_A, depth=1)

    assert len(result) == 1
    path = result[0]
    assert path.depth == 1
    assert path.links[0].from_entity_id == "proposal-P"


def test_trace_default_depth_is_3(engine: Engine) -> None:
    """When ``depth`` is omitted the effective max is 3 hops."""
    # Build a 5-hop chain: e → d → c → b → a → root
    base = datetime(2026, 1, 1, tzinfo=UTC)
    chain = [
        ("e", "d"),
        ("d", "c"),
        ("c", "b"),
        ("b", "a"),
        ("a", "root"),
    ]
    for i, (frm, to) in enumerate(chain):
        _seed_link(
            engine,
            from_entity_type="task",
            from_entity_id=frm,
            to_entity_type="task",
            to_entity_id=to,
            link_type="manual",
            created_at=base + timedelta(seconds=i),
        )

    service = LinkService(engine)
    result = service.trace("task", "root")  # depth defaults to 3

    # With a single linear chain there's exactly one terminal path;
    # depth-3 means we see root←a, a←b, b←c (3 edges).
    assert len(result) == 1
    assert result[0].depth == 3


def test_trace_detects_cycle_and_does_not_loop(engine: Engine) -> None:
    """A ``B → A`` + ``A → B`` cycle terminates rather than looping."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # A → B (so B has incoming from A)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="task",
        to_entity_id="task-B",
        link_type="manual",
        created_at=base,
    )
    # B → A (so A has incoming from B)
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-B",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    # Tracing B: incoming is A → B; recurse into A; incoming to A is
    # B → A, but B is in visited → cycle closes here. Without cycle
    # detection this would recurse forever.
    result = service.trace("task", "task-B", depth=10)

    # We get a single path showing the cycle closing edge.
    assert len(result) == 1
    path = result[0]
    # The path includes both edges (A → B then the cycle-closing
    # B → A), capped by cycle detection at depth 2.
    assert path.depth == 2
    assert path.links[0].from_entity_id == "task-A"
    assert path.links[0].to_entity_id == "task-B"
    assert path.links[1].from_entity_id == "task-B"
    assert path.links[1].to_entity_id == "task-A"


def test_trace_max_depth_exceeded_raises_config_error(engine: Engine) -> None:
    """``depth > 10`` raises :class:`ConfigError` per ADR-0017 §決定 (e)."""
    service = LinkService(engine)
    with pytest.raises(ConfigError) as excinfo:
        service.trace(*_TASK_A, depth=11)
    assert "10" in str(excinfo.value)


def test_trace_filters_by_link_types(engine: Engine) -> None:
    """``link_types`` restricts the traversal to specified types."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # proposal-P → task-A as applied_to (kept)
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-P",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="applied_to",
        created_at=base,
    )
    # decision-D → task-A as manual (filtered out)
    _seed_link(
        engine,
        from_entity_type="decision",
        from_entity_id="decision-D",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.trace(*_TASK_A, link_types=["applied_to"])

    assert len(result) == 1
    assert result[0].links[0].link_type == "applied_to"
    assert result[0].links[0].from_entity_id == "proposal-P"


def test_trace_branching_returns_multiple_paths(engine: Engine) -> None:
    """An entity with multiple incoming links produces one path per branch."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-A-source",
        to_entity_type="task",
        to_entity_id="task-X",
        link_type="manual",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-B-source",
        to_entity_type="task",
        to_entity_id="task-X",
        link_type="manual",
        created_at=base + timedelta(seconds=1),
    )

    service = LinkService(engine)
    result = service.trace("task", "task-X", depth=3)

    assert len(result) == 2
    upstream_ids = {path.links[0].from_entity_id for path in result}
    assert upstream_ids == {"proposal-A-source", "proposal-B-source"}


# ---- find_link_id ---------------------------------------------------------


def test_find_link_id_returns_id_when_match_exists(engine: Engine) -> None:
    """Natural-key lookup returns the matching link's id."""
    link_id = _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )

    service = LinkService(engine)
    result = service.find_link_id(
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
    )
    assert result == link_id


def test_find_link_id_returns_none_when_no_match(engine: Engine) -> None:
    """Lookup with a non-existent natural-key tuple returns ``None``."""
    service = LinkService(engine)
    result = service.find_link_id(
        from_entity_type="task",
        from_entity_id="ghost",
        to_entity_type="proposal",
        to_entity_id="ghost",
        link_type="manual",
    )
    assert result is None


# ---- cycle detection edge cases -------------------------------------------


def test_trace_self_loop_does_not_infinite_recurse(engine: Engine) -> None:
    """An entity with a ``references`` self-link terminates traversal."""
    _seed_link(
        engine,
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="task",
        to_entity_id="task-A",
        link_type="references",
    )

    service = LinkService(engine)
    # Self-loop: task-A has one incoming edge from task-A itself.
    # task-A is in visited from the start, so the recursion closes
    # the cycle immediately on the first incoming edge.
    result = service.trace(*_TASK_A, depth=5)

    assert len(result) == 1
    path = result[0]
    assert path.depth == 1
    assert path.links[0].from_entity_id == "task-A"
    assert path.links[0].to_entity_id == "task-A"


def test_trace_multiple_paths_share_visited_state_per_call(engine: Engine) -> None:
    """Visited tracking is per-invocation; consecutive ``trace`` calls don't leak."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Two independent provenance chains, both ending at task-shared.
    # If the visited set leaked across calls, the second invocation
    # would skip nodes it already visited in the first.
    _seed_link(
        engine,
        from_entity_type="briefing",
        from_entity_id="briefing-1",
        to_entity_type="proposal",
        to_entity_id="proposal-1",
        link_type="generated_from_briefing",
        created_at=base,
    )
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-1",
        to_entity_type="task",
        to_entity_id="task-shared",
        link_type="applied_to",
        created_at=base + timedelta(seconds=1),
    )
    _seed_link(
        engine,
        from_entity_type="briefing",
        from_entity_id="briefing-2",
        to_entity_type="proposal",
        to_entity_id="proposal-2",
        link_type="generated_from_briefing",
        created_at=base + timedelta(seconds=2),
    )
    _seed_link(
        engine,
        from_entity_type="proposal",
        from_entity_id="proposal-2",
        to_entity_type="task",
        to_entity_id="task-shared",
        link_type="applied_to",
        created_at=base + timedelta(seconds=3),
    )

    service = LinkService(engine)
    # First trace: should see two distinct upstream chains.
    first = service.trace("task", "task-shared", depth=3)
    # Second trace: same call again should yield the same result —
    # i.e. no leakage of visited state from the first invocation.
    second = service.trace("task", "task-shared", depth=3)

    assert len(first) == 2
    assert len(second) == 2
    first_terminals = {path.links[-1].from_entity_id for path in first}
    second_terminals = {path.links[-1].from_entity_id for path in second}
    assert first_terminals == second_terminals == {"briefing-1", "briefing-2"}


# ---- dataclass smoke ------------------------------------------------------


def test_link_and_link_path_are_frozen_dataclasses() -> None:
    """:class:`Link` / :class:`LinkPath` are immutable value types.

    Frozen dataclasses are part of the contract: callers may put
    :class:`Link` instances into sets (e.g. visited tracking in
    higher-level expanders) and rely on stable hashing.
    """
    link = Link(
        id="L1",
        from_entity_type="task",
        from_entity_id="task-A",
        to_entity_type="proposal",
        to_entity_id="proposal-P",
        link_type="manual",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_event_id=None,
        metadata=None,
    )
    path = LinkPath(links=(link,), depth=1)

    with pytest.raises((AttributeError, Exception)):
        # frozen dataclass: setattr fails. We accept either
        # FrozenInstanceError (dataclass) or AttributeError
        # (slots+frozen) so the test doesn't pin a particular
        # CPython implementation detail.
        link.id = "L2"  # type: ignore[misc]
    with pytest.raises((AttributeError, Exception)):
        path.depth = 2  # type: ignore[misc]
