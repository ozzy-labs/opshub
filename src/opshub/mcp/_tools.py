"""Read-tool handlers for the MCP server (ADR-0022 §(c) read namespace).

The handlers in this module satisfy the ``ToolHandler`` protocol
declared in :mod:`opshub.mcp._registry` for every entry in
:class:`ReadCategory`. Each handler:

* receives the parsed argument mapping (already validated against
  ``input_schema`` by the MCP request layer);
* runs a single read query against the SQLite store;
* returns a compact JSON string;
* never echoes raw body text — :func:`opshub.mcp._redact.redact_secrets`
  runs once on the final string in :mod:`opshub.mcp.server`.

Returning ``str`` (rather than a Python ``list[dict]``) keeps the
boundary thin: the server wraps the string in a single ``TextContent``
block, the redactor applies to that string, and the JSON body is what
both the agent and the OTel call records see. The schema is documented
in :mod:`opshub.mcp._registry`.

Service / projection imports happen at handler-call time (not at module
import) so ``opshub --help`` cold start does not pay for them. The
shared :class:`Engine` is built once per ``opshub mcp serve`` lifetime
in :func:`opshub.mcp.server.create_handlers` and threaded through the
factory closures below.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.mcp._registry import ToolHandler
    from opshub.services.recall_service import RecallHit, RecallService


__all__ = [
    "build_decision_list_handler",
    "build_inbox_list_handler",
    "build_recall_search_handler",
    "build_task_list_handler",
]


# Hard cap on rendered snippet length. Mirrors ADR-0022 §(d) — we
# never push a full body into the agent context window without an
# explicit caller request.
_SNIPPET_MAX_CHARS = 200


def _truncate(text: str | None, limit: int = _SNIPPET_MAX_CHARS) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _json_dump(payload: object) -> str:
    """Serialise a tool response payload to a stable, compact JSON string."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------- recall


def build_recall_search_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``recall.search``.

    Wiring the engine via a closure (instead of importing
    :func:`opshub.cli._wiring.build_recall_service` at module top) keeps
    the cold-start budget intact and lets unit tests stub the engine
    cheaply.

    The handler degrades gracefully when the operator has not yet
    configured an embedder backend: a clean ``ConfigError`` flows out
    of the recall service and is reported through the standard MCP
    ``isError`` path by the server wrapper.
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        # Lazy imports keep ``opshub --help`` fast (ADR-0001) and
        # let the import error from a missing extras (vector backend
        # not installed) surface only when the operator actually
        # invokes the tool.
        from opshub.cli._wiring import build_recall_service

        query: str = arguments["query"]
        entity_type: str | None = arguments.get("entity_type")
        limit: int = int(arguments.get("limit", 5))

        _ = engine  # recall builder owns its own engine resolution
        service: RecallService = build_recall_service()
        hits: list[RecallHit] = service.recall(query, entity_type=entity_type, limit=limit)
        return _json_dump(
            {
                "query": query,
                "entity_type": entity_type,
                "hits": [
                    {
                        "entity_type": hit.entity_type,
                        "entity_id": hit.entity_id,
                        "title": _truncate(hit.title),
                        "snippet": _truncate(hit.snippet),
                        "score": round(float(hit.score), 6),
                    }
                    for hit in hits
                ],
                "truncated_snippets": True,
            }
        )

    return handler


# ---------------------------------------------------------------------- tasks


def build_task_list_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``task.list``.

    Queries the ``tasks`` projection directly (read-side). Order
    matches ``opshub task list`` (``updated_at DESC, id ASC``).
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        from sqlalchemy import select

        from opshub.projections.tasks import tasks_table

        state: str | None = arguments.get("state")
        limit: int = int(arguments.get("limit", 20))

        stmt = select(
            tasks_table.c.id,
            tasks_table.c.title,
            tasks_table.c.state,
            tasks_table.c.updated_at,
        )
        if state is not None:
            stmt = stmt.where(tasks_table.c.state == state)
        stmt = stmt.order_by(
            tasks_table.c.updated_at.desc(),
            tasks_table.c.id.asc(),
        ).limit(limit)

        with engine.connect() as conn:
            rows = conn.execute(stmt).all()

        return _json_dump(
            {
                "state_filter": state,
                "items": [
                    {
                        "id": row.id,
                        "title": _truncate(row.title),
                        "state": row.state,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    }
                    for row in rows
                ],
            }
        )

    return handler


# ---------------------------------------------------------------------- inbox


def build_inbox_list_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``inbox.list``.

    Queries the ``inbox_items`` projection directly. State values
    follow the projection check constraint (see
    :mod:`opshub.projections.inbox`).
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        from sqlalchemy import select

        from opshub.projections.inbox import inbox_items_table

        state: str | None = arguments.get("state")
        limit: int = int(arguments.get("limit", 20))

        stmt = select(
            inbox_items_table.c.id,
            inbox_items_table.c.summary,
            inbox_items_table.c.state,
            inbox_items_table.c.created_at,
        )
        if state is not None:
            stmt = stmt.where(inbox_items_table.c.state == state)
        stmt = stmt.order_by(
            inbox_items_table.c.created_at.desc(),
            inbox_items_table.c.id.asc(),
        ).limit(limit)

        with engine.connect() as conn:
            rows = conn.execute(stmt).all()

        return _json_dump(
            {
                "state_filter": state,
                "items": [
                    {
                        "id": row.id,
                        "summary": _truncate(row.summary),
                        "state": row.state,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ],
            }
        )

    return handler


# ------------------------------------------------------------------- decisions


def build_decision_list_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``decision.list``.

    Decisions have no ``state`` column (see ADR-0002 immutability for
    the rationale), so only the ``limit`` filter is exposed.
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        from sqlalchemy import select

        from opshub.projections.decisions import decisions_table

        limit: int = int(arguments.get("limit", 20))

        stmt = (
            select(
                decisions_table.c.id,
                decisions_table.c.text,
                decisions_table.c.created_at,
            )
            .order_by(
                decisions_table.c.created_at.desc(),
                decisions_table.c.id.asc(),
            )
            .limit(limit)
        )

        with engine.connect() as conn:
            rows = conn.execute(stmt).all()

        return _json_dump(
            {
                "items": [
                    {
                        "id": row.id,
                        "text": _truncate(row.text),
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ],
            }
        )

    return handler
