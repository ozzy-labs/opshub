# AGENTS.md

このファイルは AI エージェント向けの共通 instructions です。

## 基本方針

- 日本語で応答する
- 推奨案とその理由を提示する
- `.env` ファイルは読み取り・ステージングしない
- 破壊的な Git 操作を避ける

## 設計判断のスタンス

- 過去の決定（ADR など）やこれまでの実装量にとらわれず、「あるべき姿」を優先して提案する。既存 ADR と食い違う場合は、その ADR の見直し・転換を提案してよい。
- ただし、過去の決定や実装量を**まったく無視してよいわけではない**。改修にかかる手間や既存設計との兼ね合いは判断材料として示したうえで、それでも「あるべき姿」を推す。
- この方針は、opshub にまだ実ユーザーがいない現段階を前提とする。1.0 リリースや実ユーザーが付いた時点で見直す。

## プロジェクト概要

`opshub`: Local-first operational memory and execution hub for humans and AI agents.

**Status**: Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connector layer + workspace ingest、MVP = framework + GitHub) + Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + Briefing 自動生成 + event-driven auto-embed) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + CLI)) + Phase 7 (connectors wave 2、MVP = Slack + Microsoft 365 + Box connectors) + Phase 8 (knowledge graph layer、MVP = ADR-0017 + `links` projection + 4 自動抽出経路 + manual link CRUD + graph traversal + `--expand-graph` integration) complete (2026-05-17) + Phase 9 (local-filesystem-backed connector layer、MVP = ADR-0019 + `sources.fingerprint` 列 (migration 0017) + `box_drive` connector (Box Drive デスクトップクライアント経由のローカル FS scan、`source_type="box_drive_file"`) + `core/platform.py` (WSL2 / macOS 判定 helper) + `opshub connector sync box_drive` 経路) complete (2026-05-23) — event store、全 projection (tasks / inbox_items / decisions / work_sessions / agent_runs / locks / handoffs / sources / connector_cursors / ingested_files / embeddings / briefings / proposals / links)、CLI (`init` / `task` / `inbox` / `decision` / `lock` / `session` / `agent run` / `handoff` / `connector auth set` / `connector sync` / `connector list` / `workspace ingest` / `workspace generate` / `projections rebuild` / `embeddings rebuild` / `embeddings drain` / `embeddings status` / `embeddings find-duplicates` / `recall` / `brief` (`--expand-graph` 対応) / `propose generate` (`--expand-graph` 対応) / `propose list` / `propose apply` / `propose reject` / `link add` / `link remove` / `link list` / `graph related` / `graph trace` / `graph expand` / `db migrate`)、markdown 生成、5 connector (GitHub + Slack + Microsoft 365 + Box + Box Drive (FS scan)) で `source_type` discriminator (`slack_message` / `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` / `box_event` / `box_drive_file`) 別に `sources` projection に persist (Phase 9 で `fingerprint` 列追加、`box_drive` の差分検出用)、workspace inbox file ingest、Pluggable Embedder (local sentence-transformers / OpenAI / Voyage の 3 backend) + sqlite-vec backed VectorStore、Pluggable LLMClient (Anthropic + OpenAI + Ollama の 3 backend) + structured output (`tool_use` / `tools=`) + Briefing 自動生成 + Proposal domain (human-in-the-loop apply) + Knowledge graph layer (4 自動抽出 + manual CRUD + `related` / `trace` / `expand` traversal + `--expand-graph` LLM context 拡張) + event-driven auto-embed (`[embedding] auto = true` opt-in) が end-to-end で動作。`llama.cpp` direct binding / proposal scoring / briefing cache + narrow scope / connector-side automatic `SourceReferenced` 発行 / graph visualisation web UI / watch mode (filewatch backend) / 追加 FS connector (OneDrive / Dropbox / Google Drive for desktop / iCloud) / multi-machine sync は Phase 6.x / 7.x / 8.x / 9.x / 10+ で順次追加 (詳細は `docs/principles.md` §9)。

## Tech Stack

- Runtime: Python 3.13+ (ADR-0001)
- Package manager: uv
- Version management: mise (`.mise.toml`)
- CLI: Typer
- DB: SQLite + SQLAlchemy 2.x Core + Alembic
- Validation: Pydantic v2
- Vector (Phase 4): sqlite-vec (`[vector]` extras, ADR-0012)

## 主要コマンド

```bash
uv sync                       # 依存関係インストール (lockfile に従う)
uv sync --locked              # CI 用、lockfile からの逸脱を許さない
uv sync --extra dev           # 開発用ツール (ruff/mypy/pyright/pytest) を含める
uv run opshub --help          # CLI 実行
uv run pytest                 # テスト実行
uv run ruff check             # lint
uv run ruff format            # format
uv run pyright                # 高速型チェック (local)
uv run mypy src               # 厳密型チェック (CI)
uv run alembic upgrade head   # DB migration 適用
```

## 検証（必須）

コード変更後、報告前に以下を通すこと:

1. `uv run ruff check` — lint 通過
2. `uv run ruff format --check` — フォーマット崩れなし
3. `uv run pyright` — 型チェック通過 (local 用、高速)
4. `uv run pytest` — テスト通過

CI では追加で `uv sync --locked` と `uv run mypy src` を実行する。

## コーディング規約

- Python: インデント 4 スペース (PEP 8)
- YAML / JSON / Markdown / TOML: インデント 2 スペース
- 改行コード: LF
- ファイル末尾: 改行あり
- 行長: 100 列 (Python `[tool.ruff] line-length = 100`)

## 規約

言語・コミット・ブランチ・PR のルールは README.md を参照すること。

<!-- begin: @ozzylabs/skills -->

## Available Skills

- `commit` — 変更をステージし、Conventional Commits でコミットする。プッシュや PR 作成は行わない。
- `commit-conventions` — Conventional Commits のメッセージ生成ルール（Type/Scope 判定表、フォーマット）。他スキルから参照される。
- `drive` — Issue または指示から実装・PR 作成・セルフレビュー・修正を自動で回し、merge-ready な PR を出す。単一/複数の Issue/PR と明示依存記法に対応。オプションでマージまで実行可能。
- `health` — リポジトリ改修中に意図せず残る状態（working tree, stash, branch, worktree, PR, issue, actions など）と skill catalog 整合性を一発で確認し、16 領域のステータス表で俯瞰しつつ各項目に固定語彙の推奨アクションを inline で付与して報告する。`--deep` 指定時は `要確認` 項目を read-only コマンドで追加調査し、機械判定可能な範囲でラベルを格上げする。検査と提示のみで、削除・close 等の実行は行わない。
- `implement` — Issue または指示をもとに、ブランチ作成・実装計画・コード変更を行う。Issue 番号またはテキスト指示を受け取る。
- `lint` — 全リンターを自動修正付きで実行し、結果を報告する。コード品質チェック、フォーマット、型チェック、セキュリティスキャンを含む。
- `lint-rules` — 拡張子別リンター・フォーマッターのコマンド対応表と型チェックルール。他スキルから参照される。
- `phase-issue` — Phase-N tracking issue を生成する。cross-session handoff context、決定事項表、PR ごとのタスク、DoD、Phase N+1 outlook を含む構造化された issue body を組み立てて gh issue create で起票する。引数で全項目を渡す非対話モードと、不足分を補う対話モード（Claude Code companion）に対応する。
- `pr` — コミット済みの変更をリモートにプッシュし、PR を作成・更新する。
- `review` — コード変更や PR を 11 観点（perspectives）でレビューし、JSON 構造化出力 + 人間可読レポートで報告する。quick / deep モードを切替可能。PR 番号またはワーキングツリー差分を入力に取る。
- `ship` — lint・コミット・PR 作成を一括実行する。変更に対して lint → コミット → PR 作成を順に実行する統合パイプライン。
- `test` — ビルド・テスト・型チェックを実行し、結果を報告する。
- `topics` — GitHub topics 候補を制約検証・人気度測定・broad+narrow / 単数複数比較・ozzy-labs 慣行ハードコードで選定し、`gh repo edit --add-topic` で適用する。スコープは ozzy-labs 内利用のみ。

<!-- end: @ozzylabs/skills -->

## Adapter Files

| Agent          | Configuration                         |
| -------------- | ------------------------------------- |
| Claude Code    | `CLAUDE.md`, `.claude/`               |
| Gemini CLI     | `.gemini/settings.json` → `AGENTS.md` |
| Codex CLI      | `AGENTS.md` + `.agents/skills/`       |
| GitHub Copilot | `AGENTS.md` + `.agents/skills/`       |

`.agents/skills/` and `.claude/skills/` are distributed from [`ozzy-labs/skills`](https://github.com/ozzy-labs/skills) via the `@ozzylabs/skills` Renovate preset, not from `commons` (see [ADR-0016](https://github.com/ozzy-labs/handbook/blob/main/adr/0016-create-skills-repo.md)).
