"""Policy-as-data registry for MCP tools (ADR-0022 §(c)).

ADR-0022 splits opshub's MCP tool surface into a **read** namespace
(safe for agent auto-approve) and a **write** namespace (durable
state change, recommended human-in-the-loop). The split is expressed
once here in plain Python data so the same descriptors drive both:

1. the MCP ``list_tools`` response (the ``annotations`` field carries
   ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint`` /
   ``openWorldHint`` exactly as ADR-0022 §(c) prescribes);
2. the in-process dispatch from :func:`opshub.mcp.server.dispatch_tool_call`.

A registry instead of `@server.tool()` decorators sprinkled across
modules makes the security posture auditable — a CI guard
(:mod:`tests/unit/mcp/test_registry_policy`) can assert that every
write-class tool has ``readOnlyHint=false`` and ``destructiveHint=true``,
and that every read-class tool advertises ``readOnlyHint=true``.

The registry stays runtime-side. The list of tools is intentionally
small (Phase 10 C2 surfaces 4 read tools and 3 write tools). The
``ReadCategory`` / ``WriteCategory`` enums document the intent and
also drive the ``annotations`` defaults so callers cannot accidentally
ship a write tool flagged as ``readOnlyHint=true``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ReadCategory",
    "ToolHandler",
    "ToolPolicy",
    "ToolSpec",
    "WriteCategory",
    "build_tool_specs",
]


class ReadCategory(StrEnum):
    """Read-only tool category.

    All members map to ``annotations.readOnlyHint = true`` and
    ``annotations.destructiveHint = false`` (ADR-0022 §(c)). They are
    safe for an agent host to invoke without human confirmation.
    """

    RECALL = "recall"
    TASK = "task"
    INBOX = "inbox"
    DECISION = "decision"


class WriteCategory(StrEnum):
    """Write tool category.

    All members map to ``annotations.readOnlyHint = false`` and
    ``annotations.destructiveHint = true`` (ADR-0022 §(c)). Agent
    hosts honouring the policy will require human confirmation before
    invoking these.

    ``connector.sync`` is classified write even though it does not
    delete data, because the SaaS round-trip is observable upstream
    (rate limit, audit log) and triggering it without operator intent
    is undesirable. ``open_world`` therefore stays ``true`` for sync
    to flag the external interaction.
    """

    TASK_CREATE = "task.create"
    INBOX_ADD = "inbox.add"
    CONNECTOR_SYNC = "connector.sync"


# Either a read or a write category.
ToolCategory = ReadCategory | WriteCategory

# Each handler receives the parsed argument mapping (already validated
# by the MCP request layer against ``input_schema``) and returns a
# plain string. We deliberately keep the return type to ``str`` so the
# server module can wrap it in a single ``TextContent`` block and
# apply the secret redaction in one place — handlers stay focused on
# the read/write semantics.
ToolHandler = Callable[[Mapping[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Frozen policy descriptor.

    Maps directly to ``mcp.types.ToolAnnotations`` at the MCP boundary.
    The defaults pin the security-relevant fields so every spec must
    state them explicitly — preventing an accidental
    ``destructiveHint=false`` on a deletion-class tool.
    """

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Full descriptor for one MCP tool.

    The spec carries everything ``list_tools`` and ``call_tool`` need
    in one immutable record so neither side reaches into a mutable
    registry at request time.
    """

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    policy: ToolPolicy
    category: ToolCategory
    handler: ToolHandler
    output_schema: Mapping[str, Any] | None = field(default=None)


def _policy_for_read() -> ToolPolicy:
    return ToolPolicy(
        read_only=True,
        destructive=False,
        # Reads can repeat safely.
        idempotent=True,
        # Read tools query the local SQLite only — no external API.
        # ``recall`` may call an embedder (potentially OpenAI / Voyage)
        # but that is observation only and is tracked via the LLM
        # backend choice; from the agent's perspective the recall
        # answer is closed-world (it returns ids already in the DB).
        open_world=False,
    )


def _policy_for_write(*, open_world: bool, destructive: bool = True) -> ToolPolicy:
    return ToolPolicy(
        read_only=False,
        destructive=destructive,
        # Writes can be invoked twice with the same arguments but
        # most of them are not idempotent in the strict sense
        # (``task.create`` mints a new ULID each call). We report
        # ``False`` so an agent host that respects the hint does not
        # auto-retry.
        idempotent=False,
        open_world=open_world,
    )


def build_tool_specs(
    *,
    handlers: Mapping[str, ToolHandler],
) -> list[ToolSpec]:
    """Materialise the full MCP tool registry.

    ``handlers`` carries the concrete async callables — the registry
    only owns the *policy* and the *schema*. Passing handlers from
    outside means the C2 surface stays inspectable in isolation
    (tests register stub handlers; the server wires real ones) and
    keeps ``opshub.mcp._registry`` decoupled from any service / DB
    code (ADR-0001 cold-start).

    The list ordering matches the categorical priority in ADR-0022
    §(c): read tools first, write tools second, so an
    ``list_tools`` response surfaces the safe surface ahead of the
    confirmation-required ones.
    """
    specs: list[ToolSpec] = [
        ToolSpec(
            name="recall.search",
            title="Hybrid semantic recall",
            description=(
                "Search opshub's tasks / decisions / inbox / sources by query text. "
                "Returns top hits with summaries — never full body text. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-form query text (1..500 chars).",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": ["task", "decision", "inbox_item", "source"],
                        "description": "Restrict hits to a single entity type.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.RECALL,
            handler=handlers["recall.search"],
        ),
        ToolSpec(
            name="task.list",
            title="List tasks",
            description=(
                "List rows from the ``tasks`` projection, optionally filtered by state. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["draft", "active", "completed"],
                        "description": "Optional state filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.TASK,
            handler=handlers["task.list"],
        ),
        ToolSpec(
            name="inbox.list",
            title="List inbox items",
            description=(
                "List rows from the ``inbox_items`` projection, optionally filtered by "
                "state. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "triaged_to_task",
                            "triaged_to_decision",
                            "discarded",
                        ],
                        "description": "Optional state filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.INBOX,
            handler=handlers["inbox.list"],
        ),
        ToolSpec(
            name="decision.list",
            title="List decisions",
            description=("List rows from the ``decisions`` projection. Read-only."),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.DECISION,
            handler=handlers["decision.list"],
        ),
        # ---- write surface (default human-in-the-loop, ADR-0022 §(c)) ----
        ToolSpec(
            name="task.create",
            title="Create task",
            description=(
                "Create a new task. Writes a TaskCreated event to the durable log; "
                "host should confirm with the operator before invoking."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "body": {
                        "type": "string",
                        "maxLength": 5000,
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            policy=_policy_for_write(open_world=False),
            category=WriteCategory.TASK_CREATE,
            handler=handlers["task.create"],
        ),
        ToolSpec(
            name="inbox.add",
            title="Add inbox item",
            description=(
                "Add an item to the inbox. Writes an ItemEnqueued event; host should "
                "confirm with the operator before invoking."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "source_ref": {
                        "type": "string",
                        "maxLength": 500,
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            policy=_policy_for_write(open_world=False),
            category=WriteCategory.INBOX_ADD,
            handler=handlers["inbox.add"],
        ),
        ToolSpec(
            name="connector.sync",
            title="Trigger connector sync",
            description=(
                "Run the named connector's sync against the SaaS. Hits the external "
                "API (rate limit / audit log applies); host should confirm with the "
                "operator before invoking. Credentials are resolved from the keyring "
                "inside opshub and never accepted as tool arguments."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Connector name (e.g. 'github', 'slack').",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            # External API → open world.
            policy=_policy_for_write(open_world=True),
            category=WriteCategory.CONNECTOR_SYNC,
            handler=handlers["connector.sync"],
        ),
    ]
    return specs
