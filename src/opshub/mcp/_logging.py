"""OpenTelemetry GenAI naming for MCP boundary traces (ADR-0022 §(e)).

ADR-0022 records MCP ``CallTool`` round-trips against the OTel GenAI
Semantic Conventions naming, but does **not** install the OTel SDK at
this stage — fully instrumenting MCP via OTel exporters is reserved for
the future ``mcp-otel`` extras. The Phase 10 C2 baseline therefore
writes structured events through ``structlog`` (the logger
:mod:`opshub.core.logging` configures) using the OTel-compatible
attribute names so a later exporter can ingest the same records without
the call-site changing.

Attributes emitted (subset of OTel GenAI):

* ``event``          — ``"mcp.execute_tool"`` (matches OTel span name).
* ``gen_ai.operation.name`` — ``"execute_tool"``.
* ``gen_ai.tool.name``     — fully qualified MCP tool name.
* ``gen_ai.tool.call.id``  — opaque per-call ULID. Lets a future
  exporter correlate the start / end records.
* ``duration_ms``    — wall clock in milliseconds.
* ``status``         — ``"ok"`` / ``"error"`` (only on completion).
* ``error.type``     — exception class name on error (no message;
  message bodies stay on the redacted MCP response only).

The module does **not** record tool *arguments*. Arguments may contain
sensitive recall queries / source IDs that the operator never
volunteered to log; only an opaque call id leaves the host. This is
also why we never put the tool *output* in the log — full bodies belong
in the MCP response, not in OTel breadcrumbs.
"""

from __future__ import annotations

from typing import Any

__all__ = ["log_tool_call_complete", "log_tool_call_start", "new_call_id"]

# structlog's ``FilteringBoundLogger`` is a Protocol whose ``info``
# method takes ``**Any`` kwargs; pyright in strict mode can't bind
# those through, so we type the logger as ``Any`` at the boundary.
# The runtime type still flows in from
# :func:`opshub.core.logging.get_logger`, and the only operations we
# perform on it (``info(event, **kwargs)``) are part of the
# documented structlog API.
_Logger = Any


def new_call_id() -> str:
    """Mint a fresh ULID for a ``gen_ai.tool.call.id`` attribute.

    ULID is already the project-wide id format (``opshub.core.ids``)
    so the value sorts chronologically and stays consistent with the
    event log.
    """
    from opshub.core.ids import new_ulid

    return new_ulid()


def log_tool_call_start(logger: _Logger, *, tool_name: str, call_id: str) -> None:
    """Emit the start record for an MCP ``execute_tool`` span.

    Event name vs OTel span name (intentional split): the structlog
    event name is ``mcp.execute_tool`` (opshub-namespaced so log
    aggregators can route it without colliding with other ``execute_tool``
    producers), while the OTel GenAI span name is ``execute_tool``
    (carried as ``gen_ai.operation.name`` in the structured payload so a
    future ``mcp-otel`` exporter can promote the attribute to the OTel
    span name without renaming the structlog event).
    """
    logger.info(
        "mcp.execute_tool",
        **{
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_name,
            "gen_ai.tool.call.id": call_id,
            "phase": "start",
        },
    )


def log_tool_call_complete(
    logger: _Logger,
    *,
    tool_name: str,
    call_id: str,
    duration_ms: float,
    status: str,
    error_type: str | None = None,
) -> None:
    """Emit the completion record for an MCP ``execute_tool`` span.

    ``status`` is ``"ok"`` on success and ``"error"`` on a raised
    exception. ``error_type`` carries the exception class name only —
    never the message body, which may contain redactable secrets that
    are scrubbed in the MCP response path instead (ADR-0022 §(b)).
    """
    payload: dict[str, object] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.call.id": call_id,
        "duration_ms": duration_ms,
        "status": status,
        "phase": "complete",
    }
    if error_type is not None:
        payload["error.type"] = error_type
    logger.info("mcp.execute_tool", **payload)
