# OpsHub

**Local-first operational memory and execution hub for humans and AI agents.**

*人間と AI エージェントのための、ローカルファーストな Operational Memory 兼 実行ハブ。*

> Status: **Design phase**. 実装はまだ開始していません。`docs/` 配下のドキュメントは設計方針であり、議論を踏まえて更新されます。

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

# Regenerate the markdown workspace from the projection
opshub workspace generate

# Rebuild projections from the event store (idempotent)
opshub projections rebuild

# Inspect embedding backend status (Phase 4)
opshub embeddings status
```

All state lives under XDG directories; override via `OPSHUB_*` env vars (e.g.
`OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`).

## ドキュメント

- [Principles (基本方針)](docs/principles.md)
- [Architecture (アーキテクチャ)](docs/architecture.md)
- [Repository & Package Structure (リポジトリ・パッケージ構成)](docs/repository-structure.md)
- [Architecture Decision Records](docs/adr/README.md)

## ステータス

このリポジトリは設計フェーズにあり、Python 実装は未着手です。`docs/` のドキュメントは現時点の方向性を示すもので、議論を踏まえて随時更新されます。

実装着手後の予定:

1. **Phase 1**: Event store + tasks + CLI + markdown 生成 (foundation)
2. **Phase 2**: Inbox triage / decisions / locks / handoffs (coordination)
3. **Phase 3**: GitHub / Slack / Microsoft 365 / Box connectors
4. **Phase 4**: Vector recall / semantic search / briefing 自動生成 (semantic layer)

詳細は [Principles 項 9 (Phased Delivery)](docs/principles.md) と各 ADR を参照。

## License

MIT
