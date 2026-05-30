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
    from opshub.services.links import Link
    from opshub.services.recall_service import RecallHit, RecallService


__all__ = [
    "build_brief_handler",
    "build_decision_list_handler",
    "build_embeddings_find_duplicates_handler",
    "build_graph_expand_handler",
    "build_graph_related_handler",
    "build_graph_trace_handler",
    "build_inbox_list_handler",
    "build_recall_search_handler",
    "build_source_get_handler",
    "build_source_list_handler",
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


def _pagination_hint(item_count: int, limit: int) -> dict[str, object]:
    """Return the ADR-0022 §(d) pagination hint envelope.

    The hint pair ``truncated`` / ``next_offset`` lets an agent decide
    whether to issue a follow-up call without re-counting on its side.
    ``truncated`` is ``True`` when the handler returned exactly ``limit``
    rows (i.e. the projection may hold more), and ``False`` otherwise.
    ``next_offset`` mirrors ``truncated``: when truncated, the value is
    ``limit`` so the next page can be requested via offset-based
    queries; otherwise ``None`` (JSON ``null``) signals end-of-stream.

    Phase 10 C2 list handlers do not yet accept an ``offset`` argument
    (the agent surface mints fresh queries each turn). The next_offset
    hint is forward-compatible: when ``offset`` is added later, the
    same envelope keeps working.
    """
    truncated = item_count >= limit
    return {
        "truncated": truncated,
        "next_offset": limit if truncated else None,
    }


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
        # ``truncated_snippets`` is the **per-row** flag (snippets are
        # capped at ``_SNIPPET_MAX_CHARS`` chars). The ``truncated`` /
        # ``next_offset`` pair from :func:`_pagination_hint` is the
        # ADR-0022 §(d) **page-level** hint — separate concept, kept
        # alongside so an agent can distinguish "snippet was clipped"
        # from "there are more hits behind this page".
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
                **_pagination_hint(item_count=len(hits), limit=limit),
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

        items = [
            {
                "id": row.id,
                "title": _truncate(row.title),
                "state": row.state,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]
        return _json_dump(
            {
                "state_filter": state,
                "items": items,
                **_pagination_hint(item_count=len(items), limit=limit),
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

        items = [
            {
                "id": row.id,
                "summary": _truncate(row.summary),
                "state": row.state,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
        return _json_dump(
            {
                "state_filter": state,
                "items": items,
                **_pagination_hint(item_count=len(items), limit=limit),
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

        # ``decisions`` has no ``created_at`` column — the projection
        # records ``recorded_at`` (see ADR-0002 immutability + the
        # ``decisions_table`` definition). Use that column so the
        # handler does not raise ``AttributeError`` at first call.
        stmt = (
            select(
                decisions_table.c.id,
                decisions_table.c.text,
                decisions_table.c.recorded_at,
            )
            .order_by(
                decisions_table.c.recorded_at.desc(),
                decisions_table.c.id.asc(),
            )
            .limit(limit)
        )

        with engine.connect() as conn:
            rows = conn.execute(stmt).all()

        items = [
            {
                "id": row.id,
                "text": _truncate(row.text),
                "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
            }
            for row in rows
        ]
        return _json_dump(
            {
                "items": items,
                **_pagination_hint(item_count=len(items), limit=limit),
            }
        )

    return handler


# ---------------------------------------------------------------------- brief


def build_brief_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``brief``.

    Thin wrapper over :class:`opshub.services.briefings.BriefingService`
    so callers (agent hosts) can ask the same briefing flow as the
    ``opshub brief`` CLI without piping markdown through stdout. The
    response shape switches on the ``format`` argument:

    * ``format="md"`` (default): ``{"format":"md", "markdown": "...",
      "briefing_id": "...", "topic": "..."}`` — keeps the payload small
      for chat-style display.
    * ``format="json"``: full Briefing record (markdown + source_refs +
      model_id / model_version / token counts + generated_at).

    The service may raise :class:`ConfigError` when the LLM backend is
    disabled — the dispatch wrapper renders the exception as a
    redacted MCP ``isError`` per ADR-0022 §(b).
    """
    _ = engine  # build_briefing_service owns its own engine resolution

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_briefing_service

        topic: str = arguments["topic"]
        expand_graph = bool(arguments.get("expand_graph", False))
        fmt: str = arguments.get("format", "md")
        max_sources = int(arguments.get("max_sources", 20))
        max_tokens = int(arguments.get("max_tokens", 1500))

        service = build_briefing_service("mcp:brief")
        briefing = service.generate(
            topic,
            max_sources=max_sources,
            max_tokens=max_tokens,
            expand_graph=expand_graph,
        )
        if fmt == "json":
            return _json_dump(
                {
                    "format": "json",
                    "briefing_id": briefing.briefing_id,
                    "topic": briefing.topic,
                    "scope": briefing.scope,
                    "markdown": briefing.markdown,
                    "source_refs": [
                        {"entity_type": et, "entity_id": eid} for et, eid in briefing.source_refs
                    ],
                    "model_id": briefing.model_id,
                    "model_version": briefing.model_version,
                    "tokens_in": briefing.tokens_in,
                    "tokens_out": briefing.tokens_out,
                    "generated_at": briefing.generated_at.isoformat(),
                }
            )
        # Default: ``md`` — return the markdown body verbatim alongside
        # the briefing_id so the caller can correlate with the event log.
        return _json_dump(
            {
                "format": "md",
                "briefing_id": briefing.briefing_id,
                "topic": briefing.topic,
                "markdown": briefing.markdown,
                "source_count": len(briefing.source_refs),
            }
        )

    return handler


# ------------------------------------------------------------------ graph.*

# Translate the MCP-side direction labels to LinkService's internal
# ``Literal["outgoing", "incoming", "both"]`` vocabulary. The MCP
# surface uses ``outbound`` / ``inbound`` because that's the wording
# already documented in ``docs/secretary-agent.md`` and the natural
# language hosts will favour ("which entities point to this thing?").
_DIRECTION_MCP_TO_SERVICE: dict[str, str] = {
    "both": "both",
    "outbound": "outgoing",
    "inbound": "incoming",
}


def _link_to_dict(link: Link) -> dict[str, object]:
    """Render a :class:`~opshub.services.links.Link` as a JSON-safe dict.

    The :class:`Link` import is held under ``TYPE_CHECKING`` so the
    runtime cold-start guard stays intact (see :mod:`opshub.mcp._tools`
    module docstring). At call time the handler closures have already
    imported :class:`LinkService` (which carries :class:`Link` along
    via dataclass), so the type is available transparently for the
    static checker.
    """
    return {
        "id": link.id,
        "from_entity_type": link.from_entity_type,
        "from_entity_id": link.from_entity_id,
        "to_entity_type": link.to_entity_type,
        "to_entity_id": link.to_entity_id,
        "link_type": link.link_type,
        # ``Link.created_at`` is non-optional per the dataclass schema
        # (Phase 8 A2 — ``Column(..., nullable=False)``); the
        # ``.isoformat()`` always succeeds.
        "created_at": link.created_at.isoformat(),
        "source_event_id": link.source_event_id,
    }


def build_graph_related_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``graph.related``."""

    async def handler(arguments: Mapping[str, Any]) -> str:
        # Lazy import keeps cold start fast (ADR-0001) and avoids
        # pulling LinkService into the SDK import path until first call.
        from typing import Literal, cast

        from opshub.services.links import LinkService

        entity_id: str = arguments["entity_id"]
        entity_type: str = arguments["entity_type"]
        direction_mcp: str = arguments.get("direction", "both")
        limit: int = int(arguments.get("limit", 50))

        service_direction = _DIRECTION_MCP_TO_SERVICE.get(direction_mcp, "both")
        # The schema enum guards against unknown values, so the cast is
        # safe at runtime. Pyright wants the explicit narrowing.
        direction_literal = cast("Literal['outgoing', 'incoming', 'both']", service_direction)

        service = LinkService(engine=engine)
        links = service.related(
            entity_type,
            entity_id,
            direction=direction_literal,
            limit=limit,
        )
        return _json_dump(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "direction": direction_mcp,
                "items": [_link_to_dict(link) for link in links],
                **_pagination_hint(item_count=len(links), limit=limit),
            }
        )

    return handler


def build_graph_trace_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``graph.trace``."""

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.services.links import LinkService

        entity_id: str = arguments["entity_id"]
        entity_type: str = arguments["entity_type"]
        depth: int = int(arguments.get("depth", 3))

        service = LinkService(engine=engine)
        paths = service.trace(entity_type, entity_id, depth=depth)
        return _json_dump(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "depth": depth,
                "paths": [
                    {
                        "depth": path.depth,
                        "links": [_link_to_dict(link) for link in path.links],
                    }
                    for path in paths
                ],
            }
        )

    return handler


def build_graph_expand_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``graph.expand``."""

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.services.links import LinkService

        entity_id: str = arguments["entity_id"]
        entity_type: str = arguments["entity_type"]
        depth: int = int(arguments.get("depth", 2))

        service = LinkService(engine=engine)
        subset = service.expand(entity_type, entity_id, depth=depth)
        return _json_dump(
            {
                "root": {
                    "entity_type": subset.root[0],
                    "entity_id": subset.root[1],
                },
                "depth": subset.depth,
                "nodes": [
                    {"entity_type": et, "entity_id": eid} for et, eid in sorted(subset.nodes)
                ],
                "edges": [_link_to_dict(link) for link in subset.edges],
                "node_count": len(subset.nodes),
                "edge_count": len(subset.edges),
            }
        )

    return handler


# ---------------------------------------------------------------- source.*


def build_source_list_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``source.list``.

    Queries the ``sources`` projection directly — no CLI counterpart
    exists for this surface, but the projection is the canonical
    answer to "which Slack messages / Box docs has opshub observed?".
    Pagination order matches recall: ``observed_at DESC, id ASC``.
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        from sqlalchemy import select

        from opshub.projections.sources import sources_table

        connector_name: str | None = arguments.get("connector_name")
        source_type: str | None = arguments.get("source_type")
        limit: int = int(arguments.get("limit", 50))

        stmt = select(
            sources_table.c.id,
            sources_table.c.connector_name,
            sources_table.c.source_type,
            sources_table.c.title,
            sources_table.c.url,
            sources_table.c.summary,
            sources_table.c.observed_at,
        )
        if connector_name is not None:
            stmt = stmt.where(sources_table.c.connector_name == connector_name)
        if source_type is not None:
            stmt = stmt.where(sources_table.c.source_type == source_type)
        stmt = stmt.order_by(
            sources_table.c.observed_at.desc(),
            sources_table.c.id.asc(),
        ).limit(limit)

        with engine.connect() as conn:
            rows = conn.execute(stmt).all()

        items = [
            {
                "id": row.id,
                "connector_name": row.connector_name,
                "source_type": row.source_type,
                "title": _truncate(row.title),
                "url": row.url,
                "summary": _truncate(row.summary),
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            }
            for row in rows
        ]
        return _json_dump(
            {
                "connector_filter": connector_name,
                "source_type_filter": source_type,
                "items": items,
                **_pagination_hint(item_count=len(items), limit=limit),
            }
        )

    return handler


def build_source_get_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``source.get``.

    Single-row lookup. Returns ``{"found": false}`` for unknown ids
    rather than raising — agent hosts can branch on the boolean
    without parsing an error message.
    """

    async def handler(arguments: Mapping[str, Any]) -> str:
        from sqlalchemy import select

        from opshub.projections.sources import sources_table

        source_id: str = arguments["source_id"]

        stmt = select(
            sources_table.c.id,
            sources_table.c.connector_name,
            sources_table.c.external_id,
            sources_table.c.source_type,
            sources_table.c.title,
            sources_table.c.url,
            sources_table.c.summary,
            sources_table.c.observed_at,
            sources_table.c.updated_at,
        ).where(sources_table.c.id == source_id)

        with engine.connect() as conn:
            row = conn.execute(stmt).first()

        if row is None:
            return _json_dump({"found": False, "source_id": source_id})

        return _json_dump(
            {
                "found": True,
                "id": row.id,
                "connector_name": row.connector_name,
                "external_id": row.external_id,
                "source_type": row.source_type,
                "title": _truncate(row.title),
                "url": row.url,
                "summary": _truncate(row.summary),
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    return handler


# ------------------------------------------------ embeddings.find_duplicates


def build_embeddings_find_duplicates_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``embeddings.find_duplicates``.

    Wraps :class:`opshub.services.duplicate_service.DuplicateService`.
    Returns the same pairs the CLI ``opshub embeddings find-duplicates``
    surfaces, in the same similarity-descending order.
    """
    _ = engine  # build_duplicate_service owns its own engine resolution

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_duplicate_service

        entity_type: str = arguments.get("entity_type", "source")
        threshold: float = float(arguments.get("threshold", 0.92))
        limit: int = int(arguments.get("limit", 20))

        service = build_duplicate_service()
        pairs = service.find_duplicates(
            entity_type=entity_type,
            threshold=threshold,
            limit=limit,
        )
        return _json_dump(
            {
                "entity_type": entity_type,
                "threshold": threshold,
                "items": [
                    {
                        "entity_type": pair.entity_type,
                        "entity_id_a": pair.entity_id_a,
                        "entity_id_b": pair.entity_id_b,
                        "text_a": _truncate(pair.text_a),
                        "text_b": _truncate(pair.text_b),
                        "similarity": round(float(pair.similarity), 6),
                    }
                    for pair in pairs
                ],
                **_pagination_hint(item_count=len(pairs), limit=limit),
            }
        )

    return handler
