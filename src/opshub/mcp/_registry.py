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

    Step 1 tool-surface widening (post Phase 10) extends the read
    surface with briefing / graph traversal / source / duplicate
    categories so the Tier 1 skill set (``personal-brief`` /
    ``next-actions`` / ``pr-review`` / ``find-document`` — the
    Phase 12 H1 rename targets, see ``docs/phase-12-plan.md`` §3 H1-c)
    can call MCP tools directly instead of falling back to the CLI shell.
    """

    RECALL = "recall"
    TASK = "task"
    INBOX = "inbox"
    DECISION = "decision"
    BRIEF = "brief"
    GRAPH_RELATED = "graph.related"
    GRAPH_TRACE = "graph.trace"
    GRAPH_EXPAND = "graph.expand"
    SOURCE_LIST = "source.list"
    SOURCE_GET = "source.get"
    EMBEDDINGS_FIND_DUPLICATES = "embeddings.find_duplicates"
    # Phase 12 H1 (ADR-0022 改訂): FTS5 search MCP surface so the Tier
    # 1 ``find-document`` skill can ask for body-level hits without
    # falling back to the ``opshub search`` CLI. The ``raw_query``
    # CLI flag is intentionally NOT exposed at the MCP boundary
    # (phrase quoting stays default — hosts may supply free-form
    # token streams without tripping FTS5 syntax).
    SEARCH = "search"


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

    ``propose.generate`` is the HITL-boundary write (Step 1 widening):
    it mints a ``ProposalGenerated`` event durable on the log, but the
    apply path that creates a task / decision still requires an
    explicit operator-driven ``opshub propose apply`` call (ADR-0016
    §決定 (c)). The classification keeps the MCP boundary honest —
    proposal generation IS a state change (cost, audit) and must
    surface with ``destructiveHint=true`` so a compliant host prompts
    before invoking. ``open_world`` is ``true`` because the LLM round
    trip leaves the local box (model provider audit).
    """

    TASK_CREATE = "task.create"
    INBOX_ADD = "inbox.add"
    CONNECTOR_SYNC = "connector.sync"
    PROPOSE_GENERATE = "propose.generate"
    # Phase 12 H1 (ADR-0022 改訂): HITL apply path closes the
    # ``propose.generate`` → ``propose.apply`` round-trip at the MCP
    # boundary. Unlike the other write categories this one advertises
    # ``destructive=false`` + ``idempotent=true`` — the handler-layer
    # idempotency normalisation (see ``_writes.build_propose_apply_handler``)
    # catches ``OpsHubError("already applied/rejected")`` and returns
    # ``{ok:true, already_applied:true, applied_entity_type,
    # applied_entity_id}`` so a second call with the same
    # ``(proposal_id, candidate_index)`` is observably a no-op. The
    # underlying state change (TaskCreated / DecisionRecorded /
    # ReplyDraftApplied + ProposalApplied) only happens on the first
    # call, so the strict "destroys data" semantics do not apply.
    PROPOSE_APPLY = "propose.apply"


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


def _policy_for_propose_apply() -> ToolPolicy:
    """Policy for the Phase 12 H1 ``propose.apply`` tool (ADR-0022 改訂).

    ``propose.apply`` is the only write-class tool with
    ``destructive=false`` + ``idempotent=true``: the handler layer
    catches ``OpsHubError("already applied/rejected")`` from
    :class:`ProposalService` and returns a normalised
    ``{ok:true, already_applied:true, ...}`` envelope so the second
    call is observably a no-op. The first call still writes
    durable events (TaskCreated / DecisionRecorded / ReplyDraftApplied
    + ProposalApplied) which is why ``read_only`` stays ``False``;
    hosts honouring ``readOnlyHint=false`` should still surface a
    confirmation prompt on first invocation even though the
    annotation makes auto-retry safe.

    ``open_world`` is ``False`` — apply is an in-process projection
    update; the LLM round-trip already happened during
    ``propose.generate``.
    """
    return ToolPolicy(
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
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
                "List rows from the ``tasks`` projection, optionally filtered by state"
                " and updated_at. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["draft", "active", "completed"],
                        "description": "Optional state filter.",
                    },
                    # Phase 12 H1 (ADR-0022 改訂): physical-column time
                    # filter on ``tasks.updated_at`` (the projection
                    # has no ``completed_at`` column — operators that
                    # want "completed this week" combine ``state=completed``
                    # with ``updated_after``). Half-open interval:
                    # ``>= updated_after`` and ``< updated_before``.
                    "updated_after": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with tasks.updated_at >= this ISO 8601"
                            " timestamp (half-open lower bound)."
                        ),
                    },
                    "updated_before": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with tasks.updated_at < this ISO 8601"
                            " timestamp (half-open upper bound)."
                        ),
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
                "List rows from the ``inbox_items`` projection, optionally filtered by"
                " state and created_at. Read-only."
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
                    # Phase 12 H1 (ADR-0022 改訂): physical-column time
                    # filter on ``inbox_items.created_at``. Half-open
                    # interval; see ``task.list`` for the rationale.
                    "created_after": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with inbox_items.created_at >= this ISO"
                            " 8601 timestamp (half-open lower bound)."
                        ),
                    },
                    "created_before": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with inbox_items.created_at < this ISO"
                            " 8601 timestamp (half-open upper bound)."
                        ),
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
            description=(
                "List rows from the ``decisions`` projection, optionally filtered by"
                " recorded_at. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    # Phase 12 H1 (ADR-0022 改訂): physical-column time
                    # filter on ``decisions.recorded_at``. Decisions
                    # are immutable (ADR-0002) so ``recorded_at`` is
                    # the only natural time anchor.
                    "recorded_after": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with decisions.recorded_at >= this ISO"
                            " 8601 timestamp (half-open lower bound)."
                        ),
                    },
                    "recorded_before": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with decisions.recorded_at < this ISO"
                            " 8601 timestamp (half-open upper bound)."
                        ),
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
        # ------------------------------------------------------------------
        # Step 1 widening — additional read tools (briefing + graph + source
        # + duplicates) so the Tier 1 skill surfaces can call MCP directly.
        # All entries reuse :func:`_policy_for_read` so the read-only invariants
        # in ``tests/unit/mcp/test_registry_policy`` apply to the new surface
        # uniformly.
        # ------------------------------------------------------------------
        ToolSpec(
            name="brief",
            title="Generate operational briefing",
            description=(
                "Generate a Markdown (or JSON) briefing for ``topic`` by recalling "
                "related entities and asking the configured LLM backend to summarise. "
                "Hits the LLM provider (open world / cost). Returns the briefing body "
                "plus source references."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Free-form topic to brief on.",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "expand_graph": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, walk 1-hop graph neighbours of each recall hit"
                            " to widen the prompt context (ADR-0017 §(e)+(f))."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["md", "json"],
                        "default": "md",
                        "description": (
                            "Response shape. ``md`` returns the briefing markdown verbatim;"
                            " ``json`` returns the full Briefing record (markdown + source_refs"
                            " + cost)."
                        ),
                    },
                    "max_sources": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                    },
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 4000,
                        "default": 1500,
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
            # Brief calls the LLM provider; classified read because no
            # durable entity is created beyond the BriefingGenerated event
            # (mirrors the recall.search treatment of the embedder backend
            # — observation only). open_world stays false because from the
            # agent's perspective the result is a summary over local
            # entities. Hosts should still treat LLM cost as caller-pays.
            policy=_policy_for_read(),
            category=ReadCategory.BRIEF,
            handler=handlers["brief"],
        ),
        ToolSpec(
            name="graph.related",
            title="List 1-hop graph neighbours",
            description=(
                "Return links connected to an entity (1-hop). Direction filter "
                "controls inbound / outbound / both. Read-only over the ``links`` projection."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Entity ULID.",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "Entity type label (e.g. 'task', 'decision', 'source').",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["both", "outbound", "inbound"],
                        "default": "both",
                        "description": (
                            "Edge direction filter. 'outbound' = links FROM entity;"
                            " 'inbound' = links TO entity; 'both' = union."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "required": ["entity_id", "entity_type"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.GRAPH_RELATED,
            handler=handlers["graph.related"],
        ),
        ToolSpec(
            name="graph.trace",
            title="Trace entity provenance backwards",
            description=(
                "Return backward-chain link paths up to ``depth`` hops (default 3,"
                " hard ceiling 10 per ADR-0017 §(e)). Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "entity_type": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 3,
                    },
                },
                "required": ["entity_id", "entity_type"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.GRAPH_TRACE,
            handler=handlers["graph.trace"],
        ),
        ToolSpec(
            name="graph.expand",
            title="Expand bidirectional graph neighbourhood",
            description=(
                "Return the bidirectional N-hop subgraph rooted at the entity"
                " (default depth 2, hard ceiling 5 per ADR-0017 §(e)). Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "entity_type": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 2,
                    },
                },
                "required": ["entity_id", "entity_type"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.GRAPH_EXPAND,
            handler=handlers["graph.expand"],
        ),
        ToolSpec(
            name="source.list",
            title="List source rows",
            description=(
                "List rows from the ``sources`` projection, optionally filtered by"
                " connector_name, source_type, or observed_at. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "connector_name": {
                        "type": "string",
                        "description": "Restrict to one connector (e.g. 'github').",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "source_type": {
                        "type": "string",
                        "description": "Restrict to one source_type label.",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    # Phase 12 H1 (ADR-0022 改訂): physical-column time
                    # filter on ``sources.observed_at``. The skill set
                    # ``meeting-followup`` walks "calendar events from
                    # the last 24h" via this pair.
                    "observed_after": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with sources.observed_at >= this ISO"
                            " 8601 timestamp (half-open lower bound)."
                        ),
                    },
                    "observed_before": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Restrict to rows with sources.observed_at < this ISO"
                            " 8601 timestamp (half-open upper bound)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.SOURCE_LIST,
            handler=handlers["source.list"],
        ),
        ToolSpec(
            name="source.get",
            title="Get one source row",
            description=(
                "Fetch a single ``sources`` projection row by ULID. Returns the"
                " truncated summary / title / url etc. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                },
                "required": ["source_id"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.SOURCE_GET,
            handler=handlers["source.get"],
        ),
        ToolSpec(
            name="embeddings.find_duplicates",
            title="Find near-duplicate entities",
            description=(
                "Scan the active embedder backend's vector store for entity pairs"
                " whose cosine similarity exceeds ``threshold``. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["task", "decision", "inbox_item", "source"],
                        "default": "source",
                        "description": "Entity family to scan (default 'source').",
                    },
                    "threshold": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.92,
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
            category=ReadCategory.EMBEDDINGS_FIND_DUPLICATES,
            handler=handlers["embeddings.find_duplicates"],
        ),
        # ------------------------------------------------------------------
        # Phase 12 H1 (ADR-0022 改訂) — FTS5 ``search`` MCP surface so the
        # Tier 1 ``find-document`` skill can request body-level hits
        # directly. The CLI ``opshub search --raw-query`` flag is
        # intentionally NOT mirrored at the MCP boundary: phrase
        # quoting stays the default so hosts can pass free-form
        # token streams without tripping FTS5 syntax characters.
        # ------------------------------------------------------------------
        ToolSpec(
            name="search",
            title="Body-level FTS5 search",
            description=(
                "Run an SQLite FTS5 MATCH against ``sources_fts`` and join hits back"
                " to ``sources``. Phrase-quoted by default (free-form token streams"
                " are safe to pass); the CLI ``--raw-query`` flag is NOT exposed at"
                " the MCP boundary. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Free-form query text. Phrase-quoted before reaching"
                            " FTS5 so syntax characters do not need to be escaped."
                        ),
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "connector_name": {
                        "type": "string",
                        "description": "Restrict to one connector (e.g. 'box_drive').",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            policy=_policy_for_read(),
            category=ReadCategory.SEARCH,
            handler=handlers["search"],
        ),
        # ------------------------------------------------------------------
        # Step 1 widening — HITL-boundary write tool (proposal generation).
        # ProposalGenerated lands on the durable event log; apply still
        # requires operator-driven ``opshub propose apply`` (ADR-0016 §(c)).
        # ------------------------------------------------------------------
        ToolSpec(
            name="propose.generate",
            title="Generate proposal candidates (HITL)",
            description=(
                "Generate next-action proposal candidates for ``topic`` (or for a"
                " specific inbox source via ``reply_to_source_id``, or seeded by a"
                " prior briefing via ``from_briefing_id``). Calls the LLM and writes"
                " a ProposalGenerated event; the apply step (task / decision creation)"
                " still requires an operator-driven ``opshub propose apply`` call —"
                " no auto-apply path exists (ADR-0016 §(c) HITL contract)."
                " Phase 12 H4 dispatch modes (``mode`` = ``inbox_triage`` /"
                " ``source_extract`` / ``meeting_followup``) stamp the originating"
                " skill onto ``proposals.scope`` (ADR-0016 §決定 (l)(b))."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Free-form proposal subject. Empty when ``reply_to_source_id``"
                            " is set (reply-draft mode)."
                        ),
                        "maxLength": 500,
                    },
                    "reply_to_source_id": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Source ULID to draft a reply for (Phase 10 reply-draft mode)."
                            " Mutually exclusive with topic-based generation."
                        ),
                        "maxLength": 200,
                    },
                    "from_briefing_id": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Optional ULID of a previously generated briefing whose markdown"
                            " seeds the LLM prompt as extra context."
                        ),
                        "maxLength": 200,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["inbox_triage", "source_extract", "meeting_followup"],
                        "description": (
                            "Phase 12 H4 (ADR-0016 改訂 §決定 (l)(b)) dispatch key for the"
                            " host skill that triggered the call. Required by inbox-triage /"
                            " source-extract / meeting-followup skills so the persisted"
                            " ``proposals.scope`` records the originating skill — mutually"
                            " exclusive with ``reply_to_source_id`` (reply-draft mode is"
                            " signalled by that field, not by ``mode``). When unset the"
                            ' service falls back to ``scope="all"`` (Phase 6 default).'
                        ),
                    },
                    "expand_graph": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Walk 1-hop graph neighbours of each recall hit to widen the"
                            " prompt context (ADR-0017 §(e)+(f))."
                        ),
                    },
                    "max_candidates": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 200,
                        "maximum": 4000,
                        "default": 2000,
                    },
                },
                "additionalProperties": False,
            },
            # LLM round-trip → open world; classified write because the
            # ProposalGenerated event is durable on the log even though
            # apply is HITL-only.
            policy=_policy_for_write(open_world=True),
            category=WriteCategory.PROPOSE_GENERATE,
            handler=handlers["propose.generate"],
        ),
        # ------------------------------------------------------------------
        # Phase 12 H1 (ADR-0022 改訂) — HITL ``propose.apply`` closes the
        # ``propose.generate`` → ``propose.apply`` round-trip at the MCP
        # boundary. Annotated ``destructive=false`` + ``idempotent=true``
        # via ``_policy_for_propose_apply``: the handler catches
        # ``OpsHubError("already applied/rejected")`` and returns a
        # normalised ``{ok:true, already_applied:true, ...}`` envelope
        # so the second call is observably a no-op. ``read_only`` stays
        # ``False`` because the first call still emits durable events
        # (TaskCreated / DecisionRecorded / ReplyDraftApplied +
        # ProposalApplied).
        # ------------------------------------------------------------------
        ToolSpec(
            name="propose.apply",
            title="Apply proposal candidate (HITL)",
            description=(
                "Apply candidate ``candidate_index`` of proposal ``proposal_id``."
                " Dispatches through ``TaskService`` / ``DecisionService`` for"
                " task / decision candidates and emits ``ProposalApplied``."
                " Idempotent: a second call for the same"
                " ``(proposal_id, candidate_index)`` returns"
                " ``{ok:true, already_applied:true, applied_entity_type,"
                " applied_entity_id}`` instead of raising. Host should still"
                " confirm the first invocation with the operator (HITL)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "proposal_id": {
                        "type": "string",
                        "description": "ULID of the proposal aggregate.",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "candidate_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Zero-based index into the proposal's ``candidates`` list."
                        ),
                    },
                },
                "required": ["proposal_id", "candidate_index"],
                "additionalProperties": False,
            },
            policy=_policy_for_propose_apply(),
            category=WriteCategory.PROPOSE_APPLY,
            handler=handlers["propose.apply"],
        ),
    ]
    return specs
