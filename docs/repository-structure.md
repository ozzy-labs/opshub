# Repository and Package Structure

> Status: Draft (in active design). Last reviewed: 2026-05-31.

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
│   ├── assistant-agent.md          # Phase 10: アシスタントエージェント層 (形A) の使い方
│   ├── mcp-setup.md                # Phase 10: エージェント host から MCP 経由で叩く手順
│   ├── skills/                     # Phase 10: アシスタント 5 Skill SSOT → Phase 12 で 14 Skill 体制に拡張 (rename 2: daily-brief → personal-brief / file-lookup → find-document、新規 9: meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract、Phase 12 時点では `ozzy-labs/skills` 配布機構を Phase 15+ defer = ADR-0004 §決定 (c) backout として運用 → Phase 16-A で opshub package 同梱経路に切り替え確定 (ADR-0029)、Phase 16-B で `src/opshub/_skills/` build-time copy + `opshub skills install` CLI 着地、Phase 16-D で in-repo dogfood = `.claude/skills/<assistant>/` + `.agents/skills/<assistant>/` populate) [P10, P12, P16]
│   │   ├── personal-brief/SKILL.md         # rename from daily-brief (P12 H1)
│   │   ├── next-actions/SKILL.md
│   │   ├── reply-draft/SKILL.md
│   │   ├── pr-review/SKILL.md
│   │   ├── find-document/SKILL.md          # rename from file-lookup (P12 H1)
│   │   ├── meeting-prep/SKILL.md           # P12 H2 新規 (info gathering)
│   │   ├── research/SKILL.md               # P12 H2 新規 (info gathering)
│   │   ├── external-brief/SKILL.md         # P12 H3 新規 (analysis、pair = personal-brief)
│   │   ├── decision-rationale/SKILL.md     # P12 H3 新規 (analysis)
│   │   ├── inbox-triage/SKILL.md           # P12 H4 新規 (HITL write、pair = source-extract)
│   │   ├── source-extract/SKILL.md         # P12 H4 新規 (HITL write、pair = inbox-triage)
│   │   ├── meeting-followup/SKILL.md       # P12 H4 新規 (HITL write、pair = meeting-prep)
│   │   ├── handoff-draft/SKILL.md          # P12 H5 新規 (draft family、text-only)
│   │   └── announcement-draft/SKILL.md     # P12 H5 新規 (draft family、text-only)
│   └── runbook/                    # 未作成
├── tools/                          # Phase 10: skill security scan (4 カテゴリ + frontmatter 隠しユニコード検出)、Phase 12 で 14 skills 全てに対する per-skill MCP dispatch pin + scan を実行 [P10, P12]
│   └── skill_scan.py
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

各エントリの末尾にある `[P1]` / `[P1+2]` / `[P1+2+3]` / `[P1+2+3+4]` / `[P1+2+3+4+5]` / `[P1+2+3+4+5+6]` / `[P2]` / `[P3]` / `[P3.x]` / `[P4]` / `[P5]` / `[P5.x]` / `[P5+8]` / `[P6]` / `[P6+8]` / `[P6.x]` / `[P7]` / `[P7.x]` / `[P7+11]` / `[P8]` / `[P8.x]` / `[P9]` / `[P9+11]` / `[P9.x]` / `[P10]` / `[P10+12]` / `[P11]` / `[P11+13]` / `[P12]` / `[P13]` / `[P13+14]` / `[P14]` / `[future]` は実装が入る (or 入った) Phase を示す。`[P1]` は Phase 1 で、`[P1+2]` は Phase 2 までで、`[P1+2+3]` は Phase 3 までで、`[P1+2+3+4]` は Phase 4 までで、`[P1+2+3+4+5]` / `[P5]` は Phase 5 までで、`[P1+2+3+4+5+6]` / `[P6]` は Phase 6 までで、`[P7]` は Phase 7 (Connectors Wave 2、Slack + Microsoft 365 + Box) で、`[P8]` は Phase 8 (Knowledge graph layer、ADR-0017 + `links` projection + 4 自動抽出 + manual link CRUD + `LinkService` traversal + `opshub link` / `opshub graph` CLI + `--expand-graph` integration) で merge 済 (2026-05-17)、`[P9]` は Phase 9 (Local-filesystem-backed Connector Layer、ADR-0019 + `sources.fingerprint` 列 (migration 0017) + `box_drive` connector + `core/platform.py` + `opshub connector sync box_drive`) で merge 済 (2026-05-23)、`[P10]` は Phase 10 (Assistant Agent Platform、ADR-0020 + ADR-0021 + ADR-0022 + ADR-0004/0016/0017/0010 改訂、本文ベース embedding + SQLite FTS5 + `opshub search` + `opshub mcp serve` + アシスタント 5 Skills) で merge 済 (2026-05-31)、`[P11]` は Phase 11 (MS Office 深掘り、ADR-0025 + ADR-0019 改訂 + ADR-0010 改訂、`core/document_extract.py` + `connectors/teams/` + `connectors/onedrive_drive/` + `connectors/box_drive` Office 抽出 hook + `connectors/ms365/mapper` outlook body deep retention) で merge 済 (2026-05-31)、`[P12]` は Phase 12 (Assistant Skills 拡張、ADR-0004 改訂 + ADR-0022 改訂 + ADR-0016 改訂、14 skills 体制 (新規 9 + rename 2) + 4 新 MCP tools (`search` (FTS5) + `propose.apply` (HITL idempotent) + 既存 4 read tools の physical column ベース時間フィルタ) + 既存 5 SKILL.md MCP 直接呼び化 + `propose.generate` の `mode` 引数追加) で merge 済 (2026-05-31)、`[P13]` は Phase 13 (Google Workspace コネクタ、ADR-0010 改訂 + ADR-0014 改訂 + ADR-0025 改訂、`connectors/google_workspace/` 5 module 構成 + `core/document_extract.extract_workspace_export(bytes, source_type)` + `[connectors-google-workspace]` extras) で merge 済 (2026-05-31)、`[P14]` は Phase 14 (Gmail + Google Calendar コネクタ、ADR-0010 改訂 + ADR-0014 改訂、`connectors/google_auth/` shared auth foundation + `connectors/google_mail/` 5 module 構成 (`gmail_message` source_type) + `connectors/google_calendar/` 5 module 構成 (`google_calendar` source_type) + mapper symmetry pin test、新 extras なし = `[connectors-google-workspace]` 共有) で merge 済 (2026-05-31)。複合 tag (例: `[P5+8]` / `[P6+8]` / `[P1+2+3+9]` / `[P6+10]` / `[P7+11]` / `[P9+11]` / `[P10+12]` / `[P11+13]` / `[P13+14]`) は当該 Phase で初出 + 後続 Phase で拡張が入った module を示す。`[P6.x]` は Phase 6 完了後の継続作業、`[P7.x]` は Phase 7 完了後、`[P8.x]` は Phase 8 完了後、`[P9.x]` は Phase 9 完了後、`[future]` は Phase 16+ (multi-machine sync / 能動性段階 1-4 / Gmail push / Calendar push 再評価 / 画像 OCR (Phase 13 → 14 から繰り越し) / Drive Comments + Suggestions (Phase 13 から繰り越し) / Gmail + Calendar 添付の本文抽出 (markitdown 経路) / 追加コネクタ Notion / Jira / Linear / Confluence (Phase 13 から繰り越し) / 外部書き戻し (Teams 返信送信 / Gmail send / Calendar event create + HITL) / Calendar instance 展開 projection / 形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab、Phase 15 から繰り越し)) 以降 (アシスタント 14 Skill の配信経路は Phase 16-A で opshub Python package 同梱 + `opshub skills install` に切り替え済、ADR-0029)。

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
│   ├── brief.py                    # LLM-backed briefing CLI (Phase 8 で graph 拡張 hook 化、epic #470 で `--expand-graph` flag 廃止 + 常時実行) [P5+8]
│   ├── propose.py                  # LLM-backed proposal CLI (generate / list / apply / reject、Phase 8 で graph 拡張 hook 化、epic #470 で `--expand-graph` flag 廃止 + 常時実行) [P6+8]
│   ├── link.py                     # Manual link CRUD (add / remove / list) [P8]
│   ├── graph.py                    # Graph traversal queries (related / trace / expand、--format md/json/dot) [P8]
│   ├── person.py                   # 人軸 identity CRUD (list / merge / split、ADR-0043) [P25]
│   ├── _wiring.py                  # 内部 helper: service/projection の組み立て (briefing / proposal / auto-embed hook / link / person service 含む) [P1+2+5+6+8+25]
│   ├── _task_list.py               # 内部 helper: task list 共通フォーマッタ [P1]
│   ├── _actor.py                   # 内部 helper: actor / work_session_id 解決 [P1+2]
│   ├── _render.py                  # 内部 helper: 汎用 table/json/md renderer + briefing / proposal / link list / link paths / graph subset renderer (md / json / dot) [P1+2+5+6+8]
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
│   ├── connector.py                # list / sync / auth set (Phase 5 で `llm:<name>` 名前空間にも対応、Phase 13 で `google_workspace` target を paste-code OAuth flow に dispatch する分岐を追加) [P1+2+3+5+13]
│   ├── _google_workspace_oauth.py  # 内部 helper: Google OAuth paste-code flow (`connector auth set google_workspace` の dispatch 先、`opshub.toml [connectors.google_workspace] client_id` / `client_secret` を読んで :class:`GoogleWorkspaceAuth` の `start_auth_flow` → `complete_auth_flow` を駆動。MS365 / Box の paste-code flow と対称) [P13]
│   ├── search.py                   # `opshub search` — FTS5 horizontal full-text search (Phase 10 step B2、ADR-0012 改訂 §4) [P10]
│   └── mcp.py                      # `opshub mcp serve` / `opshub mcp tools` (Phase 10 sub C、ADR-0022) [P10]
├── core/                           # 共通ユーティリティ [P1]
│   ├── config.py                   # Pydantic Settings (Phase 5 で LLMSettings 追加、Phase 6 で OllamaLLMSettings + `ollama` backend literal 追加、Phase 7 で ConnectorSettings + per-connector settings 追加、Phase 9 で BoxDriveConnectorSettings 追加、Phase 10 で StorageSettings.encryption 追加、Phase 11 で TeamsConnectorSettings / OneDriveDriveConnectorSettings / OfficeSettings 追加、Phase 13 で GoogleWorkspaceConnectorSettings (enabled / client_id / client_secret / redirect_uri / content_extraction) 追加) [P1+2+3+4+5+6+7+9+10+11+13]
│   ├── ids.py                      # ULID / UUID [P1]
│   ├── time.py                     # tz-aware datetime helpers [P1]
│   ├── platform.py                 # WSL2 / macOS / Linux 判定 helper (ADR-0019 §決定 (f)、`detect_platform()` / `box_drive_default_root_path()`) [P9]
│   ├── logging.py                  # structlog [P1]
│   ├── secrets.py                  # keyring-backed token storage (ADR-0014) [P1+2+3]
│   ├── sanitise.py                 # API key / Bearer token 除去 (Phase 5 で extract) [P5]
│   ├── slug.py                     # filename-safe slug for briefings/--save [P5]
│   ├── encryption.py               # SQLCipher key resolver (ADR-0021、`require_db_key()` / `OPSHUB_DB_ENCRYPTION_KEY` env override + keyring) [P10]
│   ├── excludes.py                 # 取り込み除外設定パーサ (ADR-0020 §(b)、`~/.config/opshub/excludes.yaml` を channel / sender / repo / path で評価) [P10]
│   ├── document_extract.py         # Office 文書本文抽出 (ADR-0025、markitdown 経路、`extract_document(path)` + `ExtractResult` + `SOURCE_TYPE_BY_EXTENSION` SSOT、50 MB / 500K chars cap、fail-safe。Phase 13 G2 で API 表面拡張: `extract_workspace_export(bytes, source_type)` + `GOOGLE_DOC_SOURCE_TYPE` / `GOOGLE_SLIDES_SOURCE_TYPE` / `GOOGLE_SHEETS_SOURCE_TYPE` + `GOOGLE_WORKSPACE_SOURCE_TYPES` tuple + `GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE` lookup を追加、Phase 11 path-only と backward-compat、ADR-0025 §決定 (d') + (j)) [P11+13]
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
│   │   ├── source.py               # SourceObserved / SourceReferenced (Phase 8 で `SourceReferenced` consumer 第一級化、Phase 9 で `SourceObserved.fingerprint: str | None = None` field 追加、Phase 25-A で `author_handle` / `author_display: str | None = None` 横断 author 正規化 field 追加 (ADR-0010 §改訂)、いずれも backward-compat、schema_version 据え置き 1) [P1+2+3+8+9+25]
│   │   ├── link.py                 # LinkCreated / LinkDeleted (manual link CRUD) [P8]
│   │   ├── person.py               # PersonIdentified / IdentityLinked / IdentityMerged / IdentitySplit (人軸、ADR-0043) [P25]
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
│   ├── search_service.py           # SQLite FTS5 横断本文検索 (Phase 10 sub B、`sources_fts` 仮想テーブル経由) [P10]
│   ├── duplicate_service.py        # offline near-duplicate scan [P1+2+3+4]
│   ├── briefings/                  # BriefingService + prompts (ADR-0015、Phase 8 で graph 拡張、epic #470 で `expand_graph` param 削除 + LinkService 必須化) [P5+8]
│   │   ├── __init__.py             # [P5]
│   │   ├── prompts.py              # SYSTEM_PROMPT / USER_PROMPT_TEMPLATE / render_user_prompt [P5]
│   │   └── service.py              # BriefingService.generate(topic, ...) (Phase 8 で graph 拡張 hook 化、epic #470 で `expand_graph` param 削除 + LinkService 必須化) [P5+8]
│   ├── proposals/                  # ProposalService + prompts (ADR-0016、Phase 8 で graph 拡張、Phase 10 で reply_draft 拡張、epic #470 で `expand_graph` param 削除 + LinkService 必須化) [P6+8+10]
│   │   ├── __init__.py             # [P6]
│   │   ├── prompts.py              # SYSTEM_PROMPT / render_user_prompt (briefing-seed + delimiter wrap) [P6]
│   │   ├── reply_draft_prompts.py  # 返信下書き専用 prompt (do-not-follow preamble + style_example + context_source) (ADR-0016 §決定 (i)+(k)) [P10]
│   │   └── service.py              # ProposalService.generate / apply / reject (Phase 8 で graph 拡張 hook 化、Phase 10 で `generate_reply_draft()` 追加、epic #470 で `expand_graph` param 削除 + LinkService 必須化) [P6+8+10]
│   ├── links/                      # LinkService + traversal (related / trace / expand) + writer (create / delete) (ADR-0017) [P8]
│   │   ├── __init__.py             # [P8]
│   │   └── service.py              # LinkService: read-only traversal + manual link CRUD writer methods [P8]
│   ├── persons/                    # PersonResolutionService: 人軸 identity 解決 (resolve / list / merge / split) (ADR-0043) [P25]
│   │   ├── __init__.py             # [P25]
│   │   └── service.py              # exact auto-merge / fuzzy HITL merge + operator-as-1-person [P25]
│   ├── operator_identity.py        # per-connector "who am I" 解決 (is_authored_by_operator) (ADR-0010 §改訂) [P25]
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
│   ├── sources.py                  # external source 現在状態 (Phase 9 で `fingerprint` 列を追加、migration 0017、ADR-0019 §決定 (d)、Phase 25-A で `author_handle` / `author_display` / `author_connector` 列を追加、migration 0034、ADR-0010 §改訂) [P1+2+3+9+25]
│   ├── connector_cursors.py        # connector 差分同期 cursor [P1+2+3]
│   ├── ingested_files.py           # workspace file ingest の content_hash 追跡 [P1+2+3]
│   ├── briefings.py                # LLM briefing 結果 (markdown + source_refs + cost trace、Phase 5 で実装済) [P5]
│   ├── proposals.py                # LLM proposal candidates + per-candidate state (pending/applied/rejected、ADR-0016) [P6]
│   ├── links.py                    # entity 間 graph 関係 (LinksProjector / `links_table` / `LINK_TYPES_MVP`、migration 0016、ADR-0017、Phase 25-B で `identifies` link_type 追加) [P8+25]
│   ├── persons.py                  # 人軸 person aggregate (PersonsProjection / `persons_table`、merge/split を atomic に適用、migration 0035、ADR-0043) [P25]
│   └── person_identities.py        # connector-native identity → person (PersonIdentitiesProjection / `person_identities_table`、migration 0036、ADR-0043) [P25]
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
│   ├── ms365/                      # Microsoft 365 connector (Phase 7 B1-B3、Phase 11 で outlook body deep retention に拡張) [P7+11]
│   │   ├── __init__.py             # register_connector(MS365Connector()) side effect [P7]
│   │   ├── auth.py                 # msal paste-code OAuth + refresh-token keyring 保管 [P7]
│   │   ├── fetcher.py              # httpx Graph client + 3 endpoint cursors (calendar / onedrive / outlook) [P7]
│   │   ├── mapper.py               # 3 source_type mappers (ms365_calendar / ms365_onedrive / ms365_outlook、Phase 11 で outlook body 取り込み + 500K chars truncate inline) [P7+11]
│   │   └── connector.py            # MS365Connector(sync) with per-endpoint isolation [P7]
│   ├── box/                        # Box connector (Phase 7 C1-C3) [P7]
│   │   ├── __init__.py             # register_connector(BoxConnector()) side effect [P7]
│   │   ├── auth.py                 # boxsdk OAuth2 + refresh-token keyring 保管 [P7]
│   │   ├── fetcher.py              # Box Events API + stream_position cursor [P7]
│   │   ├── mapper.py               # RawBoxEvent → SourceObserved (source_type=box_event) [P7]
│   │   └── connector.py            # BoxConnector(sync) [P7]
│   ├── box_drive/                  # Box Drive (FS-backed) connector (Phase 9 B1-B2、ADR-0019。Phase 11 で content_extraction opt-in hook 追加 / Office 抽出経路) [P9+11]
│   │   ├── __init__.py             # register_connector(BoxDriveConnector()) side effect [P9]
│   │   ├── scanner.py              # BoxDriveScanner: os.scandir() walk + stat() metadata only、`open()` 禁止 不変条件 (ADR-0019 §決定 (b)、§決定 (b') opt-in 例外節で `content_extraction = true` 時のみ markitdown 経由 open 許可) [P9+11]
│   │   ├── mapper.py               # ScannedFile → SourceObserved (source_type=box_drive_file / word_document / excel_spreadsheet / powerpoint_slide_deck、external_id=rel_path、fingerprint=f"{size}:{mtime_ns}") [P9+11]
│   │   └── connector.py            # BoxDriveConnector(sync): settings → scanner → SourceService.observe (atomic UoW per file) [P9]
│   ├── onedrive_drive/             # OneDrive Drive (FS-backed) connector (Phase 11 F4-b、ADR-0019 §決定 (j) パターン汎化、box_drive と同 contract) [P11]
│   │   ├── __init__.py             # register_connector(OneDriveDriveConnector()) side effect [P11]
│   │   ├── scanner.py              # OneDriveDriveScanner: 同 box_drive contract + `content_extraction` opt-in hook [P11]
│   │   ├── mapper.py               # ScannedFile → SourceObserved (`connector_name="onedrive_drive"`、source_type 共通 4 種、`root_path` platform default = WSL2 `/mnt/onedrive` / macOS `~/OneDrive`) [P11]
│   │   └── connector.py            # OneDriveDriveConnector(sync): box_drive と同 sync flow [P11]
│   ├── teams/                      # Microsoft Teams connector (Phase 11 F5、ADR-0010 改訂 (a)/(c)/(d)) [P11]
│   │   ├── __init__.py             # register_connector(TeamsConnector()) side effect [P11]
│   │   ├── auth.py                 # TeamsAuth: User Token resolver (`core/secrets` + keyring、`OPSHUB_CONNECTOR_TEAMS_TOKEN` env override、ADR-0014 再利用) [P11]
│   │   ├── fetcher.py              # TeamsFetcher: Microsoft Graph `/me/chats/getAllMessages` delta + 失効時 `$filter=lastModifiedDateTime ge <iso>` full-pass fallback (ADR-0010 §改訂 (c)) [P11]
│   │   ├── mapper.py               # RawTeamsChatMessage → SourceObserved (source_type=teams_message、HTML → plain text、ADR-0020 §(e) provenance) [P11]
│   │   └── connector.py            # TeamsConnector(sync): auth → fetcher → mapper → SourceService.observe + excludes (channels / senders) [P11]
│   ├── google_auth/                # Shared Google OAuth foundation (Phase 14 G2、ADR-0010 §Phase 14 改訂 (m)) — auth helper shared by `google_workspace` / `google_mail` (Phase 14 G3) / `google_calendar` (Phase 14 G4) connectors [P13+14]
│   │   ├── __init__.py             # 一行 doc package marker (no module-level imports — cold-start guard) [P14]
│   │   └── auth.py                 # GoogleWorkspaceAuth: paste-code OAuth (`drive.readonly + gmail.readonly + calendar.readonly` の 3 scope 固定 list、Phase 14 plan §X.1) + Refresh Token rotation 書き戻し (MS365 / Box pattern、ADR-0010 §Phase 13 改訂 (h)) + keyring slot `connector:google_workspace:refresh_token` (ADR-0014 §Phase 7 Validation 3 件目、Phase 14 G2 で slot 文字列を維持しつつ 3 connector で共有 = 1 Google account = 1 principal) + env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` [P13+14]
│   ├── google_workspace/           # Google Drive / Docs / Slides / Sheets connector (Phase 13 G3 + G4、ADR-0010 §Phase 13 改訂 (e)-(h) + ADR-0014 改訂 + ADR-0025 改訂 (d')+(j); Phase 14 G2 で auth.py を `google_auth/` に抽出) [P13]
│   │   ├── __init__.py             # register_connector(GoogleWorkspaceConnector()) side effect [P13]
│   │   ├── client.py               # DriveClient: Drive API v3 `changes.list` (cursor-based) + `files.list` (initial sync) + `files.export(fileId, mimeType=<MS Office mediatype>)` + httpx + rate-limit 429 + 5xx exponential backoff retry [P13]
│   │   ├── cursor.py               # page token cursor key (`CURSOR_CHANGES`) + TTL 失効時 (400 / 404 / 410) full-pass fallback (Phase 11 Teams delta-link と同パターン、ADR-0010 §Phase 13 改訂 (g)) [P13]
│   │   ├── mapper.py               # RawDriveItem → SourceObserved (source_type 分岐 = `google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` catch-all、ADR-0025 §決定 (d') の Google mimeType → source_type lookup を `core/document_extract.GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE` から import、provenance `external` / `untrusted`、ADR-0020 §(e)) [P13]
│   │   ├── connector.py            # GoogleWorkspaceConnector(sync): auth → client → mapper → SourceService.observe + content_extraction opt-in wiring (`[connectors.google_workspace] content_extraction = true` 時のみ `files.export` → `core/document_extract.extract_workspace_export(bytes, source_type)` 経由 markitdown 抽出、Phase 11 box_drive / onedrive_drive と同 wiring パターン) [P13]
│   │   └── settings.py             # re-export shim (`GoogleWorkspaceConnectorSettings` は `core/config.py` で定義、本ファイルは Phase 7 既存 connector 配置との対称性を保つための薄い import 経路) [P13]
│   ├── google_mail/                # Gmail connector (Phase 14 G3、ADR-0010 §Phase 14 改訂 (i)-(l) — `connectors/google_auth/` shared auth foundation 使用、Outlook と symmetric な message 単位 mapper) [P14]
│   │   ├── __init__.py             # register_connector(GoogleMailConnector()) side effect [P14]
│   │   ├── client.py               # GmailClient: Gmail API v1 `users.messages.list` (initial sync) + `users.messages.get(format=full)` + `users.history.list` (delta) + httpx + rate-limit 429 + 5xx exponential backoff retry [P14]
│   │   ├── cursor.py               # History API `startHistoryId` cursor + 7 日 TTL 失効時 (HTTP 404) `users.messages.list` full-pass fallback + WARN (ADR-0010 §Phase 14 改訂 (j) delta-cursor 型一般化) [P14]
│   │   ├── mapper.py               # RawGmailMessage → SourceObserved (`source_type=gmail_message`、Outlook と symmetric: body = text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし / `[Labels: ...]` prepend / `[gmail body truncated: N / M chars]` tag / threadId field、provenance `external` / `untrusted`、ADR-0010 §Phase 14 改訂 (k)+(l)) [P14]
│   │   ├── connector.py            # GoogleMailConnector(sync): google_auth → client → cursor → mapper → SourceService.observe (1 message = 1 SourceObserved emit) [P14]
│   │   └── settings.py             # re-export shim (`GoogleMailConnectorSettings` は `core/config.py` で定義、env prefix `OPSHUB_CONNECTORS__GOOGLE_MAIL__`、field = `enabled` / `initial_window_days` / `fallback_window_days`、Phase 7 既存 connector 配置との対称性を保つための薄い import 経路) [P14]
│   └── google_calendar/            # Google Calendar connector (Phase 14 G4、ADR-0010 §Phase 14 改訂 (i)-(l) — `connectors/google_auth/` shared auth foundation 使用、MS365 Calendar と symmetric な master event only mapper + override 別 record) [P14]
│       ├── __init__.py             # register_connector(GoogleCalendarConnector()) side effect [P14]
│       ├── client.py               # GoogleCalendarClient: Calendar API v3 `events.list(syncToken=...)` (delta) + `events.list(timeMin/timeMax)` (full-pass、`singleEvents=false` + `showDeleted=true` 固定) + httpx + rate-limit 429 + 5xx exponential backoff retry [P14]
│       ├── cursor.py               # syncToken cursor + 410 GONE 失効時 (`SyncTokenExpiredError`) `_fallback_window_pass` (timeMin/timeMax window walk + 各 page で `cursor_set` 即時更新 + WARN `connector.events_list.expired`、ADR-0010 §Phase 14 改訂 (j) delta-cursor 型一般化) [P14]
│       ├── mapper.py               # RawCalendarEvent → SourceObserved (`source_type=google_calendar`、MS365 Calendar と symmetric: summary = `f"{start_iso} - {end_iso} ({attendees_count} attendees)"` / RRULE field / attendee email list + 議題 (description) + 会議室 (location) を body に追記、master event = `recurringEventId` 無し / override = `recurringEventId` + `originalStartTime` 持ち別 record として emit + body に `Override of: <master_id>` back-pointer、provenance `external` / `untrusted`) [P14]
│       ├── connector.py            # GoogleCalendarConnector(sync): google_auth → client → cursor → mapper → SourceService.observe (master event + override を独立 SourceObserved として emit、empty calendar / no-change-delta も `(None, next_sync_token)` で必ず cursor 前進) [P14]
│       └── settings.py             # re-export shim (`GoogleCalendarConnectorSettings` は `core/config.py` で定義、env prefix `OPSHUB_CONNECTORS__GOOGLE_CALENDAR__`、field = `enabled` / `calendar_id` / `time_min_days` / `time_max_days`、Phase 7 既存 connector 配置との対称性を保つための薄い import 経路) [P14]
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
├── mcp/                            # MCP server surface (Phase 10 sub C + Step 1 widening PR #231 + Phase 12 H1 ADR-0022 改訂、ADR-0022) — stdio one transport [P10+12]
│   ├── __init__.py                 # [P10]
│   ├── server.py                   # serve_stdio() + dispatch_tool_call() + build_tool_specs_for_engine() (Phase 12 H1 で `search` / `propose.apply` を spec list に追加) [P10+12]
│   ├── _registry.py                # policy-as-data registry (ToolSpec / ToolPolicy / ReadCategory / WriteCategory、ADR-0022 §(c)。Phase 12 H1 で `ReadCategory.SEARCH` + `WriteCategory.PROPOSE_APPLY` 追加、`_NON_DESTRUCTIVE_WRITES = {"propose.apply"}` carve-out) [P10+12]
│   ├── _tools.py                   # read tool handlers (recall.search / task.list / inbox.list / decision.list / brief / graph.related / graph.trace / graph.expand / source.list / source.get / embeddings.find_duplicates / Phase 12 H1 で `build_search_handler` (FTS5、phrase-quoted default、`raw_query` flag は schema 除外) 追加 + 既存 4 read tools の physical column ベース時間フィルタ追加) [P10+12]
│   ├── _writes.py                  # write tool handlers (task.create / inbox.add / connector.sync / propose.generate (Step 1 widening + Phase 12 H4 で `mode` 引数追加 = `inbox_triage` / `source_extract` / `meeting_followup`、`_PROPOSE_GENERATE_MODES` frozenset、ADR-0016 §決定 (l)(b)) / Phase 12 H1 で `build_propose_apply_handler` (HITL idempotent 正規化、`OpsHubError("already applied")` catch → `{ok:true, already_applied:true}` 正規化、`_lookup_applied_entity` で event log 走査) 追加、HITL) [P10+12]
│   ├── _redact.py                  # redact_secrets() (ADR-0022 §(b) — sk-... / ghp_... / Bearer ... をマスク) [P10]
│   └── _logging.py                 # OTel GenAI semconv (execute_tool / tool.name / tool.call.id) for structlog (ADR-0022 §(e)) [P10]
# graph/ サブパッケージは新設せず、Phase 8 では `projections/links.py` + `services/links/`
# + `cli/link.py` + `cli/graph.py` の組み合わせで Knowledge graph layer を提供する。
# ADR-0017 §決定 (a) の単一 `links` table + LinkService traversal で完結。
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
