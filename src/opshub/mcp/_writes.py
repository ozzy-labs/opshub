"""Write-tool handlers for the MCP server (ADR-0022 §(c) write namespace).

Counterpart of :mod:`opshub.mcp._tools` for the write surface
(``task.create`` / ``inbox.add`` / ``connector.sync``). The handlers
in this module:

* never accept SaaS tokens as input — credentials stay inside the
  ① core (ADR-0014 keyring path) and are looked up by the underlying
  service / connector;
* funnel every state change through the existing service layer so
  Phase 1-9 validation, projection writes, and event-log immutability
  apply identically to CLI and MCP invocations;
* surface ``ConnectorSyncFailed`` / ``ValidationError`` etc. as raised
  exceptions — the server wrapper renders them as MCP ``isError``
  results with the message run through
  :func:`opshub.mcp._redact.redact_secrets`.

ADR-0022 §(c) makes the read/write split visible at the MCP boundary
via tool annotations (``readOnlyHint=false`` + ``destructiveHint=true``
for everything here). Host policies that honour the hints will require
human confirmation before invoking these tools; opshub does **not**
double-prompt inside the handlers (the confirmation belongs on the
agent host side per §Negative #2 of ADR-0022).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.mcp._registry import ToolHandler


__all__ = [
    "build_connector_sync_handler",
    "build_inbox_add_handler",
    "build_propose_generate_handler",
    "build_task_create_handler",
]


def _json_dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- task.create


def build_task_create_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``task.create``.

    Mints a new task via :class:`opshub.services.TaskService`, the
    same path as ``opshub task create`` (Phase 1 §14). The actor
    column on the event is set to ``"mcp:task.create"`` so the event
    log records the boundary explicitly.

    ``engine`` is accepted for symmetry with the read-tool builders
    but ``build_task_service`` resolves its own engine via
    :func:`opshub.cli._wiring.build_engine` (so a config / encryption
    change takes effect on the next call without restarting).
    """
    _ = engine

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_task_service

        title: str = arguments["title"]
        body: str | None = arguments.get("body")

        service = build_task_service("mcp:task.create")
        event = service.create_task(title=title, body=body)
        return _json_dump(
            {
                "ok": True,
                "task_id": event.aggregate_id,
                "title": title,
            }
        )

    return handler


# ------------------------------------------------------------------ inbox.add


def build_inbox_add_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``inbox.add``.

    ``engine`` is accepted for symmetry; ``build_inbox_service`` owns
    its own engine resolution (same as :func:`build_task_create_handler`).
    """
    _ = engine

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_inbox_service

        summary: str = arguments["summary"]
        source_ref: str | None = arguments.get("source_ref")

        service = build_inbox_service("mcp:inbox.add")
        event = service.enqueue(summary=summary, source_ref=source_ref)
        return _json_dump(
            {
                "ok": True,
                "item_id": event.aggregate_id,
                "summary": summary,
            }
        )

    return handler


# ------------------------------------------------------------- connector.sync


def build_connector_sync_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``connector.sync``.

    Resolves the connector from the in-process registry — the same
    discovery path as ``opshub connector sync``. Credentials are not
    threaded through the arguments; the connector implementation
    reads them out of the keyring via :mod:`opshub.core.secrets` so
    the MCP boundary stays token-free (ADR-0022 §(b)).

    The handler reports only ``observed_count`` plus an ``ok`` flag
    on success — it does not echo per-item content into the MCP
    response, keeping context efficient (§(d)) and the data
    exfiltration surface narrow.

    ``engine`` is accepted for symmetry; ``build_source_service`` owns
    its own engine resolution.
    """
    _ = engine

    async def handler(arguments: Mapping[str, Any]) -> str:
        # The connector discovery path lives inline in the CLI today
        # (``opshub.cli.connector``). Mirror that import set here so
        # the MCP path covers the same connectors without depending on
        # the CLI module (which would pull typer into the request
        # path).
        try:
            import opshub.connectors.github  # pyright: ignore[reportUnusedImport]
        except ImportError:
            pass
        try:
            import opshub.connectors.slack  # pyright: ignore[reportUnusedImport]
        except ImportError:
            pass
        try:
            import opshub.connectors.ms365  # pyright: ignore[reportUnusedImport]
        except ImportError:
            pass
        try:
            import opshub.connectors.box  # pyright: ignore[reportUnusedImport]
        except ImportError:
            pass
        try:
            import opshub.connectors.box_drive  # noqa: F401  # pyright: ignore[reportUnusedImport]
        except ImportError:
            pass

        from opshub.cli._wiring import build_source_service
        from opshub.connectors import discover_connectors
        from opshub.connectors.context import ConnectorContext
        from opshub.core.errors import OpsHubError
        from opshub.core.logging import get_logger

        name: str = arguments["name"]
        connectors = {c.name: c for c in discover_connectors()}
        connector = connectors.get(name)
        if connector is None:
            available = sorted(connectors)
            raise OpsHubError(f"unknown connector {name!r}; available: {available}")

        source = build_source_service(actor=f"mcp:connector.sync:{name}")
        logger = get_logger().bind(connector=name)
        cursor = source.cursor_get(name)
        source.cursor_set(name, cursor, sync_started=True)
        context = ConnectorContext(
            source_service=source,
            cursor_value=cursor,
            secrets=None,
            logger=logger,
        )
        try:
            result = connector.sync(context)
        except Exception as exc:
            # Match the CLI's sanitise: record only the exception
            # *type* on ConnectorSyncFailed so tokens / PII never
            # land in the event log. Re-raise so the server wrapper
            # renders an MCP ``isError`` response with a redacted
            # message.
            source.record_sync_failure(name, error_message=type(exc).__name__)
            raise

        source.cursor_set(name, result.new_cursor, sync_started=False)
        return _json_dump(
            {
                "ok": True,
                "connector": name,
                "observed_count": result.observed_count,
            }
        )

    return handler


# ----------------------------------------------------------- propose.generate


def build_propose_generate_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``propose.generate``.

    HITL-boundary write tool (ADR-0022 §(c)). The handler delegates to
    :class:`opshub.services.proposals.ProposalService`:

    * topic-based mode (default): ``ProposalService.generate``
    * reply-draft mode (``reply_to_source_id`` set):
      ``ProposalService.generate_reply_draft``

    The schema treats absent text fields as empty strings (``default: ""``)
    so the MCP boundary stays ``additionalProperties: false`` while
    accepting partial input. We coerce empty strings back to ``None``
    inside the handler so the underlying service contract (``str | None``
    for optional fields) is honoured.

    The new :class:`Proposal` row is durable on the event log via
    :class:`ProposalGenerated`, but apply (task / decision creation)
    still requires an operator-driven ``opshub propose apply`` call —
    the handler never invokes :meth:`ProposalService.apply`. Hosts
    that honour the ``destructiveHint=true`` annotation will prompt the
    operator before invoking; opshub does not double-prompt.

    ``engine`` is accepted for symmetry; ``build_proposal_service``
    owns its own engine resolution (same pattern as the other write
    handlers).
    """
    _ = engine

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_proposal_service
        from opshub.core.errors import OpsHubError

        # Schema-default empty strings are normalised back to ``None`` so
        # the service's ``str | None`` contracts are honoured (a literal
        # empty string would survive the schema, but the service uses
        # truthiness for the optional-field guards).
        def _nz(value: object) -> str | None:
            if value is None:
                return None
            text = str(value)
            return text if text else None

        topic_raw = _nz(arguments.get("topic", ""))
        reply_to_source_id = _nz(arguments.get("reply_to_source_id", ""))
        from_briefing_id = _nz(arguments.get("from_briefing_id", ""))
        expand_graph = bool(arguments.get("expand_graph", False))
        max_candidates = int(arguments.get("max_candidates", 5))
        max_tokens = int(arguments.get("max_tokens", 2000))

        service = build_proposal_service("mcp:propose.generate")

        if reply_to_source_id is not None:
            # Reply-draft mode: topic / from_briefing_id are ignored by
            # the service (the source row provides the context), but we
            # surface a clear error when an operator passes both so a
            # misconfigured agent host fails loud instead of silently
            # discarding the topic.
            if topic_raw is not None or from_briefing_id is not None:
                raise OpsHubError(
                    "reply_to_source_id is mutually exclusive with topic /"
                    " from_briefing_id; supply only one mode per call"
                )
            proposal = service.generate_reply_draft(
                reply_to_source_id,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                expand_graph=expand_graph,
            )
        else:
            if topic_raw is None:
                raise OpsHubError(
                    "propose.generate requires either ``topic`` or ``reply_to_source_id``"
                )
            proposal = service.generate(
                topic_raw,
                from_briefing_id=from_briefing_id,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                expand_graph=expand_graph,
            )

        # Render candidates as plain dicts — the typed discriminated
        # union (:class:`Candidate`) is a Pydantic model so
        # ``model_dump`` gives a JSON-safe representation. Each row
        # carries its ``kind`` so the host can branch on rendering.
        candidate_payloads: list[dict[str, object]] = []
        for index, candidate in enumerate(proposal.candidates):
            dumped = candidate.model_dump(mode="json")
            dumped["index"] = index
            candidate_payloads.append(dumped)

        return _json_dump(
            {
                "ok": True,
                "proposal_id": proposal.proposal_id,
                "topic": proposal.topic,
                "scope": proposal.scope,
                "briefing_id": proposal.briefing_id,
                "candidates": candidate_payloads,
                "model_id": proposal.model_id,
                "model_version": proposal.model_version,
                "tokens_in": proposal.tokens_in,
                "tokens_out": proposal.tokens_out,
                "generated_at": proposal.generated_at.isoformat(),
                # Hint to the host: apply is HITL-only. The host should
                # surface an "approve candidate #N" prompt and then call
                # ``opshub propose apply`` via the operator's shell.
                "hitl_apply_required": True,
            }
        )

    return handler
