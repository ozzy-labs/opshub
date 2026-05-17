# OpsHub

**Local-first operational memory and execution hub for humans and AI agents.**

*人間と AI エージェントのための、ローカルファーストな Operational Memory 兼 実行ハブ。*

> Status: **Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connectors + workspace ingest、MVP = framework + GitHub) + Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) complete (2026-05-17)**. Briefing 自動生成 / `links` projection 本実装 / event 駆動自動 embed は Phase 5+ で別途。Slack / Microsoft 365 / Box の connector は Phase 3.x 以降で順次追加する。`docs/` 配下のドキュメントは現状の方針を反映しつつ、議論を踏まえて更新されます。

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

# Connector layer (Phase 3, ADR-0010 / ADR-0014)
opshub connector auth set github       # store GitHub PAT in OS keychain
opshub connector sync github           # incremental sync (requires OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo)
opshub connector list                  # show registered connectors

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
opshub embeddings find-duplicates -t 0.92      # offline near-duplicate scan
opshub recall "認証の最近の決定"               # semantic search across all entities
```

All state lives under XDG directories; override via `OPSHUB_*` env vars (e.g.
`OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`).

## ドキュメント

- [Principles (基本方針)](docs/principles.md)
- [Architecture (アーキテクチャ)](docs/architecture.md)
- [Repository & Package Structure (リポジトリ・パッケージ構成)](docs/repository-structure.md)
- [Architecture Decision Records](docs/adr/README.md)

## ステータス

Phase 1 (foundation)・Phase 2 (coordination)・Phase 3 (connectors + workspace ingest、MVP = framework + GitHub)・Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) を 2026-05-17 に完了しました。`opshub init` / `task` / `inbox` / `decision` / `lock` / `session` / `agent run` / `handoff` / `connector` (`auth set` / `sync` / `list`) / `workspace ingest` / `workspace generate` / `projections rebuild` / `embeddings` (`rebuild` / `status` / `find-duplicates`) / `recall` が動作し、event store + 全 projection + markdown 生成 + GitHub connector + workspace inbox file ingest + semantic recall (local / OpenAI / Voyage 3 backend + sqlite-vec) + tests + CI が green の状態です。次は Phase 5 (briefing 自動生成 / `links` projection 本実装 / event 駆動自動 embed) の設計に着手します。Slack / Microsoft 365 / Box の connector は Phase 3.x 以降で順次追加します。

Phase ロードマップ:

1. **Phase 1**: Event store + tasks + CLI + markdown 生成 (foundation) — ✅ Complete (2026-05-17)
2. **Phase 2**: Inbox triage / decisions / locks / work sessions / agent runs / handoffs (coordination) — ✅ Complete (2026-05-17)
3. **Phase 3**: Connector framework + GitHub connector + workspace inbox file ingest — ✅ Complete (2026-05-17) (Slack / Microsoft 365 / Box は Phase 3.x で順次)
4. **Phase 4**: Vector recall / semantic search / 重複検出 (semantic layer、MVP = Pluggable Embedder + sqlite-vec) — ✅ Complete (2026-05-17) (briefing 自動生成 / event 駆動自動 embed は Phase 5 で)

詳細は [Principles 項 9 (Phased Delivery)](docs/principles.md) と各 ADR を参照。

## License

MIT
