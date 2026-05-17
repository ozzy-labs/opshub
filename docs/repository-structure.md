# Repository and Package Structure

> Status: Draft (in active design). Last reviewed: 2026-05-17.

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
├── workspace/                      # Phase 2 以降で実装予定 (現状未存在)
│   └── _template/                  # 実 workspace の seed (実体は repo 外)、Phase 2 以降で実装
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

各エントリの末尾にある `[P1]` / `[P1+2]` / `[P1+2+3]` / `[P1+2+3+4]` / `[P1+2+3+4+5]` / `[P1+2+3+4+5+6]` / `[P2]` / `[P3]` / `[P3.x]` / `[P4]` / `[P5]` / `[P5.x]` / `[P6]` / `[P6.x]` / `[P7]` / `[P7.x]` / `[future]` は実装が入る (or 入った) Phase を示す。`[P1]` は Phase 1 で、`[P1+2]` は Phase 2 までで、`[P1+2+3]` は Phase 3 までで、`[P1+2+3+4]` は Phase 4 までで、`[P1+2+3+4+5]` / `[P5]` は Phase 5 までで、`[P1+2+3+4+5+6]` / `[P6]` は Phase 6 までで、`[P7]` は Phase 7 (Connectors Wave 2、Slack + Microsoft 365 + Box) までで merge 済 (2026-05-17)。`[P6.x]` は Phase 6 完了後の継続作業 (`llama.cpp` direct binding / proposal scoring / `links` projection 本実装 / multi-machine sync 等)、`[P7.x]` は Phase 7 完了後の継続作業 (additional connectors / common OAuth helper refactor / connector observability 等)、`[future]` は Phase 8 (Knowledge graph、epic #128) 以降。

```text
src/opshub/
├── __init__.py                     # [P1]
├── __main__.py                     # `python -m opshub` エントリ [P1]
├── cli/                            # Typer command 群
│   ├── __init__.py                 # [P1]
│   ├── app.py                      # Typer app root [P1]
│   ├── init.py                     # [P1]
│   ├── db.py                       # migrate / status [P1]
│   ├── task.py                     # create / list / status / archive [P1]
│   ├── projections.py              # rebuild [P1]
│   ├── embeddings.py               # rebuild / drain / status / find-duplicates [P1+2+3+4+5]
│   ├── recall.py                   # semantic search [P1+2+3+4]
│   ├── brief.py                    # LLM-backed briefing CLI [P5]
│   ├── propose.py                  # LLM-backed proposal CLI (generate / list / apply / reject) [P6]
│   ├── _wiring.py                  # 内部 helper: service/projection の組み立て (briefing / proposal / auto-embed hook 含む) [P1+2+5+6]
│   ├── _task_list.py               # 内部 helper: task list 共通フォーマッタ [P1]
│   ├── _actor.py                   # 内部 helper: actor / work_session_id 解決 [P1+2]
│   ├── _render.py                  # 内部 helper: 汎用 table/json/md renderer + briefing / proposal renderer [P1+2+5+6]
│   ├── _inbox_list.py              # 内部 helper: inbox list 共通フォーマッタ [P1+2]
│   ├── _decision_list.py           # 内部 helper: decision list 共通フォーマッタ [P1+2]
│   ├── _handoff_render.py          # 内部 helper: handoff list 共通フォーマッタ [P1+2]
│   ├── inbox.py                    # add / list / triage [P1+2]
│   ├── decision.py                 # record / list [P1+2]
│   ├── lock.py                     # acquire / release / list [P1+2]
│   ├── session.py                  # work session start / end / list [P1+2]
│   ├── agent.py                    # agent run begin / end [P1+2]
│   ├── handoff.py                  # open / close / list [P1+2]
│   ├── workspace.py                # generate / ingest [P1+2+3]
│   ├── event.py                    # event append / list [future]
│   ├── source.py                   # source add / list [future]
│   └── connector.py                # list / sync / auth set (Phase 5 で `llm:<name>` 名前空間にも対応) [P1+2+3+5]
├── core/                           # 共通ユーティリティ [P1]
│   ├── config.py                   # Pydantic Settings (Phase 5 で LLMSettings 追加、Phase 6 で OllamaLLMSettings + `ollama` backend literal 追加) [P1+2+3+4+5+6]
│   ├── ids.py                      # ULID / UUID [P1]
│   ├── time.py                     # tz-aware datetime helpers [P1]
│   ├── logging.py                  # structlog [P1]
│   ├── secrets.py                  # keyring-backed token storage (ADR-0014) [P1+2+3]
│   ├── sanitise.py                 # API key / Bearer token 除去 (Phase 5 で extract) [P5]
│   ├── slug.py                     # filename-safe slug for briefings/--save [P5]
│   └── errors.py                   # [P1]
├── db/                             # 永続化レイヤ [P1]
│   ├── engine.py                   # SQLAlchemy Engine / Session [P1]
│   ├── schema.py                   # Core Table 定義 [P1]
│   ├── unit_of_work.py             # [P1]
│   └── migrations/                 # Alembic env.py 等 [P1]
├── domain/                         # event / aggregate / value object
│   ├── events/
│   │   ├── base.py                 # DomainEvent 抽象 [P1]
│   │   ├── task.py                 # [P1]
│   │   ├── inbox.py                # [P1+2]
│   │   ├── decision.py             # [P1+2]
│   │   ├── coordination.py         # work_session / agent_run / lock [P1+2]
│   │   ├── handoff.py              # [P1+2]
│   │   ├── source.py               # SourceObserved / SourceReferenced [P1+2+3]
│   │   ├── connector.py            # ConnectorSyncStarted / Completed / Failed [P1+2+3]
│   │   ├── file_ingest.py          # FileIngested [P1+2+3]
│   │   ├── embedding.py            # TextEmbedded / EmbeddingRebuildRequested / EmbeddingFailed [P1+2+3+4]
│   │   ├── briefing.py             # BriefingRequested / BriefingGenerated / BriefingFailed [P5]
│   │   ├── proposal.py             # ProposalRequested / ProposalGenerated / ProposalApplied / ProposalRejected / ProposalFailed + Candidate discriminated union [P6]
│   │   └── agent.py                # [future]
│   ├── ids.py                      # TaskId / SourceId など [P1]
│   └── value_objects.py            # [P1]
├── services/                       # application services
│   ├── task_service.py             # [P1]
│   ├── event_store.py              # EventStore Protocol + SQLAlchemy 実装 [P1]
│   ├── projector.py                # Projector Protocol + fan-out 実装 [P1]
│   ├── inbox_service.py            # [P1+2]
│   ├── decision_service.py         # [P1+2]
│   ├── lock_service.py             # [P1+2]
│   ├── work_session_service.py     # [P1+2]
│   ├── agent_run_service.py        # [P1+2]
│   ├── handoff_service.py          # [P1+2]
│   ├── workspace_service.py        # [P2+]
│   ├── source_service.py           # connector → source/inbox event chain [P1+2+3]
│   ├── file_ingest_service.py      # workspace/inbox/*.md → event + ingested_files [P1+2+3]
│   ├── embedding_service.py        # CLI-driven embed pending entities + `embed_one_if_pending` (P5 で sanitise extract + auto-embed hook 用 single-row API 追加) [P1+2+3+4+5]
│   ├── recall_service.py           # vector + SQL filter hybrid search [P1+2+3+4]
│   ├── duplicate_service.py        # offline near-duplicate scan [P1+2+3+4]
│   ├── briefings/                  # BriefingService + prompts (ADR-0015) [P5]
│   │   ├── __init__.py             # [P5]
│   │   ├── prompts.py              # SYSTEM_PROMPT / USER_PROMPT_TEMPLATE / render_user_prompt [P5]
│   │   └── service.py              # BriefingService.generate(topic, ...) [P5]
│   ├── proposals/                  # ProposalService + prompts (ADR-0016) [P6]
│   │   ├── __init__.py             # [P6]
│   │   ├── prompts.py              # SYSTEM_PROMPT / render_user_prompt (briefing-seed + delimiter wrap) [P6]
│   │   └── service.py              # ProposalService.generate / apply / reject [P6]
│   ├── auto_embed_hook.py          # post-commit projector hook for opt-in auto-embed [P5]
│   └── event_hook.py               # EventHook Protocol (post-commit fan-out) [P5]
├── projections/                    # event → projection reducer
│   ├── base.py                     # [P1]
│   ├── registry.py                 # 一元化された projection 一覧 [P1+2+3]
│   ├── tasks.py                    # [P1]
│   ├── rebuild.py                  # [P1]
│   ├── inbox.py                    # [P1+2]
│   ├── decisions.py                # [P1+2]
│   ├── work_sessions.py            # [P1+2]
│   ├── agent_runs.py               # [P1+2]
│   ├── locks.py                    # [P1+2]
│   ├── handoffs.py                 # [P1+2]
│   ├── sources.py                  # external source 現在状態 [P1+2+3]
│   ├── connector_cursors.py        # connector 差分同期 cursor [P1+2+3]
│   ├── ingested_files.py           # workspace file ingest の content_hash 追跡 [P1+2+3]
│   ├── briefings.py                # LLM briefing 結果 (markdown + source_refs + cost trace、Phase 5 で実装済) [P5]
│   ├── proposals.py                # LLM proposal candidates + per-candidate state (pending/applied/rejected、ADR-0016) [P6]
│   └── links.py                    # entity 間 graph 関係 [P6.x]
├── connectors/                     # [P1+2+3]
│   ├── __init__.py                 # discover_connectors / register_connector [P1+2+3]
│   ├── base.py                     # Connector Protocol + SyncResult [P1+2+3]
│   ├── context.py                  # ConnectorContext dataclass [P1+2+3]
│   ├── _registry.py                # 内部: in-process registry [P1+2+3]
│   ├── github/                     # GitHub connector (MVP) [P1+2+3]
│   │   ├── __init__.py             # register_connector(GitHubConnector()) side effect [P1+2+3]
│   │   ├── auth.py                 # PAT 解決 (env / keyring) [P1+2+3]
│   │   ├── api.py                  # httpx fetch primitives [P1+2+3]
│   │   └── connector.py            # GitHubConnector(sync) [P1+2+3]
│   ├── slack/                      # Slack connector (Phase 7 A1-A3) [P7]
│   │   ├── __init__.py             # register_connector(SlackConnector()) side effect [P7]
│   │   ├── auth.py                 # bot token 解決 (env / keyring) [P7]
│   │   ├── fetcher.py              # slack_sdk WebClient + cursor pagination [P7]
│   │   ├── mapper.py               # RawSlackMessage → SourceObserved (source_type=slack_message) [P7]
│   │   └── connector.py            # SlackConnector(sync) [P7]
│   ├── ms365/                      # Microsoft 365 connector (Phase 7 B1-B3) [P7]
│   │   ├── __init__.py             # register_connector(MS365Connector()) side effect [P7]
│   │   ├── auth.py                 # msal paste-code OAuth + refresh-token keyring 保管 [P7]
│   │   ├── fetcher.py              # httpx Graph client + 3 endpoint cursors (calendar / onedrive / outlook) [P7]
│   │   ├── mapper.py               # 3 source_type mappers (ms365_calendar / ms365_onedrive / ms365_outlook) [P7]
│   │   └── connector.py            # MS365Connector(sync) with per-endpoint isolation [P7]
│   └── box/                        # Box connector (Phase 7 C1-C3) [P7]
│       ├── __init__.py             # register_connector(BoxConnector()) side effect [P7]
│       ├── auth.py                 # boxsdk OAuth2 + refresh-token keyring 保管 [P7]
│       ├── fetcher.py              # Box Events API + stream_position cursor [P7]
│       ├── mapper.py               # RawBoxEvent → SourceObserved (source_type=box_event) [P7]
│       └── connector.py            # BoxConnector(sync) [P7]
├── markdown/                       # workspace surface 生成 + ingest parser
│   ├── render/                     # Jinja2 テンプレート [P1]
│   ├── templates/                  # Jinja2 ファイル (per-renderer) [P1+2]
│   ├── ingest.py                   # parse_inbox_file / compute_file_hash [P1+2+3]
│   ├── tasks.py                    # [P1]
│   ├── workspace.py                # workspace 全体生成 [P1+2]
│   ├── briefings.py                # [P5]
│   ├── reviews.py                  # [P5]
│   ├── inbox.py                    # [P1+2]
│   ├── decisions.py                # [P1+2]
│   └── handoffs.py                 # [P1+2]
├── vectors/                        # 抽象 interface [P1] + 具象 backend [P1+2+3+4]
│   ├── embedder.py                 # Embedder Protocol [P1]
│   ├── store.py                    # VectorStore Protocol [P1]
│   ├── local_embedder.py           # LocalSentenceTransformerEmbedder (bge-m3) [P1+2+3+4]
│   ├── openai_embedder.py          # OpenAIEmbedder (text-embedding-3-small) [P1+2+3+4]
│   ├── voyage_embedder.py          # VoyageEmbedder (voyage-3) [P1+2+3+4]
│   ├── sqlite_vec_store.py         # SqliteVecStore (sqlite-vec backed VectorStore) [P1+2+3+4]
│   └── factory.py                  # backend resolution (build_embedder / build_vector_store) [P1+2+3+4]
├── llm/                            # LLM 抽象 + 具象 backend (ADR-0015 + ADR-0016) [P5+6]
│   ├── __init__.py                 # [P5]
│   ├── client.py                   # LLMMessage / LLMResponse / LLMClient Protocol + StructuredResponse (Phase 6 で complete_structured 追加) [P5+6]
│   ├── schema.py                   # pydantic_to_tool_schema helper (SSOT、ADR-0016 §決定 (b)) [P6]
│   ├── anthropic_client.py         # AnthropicLLMClient (claude-haiku-4-5 default、Phase 6 で tool_use structured output 拡張) [P5+6]
│   ├── openai_client.py            # OpenAILLMClient (gpt-4o-mini default、Phase 6 で tools= structured output 拡張) [P5+6]
│   ├── ollama_client.py            # OllamaLLMClient (local daemon、OpenAI 互換 endpoint、ADR-0016 §決定 (h)) [P6]
│   └── factory.py                  # build_llm_client + NoOpLLMClient (Phase 6 で `ollama` branch + NoOp.complete_structured 追加) [P5+6]
├── graph/                          # entity 間 link [Phase 4 以降で検討]
│   ├── links.py
│   └── queries.py
├── runtime/                        # 現時点で計画なし (将来検討、`services/` に統合する案あり)
│   ├── locks.py
│   ├── work_session.py
│   └── handoff.py
└── agents/                         # 現時点で計画なし (将来検討、MCP 経路を採るか未確定)
    ├── prompts.py
    ├── boundary.py
    └── mcp_server.py
```

## 3. モジュール責務の鉄則

1. **`cli/` はビジネスロジックを書かない**。Typer 引数を `services/` に渡すだけ。
2. **`services/` は必ず event を append してから projection を更新する**。projection 直書き禁止。
3. **`connectors/` は SQL を直接叩かない**。必ず `services/` 経由で event 化。
4. **`markdown/` は read-only**。projection を読んで render するだけ。書き込み禁止。
5. **`domain/events/` の event 型は immutable, versioned**。`schema_version` フィールド必須。
6. **`core/` は他のモジュールに依存しない**。逆依存防止。

> これらは現状コードレビューで担保。CI による機械的強制 (import-linter / カスタム ruff rule) は Phase 2.x で検討する。

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
