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
    "build_propose_apply_handler",
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

    Resolves the connector from the in-process registry. Population of
    that registry is delegated to
    :func:`opshub.connectors._discovery.import_connector_modules` —
    the SSOT shared with ``opshub <connector> sync`` (CLI per-noun
    drivers) and ``opshub connectors`` (the list command), so the set
    of names this handler accepts always matches the CLI surface. The
    helper is typer-free so this MCP request path stays free of the
    CLI framework (ADR-0022 §(b) for the token-free posture; the
    helper itself documents why the import set lives in
    ``_discovery`` rather than duplicated inline here as it was
    pre-PR-#437).

    Credentials are not threaded through the arguments; the connector
    implementation reads them out of the keyring via
    :mod:`opshub.core.secrets` so the MCP boundary stays token-free.

    The handler reports only ``observed_count`` plus an ``ok`` flag
    on success — it does not echo per-item content into the MCP
    response, keeping context efficient (§(d)) and the data
    exfiltration surface narrow.

    ``engine`` is accepted for symmetry; ``build_source_service`` owns
    its own engine resolution.
    """
    _ = engine

    async def handler(arguments: Mapping[str, Any]) -> str:
        # Mirror the CLI connector set so MCP ``connector.sync``
        # accepts every name the CLI does. Until Phase 17-B / the
        # _discovery extraction this import block was duplicated
        # inline here, and the inline copy missed
        # ``google_calendar`` / ``google_mail`` when Phase 14 landed.
        # ``import_connector_modules`` is the SSOT shared with the
        # ``opshub <connector> sync`` driver
        # (``opshub.cli._connector_common``) and the ``opshub
        # connectors`` list command; it imports nothing typer-related
        # so this request path stays free of the CLI framework.
        from opshub.connectors._discovery import import_connector_modules

        import_connector_modules()

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


_PROPOSE_GENERATE_MODES: frozenset[str] = frozenset(
    {"inbox_triage", "source_extract", "meeting_followup"}
)
"""Phase 12 H4 ``mode`` literals for ``propose.generate``.

ADR-0016 改訂 §決定 (l)(b) limits the ``mode`` argument to persist-bearing
structured-output dispatch keys. The four members (this triple plus the
implicit ``reply_draft`` mode signalled via ``reply_to_source_id``) all
route through :class:`ProposalService` and produce a durable
``ProposalGenerated`` event; ``handoff_draft`` / ``announcement_draft``
are excluded because they return text only (§決定 (l)(b) Negative arm).
"""


def build_propose_generate_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``propose.generate``.

    HITL-boundary write tool (ADR-0022 §(c)). The handler delegates to
    :class:`opshub.services.proposals.ProposalService`:

    * topic-based mode (default): ``ProposalService.generate``
    * reply-draft mode (``reply_to_source_id`` set):
      ``ProposalService.generate_reply_draft``
    * Phase 12 H4 dispatch modes (``mode`` set to one of
      :data:`_PROPOSE_GENERATE_MODES`): ``ProposalService.generate``
      with ``scope=<mode>`` so the persisted proposal records the
      dispatch key (ADR-0016 改訂 §決定 (l)(b)). The host LLM still
      supplies ``topic`` to phrase the inbox / source / meeting context;
      ``mode`` is the schema-level intent marker so a downstream audit
      can join proposals by dispatch.

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
        mode_raw = _nz(arguments.get("mode", ""))
        expand_graph = bool(arguments.get("expand_graph", False))
        max_candidates = int(arguments.get("max_candidates", 5))
        max_tokens = int(arguments.get("max_tokens", 2000))

        if mode_raw is not None and mode_raw not in _PROPOSE_GENERATE_MODES:
            # Schema enum catches this already; defence in depth in case
            # an out-of-band caller bypasses schema validation.
            allowed = ", ".join(sorted(_PROPOSE_GENERATE_MODES))
            raise OpsHubError(
                f"propose.generate ``mode`` must be one of {{{allowed}}}; got {mode_raw!r}"
            )

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
            if mode_raw is not None:
                raise OpsHubError(
                    "reply_to_source_id is mutually exclusive with ``mode``;"
                    " reply-draft mode is signalled by reply_to_source_id alone"
                    " (ADR-0016 §決定 (l)(b))"
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
            # Phase 12 H4: ``mode`` stamps the dispatch key onto the
            # persisted proposal's ``scope`` so an audit / projection
            # query can recover the originating skill. When unset, the
            # service default (``scope="all"``) preserves the Phase 6
            # contract.
            scope = mode_raw if mode_raw is not None else "all"
            proposal = service.generate(
                topic_raw,
                scope=scope,
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


# ------------------------------------------------------------- propose.apply


def _lookup_applied_entity(
    engine: Engine, *, proposal_id: str, candidate_index: int
) -> tuple[str, str] | None:
    """Find the ``(applied_entity_type, applied_entity_id)`` from history.

    Phase 12 H1 idempotency normalisation: when ``ProposalService.apply``
    raises ``OpsHubError("candidate N already applied")``, the handler
    layer needs the original ``(type, id)`` so the response carries the
    same payload as the first call. The ``proposals`` projection only
    stores per-candidate state (``"applied"`` / ``"rejected"`` /
    ``"pending"``) so we walk the event log instead — there is exactly
    one ``ProposalApplied`` event per ``(proposal_id, candidate_index)``
    pair (the service-layer guard prevents duplicates), keeping the
    scan O(1) in the common case.

    Returns ``None`` when no historical apply is found (defensive — the
    caller falls back to surfacing the original error).
    """
    import json as _json

    from sqlalchemy import select

    from opshub.db.schema import events_table

    # Event log discriminator: the Pydantic event class
    # :class:`opshub.domain.events.proposal.ProposalApplied` declares
    # ``event_type: Literal["proposal.applied"]`` (dot-notation, ADR-0002
    # event naming). Phase 12 H6 surfaced that an earlier draft of this
    # lookup filtered on the CamelCase class name and silently missed
    # every historical apply, causing the second ``propose.apply`` to
    # re-raise instead of returning ``already_applied=true``. The string
    # below MUST stay in sync with the event class literal.
    stmt = (
        select(events_table.c.payload)
        .where(
            events_table.c.aggregate_id == proposal_id,
            events_table.c.event_type == "proposal.applied",
        )
        .order_by(events_table.c.recorded_at.asc(), events_table.c.id.asc())
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    for row in rows:
        try:
            payload = _json.loads(str(row.payload))
        except (TypeError, ValueError):
            continue
        if int(payload.get("candidate_index", -1)) != int(candidate_index):
            continue
        entity_type = payload.get("applied_entity_type")
        entity_id = payload.get("applied_entity_id")
        if isinstance(entity_type, str) and isinstance(entity_id, str):
            return entity_type, entity_id
    return None


def build_propose_apply_handler(engine: Engine) -> ToolHandler:
    """Return the handler bound to ``engine`` for ``propose.apply`` (Phase 12 H1).

    HITL-boundary write tool (ADR-0022 改訂 §決定 + ADR-0016 改訂 §決定 (l)).
    Wraps :meth:`opshub.services.proposals.ProposalService.apply` and
    normalises the idempotency case at the MCP boundary:

    * **First call** with ``(proposal_id, candidate_index)``:
      returns ``{"ok": true, "already_applied": false,
      "applied_entity_type": ..., "applied_entity_id": ...,
      "proposal_id": ..., "candidate_index": ...}``.
    * **Second call** with the same arguments: ``ProposalService.apply``
      raises ``OpsHubError("candidate N already applied")``; the
      handler catches that specific error, walks the event log to
      recover the historical ``(applied_entity_type, applied_entity_id)``
      via :func:`_lookup_applied_entity`, and returns
      ``{"ok": true, "already_applied": true, ...}``. This is what
      makes ``annotations.idempotentHint=true`` (set by
      :func:`_policy_for_propose_apply`) honest at the MCP surface.
    * Other ``OpsHubError`` paths (unknown proposal, out-of-range
      index, already rejected) propagate to the server wrapper as
      MCP ``isError`` responses, exactly like the existing write
      tools.

    ``engine`` is accepted both for symmetry with the other write
    handlers AND because ``_lookup_applied_entity`` needs it to scan
    the event log. ``build_proposal_service`` continues to resolve
    its own engine via :func:`opshub.cli._wiring.build_engine` so a
    config / encryption switch takes effect on the next call.
    """

    # The ``OpsHubError`` text emitted by ``_read_candidate_and_states``
    # for the two idempotent paths. Match the literals exactly so an
    # unrelated future ``OpsHubError`` from the service (e.g. proposal
    # not found, index out of range) does not get accidentally
    # normalised. The substring match keeps any future suffix wording
    # (logging hints etc.) compatible.
    already_applied_prefix = "already applied"
    already_rejected_prefix = "already rejected"

    async def handler(arguments: Mapping[str, Any]) -> str:
        from opshub.cli._wiring import build_proposal_service
        from opshub.core.errors import OpsHubError

        proposal_id: str = arguments["proposal_id"]
        candidate_index: int = int(arguments["candidate_index"])

        service = build_proposal_service("mcp:propose.apply")
        try:
            applied_entity_type, applied_entity_id = service.apply(proposal_id, candidate_index)
        except OpsHubError as exc:
            message = str(exc)
            if already_applied_prefix in message:
                # Idempotent path: recover the historical entity tuple
                # from the event log so the second call carries the
                # same payload as the first.
                hit = _lookup_applied_entity(
                    engine,
                    proposal_id=proposal_id,
                    candidate_index=candidate_index,
                )
                if hit is None:
                    # Defensive: the projection says "applied" but no
                    # matching event was found. Re-raise so the host
                    # sees the original failure rather than a partial
                    # ``{"ok": true}`` envelope.
                    raise
                applied_entity_type, applied_entity_id = hit
                return _json_dump(
                    {
                        "ok": True,
                        "already_applied": True,
                        "applied_entity_type": applied_entity_type,
                        "applied_entity_id": applied_entity_id,
                        "proposal_id": proposal_id,
                        "candidate_index": candidate_index,
                    }
                )
            # ``already rejected`` and every other ``OpsHubError`` (not
            # found / index out of range) propagate as MCP ``isError``.
            if already_rejected_prefix in message:
                raise
            raise

        return _json_dump(
            {
                "ok": True,
                "already_applied": False,
                "applied_entity_type": applied_entity_type,
                "applied_entity_id": applied_entity_id,
                "proposal_id": proposal_id,
                "candidate_index": candidate_index,
            }
        )

    return handler
