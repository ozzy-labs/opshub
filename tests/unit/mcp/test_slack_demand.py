"""Phase 18-C unit tests for the ``slack.demand.list`` MCP handler.

Pins the contract for the new read tool added by ADR-0033 §決定 (c):

1. Empty table → ``items=[]``, ``total=0``, ``truncated=False``,
   ``next_offset=None``.
2. ``types`` filter restricts rows to the selected ``channel_type``
   values (e.g. ``types=["im"]`` returns DM rows only).
3. ``demand_kinds`` filter restricts rows to the selected
   ``demand_kind`` values (e.g. ``demand_kinds=["mention"]``).
4. ``since_ts`` filter excludes rows whose ``last_demand_ts`` is
   strictly older than the threshold.
5. ``limit`` trims the page size; the pagination hint pair
   (``truncated`` / ``next_offset``) follows the ADR-0022 §(d)
   envelope.
6. Order is fixed at ``last_demand_desc`` (newest first) per
   ADR-0033 §決定 (e); the secondary ``team_id ASC, channel_id ASC``
   (Phase 24-D natural-key prefix) keeps deterministic ordering on
   ties.
7. Phase 24-D (ADR-0041 §(g), issue #556): every item carries a
   ``workspace`` object (``team_id`` + best-effort ``alias`` resolved
   from the Slack cursor binding), and rows are keyed per workspace so
   the same channel id may appear once per ``team_id``.

The handler queries the projection table directly (not the rebuild
driver), so the tests seed the rows with ``insert(...)`` instead of
appending ``SourceObserved`` events through the event store —
projection-side behaviour is already covered by the Phase 18-B unit
+ integration tests (``tests/unit/projections/test_slack_demand_digest.py``
and ``tests/integration/test_slack_demand_digest_rebuild.py``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.mcp._tools import build_slack_demand_list_handler
from opshub.projections.slack_demand_digest import slack_demand_digest_table
from opshub.projections.sources import sources_table

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB with the ``slack_demand_digest`` table.

    The handler does not require the full projection registry — the
    one table is enough for unit-scope assertions about filtering /
    ordering / pagination.
    """
    db_path = tmp_path / "slack_demand_mcp.sqlite"
    eng = create_engine_for_sqlite(db_path)
    try:
        # ``sources`` first — ``slack_demand_digest.last_source_id`` is
        # a FK to ``sources.id``, so SQLite needs both tables present
        # even though the FK is ``ON DELETE SET NULL`` and the tests
        # never join through it.
        sources_table.create(eng)
        slack_demand_digest_table.create(eng)
        yield eng
    finally:
        eng.dispose()


def _seed_row(
    engine: Engine,
    *,
    channel_id: str,
    channel_type: str,
    demand_kind: str,
    last_demand_ts: float,
    team_id: str = "T0TEST",
    channel_name: str | None = None,
    last_demand_user_id: str | None = None,
    last_demand_excerpt: str | None = None,
    last_demand_permalink: str | None = None,
    last_source_id: str | None = None,
) -> None:
    """Insert one digest row directly (bypassing the projection)."""
    with engine.begin() as conn:
        conn.execute(
            insert(slack_demand_digest_table).values(
                team_id=team_id,
                channel_id=channel_id,
                channel_type=channel_type,
                channel_name=channel_name,
                demand_kind=demand_kind,
                last_demand_ts=last_demand_ts,
                last_demand_user_id=last_demand_user_id,
                last_demand_excerpt=last_demand_excerpt,
                last_demand_permalink=last_demand_permalink,
                last_source_id=last_source_id,
                updated_at=_NOW,
            )
        )


def _parse(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# 1. Empty
# ---------------------------------------------------------------------------


async def test_handler_returns_empty_envelope_when_table_is_empty(engine: Engine) -> None:
    """An empty projection produces ``items=[]`` + ``total=0`` envelope.

    Also pins the pagination hint pair to ``truncated=False`` +
    ``next_offset=null`` so a host can branch on the boolean without
    inspecting ``len(items)``.
    """
    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["truncated"] is False
    assert payload["next_offset"] is None


# ---------------------------------------------------------------------------
# 2. types filter
# ---------------------------------------------------------------------------


async def test_handler_types_filter_returns_only_dm_rows(engine: Engine) -> None:
    """``types=["im"]`` returns DM rows only; public channels excluded.

    Seeds one row per channel_type and asserts that only the DM row
    (``channel_type="im"``) is returned. This is the canonical
    next-actions Step 2 use case: "show me my DMs only".
    """
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.0,
        channel_name="alice",
        last_demand_excerpt="quick question",
        last_demand_permalink="https://example.slack.com/archives/D100DMA/p1700000020",
        last_source_id=None,
    )
    _seed_row(
        engine,
        channel_id="C200PUB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000010.0,
        channel_name="general",
        last_demand_excerpt="<@U_SELF> please review",
        last_source_id=None,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"types": ["im"]}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert [row["channel_id"] for row in items] == ["D100DMA"]
    assert items[0]["channel_type"] == "im"
    assert items[0]["demand_kind"] == "dm"
    assert payload["total"] == 1


# ---------------------------------------------------------------------------
# 3. demand_kinds filter
# ---------------------------------------------------------------------------


async def test_handler_demand_kinds_filter_returns_only_mentions(engine: Engine) -> None:
    """``demand_kinds=["mention"]`` excludes ``dm`` rows."""
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.0,
    )
    _seed_row(
        engine,
        channel_id="C200PUB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000010.0,
    )
    _seed_row(
        engine,
        channel_id="C300PUB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000030.0,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"demand_kinds": ["mention"]}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert {row["demand_kind"] for row in items} == {"mention"}
    # Newest-first order pins ``C300PUB`` (ts=1700000030) ahead of
    # ``C200PUB`` (ts=1700000010).
    assert [row["channel_id"] for row in items] == ["C300PUB", "C200PUB"]


# ---------------------------------------------------------------------------
# 4. since_ts filter
# ---------------------------------------------------------------------------


async def test_handler_since_ts_excludes_older_rows(engine: Engine) -> None:
    """``since_ts`` is a half-open lower bound on ``last_demand_ts``.

    Rows whose ``last_demand_ts`` is **strictly less than** ``since_ts``
    must be excluded. A row exactly at ``since_ts`` is included (the
    ``>=`` semantics mirror the ADR-0022 H1 physical-column time
    filters on other read tools).
    """
    _seed_row(
        engine,
        channel_id="D_OLD",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1699000000.0,
    )
    _seed_row(
        engine,
        channel_id="D_EDGE",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000000.0,
    )
    _seed_row(
        engine,
        channel_id="D_NEW",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000100.0,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"since_ts": 1700000000.0}))
    items = cast("list[dict[str, Any]]", payload["items"])
    # ``D_OLD`` is filtered out; ``D_EDGE`` (== threshold) and ``D_NEW``
    # (> threshold) survive.
    assert [row["channel_id"] for row in items] == ["D_NEW", "D_EDGE"]


# ---------------------------------------------------------------------------
# 5. limit + pagination hint
# ---------------------------------------------------------------------------


async def test_handler_limit_trims_results_and_sets_pagination_hint(engine: Engine) -> None:
    """``limit`` trims the page; ``truncated`` + ``next_offset`` follow §(d)."""
    for i in range(5):
        _seed_row(
            engine,
            channel_id=f"C{i:03d}PUB",
            channel_type="public",
            demand_kind="mention",
            last_demand_ts=1700000000.0 + i,
        )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"limit": 2}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 2
    assert payload["total"] == 2
    # Page is full → ``truncated=True`` + ``next_offset=limit``.
    assert payload["truncated"] is True
    assert payload["next_offset"] == 2


async def test_handler_pagination_hint_not_truncated_when_partial(engine: Engine) -> None:
    """When the result row count is below ``limit``, the hint is ``null``."""
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.0,
    )
    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"limit": 50}))
    assert payload["truncated"] is False
    assert payload["next_offset"] is None
    assert payload["total"] == 1


# ---------------------------------------------------------------------------
# 6. order — last_demand_desc + secondary channel_id ASC
# ---------------------------------------------------------------------------


async def test_handler_orders_by_last_demand_ts_desc(engine: Engine) -> None:
    """ADR-0033 §決定 (e) — sort key is ``last_demand_ts DESC``.

    The handler's hard-coded order (no static type tier) means a DM
    with a newer ``last_demand_ts`` outranks a mention with an older
    one even though host-side priority might tier them differently.
    """
    _seed_row(
        engine,
        channel_id="C100PUB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000010.0,
    )
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.0,
    )
    _seed_row(
        engine,
        channel_id="C200PUB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000030.0,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"order": "last_demand_desc"}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert [row["channel_id"] for row in items] == ["C200PUB", "D100DMA", "C100PUB"]


async def test_handler_secondary_order_is_channel_id_ascending(engine: Engine) -> None:
    """Tie-breaker on equal ``last_demand_ts`` is ``team_id, channel_id ASC``.

    Two rows with the same ts must appear in a deterministic order
    so pagination boundaries do not flip between calls. (Same
    ``team_id`` here — the cross-workspace arm of the tiebreaker is
    pinned by ``test_handler_same_channel_id_across_workspaces...``.)
    """
    _seed_row(
        engine,
        channel_id="C_BBB",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000050.0,
    )
    _seed_row(
        engine,
        channel_id="C_AAA",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000050.0,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert [row["channel_id"] for row in items] == ["C_AAA", "C_BBB"]


# ---------------------------------------------------------------------------
# 7. Item shape — every projection column surfaces
# ---------------------------------------------------------------------------


async def test_handler_item_shape_includes_all_projection_columns(engine: Engine) -> None:
    """Every ``slack_demand_digest`` column reaches the JSON envelope.

    The skills (``next-actions`` / ``personal-brief``) rely on
    ``last_demand_permalink`` for the Slack web URL and
    ``last_source_id`` to chain into ``source.get`` for full context.
    A regression that drops one of those columns is exactly what
    this pin catches.
    """
    # Seed a matching ``sources`` row so the FK ``last_source_id`` →
    # ``sources.id`` resolves. The handler does not join through it,
    # but the SQLite FK constraint enforces existence at insert time.
    source_id = "01HSOURCE0000000000DMA"
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name="slack",
                external_id="D100DMA:1700000020.500000",
                source_type="slack_message",
                title="alice in #alice: quick question",
                url=None,
                summary="quick question about phase 18-c",
                # epic #470 / issue #481: ``sources.body`` is NOT NULL.
                body="quick question about phase 18-c",
                fingerprint=None,
                provenance_origin="external",
                provenance_trust="untrusted",
                observed_at=_NOW,
                updated_at=_NOW,
            )
        )
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.5,
        channel_name="alice",
        last_demand_user_id="U_ALICE",
        last_demand_excerpt="quick question about phase 18-c",
        last_demand_permalink="https://example.slack.com/archives/D100DMA/p1700000020500000",
        last_source_id=source_id,
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 1
    row = items[0]
    # Phase 23-D (issue #534): the epoch float ``last_demand_ts`` is
    # rendered as an ISO 8601 UTC string ``last_demand_at`` so the read
    # tool speaks the same timestamp dialect as ``task.list`` etc.
    assert row == {
        # Phase 24-D (ADR-0041 §(g)): the workspace object carries the
        # stable team_id; ``alias`` is ``None`` here because the test
        # DB has no Slack cursor binding to resolve it from.
        "workspace": {"team_id": "T0TEST", "alias": None},
        "channel_id": "D100DMA",
        "channel_type": "im",
        "channel_name": "alice",
        "demand_kind": "dm",
        "last_demand_at": "2023-11-14T22:13:40.500000+00:00",
        "last_demand_user_id": "U_ALICE",
        "last_demand_excerpt": "quick question about phase 18-c",
        "last_demand_permalink": "https://example.slack.com/archives/D100DMA/p1700000020500000",
        "last_source_id": source_id,
    }
    # The raw epoch float must NOT leak alongside the ISO field.
    assert "last_demand_ts" not in row


# ---------------------------------------------------------------------------
# 7b. last_demand_at — ISO 8601 UTC rendering (Phase 23-D, issue #534)
# ---------------------------------------------------------------------------


async def test_handler_renders_last_demand_at_as_iso_utc(engine: Engine) -> None:
    """The epoch ``last_demand_ts`` column surfaces as ISO 8601 ``last_demand_at``.

    Issue #534 §3: every read tool emits timestamps via ``.isoformat()``.
    The projection stores a Slack epoch float; the handler converts it
    to a UTC ISO string at the boundary so a skill never has to do
    epoch arithmetic on Slack's ``ts`` format.
    """
    _seed_row(
        engine,
        channel_id="D900ISO",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000000.0,
    )
    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert items[0]["last_demand_at"] == "2023-11-14T22:13:20+00:00"
    assert "last_demand_ts" not in items[0]


# ---------------------------------------------------------------------------
# 8. Unknown filter values are silently dropped (defensive)
# ---------------------------------------------------------------------------


async def test_handler_unknown_type_filter_values_are_silently_dropped(engine: Engine) -> None:
    """Unknown ``types`` values are filtered out before reaching SQL.

    The MCP schema enum guards the boundary, but the handler also
    drops unknown values defensively so a stray test arg or future
    schema relaxation cannot inject arbitrary strings into the
    ``WHERE channel_type IN (...)`` clause. When every value is
    unknown the filter collapses to an empty tuple, which returns
    no rows.
    """
    _seed_row(
        engine,
        channel_id="D100DMA",
        channel_type="im",
        demand_kind="dm",
        last_demand_ts=1700000020.0,
    )
    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({"types": ["unknown_type"]}))
    assert payload["items"] == []
    assert payload["total"] == 0


# ---------------------------------------------------------------------------
# 9. Workspace axis (Phase 24-D, ADR-0041 §(g), issue #556)
# ---------------------------------------------------------------------------


async def test_handler_same_channel_id_across_workspaces_yields_two_items(
    engine: Engine,
) -> None:
    """The same channel id under two workspaces produces two distinct items.

    Channel ids are only unique within one Slack workspace; the Phase
    24-D ``(team_id, channel_id, demand_kind)`` re-key keeps the two
    conversations apart. The tie on ``last_demand_ts`` also pins the
    cross-workspace arm of the tiebreaker (``team_id ASC``).
    """
    _seed_row(
        engine,
        channel_id="C0COLLIDE",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000060.0,
        team_id="T_BBB",
    )
    _seed_row(
        engine,
        channel_id="C0COLLIDE",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000060.0,
        team_id="T_AAA",
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert [(row["workspace"]["team_id"], row["channel_id"]) for row in items] == [
        ("T_AAA", "C0COLLIDE"),
        ("T_BBB", "C0COLLIDE"),
    ]


async def test_handler_workspace_alias_resolved_from_cursor_binding(engine: Engine) -> None:
    """``workspace.alias`` resolves through the Slack cursor's per-alias binding.

    The Phase 24-C cursor envelope binds each configured alias to its
    workspace ``team_id``; the handler translates the stored stable id
    back into the operator-facing label. A team_id with no binding
    (cursor reset / alias removed) degrades to ``alias=null``.
    """
    from opshub.projections.connector_cursors import connector_cursors_table

    connector_cursors_table.create(engine)
    cursor_json = json.dumps(
        {
            "workspaces": {
                "acme": {
                    "channels": {},
                    "backfill": {},
                    "threads": {},
                    "team_id": "T_ACME",
                }
            }
        }
    )
    with engine.begin() as conn:
        conn.execute(
            insert(connector_cursors_table).values(
                connector_name="slack",
                cursor_value=cursor_json,
                updated_at=_NOW,
                last_synced_at=_NOW,
            )
        )

    _seed_row(
        engine,
        channel_id="C0BOUND",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000070.0,
        team_id="T_ACME",
    )
    _seed_row(
        engine,
        channel_id="C0ORPHAN",
        channel_type="public",
        demand_kind="mention",
        last_demand_ts=1700000060.0,
        team_id="T_GONE",
    )

    handler = build_slack_demand_list_handler(engine)
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert items[0]["workspace"] == {"team_id": "T_ACME", "alias": "acme"}
    assert items[1]["workspace"] == {"team_id": "T_GONE", "alias": None}
