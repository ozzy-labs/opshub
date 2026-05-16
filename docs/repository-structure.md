# Repository and Package Structure

> Status: Draft (in active design). Last reviewed: 2026-05-16.

OpsHub リポジトリのトップレベル構成と Python パッケージ (`src/opshub/`) の内部構造を記述する。実装着手時はこの構成から開始し、必要に応じて更新する。

## 1. トップレベル構成

```text
opshub/
├── README.md                       # 概要・ドキュメント index・status
├── LICENSE                         # MIT
├── AGENTS.md                       # 4 CLI 共通指示 (multi-agent-repo 準拠、未作成)
├── CLAUDE.md                       # Claude 固有差分 (未作成)
├── .gitignore                      # Python 用 (現在は Node 用、要置換)
├── .mise.toml                      # Python 3.13 / uv / just のバージョン固定
├── pyproject.toml                  # PEP 621 + uv + ruff + mypy + pytest 設定
├── uv.lock                         # commit する
├── justfile                        # task runner
├── lefthook.yaml                   # commitlint + ruff + mypy + gitleaks
├── commitlint.config.mjs           # Conventional Commits
├── .python-version                 # 3.13
├── .devcontainer/
│   └── devcontainer.json
├── .agents/skills/                 # 4 CLI 共通スキル (morning-brief, triage 等)
├── .claude/                        # Claude 固有: settings / skills / commands
│   └── settings.json
├── .codex/config.toml
├── .gemini/settings.json
├── .github/
│   ├── workflows/
│   │   ├── ci.yaml                 # lint + type + test
│   │   ├── release-please.yaml
│   │   └── gitleaks.yaml
│   ├── renovate.json
│   ├── agents/                     # Copilot CLI 用 (将来)
│   └── copilot-instructions.md
├── docs/
│   ├── principles.md
│   ├── architecture.md
│   ├── repository-structure.md     # 本ドキュメント
│   ├── data-model.md               # 未作成
│   ├── concepts/                   # 未作成
│   │   ├── operational-memory.md
│   │   ├── connectors.md
│   │   └── workspace.md
│   ├── adr/                        # Architecture Decision Records
│   └── runbook/                    # 未作成
├── src/
│   └── opshub/                     # 項目 2 で詳細
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── migrations/                     # Alembic
│   └── versions/
├── prompts/                        # 内蔵プロンプト
│   ├── triage.md
│   ├── summarize_thread.md
│   └── extract_action_items.md
├── workspace/
│   └── _template/                  # 実 workspace の seed (実体は repo 外)
└── scripts/
    └── dev/                        # 開発補助シェル
```

### 1.1 Repo 外に置くもの

個人データを repo に含めないため、以下は **repo 外** に置く。

| 種別 | 推奨パス |
|---|---|
| SQLite DB (event store + projections) | `~/.local/share/opshub/db/opshub.sqlite` |
| 実 workspace | `~/opshub/workspace/` |
| 短期 cache (SaaS body 等) | `~/.cache/opshub/` |
| SaaS token | OS keychain (`keyring` library 経由) |

これらのパスはユーザー設定で上書き可能。

## 2. Python パッケージ構成 (src/opshub/)

```text
src/opshub/
├── __init__.py
├── __main__.py                     # `python -m opshub` エントリ
├── cli/                            # Typer command 群
│   ├── __init__.py
│   ├── app.py                      # Typer app root
│   ├── init.py
│   ├── db.py                       # migrate / status
│   ├── event.py                    # event append / list
│   ├── task.py                     # create / list / status / archive
│   ├── source.py                   # source add / list
│   ├── inbox.py                    # list / triage
│   ├── workspace.py                # generate
│   ├── projections.py              # rebuild
│   ├── lock.py                     # acquire / release / list
│   ├── agent.py                    # session start / end
│   └── connector.py                # sync / status (Phase 3+)
├── core/                           # 共通ユーティリティ
│   ├── config.py                   # Pydantic Settings
│   ├── ids.py                      # ULID / UUID
│   ├── time.py                     # tz-aware datetime helpers
│   ├── logging.py                  # structlog
│   └── errors.py
├── db/                             # 永続化レイヤ
│   ├── engine.py                   # SQLAlchemy Engine / Session
│   ├── schema.py                   # Core Table 定義
│   ├── unit_of_work.py
│   └── migrations/                 # Alembic env.py 等
├── domain/                         # event / aggregate / value object
│   ├── events/
│   │   ├── base.py                 # DomainEvent 抽象
│   │   ├── task.py
│   │   ├── source.py
│   │   ├── decision.py
│   │   ├── inbox.py
│   │   └── agent.py
│   ├── ids.py                      # TaskId / SourceId など
│   └── value_objects.py
├── services/                       # application services
│   ├── task_service.py
│   ├── source_service.py
│   ├── inbox_service.py
│   ├── decision_service.py
│   ├── lock_service.py
│   ├── agent_session_service.py
│   └── workspace_service.py
├── projections/                    # event → projection reducer
│   ├── base.py
│   ├── tasks.py
│   ├── sources.py
│   ├── inbox.py
│   ├── decisions.py
│   ├── links.py
│   └── rebuild.py
├── connectors/                     # Phase 3+
│   ├── base.py                     # Connector 抽象
│   ├── github/
│   ├── slack/
│   ├── msgraph/
│   └── box/
├── markdown/                       # workspace surface 生成
│   ├── render/                     # Jinja2 テンプレート
│   ├── tasks.py
│   ├── briefings.py
│   ├── reviews.py
│   └── handoffs.py
├── graph/                          # entity 間 link
│   ├── links.py
│   └── queries.py
├── vectors/                        # Phase 4+
│   ├── embedder.py
│   ├── store.py                    # sqlite-vec
│   └── recall.py
├── runtime/                        # multi-agent coordination
│   ├── locks.py
│   ├── work_session.py
│   └── handoff.py
└── agents/                         # agent runtime helpers
    ├── prompts.py
    ├── boundary.py
    └── mcp_server.py               # 任意、Phase 4+ で追加
```

## 3. モジュール責務の鉄則

1. **`cli/` はビジネスロジックを書かない**。Typer 引数を `services/` に渡すだけ。
2. **`services/` は必ず event を append してから projection を更新する**。projection 直書き禁止。
3. **`connectors/` は SQL を直接叩かない**。必ず `services/` 経由で event 化。
4. **`markdown/` は read-only**。projection を読んで render するだけ。書き込み禁止。
5. **`domain/events/` の event 型は immutable, versioned**。`schema_version` フィールド必須。
6. **`core/` は他のモジュールに依存しない**。逆依存防止。

→ [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md)

## 4. パッケージング方針

現状: **single Python package (`src/opshub/`)** で開始する。

将来 connector が増えた段階で `uv workspace` に分割を検討:

- `packages/opshub-core/`
- `packages/opshub-cli/`
- `packages/opshub-connector-github/`
- `packages/opshub-connector-slack/`
- ...

→ [ADR-0007: Single Python Package, defer Monorepo](adr/0007-single-python-package.md)

## 5. ファイル命名・配置の慣習

1. **モジュール名は `snake_case`**。Python 標準。
2. **テストは `tests/unit/<モジュール>/test_*.py`** ミラー構造。
3. **ADR は `docs/adr/NNNN-kebab-case.md`** (4 桁番号、小文字、ハイフン)。
4. **YAML ファイルは `.yaml` 拡張子**で統一 (ツールが `.yml` 必須の場合のみ例外)。
5. **prompt ファイルは `prompts/<scope>_<intent>.md`** (例: `triage.md` / `summarize_thread.md`)。i18n が必要になったら `triage.ja.md` のような兄弟ファイル。
6. **devcontainer / mise / lefthook 等の設定はリポジトリ root**。サブディレクトリには置かない (発見性のため)。

## Open Questions

1. `workspace/_template/` の中身 (初期 seed として何を入れるか)
2. `prompts/` の i18n (英語のみで開始するか、日本語版も同梱するか)
3. `docs/concepts/*` を必要とするか、`architecture.md` に統合するか
4. `docs/runbook/` の運用 (incident playbook 系をどこまで書くか)
5. `migrations/` を `db/migrations/` の下に置くか、root 直下に置くか
