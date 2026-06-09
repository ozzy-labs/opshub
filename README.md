# OpsHub

[![PyPI](https://img.shields.io/pypi/v/ozzylabs-opshub.svg)](https://pypi.org/project/ozzylabs-opshub/)
[![CI](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

English | [日本語](README.ja.md)

**Local-first assistant agent platform — auditable operational memory for humans and AI agents.**

OpsHub is the local-first **assistant agent platform**: an auditable, event-sourced operational memory that an AI agent host (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) can drive on your behalf. Ask your agent "what should I do next?" or "draft a reply to that Slack thread" and it talks to OpsHub over MCP to recall, summarise, and propose — never sending your state to a cloud service.

Three layers ([ADR-0004](docs/adr/0004-agent-runtime-boundary.md)):

1. **You (human)** — ask in natural language.
2. **Assistant agent** — runs in your editor / terminal (Claude Code etc.). OpsHub ships **MCP server (`opshub mcp serve`) + Agent Skills (SKILL.md)** but no LLM runtime; the agent host owns the brain.
3. **OpsHub core (CLI)** — append-only event log + projections + body store + connectors. Same surface, whether you call it from the agent or from the CLI directly.

## Install

OpsHub is distributed on PyPI under the name **`ozzylabs-opshub`** (PyPI has no
namespace concept, so we follow the PEP 423 `<owner>-<package>` convention).
The CLI command stays `opshub`.

```bash
uv tool install ozzylabs-opshub
# or
pipx install ozzylabs-opshub
```

Optional extras (pulled only if needed):

```bash
uv tool install "ozzylabs-opshub[llm-anthropic,connectors-github]"
```

See [Optional dependencies](#optional-dependencies) below for the full extras
matrix.

### Alternative: install directly from GitHub

You can also install from a tagged git ref (no PyPI involvement):

```bash
uv tool install git+https://github.com/ozzy-labs/opshub.git@v0.1.0
```

This is useful for pre-release tags, unreleased fixes on `main`, or air-gapped
environments where PyPI isn't reachable.

## Quickstart

```bash
opshub init                                           # one-time DB + workspace setup
opshub task create "Write blog post about OpsHub"     # create a task
opshub task list                                       # see open tasks

# Once the LLM backend is configured (next section):
opshub brief "current priorities"                      # LLM-summarised briefing
opshub propose generate "what's next?"                 # LLM-proposed next-actions
opshub propose apply <proposal-id> 0                  # operator-approved entity creation
```

All state lives under XDG directories; override via `OPSHUB_*` env vars
(e.g. `OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`).

`opshub init` writes `~/.config/opshub/config.toml`; editing it takes effect on the **next startup** (no re-install / re-init needed). Precedence is `init args > env > toml > defaults` ([ADR-0032](docs/adr/0032-runtime-toml-config-loading.md)) — `OPSHUB_*` env vars override TOML for CI / headless, and `OPSHUB_CONFIG_DIR=<dir>` relocates the config directory. Invalid values fail-fast on startup with `ConfigError`; see [`docs/troubleshooting.md` §3.10](docs/troubleshooting.md) for operator-side details.

## Ask your assistant

Once you wire OpsHub into an agent host over MCP (see [Connect an agent host](#connect-an-agent-host-mcp) below), you can talk to your assistant in plain language. The agent calls the right OpsHub commands behind the scenes.

Phase 12 (2026-05-31) widened the assistant skill repertoire from 5 to **14** (10 read / 4 HITL write). The catalog below shows the most common triggers; the full responsibility map lives in [`docs/assistant-agent.md`](docs/assistant-agent.md).

| You ask | Skill that fires | What it does |
|---|---|---|
| "What should I do next?" / "今日のまとめ" / "今週どうなってる" | `personal-brief` / `next-actions` | Signals over the requested window (今日 / 今週 / 今月 / 先週 / 先月) + active tasks + untriaged inbox |
| "Draft a reply to that Slack thread" / "返信案考えて" | `reply-draft` | LLM-generated draft grounded in your past sending style (HITL apply, idempotent — OpsHub never sends) |
| "Review PR #123" | `pr-review` | Pulls related decisions / tasks / past discussion so the agent can review with context |
| "Find that Box file about X" / "Word/Excel/PPT 探して" / "あの Google Doc" / "Sheets 探して" / "あの Gmail" / "Gmail に来てた件" / "Google Calendar の予定" | `find-document` | Full-text + semantic search across Slack / Box / GitHub / MS365 / Teams / Box Drive / OneDrive Drive / Google Workspace (incl. Office body extraction, Phase 11; Google Docs / Slides / Sheets via Drive API export, Phase 13; Gmail + Google Calendar via Gmail API + Calendar API, Phase 14; FTS5 over MCP since Phase 12 H1) |
| "Summarise that Teams thread" / "Teams スレッド要約して" | `personal-brief` / `find-document` | Body-based recall over Teams chat history (Phase 11) |
| "Prep me for tomorrow's meeting" / "明日の会議準備" | `meeting-prep` (Phase 12) | Purpose + prior discussion + related decisions / sources for the upcoming calendar event |
| "Research X end-to-end" / "`<X>` について調べて" | `research` (Phase 12) | Cross-cutting topical research (semantic recall + FTS5 + graph expand + briefing) |
| "Weekly status for my manager" / "上司向け週次報告" | `external-brief` (Phase 12) | Outward-facing report (completed tasks + confirmed decisions, restrained tone) — pair of personal-brief |
| "Why did we choose X?" / "あの決定はなぜ" | `decision-rationale` (Phase 12) | Decision + source + prior decisions traced via `graph.trace` |
| "Triage my inbox" / "受信箱整理して" | `inbox-triage` (Phase 12, HITL) | Generate per-item action candidates over open inbox items, you approve each one |
| "Extract tasks from this doc" / "この資料から task 抽出" | `source-extract` (Phase 12, HITL) | Pull task / decision candidates from one source body |
| "Action items from yesterday's meeting" / "会議後のフォロー" | `meeting-followup` (Phase 12, HITL) | Action items extracted from recent calendar events — pair of meeting-prep |
| "Write the handoff doc" / "引き継ぎ書作って" | `handoff-draft` (Phase 12) | Markdown handoff text built from in-progress tasks + decisions + recall (text-only, no persist) |
| "Draft the release announcement" / "リリース告知文書いて" | `announcement-draft` (Phase 12) | Markdown announcement built from `recall.search` + recent decisions + briefing (text-only) |

The 14 assistant skills live under [`docs/skills/<name>/SKILL.md`](docs/skills/) (Phase 12 H1 made opshub the SSOT). The original 5 skills were renamed (`daily-brief` → `personal-brief`, `file-lookup` → `find-document`); the other 9 are new in Phase 12 H2-H5. The distribution channel was pinned in Phase 16-A ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) to **opshub Python package bundling + `opshub skills install`** (landed in Phase 16-B/C/D); see [Install the assistant skills](#install-the-assistant-skills) below. The `@ozzylabs/skills` Renovate preset (handbook ADR-0016) keeps shipping ecosystem-common skills (drive / lint / commit / ...); the assistant 14 are carved out into the opshub-bundled channel (disjoint namespaces, ADR-0029 §決定 (h)). See [`docs/assistant-agent.md`](docs/assistant-agent.md) for the responsibility map, MCP tool dependency matrix, pair structure, HITL boundary, and what OpsHub deliberately does not do (no write-back to SaaS, no always-on runtime, no auto-apply).

## Connect an agent host (MCP)

```bash
uv tool install "ozzylabs-opshub[mcp]"
opshub init
opshub db migrate
opshub mcp tools           # inspect read / write tool surface
```

Then point your agent host (Claude Code etc.) at `opshub mcp serve` as a stdio MCP server. Example for Claude Code (`~/.claude/mcp_servers.json`):

```json
{
  "mcpServers": {
    "opshub": { "command": "opshub", "args": ["mcp", "serve"] }
  }
}
```

Full setup (other hosts, encryption, troubleshooting): [`docs/mcp-setup.md`](docs/mcp-setup.md).

## Install the assistant skills

Phase 16-A ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) confirmed package bundling + `opshub skills install` as the canonical distribution channel for the 14 assistant skills (SSOT remains opshub `docs/skills/<name>/SKILL.md`). Phase 16-B ([#383](https://github.com/ozzy-labs/opshub/issues/383)) shipped the CLI; Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384)) wired it into `opshub init` so the standard 2-step setup also installs the 14 assistant skills:

```bash
uv tool install ozzylabs-opshub[mcp]
opshub init   # MCP server init + 14 assistant skills install (TTY prompt; non-interactive default = install)
```

`opshub init` prompts via `rich.prompt.Confirm` when stdin is a TTY (default = yes); on a non-TTY (script / fresh shell driving `uv tool install ... && opshub init`) it installs by default. Override with `opshub init --install-skills` / `opshub init --no-install-skills`. Use `opshub skills install` directly for follow-up updates, `--scope project` switches, or `--skip-existing` to preserve hand-edits; `opshub skills list` shows per-skill installed / missing / modified status. See [`docs/assistant-agent.md`](docs/assistant-agent.md) §8 for the full flag matrix (`--host` / `--scope` / `--skip-existing` / `--dry-run` / `--print-paths`).

## Configure an LLM backend (optional)

OpsHub is functional without any LLM — `task` / `decision` / `inbox` /
`connector sync` all work standalone. To enable `brief` / `propose`, configure
one of:

```bash
opshub llm auth set anthropic       # Anthropic Claude (recommended)
opshub llm auth set openai          # OpenAI
# Or for local-only:
ollama serve && ollama pull llama3.2:3b
# Then set [llm] backend = "ollama" in ~/.config/opshub/config.toml
```

See [`docs/principles.md`](docs/principles.md) §1 (Local-first) for the design
rationale.

## What's in OpsHub today

Phases 1–8 shipped (2026-05-17, v0.1.0). Phase 9 shipped 2026-05-23. Phase 10 + Phase 11 + Phase 12 + Phase 13 + Phase 14 shipped 2026-05-31:

| Phase | Layer | Highlights |
|---|---|---|
| 1 | Foundation | Event store, tasks, CLI, markdown workspace |
| 2 | Coordination | Inbox / decisions / sessions / locks / handoffs |
| 3 | Connectors framework | GitHub connector + workspace file ingest |
| 4 | Semantic recall | Pluggable Embedder (local / OpenAI / Voyage) + sqlite-vec + `recall` + duplicate detection |
| 5 | Briefing | Pluggable LLM (Anthropic / OpenAI) + `brief` + prompt injection mitigation |
| 6 | Action loop | Structured output + Ollama backend + `propose` (human-in-the-loop) |
| 7 | Connectors wave 2 | Slack + Microsoft 365 + Box |
| 8 | Knowledge graph | `links` projection + auto-extraction + `graph` + `--expand-graph` |
| 9 | Local-FS connectors | `box_drive` (Box Drive desktop client → local FS scan, ADR-0019) |
| 10 | Assistant agent platform | Full local body retention (ADR-0020) + encryption at rest (ADR-0021) + MCP server (ADR-0022) + `opshub search` (FTS5) + `opshub mcp serve` + assistant 5 Skills (renamed in Phase 12 H1 to `personal-brief` / `next-actions` / `reply-draft` / `pr-review` / `find-document`) + reply-draft (ADR-0016 §決定 (i)) + ADR-0004 revision (form-A: no agent runtime in core) + ADR-0010 revision (write-back ban) + ADR-0017 revision (reply_draft link types) |
| 11 | MS Office deep-dive | Office content extraction (ADR-0025, markitdown for `.docx`/`.xlsx`/`.pptx`, 50 MB / 500K chars cap, fail-safe) + ADR-0019 revision (`content_extraction` opt-in exception + `onedrive_drive` pattern generalisation) + ADR-0010 revision (Teams connector + body-extraction contract + delta-link cursor + invalidated-token fallback + Teams User Token principal) + new `teams` connector (Microsoft Graph chat delta + `Chat.Read`) + new `onedrive_drive` connector (FS scan, WSL2 `/mnt/onedrive` / macOS `~/OneDrive`) + `box_drive` Office extraction hook + Outlook body deep retention |
| 12 | Assistant Skills expansion | Assistant skill repertoire grows from 5 to **14** (10 read / 4 HITL write) — `meeting-prep` / `research` / `inbox-triage` / `external-brief` / `decision-rationale` / `handoff-draft` / `announcement-draft` / `meeting-followup` / `source-extract` are new; `daily-brief` / `file-lookup` renamed to `personal-brief` / `find-document`. 4 new MCP tools (`search` FTS5 + `propose.apply` HITL idempotent + physical-column time filters on `task.list` / `inbox.list` / `decision.list` / `source.list`). The original 5 skills now call MCP directly (CLI fallback dropped). ADR-0004 revision (Skills SSOT moves into opshub `docs/skills/`, distribution deferred to Phase 15+ — later pinned in Phase 16-A to opshub package bundling, [ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) + ADR-0022 revision (4 new MCP tools contract) + ADR-0016 revision (draft-family unified policy: persist boundary by reply-source presence, `mode` arg scope, triage scope, Candidate union freeze). `docs/assistant-agent.md` becomes the 14-skill catalog SSOT (responsibility map + HITL boundary + MCP tool dependency matrix + pair structure) |
| 13 | Google Workspace connector | New `google_workspace` connector pulls Google Docs / Slides / Sheets via Drive API v3 (`changes.list` cursor + invalidated-token full-pass fallback) + OAuth Refresh Token (offline access + refresh-token rotation write-back = MS365 / Box pattern, deliberately separate from the Teams verbatim-user-token pattern, ADR-0010 §Phase 13 revision (e)-(h)). Workspace native bodies (`google_doc` / `google_slides` / `google_sheets`) are extracted by exporting through MS Office mediatype (`.docx` / `.pptx` / `.xlsx`) and feeding the bytes into the Phase 11 markitdown path (`core/document_extract.extract_workspace_export(bytes, source_type)`, ADR-0025 §決定 (d') + (j)). 3 ADR revisions (ADR-0010 + ADR-0014 + ADR-0025), zero new ADRs (continuing the Phase 11 → 12 single-revision trajectory: 1 new + 2 revisions → 0 new + 3 revisions → 0 new + 3 revisions). Drive `files.watch` push notification is forbidden — `changes.list` poll-only keeps form-A integrity (no proactive runtime). New extras `connectors-google-workspace` (httpx). New setup doc `docs/google-workspace-setup.md` |
| 14 | Gmail + Google Calendar connectors | New `google_mail` connector pulls Gmail messages via Gmail API v1 (`users.messages.list` initial + `users.history.list` delta + 7-day TTL invalidated-cursor full-pass fallback). New `google_calendar` connector pulls Calendar events via Calendar API v3 (`events.list(syncToken=...)` delta + `410 GONE` invalidated-cursor full-pass + `timeMin`/`timeMax` window fallback). Both reuse the Phase 13 Google OAuth principal via a new shared `connectors/google_auth/` foundation — 1 Google account = 1 principal sharing the 3-scope fixed list `drive.readonly + gmail.readonly + calendar.readonly` across Drive + Gmail + Calendar (re-consent once, applies to all three). Mappers are deliberately **symmetric** with the MS365 Outlook / Calendar mappers (Phase 7 + Phase 11): Gmail = message unit `gmail_message` with `text/plain` preferred → `text/html` raw kept / no markitdown / no attachment retention / `[Labels: ...]` prepend / `[gmail body truncated]` tag / threadId field. Calendar = master event only `google_calendar` with override emitted as separate record / `start_iso - end_iso (N attendees)` summary / RRULE field / attendee list in body. Push notifications (`watch` / `events.watch`) are forbidden — poll-only keeps form-A integrity. Symmetry is mechanically verified by `tests/unit/connectors/test_mapper_symmetry.py`. 2 ADR revisions (ADR-0010 + ADR-0014), zero new ADRs (continuing the single-revision trajectory). No new extras — Gmail / Calendar ride on `connectors-google-workspace` (httpx) |

Next: **Phase 16+ candidates** — multi-machine sync, proactive assistant (cron-delegated commands, plus revisiting Gmail push / Calendar push), image OCR (PPT figures / slide images, carried over from Phase 13 → 14), additional connectors (Notion / Jira / Linear / Confluence, carried over from Phase 13), Drive Comments / Suggestions ingest (Phase 13 follow-up), Gmail / Calendar attachment body extraction (ADR-0025 extension), external write-back (Teams / Drive / Gmail reply send + Calendar event create with HITL), Calendar instance expansion projection (ms365 + google simultaneously), morphological tokenizer adoption (Lindera / SudachiPy / MeCab, carried over from Phase 15). The assistant 14 skill distribution channel was pinned in Phase 16-A ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) to opshub Python package bundling + `opshub skills install` and landed in Phase 16-B/C/D. Longer phase-by-phase narrative lives in
[`docs/architecture.md`](docs/architecture.md) §9 (Phased Delivery).

## Commands

```bash
# Foundation (Phase 1)
opshub task create "draft phase 2 plan"
opshub task list --format md                          # formats: table / md / json

# Coordination (Phase 2)
opshub inbox add "triage the failing nightly build"
opshub inbox triage <id> --to-task "fix nightly build"
opshub inbox list --format md
opshub decision record "adopt sqlite-vec for Phase 4"
opshub decision list
opshub lock acquire task:<task-ulid>                  # ADR-0013 coordination lock
opshub lock release <lock-id>
opshub lock list
opshub session start --scope "phase-3 design"         # work session bracket
opshub agent run begin claude                         # auto-injected into agent runs
opshub agent run end <run-id> --summary "drafted ADR-0017"
opshub session end --summary "EOD wrap"
opshub handoff open --from agent:claude --to ozzy --topic "review"
opshub handoff close <handoff-id> --note "merged"

# Connectors (Phase 3 + Phase 7, ADR-0010 / ADR-0014)
opshub github auth set                      # store GitHub PAT in OS keychain
opshub github sync                          # incremental sync (OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo)
opshub slack auth set             # store Slack OAuth token in OS keychain (User Token preferred, Bot Token also accepted — ADR-0018; full walkthrough: docs/slack-setup.md)
opshub slack conversations                                    # complete paste-ready [connectors.slack] block / name order (default; ADR-0035, #535)
opshub slack conversations --since 30d                        # name order + your last-post date (engagement-axis implicit, requires search:read User Token; ADR-0035 §(d))
opshub slack conversations --sort=last_self_post              # descending by your last post (implicit --since 90d cutoff + stderr notice; ADR-0035 §(e))
opshub slack conversations --sort=last_activity --since 30d   # descending by any-author last activity within 30 days
opshub slack conversations --format=table                     # pre-19-D default rendering (eyeball / script use; ADR-0035 §(a))
opshub slack sync                           # incremental sync ([connectors.slack] channels; sync_since date floor — ADR-0036)
opshub slack status                         # sync status: 3-axis cursor in human terms (daily view; --verbose for raw — #536)
opshub ms365 auth set             # OAuth paste-code (Microsoft Graph Calendar / OneDrive / Outlook)
opshub ms365 sync                           # incremental sync per endpoint
opshub box auth set               # OAuth paste-code (Box Events API)
opshub box sync                             # incremental sync (Box stream_position cursor)
opshub box_drive sync                       # Phase 9: scan local Box Drive mount (see docs/box-drive-setup.md)
opshub onedrive_drive sync                  # Phase 11: scan local OneDrive Desktop mount (see docs/onedrive-drive-setup.md)
opshub teams auth set             # Phase 11: store Microsoft Graph User Token (Chat.Read, see docs/teams-setup.md)
opshub teams sync                           # Phase 11: Graph chat delta + invalidated-token fallback
opshub google_workspace auth set            # Phase 14: Google OAuth paste-code, shared across Drive + Gmail + Calendar (drive.readonly + gmail.readonly + calendar.readonly, single re-consent applies to all three; see docs/google-workspace-setup.md)
opshub google_workspace sync                # Phase 13: Drive API v3 changes.list cursor + invalidated-token fallback (Workspace export → markitdown when content_extraction = true)
opshub google_mail sync                     # Phase 14: Gmail API v1 users.history.list delta + 7-day TTL fallback (message unit, Outlook-symmetric body extraction)
opshub google_calendar sync                 # Phase 14: Calendar API v3 events.list(syncToken=...) + 410 GONE fallback (master event only + override separate record, MS365 Calendar-symmetric)
opshub web sync                             # Phase 21: render [connectors.web] pages with headless Chromium, persist web_page sources (no auth; run 'playwright install chromium' once; SHA-256 fingerprint change detection — ADR-0037)
opshub connectors                                 # show registered connectors

# Workspace + projections
opshub workspace ingest                               # ingest workspace/inbox/*.md (Phase 3)
opshub workspace ingest --dry-run                     # scan only, no writes
opshub workspace generate                             # regenerate markdown workspace from projections
opshub projections rebuild                            # rebuild projections from the event store (idempotent)

# Semantic recall (Phase 4, ADR-0012)
opshub embedder auth set openai             # store OpenAI API key in OS keychain
opshub embeddings rebuild                             # bulk-embed task/decision/inbox/source bodies (Phase 10: now body-based, ADR-0020)
opshub embeddings status                              # show backend + per-entity-type embedded vs pending
opshub embeddings drain                               # retry pending embeddings (auto-embed hook backup)
opshub embeddings find-duplicates -t 0.92             # offline near-duplicate scan
opshub recall "recent decisions about authentication" # semantic search across all entities

# Full-text search across source bodies (Phase 10, ADR-0012 改訂 §4 + ADR-0020)
opshub search "ticket-1234"                           # SQLite FTS5 across Slack / GitHub / Box / MS365 / Box Drive bodies
opshub search "deploy AND failure" --raw              # opt into FTS5 boolean / phrase / prefix syntax
opshub search "channel ID" --connector slack          # restrict to one connector

# Briefing (Phase 5, ADR-0015)
opshub llm auth set anthropic               # store Anthropic API key in OS keychain
opshub brief "phase 5 progress"                       # LLM-backed briefing (markdown to stdout)
opshub brief "phase 5 progress" --save                # also persist under <workspace>/briefings/
opshub brief "phase 5 progress" --format json         # JSON with briefing_id / model / tokens / source_refs
opshub brief "phase 8 review" --expand-graph          # widen LLM context via 1-hop graph expansion

# Action loop (Phase 6, ADR-0016, Phase 10 reply-draft)
opshub propose generate "next steps"                  # LLM proposes task/decision candidates
opshub propose generate "next steps" --from-briefing <id>
opshub propose generate "next steps" --format json
opshub propose generate "next steps" --expand-graph   # 1-hop graph expansion for proposals
opshub propose generate "" --reply-to <source-id>     # Phase 10: reply-draft mode (topic is ignored; ADR-0016 §決定 (i))
opshub propose list                                   # recent proposals (markdown table)
opshub propose list --state pending --limit 10        # filter: pending / applied / rejected
opshub propose apply <proposal-id> <candidate-index>  # operator approval → creates entities (reply-draft saves locally, never sends)
opshub propose reject <proposal-id> <candidate-index> --reason "out of scope"

# Knowledge graph (Phase 8, ADR-0017)
opshub link add task:<task-id> source:<src-id> --type references
opshub link list --from task:<task-id>                # filter by --from / --to / --type
opshub link remove <link-id> --reason "wrong source"  # hard delete (emits LinkDeleted)
opshub graph related task:<task-id> --direction both  # 1-hop neighbours (md / json / dot)
opshub graph trace task:<task-id> --depth 3           # backward provenance walk (default 3, max 10)
opshub graph expand task:<task-id> --depth 2 --format dot

# MCP server surface (Phase 10, ADR-0022)
opshub mcp tools                                       # inspect read / write tool surface (auditable policy-as-data)
opshub mcp tools -f json                               # JSON for diff / scripting
opshub mcp serve                                       # stdio MCP server — agent host spawns this as a subprocess
```

### Progress for long-running commands

Long-running commands — `connector sync`, `embeddings rebuild` / `drain`, and
`projections rebuild` — show a progress indicator on stderr while they run
(a spinner + item count for streaming syncs, a percentage bar + ETA for
counted work). It is shown automatically when stderr is a TTY and is a no-op
otherwise, so piped / redirected output stays clean. Force it either way with
the root `--progress` / `--no-progress` flag or the `OPSHUB_PROGRESS`
environment variable (precedence: flag > env > TTY). `OPSHUB_PROGRESS`
accepts `1` / `true` / `yes` / `on` to force on and `0` / `false` / `no` /
`off` to force off (case-insensitive, surrounding whitespace trimmed; any
other value is ignored and falls back to TTY auto-detection). See
[ADR-0026](docs/adr/0026-cli-progress-reporting.md).

### Troubleshooting (verbosity / debug / log file)

Every subcommand inherits a small set of global flags for incident
investigation ([ADR-0027](docs/adr/0027-observability-and-troubleshooting-logging.md)):
`-v` / `-vv` raises the log level to INFO / DEBUG, `-q` / `-qq` lowers
it to WARNING / ERROR, `--debug` adds a sanitised full traceback to
stderr on `OpsHubError`, `--log-format auto|json|console` overrides
the renderer choice, and `--log-file PATH` tees the structlog output
to a file created with mode 0600. The matching env vars
(`OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` /
`OPSHUB_LOG_FILE`) cover paths where flags cannot be passed — notably
`opshub mcp serve` started by an agent host. Tokens / keys / known
secret shapes (`sk-`, `ghp_`, `github_pat_`, `xox*-`, `AKIA`, `AIza`,
`Bearer`, JWT) are redacted at every verbosity. See
[`docs/troubleshooting.md`](docs/troubleshooting.md) for the recipes
and [`SECURITY.md`](SECURITY.md) §Debug-safe logging for the
guarantees.

## Optional dependencies

| Extras | Purpose | Heavy? |
|---|---|---|
| `vector` | sqlite-vec for semantic recall | Small |
| `local-embedding` | sentence-transformers (bge-m3, ~500MB) | Heavy |
| `api-embedding-openai` / `api-embedding-voyage` | API embedder backends | Small |
| `llm-anthropic` / `llm-openai` | API LLM backends | Small |
| `llm-ollama` | Ollama daemon client | Small |
| `connectors-github` / `connectors-slack` / `connectors-ms365` / `connectors-box` | SaaS connectors | Small |
| `connectors-teams` | Microsoft Teams connector (Phase 11, msal + httpx) | Small |
| `connectors-google-workspace` | Google Workspace connector (Phase 13, httpx). Pair with `[office]` extras to enable `content_extraction = true` and Workspace export → markitdown body retention for `google_doc` / `google_slides` / `google_sheets` | Small |
| `office` | Office document content extraction (Phase 11, ADR-0025). Pulls `markitdown` with the `[docx,xlsx,pptx]` sub-extras (i.e. `mammoth` / `openpyxl` / `python-pptx`) so only the three Office sub-formats opshub supports are installed | Small |
| `browser` | Playwright browser read layer (Phase 21, ADR-0037). Pulls `playwright>=1.50` for the `web` connector + the MCP `browser.fetch` tool. Run `playwright install chromium` once after installing (Chromium binary is not bundled; an absent binary fails with a `ConfigError` naming the install command) | Small (the Chromium binary is fetched separately) |
| `secrets` | OS keyring backend | Small |
| `encryption` | SQLCipher-backed at-rest encryption (Phase 10, ADR-0021) | Small |
| `mcp` | MCP server SDK for `opshub mcp serve` (Phase 10, ADR-0022) | Small |
| `dev` | Test + lint toolchain | Medium |

## Documentation

- [`docs/principles.md`](docs/principles.md) — design principles (local-first, event-sourced, full local content retention)
- [`docs/architecture.md`](docs/architecture.md) — layered architecture overview
- [`docs/assistant-agent.md`](docs/assistant-agent.md) — Phase 10 assistant agent platform usage (skills catalog, what it can / cannot do)
- [`docs/mcp-setup.md`](docs/mcp-setup.md) — Phase 10 MCP setup for agent hosts (Claude Code etc.)
- [`docs/adr/`](docs/adr/README.md) — Architecture Decision Records (incl. ADR-0026 CLI progress reporting)
- [`docs/slack-setup.md`](docs/slack-setup.md) — Phase 7 `slack` connector setup (Slack app → MVP scopes → token → discover → sync, end-to-end)
- [`docs/box-drive-setup.md`](docs/box-drive-setup.md) — Phase 9 `box_drive` connector setup (WSL2 / macOS)
- [`docs/onedrive-drive-setup.md`](docs/onedrive-drive-setup.md) — Phase 11 `onedrive_drive` connector setup (WSL2 / macOS)
- [`docs/teams-setup.md`](docs/teams-setup.md) — Phase 11 `teams` connector setup (Azure app registration + User Token)
- [`docs/google-workspace-setup.md`](docs/google-workspace-setup.md) — Phase 13 `google_workspace` connector setup (GCP project + OAuth consent screen + paste-code Refresh Token)
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — Phase 14 epic #317 troubleshooting recipes (`-v` / `--debug` / `--log-file`, MCP serve env vars, redaction guarantees)
- [`docs/upgrading.md`](docs/upgrading.md) — version migration notes (when applicable)
- [`docs/release-notes-v0.1.0.md`](docs/release-notes-v0.1.0.md) — v0.1.0 narrative release notes
- [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md) — how to cut a release (maintainers)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guidelines
- [`SECURITY.md`](SECURITY.md) — vulnerability disclosure + Phase 10 body retention threat model

## License

MIT. See [LICENSE](LICENSE).
