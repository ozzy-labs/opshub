"""stdio MCP server for opshub (ADR-0022).

Wires the policy-as-data registry in :mod:`opshub.mcp._registry` to the
Python MCP SDK's low-level :class:`mcp.server.lowlevel.Server`. The
module is imported lazily by ``opshub mcp serve`` and never at module
top inside the CLI subcommand, so a ``opshub --help`` cold start that
does not invoke the MCP server pays nothing for the SDK import.

Invariants enforced here (ADR-0022):

1. **stdio one transport** — the only entry point exposed is
   :func:`serve_stdio`, which runs the server over
   :func:`mcp.server.stdio.stdio_server`. There is no ``serve_http``
   sibling, and the module avoids importing ``mcp.server.streamable_http``
   / ``mcp.server.sse``. ``tests/unit/mcp/test_no_network_listen``
   guards against a regressing PR that imports those siblings.
2. **No token passthrough** — :class:`opshub.mcp._registry.ToolSpec`
   defines input schemas that never carry token / secret fields, and
   every tool response runs through
   :func:`opshub.mcp._redact.redact_secrets` before reaching the
   client. The redactor catches stray secrets in exception messages
   that get wrapped into ``isError`` responses.
3. **Read / write split** — the ``annotations`` field on every
   advertised tool reflects the ``ToolPolicy`` from the registry.
   Hosts that honour MCP hints will auto-approve reads and prompt
   for writes. ``tests/unit/mcp/test_registry_policy`` asserts the
   invariants on the data side.
4. **Context efficient returns** — handlers in :mod:`opshub.mcp._tools`
   truncate snippets and never echo full body text without an explicit
   caller request (none of the C2 tools expose that flag).
5. **OTel GenAI naming** — every ``call_tool`` invocation runs through
   :func:`opshub.mcp._logging.log_tool_call_start` /
   :func:`opshub.mcp._logging.log_tool_call_complete` to record the
   start / end span attributes (``gen_ai.operation.name = execute_tool``
   etc.). The records flow to ``structlog`` today; the future
   ``mcp-otel`` extras will wire them to an OTel exporter without
   touching the call sites.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.mcp._registry import ToolHandler, ToolSpec


__all__ = ["build_tool_specs_for_engine", "dispatch_tool_call", "serve_stdio"]


def _stderr_is_tty() -> bool:
    """Return True when stderr is attached to an interactive terminal.

    Factored out so tests can monkey-patch the probe without reaching
    into :mod:`sys`. The MCP server is normally spawned as a
    subprocess by the agent host (stderr is piped, not a TTY), but
    operators may also launch it manually for diagnostics — the probe
    keeps the JSON-vs-console renderer choice automatic.
    """
    isatty = getattr(sys.stderr, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def build_tool_specs_for_engine(engine: Engine) -> list[ToolSpec]:
    """Wire the registry against a live :class:`Engine`.

    Kept separate from :func:`serve_stdio` so unit tests can build the
    spec list against an in-memory engine without spawning the actual
    stdio server.
    """
    from opshub.mcp._registry import build_tool_specs
    from opshub.mcp._tools import (
        build_brief_handler,
        build_decision_list_handler,
        build_embeddings_find_duplicates_handler,
        build_graph_expand_handler,
        build_graph_related_handler,
        build_graph_trace_handler,
        build_inbox_list_handler,
        build_recall_search_handler,
        build_search_handler,
        build_source_get_handler,
        build_source_list_handler,
        build_task_list_handler,
    )
    from opshub.mcp._writes import (
        build_connector_sync_handler,
        build_inbox_add_handler,
        build_propose_apply_handler,
        build_propose_generate_handler,
        build_task_create_handler,
    )

    handlers: dict[str, ToolHandler] = {
        "recall.search": build_recall_search_handler(engine),
        "task.list": build_task_list_handler(engine),
        "inbox.list": build_inbox_list_handler(engine),
        "decision.list": build_decision_list_handler(engine),
        "task.create": build_task_create_handler(engine),
        "inbox.add": build_inbox_add_handler(engine),
        "connector.sync": build_connector_sync_handler(engine),
        # Step 1 widening (additional read tools + HITL propose).
        "brief": build_brief_handler(engine),
        "graph.related": build_graph_related_handler(engine),
        "graph.trace": build_graph_trace_handler(engine),
        "graph.expand": build_graph_expand_handler(engine),
        "source.list": build_source_list_handler(engine),
        "source.get": build_source_get_handler(engine),
        "embeddings.find_duplicates": build_embeddings_find_duplicates_handler(engine),
        "propose.generate": build_propose_generate_handler(engine),
        # Phase 12 H1 (ADR-0022 改訂): FTS5 search + HITL propose.apply.
        "search": build_search_handler(engine),
        "propose.apply": build_propose_apply_handler(engine),
    }
    return build_tool_specs(handlers=handlers)


def _to_mcp_tool(spec: ToolSpec) -> object:
    """Convert one ``ToolSpec`` into an ``mcp.types.Tool``.

    Materialises the policy as MCP ``annotations`` so a compliant
    client can decide auto-approve vs human-in-the-loop without
    inspecting the tool name.
    """
    from mcp import types

    annotations = types.ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.policy.read_only,
        destructiveHint=spec.policy.destructive,
        idempotentHint=spec.policy.idempotent,
        openWorldHint=spec.policy.open_world,
    )
    return types.Tool(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        inputSchema=dict(spec.input_schema),
        annotations=annotations,
    )


def _to_text_content(text: str) -> list[Any]:
    """Wrap a string in a single ``TextContent`` block."""
    from mcp import types

    return [types.TextContent(type="text", text=text)]


async def dispatch_tool_call(
    specs_by_name: Mapping[str, ToolSpec],
    name: str,
    arguments: Mapping[str, Any] | None,
) -> list[Any]:
    """Run the handler for ``name`` and return the wrapped MCP content.

    Wraps the call with OTel GenAI start / complete records and the
    secret redactor (ADR-0022 §(b) / §(e)).

    Exceptions are re-raised so the MCP server low-level handler can
    convert them to ``CallToolResult(isError=true)``. Before re-raising
    we rewrap the exception message through
    :func:`opshub.mcp._redact.redact_secrets`: the SDK serialises the
    exception's ``str()`` into the ``isError`` payload, so a token that
    slipped into ``ConnectorSyncFailed("Bearer xoxb-…")`` would otherwise
    cross the MCP boundary verbatim. The completion record is still
    emitted via ``try / finally``.
    """
    from opshub.core.errors import OpsHubError
    from opshub.core.logging import get_logger
    from opshub.mcp._logging import (
        log_tool_call_complete,
        log_tool_call_start,
        new_call_id,
    )
    from opshub.mcp._redact import redact_secrets

    spec = specs_by_name.get(name)
    if spec is None:
        # Surface as a clean opshub error so the MCP server wraps it
        # in a properly-typed ``isError`` payload.
        raise OpsHubError(f"unknown MCP tool: {name!r}")

    logger = get_logger().bind(component="mcp")
    call_id = new_call_id()
    log_tool_call_start(logger, tool_name=name, call_id=call_id)
    start = time.perf_counter()
    status = "error"
    error_type: str | None = None
    try:
        result_text = await spec.handler(arguments or {})
        status = "ok"
        return _to_text_content(redact_secrets(result_text))
    except Exception as exc:
        error_type = type(exc).__name__
        # Re-raise as ``OpsHubError`` with the message scrubbed so the
        # SDK's ``isError`` payload never carries a raw token. The
        # original exception is attached via ``raise ... from exc`` so
        # the traceback (server-side only) keeps full context.
        redacted_message = redact_secrets(str(exc))
        raise OpsHubError(redacted_message) from exc
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        log_tool_call_complete(
            logger,
            tool_name=name,
            call_id=call_id,
            duration_ms=round(duration_ms, 3),
            status=status,
            error_type=error_type,
        )


async def serve_stdio(*, server_name: str = "opshub", server_version: str = "0.0.0") -> None:
    """Run the MCP server over stdio until the client disconnects.

    Builds a fresh :class:`Engine` for this server lifetime and wires
    the read / write handlers against it. Returns when the agent host
    closes the stdio pair (e.g. the parent process exits).

    Logging bootstrap (Phase 14 T3, #320 / ADR-0027). The MCP server
    is spawned as a subprocess by the agent host, so the CLI flags
    that :mod:`opshub.cli.app` parses do not reach this entry point.
    Instead we resolve ``OPSHUB_LOG_LEVEL`` / ``OPSHUB_LOG_FORMAT`` /
    ``OPSHUB_DEBUG`` / ``OPSHUB_LOG_FILE`` via
    :func:`opshub.core.logging.resolve_log_settings` and feed the
    result into :func:`opshub.core.logging.configure_logging`.
    ``configure_logging`` is idempotent (first-call-wins), so the
    inevitable ``get_logger`` call inside ``dispatch_tool_call`` will
    see the env-driven settings rather than the bare defaults. The
    redaction processor wired by T1 is already on the structlog
    pipeline, so :mod:`opshub.mcp._logging` events are scrubbed
    without any change at the call site.
    """
    import mcp.server.stdio
    from mcp import types
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.models import InitializationOptions

    from opshub.cli._wiring import build_engine
    from opshub.core.logging import configure_logging, resolve_log_settings

    # Resolve env-driven log settings before any ``get_logger`` call
    # in the dispatch loop. ``log_format`` of ``auto`` folds back to
    # the stderr-isatty probe (MCP servers are normally non-TTY, so
    # the JSON renderer is the natural choice — JSON output is also
    # easier for the agent host to capture alongside the MCP stream).
    # An explicit ``OPSHUB_LOG_FORMAT=console`` overrides the auto
    # path for operators running ``opshub mcp serve`` interactively.
    log_settings = resolve_log_settings()
    use_json = log_settings.log_format == "json" or (
        log_settings.log_format == "auto" and not _stderr_is_tty()
    )
    configure_logging(
        level=log_settings.level,
        json=use_json,
        log_file=log_settings.log_file,
    )

    engine = build_engine()
    specs = build_tool_specs_for_engine(engine)
    specs_by_name = {s.name: s for s in specs}

    server: Server = Server(server_name)

    # mcp SDK 1.27 ships untyped low-level ``Server`` decorators
    # (`list_tools()` / `call_tool()`). The ``unused-ignore`` rider
    # keeps mypy --strict quiet on Python versions / SDK builds where
    # the upstream type info has already started to land, so the
    # ignore self-deletes once mcp adds full stubs upstream
    # (Round 2 Cluster B L1).
    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator,unused-ignore]
    async def _list_tools() -> list[Any]:  # pyright: ignore[reportUnusedFunction]
        return [_to_mcp_tool(spec) for spec in specs]

    @server.call_tool()  # type: ignore[untyped-decorator,unused-ignore]
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        return await dispatch_tool_call(specs_by_name, name, arguments)

    # Silence ``types`` import warning for clients that need it at type
    # level (kept for forward-compat with future structured outputs).
    _ = types

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server_name,
                server_version=server_version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
