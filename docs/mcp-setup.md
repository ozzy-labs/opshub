# MCP Setup — connecting an agent host to opshub

> Phase 10 sub-issue C / [ADR-0022](adr/0022-mcp-server-surface.md).

opshub ships a Model Context Protocol (MCP) server so external agent hosts (Claude Code 等) can call the ① core's read and write surface as MCP tools. The server is **stdio one transport** — no HTTP listener — so the agent host spawns `opshub mcp serve` as a subprocess and talks over stdin / stdout.

This doc covers the operator setup. The design rationale lives in [ADR-0022](adr/0022-mcp-server-surface.md).

## 1. Install the MCP extras

The `mcp` Python SDK is gated behind the `mcp` extras (ADR-0001 distribution constraint). Operators who connect an agent host install:

```sh
uv sync --extra mcp
```

If your distribution already includes other extras (e.g. `encryption`, `connectors-slack`), keep them on the same `uv sync` invocation:

```sh
uv sync --extra mcp --extra encryption --extra connectors-slack
```

Running `opshub mcp serve` without the extras prints a clear install hint and exits with code 2.

## 2. Initialise the database

The MCP server reads the same SQLite store as the CLI, so `opshub init` (or `opshub db migrate` on an existing store) must have run before the agent connects.

```sh
opshub init
opshub db migrate
```

## 3. Inspect the tool surface

`opshub mcp tools` prints the registry without starting the server. Use this to audit the read / write split before pointing an agent host at the server:

```sh
opshub mcp tools           # table form
opshub mcp tools -f json   # JSON form (matches the MCP annotations)
```

The output reflects the policy-as-data registry in `src/opshub/mcp/_registry.py`. Read tools advertise `readOnlyHint=true`; write tools advertise `readOnlyHint=false` + `destructiveHint=true`. Hosts that honour the hints (Claude Code 等) will auto-approve reads and require human confirmation for writes (ADR-0022 §(c)).

Current tools (Phase 10 C2 baseline + Step 1 widening):

| Kind  | Name                          | Purpose                                                          |
| ----- | ----------------------------- | ---------------------------------------------------------------- |
| read  | `recall.search`               | Hybrid semantic search across tasks / decisions / inbox / sources |
| read  | `task.list`                   | List tasks (state-filterable)                                     |
| read  | `inbox.list`                  | List inbox items (state-filterable)                               |
| read  | `decision.list`               | List recorded decisions                                           |
| read  | `brief`                       | Generate operational briefing (LLM round trip)                    |
| read  | `graph.related`               | List 1-hop graph neighbours of an entity                          |
| read  | `graph.trace`                 | Trace entity provenance backward up to N hops                     |
| read  | `graph.expand`                | Bidirectional N-hop graph subgraph rooted at an entity            |
| read  | `source.list`                 | List rows from the `sources` projection                           |
| read  | `source.get`                  | Fetch one source row by ULID                                      |
| read  | `embeddings.find_duplicates`  | Scan embeddings for near-duplicate pairs above a threshold        |
| write | `task.create`                 | Create a new task                                                 |
| write | `inbox.add`                   | Add an inbox item                                                 |
| write | `connector.sync`              | Trigger a registered connector's sync                             |
| write | `propose.generate`            | Generate next-action / reply-draft candidates (HITL apply)        |

Step 1 widening (post Phase 10) added the 7 new read tools and the HITL write `propose.generate` so Tier 1 skills (`daily-brief`, `next-actions`, `pr-review`, `file-lookup`) can call MCP directly instead of falling back to the CLI shell. `propose.generate` writes a `ProposalGenerated` event but the apply step (task / decision creation) still requires an operator-driven `opshub propose apply` invocation (ADR-0016 §決定 (c)).

## 4. Wire the agent host

### Claude Code

Edit `~/.claude/mcp_servers.json` (or use `claude mcp add`) to register opshub as an MCP server:

```json
{
  "mcpServers": {
    "opshub": {
      "command": "opshub",
      "args": ["mcp", "serve"]
    }
  }
}
```

If `opshub` is installed in a project-local venv, point `command` at the full binary path (e.g. `/path/to/project/.venv/bin/opshub`).

### Other MCP-capable hosts

Any host that supports stdio MCP servers can spawn `opshub mcp serve` the same way. The host should:

* spawn `opshub mcp serve` as a subprocess and connect to its stdio pair;
* honour the tool annotations — auto-approve reads, prompt for writes (see §5 below);
* close stdin to shut the server down (the server exits cleanly on EOF).

## 5. What the agent can / cannot do

opshub's MCP server enforces three security boundaries on the server side. The agent host should respect them on the client side too.

### No SaaS tokens cross the boundary (ADR-0022 §(b))

Tool input schemas never accept tokens. Connector credentials are resolved inside `opshub` via the keyring path (ADR-0014) and never leak into tool arguments, tool results, or the agent's transcript.

If you store new connector tokens, use `opshub connector auth set <name>` — the agent never sees the token.

### Read tools are safe to auto-approve, writes are not (ADR-0022 §(c))

The annotations on every tool reflect a policy-as-data registry. A compliant host will:

* auto-approve `recall.search`, `task.list`, `inbox.list`, `decision.list`, `brief`, `graph.related`, `graph.trace`, `graph.expand`, `source.list`, `source.get`, `embeddings.find_duplicates` — they are read-only against the local DB (or, for `brief`, observation-only via the LLM provider);
* require operator confirmation for `task.create`, `inbox.add`, `connector.sync`, `propose.generate` — they mutate the durable log (`propose.generate` writes a `ProposalGenerated` event even though apply is still HITL-only) or call an external SaaS.

The 84% vs <5% asymmetry between auto-approve and human-in-the-loop tool-poisoning success rates (cited in ADR-0022 §(c)) is the primary reason the write surface is intentionally narrow and explicitly flagged.

### Returns are summarised, not full-body (ADR-0022 §(d))

`recall.search` and the list tools cap snippets at ~200 characters. Full body text is never echoed without an explicit caller request (the current Phase 10 C2 tools do not expose that flag — Sub-issue D / E will add deliberate `include_full_body` paths where required).

This shrinks the agent context window and the data-exfiltration surface in one move.

## 6. Tool input / output examples (Step 1 widening)

These illustrate the new surface in JSON form. Inputs match `inputSchema`; outputs are the JSON body wrapped in a single `TextContent` block.

### `brief`

```json
// input
{"topic": "today", "format": "md", "expand_graph": false}

// output (format=md)
{"format":"md", "briefing_id":"01H…", "topic":"today",
 "markdown":"# Briefing — today\n…", "source_count": 12}
```

`format="json"` also returns `source_refs`, `model_id`, token counts, and `generated_at`.

### `graph.related` / `graph.trace` / `graph.expand`

```json
// graph.related input
{"entity_type":"task", "entity_id":"01HTASK…", "direction":"outbound", "limit": 50}

// graph.related output
{"entity_type":"task", "entity_id":"01HTASK…", "direction":"outbound",
 "items":[{"id":"01HLINK…", "from_entity_type":"task", "from_entity_id":"01HTASK…",
            "to_entity_type":"source", "to_entity_id":"01HSRC…",
            "link_type":"references", "created_at":"2026-05-30T12:00:00+00:00",
            "source_event_id":null}],
 "truncated": false, "next_offset": null}
```

`graph.trace` returns `paths[]` (each path has `depth` + `links[]`); `graph.expand` returns `nodes[]` + `edges[]` + `node_count` + `edge_count`.

### `source.list` / `source.get`

```json
// source.list
{"connector_name": "github", "source_type": "pull_request", "limit": 50}

// source.get
{"source_id": "01HSRC…"}
// → {"found": true, "id":"01HSRC…", "connector_name":"github",
//    "external_id":"PR-123", "source_type":"pull_request",
//    "title":"…", "url":"https://…", "summary":"…",
//    "observed_at":"…", "updated_at":"…"}
// or {"found": false, "source_id":"…"} when the id is unknown.
```

### `embeddings.find_duplicates`

```json
// input
{"entity_type": "source", "threshold": 0.92, "limit": 20}

// output
{"entity_type":"source", "threshold":0.92,
 "items":[{"entity_type":"source", "entity_id_a":"01HSRC…A",
            "entity_id_b":"01HSRC…B", "text_a":"…", "text_b":"…",
            "similarity":0.971}],
 "truncated": false, "next_offset": null}
```

### `propose.generate` (HITL write)

```json
// topic mode
{"topic": "next-action review", "from_briefing_id": "", "max_candidates": 5}

// reply-draft mode (mutually exclusive with topic / from_briefing_id)
{"reply_to_source_id": "01HSRC…", "max_candidates": 3}

// output
{"ok": true, "proposal_id": "01HPROP…",
 "candidates": [{"kind": "task", "title": "…", "index": 0}],
 "model_id": "…", "tokens_in": 0, "tokens_out": 0,
 "generated_at": "2026-05-30T12:00:00+00:00",
 "hitl_apply_required": true}
```

`hitl_apply_required` is always `true` — apply (task / decision creation) still requires `opshub propose apply <proposal-id> <candidate-index>` from the operator.

## 7. Logging

Every `CallTool` round-trip writes a pair of structured log records via `structlog` using the OpenTelemetry GenAI naming convention (ADR-0022 §(e)):

```json
{"event": "mcp.execute_tool", "gen_ai.operation.name": "execute_tool",
 "gen_ai.tool.name": "recall.search", "gen_ai.tool.call.id": "01HXXX...",
 "phase": "start"}
{"event": "mcp.execute_tool", "gen_ai.operation.name": "execute_tool",
 "gen_ai.tool.name": "recall.search", "gen_ai.tool.call.id": "01HXXX...",
 "duration_ms": 12.3, "status": "ok", "phase": "complete"}
```

Tool arguments and tool outputs are deliberately **not** logged — they may contain operator queries or source IDs the operator never volunteered to a log destination. A future `mcp-otel` extras will wire these records to an OpenTelemetry exporter without touching the call sites.

## 8. Troubleshooting

| Symptom                                                  | Likely cause                                                              |
| -------------------------------------------------------- | ------------------------------------------------------------------------- |
| `MCP extras missing. Install with: uv sync --extra mcp`  | `mcp` extras not installed.                                               |
| Agent host shows zero tools after connecting             | `opshub init` not run yet (the engine wiring opens the SQLite store).     |
| `recall.search` fails with a ConfigError                 | No embedder backend configured. See `[embedding]` in `opshub.toml`.       |
| `connector.sync` fails with `ConnectorSyncFailed`        | Connector credentials missing. Run `opshub connector auth set <name>`.    |
| `unknown connector` error from `connector.sync`          | Connector extras not installed (`--extra connectors-<name>`).             |

## 9. Related

- [ADR-0022: MCP Server Surface](adr/0022-mcp-server-surface.md) — the invariants this doc operationalises.
- [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) — Phase 10 形A (opshub provides MCP + Agent Skills, the brain lives in the external host).
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md) — the keyring path that keeps tokens out of the MCP boundary.
- [Phase 10 plan §3 Sub-issue C / §4-C](phase-10-plan.md) — the planning ticket for the MCP surface.
