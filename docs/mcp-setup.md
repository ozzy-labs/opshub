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

Current tools (Phase 10 C2):

| Kind  | Name              | Purpose                                                          |
| ----- | ----------------- | ---------------------------------------------------------------- |
| read  | `recall.search`   | Hybrid semantic search across tasks / decisions / inbox / sources |
| read  | `task.list`       | List tasks (state-filterable)                                     |
| read  | `inbox.list`      | List inbox items (state-filterable)                               |
| read  | `decision.list`   | List recorded decisions                                           |
| write | `task.create`     | Create a new task                                                 |
| write | `inbox.add`       | Add an inbox item                                                 |
| write | `connector.sync`  | Trigger a registered connector's sync                             |

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

* auto-approve `recall.search`, `task.list`, `inbox.list`, `decision.list` — they are read-only against the local DB;
* require operator confirmation for `task.create`, `inbox.add`, `connector.sync` — they mutate the durable log or call an external SaaS.

The 84% vs <5% asymmetry between auto-approve and human-in-the-loop tool-poisoning success rates (cited in ADR-0022 §(c)) is the primary reason the write surface is intentionally narrow and explicitly flagged.

### Returns are summarised, not full-body (ADR-0022 §(d))

`recall.search` and the list tools cap snippets at ~200 characters. Full body text is never echoed without an explicit caller request (the current Phase 10 C2 tools do not expose that flag — Sub-issue D / E will add deliberate `include_full_body` paths where required).

This shrinks the agent context window and the data-exfiltration surface in one move.

## 6. Logging

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

## 7. Troubleshooting

| Symptom                                                  | Likely cause                                                              |
| -------------------------------------------------------- | ------------------------------------------------------------------------- |
| `MCP extras missing. Install with: uv sync --extra mcp`  | `mcp` extras not installed.                                               |
| Agent host shows zero tools after connecting             | `opshub init` not run yet (the engine wiring opens the SQLite store).     |
| `recall.search` fails with a ConfigError                 | No embedder backend configured. See `[embedding]` in `opshub.toml`.       |
| `connector.sync` fails with `ConnectorSyncFailed`        | Connector credentials missing. Run `opshub connector auth set <name>`.    |
| `unknown connector` error from `connector.sync`          | Connector extras not installed (`--extra connectors-<name>`).             |

## 8. Related

- [ADR-0022: MCP Server Surface](adr/0022-mcp-server-surface.md) — the invariants this doc operationalises.
- [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) — Phase 10 形A (opshub provides MCP + Agent Skills, the brain lives in the external host).
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md) — the keyring path that keeps tokens out of the MCP boundary.
- [Phase 10 plan §3 Sub-issue C / §4-C](phase-10-plan.md) — the planning ticket for the MCP surface.
