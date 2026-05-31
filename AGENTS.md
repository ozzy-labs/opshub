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

`opshub`: Local-first secretary agent platform — auditable operational memory for humans and AI agents.

**Status**: Phase 1-11 complete (Phase 11 complete 2026-05-31, epic #233). Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connector layer + workspace ingest、MVP = framework + GitHub) + Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + Briefing 自動生成 + event-driven auto-embed) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain) + Phase 7 (connectors wave 2、MVP = Slack + Microsoft 365 + Box) + Phase 8 (knowledge graph layer、MVP = ADR-0017 + `links` projection + 4 自動抽出 + manual link CRUD + traversal + `--expand-graph`) complete (2026-05-17) + Phase 9 (local-filesystem-backed connector layer、MVP = ADR-0019 + `sources.fingerprint` (migration 0017) + `box_drive` connector + `core/platform.py`) complete (2026-05-23) + Phase 10 (Secretary Agent Platform、MVP = ADR-0020 (full local content retention、ADR-0005 supersede) + ADR-0021 (encryption at rest、SQLCipher + keyring) + ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) + ADR-0004 改訂 (形A) + ADR-0016 改訂 (reply_draft) + ADR-0017 改訂 (reply_draft link types) + ADR-0010 改訂 (write-back 明示禁止) + 本文ベース embedding (migration 0018) + SQLite FTS5 (migration 0019) + `opshub search` + `opshub mcp serve` + 秘書 5 Skills + `tools/skill_scan.py`) complete (2026-05-31) + **Phase 11 (MS Office 深掘り、MVP = ADR-0025 (Office Document Content Extraction、markitdown 経路 + 50 MB / 500K chars cap + source_type 3 種 `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` + fail-safe) + ADR-0019 改訂 (`content_extraction = true` opt-in 例外節 + `onedrive_drive` パターン汎化、§決定 (b') + (j)) + ADR-0010 改訂 (Teams connector 追加 + 本文抽出契約 + delta-link cursor + 失効時 full-pass fallback + Teams User Token principal、§改訂 (a)/(b)/(c)/(d)) + `src/opshub/core/document_extract.py` + `connectors/teams/` (Microsoft Graph chat delta + User Token) + `connectors/onedrive_drive/` (FS scan、WSL2 `/mnt/onedrive` / macOS `~/OneDrive` platform default) + `connectors/box_drive` の Office 抽出 hook + `connectors/ms365/mapper` の Outlook body deep retention、epic #233) complete (2026-05-31)** — event store、全 projection (tasks / inbox_items / decisions / work_sessions / agent_runs / locks / handoffs / sources (Phase 10 で `body` + `provenance_origin` + `provenance_trust` 追加、Phase 11 で `teams_message` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` source_type 追加) / connector_cursors / ingested_files / embeddings / briefings / proposals (Phase 10 で `reply_draft` candidate kind 追加) / links (Phase 10 で `reply_draft_replies_to` / `referenced_in_reply_draft` link types 追加))、CLI (`init` / `task` / `inbox` / `decision` / `lock` / `session` / `agent run` / `handoff` / `connector auth set` / `connector sync` / `connector list` / `workspace ingest` / `workspace generate` / `projections rebuild` / `embeddings rebuild` / `embeddings drain` / `embeddings status` / `embeddings find-duplicates` / `recall` / `search` (Phase 10) / `brief` / `propose generate` (Phase 10 で `--reply-to` 対応) / `propose list` / `propose apply` / `propose reject` / `link add` / `link remove` / `link list` / `graph related` / `graph trace` / `graph expand` / `mcp serve` (Phase 10) / `mcp tools` (Phase 10) / `db migrate`)、markdown 生成、**7 connector** (GitHub + Slack + Microsoft 365 + Box + Box Drive (FS scan) + **Teams** (Phase 11、Microsoft Graph chat delta) + **OneDrive Drive** (Phase 11、FS scan)) で `source_type` discriminator 別に `sources` projection に persist (Phase 10 で本文 + provenance 取り込みに拡張、`box_drive` / `onedrive_drive` は ADR-0019 §不変条件 (b) で default `body=None`、Phase 11 で `content_extraction = true` opt-in 時のみ markitdown 経由で Office 文書 本文取り込み)、workspace inbox file ingest、Pluggable Embedder + sqlite-vec backed VectorStore、Pluggable LLMClient (Anthropic + OpenAI + Ollama) + structured output + Briefing 自動生成 + Proposal domain (human-in-the-loop apply、Phase 10 で reply_draft mode 追加) + Knowledge graph layer + event-driven auto-embed + MCP server (stdio one transport、policy-as-data registry、Phase 10 sub C) + 秘書 5 Skill (`docs/skills/<name>/SKILL.md` を SSOT として `ozzy-labs/skills` 経由で配布、Phase 10 sub D) + 保存時暗号化 (`[storage] encryption = true` で opt-in、SQLCipher AES-256、Phase 10 sub A、ADR-0021) が end-to-end で動作。次の候補は Phase 12+ (multi-machine sync / 能動性段階 1-4 = cron 委譲 / 記憶キュレーション / 通知 / filewatch / 画像 OCR / 追加コネクタ Google Workspace / Notion / Jira / 外部書き戻し = Teams 返信送信 + HITL、詳細は `docs/principles.md` §9)。

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

秘書 5 Skill（personal-brief / next-actions / reply-draft / pr-review / find-document、Phase 12 H1 で rename 済）の catalog は [`docs/secretary-agent.md`](docs/secretary-agent.md) を参照。Codex / Copilot CLI も MCP 経由で同じ surface を叩ける。
