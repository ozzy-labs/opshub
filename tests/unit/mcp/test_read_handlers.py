"""Regression tests for the real MCP read-tool handlers (ADR-0022 §(c)).

The dispatch tests in :mod:`tests.unit.mcp.test_server_dispatch` stub
the handlers so they cannot catch regressions inside the real ones
(e.g. column-name typos against the projection tables). This module
exercises :func:`build_task_list_handler` /
:func:`build_inbox_list_handler` / :func:`build_decision_list_handler`
against an actual SQLAlchemy :class:`Engine` provisioned with the
projection schemas, so a typo like ``decisions.created_at`` (the
projection records ``recorded_at``) surfaces here instead of waiting
for the e2e test.

The recall handler intentionally goes through
:func:`opshub.cli._wiring.build_recall_service`, which resolves the
embedder + vector store from settings — covering it here would
duplicate the recall-service tests without adding signal. The
end-to-end recall MCP call is exercised by
:mod:`tests.integration.test_phase10_assistant_lifecycle`.

The tests are ``async def`` because the handlers themselves are async
(``ToolHandler = Callable[..., Awaitable[str]]``) and ``asyncio_mode =
"auto"`` in :file:`pyproject.toml` collects them as coroutine tests
automatically.
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
from opshub.mcp._tools import (
    build_decision_list_handler,
    build_inbox_list_handler,
    build_task_list_handler,
)
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.tasks import tasks_table

_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB with the three read-target projection tables."""
    db_path = tmp_path / "mcp_read_handlers.sqlite"
    eng = create_engine_for_sqlite(db_path)
    try:
        tasks_table.create(eng)
        inbox_items_table.create(eng)
        decisions_table.create(eng)
        yield eng
    finally:
        eng.dispose()


def _seed_task(engine: Engine, *, task_id: str, title: str, state: str = "draft") -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=title,
                body=None,
                state=state,
                result_note=None,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _seed_inbox(engine: Engine, *, item_id: str, summary: str, state: str = "pending") -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(inbox_items_table).values(
                id=item_id,
                summary=summary,
                source_ref=None,
                state=state,
                disposition=None,
                target_id=None,
                reason=None,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _seed_decision(engine: Engine, *, decision_id: str, decision_text: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(decisions_table).values(
                id=decision_id,
                text=decision_text,
                context=None,
                actor="test",
                recorded_at=_NOW,
            )
        )


def _parse(raw: str) -> dict[str, Any]:
    """Decode the handler's JSON response into a typed dict."""
    return cast("dict[str, Any]", json.loads(raw))


async def test_task_list_handler_returns_items_envelope(engine: Engine) -> None:
    _seed_task(engine, task_id="01HTASK0000000000000000000", title="probe task")
    handler = build_task_list_handler(engine)
    payload = _parse(await handler({"limit": 10}))
    items = cast("list[dict[str, Any]]", payload["items"])
    titles = [row["title"] for row in items]
    assert "probe task" in titles


async def test_inbox_list_handler_returns_items_envelope(engine: Engine) -> None:
    _seed_inbox(engine, item_id="01HINBOX000000000000000000", summary="probe inbox")
    handler = build_inbox_list_handler(engine)
    payload = _parse(await handler({"limit": 10}))
    items = cast("list[dict[str, Any]]", payload["items"])
    summaries = [row["summary"] for row in items]
    assert "probe inbox" in summaries


async def test_decision_list_handler_uses_recorded_at_not_created_at(engine: Engine) -> None:
    """Regression: ``decisions`` has no ``created_at`` column.

    The Phase 10 Sub C handler initially queried ``created_at`` which
    raises ``AttributeError`` on the SQLAlchemy column collection —
    the e2e lifecycle test in
    :mod:`tests.integration.test_phase10_assistant_lifecycle`
    discovered this, and the closeout PR pins it with a unit-level
    test against the real handler.
    """
    _seed_decision(
        engine,
        decision_id="01HDECISION0000000000000000",
        decision_text="adopt phase 10 assistant platform",
    )
    handler = build_decision_list_handler(engine)
    payload = _parse(await handler({"limit": 10}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    assert len(rows) == 1
    assert rows[0]["text"] == "adopt phase 10 assistant platform"
    # ``decisions.recorded_at`` is the authoritative timestamp on the
    # projection (ADR-0002 immutability). Pin the response field name
    # so a future rename has to update this test alongside.
    assert "recorded_at" in rows[0]
    assert cast("str", rows[0]["recorded_at"]).startswith("2026-05-30")


async def test_decision_list_handler_respects_limit(engine: Engine) -> None:
    """The ``limit`` argument must trim the result row count."""
    for i in range(3):
        _seed_decision(
            engine,
            decision_id=f"01HDECISIONABC{i:012d}",
            decision_text=f"decision-{i}",
        )
    handler = build_decision_list_handler(engine)
    payload = _parse(await handler({"limit": 2}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    assert len(rows) == 2


# ---------------------------------------------------------------------- F
# Phase 10 audit follow-up (Cluster 2): list handlers must surface
# ADR-0022 §(d) ``truncated`` / ``next_offset`` pagination hints so an
# agent can decide whether to issue a follow-up call without re-counting.


async def test_decision_list_hint_truncated_when_limit_reached(engine: Engine) -> None:
    """``truncated=True`` + ``next_offset=limit`` when the page is full."""
    for i in range(3):
        _seed_decision(
            engine,
            decision_id=f"01HDECISIONABC{i:012d}",
            decision_text=f"decision-{i}",
        )
    handler = build_decision_list_handler(engine)
    payload = _parse(await handler({"limit": 2}))
    assert payload["truncated"] is True
    assert payload["next_offset"] == 2


async def test_decision_list_hint_not_truncated_when_limit_not_reached(
    engine: Engine,
) -> None:
    """``truncated=False`` + ``next_offset=None`` when the page is partial."""
    _seed_decision(
        engine,
        decision_id="01HDECISIONABC000000000000",
        decision_text="only one",
    )
    handler = build_decision_list_handler(engine)
    payload = _parse(await handler({"limit": 5}))
    assert payload["truncated"] is False
    assert payload["next_offset"] is None


async def test_task_list_hint_truncated_when_limit_reached(engine: Engine) -> None:
    """``task.list`` carries the same pagination hint envelope."""
    for i in range(3):
        _seed_task(engine, task_id=f"01HTASK{i:019d}", title=f"task-{i}")
    handler = build_task_list_handler(engine)
    payload = _parse(await handler({"limit": 2}))
    assert payload["truncated"] is True
    assert payload["next_offset"] == 2


async def test_inbox_list_hint_not_truncated_when_partial(engine: Engine) -> None:
    """``inbox.list`` carries the same pagination hint envelope."""
    _seed_inbox(
        engine,
        item_id="01HINBOX000000000000000001",
        summary="only one",
    )
    handler = build_inbox_list_handler(engine)
    payload = _parse(await handler({"limit": 5}))
    assert payload["truncated"] is False
    assert payload["next_offset"] is None
