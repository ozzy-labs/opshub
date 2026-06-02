# Architecture

> Status: Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connectors + workspace ingest、MVP scope = framework + GitHub) + Phase 4 (semantic recall layer、MVP scope = full Pluggable Embedder + recall + duplicate detection) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + `opshub brief` + event-driven auto-embed 補助) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + `opshub propose` CLI、human-in-the-loop apply 必須)) + Phase 7 (Connectors Wave 2、MVP = Slack + Microsoft 365 + Box、epic #113) + Phase 8 (Knowledge graph layer、MVP = ADR-0017 + `links` projection (migration 0016) + 4 自動抽出経路 + manual link CRUD + `LinkService` traversal + `opshub link` / `opshub graph` CLI + `--expand-graph` integration、epic #128) complete (2026-05-17) + Phase 9 (Local-filesystem-backed Connector Layer、MVP = ADR-0019 + `sources.fingerprint` 列 (migration 0017) + `box_drive` connector (scanner + mapper + connector + settings) + `core/platform.py` + `opshub connector sync box_drive`、epic #187) complete (2026-05-23) + Phase 10 (Secretary Agent Platform、MVP = ADR-0020 (full local content retention、ADR-0005 supersede) + ADR-0021 (encryption at rest、SQLCipher + keyring) + ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) + ADR-0004 改訂 (形A) + ADR-0016/0017/0010 改訂 (reply_draft) + 本文ベース embedding + SQLite FTS5 + `opshub search` + `opshub mcp serve` + 秘書 5 Skills + `tools/skill_scan.py`、epic #203) complete (2026-05-31) + Phase 11 (MS Office 深掘り、MVP = ADR-0025 (Office Document Content Extraction、markitdown 経路 + 50 MB / 500K chars cap + source_type 3 種) + ADR-0019 改訂 (`content_extraction = true` opt-in 例外節 + onedrive_drive パターン汎化) + ADR-0010 改訂 (Teams 追加 + 本文抽出契約 + delta-link cursor + 失効時 full-pass fallback + Teams User Token principal) + `core/document_extract.py` + `connectors/teams/` + `connectors/onedrive_drive/` + `connectors/box_drive` の Office 抽出 hook + `connectors/ms365/mapper` の outlook body deep retention、epic #233) complete (2026-05-31) + **Phase 12 (Secretary Skills 拡張、MVP = 14 skills 体制 (新規 9 = meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract + 既存 5 のうち rename 2 = daily-brief → personal-brief / file-lookup → find-document) + 4 新 MCP tools (`search` FTS5 + `propose.apply` HITL idempotent + 既存 4 read tools の physical column ベース時間フィルタ) + 既存 5 SKILL.md を MCP 直接呼びに統一 + ADR-0004 改訂 (Skills SSOT 移管 + Skill catalog SSOT 独立条文化) + ADR-0022 改訂 (4 新 MCP tools 契約化) + ADR-0016 改訂 (draft 系統一方針 §決定 (l)) + `docs/secretary-agent.md` を 14 skills 責務マップ SSOT に拡張、epic #253) complete (2026-05-31)** + **Phase 13 (Google Workspace コネクタ、MVP = ADR-0010 改訂 (§Phase 13 改訂 (e)-(h) = `google_workspace` 新コネクタ + Drive `files.watch` 禁止 + Workspace export 経路の本文抽出契約 + Drive `changes.list` cursor + TTL 失効時 full-pass fallback + Refresh Token principal = MS365 / Box pattern、Teams pattern とは別系統) + ADR-0014 改訂 (§Phase 7 Validation rotation pin リスト 3 件目に `connector:google_workspace:refresh_token` 追加) + ADR-0025 改訂 (§決定 (d') 新 source_type 3 種 `google_doc` / `google_slides` / `google_sheets` + §決定 (j) Workspace export 経路 = Drive API `files.export` → MS Office mediatype → markitdown 統一、`extract_workspace_export(bytes, source_type)` で API 表面拡張) + `connectors/google_workspace/` 5 module 構成 (auth + client + cursor + mapper + connector + settings) + `[connectors-google-workspace]` extras (httpx)、epic #274) complete (2026-05-31)** + **Phase 14 (Gmail + Google Calendar コネクタ、MVP = ADR-0010 改訂 (§Phase 14 改訂 (i)-(m) = Gmail + Calendar 追加 / delta-cursor 型 connector 全般への TTL fallback 一般化 ((j) で Phase 11/13 SSOT 統合) / Outlook 流本文抽出 (text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし) を Gmail / Calendar に適用 / Gmail unit = message 単位 + Calendar unit = master event only + override 別 record + label / attendee は summary / body 埋め込みのみ / Google OAuth principal scope 拡張 + shared auth foundation `connectors/google_auth/` 抽出方針) + ADR-0014 改訂 (`connector:google_workspace:refresh_token` slot scope を Drive → Drive + Gmail + Calendar に拡大、新 slot 追加なし = 1 Google account = 1 principal で 3 connector 共有、shared auth foundation 抽出方針追記) + `connectors/google_auth/` (Phase 13 `google_workspace/auth.py` の物理移動 + 3-scope 固定 list、token rotation pin test 集約) + `connectors/google_mail/` 5 module 構成 (httpx + Gmail API v1 `users.messages.list` + `users.history.list` delta + 7 日 TTL fallback、Outlook と symmetric な message 単位 mapper) + `connectors/google_calendar/` 5 module 構成 (httpx + Calendar API v3 `events.list(syncToken=...)` + 410 GONE fallback + master event only + override 別 record、MS365 Calendar と symmetric な mapper) + mapper symmetry pin test (`tests/unit/connectors/test_mapper_symmetry.py`)、epic #292) complete (2026-05-31)** + **Phase 15 (Search 品質改善、MVP = ADR-0028 (FTS5 sources_fts tokenizer choice、新規) + migration 0028 (`sources_fts` を FTS5 built-in `trigram` に物理張り替え + `sources.body` から back-fill + trigger 3 本再作成) + SearchService 短クエリ LIKE fallback (`_MIN_FTS_QUERY_CHARS = 3`、1-2 文字 query は `LOWER(body) LIKE LOWER(?)` full scan、NFC 正規化 + ASCII case-insensitive + LIKE wildcard escape + `raw_query=True` bypass) + cross-cutting fix (`tests/unit/mcp/test_phase12_handlers.py::_bootstrap_fts_index()` の seed tokenizer 同期) + `opshub search --help` の `--raw` 説明更新 + `docs/troubleshooting.md` §3.6 日本語 search 節追加。日本語自然文は default mode で 3 文字以上の substring を hit、短クエリは LIKE fallback で hit、`--raw` は FTS5 power-user 契約として維持。MCP `search` tool (ADR-0022 §決定 (f)) は `raw_query` hard-coded `false` のため秘書 14 Skill も透過的に恩恵を受ける。形 A + 外部書き戻しなし + immutable migration 規範を継承、新規 ADR 1 本 + 改訂ゼロ、epic #338) complete (2026-06-02)**. 次の候補は Phase 16+ (形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab、Phase 15 で defer) / dual index (unicode61 + trigram) / `opshub search rebuild-index` 専用 CLI / MCP `search` tool 契約改訂 / 検索結果の semantic re-rank / snippet() ハイライト / 検索クエリの NFKC 正規化 / multi-machine sync / 能動性段階 1-4 = cron 委譲 / 記憶キュレーション / 通知 / filewatch / Gmail push / Calendar push 再評価、画像 OCR (Phase 13 → 14 から繰り越し)、Drive Comments + Suggestions (Phase 13 から繰り越し)、Gmail / Calendar 添付の本文抽出 (markitdown 経路、ADR-0025 拡張)、追加コネクタ Notion / Jira / Linear / Confluence (Phase 13 から繰り越し)、外部書き戻し (Teams 返信送信 / Gmail send / Calendar event create + HITL)、Calendar instance 展開 projection (master + RRULE → instance dynamic、ms365 / google 両 calendar 同時)、`ozzy-labs/skills` 配布完成)。`llama.cpp` direct binding / briefing cache + narrow scope / connector-side automatic `SourceReferenced` 発行 / watch mode (filewatch backend) / 追加 FS connector / multi-machine sync は Phase 6.x / 7.x / 8.x / 9.x / 11.x+ 以降。詳細は §9 (Phased Delivery) を参照。

OpsHub の高レベルアーキテクチャ・データフロー・データモデル・用語を記述する。具体的な決定の根拠は対応 ADR を参照。

## 1. レイヤ全体像

```text
┌────────────────────────────────────────────────────────────┐
│  External Systems                                          │
│  GitHub  Slack  Microsoft 365  Box  Office Files  ...      │
└─────────────────┬──────────────────────────────────────────┘
                  │ (Phase 3 ✅ GitHub / Phase 7 ✅ Slack・MS365・Box / Phase 9 ✅ box_drive (FS scan、ADR-0019))
                  ▼
┌────────────────────────────────────────────────────────────┐
│  Connector Layer  (src/opshub/connectors/*)                │
│  - external metadata → source entity                       │
│  - cursor checkpointing                                    │
│  - emits SourceObserved / SourceReferenced events          │
│  ✅ Phase 3 実装済 (GitHub connector + 共通 framework)     │
└─────────────────┬──────────────────────────────────────────┘
                  │ Service call (append event)
                  ▼
┌────────────────────────────────────────────────────────────┐
│  Application Services  (src/opshub/services/*)             │
│  - command 検証                                            │
│  - domain event を append                                  │
│  - projector に通知                                        │
│  - lock 取得 / 解放                                        │
└─────────────────┬──────────────────────────────────────────┘
                  │ append + notify
                  ▼
┌────────────────────────┐          ┌────────────────────────┐
│ Event Store (auth.)    │ ─rebuild→│ Projection Layer       │
│ events table           │          │ tasks / sources /      │
│ append-only, immutable │          │ inbox_items / decisions│
│ SQLite                 │          │ / links / sessions ... │
└────────────────────────┘          └─────────────┬──────────┘
                                                  │
                          ┌───────────────────────┼───────────────────┐
                          ▼                       ▼                   ▼
                ┌────────────────┐     ┌────────────────┐    ┌────────────────┐
                │ Graph Layer    │     │ Vector Layer   │    │ Workspace      │
                │ links table    │     │ sqlite-vec     │    │ Generation     │
                │ + queries      │     │ ✅ Phase 4 実装済│    │ markdown out   │
                └────────────────┘     └────────────────┘    └────────────────┘
                                                                      │
                          ┌───────────────────────────────────────────┘
                          ▼
┌────────────────────────────────────────────────────────────┐
│  Agent Runtime Boundary                                    │
│  Claude Code / Codex CLI / Gemini CLI / Copilot CLI        │
│  → 必ず `opshub` CLI 経由で event / command を発行         │
│  → workspace markdown を Read tool で参照                  │
└────────────────────────────────────────────────────────────┘
```

## 2. コンポーネント責務

### 2.1 Connector Layer

外部 SaaS から差分メタデータを取得し、Source Entity / Source Event / Inbox Item を Application Services 経由で登録する。

- 行うこと: 差分 fetch / cursor 保存 / normalization / event 発行依頼
- 行わないこと: Task / Decision / Link の自動生成、projection 直書き、event の bypass

Phase 3 実装状況: **GitHub connector が最初の具象実装** (`src/opshub/connectors/github/`、ADR-0010 で contract が検証され Accepted 昇格)。共通基盤 (`Connector` Protocol / `ConnectorContext` / `SourceService` / `connector_cursors` projection / `core.secrets` keychain backend) は完了。Slack / Microsoft 365 / Box は Phase 3.x で同じ contract に従って追加する。workspace 上の人手記述 `.md` は別経路 (`opshub workspace ingest` + `FileIngestService` + `ingested_files` projection、ADR-0005 整合) で event 化する。

Phase 9 実装状況: **`box_drive` connector が最初の FS-backed 具象実装** (`src/opshub/connectors/box_drive/`、ADR-0019)。Phase 7 までの 4 connector が vendor Web API + OAuth に依存していたのに対し、`box_drive` は OS-level Box Drive 認証 + ローカル FS scan で同じ `Connector` Protocol に適合する。`source_type="box_drive_file"`、`fingerprint = f"{size}:{mtime_ns}"` 列 (`sources` projection、migration 0017) で差分検出。詳細は §2.11 を参照。

Phase 10 実装状況: **本文取り込み + provenance タグの拡張** ([ADR-0020](adr/0020-full-local-content-retention.md))。`SourceObserved` event に `body` / `provenance_origin` / `provenance_trust` の 3 フィールドが追加され、Slack / Box / GitHub / MS365 の各 connector mapper が要約だけでなく本文も取り込むようになった。連動して `sources` projection の `body` 列 (migration 0018) と FTS5 仮想テーブル `sources_fts` (migration 0019) が追加される。`box_drive` は ADR-0019 §不変条件 (b) で `open()` 禁止のため `body=None` のまま (FS scan は metadata のみ)。**取り込み除外設定** (`~/.config/opshub/excludes.yaml`) は channel / sender / repo / path で外部入力を弾き、保存時暗号化は SQLCipher で DB 丸ごと AES-256 (ADR-0021)。詳細は §8.1 を参照。

10 connector の一覧 (Phase 11 で Teams / OneDrive Drive を、Phase 13 で Google Workspace を、Phase 14 で Gmail / Google Calendar を追加。Phase 14 で Google 3 connector は `connectors/google_auth/` shared auth foundation = 1 Google account principal + 3-scope 固定 list `drive.readonly + gmail.readonly + calendar.readonly` を共有):

| Connector | 経路 | source_type | 認証 | Extras |
|---|---|---|---|---|
| GitHub | Web API (PyGithub) | `github_issue` / `github_pr` / etc. | PAT (`core/secrets` + keyring) | `[connectors-github]` |
| Slack | Web API (slack-sdk)。conversation 一覧は `opshub connector slack conversations` (`users.conversations` 経由、joined-only default + DM/MPIM 統合、`--format toml` で `[connectors.slack] channels` 用 snippet 出力、`--all` で `conversations.list` workspace-wide fallback、type 別固定ソート (`public → private → mpim → im`) + `--since 7d` / `--since 2026-05-01` で最終メッセージ ts による activity フィルター ([#374](https://github.com/ozzy-labs/opshub/issues/374))、Phase 14.x #341 / #366) で取得 | `slack_message` | User Token (`xoxp-`) / Bot Token (`xoxb-`) | `[connectors-slack]` |
| Microsoft 365 | Web API (msgraph、Phase 11 で outlook 本文 deep retention) | `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` | OAuth paste-code (msal) | `[connectors-ms365]` |
| Box | Web API (boxsdk) | `box_event` | OAuth paste-code (boxsdk) | `[connectors-box]` |
| Box Drive (FS) | Local FS scan (`os.scandir()` + `stat()`、ADR-0019。Phase 11 で `content_extraction` opt-in 経路を追加し Office 文書 (`.docx`/`.xlsx`/`.pptx`) を markitdown 抽出) | `box_drive_file` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` | なし (OS daemon に委譲、`opshub.toml` 設定のみ) | `[office]` (extraction 利用時) |
| OneDrive Drive (FS) | Local FS scan (`os.scandir()` + `stat()`、ADR-0019 §決定 (j) パターン汎化。Phase 11 で box_drive と同パターン追加) | `onedrive_drive_file` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` | なし (OS daemon = OneDrive Desktop に委譲、`opshub.toml` 設定のみ) | `[office]` (extraction 利用時) |
| Teams | Microsoft Graph delta query (`/me/chats/getAllMessages`、Phase 11 ADR-0010 改訂 (a)/(c)/(d)) | `teams_message` | User Token (delegated permissions、`Chat.Read`)、Bot Token は alternative | `[connectors-teams]` (msal + httpx) |
| Google Workspace | Drive API v3 `changes.list` cursor + `files.export` で Workspace native (Docs / Slides / Sheets) → MS Office mediatype → markitdown (Phase 13 ADR-0010 改訂 (e)-(h)、ADR-0025 §決定 (d') + (j)。Phase 14 G2 で auth.py を `connectors/google_auth/` に shared 化) | `google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` (catch-all) | OAuth paste-code (Refresh Token + offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern、Teams pattern とは別系統。Phase 14 で 3-scope 固定 list = `drive.readonly + gmail.readonly + calendar.readonly`、1 Google account principal を Gmail / Calendar と共有) | `[connectors-google-workspace]` (httpx) + `[office]` (extraction 利用時) |
| Gmail (Phase 14) | Gmail API v1 (`users.messages.list` initial + `users.history.list` delta + 7 日 TTL 失効時 full-pass fallback、Phase 14 ADR-0010 改訂 (i)/(j)/(k)/(l)、Outlook と symmetric な message 単位 mapper、body = text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし / `[Labels: ...]` prepend / `[gmail body truncated]` tag / threadId field) | `gmail_message` | OAuth paste-code (`connectors/google_auth/` 共有、Refresh Token rotation 書き戻し、Phase 14 で 3-scope) | `[connectors-google-workspace]` (httpx 共有) |
| Google Calendar (Phase 14) | Calendar API v3 (`events.list(syncToken=...)` + 410 GONE 失効時 full-pass + `timeMin` / `timeMax` window fallback、Phase 14 ADR-0010 改訂 (i)/(j)/(k)/(l)、MS365 Calendar と symmetric な master event only + override 別 record、summary = `start_iso - end_iso (N attendees)` / RRULE field / attendee body 埋め込み) | `google_calendar` | OAuth paste-code (`connectors/google_auth/` 共有、Refresh Token rotation 書き戻し、Phase 14 で 3-scope) | `[connectors-google-workspace]` (httpx 共有) |

長時間 `opshub connector sync <name>` 中の進捗表示は CLI driver 側で `_ProgressSourceProxy` (`src/opshub/cli/connector.py` L37-65) が source service を透過プロキシでラップし、`observe()` 成功ごとに `advance(1)` する形で実現する。`Connector` Protocol および 10 connector 本体は無改修で、進捗表示は connector 層の不変条件 (差分 fetch / cursor 保存 / event 発行依頼) を一切変えない。詳細は [ADR-0026](adr/0026-cli-progress-reporting.md) を参照。

### 2.2 Application Services

CLI / Connector / Agent から渡された command を受け、ドメイン的に有効化を検証し、Event Store に append、Projector に通知、必要なら Lock を取得・解放する。

長時間処理 (connector sync / embeddings rebuild・drain / projections rebuild) の進捗表示は CLI 層に閉じる (ADR-0026)。service は rich に依存せず、optional な `progress_callback` (default `None`) を受けるだけの IoC seam を提供する (例: `EmbeddingService.embed_pending` / `projections.rebuild_all`)。connector sync は `Connector` Protocol を変えずに source service を透過プロキシでラップして進捗を取る。進捗は stderr へ描画し非 TTY では no-op のため、stdout の結果サマリは不変。

**Observability surface (Phase 14、[ADR-0027](adr/0027-observability-and-troubleshooting-logging.md))**: structlog ベースのログは redaction processor を経由するように構造化されている。`src/opshub/core/logging.py` の `_redaction_processor` が全イベント値 + `format_exc_info` 展開後の traceback 文字列を `core/sanitise.sanitise_error_message` に通すため、`sk-` / `ghp_` / `github_pat_` / `xox*-` / `AKIA` / `AIza` / JWT / `Bearer` の既知トークン形状はどのレンダラ (`JSONRenderer` / `ConsoleRenderer`) にも届かない (R1)。verbosity は root callback (`src/opshub/cli/app.py`) のグローバルフラグ `-v` / `-q` / `--debug` / `--log-format` / `--log-file` で制御し、フラグを渡せない subprocess 経路 (`opshub mcp serve` / cron) では `OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` / `OPSHUB_LOG_FILE` 環境変数が同等に効く。優先順位は CLI フラグ > 環境変数 > デフォルト。`--debug` 時に出る full traceback は `format_debug_traceback` でサニタイズ済み (R2)、connector sync 失敗時のデフォルトは「型名のみ + `sync failed: <Type>` の **stderr** サマリ不変 (成功時の `synced ...` は stdout 据え置き)」を維持し `--debug` 時にだけサニタイズ済みメッセージ + traceback を stderr に opt-in 追加する (R3)。`--log-file` 指定時は `O_CREAT | O_WRONLY | O_APPEND` + mode `0600` でファイルを作成しファイル内容にも redaction を適用する (R5)。operator 向け手順は [docs/troubleshooting.md](troubleshooting.md) に集約。

### 2.3 Event Store

`events` テーブル (SQLite)。append-only、immutable、`schema_version` 付き。Event は semantic ("TaskActivated") であるべきで、generic CRUD event は避ける。

### 2.4 Projection Layer

current state の relational view。`tasks` / `sources` / `inbox_items` / `decisions` / `links` / `work_sessions` / `agent_runs` / `locks` / `connector_cursors` 等。すべて event 列から rebuildable。

### 2.5 Graph Layer

entity 間の関係性 (`task → source` / `decision → meeting` 等) を `links` テーブルで relational に持つ。専用 graph DB は採用しない (Phase 4 以降で再評価)。

### 2.6 Vector Layer (interfaces in Phase 1, implementation shipped in Phase 4)

semantic 索引層は **Pluggable Embedder + Pluggable VectorStore** として設計する (ADR-0012)。Phase 1 で抽象境界 (`Embedder` / `VectorStore` Protocol) と `embeddings` projection schema を確定し、Phase 4 で 3 backend (local `sentence-transformers` / OpenAI / Voyage) + sqlite-vec backed `SqliteVecStore` を実装した (2026-05-17 完了)。

- **Embed 対象**: task title / decision text / inbox_item summary / source summary (ADR-0005 整合、full body は対象外)。briefing / extracted action items は Phase 5+ で追加
- **Storage**: sqlite-vec 仮想テーブル (backend ごと dim 別: `embeddings_vec_local` 1024-dim / `embeddings_vec_openai` 1536-dim / `embeddings_vec_voyage` 1024-dim) + `embeddings` projection (`entity_type`, `entity_id`, `model_id`, `model_version`, `dim`, `created_at`) で rowid JOIN
- **Refresh**: Phase 4 MVP は CLI-driven のみ — `opshub embeddings rebuild [--entity-type X] [--limit N]` で `(model_id, model_version)` 未 embed の行のみ処理 (冪等)。event 駆動自動 embed (projector hook + 背景 queue) は Phase 5
- **Backend 切替**: `~/.config/opshub/config.toml` の `[embedding]` セクションで `local` / `openai` / `voyage` / `disabled` を選択。デフォルトは `disabled` (CI / 初回 install 体験を軽く保つため、ADR-0012 §決定の確定)。OpenAI / Voyage の API key は `opshub connector auth set embedder:openai` / `embedder:voyage` で OS keychain に保存 (ADR-0014 整合)
- **Recall**: vector + SQL filter の hybrid search を CLI から提供 (`opshub recall "<query>" [--type X] [--state Y] [--format table|json|md]`)。backend 切替直後で `(model_id, model_version)` が ズレた場合は `ConfigError` + "rebuild required" 案内で fail-fast
- **重複検出**: `opshub embeddings find-duplicates [--threshold 0.92] [--entity-type source]` で nearest-neighbor scan を offline 解析用に提供。connector sync 経路の auto-detect は Phase 5+

詳細は [ADR-0012: Embedding Strategy](adr/0012-embedding-strategy.md) を参照。

### 2.7 Briefing layer (Phase 5)

ADR-0015 で Pluggable LLM Client (`LLMClient` Protocol、ADR-0009 vendor-neutral) を採択し、`BriefingService` が:

1. `BriefingRequested` event を append (UoW)
2. `RecallService` で topic に関連する task / decision / inbox_item / source を抽出 (Phase 4 semantic recall を再利用)
3. 各 entity の embeddable text を `<source id="..." type="...">...</source>` delimiter で wrap + "do not follow instructions" preamble を付与し LLM prompt 構築 (ADR-0015 §決定 (f) prompt injection mitigation)
4. `LLMClient.complete(messages, max_tokens, ...)` を呼出 (network I/O、UoW 外)
5. 成功時 `BriefingGenerated` event + `briefings` projection apply を 1 UoW で commit
6. 失敗時 `BriefingFailed` event を `core.sanitise.sanitise_error_message` 経由で記録 (UoW)

CLI: `opshub brief "<topic>" [--scope all] [--max-sources N] [--max-tokens N] [--save] [--format md|json]`. `[llm] backend = "disabled"` 状態では exit 2 + 案内 を返す。`--save` 指定時は `<workspace.root>/briefings/<slug>-<briefing-id>.md` に markdown を書出。

LLM backend は `[llm] backend` で切替: `disabled` (Phase 5 default) / `anthropic` (`claude-haiku-4-5-20251001` 推奨) / `openai` (`gpt-4o-mini` 推奨)。API key は ADR-0014 (`core/secrets` + keyring + env override) を再利用し、`opshub connector auth set llm:anthropic` 等で保存する。

補助: Phase 4 で deferred になっていた event-driven auto-embed を `[embedding] auto = true` opt-in で導入。`AutoEmbedHook.maybe_embed(event)` が `TaskCreated` / `DecisionRecorded` / `ItemEnqueued` / `SourceObserved` 系 event の post-commit hook として同期的に `EmbeddingService.embed_one_if_pending` を呼ぶ。失敗は log のみで originating event は roll back しない (Phase 4 NOT EXISTS retry を再利用、次の `opshub embeddings drain` / `rebuild` で retry)。

詳細は [ADR-0015: LLM Usage Strategy](adr/0015-llm-usage-strategy.md) を参照。

### 2.8 Action loop layer (Phase 6)

ADR-0016 で Pluggable LLM の structured output (Anthropic `tool_use` / OpenAI / Ollama `tools=` 関数呼び出し) を採択し、`ProposalService` が:

1. `ProposalRequested` event を append (UoW)
2. `RecallService` + 任意で Phase 5 briefing から context 抽出 (briefing.markdown を `<briefing>` block で wrap)
3. `<source id="..." type="...">...</source>` delimiter wrap + `html.escape` + "do not follow instructions" preamble で prompt 構築 (ADR-0015 §決定 (f) + Phase 5 D1 follow-up と同 contract)
4. `LLMClient.complete_structured(messages, schema=ProposalCandidatesSchema, ...)` を呼出 (network I/O、UoW 外)。Pydantic v2 model を SSOT として各 LLM client が native tool format に serialize (Anthropic `input_schema` / OpenAI `parameters` + strict mode / Ollama OpenAI 互換)
5. 成功時 `ProposalGenerated` event + `proposals` projection apply を 1 UoW で commit。失敗時 `ProposalFailed` event を `core.sanitise.sanitise_error_message` 経由で記録 (UoW)
6. `ProposalService.apply(proposal_id, candidate_index)` で operator 承認 → 既存 `TaskService.create_task` / `DecisionService.record_decision` 経由で実 entity 化 (ADR-0016 §決定 (g) validation 二重化禁止) → `ProposalApplied` event。再 apply / reject は service-layer fail-fast (`OpsHubError`、§決定 (d) idempotency)

**Human-in-the-loop 必須** (ADR-0004 + ADR-0016 §決定 (c)): apply は operator-triggered のみ。auto-apply は Phase 6.x 以降も導入しない。

**Local LLM backend (Ollama)** で principles.md §1 (Local-first) と LLM 利用の tension を緩和。3 backend (Anthropic + OpenAI + Ollama) で ADR-0009 (Multi-Agent Neutrality) を完備。`llama.cpp` direct binding は配布性の問題で Phase 6.x 持ち越し。

CLI: `opshub propose generate / list / apply / reject` 4 subcommands。

詳細は [ADR-0016: Action Loop and Structured Output](adr/0016-action-loop-and-structured-output.md) を参照。

### 2.9 Connectors Wave 2 layer (Phase 7)

Phase 3 で確立した connector framework (ADR-0010 + ADR-0014 + ADR-0005) を再利用し、Slack / Microsoft 365 / Box の 3 SaaS connector を追加した。各 connector は `connectors/<name>/` package で auth + fetcher + mapper の 3 module を持ち、`connectors/registry.py` で動的登録され `opshub connector sync <name>` が dispatch する。

| Connector | 取得対象 | source_type | OAuth flow | Extras |
|---|---|---|---|---|
| Slack | channel messages | `slack_message` | Bot token (`xoxb-`) | `[connectors-slack]` (slack-sdk) |
| Microsoft 365 | Calendar / OneDrive / Outlook | `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` | OAuth 2.0 paste-code (msal) | `[connectors-ms365]` (msal + httpx) |
| Box | file/folder events | `box_event` | OAuth 2.0 paste-code (boxsdk) | `[connectors-box]` (boxsdk) |

各 connector は ADR-0005 (External Content Min) を遵守: body 全文を取り込まず metadata + summary 200 chars 以内のみ persist。token は `core/secrets` + keyring + env var override (`OPSHUB_CONNECTOR_<NAME>_<PURPOSE>`)。rate-limit は exponential backoff (1s/2s/4s, max 3 retries) → 最終失敗で `ConnectorSyncFailed` event。

各 connector の sync は cursor-based (`connector_cursors` projection を再利用)。MS365 は 3 endpoint × 3 cursor key (`ms365:calendar` / `ms365:onedrive` / `ms365:outlook`) を独立管理し、1 endpoint failure が他の endpoint sync を blocking しない。

CLI: `opshub connector auth set connector:<slack|ms365|box>` でクレデンシャル保存、`opshub connector sync <name>` で取り込み。Phase 5 brief / Phase 6 propose は新 source_type を automatic に活用 (RecallService が `sources` projection 横断 query する設計のため)。

connector-side automatic `SourceReferenced` 発行 (Slack message URL parse / GitHub issue link parse 等) は Phase 8.x で各 connector mapper を拡張する形で対応。

### 2.10 Knowledge graph layer (Phase 8)

ADR-0017 で `links` projection を導入し、Phase 1-7 で蓄積された entity 間 reference を materialise する経路を確立した。

**4 自動抽出経路** (`LinksExtractor` projector、ADR-0017 §決定 (c) 純粋 derived state):

| Source event | Link |
|---|---|
| `ProposalApplied` (Phase 6) | `proposal → task/decision` (`applied_to`) |
| `BriefingGenerated.source_refs` (Phase 5) | `briefing → entity` per source_ref (`referenced_in_briefing`) |
| `ProposalRequested.briefing_id` (Phase 6) | `proposal → briefing` (`generated_from_briefing`) |
| `SourceReferenced` (Phase 3 placeholder closeout) | `source → entity` (`references`) |

**Manual link CRUD** (ADR-0017 §決定 (d) event-sourced 経路): `opshub link add` emits `LinkCreated`、`opshub link remove` emits `LinkDeleted`。Auto-extracted links は新 event を発行せず projection に直接 derive される。

**Graph traversal** (`LinkService`): `related` (1-hop bidirectional)、`trace` (incoming-direction provenance、default depth 3 / max 10)、`expand` (bidirectional N-hop、default 2 / max 5)。Cycle detection + visited tracking 必須。

**`--expand-graph` flag** (ADR-0017 §決定 (f) default off): `opshub brief --expand-graph` / `opshub propose generate --expand-graph` で RecallService の hit を 1-hop graph 拡張し LLM prompt に追加 source block を注入。Phase 5 D1 follow-up と同じ delimiter wrap + html.escape contract が graph-expanded sources にも適用される — security 不変。

CLI: `opshub link {add,remove,list}` + `opshub graph {related,trace,expand}` + `--format md|json|dot` (DOT は Graphviz 出力)。

**Phase 3 `SourceReferenced` placeholder closeout**: Phase 3 で defined だが consumer のなかった event を Phase 8 B2 `LinksExtractor` が消費。Connector-side automatic 発行 (GitHub Issue body `#123` parse / Slack message URL parse etc.) は Phase 8.x で別 PR。

詳細は [ADR-0017: Knowledge Graph](adr/0017-knowledge-graph.md) を参照。

### 2.11 Local-filesystem-backed Connector Layer (Phase 9)

ADR-0019 で **Local-filesystem-backed Connector pattern** を導入し、第一弾として
`box_drive` connector を実装した。Box Drive デスクトップクライアントが OS に
マウントしたローカル FS (`/mnt/b` on WSL2、`~/Box` on macOS) を直接 walk して
metadata 経由で source を取り込む。Box Web API が使えない企業環境
(developer app 登録不能 / OAuth grant 不可 / `api.box.com` egress 制限) でも
Box content を operational memory に取り込めるようにする 5 つ目の connector
category。

**Web API 経由 (Phase 7 `box`) と FS 経由 (Phase 9 `box_drive`) の比較**:

| 観点 | Phase 7 `box` connector | Phase 9 `box_drive` connector |
|---|---|---|
| 取得経路 | Box Platform API (`api.box.com`) | OS-mounted FS (`os.scandir()` + `stat()`) |
| 認証 | OAuth refresh token (`core/secrets` + keyring、ADR-0014) | OS-level Box Drive client (Web Box ログイン) |
| `source_type` | `box_event` | `box_drive_file` |
| 取得対象 | Box の event stream entry | FS 上の file metadata |
| Identity | Box item ID | `rel_path` (root_path 相対パス) |
| 差分検出 | `stream_position` cursor | `sources.fingerprint = f"{size}:{mtime_ns}"` (migration 0017) |
| Operator setup | OAuth paste-code | `mountvol B:` (WSL2) / Box Drive install (macOS)、`docs/box-drive-setup.md` 参照 |
| Network egress | 必須 (`api.box.com`) | 不要 (OS daemon 経由) |
| 配布 extras | `connectors-box` (boxsdk) | なし (stdlib のみ) |

両 connector は **同じ Box content を異なる `source_type` で二重取り込みを
許容する**。operator は独立に enable / disable 可能で、Phase 8 `links` projection の
manual `link add` で束ねる経路が将来開いている (`source_type` 分離設計の利点)。

**FS-backed pattern の構造的不変条件** (ADR-0019 §決定 (b)、§不変条件):

- Source は `os.stat()` metadata のみ参照、`open()` / `read_text` /
  `read_bytes` / magic bytes 検査 / shebang 検査 / file content hash を一切
  実行しない。CldAPI (Microsoft) / File Provider Extension (macOS) の
  placeholder hydration を防ぐため。
- 本不変条件は scanner の `tests/unit/connectors/box_drive/test_scanner.py`
  内 `test_scanner_never_opens_files` (`unittest.mock.patch("builtins.open",
  side_effect=AssertionError("forbidden"))`) で構造的に pin される。
- CldAPI non-hydration contract は
  `tests/integration/test_box_drive_no_hydration.py` (`OPSHUB_BOX_DRIVE_TEST_ROOT`
  env 設定時のみ実行 = real env opt-in) で持続検証する。

**`Connector` Protocol freeze**: Phase 7 4 connector で確立した
`Connector` Protocol (ADR-0010) を変えずに `auth layer を OS-level Box
Drive 認証への依存に置換` する形で 5 つ目の connector category を 1 connector
分の特殊化として閉じ込めた。`opshub connector auth set connector:box_drive` は
actionable error で reject される (paste-code 不要、`opshub.toml` 設定を案内)。

詳細は [ADR-0019: Local-filesystem-backed Connector](adr/0019-local-filesystem-backed-connector.md)
と [docs/box-drive-setup.md](box-drive-setup.md) を参照。Phase 9.x の outlook
は ADR-0019 §関連と principles.md §Open Questions を参照
(watch mode / 追加 FS connector / xattr identity / `opshub source list --stale`)。
共通 `excludes.yaml` 機構は Phase 10 ADR-0020 §(b) で横断統合済 (`src/opshub/core/excludes.py`)。

### 2.11a MCP Server Layer (Phase 10)

[ADR-0022](adr/0022-mcp-server-surface.md) で **エージェント host に ① コアを露出する MCP サーバ面** を追加した。`opshub mcp serve` (`src/opshub/mcp/`) が stdio で起動し、policy-as-data registry で定義された read / write tool 群を MCP プロトコル経由で公開する。**ネットワーク listen は禁止** (stdio 一択、HTTP/SSE モジュールへの import も `tests/unit/mcp/test_no_network_listen` で構造的に弾く) なので、confused deputy / SSRF / セッション乗っ取りが non-applicable になる。

主要不変条件 (ADR-0022 §決定):

- **stdio one transport** — `serve_stdio()` のみ entry point。HTTP / SSE は実装しない。
- **No token passthrough** — `ToolSpec` の `input_schema` は SaaS トークン受け入れ field を持たず、tool response は `redact_secrets` で `sk-...` / `ghp_...` / `Bearer ...` を redact する (ADR-0022 §(b))。
- **Read / write split = policy-as-data** — `ReadCategory` / `WriteCategory` enum が `ToolPolicy(read_only, destructive, idempotent, open_world)` を駆動。エージェント host は MCP `annotations` を見て read は auto-approve、write は人確認 (ADR-0022 §(c))。
- **Context-efficient returns** — `recall.search` / list 系は本文ではなく 200 文字 snippet を返す (ADR-0022 §(d))。データ持ち出し面と LLM context の両方を縮小。
- **OTel GenAI naming** — `gen_ai.operation.name=execute_tool` / `gen_ai.tool.name=<name>` / `gen_ai.tool.call.id=<ulid>` を structlog に記録 (将来 `mcp-otel` extras で exporter に出す、ADR-0022 §(e))。

Phase 10 C2 baseline + Step 1 widening (PR #231) + Phase 12 H1 (ADR-0022 改訂 §決定 (f)) で出荷した tool 一覧 (`src/opshub/mcp/_registry.py`、計 17 tools = read 12 + write 5):

| Kind | Name | 目的 |
|---|---|---|
| read | `recall.search` | semantic recall (vector、tasks / decisions / inbox / sources)。FTS5 は `opshub search` CLI および `search` MCP tool で提供、両者は補完関係 (principles §6.4) |
| read | `task.list` | tasks projection 取得 (state filter + Phase 12 H1 で `updated_after` / `updated_before` 物理列フィルタ追加、`tasks.updated_at` ベース、ISO 8601 半開区間) |
| read | `inbox.list` | inbox_items projection 取得 (state filter + Phase 12 H1 で `created_after` / `created_before` 物理列フィルタ追加、`inbox_items.created_at` ベース) |
| read | `decision.list` | decisions projection 取得 (Phase 12 H1 で `recorded_after` / `recorded_before` 物理列フィルタ追加、`decisions.recorded_at` ベース) |
| read | `brief` | LLM 要約 briefing 生成 (Step 1 widening、PR #231) |
| read | `graph.related` | 1-hop graph 隣接取得 (Step 1 widening) |
| read | `graph.trace` | backward provenance walk (Step 1 widening) |
| read | `graph.expand` | bidirectional N-hop subgraph (Step 1 widening) |
| read | `source.list` | sources projection 取得 (connector / source_type filter + Phase 12 H1 で `observed_after` / `observed_before` 物理列フィルタ追加、`sources.observed_at` ベース) |
| read | `source.get` | 1 source row 取得 by ULID (Step 1 widening) |
| read | `embeddings.find_duplicates` | offline near-duplicate scan (Step 1 widening) |
| read | `search` | **Phase 12 H1 新規** — body-level FTS5 横断検索 (`ReadCategory.SEARCH`、phrase-quoted default、`raw_query` flag は CLI 専用で MCP schema 除外。`SearchService.search` の MCP 露出。Phase 15 で SearchService 内部に trigram tokenizer + 短クエリ LIKE fallback が入ったため、日本語自然文と 1-2 文字短クエリも透過的に hit する。ADR-0028 参照) |
| write | `task.create` | TaskCreated event を追記 (HITL) |
| write | `inbox.add` | ItemEnqueued event を追記 (HITL) |
| write | `connector.sync` | 登録済 connector の sync を発火 (HITL、外部 API hit) |
| write | `propose.generate` | LLM proposal 生成 (HITL、Step 1 widening + Phase 12 H4 で `mode` 引数追加 = `inbox_triage` / `source_extract` / `meeting_followup`、persist 経路を持つ dispatch key に限定、ADR-0016 §決定 (l)(b)) |
| write | `propose.apply` | **Phase 12 H1 新規** — proposal candidate を apply (HITL、`WriteCategory.PROPOSE_APPLY`、`destructive=false` + `idempotent=true`。handler 層で `OpsHubError("already applied")` catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化して idempotent annotation を成立させる) |

MCP セットアップ手順は [docs/mcp-setup.md](mcp-setup.md) を参照。エージェント host (Claude Code 等) が subprocess として `opshub mcp serve` を spawn し、stdin / stdout で MCP プロトコルを話す。

### 2.11b Secretary Agent Layer (Phase 10、形A)

[ADR-0004 改訂](adr/0004-agent-runtime-boundary.md) で **形A** を確立した。opshub は **MCP サーバ (口) + Agent Skills (手順書)** のみを提供し、頭脳 (LLM 推論ループ / ReAct / state machine) は外部エージェント host (Claude Code / Codex CLI / Gemini CLI / Copilot CLI) が担う。opshub 自身には:

- **runtime なし** — LangGraph / OpenAI Agents SDK / Claude Agent SDK 相当の loop は持たない。
- **常駐プロセスなし** — `opshub mcp serve` は host が spawn する subprocess。リクエスト駆動。
- **人格なし** — システムプロンプト / persona は host 側 (skill description + host の persona) に置く。

代わりに opshub は:

1. **MCP tool 面** (§2.11a)
2. **秘書 14 Skill** (Phase 12 で 5 → 14 拡張、`docs/skills/<name>/SKILL.md` を opshub SSOT として保持、配信機構 = `ozzy-labs/skills` CI + Renovate preset は Phase 15+ に defer、ADR-0004 §決定 (c) backout)。Skill catalog SSOT は [docs/secretary-agent.md](secretary-agent.md) (ADR-0004 §決定 (c-2))。

   **read 自律 OK (10)**:

   | skill | pair | description トリガ | 使う MCP tool | 自律範囲 |
   |---|---|---|---|---|
   | `personal-brief` | ↔ external-brief | 「今日 / 今週 / 今月 / 先週 / 先月 のまとめ」「自分の状況」 | `brief` または `recall.search` + `task.list` (`updated_after/before`) + `inbox.list` (`created_after/before`) + `decision.list` (`recorded_after/before`) | 自律 OK |
   | `next-actions` | (stand-alone) | 「次に何 / やること / 今週やること」「優先度高いのは?」 | `task.list` (`updated_after/before`) + `recall.search` (+ HITL `task.create`) | read 自律 / write 人確認 |
   | `pr-review` | (stand-alone) | 「PR レビューして」「この差分どう?」 | `recall.search` + `decision.list` (`recorded_after/before`) + `task.list` + `graph.related` / `graph.trace` | 自律 OK |
   | `find-document` | (stand-alone) | 「Box にあったあの資料」「<キーワード>含むファイル」 | `search` (FTS5、Phase 12 H1) + 補助的に `recall.search` / `source.list` (`observed_after/before`) / `source.get` | 自律 OK |
   | `meeting-prep` | ↔ meeting-followup | 「来週の会議準備」「明日のミーティング前確認」 | `source.list` (`source_type=ms365_calendar` + `observed_after/before`) + `recall.search` + `graph.related` | 自律 OK |
   | `research` | (stand-alone) | 「`<X>` について調べて」「<トピック> 網羅的に」 | `recall.search` (semantic) + `search` (FTS5) + `graph.related` / `graph.expand` + `brief` | 自律 OK |
   | `external-brief` | ↔ personal-brief | 「上司向け週次報告」「クライアント向け進捗」 | `task.list` (`state=completed` + `updated_after`) + `decision.list` (`recorded_after`) + `brief` (外向き tone) | 自律 OK |
   | `decision-rationale` | (stand-alone) | 「あの決定はなぜ」「X を選んだ理由」 | `decision.list` (topic 絞り) + `graph.trace` + `recall.search` | 自律 OK |
   | `handoff-draft` | (draft family) | 「引き継ぎ書作って」「handoff 書く」 | `task.list` (`state=in_progress`) + `decision.list` + `recall.search` + `graph.related` (text-only、persist なし、ADR-0016 §決定 (l)(a)) | 自律 OK |
   | `announcement-draft` | (draft family) | 「リリース告知文書いて」「announcement」 | `recall.search` + `decision.list` (`recorded_after=last_release`) + `brief` (announcement tone、text-only、persist なし) | 自律 OK |

   **HITL write (4)**:

   | skill | pair | description トリガ | 使う MCP tool | 自律範囲 |
   |---|---|---|---|---|
   | `reply-draft` | (draft family) | 「返信案 / 下書き」 | `recall.search` + `propose.generate` (`reply_to_source_id`) + `propose.apply` (HITL、idempotent) | generate / apply とも人確認 |
   | `inbox-triage` | ↔ source-extract | 「受信箱整理」「inbox 仕分け」 | `inbox.list` (`state=open`) + `propose.generate` (`mode=inbox_triage`) + `propose.apply` (HITL) | generate / apply とも人確認 |
   | `source-extract` | ↔ inbox-triage | 「この資料から task 抽出」「<source_id> から候補」 | `source.get` + `propose.generate` (`mode=source_extract`) + `propose.apply` (HITL) | generate / apply とも人確認 |
   | `meeting-followup` | ↔ meeting-prep | 「会議後の action items」「議事録から task 抽出」 | `source.list` (`source_type=ms365_calendar` + `observed_after/before`) + `source.get` + `recall.search` + `propose.generate` (`mode=meeting_followup`) + `propose.apply` (HITL) | generate / apply とも人確認 |

   **Pair structure** (4 pair、host LLM の routing 精度向上のための対称軸):

   - `personal-brief` ↔ `external-brief`: 自分向け (粒度細かめ、進行中タスクも含む) ↔ 外向き (完了 + 確定 decision 中心、tone 制御)
   - `meeting-prep` ↔ `meeting-followup`: 会議前 (read-only、preparation context) ↔ 会議後 (HITL write、action items 抽出)
   - `inbox-triage` ↔ `source-extract`: 集合 (inbox 全体を一気に仕分け) ↔ 個別 (1 source から候補抽出)
   - `reply-draft` / `handoff-draft` / `announcement-draft`: draft family (返信 / 引き継ぎ / 告知、persist 境界は ADR-0016 §決定 (l)(a) で「返信元 source の有無」で切る = reply-draft のみ persist、handoff/announcement は text-only)

   **HITL boundary** (ADR-0022 §決定 (c) annotation policy):

   - read tools 12 + read 自律 OK skill 10 → host LLM auto-approve
   - write tools 5 (`task.create` / `inbox.add` / `connector.sync` / `propose.generate` / `propose.apply`) + HITL write skill 4 → host LLM が user 確認必須
   - auto-apply 経路は構造的に存在しない (ADR-0016 §決定 (c))
   - 外部 SaaS 書き戻し経路も構造的に存在しない (ADR-0010 §禁止事項 7)

   **MCP tool 依存マップ** (skill × MCP tool マトリクス、`docs/secretary-agent.md` §MCP tool 依存マップ で詳細表を保持):

   ```text
   read tools (12):
     recall.search       → personal-brief, next-actions, reply-draft, pr-review,
                           find-document, meeting-prep, research, decision-rationale,
                           handoff-draft, announcement-draft, meeting-followup
     task.list           → personal-brief, next-actions, pr-review, external-brief, handoff-draft
     inbox.list          → personal-brief, inbox-triage
     decision.list       → personal-brief, pr-review, external-brief, decision-rationale,
                           handoff-draft, announcement-draft
     brief               → personal-brief, external-brief, research, announcement-draft
     graph.related       → pr-review, meeting-prep, research, handoff-draft
     graph.trace         → pr-review, decision-rationale
     graph.expand        → research
     source.list         → find-document, meeting-prep, meeting-followup
     source.get          → find-document, source-extract, meeting-followup
     embeddings.find_duplicates → (現状未利用、agent host の自律判断で活用可)
     search (FTS5)       → find-document, research

   write tools (5、HITL):
     task.create         → next-actions
     inbox.add           → (現状未利用、host LLM の自律判断で活用可)
     connector.sync      → (現状未利用、operator が CLI から呼ぶことを推奨)
     propose.generate    → reply-draft, inbox-triage, source-extract, meeting-followup
     propose.apply       → reply-draft, inbox-triage, source-extract, meeting-followup
   ```

3. **skill security scan** (`tools/skill_scan.py`) — プロンプトインジェクション / コマンドインジェクション / ハードコード鍵 / データ持ち出し の 4 カテゴリ + frontmatter の隠しユニコード / 「ignore previous instructions」類のパターン検出。Phase 12 では 14 skills 全てに対して `tests/unit/skills/test_skill_specs.py` の per-skill MCP dispatch pin + skill security scan が走る。

詳細は [docs/secretary-agent.md](secretary-agent.md) を参照。

### 2.11c Office Document Extraction Layer (Phase 11)

[ADR-0025](adr/0025-office-document-content-extraction.md) で Word `.docx` / Excel `.xlsx` / PowerPoint `.pptx` の本文を Operational Memory へ取り込むための抽出層を導入した。本層は `src/opshub/core/document_extract.py` に閉じた 1 モジュール = `extract_document(path)` 1 関数で、Phase 11 F4 の box_drive / onedrive_drive scanner から呼ばれる。

**抽出経路** ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (a)):

- **markitdown** (Microsoft 公式 多形式 → markdown 変換) を唯一の抽出 backend に固定。`[office]` extras (`uv sync --extra office`) で `markitdown[docx,xlsx,pptx]>=0.1` を pull する。
- markitdown のインポートは `extract_document` の関数ボディ内に閉じ込め、cold-start path に extras が露出しないようにする (M6 guard ＝ `tests/integration/test_cold_start.py`、`opshub --help` ≤ 300 ms 維持)。

**Size cap + fail-safe** ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (b)/(c)):

| 不変条件 | 値 | 振る舞い |
|---|---|---|
| ファイル size 上限 | 50 MB (`max_file_bytes`、`opshub.toml [office] max_file_size_mb` で override) | 超過時 `body=None` + `skip_reason="file too large"` + `structlog.warning` |
| 抽出後 text 上限 | 500 000 chars (`max_chars`、`opshub.toml` 上書き可) | 超過時 head-truncate + `\n\n[truncated: original=<N> chars, limit=<M>]` 注記 |
| 抽出失敗 | markitdown exception (corrupted / password-protected / unsupported) | `body=None` + `skip_reason="extraction failed: <class>"` + sanitised warning。SourceObserved は metadata のみで発行継続 |

`extract_document` は **例外を漏らさない** (`ExtractResult.body is None` + `skip_reason` で結果を返す)。scanner の happy path は単一で済む。

**source_type 細分** ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (d)):

| 拡張子 | `source_type` |
|---|---|
| `.docx` | `word_document` |
| `.xlsx` | `excel_spreadsheet` |
| `.pptx` | `powerpoint_slide_deck` |

`SOURCE_TYPE_BY_EXTENSION` を `core/document_extract.py` 内に SSOT として置き、connector mapper はこの map を import する。文字列の二重定義を避けるため再定義禁止。

**ADR-0019 §不変条件 (b) との緊張解消**: ADR-0019 §決定 (b') で「`[connectors.<name>] content_extraction = true` opt-in 時に限り `core/document_extract.extract_document(path)` 経路のみ `open()` 許可」と境界を狭める例外節を追加。default は `content_extraction = false` で FS scan は `stat()` 完結を維持 (Phase 9 不変)。

**Phase 13 拡張: Workspace export 経路** ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (d') + (j))。Google Workspace native 形式 (`google_doc` / `google_slides` / `google_sheets`) は Drive API `files.export(fileId, mimeType=<MS Office mediatype>)` で bytes として取得し、`core/document_extract.extract_workspace_export(bytes, source_type)` で markitdown 経路に流す。3 形式とも MS Office mediatype 経由で統一 (Docs だけ markdown 直接 export を採ると `core/document_extract.py` の経路が `google_doc` のみ別分岐になり API 表面整合性が崩れるため)。`extract_document(path)` (Phase 11) と `extract_workspace_export(bytes, ...)` (Phase 13) はどちらも同じ markitdown 内部処理に合流し、size cap / chars cap / fail-safe / 切詰 marker は共通。Phase 13 G4 で connector が `content_extraction = true` opt-in 時にのみ起動し、metadata-only sync (`content_extraction = false`、Phase 13 G3 完了時挙動) では Drive `files.export` を呼ばないため markitdown cost は発生しない (`google_workspace_file` の catch-all source_type も permanently metadata-only)。

### 2.12 Workspace Generation Layer

projection を読み、markdown (tasks / briefings / reviews / handoffs / dashboards) を生成。read-only。Jinja2 で template 化。

### 2.13 Agent Runtime Boundary

agent は `opshub` CLI 経由で操作。直接 SQL / 直接 markdown 書き換え / event bypass は禁止。lefthook / CI で検出する。

## 3. データフロー不変条件

1. 状態変更は **必ず event を経由**。`UPDATE tasks SET ...` を書く場所はゼロ。
2. projection は event の **純粋関数**。同じ event 列から同じ projection が得られる (replay 性) ことを CI で検証。
3. workspace markdown は projection の純粋関数。`workspace generate` を 2 回実行しても同じ結果。
4. connector は event を append するだけ。projection 更新は services の責任。
5. lock 取得失敗 → コマンドは fail-fast。retry は呼び出し側の責任。

## 4. データモデル (初期)

詳細スキーマは `docs/data-model.md` (後日作成予定) を参照。本ドキュメントでは骨子のみ。

### 4.1 必須テーブル

| Table | 種別 | Phase | 概要 |
|---|---|---|---|
| `events` | authoritative | Phase 1 (✅ 実装済) | append-only domain event log |
| `tasks` | projection | Phase 1 (✅ 実装済) | current task state |
| `embeddings` | projection | Phase 1+2+3+4 (✅ 実装済) | 派生 semantic 索引メタ (`entity_type` / `entity_id` / `model_id` / `model_version` / `dim` / `created_at`)。実体 vector は `embeddings_vec_*` 仮想テーブル側に rowid で JOIN |
| `embeddings_vec_local` | virtual (vec0) | Phase 4 (✅ 実装済) | local backend (bge-m3 1024-dim) + voyage backend (voyage-3 1024-dim) の vector 本体 |
| `embeddings_vec_openai` | virtual (vec0) | Phase 4 (✅ 実装済) | OpenAI backend (text-embedding-3-small 1536-dim) の vector 本体 |
| `embeddings_vec_voyage` | virtual (vec0) | Phase 4 (✅ 実装済) | Voyage backend 専用 vec0 (現状は 1024-dim、Phase 5+ で並列保持時に分離) |
| `inbox_items` | projection | Phase 1+2 (✅ 実装済) | 未 triage queue |
| `decisions` | projection | Phase 1+2 (✅ 実装済) | 決定記録 |
| `work_sessions` | projection | Phase 1+2 (✅ 実装済) | 人間 / agent の execution session |
| `agent_runs` | projection | Phase 1+2 (✅ 実装済) | agent 実行記録 |
| `locks` | projection | Phase 1+2 (✅ 実装済) | coordination lock |
| `handoffs` | projection | Phase 1+2 (✅ 実装済) | agent 間 / 人 - agent 間の引き継ぎ記録 |
| `sources` | projection | Phase 1+2+3+9+10+11+13 (✅ 実装済) | external item の現在状態 (Phase 9 で `fingerprint` 列追加、`box_drive` の `f"{size}:{mtime_ns}"` 差分検出用、migration 0017 / ADR-0019 §決定 (d)。Phase 10 で `body` + `provenance_origin` + `provenance_trust` 列追加 (migration 0018) + FTS5 仮想テーブル `sources_fts` (migration 0019)、ADR-0020。Phase 11 で `teams_message` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` source_type discriminator 追加。Phase 13 で Google Workspace 由来 `google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` source_type discriminator 追加、ADR-0025 §決定 (d')) |
| `connector_cursors` | projection | Phase 1+2+3 (✅ 実装済) | 差分同期チェックポイント |
| `ingested_files` | projection | Phase 1+2+3 (✅ 実装済) | workspace inbox file ingest の content-hash 追跡 |
| `briefings` | projection | Phase 5 (✅ 実装済) | LLM briefing 結果 (`id` / `topic` / `scope` / `markdown` / `source_refs` JSON / `model_id` / `model_version` / `tokens_in` / `tokens_out` / `generated_at`)、再生成は新 row として追記 (event-sourced trace 維持) |
| `proposals` | projection | Phase 6 (✅ 実装済) | LLM proposal candidates (`id` / `topic` / `scope` / `briefing_id` / `candidates` JSON / `candidate_states` JSON (`pending` \| `applied` \| `rejected`) / `model_id` / `model_version` / `tokens_in` / `tokens_out` / `generated_at`)、`(proposal_id, candidate_index)` を natural key とした per-candidate state machine (ADR-0016 §決定 (d)) |
| `links` | projection | Phase 8 (✅ 実装済) | entity 間 graph 関係 (ADR-0017、4 自動抽出 + manual link CRUD、natural key `(from_*, to_*, link_type)` UPSERT、bidirectional 2 INDEX) |
| `projects` | projection | Phase 3+ | task / decision のグルーピング (lock scope は Phase 2 で予約のみ) |

### 4.2 Event 命名規約

- semantic: `TaskActivated` / `TaskBlocked` / `DecisionRecorded` / `SourceReferenced`
- 避ける: `TaskUpdated` / `RowChanged` のような generic CRUD 名

## 5. 用語タクソノミー (Glossary)

| 用語 | 物理 | 説明 |
|---|---|---|
| `Connector` | `src/opshub/connectors/<name>/` | 外部 SaaS との取り込みユニット |
| `Source Event` | `events` テーブル (type=`source.*`) | Connector が記録する変化 |
| `Source Entity` | `sources` テーブル | 外部アイテムの現在状態 |
| `Inbox Item` | `inbox_items` テーブル | 未 triage の operational queue |
| `Task` | `tasks` テーブル | triage から派生する人手対応単位 |
| `Decision` | `decisions` テーブル | 重要な判断ログ |
| `Link` | `links` テーブル | entity 間関係性 |
| `Work Session` | `work_sessions` テーブル | 作業セッション |
| `Agent Run` | `agent_runs` テーブル | agent 実行記録 |
| `Lock` | `locks` テーブル | coordination lock |
| `Cursor` | `connector_cursors` テーブル | 同期チェックポイント |
| `Operational Memory` | (概念) | Event Store + Projections + Graph + Vector の総体 |
| `Workspace` | `~/opshub/workspace/` (repo 外) | 生成されるユーザー / agent 可読サーフェス |

## 6. Multi-Agent Coordination

OpsHub は次を前提とする。

- Claude Code
- Codex CLI
- GitHub Copilot CLI
- Gemini CLI

複数 agent の同時実行を想定し、以下の概念を導入する。

- `Work Session`: 作業の上位スコープ (start - end)
- `Lock`: 競合回避。粒度は [ADR-0013](adr/0013-lock-granularity.md) で `task:<id>` / `project:<id>` / `global:` の 3 階層、owner = (actor, work_session_id)、fail-fast conflict semantics を採択
- `Agent Run`: 個別 agent の実行記録
- `Handoff`: agent 間 / 人 - agent 間の引き継ぎ記録

推奨フロー:

```text
acquire lock
→ inspect context
→ create plan
→ execute bounded change
→ record run summary
→ release lock
```

## 7. Workspace ディレクトリ構造

実 workspace は repo 外 (`~/opshub/workspace/` など) に置き、初期 seed のみ repo 内 `workspace/_template/` に持つ。

```text
workspace/
├── inbox/         # 未 triage の operational information
├── plans/         # 人間 / agent による execution plans
├── handoffs/      # agent 間 / 人 - agent 間 coordination
├── notes/         # 人手記述ノート
├── generated/
│   ├── tasks/
│   ├── briefings/
│   └── reviews/
└── runtime/       # runtime metadata / 一時 artifact
```

- `inbox/` / `plans/` / `notes/`: 人手記述 (event 化される)
- `handoffs/`: 人手 + 生成のハイブリッド
- `generated/`: 完全に disposable
- `runtime/`: gitignore 対象

> Phase 1 では `generated/tasks/` のみ実体化される。`inbox/` / `plans/` / `notes/` / `handoffs/` / `runtime/` および `generated/{briefings,reviews}/` は Phase 2-4 で順次追加。`opshub init` がこれらのディレクトリを自動作成するかは Phase 2 で決定する。

## 8. Security Principles

Phase 10 で本文保持に方針転換 ([ADR-0020](adr/0020-full-local-content-retention.md))。詳細は [principles.md §6](principles.md) と [ADR-0020](adr/0020-full-local-content-retention.md)、操作上の責務は [SECURITY.md](../SECURITY.md) を参照。

### 8.1 External Content Retention (Phase 10 改訂)

Phase 10 で本文をローカルに保持する方針 ([ADR-0020](adr/0020-full-local-content-retention.md)、ADR-0005 を supersede) に移行した。要約のみを残す旧方針は秘書ユースケース (横断検索 / 再要約 / 返信下書きの文体再現) で上流再取得を強要し、SaaS 側で本文が消えた瞬間に文脈を失う ＝ §1 Local-first と矛盾していた。

| 保持する | 保持しない |
|---|---|
| 外部 SaaS の本文 (Slack message body / GitHub issue body / Outlook 本文 / Box ファイル抽出テキスト) | credentials / access tokens (`core/secrets` + keyring 経由、本文ストアへの混入禁止) |
| external IDs | binary artifacts (画像 / 添付バイナリ — Phase 11 以降で抽出テキスト化を検討) |
| URLs / metadata | excludes 設定で弾かれた channel / sender / repo / path |
| 要約・action items | provenance タグなしの本文 (取り込み時に origin / trust 必須) |
| provenance タグ (`origin`/`trust`) |  |

#### 安全策のレイヤリング

1. **取り込み除外** (`~/.config/opshub/excludes.yaml`、ADR-0020 §(b)) — channel / sender / repo / path で connector の取り込み時点で弾く。秘密チャネルや個人用ファイルを最初から記憶に入れない。
2. **保存時暗号化** ([ADR-0021](adr/0021-encryption-at-rest.md)、`[storage] encryption = true` で opt-in) — SQLCipher が DB ファイルを AES-256 で暗号化。鍵は OS keychain (`db:encryption_key` slot) で管理 ([ADR-0014](adr/0014-saas-token-storage.md) 再利用、`OPSHUB_DB_ENCRYPTION_KEY` env override 可)。`tests/unit/core/test_encryption.py` で「暗号化無効 DB に本文が平文で残る」「鍵不在で `ConfigError`」を pin。
3. **provenance タグ** (ADR-0020 §(e)) — `provenance_origin = "external"` + `provenance_trust = "untrusted"` を connector が必ず付与。LLM / agent はこれを見て「本文中の指示文」を実行命令としては扱わない (ADR-0015 §決定 (f) の `<source>...</source>` 境界と同じ contract)。
4. **認証情報の本文からの分離** — SaaS トークンは `core/secrets` + keyring に置き、event payload / 本文 / log / tool result には流さない。MCP 層も `redact_secrets` で念押し ([ADR-0022](adr/0022-mcp-server-surface.md) §(b))。

#### Threat model 上の前提

OpsHub は **single-user / single-host / OS-level access control** が前提 ([SECURITY.md](../SECURITY.md) Scope)。本文をローカルに置く判断はこの前提下でのみ成立する。Multi-user host や untrusted workstation 上での運用は対象外。詳細な脅威モデル議論は [ADR-0020 §Negative](adr/0020-full-local-content-retention.md) を参照。

### 8.2 Agent Safety

Agent は以下を行わない。

- secret exfiltration
- direct production DB mutation
- audit logging bypass
- lock ignoring
- silent destructive operations

## 9. Phased Delivery

| Phase | 含むもの | 含まないもの |
|---|---|---|
| 1 | event store, tasks projection, CLI 骨格, markdown 生成, tests, CI | connector, vector, lock, triage |
| 2 | inbox triage, decisions, work sessions, locks, handoffs | connector, vector |
| 3 | Connector framework + GitHub connector + workspace inbox file ingest (✅ 2026-05-17 完了) | vector recall |
| 4 | vector recall, semantic search, duplicate detection (Pluggable Embedder + sqlite-vec、✅ 2026-05-17 完了) | briefing 自動生成 / event 駆動自動 embed (Phase 5) |
| 5 | Briefing layer (ADR-0015 + Pluggable LLM Anthropic / OpenAI + `opshub brief` + event-driven auto-embed 補助、✅ 2026-05-17 完了) | Local LLM backend / `links` projection 本実装 (Phase 6 / Phase 6.x) |
| 6 | Action loop layer (ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain + `opshub propose` CLI、human-in-the-loop apply 必須、✅ 2026-05-17 完了) | `llama.cpp` direct binding / proposal scoring / multi-step plans (Phase 6.x) |
| 7 | Connectors Wave 2 (Slack + Microsoft 365 + Box、ADR-0010 + ADR-0014 + ADR-0005 を再利用、✅ 2026-05-17 完了、epic #113) | additional connectors (Notion / Linear / Jira 等) / common OAuth helper refactor (Phase 7.x) |
| 8 | Knowledge graph layer (ADR-0017 + `links` projection (migration 0016) + 4 自動抽出経路 + manual link CRUD + `LinkService` traversal + `opshub link` / `opshub graph` CLI + `--expand-graph` integration、✅ 2026-05-17 完了、epic #128) | connector-side automatic `SourceReferenced` 発行 / graph visualisation web UI (Phase 8.x) / multi-machine sync (Phase 10+) |
| 9 | Local-filesystem-backed Connector Layer (ADR-0019 + `sources.fingerprint` 列 (migration 0017) + `box_drive` connector (scanner + mapper + connector + settings) + `core/platform.py` (WSL2 / macOS 判定) + `opshub connector sync box_drive` 経路、✅ 2026-05-23 完了、epic #187) | watch mode (filewatch backend) / 追加 FS connector (OneDrive / Dropbox / Google Drive for desktop / iCloud) / xattr identity / `opshub source list --stale` (Phase 9.x) / multi-machine sync (Phase 12+) |
| 10 | Secretary Agent Platform (ADR-0020 (full local content retention、ADR-0005 supersede) + ADR-0021 (encryption at rest、SQLCipher + keyring) + ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) + ADR-0004 改訂 (形A、opshub は MCP + Agent Skills のみ、runtime なし) + ADR-0016 改訂 (`ReplyDraftCandidatePayload`) + ADR-0017 改訂 (`reply_draft_replies_to` / `referenced_in_reply_draft` link types) + ADR-0010 改訂 (write-back 明示禁止) + 本文ベース embedding (migration 0018) + SQLite FTS5 (migration 0019) + `opshub search` CLI + `opshub mcp serve` CLI + 秘書 5 Skills (Phase 12 H1 で `personal-brief` / `next-actions` / `reply-draft` / `pr-review` / `find-document` に rename 済) + `tools/skill_scan.py`、✅ 2026-05-31 完了、epic #203) | MS Office 深掘り (Teams + Outlook 本文 + Word/Excel/PowerPoint 抽出、ADR-0025 = Phase 11) / 能動性段階 1-4 (cron 委譲 / 記憶キュレーション / 通知 / filewatch、Phase 12+) / multi-machine sync (Phase 12+) |
| 11 | MS Office 深掘り (ADR-0025 (Office Document Content Extraction、markitdown 経路 + 50 MB / 500K chars cap + source_type 3 種 `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` + fail-safe) + ADR-0019 改訂 (`content_extraction = true` opt-in 例外節 + onedrive_drive パターン汎化、§決定 (b') + (j)) + ADR-0010 改訂 (Teams connector + 本文抽出契約 + delta-link cursor + 失効時 full-pass fallback + Teams User Token principal) + `src/opshub/core/document_extract.py` + `connectors/teams/` (Graph delta + User Token) + `connectors/onedrive_drive/` (WSL2 `/mnt/onedrive` / macOS `~/OneDrive` platform default) + `connectors/box_drive` Office hook + `connectors/ms365/mapper` outlook body deep retention、✅ 2026-05-31 完了、epic #233) | 画像 OCR (PPT 内画像 / Office 図表、tesseract / pytesseract 経路、Phase 12+ defer) / 外部書き戻し (返信送信 = Teams 候補、新 ADR 要、Phase 12+) / 追加コネクタ (Google Workspace = markitdown 経路再利用 / Notion / Jira、Phase 12+) / multi-machine sync (Phase 12+) / 能動性段階 1-4 (Phase 12+) |
| 12 | Secretary Skills 拡張 (秘書 Skill レパートリーを 5 → 14 に拡張: 新規 9 = meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract + 既存 5 のうち rename 2 = daily-brief → personal-brief / file-lookup → find-document、区分: read 自律 OK 10 + HITL write 4 + pair structure 4 = personal-brief ↔ external-brief / meeting-prep ↔ meeting-followup / inbox-triage ↔ source-extract / draft family) + 4 新 MCP tools (`search` (FTS5、phrase-quoted default、`raw_query` flag は CLI 専用で MCP schema 除外、`ReadCategory.SEARCH`) + `propose.apply` (HITL、idempotent 正規化、`WriteCategory.PROPOSE_APPLY`、`destructive=false` + `idempotent=true`) + 既存 4 read tools の physical column ベース時間フィルタ = `task.list.updated_after/before` (`tasks.updated_at`) / `inbox.list.created_after/before` (`inbox_items.created_at`) / `decision.list.recorded_after/before` (`decisions.recorded_at`) / `source.list.observed_after/before` (`sources.observed_at`)、ISO 8601 半開区間) + 既存 5 SKILL.md を MCP 直接呼びに統一 (CLI fallback 廃止) + `propose.generate` の `mode` 引数追加 (`inbox_triage` / `source_extract` / `meeting_followup`、persist 経路を持つ 4 mode のみ) + ADR 改訂 3本 (ADR-0004 改訂 (Skills SSOT を opshub `docs/skills/` に移管 + §決定 (c-2) Skill catalog SSOT = `docs/secretary-agent.md` 独立条文化) + ADR-0022 改訂 (§決定 (f) 4 新 MCP tools 契約化) + ADR-0016 改訂 (§決定 (l) draft 系統一方針: persist 境界 = 返信元 source の有無 / `mode` 引数射程 / triage = reply_draft 専用 / Candidate union freeze)) + `docs/secretary-agent.md` を 14 skills 責務マップ SSOT に拡張 (§形A 責務分担 / §秘書への依頼例 / §Skill catalog (read 10 / HITL write 4) / §Pair structure / §HITL boundary / §MCP tool 依存マップ / §できること・できないこと / §セットアップ / §skill security / §関連)、✅ 2026-05-31 完了、epic #253) | `ozzy-labs/skills` 配布完成 (Phase 15+ defer、Phase 13 では Google Workspace コネクタ + Phase 14 では Gmail + Google Calendar コネクタが優先され配信機構は touch されなかったため Phase 14 audit 2026-06-01 で表記を Phase 15+ に統一、ADR-0004 §決定 (c) backout) / 削除候補 skills (agenda-builder / retrospective / weekly-plan / options-compare / risk-assessment、需要顕在化時に個別追加) / draft 系 persist 需要 (handoff-draft / announcement-draft が persist 要求時に ADR-0016 §決定 (f) versioning パターンで対応) / Phase 15+ = 統合・検索融合レイヤ / 能動性 / 外部書き戻し / Codex / Gemini / Copilot Skills 横展開 |
| 13 | Google Workspace コネクタ (Phase 11 で確立した markitdown 経路を Web API 経由で再利用、Google Docs / Slides / Sheets を Drive API v3 + OAuth Refresh Token + Workspace export → MS Office mediatype → markitdown で取り込む。改訂 3 本: ADR-0010 改訂 (§Phase 13 改訂 (e)-(h) = `google_workspace` 新コネクタ + Drive `files.watch` 禁止 + Workspace export 経路の本文抽出契約 + Drive `changes.list` cursor + TTL 失効時 full-pass fallback 義務 + Refresh Token principal = MS365 / Box pattern、Teams pattern とは別系統である旨を明文化) + ADR-0014 改訂 (§Phase 7 Validation rotation pin リスト 3 件目に `connector:google_workspace:refresh_token` 追加 + env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`) + ADR-0025 改訂 (§決定 (d') 新 source_type 3 種 `google_doc` / `google_slides` / `google_sheets` + §決定 (j) Workspace export 経路 = Drive API `files.export` → MS Office mediatype → markitdown 統一、`extract_workspace_export(bytes, source_type)` で API 表面拡張)。`connectors/google_workspace/` 5 module 構成 (`auth.py` paste-code OAuth + refresh token rotation 書き戻し / `client.py` Drive API v3 + httpx + rate-limit retry / `cursor.py` page token + TTL fallback / `mapper.py` Google mimeType → source_type 分岐 + provenance / `connector.py` content_extraction opt-in wiring + files.export 経由 markitdown 抽出) + `[connectors-google-workspace]` extras (httpx)。✅ 2026-05-31 完了、epic #274 | 画像 OCR (PPT 内画像 + Office 図表、tesseract / pytesseract、Phase 11 OQ7 / Phase 12 §9 / Phase 13 から繰り越し、Phase 14 を経て再 defer = Phase 15+) / 外部書き戻し (Drive write / Teams reply send、新 ADR 要、Phase 15+ 能動性段階で再評価) / 追加コネクタ (Notion / Jira / Linear / Confluence、Phase 13 から繰り越し = Phase 15+) / Google Workspace multi-account 対応 (OQ11、Phase 15+) / Drive `files.watch` + Gmail `users.watch` + Calendar `events.watch` push notification (Phase 15+ 能動性段階で grouping して再評価) / Drive Comments / Suggestions 取り込み (Phase 15+ へ移送、Phase 14 では着手せず) / multi-machine sync (Phase 15+) |
| 15 | Search 品質改善 (FTS5 日本語 tokenizer trigram 化 + 短クエリ LIKE fallback、ADR-0028 新規 + 改訂ゼロ。migration `0028_rebuild_sources_fts_trigram` で `sources_fts` を `unicode61 remove_diacritics 2` から FTS5 built-in `trigram` に物理張り替え + `sources.body` から back-fill + trigger 3 本再作成 (downgrade で元 tokenizer に復元)。`src/opshub/services/search_service.py` に `_MIN_FTS_QUERY_CHARS = 3` 閾値 + `_search_like_fallback` を追加し、1-2 文字短クエリは `LOWER(body) LIKE LOWER(?)` full scan に routing (NFC 正規化 + ASCII case-insensitive + LIKE wildcard escape + `raw_query=True` bypass)。cross-cutting fix = `tests/unit/mcp/test_phase12_handlers.py::_bootstrap_fts_index()` の seed tokenizer も `trigram` に同期 (production と MCP unit boundary 再整合)。`opshub search --help` の `--raw` 説明更新 + `docs/troubleshooting.md` §3.6 日本語 search 節追加。MCP `search` tool (ADR-0022) は契約不変で秘書 14 Skill (`find-document` / `research` / etc.) も透過的に恩恵を受ける。✅ 2026-06-02 完了、epic #338 | 形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab、Phase 15 で defer、trigram で operator 体験が不足する場合に ADR-0028 改訂 + tokenizer 評価) / dual index (unicode61 + trigram、Phase 15 で却下、BM25 ranking 劣化観測時に再評価) / `opshub search rebuild-index` 専用 CLI (projection rebuild 経由で十分か、需要顕在化時に切る) / MCP `search` tool 契約改訂 (`raw_query` operator 露出) / 検索結果の semantic re-rank (vector との hybrid score、別 ADR) / snippet() ハイライト / 検索クエリの NFKC 正規化 / 全角半角統一 (operator 観測されてから別 issue) は Phase 16+ |

詳細は [Principles 9 (Phased Delivery)](principles.md) 参照。

## Open Questions

1. 4.x で扱う `embeddings` テーブルのスキーマ (sqlite-vec への bind 方法を含む)
2. `Decision` テーブルと `Task` テーブルの関係 (Decision は Task の親か別 entity か)
3. `events` の partitioning / archive 戦略 (long-term 運用時)
4. multi-machine 利用 (将来 sync を許す場合の競合解決) — principles.md §Open Q #5 と同件、Phase 10+ で別 plan (Phase 9 = Local-filesystem-backed Connector Layer、epic #187 を先行で 2026-05-23 完了)
