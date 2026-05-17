# OpsHub

**Local-first operational memory and execution hub for humans and AI agents.**

*人間と AI エージェントのための、ローカルファーストな Operational Memory 兼 実行ハブ。*

> Status: **Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connectors + workspace ingest、MVP = framework + GitHub) + Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + `opshub brief` + event-driven auto-embed 補助) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + CLI)) + Phase 7 (connectors wave 2、MVP = Slack + Microsoft 365 + Box connectors) complete (2026-05-17)**. `llama.cpp` direct binding / briefing cache + narrow scope / `links` projection 本実装 / multi-machine sync は Phase 6.x / 7.x 以降で扱う。Phase 8 (Knowledge graph、epic #128) は planned。`docs/` 配下のドキュメントは現状の方針を反映しつつ、議論を踏まえて更新されます。

## 概要

OpsHub は GitHub・Slack・Microsoft 365・Box などの業務シグナルをローカルの event store に集約し、Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI のいずれからも、triage・下書き・実行を 1 つのワークスペースで行えるようにする。

OpsHub は以下を提供する。

- **Operational Memory**: 業務上の出来事・決定・タスクをローカルに継続保持する仕組み
- **Workspace Runtime**: 人間と AI エージェントが共有する作業面
- **Multi-Agent Coordination Layer**: 複数 AI エージェントの協調実行
- **Execution Hub**: SaaS への返信・送信などのアクション実行ポイント

OpsHub は以下では「ない」。

- 単なる TODO 管理ツール
- SaaS 型プロジェクト管理ツール
- AI チャットボットラッパー
- ドキュメント保管庫
- 単独の workflow オーケストレータ

## Quickstart

OpsHub is distributed as a Python CLI. Install via `uv` (recommended) or `pip`:

```bash
# Recommended: isolated tool install via uv
uv tool install --from . opshub

# Or in a project venv
uv pip install -e .
```

First-time setup:

```bash
opshub init
```

This creates `~/.config/opshub/config.toml`, `~/.local/share/opshub/`, the
workspace at `~/opshub/workspace/`, and applies the database migrations.

Daily use:

```bash
# Capture a task
opshub task create "draft phase 2 plan"

# List tasks (formats: table / md / json)
opshub task list --format md

# Capture / triage / list an inbox item
opshub inbox add "triage the failing nightly build"
opshub inbox triage <id> --to-task "fix nightly build"
opshub inbox list --format md

# Record / list a decision
opshub decision record "adopt sqlite-vec for Phase 4"
opshub decision list

# Acquire / release / list a coordination lock (ADR-0013)
opshub lock acquire task:<task-ulid>
opshub lock release <lock-id>
opshub lock list

# Bracket a work session (auto-injected into agent runs)
opshub session start --scope "phase-3 design"
opshub agent run begin claude
opshub agent run end <run-id> --summary "drafted ADR-0017"
opshub session end --summary "EOD wrap"

# Open / close a handoff between actors
opshub handoff open --from agent:claude --to ozzy --topic "review"
opshub handoff close <handoff-id> --note "merged"

# Connector layer (Phase 3 GitHub + Phase 7 wave 2、ADR-0010 / ADR-0014)
opshub connector auth set github                    # store GitHub PAT in OS keychain
opshub connector sync github                        # incremental sync (requires OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo)
opshub connector auth set connector:slack           # store Slack bot token in OS keychain
opshub connector sync slack                         # incremental sync (requires [connectors.slack] channels)
opshub connector auth set connector:ms365           # OAuth paste-code flow (Microsoft Graph Calendar / OneDrive / Outlook)
opshub connector sync ms365                         # incremental sync per endpoint (independent cursors)
opshub connector auth set connector:box             # OAuth paste-code flow (Box Events API)
opshub connector sync box                           # incremental sync (Box stream_position cursor)
opshub connector list                               # show registered connectors

# Ingest hand-written notes (Phase 3 workspace inbox)
opshub workspace ingest                # ingest workspace/inbox/*.md
opshub workspace ingest --dry-run      # scan only, no writes

# Regenerate the markdown workspace from the projections
opshub workspace generate

# Rebuild projections from the event store (idempotent)
opshub projections rebuild

# Semantic recall layer (Phase 4, ADR-0012)
opshub connector auth set embedder:openai      # store OpenAI API key in OS keychain
opshub embeddings rebuild                      # bulk-embed task/decision/inbox/source summaries
opshub embeddings status                       # show backend + per-entity-type embedded vs pending
opshub embeddings drain                        # retry pending embeddings (auto-embed hook backup)
opshub embeddings find-duplicates -t 0.92      # offline near-duplicate scan
opshub recall "認証の最近の決定"               # semantic search across all entities

# Briefing layer (Phase 5, ADR-0015)
opshub connector auth set llm:anthropic        # store Anthropic API key in OS keychain
opshub brief "phase 5 progress"                # LLM-backed briefing on a topic (markdown to stdout)
opshub brief "phase 5 progress" --save         # also persist under <workspace>/briefings/
opshub brief "phase 5 progress" --format json  # JSON record with briefing_id / model / tokens / source_refs

# Action loop layer (Phase 6, ADR-0016)
opshub propose generate "next steps"                       # LLM proposes task/decision candidates (markdown to stdout)
opshub propose generate "next steps" --from-briefing <id>  # seed from a Phase 5 briefing
opshub propose generate "next steps" --format json         # JSON record with proposal_id / candidates / model / tokens
opshub propose list                                        # recent proposals (markdown table)
opshub propose list --state pending --limit 10             # filter by candidate state (pending / applied / rejected)
opshub propose apply <proposal-id> <candidate-index>       # operator approval → creates task / decision via existing services
opshub propose reject <proposal-id> <candidate-index> --reason "out of scope"
```

All state lives under XDG directories; override via `OPSHUB_*` env vars (e.g.
`OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`).

## ドキュメント

- [Principles (基本方針)](docs/principles.md)
- [Architecture (アーキテクチャ)](docs/architecture.md)
- [Repository & Package Structure (リポジトリ・パッケージ構成)](docs/repository-structure.md)
- [Architecture Decision Records](docs/adr/README.md)

## ステータス

Phase 1 (foundation)・Phase 2 (coordination)・Phase 3 (connectors + workspace ingest、MVP = framework + GitHub)・Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出)・Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM + `opshub brief` + event-driven auto-embed 補助)・Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain)・Phase 7 (connectors wave 2、MVP = Slack + Microsoft 365 + Box connectors) を 2026-05-17 に完了しました。`opshub init` / `task` / `inbox` / `decision` / `lock` / `session` / `agent run` / `handoff` / `connector` (`auth set` / `sync` (`github` / `slack` / `ms365` / `box`) / `list`) / `workspace ingest` / `workspace generate` / `projections rebuild` / `embeddings` (`rebuild` / `drain` / `status` / `find-duplicates`) / `recall` / `brief` / `propose` (`generate` / `list` / `apply` / `reject`) が動作し、event store + 全 projection (`briefings` / `proposals` 含む) + markdown 生成 + 4 connector (GitHub + Slack + Microsoft 365 + Box) + workspace inbox file ingest + semantic recall + Pluggable LLM (Anthropic / OpenAI / Ollama) + structured output + Proposal domain + tests + CI が green の状態です。`llama.cpp` direct binding / briefing cache + narrow scope / `links` projection 本実装 / multi-machine sync は Phase 6.x / 7.x 以降。Phase 8 (Knowledge graph、epic #128) は planned。

Phase ロードマップ:

1. **Phase 1**: Event store + tasks + CLI + markdown 生成 (foundation) — ✅ Complete (2026-05-17)
2. **Phase 2**: Inbox triage / decisions / locks / work sessions / agent runs / handoffs (coordination) — ✅ Complete (2026-05-17)
3. **Phase 3**: Connector framework + GitHub connector + workspace inbox file ingest — ✅ Complete (2026-05-17)
4. **Phase 4**: Vector recall / semantic search / 重複検出 (semantic layer、MVP = Pluggable Embedder + sqlite-vec) — ✅ Complete (2026-05-17) (briefing 自動生成 / event 駆動自動 embed は Phase 5 で)
5. **Phase 5**: Briefing layer (ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + `opshub brief` + event-driven auto-embed 補助) — ✅ Complete (2026-05-17)
6. **Phase 6**: Action loop layer (ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + `opshub propose` CLI)) — ✅ Complete (2026-05-17) (`llama.cpp` direct binding / proposal scoring は Phase 6.x で)
7. **Phase 7**: Connectors Wave 2 (Slack + Microsoft 365 + Box) — ✅ Complete (2026-05-17) (Phase 3 framework + ADR-0010 + ADR-0014 + ADR-0005 を再利用、epic #113)
8. **Phase 8**: Knowledge graph (epic #128) — Planned

詳細は [Principles 項 9 (Phased Delivery)](docs/principles.md) と各 ADR を参照。

## License

MIT
