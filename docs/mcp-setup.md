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

Current tools (Phase 10 C2 baseline + Step 1 widening + Phase 12 H1):

| Kind  | Name                          | Purpose                                                          |
| ----- | ----------------------------- | ---------------------------------------------------------------- |
| read  | `recall.search`               | Hybrid semantic search across tasks / decisions / inbox / sources |
| read  | `task.list`                   | List tasks (state + `updated_after` / `updated_before` filterable) |
| read  | `inbox.list`                  | List inbox items (state + `created_after` / `created_before` filterable) |
| read  | `decision.list`               | List decisions (`recorded_after` / `recorded_before` filterable)  |
| read  | `brief`                       | Generate operational briefing (LLM round trip)                    |
| read  | `graph.related`               | List 1-hop graph neighbours of an entity                          |
| read  | `graph.trace`                 | Trace entity provenance backward up to N hops                     |
| read  | `graph.expand`                | Bidirectional N-hop graph subgraph rooted at an entity            |
| read  | `source.list`                 | List sources (connector / type + `observed_after` / `observed_before` filterable) |
| read  | `source.get`                  | Fetch one source row by ULID                                      |
| read  | `embeddings.find_duplicates`  | Scan embeddings for near-duplicate pairs above a threshold        |
| read  | `search`                      | Body-level FTS5 search (Phase 12 H1, phrase-quoted by default)    |
| write | `task.create`                 | Create a new task                                                 |
| write | `inbox.add`                   | Add an inbox item                                                 |
| write | `connector.sync`              | Trigger a registered connector's sync                             |
| write | `propose.generate`            | Generate next-action / reply-draft candidates (HITL apply)        |
| write | `propose.apply`               | Apply a proposal candidate (HITL, idempotent, Phase 12 H1)        |

Step 1 widening (post Phase 10) added the 7 new read tools and the HITL write `propose.generate`. Phase 12 H1 (ADR-0022 改訂 §決定 (f)) added 4 new MCP tools / arguments:

* **`search` (read, Phase 12 H1)** — body-level FTS5 search across `sources.body` (and the FTS5 `sources_fts` virtual table populated by migration 0019). Phrase-quoted by default; the CLI `--raw-query` flag is intentionally **not** exposed at the MCP boundary so host LLMs' free-form token streams stay safe from FTS5 syntax escapes. `ReadCategory.SEARCH` annotation = `readOnlyHint=true, destructiveHint=false`.
* **`propose.apply` (HITL write, Phase 12 H1)** — apply a previously-generated proposal candidate. **Idempotent**: the second call for the same `(proposal_id, candidate_index)` returns `{ok: true, already_applied: true, applied_entity_type: ..., applied_entity_id: ...}` instead of raising, by catching `OpsHubError("already applied")` and walking the event log to recover the historical entity tuple. `WriteCategory.PROPOSE_APPLY` annotation = `readOnlyHint=false, destructiveHint=false, idempotentHint=true` (the first MCP write tool to advertise `destructive=false`, the others remain `destructive=true`).
* **Physical-column time filters (Phase 12 H1)** — `task.list` accepts `updated_after` / `updated_before` (filters `tasks.updated_at`); `inbox.list` accepts `created_after` / `created_before` (`inbox_items.created_at`); `decision.list` accepts `recorded_after` / `recorded_before` (`decisions.recorded_at`); `source.list` accepts `observed_after` / `observed_before` (`sources.observed_at`). All ISO 8601, optional, `>= after` / `< before` half-open intervals. Physical-column naming (rather than business concepts like `completed_after`) keeps the projection schema and the MCP argument names aligned.
* **`propose.generate` `mode` argument (Phase 12 H4)** — accepts `inbox_triage` / `source_extract` / `meeting_followup` (the persist-bearing dispatch keys, ADR-0016 §決定 (l)(b)). `mode` is mutually exclusive with `reply_to_source_id`; the implicit `reply_draft` mode is signalled by `reply_to_source_id` alone. `handoff_draft` / `announcement_draft` skills do **not** route through `propose.generate` because they are text-only (no persist boundary, ADR-0016 §決定 (l)(a)).

Phase 12 H1 also unified the original 5 skills (`personal-brief`, `next-actions`, `reply-draft`, `pr-review`, `find-document`) on the MCP surface — they call MCP tools directly instead of falling back to the CLI shell. Phase 12 H2-H5 added 9 new skills on top (see `docs/secretary-agent.md` for the 14-skill catalog).

## 3a. Install the 14 secretary skills on the agent host (Phase 12)

Phase 12 ships **14 secretary skills** as opshub-resident SKILL.md files under `docs/skills/<name>/SKILL.md` (SSOT, ADR-0004 §決定 (c)). The `@ozzylabs/skills` Renovate preset distribution is deferred to Phase 13+; in Phase 12, copy them into each host's skill loader manually:

```sh
# Claude Code (user-level)
cp -r path/to/opshub/docs/skills/* ~/.claude/skills/

# Codex CLI / GitHub Copilot CLI (user-level)
cp -r path/to/opshub/docs/skills/* ~/.agents/skills/

# Project-local install (any host)
cp -r path/to/opshub/docs/skills/* ./.claude/skills/   # or ./.agents/skills/
```

The 14 skills are:

| Tier | Skill | Trigger phrase examples |
| --- | --- | --- |
| read | `personal-brief` | "今日のまとめ" / "今週どうなってる" / "最近どうなってる" |
| read | `next-actions` | "次に何やる?" / "やること教えて" / "優先度高いのは?" |
| read | `pr-review` | "PR #N レビューして" / "この差分どう?" |
| read | `find-document` | "Box にあったあの資料" / "<キーワード>含むファイル" |
| read | `meeting-prep` (Phase 12 H2) | "来週の会議準備" / "明日のミーティング前確認" |
| read | `research` (Phase 12 H2) | "<X> について調べて" / "<トピック> 網羅的に教えて" |
| read | `external-brief` (Phase 12 H3) | "上司向け週次報告" / "クライアント向け進捗まとめ" |
| read | `decision-rationale` (Phase 12 H3) | "あの決定はなぜ" / "X を選んだ理由" |
| read | `handoff-draft` (Phase 12 H5) | "引き継ぎ書作って" / "handoff 書く" |
| read | `announcement-draft` (Phase 12 H5) | "リリース告知文書いて" / "announcement 作って" |
| HITL write | `reply-draft` | "返信案考えて" / "下書き作って" |
| HITL write | `inbox-triage` (Phase 12 H4) | "受信箱整理して" / "inbox 仕分けて" |
| HITL write | `source-extract` (Phase 12 H4) | "この資料から task 抽出" / "<source_id> から候補を" |
| HITL write | `meeting-followup` (Phase 12 H4) | "会議後の action items" / "議事録から task 抽出" |

The full responsibility map (pair structure / MCP tool dependency matrix / HITL boundary / can-do/can't-do) lives in [docs/secretary-agent.md](secretary-agent.md). Skill descriptions include Japanese trigger phrases, so asking the agent host in natural language routes to the right skill automatically.

After copying, restart the agent host (or its skill loader if it supports hot reload) so the new skills become visible.

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

* auto-approve `recall.search`, `task.list`, `inbox.list`, `decision.list`, `brief`, `graph.related`, `graph.trace`, `graph.expand`, `source.list`, `source.get`, `embeddings.find_duplicates`, `search` (Phase 12 H1, FTS5) — they are read-only against the local DB (or, for `brief`, observation-only via the LLM provider);
* require operator confirmation for `task.create`, `inbox.add`, `connector.sync`, `propose.generate`, `propose.apply` (Phase 12 H1) — they mutate the durable log (`propose.generate` writes a `ProposalGenerated` event even though apply is still HITL-only; `propose.apply` writes `TaskCreated` / `DecisionRecorded` / `ReplyDraftSaved`, idempotent on the second call) or call an external SaaS.

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
// source.list — Phase 13 widens the connector_name vocabulary to
// {github, slack, ms365, box, box_drive, onedrive_drive, teams,
//  google_workspace} and the source_type vocabulary to include
// google_doc / google_slides / google_sheets / google_workspace_file
// alongside the Phase 11 office source_types.
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

`hitl_apply_required` is always `true` — apply (task / decision creation) still requires either `opshub propose apply <proposal-id> <candidate-index>` from the operator, or a confirmed `propose.apply` MCP call from the agent host.

### `propose.apply` (Phase 12 H1 HITL write, idempotent)

```json
// first call
{"proposal_id": "01HPROP…", "candidate_index": 0}

// first-call output
{"ok": true, "already_applied": false,
 "applied_entity_type": "task", "applied_entity_id": "01HTASK…",
 "proposal_id": "01HPROP…", "candidate_index": 0}

// second call with the same arguments
// → {"ok": true, "already_applied": true,
//    "applied_entity_type": "task", "applied_entity_id": "01HTASK…",
//    "proposal_id": "01HPROP…", "candidate_index": 0}
// (handler catches OpsHubError("already applied") and recovers
//  the historical entity tuple via _lookup_applied_entity)
//
// "already rejected" propagates as MCP isError, as do unknown proposals
// and out-of-range candidate indices.
```

### `search` (Phase 12 H1 read, FTS5 phrase-quoted)

```json
// input — raw_query flag is intentionally absent from the MCP schema;
// the body is phrase-quoted server-side so free-form host tokens stay safe.
{"query": "Q3 architecture review", "limit": 20}

// output
{"query": "Q3 architecture review", "hits": [
   {"entity_type": "source", "entity_id": "01HSRC…",
    "title": "q3-review-notes.docx", "snippet": "…", "rank": 0.0123},
   …],
 "truncated_snippets": true}
```

For the CLI `--raw-query` power-user path, use `opshub search "..." --raw` instead — that flag stays available at the CLI boundary but is intentionally not mirrored to the MCP surface.

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
| `connector.sync google_workspace` fails with `ConfigError: ... client_id`| Google OAuth client not configured. Set `[connectors.google_workspace] client_id` / `client_secret` (see `docs/google-workspace-setup.md`) and re-run `opshub connector auth set google_workspace`. |

## 9. Related

- [ADR-0022: MCP Server Surface](adr/0022-mcp-server-surface.md) — the invariants this doc operationalises.
- [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) — Phase 10 形A (opshub provides MCP + Agent Skills, the brain lives in the external host).
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md) — the keyring path that keeps tokens out of the MCP boundary.
- [Phase 10 plan §3 Sub-issue C / §4-C](phase-10-plan.md) — the planning ticket for the MCP surface.
