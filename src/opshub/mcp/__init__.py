"""Phase 10 MCP server surface (ADR-0022).

Exposes the ① core (events / projections / services) to external agent
hosts (Claude Code 等) over the Model Context Protocol.

Invariants (ADR-0022):

* **stdio one transport** — no HTTP / SSE / WebSocket listener. The
  module deliberately imports nothing that would open a network socket;
  attempts to add one are caught by the static check in
  ``tests/unit/mcp/test_no_network_listen.py``.
* **No token passthrough** — SaaS tokens stay in the keyring
  (ADR-0014). Tool input schemas never expose a token field, and tool
  outputs run through :func:`opshub.mcp._redact.redact_secrets` before
  reaching the agent.
* **Read / write split (policy-as-data)** — every tool is registered
  via :func:`opshub.mcp._registry.register_tool` with an explicit
  ``ToolPolicy`` declaring ``read_only`` / ``destructive`` /
  ``idempotent`` hints. The hints surface as MCP ``annotations`` and
  are honoured by host-side default policies (read = auto-approve OK,
  write = human-in-the-loop).

The package keeps all heavy imports (``opshub.core``, ``opshub.db``,
``opshub.services``, ``opshub.projections``, ``mcp``) deferred so a
``opshub --help`` invocation that never spawns ``opshub mcp serve``
pays nothing.
"""

from __future__ import annotations

__all__: list[str] = []
