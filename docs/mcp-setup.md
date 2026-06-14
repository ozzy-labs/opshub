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

### 1.1 Slack connector token scopes

If the `connectors-slack` extras are enabled and the host will surface Slack data via `find-document` / `search` / `recall.search`, the User Token (`xoxp-...`) must carry the right scopes ([ADR-0018](adr/0018-slack-token-principal.md) §Decision (7)):

**Discovery listing (`opshub slack conversations`)** uses the `*:read` scopes:

| Purpose | Scope | Required for |
| --- | --- | --- |
| public channel listing | `channels:read` | `opshub slack conversations` (default) |
| user name lookup | `users:read` | DM / MPIM name resolution in `opshub slack conversations` |
| private channel listing | `groups:read` | `--types ...,private` in `opshub slack conversations`, private channel sync |
| DM listing | `im:read` | `--types ...,im` in `opshub slack conversations`, DM sync |
| MPIM listing | `mpim:read` | `--types ...,mpim` in `opshub slack conversations`, MPIM sync |

**Engagement-axis activity (`opshub slack conversations --sort=last_self_post` or `--sort=name + --since`, Phase 19-D [ADR-0035](adr/0035-slack-sort-axis-consolidation.md) §(c) §(d))** needs `search:read` so the discovery command can build the per-channel index of the operator's own most-recent post via `search.messages`:

| Purpose | Scope | Required for |
| --- | --- | --- |
| engagement axis lookup | `search:read` | `opshub slack conversations --sort=last_self_post` (explicit) **or** `--sort=name + --since` (engagement-axis implicit default per ADR-0035 §(d)); **User Token only** — Bot Tokens cannot hold `search:read`, opt back into the any-author probe with `--sort=last_activity` if you cannot grant the scope |

**Any-author activity (`opshub slack conversations --sort=last_activity --since <when>`, legacy [#374](https://github.com/ozzy-labs/opshub/issues/374)) and `opshub slack sync`** need the matching `*:history` scopes:

| Purpose | Scope | Required for |
| --- | --- | --- |
| public channel message history | `channels:history` | `opshub slack sync`, `opshub slack conversations --sort=last_activity --since <when>` for public channels |
| private channel message history | `groups:history` | private channel sync, `--sort=last_activity` for private channels |
| DM message history | `im:history` | DM sync, `--sort=last_activity` for DMs |
| MPIM message history | `mpim:history` | MPIM sync, `--sort=last_activity` for MPIMs |

Bot Tokens (`xoxb-...`) work as an alternative principal, but the bot must be `/invite`d into every channel it should see (ADR-0018 §Decision (2)). Bot Tokens **cannot** satisfy the engagement axis (`search:read` is a User-Token-only scope); the discovery command surfaces an explicit `ConfigError` recommending `--sort=last_activity` when a Bot Token is detected on the engagement path ([ADR-0034](adr/0034-slack-engagement-axis.md) §決定, [ADR-0035](adr/0035-slack-sort-axis-consolidation.md) §(f)).

Note that `--since` alone (without `--sort`) takes the engagement axis as its implicit default (ADR-0035 §(d)), so it requires `search:read` even though no `--sort=last_self_post` is spelled out. Bot Token users and `search:read`-less User Token users should pass `--sort=last_activity` explicitly to fall back to the any-author probe.

The `*:history` scopes are checked per type only on the any-author axis: when `--sort=last_activity --since <when>` is used and one of them is missing, that conversation type is dropped from the activity-filtered output and a single warning surfaces on stderr (other types continue to flow, `exit 0`). The discovery listing itself (no `--since`, default `--sort=name`) only needs the `*:read` scopes.

## 2. Initialise the database

The MCP server reads the same SQLite store as the CLI, so `opshub init` (or `opshub db migrate` on an existing store) must have run before the agent connects. Since Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384), [ADR-0029](adr/0029-distribute-assistant-skills-via-opshub-package.md)) `opshub init` also installs the 15 assistant skills into `~/.claude/skills/` and `~/.agents/skills/` (prompting on a TTY, defaulting to install otherwise — see §3a). Pass `--no-install-skills` to opt out.

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

Current tools (Phase 10 C2 baseline + Step 1 widening + Phase 12 H1 + Phase 18-C + Phase 21-D + Phase 25-D = **27 tools, 16 read + 11 write**):

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
| read  | `slack.demand.list`           | List Slack `<@self>` mention / DM digest rows (Phase 18-C, ADR-0033) |
| read  | `commitment.list`             | List two-way commitment ledger rows (`direction` / `state` / `person` filter, no LLM, Phase 25-D, ADR-0042) |
| read  | `person.list`                 | List resolved person-axis identity graph (idempotent resolve then list, Phase 25-D, ADR-0043) |
| read  | `catchup`                     | Summarise the "since last seen" diff, priority-ordered (Phase 25-D/E, ADR-0042) |
| write | `task.create`                 | Create a new task                                                 |
| write | `inbox.add`                   | Add an inbox item                                                 |
| write | `connector.sync`              | Trigger a registered connector's sync (Slack: always all workspaces — ADR-0041 §(f)) |
| write | `propose.generate`            | Generate next-action / reply-draft candidates (HITL apply)        |
| write | `propose.apply`               | Apply a proposal candidate (HITL, idempotent, Phase 12 H1)        |
| write | `browser.fetch`               | Render a Web page with headless Chromium, return extracted text + title (HITL, network egress, no persist, Phase 21-D, ADR-0037) |
| write | `commitment.scan`             | Run the flagship LLM commitment-extraction pass (HITL, open world, ConfigError if no LLM, Phase 25-D, ADR-0042) |
| write | `commitment.resolve`          | Mark a commitment done (HITL, local state transition, Phase 25-D, ADR-0042) |
| write | `commitment.dismiss`          | Mark an extraction a false positive (HITL, local state transition, Phase 25-D, ADR-0042) |
| write | `person.merge`                | Merge two persons (HITL, over-merge fix is `person.split`, Phase 25-D, ADR-0043) |
| write | `person.split`                | Detach one identity to a new person (HITL, undo an over-merge, Phase 25-D, ADR-0043) |

Step 1 widening (post Phase 10) added the 7 new read tools and the HITL write `propose.generate`. Phase 12 H1 (ADR-0022 改訂 §決定 (f)) added 4 new MCP tools / arguments:

* **`search` (read, Phase 12 H1)** — body-level FTS5 search across `sources.body` (and the FTS5 `sources_fts` virtual table populated by migration 0019). Phrase-quoted by default; the CLI `--raw-query` flag is intentionally **not** exposed at the MCP boundary so host LLMs' free-form token streams stay safe from FTS5 syntax escapes. `ReadCategory.SEARCH` annotation = `readOnlyHint=true, destructiveHint=false`.
* **`propose.apply` (HITL write, Phase 12 H1)** — apply a previously-generated proposal candidate. **Idempotent**: the second call for the same `(proposal_id, candidate_index)` returns `{ok: true, already_applied: true, applied_entity_type: ..., applied_entity_id: ...}` instead of raising, by catching `OpsHubError("already applied")` and walking the event log to recover the historical entity tuple. `WriteCategory.PROPOSE_APPLY` annotation = `readOnlyHint=false, destructiveHint=false, idempotentHint=true` (the first MCP write tool to advertise `destructive=false`, the others remain `destructive=true`).
* **Physical-column time filters (Phase 12 H1)** — `task.list` accepts `updated_after` / `updated_before` (filters `tasks.updated_at`); `inbox.list` accepts `created_after` / `created_before` (`inbox_items.created_at`); `decision.list` accepts `recorded_after` / `recorded_before` (`decisions.recorded_at`); `source.list` accepts `observed_after` / `observed_before` (`sources.observed_at`). All ISO 8601, optional, `>= after` / `< before` half-open intervals. Physical-column naming (rather than business concepts like `completed_after`) keeps the projection schema and the MCP argument names aligned.
* **`propose.generate` `mode` argument (Phase 12 H4)** — accepts `inbox_triage` / `source_extract` / `meeting_followup` (the persist-bearing dispatch keys, ADR-0016 §決定 (l)(b)). `mode` is mutually exclusive with `reply_to_source_id`; the implicit `reply_draft` mode is signalled by `reply_to_source_id` alone. `handoff_draft` / `announcement_draft` skills do **not** route through `propose.generate` because they are text-only (no persist boundary, ADR-0016 §決定 (l)(a)).

Phase 18-C ([ADR-0033 §決定 (c)](adr/0033-slack-mention-demand-digest.md), `ReadCategory.SLACK_DEMAND_LIST`) added one new read tool:

* **`slack.demand.list` (read, Phase 18-C)** — list rows from the Phase 18-B `slack_demand_digest` projection, optionally filtered by `types` (channel kinds `im` / `mpim` / `private` / `public`), `demand_kinds` (`mention` / `dm` / `mpim`), and `since_ts` (Slack epoch lower bound). Read-only over local SQLite — no Slack API round-trip; the projection consumes already-stored `SourceObserved` events. Order is fixed at `last_demand_desc` (newest first); the `order` argument is reserved for forward compatibility. **Phase 24-D ([ADR-0041](adr/0041-slack-multi-workspace.md) §(g)):** each row now carries a `workspace` object (`{"team_id": "T...", "alias": "acme" | null}`) — the digest row key is keyed on the stable workspace `team_id` and the `alias` label is resolved best-effort from the configured workspaces. There is no workspace *filter* argument yet (output field only; the filter is deferred until there is demand). Used by `next-actions` (priority signal), `personal-brief` (period summary), and `inbox-triage` (auxiliary priority).

Phase 21-D ([ADR-0037 §決定 (e)](adr/0037-browser-read-layer-playwright.md) + [ADR-0022 §決定 (g)](adr/0022-mcp-server-surface.md), `WriteCategory.BROWSER_FETCH`) added one new **write** tool, bringing the surface to 19 tools (13 read + 6 write):

* **`browser.fetch` (write / HITL, Phase 21-D)** — render an `http` / `https` `url` with headless Chromium (the Playwright browser read layer, [ADR-0037](adr/0037-browser-read-layer-playwright.md)) and return the extracted post-render DOM text (200-char snippet, secret-redacted) plus the page `<title>`, `text_chars` (full length), `truncated` (browser core's 500K cap hit) and `persisted: false`. **Classified write-category even though it returns data**: the call **egresses the public network**, which the "read tool = local SQLite only" invariant reserves for the same HITL bucket as `connector.sync` (`readOnlyHint=false` / `destructiveHint=true` / `openWorldHint=true`). Nothing is persisted — durable ingestion is the `web` connector's job (`opshub web sync`). Requires the `browser` extra plus a one-time `playwright install chromium`; a `ConfigError` names the install command when Chromium is missing. The async MCP handler bridges to the sync browser core via `asyncio.to_thread` (Playwright's sync API cannot run inside the asyncio loop, ADR-0037 §決定 (h)). No assistant skill calls it as a primary path yet (the `research`-skill wiring is deferred to the operations phase, ADR-0037 §Non-goals).

Phase 25-D ([ADR-0042](adr/0042-commitment-ledger.md) / [ADR-0043](adr/0043-cross-source-identity-resolution.md) + [ADR-0022 §決定 (h)](adr/0022-mcp-server-surface.md)) added the 秘書化 v1 surface — 3 read + 5 write tools, bringing the surface to **27 tools (16 read + 11 write)**:

* **`commitment.list` (read, Phase 25-D)** — list rows from the two-way commitment ledger ([ADR-0042](adr/0042-commitment-ledger.md)), filterable by `direction` (`i_owe` / `owed_to_me`), `state`, and `person`. **No LLM call** — the read just reflects what a prior `commitment.scan` mined. `due_before` is deliberately **not** exposed (`due` is free-form text the model read, not a comparable date). Used by `next-actions` (priority signal) and `personal-brief` (period summary).
* **`person.list` (read, Phase 25-D)** — resolve any unbound `(connector, handle)` (idempotent) then list the person-axis identity graph ([ADR-0043](adr/0043-cross-source-identity-resolution.md)), mirroring `opshub person list`. Read-only over local SQLite.
* **`catchup` (read, Phase 25-D/E)** — summarise everything since the operator last caught up (new sources + overdue commitments + unhandled Slack demand), priority-ordered ([ADR-0042](adr/0042-commitment-ledger.md), ADR-0015 応用). `since_last_seen` (default `true`) bounds the diff at the stored seen-marker.
* **`commitment.scan` (write / HITL, Phase 25-D)** — the flagship LLM extraction pass: read the sources observed since the stored scan cursor and call the configured LLM once per source to mine two-way commitments, minting durable `CommitmentExtracted` events. **Open-world write** (the LLM round-trip egresses the network, same bucket as `connector.sync`); `[llm] backend = disabled` surfaces a clean `ConfigError`. `max_sources` caps the per-call cost (default 200; a large backlog drains across several scans).
* **`commitment.resolve` / `commitment.dismiss` (write / HITL, Phase 25-D)** — closed-world destructive state transitions over local SQLite (mark a commitment done / a false positive). Not in the `propose.apply` non-destructive carve-out — each is a real mutation the service fail-fasts on duplicates of.
* **`person.merge` / `person.split` (write / HITL, Phase 25-D)** — closed-world destructive identity-graph mutations (merge two persons / detach one identity to a new person). The resolver only auto-merges exact matches; fuzzy merges (and undoing an over-merge) are operator HITL via these tools ([ADR-0043](adr/0043-cross-source-identity-resolution.md) §決定 (c)).

Phase 12 H1 also unified the original 5 skills (`personal-brief`, `next-actions`, `reply-draft`, `pr-review`, `find-document`) on the MCP surface — they call MCP tools directly instead of falling back to the CLI shell. Phase 12 H2-H5 added 9 new skills on top, and Phase 25-E added `catchup` (see `docs/assistant-agent.md` for the 15-skill catalog).

## 3a. Install the 15 assistant skills on the agent host

Phase 16-A ([ADR-0029](adr/0029-distribute-assistant-skills-via-opshub-package.md)) confirms **opshub package bundling + `opshub skills install`** as the canonical distribution channel for the 15 assistant skills (SSOT remains opshub `docs/skills/<name>/SKILL.md`, [ADR-0004 §決定 (c)](adr/0004-agent-runtime-boundary.md)). Phase 16-B ([#383](https://github.com/ozzy-labs/opshub/issues/383)) shipped the CLI; Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384)) wired it into `opshub init` so step §2 above also installs the 15 assistant skills (TTY prompt via `rich.prompt.Confirm`, default = yes; non-TTY default = install per ADR-0029 §決定 (d)).

If you ran `opshub init` already, the 15 skills are in place. Override with `opshub init --install-skills` / `opshub init --no-install-skills` for non-interactive scripts that need the explicit choice. Use `opshub skills install` directly to push later SSOT updates, add a `--scope project` install, or pass `--skip-existing` to preserve hand-edits. Flag details (`--host` / `--scope` / `--skip-existing` / `--dry-run` / `--print-paths`) and the `opshub skills list` status command live in [`docs/assistant-agent.md`](assistant-agent.md) §8.

The 15 skills are:

| Tier | Skill | Trigger phrase examples |
| --- | --- | --- |
| read | `personal-brief` | "今日のまとめ" / "今週どうなってる" / "最近どうなってる" |
| read | `next-actions` | "次に何やる?" / "やること教えて" / "優先度高いのは?" |
| read | `pr-review` | "PR #N レビューして" / "この差分どう?" |
| read | `find-document` | "Box にあったあの資料" / "<キーワード>含むファイル" |
| read | `meeting-prep` (Phase 12 H2) | "来週の会議準備" / "明日のミーティング前確認" |
| read | `research` (Phase 12 H2) | "`<X>` について調べて" / "`<トピック>` 網羅的に教えて" |
| read | `external-brief` (Phase 12 H3) | "上司向け週次報告" / "クライアント向け進捗まとめ" |
| read | `decision-rationale` (Phase 12 H3) | "あの決定はなぜ" / "X を選んだ理由" |
| read | `handoff-draft` (Phase 12 H5) | "引き継ぎ書作って" / "handoff 書く" |
| read | `announcement-draft` (Phase 12 H5) | "リリース告知文書いて" / "announcement 作って" |
| read | `catchup` (Phase 25-E) | "前回見て以降どうなった" / "久しぶりに状況確認" / "差分だけ教えて" |
| HITL write | `reply-draft` | "返信案考えて" / "下書き作って" |
| HITL write | `inbox-triage` (Phase 12 H4) | "受信箱整理して" / "inbox 仕分けて" |
| HITL write | `source-extract` (Phase 12 H4) | "この資料から task 抽出" / "<source_id> から候補を" |
| HITL write | `meeting-followup` (Phase 12 H4) | "会議後の action items" / "議事録から task 抽出" |

The full responsibility map (pair structure / MCP tool dependency matrix / HITL boundary / can-do/can't-do) lives in [docs/assistant-agent.md](assistant-agent.md). Skill descriptions include Japanese trigger phrases, so asking the agent host in natural language routes to the right skill automatically.

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

If you store new connector tokens, use `opshub <connector> auth set` (e.g. `opshub slack auth set --workspace <alias>`, `opshub github auth set`; Phase 17 ADR-0031, Slack per-workspace since Phase 24 ADR-0041 §(a)) — the agent never sees the token.

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
{"topic": "today", "format": "md"}

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
// source.list — Phase 14 widens the connector_name vocabulary to
// {github, slack, ms365, box, box_drive, onedrive_drive, teams,
//  google_workspace, google_mail, google_calendar} and the source_type
// vocabulary to include gmail_message / google_calendar alongside the
// Phase 13 google_* source_types and Phase 11 office source_types.
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
| `connector.sync` fails with `ConnectorSyncFailed`        | Connector credentials missing. Run `opshub <connector> auth set` (e.g. `opshub slack auth set --workspace <alias>`). Slack multi-workspace: the message names the failed alias(es) — fix those and re-run (ADR-0041 §(b)). |
| `unknown connector` error from `connector.sync`          | Connector extras not installed (`--extra connectors-<name>`).             |
| `connector.sync google_workspace` fails with `ConfigError: ... client_id`| Google OAuth client not configured. Set `[connectors.google_workspace] client_id` / `client_secret` (see `docs/google-workspace-setup.md`) and re-run `opshub google_workspace auth set`. |

### 8.1 Debugging `opshub mcp serve` itself (Phase 14)

When `opshub mcp serve` is spawned by an agent host, the CLI flags
that `opshub` parses at the root callback (`-v` / `-q` / `--debug` /
`--log-format` / `--log-file`) do **not** reach the server — the host
invokes the binary directly without going through the operator's
shell. Drive the log surface with the matching env vars instead
(Phase 14 epic #317 / [ADR-0027](adr/0027-observability-and-troubleshooting-logging.md)):

* `OPSHUB_LOG_LEVEL=DEBUG` (or `OPSHUB_DEBUG=1`) — DEBUG-level structlog output on stderr.
* `OPSHUB_LOG_FORMAT=json` — force JSON renderer so the agent host can capture log lines alongside the MCP stream (`console` is also accepted for interactive runs).
* `OPSHUB_LOG_FILE=/path/to/opshub-mcp.log` — tee the structlog output to a file created with mode 0600 (append-safe via `O_APPEND`).

In Claude Code (`~/.claude/mcp_servers.json`):

```json
{
  "mcpServers": {
    "opshub": {
      "command": "opshub",
      "args": ["mcp", "serve"],
      "env": {
        "OPSHUB_LOG_LEVEL": "DEBUG",
        "OPSHUB_LOG_FORMAT": "json"
      }
    }
  }
}
```

You can also launch the server manually from a shell for diagnostics:

```sh
OPSHUB_LOG_LEVEL=DEBUG opshub mcp serve
# blocks on stdio; Ctrl-D shuts it down cleanly
```

The server resolves these env vars via
`opshub.core.logging.resolve_log_settings` **before** any `get_logger`
call (`src/opshub/mcp/server.py:serve_stdio`), so the redaction
processor (R1) is already on the pipeline and `--debug` semantics
never relax the no-token-passthrough contract on the MCP response
side ([ADR-0022](adr/0022-mcp-server-surface.md) §(b)). For the full
recipe set (connector sync failures, embedding / LLM errors,
encrypted-DB issues) see [`docs/troubleshooting.md`](troubleshooting.md).

## 9. Related

* [ADR-0022: MCP Server Surface](adr/0022-mcp-server-surface.md) — the invariants this doc operationalises.
* [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) — Phase 10 形A (opshub provides MCP + Agent Skills, the brain lives in the external host).
* [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md) — the keyring path that keeps tokens out of the MCP boundary.
* [Phase 10 plan §3 Sub-issue C / §4-C](phase-10-plan.md) — the planning ticket for the MCP surface.
