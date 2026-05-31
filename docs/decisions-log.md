# Decisions Log (Rejected Alternatives)

> Status: Draft (in active design). Last reviewed: 2026-05-31.

設計フェーズで検討したが採用しなかった案の索引。詳細理由は対応する ADR / docs に記載。本ドキュメントは「あの議論はどこで結論が出たか」の早見表として機能する。

Phase 1 (foundation) は 2026-05-17 に完了。Event store (SQLite append-only) + tasks projection + `opshub` CLI (Typer ベース) + workspace markdown 生成 + Pluggable Embedder / VectorStore Protocol の境界確定 + Alembic migration 群 (0001-0008) + tests + CI を整備した。`@ozzylabs` ツール一式 (uv / ruff / mypy / pytest / pre-commit) の選定根拠は §4 で pin、event-sourced + projection アーキテクチャは §3 (ADR-0002 / ADR-0003) で pin。Phase 1 範囲では connector / vector / lock / triage は扱わず Phase 2-4 に持ち越した。

Phase 2 (coordination) も 2026-05-17 に完了。Inbox triage (`opshub inbox add/triage/list`) + decisions (`opshub decision record/list`) + coordination lock (`opshub lock acquire/release/list`、ADR-0013) + work session (`opshub session start/end`) + agent run (`opshub agent run begin/end`) + handoff (`opshub handoff open/close`) を event + projection + CLI の 3 層で追加した。Multi-Agent Coordination Layer の責務分離 (§6) と lock 戦略 (ADR-0013) は本 phase で確立。connector / vector は引き続き Phase 3-4 に持ち越し。

Phase 3 (connectors + workspace ingest) も 2026-05-17 に完了。共通 `Connector` Protocol + `ConnectorContext` + `SourceService` + `connector_cursors` projection + `core.secrets` keychain backend (macOS Keychain / Linux Secret Service 経由) を整備し、最初の具象として GitHub connector (`src/opshub/connectors/github/`) を実装。並行して `opshub workspace ingest` + `FileIngestService` + `ingested_files` projection で workspace 上の手書き `.md` を event 化する path を追加した。Connector Contract は本 phase で 1 connector 実装を経て validated され、ADR-0010 を Accepted に昇格 (Slack / Microsoft 365 / Box は同 contract に従って Phase 7 で追加)。SaaS token storage は §7 (ADR-0014) で pin、external content minimization は §3 (ADR-0005) で pin。

Phase 4 (semantic recall layer) も 2026-05-17 に完了。Phase 1 で freeze した `Embedder` / `VectorStore` Protocol 上に 3 backend (local `sentence-transformers` / OpenAI / Voyage) + sqlite-vec backed `SqliteVecStore` を実装し、`opshub embeddings rebuild/drain/status/find-duplicates` と `opshub recall` を追加した。Embed 対象は task title / decision text / inbox_item summary / source summary に限定 (ADR-0005 整合)。Embedding 戦略 (model 選定 / dim / 切替時の再 embed) は §11 (ADR-0012) で closeout。`embeddings` projection (`entity_type`, `entity_id`, `model_id`, `model_version`, `dim`, `created_at`) + backend ごとの sqlite-vec 仮想テーブル (`embeddings_vec_local` 1024-dim / `embeddings_vec_openai` 1536-dim / `embeddings_vec_voyage` 1024-dim) で rowid JOIN する設計を採用。briefing 自動生成 / event 駆動自動 embed は Phase 5 に持ち越し。

Phase 5 (briefing layer + Pluggable LLM + event-driven auto-embed 補助) は 2026-05-17 に完了。LLM 利用方針は §12 (ADR-0015) で closeout し、principles.md §Open Q #1 を §確定済み に移動した。

Phase 6 (action loop layer + Pluggable LLM structured output + Ollama backend + Proposal domain) も 2026-05-17 に完了。Action loop / structured output / Local LLM の方針は §13 (ADR-0016) で closeout し、ADR-0015 §決定 (a) deferred (Local LLM) を ADR-0016 §決定 (h) で closeout (Ollama 採用)。実装は PR #100 (ADR-0016) / #101 (Proposal events) / #102 (LLMClient.complete_structured Protocol 拡張) / #103 (Anthropic + OpenAI structured) / #104 (proposals projection + migration 0015) / #105 (OllamaLLMClient) / #106 (ProposalService) / #112 (`opshub propose` CLI) / #114 (closeout) の 9 PR で構成。

Phase 7 (Connectors Wave 2、MVP = Slack + Microsoft 365 + Box) も 2026-05-17 に完了。Phase 3 で確立した connector framework (ADR-0010 Connector Contract + ADR-0014 SaaS token storage + ADR-0005 External Content Minimization) を再利用し、3 SaaS connector を `connectors/<name>/` package 単位で auth + fetcher + mapper の 3 module 構成で追加した。新規 `source_type` discriminator は `slack_message` / `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` / `box_event` の 5 種類で、Phase 4 semantic recall + Phase 5 brief + Phase 6 propose は新 source_type を automatic に活用する (RecallService が `sources` projection 横断 query する設計のため)。実装は PR #115 (Slack auth + extras) / #121 (Slack fetcher) / #131 (Slack mapper + sync integration) / #116 (MS365 auth + OAuth + extras) / #120 (MS365 fetcher) / #130 (MS365 mapper + sync integration) / #118 (Box auth + OAuth + extras) / #119 (Box fetcher) / #129 (Box mapper + sync integration) / closeout PR (本コミット) の 10 PR で構成。ADR-0010 (Connector Contract) は本 phase で 3 connector 実装を経て validated (signature 変更なし、Validation セクションに Phase 7 確認を追記)。Phase 7.x 候補 (additional connectors / 共通 OAuth helper refactor / connector observability / `links` projection 本実装) と Phase 8 (Knowledge graph、epic #128) は principles.md §9 / docs/phase-7-plan.md §5 を参照。

Phase 8 (Knowledge graph layer、epic #128) は 2026-05-17 に完了。`links` projection (migration 0016) + 4 種類の自動 link 抽出 (`ProposalApplied` / `BriefingGenerated.source_refs` / `ProposalRequested.briefing_id` / `SourceReferenced`) + manual `opshub link add/remove` + `LinkService` traversal (`related` / `trace` / `expand`) + `opshub link` / `opshub graph` CLI + `--expand-graph` flag (Phase 5/6 backward-compat、default off) を 9 PR で実装した: #134 (A1 ADR-0017) / #136 (A2 links projection + migration 0016) / #135 (B1 link events + SourceReferenced closeout) / #138 (B2 LinksExtractor projector) / #137 (C1 LinkService related + trace) / #139 (C2 LinkService expand + bidirectional) / #140 (D1 link + graph CLI) / #141 (D2 brief/propose --expand-graph + graph expand wiring) / closeout PR (本コミット、E1)。8 決定 (links projection schema / link_type enum / 自動抽出は新 event を発行しない / manual link CRUD via events / traversal depth limits + cycle detection / `--expand-graph` default off / connector-side automatic SourceReferenced 抽出を Phase 8.x 持ち越し / `LinkDeleted` hard delete) は §14 (ADR-0017) で pin。Phase 3 で placeholder のままだった `SourceReferenced` event は本 phase で第一級に格上げされ `LinksProjector` で消費される (connector-side automatic 発行は Phase 8.x 持ち越し)。principles.md §Open Q #5 (Multi-machine sync) は本 phase では closeout しない (Phase 9 候補だが、本 ADR §決定 (c) auto extraction = pure derived state により follower 側で events から再 derive する選択肢が技術的に成立することは保証済)。

Phase 9 (Local-filesystem-backed Connector Layer、epic #187) は 2026-05-23 に完了。Phase 9 で OpsHub に 5 つ目の connector category を追加し、Box Drive デスクトップクライアント経由でローカル FS にマウントされた SaaS sync content を source として取り込む新パターンを §15 (ADR-0019) で pin した。9 決定: (a) `Connector` Protocol は変えず auth layer を OS-level Box Drive 認証への依存に置換、(b) Source は `os.stat()` metadata のみ参照し `open()` / magic bytes read を禁止 (CldAPI hydration 防止、ADR-0005 External Content Min の延長)、(c) Identity = `rel_path` (path-as-id、grep 可能形式、rename = 旧停止+新発火 MVP 制限を明記)、(d) Diff detection は `sources.fingerprint` 列 + scanner in-memory 比較 (`fingerprint = f"{size}:{mtime_ns}"`)、(e) 削除追跡なし (stale row は Phase 9.x `opshub source list --stale` 候補)、(f) `root_path` platform-aware default (WSL2=`/mnt/b` / macOS=`~/Box` / Linux native=未対応 / Windows native=POSIX-only 前提により対象外)、(g) Excludes は `opshub.toml` inline (`[connectors.box_drive] exclude_globs`)、共通 `~/.config/opshub/excludes.yaml` 機構化は Phase 9.x、(h) Operator precondition (`mountvol B: \\?\Volume{GUID}\` + `wsl --shutdown`) は opshub 範囲外、`docs/box-drive-setup.md` に外出し、(i) Watch mode (filewatch / inotify / FSEvents / CldAPI callback) は Phase 9.x 持ち越し (Phase 9 MVP は scan-only で identity 戦略を pin)。Phase 7 `box` connector (`source_type="box_event"`、Web API 経路) と Phase 9 `box_drive` connector (`source_type="box_drive_file"`、FS scan 経路) は二重取り込み許容で operator が独立に enable / disable 可能。実装は 5 PR で構成: PR #190 (A1 ADR-0019 起票) / PR #192 (A2 `sources.fingerprint` 列 + migration 0017 + `SourceObserved.fingerprint` field 追加 + `SourceService.observe(..., fingerprint=)` keyword arg) / PR #191 (B1 `core/platform.py` (WSL2 / macOS / Linux 判定) + `BoxDriveScanner` (`os.scandir()` + stat-only walk、`open()` 禁止 invariant の test pin、CldAPI non-hydration contract test opt-in)) / PR #193 (B2 `BoxDriveMapper` + `BoxDriveConnector` + `BoxDriveConnectorSettings` + `auth set connector:box_drive` の actionable error reject + registry 登録 + atomic failing-projector test) / closeout PR (本コミット、C1 = `opshub connector sync box_drive` e2e integration test + `docs/box-drive-setup.md` + ADR-0019 Validation セクション追記 + 関連 docs 更新)。準備 PR #188 で `docs/phase-9-plan.md` を起票済。Phase 10 候補は Multi-machine sync (principles.md §Open Q #5)、Phase 9.x 候補は watch mode (filewatch backend) / 追加 FS connector (OneDrive / Dropbox / Google Drive for desktop / iCloud) / xattr identity / `opshub source list --stale` / 共通 `excludes.yaml` 機構。

Phase 10 / Phase 11 / Phase 12 / Phase 13 / Phase 14 narratives は §19 (Phase 10 ADR-0022) / §19a (Phase 10 reply_draft) / §20 (Phase 10 ADR-0004 改訂 form-A) / §21 (Phase 11 ADR-0025 Office extraction) / §22 (Phase 11 ADR-0019 改訂) / §23 (Phase 11 ADR-0010 改訂) / §24 (Phase 12 ADR-0004 改訂) / §25 (Phase 12 ADR-0022 改訂) / §26 (Phase 12 ADR-0016 改訂) / §27 (Phase 13 ADR-0010 改訂) / §28 (Phase 13 ADR-0014 改訂) / §29 (Phase 13 ADR-0025 改訂) / §30 (Phase 14 ADR-0010 改訂) / §31 (Phase 14 ADR-0014 改訂) に詳細を集約。本節 (Phase 1-9 narrative) の続編としては以下を最低限の chronicle として残す。

Phase 10 (Secretary Agent Platform、epic #203) は 2026-05-31 に完了。本文ベース embedding (migration 0018) + SQLite FTS5 (migration 0019) + `opshub search` CLI + `opshub mcp serve` CLI + 秘書 5 Skills (Phase 12 H1 で `personal-brief` / `next-actions` / `reply-draft` / `pr-review` / `find-document` に rename 済) + `tools/skill_scan.py` を投入し、ADR-0020 (本文ローカル保持、ADR-0005 supersede) / ADR-0021 (保存時暗号化、SQLCipher + keyring) / ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) を新規 + ADR-0004 / 0016 / 0017 / 0010 を改訂した。詳細は §19 / §19a / §20、principles.md §6-§9。

Phase 11 (MS Office 深掘り、epic #233) は 2026-05-31 に完了。ADR-0025 (Office Document Content Extraction、markitdown 経路) を新規 + ADR-0019 / 0010 を改訂。`src/opshub/core/document_extract.py` + `connectors/teams/` (Microsoft Graph chat delta + User Token) + `connectors/onedrive_drive/` (FS scan、WSL2 `/mnt/onedrive` / macOS `~/OneDrive` platform default) + `connectors/box_drive` の Office 抽出 hook + `connectors/ms365/mapper` の Outlook body deep retention を投入。詳細は §21 / §22 / §23。

Phase 14 (Gmail + Google Calendar コネクタ、epic #292) は 2026-05-31 に着手。Phase 13 で確立した Google OAuth principal (`connector:google_workspace:refresh_token` keyring slot + offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern) を流用し、scope を `drive.readonly` から `drive.readonly + gmail.readonly + calendar.readonly` に拡張、`connectors/google_auth/auth.py` shared auth foundation (G1 plan の `google_common` 仮置きから G2 着手時に rename 採用、catch-all 化リスク回避) を抽出して 3 connector (Drive / Gmail / Calendar) で共有する設計を取る。Gmail = Gmail API History API delta + message 単位 (Outlook と symmetric)、Calendar = Calendar API `events.list(syncToken=...)` + master event only + override 別 record (MS365 Calendar と symmetric)。本文抽出は Outlook 流継承 (text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし)。ADR は **新規ゼロ + 改訂 2 本** (ADR-0010 = §30、ADR-0014 = §31) で完結 (Phase 11 / 12 / 13 流の単一改訂路線継続 = Phase 11 1 新規 + 2 改訂、Phase 12 0 新規 + 3 改訂、Phase 13 0 新規 + 3 改訂、**Phase 14 0 新規 + 2 改訂**)。新 source_type 2 種 = `gmail_message` (Outlook symmetric) + `google_calendar` (ms365_calendar symmetric)。能動性 (Gmail `users.watch` / Calendar `events.watch`) は Phase 13 Drive `files.watch` 禁止と同型で構造的に経路非存在 (形 A 整合)、外部書き戻し (Gmail send API / Calendar write API) も ADR-0010 §禁止事項 7 の自然延長として禁止。Wave 構成 (Wave 1 = G1 ADR + plan #293 → Wave 2 = G2 shared auth foundation 抽出 #294 → Wave 3 = G3 Gmail #295 + G4 Calendar #296 を並列 → Wave 4 = G5 closeout #297) で進行中。詳細は §30 / §31 と `docs/phase-14-plan.md` を参照。Phase 15+ 候補 = 画像 OCR / Drive Comments / Suggestions / メール添付の本文抽出 (Phase 13 §9 から Phase 14 を挟んで再 defer) / Notion / Jira / Linear / Confluence / 能動性段階 1-4 / 外部書き戻し / Calendar instance 展開 projection / Gmail thread aggregation projection。

Phase 13 (Google Workspace コネクタ、epic #274) は 2026-05-31 に完了。Phase 11 で確立した markitdown 経路を Web API 経由で再利用し、Google Docs / Slides / Sheets を Drive API v3 + OAuth Refresh Token (offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern) + Workspace export (MS Office mediatype) → markitdown で取り込む新コネクタ `google_workspace` を実装した。ADR は **新規ゼロ + 改訂 3 本** (ADR-0010 = §27、ADR-0014 = §28、ADR-0025 = §29) で完結 (Phase 11 流の単一改訂路線 = Phase 11 1 新規 + 2 改訂、Phase 12 0 新規 + 3 改訂、Phase 13 0 新規 + 3 改訂、と縮退継続)。新 source_type 4 種 = `google_doc` / `google_slides` / `google_sheets` (markitdown 抽出対象) + `google_workspace_file` (catch-all、metadata-only)。Teams pattern (verbatim user token + アプリ層 refresh なし) とは別系統であることを ADR-0010 §Phase 13 改訂 (h) で明文化し、両 pattern を ADR-0010 内に並立させた。実装は Wave 構成 (Wave 1 = G1 ADR + plan #275 → Wave 2 = G2 抽出 foundation + literal 公開 #276 + G3 OAuth + Drive API metadata + cursor + fallback #277 を並列 → Wave 3 = G4 Workspace export 統合 + body + provenance + content_extraction wiring #278 → Wave 4 = G5 closeout #279) で完了。Drive `files.watch` push notification は禁止 (形 A 整合、能動性混入防止)、`changes.list` poll のみに制限。外部書き戻し (Drive write API / `comments.create` / `permissions.*`) は ADR-0010 §禁止事項 7 の Google Workspace への自然延長として禁止 (構造的に経路非存在)。**Phase 13 事後監査 cluster A (#286) で ADR-0010 §Phase 13 改訂 (g) TTL fallback 実装の乖離を是正**: G3 (#277) 初期実装は「`getStartPageToken` で root token を取って "now" 以降だけ見る」shape で TTL 失効中の変更を 1 件も拾わない bug があった。Cluster A audit で Teams `_fallback_pass` 同型の 3-step recovery (WARNING log `connector.changes_list.expired` → `list_files_modified_since` full-pass emit → `getStartPageToken` で cursor 更新) に置換し、`GoogleWorkspaceConnectorSettings.fallback_window_days: int = 30` を `core/config.py` に追加。docs/google-workspace-setup.md / docs/upgrading.md の `fallback_window_days = 30` 設定行が初めて実装 backed になった (docs 先行・実装未追従の状態を解消)。詳細は §27 / §28 / §29 と `docs/phase-13-plan.md` を参照。Phase 14+ 候補 = 画像 OCR (Phase 11 OQ7 / Phase 12 §9 / Phase 13 から繰り越し) / Drive Comments / Suggestions 取り込み / Notion / Jira / Linear / Confluence (Phase 13 から繰り越し) / Drive `files.watch` 再評価 (能動性段階 1-4) / 外部書き戻し (緊張点③) / Google Workspace multi-account 対応 (OQ11) / `ozzy-labs/skills` 配布完成。

Phase 12 (Secretary Skills 拡張、epic #253) は 2026-05-31 に完了。秘書 Skill レパートリーを **5 → 14** に拡張し、4 新 MCP tools (`search` FTS5 + `propose.apply` HITL idempotent + 既存 4 read tools の physical column ベース時間フィルタ) を露出、既存 5 SKILL.md を MCP 直接呼びに統一、`propose.generate` の `mode` 引数追加 (`inbox_triage` / `source_extract` / `meeting_followup`) を ADR 改訂 3 本 (ADR-0004 改訂 = §24、ADR-0022 改訂 = §25、ADR-0016 改訂 = §26) で吸収した。新規 9 skills = meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract、既存 5 のうち rename 2 = daily-brief → personal-brief / file-lookup → find-document。区分は read 自律 OK 10 + HITL write 4、pair structure 4 = personal-brief ↔ external-brief / meeting-prep ↔ meeting-followup / inbox-triage ↔ source-extract / draft family。Skill catalog (14 skills 責務マップ + HITL boundary + MCP tool 依存マップ + pair structure) の SSOT は `docs/secretary-agent.md` を 10 § 構成に拡張して集約した (§形A 責務分担 / §秘書への依頼例 / §Skill catalog / §Pair structure / §HITL boundary / §MCP tool 依存マップ / §できること・できないこと / §セットアップ / §skill security / §関連)。実装は Wave 構成 (Wave 1 = H1 foundation #262 / Wave 2 = H2 info-gathering #265 / H3 analysis #264 / H4 HITL write #266 / H5 draft #263 を 4 並列 / Wave 3 = H6 closeout #259) + hotfix 2 件 (#267 ruff format + #268 mypy strict) で完了。新 ADR ゼロ、改訂 3 本 (ADR-0004 / 0022 / 0016) に縮退 (Phase 10 の 3 新規 + 4 改訂 → Phase 11 の 1 新規 + 2 改訂 → Phase 12 の 0 新規 + 3 改訂、と縮退継続)。`ozzy-labs/skills` 配布完成 (ADR-0004 §決定 (c) backout) と削除候補 skills (agenda-builder / retrospective / weekly-plan / options-compare / risk-assessment) の需要顕在化時の追加は Phase 13+ に defer。詳細は §24 / §25 / §26 と `docs/phase-12-plan.md` を参照。

## 1. Repository / Product 命名

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| `relay` | `opshub` | 機能が伝わらない | ADR-0008 |
| `inbox` | `opshub` | scope より狭い | ADR-0008 |
| `triage` | `opshub` | 「処理」側に寄りすぎ、保存・横断検索が読めない | ADR-0008 |
| `agenda` | `opshub` | npm `agenda` パッケージとの衝突 | ADR-0008 |
| `desk` | `opshub` | Zendesk のブランド連想 | ADR-0008 |
| `dispatch` | `opshub` | Netflix Dispatch 連想、能動的すぎ | ADR-0008 |
| `signal` | `opshub` | Signal Messenger SEO 衝突 | ADR-0008 |
| `agentic-opshub` (with prefix) | `opshub` | ゼロベース命名と不整合、長さ 14 字 | ADR-0008 |
| `Agentic OpsHub` (displayName のみ) | `OpsHub` | repo 名と displayName を乖離させると発見性低下 | ADR-0008 |

## 2. Description / Tagline 文言

| 却下表現 | 採用表現 | 理由 |
|---|---|---|
| `Pluggable connectors` | `Connectors` | `Connector` 単独で「組み合わせて使える単位」のニュアンスを内包。`Pluggable` は engineering jargon 寄り |
| `markdown store` | `single local store` | storage 形式 (markdown / SQLite / event log) を早期確定させない抽象表現 |
| `Event-sourced local workspace ...` を冒頭に置く案 | `Local-first operational memory and execution hub ...` | 実装語を先頭にすると「触る前に難解そう」と感じる読者がいる |
| `for multi-agent workflows` を主語 | `for humans and AI agents` | 単一 agent ユーザーを排除して見える |

## 3. アーキテクチャ / Storage

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Markdown を source of truth (Obsidian 流) | Markdown を workspace surface (projection) | クエリ性 / 整合性 / event-sourced との衝突 | ADR-0003 |
| CRUD ベース + 別途 audit log | Event-Sourced | 二重管理・replay 不能 | ADR-0002 |
| Hybrid (重要 entity のみ event-sourced) | 一律 event-sourced | 境界が曖昧化・複雑化 | ADR-0002 |
| Git-backed (markdown + commit を event 代わり) | SQLite append-only events | 構造化クエリに不向き、粒度制御困難 | ADR-0002 |
| CQRS + 別 DB | 単一 SQLite | over-engineering | ADR-0002 |
| `markdown 主体 + SQLite 索引` (初期検討) | Event Store + Projections + Workspace surface | より principled、Brief 採用 | ADR-0002, 0003 |
| Full body をローカル保持 | 最小化 (ID / URL / summary / metadata) | 機密 / TOS / 容量 / agent context 効率 | ADR-0005 (Phase 10 で **撤回**、§16 / §18 参照) |
| Encrypted local body 保持 | 最小化 | 機密性は担保できるが TOS / 容量問題は残る | ADR-0005 (Phase 10 で **撤回**、§16 / §17 / §18 参照) |

## 4. 言語 / スタック

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| TypeScript + drizzle + zod + better-sqlite3 + commander | Python + uv + Typer + SQLAlchemy + Pydantic | sqlite-vec Node binding 未成熟、Alembic 級 migration なし、local embedding 選択肢が狭い | ADR-0001 |
| TypeScript + Bun + libSQL + drizzle | Python | 単一バイナリ配布は魅力的だが Alembic 不在、sqlite-vec 参考実装薄い、Bun が若い | ADR-0001 |
| Rust + sqlx + clap | Python | 開発速度・AI 駆動開発相性・Phase 4 LLM/Embedding エコシステムで Python が優位 | ADR-0001 |
| Go + sqlc + cobra | Python | Pydantic 相当の validation が弱い、event-sourcing 参考実装が少ない、sqlite-vec Go binding 希少 | ADR-0001 |
| Python + Django / FastAPI | Python + Typer | Web フレームワーク不要 (CLI + 内部 SQLite) | ADR-0001 |
| Python + Click (Typer 不採用) | Python + Typer | Typer は Click 上位互換、Pydantic と統合しやすい | ADR-0001 |

## 5. Agent 連携

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| MCP-first (MVP から MCP サーバー提供) | CLI-first | context 常駐コスト・保守コスト | ADR-0006 |
| CLI + MCP を MVP から両提供 | CLI のみ | 二重実装で工数増 | ADR-0006 |
| REST API | CLI | ローカル利用に Web サーバー不要 | ADR-0006 |
| Python SDK のみ (CLI なし) | CLI | shell / cron / 他言語からの利用困難 | ADR-0006 |
| Agent に DB 直接アクセス権 | CLI / Service / Repository 経由 | audit / safety / coordination が崩れる | ADR-0004 |
| Read-only DB + write は CLI | すべて CLI | projection 構造変更時の prompt 修正コスト | ADR-0004 |
| Claude Code 単独サポート | 4 vendor 対等サポート | vendor lock-in リスク、ozzy-labs 全体方針との不整合 | ADR-0009 |
| Claude Code + 1 vendor (例: Codex CLI) | 4 vendor 対等サポート | 中途半端、追加コストわずか | ADR-0009 |

## 6. リポ / パッケージ構成

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Monorepo (uv workspace) を最初から | Single Python package | MVP overhead、境界未確定 | ADR-0007 |
| Connector のみ別 package | Single Python package | 中途半端、`optional-dependencies` で代替 | ADR-0007 |
| `docs/decisions/` | `docs/adr/` | SEO / adr-tools デフォルト | ADR-0000 |
| `agentic-` prefix を新カテゴリに適用 | 無接頭辞単語 1 語 | ゼロベース命名と不整合 | ADR-0008 |

## 7. Connector

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Connector が Task を直接生成 | inbox item 経由 + triage 必須 | 誤検知タスク、tasks テーブル膨張 | ADR-0010 |
| Connector が「重要そう」なイベントを Task 化 | inbox item 経由 + triage 必須 | 判断基準が SaaS / 用途で異なる | ADR-0010 |
| Inbox を経由しない直接 Triaged Event Stream | inbox item を projection として持つ | 未処理 / 処理済みの区別不能 | ADR-0010 |

## 8. ozzy-labs エコシステム

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Standalone リポとして運営 | エコシステムにフル参加 | bootstrap / agent 連携 / skill 配信を自前整備するコスト | ADR-0011 |
| commons は採用、skills は採用しない | skills も 4 vendor opt-in | 13 skill の自前実装コスト、ADR-0009 と矛盾 | ADR-0011 |
| commons を手動 fork | pinned + Phase 1 TODO | 継続同期の利益を失う | ADR-0011 |
| commons-python 別リポ作成 | commons 単一 | 共通部分は言語非依存、maintain コスト増 | ADR-0011 |

## 9. ツール

| 却下案 | 採用案 (暫定) | 理由 | 参照 |
|---|---|---|---|
| Taskfile | `just` | 知識 MCP の `tools/just` 整合 | ADR-0001, Open Questions |
| pre-commit (Python 標準) | lefthook | ozzy-labs 共通の lefthook 採用 | ADR-0001 |
| SQLAlchemy ORM | SQLAlchemy Core | event-sourced で aggregate を ORM map すると複雑化 | ADR-0001 |
| eventsourcing library | 自前実装 | append + reducer + 純粋関数 projector で十分 | ADR-0002 |
| 専用 graph DB | relational link table | OpsHub の規模で過剰 | Architecture 2.5 |

## 10. Description / Tagline の最終ぶれ

| 段階 | 表現 |
|---|---|
| 初版 | `Local-first operational memory and execution hub for humans and AI agents — aggregating GitHub, Slack, Microsoft 365, and Box into one markdown store.` |
| 改訂 1 (拡張性反映) | `... Pluggable connectors aggregate work signals — GitHub, Slack, Microsoft 365, Box, and more — into a single markdown store.` |
| 改訂 2 (storage 抽象化 + `Connectors` 単独化) | `... Connectors aggregate work signals — GitHub, Slack, Microsoft 365, Box, and more — into a single local store.` ← **採用** |

## 11. Embedding 戦略 (ADR-0012)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| API embedder のみ (OpenAI / Voyage 等) | Pluggable Embedder + VectorStore | local-first 違反、機密 summary の外部流出、コスト | ADR-0012 |
| Local embedder のみ (sentence-transformers 等) | Pluggable | 配布が ~500MB-2GB に肥大化、CPU 環境で初回 embed が遅い、品質の逃げ場なし | ADR-0012 |
| 抽象レイヤなし (Phase 4 で具象着手) | Phase 1 で Embedder / VectorStore Protocol 定義 | Phase 4 で `services/` / CLI / projection に破壊的改修 | ADR-0012 |
| 複数 vector store 並列 (sqlite-vec + LanceDB) | 単一 sqlite-vec | ADR-0002 単一 SQLite 原則違反、backup / replay 対象増 | ADR-0012 |
| 単一 model + version 列なし | `model_id` + `model_version` 列で増分 re-embed 可能に | モデル変更で全件 re-embed 必須、A/B 比較不能 | ADR-0012 |
| event payload も embed | summary 系のみ | event は immutable で量が多い、検索は projection で代替可能 | ADR-0012 |
| Hybrid (短期 API + 長期 local archive) | Pluggable で柔軟性 | 同一 entity が異 embedder で recall 結果不安定、切替ロジック複雑 | ADR-0012 |

## 12. LLM 利用方針 (ADR-0015)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| LLM 抽象なし (Anthropic / OpenAI SDK を BriefingService が直接呼ぶ) | Pluggable `LLMClient` Protocol + Anthropic / OpenAI 具象 | Phase 6+ の backend 追加で service に分岐が滲み、test stub 化も困難。ADR-0012 Pluggable Embedder と非対称になり ADR-0009 と衝突 | ADR-0015 |
| Local LLM (Ollama / `llama.cpp`) を MVP に含める | API backend 2 つ (Anthropic + OpenAI) のみ MVP、local は Phase 5.x | model ファイル 4-30GB で配布が壊れる、daemon 前提の cross-platform 問題、briefing 品質の validation 未実施 | ADR-0015 |
| Default backend = `anthropic` (or `openai`) | `disabled` を default | 片方 vendor 固定化で ADR-0009 と衝突、初回 install で認証エラーを踏む、Phase 4 embedding default と非一致 | ADR-0015 |
| Prompt を初手から外部ファイル化 (`~/.config/opshub/prompts/briefing.md`) | inline Python 定数 (Phase 5 MVP) | 1 flow のために loader + packaging を実装するのは scope creep、Phase 5.x で後付け可能 | ADR-0015 |
| Prompt injection 対策を system prompt の自然言語注意のみ | 明示 delimiter wrap (`<source id="...">...</source>`) + do-not-follow preamble | 「prompt で強く言えば従う」は誤り (知識 MCP), OWASP LLM01:2025 典型攻撃面 | ADR-0015 |
| Auto fallback (Anthropic → OpenAI) を MVP に含める | 自動 fallback なし、`OpsHubError` で caller に伝播 | 再現性 / cache invariant が壊れる、cost surprise、Protocol 層が複雑化 | ADR-0015 |
| API key を `[llm.<backend>] api_key_env` で config 駆動 | ADR-0014 (`core/secrets` + keyring + env override) 再利用、key 規約 `llm:<name>:api_key` | embedding (`embedder:<name>:api_key`) と規約統一、operator の mental model を 1 つに | ADR-0015 |
| `max_tokens` を Protocol で optional + default 持ち | `max_tokens` 必須引数 | caller が cost を把握できず observability 低下、backend ごとの reasonable default 差で Protocol 側の選定が lock-in | ADR-0015 |

## 13. Action loop / structured output (ADR-0016)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Grammar / JSON-mode constrained decoding (provider 固有 `response_format` 等) | provider-native tool calling (Anthropic `tool_use` / OpenAI `tools=` / Ollama OpenAI 互換 `tools=`) | JSON-mode は availability が provider 固有で portability が劣る、tool calling は 3 backend 同一概念モデル + multi-step proposal への拡張余地 | ADR-0016 |
| Free-form text + regex 抽出 | Pydantic v2 model を SSOT、各 client が JSON schema に serialize | regex は brittle、schema validation の二重化、prompt engineering で format 強制は遵守率不安定 | ADR-0016 |
| Single-shot markdown を人間が手で apply | `(proposal_id, candidate_index)` natural key + CLI apply 経路 | markdown には index が無く idempotent apply 不可、Action loop 自動化が薄まる、`ProposalApplied` event の意義が消える | ADR-0016 |
| Auto-apply mode (`opshub propose --auto-apply` / `[llm] auto_apply = true`) | human-in-the-loop 必須、Phase 6.x 以降も禁止 | ADR-0004 (Agent Runtime Boundary) と矛盾、LLM 生成 text が prompt injection / hallucination 経由で durable state に到達、OpsHub の core value (信用できる operational memory) と直接矛盾 | ADR-0016 |
| Apply 経路で entity event を直接 append (TaskService 不経由) | 既存 TaskService / DecisionService を経由 | Phase 1-2 で確立した validation / sanitisation が bypass、validation の 2 系統化、ADR-0005 (External Content Minimization) summary 制約も bypass | ADR-0016 |
| In-place migration (Phase 6.x で v1 candidate を v2 に rewrite) | `schema_version: Literal["v1"]` literal + 両 version 読み分け | ADR-0002 event immutability 違反、event log の audit trail / replay 整合性が崩れる、Pydantic discriminated union で type-safe に表現可 | ADR-0016 |
| `llama.cpp` direct (python binding) を MVP に含める | Ollama daemon 経由のみ MVP、`llama.cpp` direct は Phase 6.x | OS-specific binary install + model file 4-30GB が ADR-0001 配布制約 (`uv tool install opshub`) を破る、Ollama で 90% covered | ADR-0016 |

## 14. Knowledge graph (ADR-0017)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| link_type ごとに別 table (`applied_links` / `briefing_links` / `reference_links` / `manual_links`) | 単一 `links` table + `link_type` 列 + 2 INDEX (`links_from_idx` / `links_to_idx`) | schema 量増加 / index 重複 / bidirectional 全方向 traversal が UNION query 化 / 新 link_type 追加で毎回 migration が必要 (manual path の free-form link_type を吸収できない) | ADR-0017 |
| Graph database (Neo4j embedded / SQLite graph extension) | SQLite 単一 + 2 index | ADR-0001 配布制約 (OS-specific binary) と ADR-0002 (SQLite 単一 storage) 違反、Phase 8 MVP の graph 規模では SQLite + 2 index で十分 | ADR-0017 |
| Graph を JSON column として各 entity row に materialize (`tasks.outgoing_links` / `decisions.incoming_links` 等) | 単一 `links` table | state が分散 duplicate、bidirectional query が 6 entity table UNION、link add/remove で 2 entity を UPDATE、index 重複 | ADR-0017 |
| Connector-side automatic `SourceReferenced` 発行 (GitHub Issue body `#task-id` parse / Slack URL parse / MS365 attendee / Box file metadata) を Phase 8 MVP に含める | Phase 8 MVP は manual + 既存 event 4 経路のみ、connector-side 自動抽出は Phase 8.x | 4 connector × parsing rule の test 整備で 1 phase 分の作業量、manual baseline で運用 link 構造を pin する前に reactive extraction で table 汚染リスク、既存 4 経路で task → proposal → briefing → source の主要 chain は trace 可能 | ADR-0017 |
| Auto-extraction projector が `LinkCreated` event を派生発行 (manual も同 event 経由で events 経路統一) | Auto-extraction = pure derived state projector、manual `link add` のみ `LinkCreated` event を発行 | event log に同事実が 2 回記録 (元 event + 派生 event) で replay / audit 二重化、ADR-0002 single source of truth 違反、replay で派生 event 再発行 / skip 判断が必要で idempotency 複雑化、manual と auto-extracted の出自区別が events log で失われる | ADR-0017 |
| Soft delete (`links` table に `deleted_at` 列を追加) | Hard delete (`DELETE FROM links WHERE id = ?`) | event log で「いつ誰が delete したか」は `LinkDeleted` event で保全済、projection 側に重複 deleted state を持つ必要なし、deleted but queryable use case は events table 直接 query で代替可能、rebuild の冪等性が hard delete だと単純 | ADR-0017 |
| `--expand-graph` を default on | default off、opt-in flag | Phase 5/6 既存 test snapshot を破壊、graph 拡張 cost (prompt token 増) が operator の意図せず発生、graph 拡張の品質改善は Phase 8.x の運用 metric で validation してから default 化を議論 | ADR-0017 |
| In-place migration of historical Candidate v1 → v2 で graph payload を rewrite | event log immutable、両 version 読み分け (ADR-0016 §決定 (f) 踏襲) | ADR-0002 event immutability 原則違反、ADR-0016 §決定 (f) で既に同 pattern が pin 済、link_type enum 拡張も同 pattern を踏襲 | ADR-0017 |

## 15. Local-filesystem-backed Connector (ADR-0019)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| 既存 `box` connector を拡張 (`source_type="box_event"` のまま FS scan も追加) | 新 `box_drive` connector + `source_type="box_drive_file"` で分離 | Web API + FS walk の dual mode で settings / cursor / failure semantics が分岐し複雑化、`box_event` (event stream entry) と `box_drive_file` (FS 上の file) は意味が異なり同一 source_type だと識別不能、両経路を独立に enable / disable したい operator UX、ADR-0010 「1 connector → 1 service 呼び出し / 1 event 単位」 atomic 性原則と整合 | ADR-0019 |
| Filesystem-backed pattern を Phase 9 で `local_drive/` 共通基底として抽象化 | Phase 9 MVP は box_drive 専用、共通基底抽出は Phase 9.x で 2 vendor 目を実装する際に | 1 vendor 経験から抽象を抽出 (XP rule of three)、vendor ごとの quirks (OneDrive xattr / Dropbox smart sync / Google Drive 分岐 root / iCloud) を 1 vendor 段階で抽象化すると後で破壊的に変わる | ADR-0019 |
| Spike 経由で identity 戦略 (rel_path vs xattr vs Box item ID via local sidecar) を先に pin | spike 不採用、OS contract + 設計選択で rel_path 一本に確定 | xattr / ADS は Box Drive がサポートしない (placeholder への拡張属性書き込み経路なし)、local sidecar は Box Drive が同期対象にして clutter、rename 制限は spike しても解消されない、Phase 9 plan §6 で spike 不採用方針を pin 済 | ADR-0019 |
| 削除追跡を MVP に含める (`SourceDeleted` event + symmetric diff) | Phase 9 MVP は削除追跡なし (stale row は Phase 9.x `opshub source list --stale` 候補) | scanner 責務が walk + fingerprint 比較 + 削除検出 + event 発行に肥大化、event-sourced append-only で stale row が残るのが自然、operator が炙り出したい use case は Phase 9.x の `--stale` flag で対応可能 (YAGNI) | ADR-0019 |
| Content hash (SHA-256 等) を fingerprint に使う | `f"{size}:{mtime_ns}"` を fingerprint に使う | 内容 hash は本文 read を要するため ADR §決定 (b) `open()` 禁止 不変条件違反、CldAPI hydration を triggered する、size + mtime_ns は stat() のみで完結し agent 観点で「mtime が動いた = 変更」が正解 semantics | ADR-0019 |
| Watch mode (inotify / FSEvents / CldAPI callback / `watchdog`) を Phase 9 MVP に含める | Phase 9 MVP は scan-only、Watch mode は Phase 9.x で filewatch backend abstraction として | Watch mode を先に実装すると identity が rename event 保持で設計され rel_path 一本 + rename = 旧停止+新発火 MVP 制限が表現できなくなる、scan mode で 1 vendor 分の運用知見を蓄えてから filewatch を設計する順序が安全 | ADR-0019 |
| `mountvol` / `wsl --shutdown` 等の OS setup を opshub が自動化 | OS setup は opshub 範囲外、`docs/box-drive-setup.md` (C1 PR で新設) に外出し | `mountvol` は Windows PowerShell elevation 要、`\\?\Volume{GUID}\` の GUID 判定が opshub から不能 (Windows レジストリ参照要)、WSL2 再起動を opshub が trigger するのは UX 上問題 (他 process 巻き込み)、Phase 1-8 で確立した「opshub は SQLite + events + projection + CLI」境界と整合 | ADR-0019 |
| `~/.config/opshub/excludes.yaml` 共通機構を Phase 9 で実装 | Phase 9 は `opshub.toml` inline (`[connectors.box_drive] exclude_globs`)、共通機構は Phase 9.x | 共通機構を先に作ると 4 既存 connector の filter logic 同期移行で scope 肥大化、`opshub.toml` inline は config が 1 ファイル集約で operator UX 良好、Phase 9.x で導入時は両 sources を merge 経路で migration 可能 | ADR-0019 |
| `connector:box_drive` keyring key で OS Box Drive token を保管 | keyring 不使用、OS-level Box Drive 認証への依存に置換 (`opshub connector auth set box_drive` は actionable error で reject) | Box Drive は OS daemon が token を保持する経路で operator が Web Box ログイン済前提、opshub 側で token を二重管理する経路を持たない、ADR-0014 SaaS token storage は Web API 経路用 (Phase 7 4 connector) と切り分け | ADR-0019 |

## 16. Full Local Content Retention (ADR-0020、ADR-0005 supersede)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| ADR-0005 維持 (summary のみ保持を継続) | 本文をローカル保持 (ADR-0005 を Superseded) | Phase 10 の返信下書き / 本文検索が summary では成立しない、storage / context 懸念は Phase 4-8 設計 (recall で絞り込み context に full body を流さない) で別解済み、残る機密懸念は暗号化 + excludes + provenance で対処すべき問題で本文を持たないのは過剰制約 | ADR-0020 |
| 本文は別 content store (event は参照のみ) | 本文を `SourceObserved.body` に載せ event を SSOT に、projection (`sources.body`) へ materialise | event log だけで本文を再構築できなくなり ADR-0002 replayability 違反、別 store 消失で本文永久喪失、event ↔ body store の二重整合が append-only の単純性を破壊、個人スケールでは event payload に載せる単純解で十分 | ADR-0020 |
| ユーザーが source 単位で full / minimal を選ぶフラグ | excludes (観測前遮断) + 暗号化 (保存時) + provenance (利用時) の 3 層で統一 | 「何が機密か」を source 単位で判断するのは非現実的、schema が full / minimal で分岐し複雑化 (ADR-0005 §Alternatives #3 と同根) | ADR-0020 |
| provenance タグなしで本文保持 (緩和層を持たない) | sources に出自・信頼度タグを追加し低信頼外部本文を agent context へ渡す際に明示 | 外部本文をそのまま agent context へ流すと間接プロンプトインジェクションで秘書が乗っ取られる、本文保持に転換する以上攻撃面縮小を偶発的 (本文を持たない) から明示的 (出自・信頼度で区別) に設計し直すのは必須 | ADR-0020 |

## 17. Encryption at Rest (ADR-0021)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| アプリ層の列暗号化 (`sources.body` / event payload 列のみ) | SQLCipher で DB 丸ごと AES-256 暗号化 | 本文は event payload + projection + FTS index + embedding 一時データに波及し平文露出面を実装者が網羅し続ける必要がある (1 箇所漏れで平文がディスクに落ちる)、暗号化列は FTS で index できず本文検索 (Sub-issue B) と両立不能、DB 丸ごとなら波及先すべてが透過的に守られる | ADR-0021 |
| OS / FS レベル暗号化に委ねる (FileVault / LUKS / dm-crypt) | opshub が DB 単位の SQLCipher 暗号化を提供、鍵は keyring | operator 環境依存で opshub が保証できない (WSL2 / 非暗号化外部ボリューム / 一時マウント)、「opshub が本文を保持するなら保存時暗号化を提供する」責任境界、DB 単位の鍵なら opshub が鍵ライフサイクルを制御でき粒度が適切 | ADR-0021 |
| 暗号化なし + 機密本文を excludes で除外して運用回避 | excludes (取り込まない前段) と保存時暗号化 (取り込んだ本文の保護) を別レイヤーで両立 | excludes は取り込み前の防御で取り込んだ本文の保存時保護にならない、機密判定が完全でない以上保存時暗号化は別レイヤーとして必須、本文保持で default は `encryption = true` | ADR-0021 |
| DB 暗号鍵を専用の鍵管理 (新規 secret store) で保管 | ADR-0014 の keyring 経路を再利用 (`db:encryption_key` + `OPSHUB_DB_ENCRYPTION_KEY` env override) | DB 鍵も SaaS token も同じ keyring 経路で operator のメンタルモデルを一元化、opshub が鍵をディスク平文に書かない原則を継承、新規 secret store は重複 | ADR-0021 |

## 18. Body-based Embedding (ADR-0012 Phase 10 改訂)

ADR-0012 (Embedding Strategy) §4 の「embed 対象」を Phase 10 (Sub-issue B、本文ベース横断検索) に合わせて改訂。当初版 (2026-05-17) は ADR-0005 (External Content Minimization) 整合で `sources.summary` を embed し full body は対象外と pin していたが、ADR-0020 (Full Local Content Retention) が ADR-0005 を Superseded して `sources.body` が SSOT として保持されるようになったため、embed 元を本文ベースに切替える。

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| ADR-0005 整合維持 (`sources.summary` のみ embed) | `COALESCE(sources.body, sources.summary)` を embed | Phase 10 の返信下書き / 本文検索ユースケースで summary embedding では recall が不十分 (細部・固有名詞・依頼の機微が summary で抜け落ちる)、ADR-0020 で本文がローカル保持される以上 embed 元を本文に揃えるのが整合、historical row (body=NULL) は自動的に summary フォールバックで backward-compat | ADR-0012 改訂版 §4、Alternative #8 |
| 別 vector store (body 用 / summary 用) を並列運用 | 単一 vector store で `COALESCE` fallback | ADR-0012 §Alternative #4 (複数 vector store 並列) と同根の却下理由 (ADR-0002 単一 SQLite 原則 / backup / replay 対象増)、fallback chain で同じ index に同居させても entity ごとに最大 1 vector のため `(model_id, model_version)` UNIQUE 制約は壊れない | ADR-0012 改訂版 §4 |
| Body 長文を chunk + max pool で複数 vector 化 | 単純 head-truncation (Phase 10 MVP)、chunk 戦略は Phase 11+ で再評価 | Phase 10 step B2 の MVP scope を絞るため head-truncation で先行、chunk + pool は Open Q #2 で briefing 長文化と合わせて Phase 11+ で評価 | ADR-0012 §Open Q #2、改訂版 §4 |
| `provenance_trust=untrusted` 本文は embed 対象外 | 信頼度を問わず embed、agent context に流す段階で防御 | recall 性能と poisoning 防御を同じ層で混ぜると recall 取りこぼしが恒久化、ADR-0015 §決定 (f) do-not-follow preamble + ADR-0020 §(e) provenance タグで context 注入段階で防御層を分離するほうが責務として清潔 | ADR-0012 改訂版 §4、ADR-0020 §(e) |
| Body 切替時に既存 vector を invalidate しない (model_id 一致で skip) | `opshub embeddings rebuild --purge` で強制 re-embed | `model_id` / `model_version` は同じでも embed 元 text が summary→body に変わると vector の意味が変わる、operator が明示的に rebuild する経路を `embeddings rebuild` に乗せて backend 切替経路と同じ運用に統一 | ADR-0012 改訂版 §4、Phase 10 plan §3-B |

## 19. MCP Server Surface (ADR-0022)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| HTTP / Streamable HTTP transport も並列にサポート | stdio 一択・ネットワーク listen 禁止 | ネットワーク攻撃面 (confused deputy / SSRF / session hijack) が乗り本文保持 (ADR-0020) で増えた機密データ露出リスクと非両立、opshub は単一 operator・単一マシン (ADR-0002 / ADR-0003) で multi-host 要件が現時点で存在しない、将来 multi-machine sync 要件化時に別 ADR で議論可能 | ADR-0022 |
| Token Passthrough を許容 (tool 引数で SaaS トークンを受ける) | tool 引数で受けず opshub 内部で keyring (ADR-0014) から取得、戻り値から redact | トークンが LLM context / MCP 呼び出しログ / agent host transcript に流れ込み prompt injection / transcript 流出経路で漏洩、Anthropic MCP security best practices および MCP spec の "Token Passthrough is forbidden" 規定に反する、ADR-0014 で既に keyring 経由のトークン管理を確立済み | ADR-0022 |
| read / write tool 区別なし (全 tool を同一 namespace + 同一 annotation で expose) | read (自律 OK) / write (人確認推奨) を tool name namespace と MCP annotation (`readOnlyHint` / `destructiveHint`) で分離 | tool poisoning auto-approve 攻撃成功率 84% vs human-in-the-loop <5% の非対称が反映されず本文保持で拡大した indirect prompt injection 面が durable state 改変に直結、agent host が auto-approve 判断する手がかりがなく安全側 = UX 崩壊 / 緩い側 = 攻撃面最大化、宣言的境界が Phase 10 形A と整合 | ADR-0022 |
| Microsoft Agent Governance Toolkit 全体 / Agent Mesh / DID / trust score を導入 | policy-as-data (annotation で宣言) の発想のみ流用、重量機構は却下 | opshub は単一 operator・単一マシンの operational memory (ADR-0002 / ADR-0003) で multi-agent mesh / 分散 trust 機構の前提が成立しない、Agent Mesh / DID は agent 間 trust 協調機構で単一ホストでは過剰機構 (ADR-0001 配布制約とも非両立)、将来 multi-host 化で再評価 | ADR-0022 |
| Tool poisoning 緩和を agent host 側に全任せ (opshub は何もしない) | opshub 側で policy-as-data 宣言 + 将来の confirmation / dry-run 経路を予約 | annotation を honor しない agent host で write tool が auto-approve され durable state が破壊される経路を opshub 側で一切防げない、「①コアの境界を ①コア側で守る」のは ADR-0004 確立済みの責務 (CLI / service 層 validation と同じ) | ADR-0022 |
| MCP tool を CLI command 1:1 で機械生成 (低レベル粒度) | 読み取り系は CLI 同等粒度 (recall / search / brief)、秘書ユースケース粒度は Sub-issue D の Agent Skills で表現する二段構え | CLI は人間が叩く前提で sub-verb / flag が細かく agent の学習負荷が高い、秘書ユースケース粒度とずれて複数 tool 逐次呼び出しオーバーヘッドが大きい、MCP tool は ①コア operation を直接露出し組み立ては Skills で行う方が Phase 10 形A と整合 | ADR-0022 |
| MCP tool 呼び出しを opshub event log に append | structlog の JSON ログに OTel GenAI naming (`execute_tool`) で出力、event log は durable state 遷移のみに保つ | event log は ①コアの durable state 遷移を記録する SSOT (ADR-0002) で ②→① boundary trace を混ぜると event semantics が二重化、replay 時に MCP 呼び出し event を実行/skip 判断が曖昧で event-sourced replayability が崩れる、OTel exporter は opt-in extras で将来予約 | ADR-0022 |

## 19a. Reply Draft Generation (ADR-0016 / ADR-0017 / ADR-0010 Phase 10 改訂)

Phase 10 Sub-issue E (返信下書き生成) は **既存 ADR の改訂** で吸収し、新 ADR を立てない方針を 2026-05-30 に確定した。理由は Phase 6 propose lifecycle (generate → review → apply / reject) と Phase 8 knowledge graph (links + `--expand-graph`) と Phase 10 本文保持 (ADR-0020) + 本文 embedding (ADR-0012 改訂) が前提として揃っており、新 candidate kind (`reply_draft`) を 1 つ追加し link_type を 2 種足し write-back 不許可を contract に書くだけで Sub-issue E の DoD が成立するため。

| 改訂 ADR | 変更点 |
|---|---|
| ADR-0016 §決定 (i)+(j)+(k) 追加 | `ReplyDraftCandidatePayload`(`kind="reply_draft"`、`reply_to_source_id/type` 必須、schema v2) / triage 3 分類 (`respond`/`notify`/`ignore`) を `propose generate` の structured field に追加 (generate-time の prompt-hint signal にとどめ、persist しない / `Proposal` / `ProposalGenerated` event には triage を載せない / auto-apply 経路は構造的に閉じる、Phase 10 監査 Round 2 で明確化) / 文体は静的プロンプトでなく recall した「自分が author の過去送信 event」を `<style_example>` 注入 + 文脈は `--expand-graph` で `<context_source>` 注入 |
| ADR-0017 §決定 (b) 改訂 | link_type enum に `reply_draft_replies_to` / `referenced_in_reply_draft` の 2 種追加 (auto-extracted 全 7 種に拡張)。新 event 発行はせず ADR-0017 §決定 (c) pure derived state projector パターンを踏襲 |
| ADR-0010 §禁止事項 7 追加 + §Phase 10 改訂節 | 外部 SaaS への書き戻し (write-back) を当面 scope 外と明示。`post` / `send` / `comment` / `reply` 等のメソッドを connector に実装しない契約。`tests/` で経路非存在を contract test として pin |

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Reply-draft を新 ADR / 新 sub-system (`ReplyDraftService` + 専用 projection + `opshub reply ...` CLI) として独立 | Phase 6 propose lifecycle に `reply_draft` candidate kind を 1 つ追加して吸収 (ADR-0016 §決定 (i)) | Phase 6 で確立した generate→review→apply / triage / HITL / 既存 service 経由 / idempotent key / schema versioning が再利用可能、独立 sub-system は重複と CLI 表面の肥大化、`opshub propose generate --reply-to <source_id>` 1 経路で mental model が小さい (Sub-issue D 秘書 Skill 表とも整合) |
| Triage を separate API / separate event (`Triaged(source_id, classification)` + projection) に分離 | `propose generate` の structured output schema に `triage: Literal["respond","notify","ignore"] \| None` を載せる | LLM call の 1 段化で cost 倍を回避、triage を durable state にすると auto-apply 禁止原則 (ADR-0016 §決定 (c)) と緊張、「ノイズ source の自動破棄」は ADR-0020 §(b) excludes 経路の責務、LLM triage は post-hoc hint に留める |
| 文体を静的システムプロンプトに書く (Inbox Zero 流) | `author = self` 過去送信 event を recall して `<style_example>` ブロックとして注入、文脈は `--expand-graph` で `<context_source>` 注入 | テンプレ口調の暴走 (Inbox Zero の弱点) を回避、Sub-issue A 本文保持 + Sub-issue B 本文 embedding / FTS5 で hybrid recall が可能になった前提を活かす、Read AI Ada の自前 graph 相当を ADR-0017 §決定 (f) `--expand-graph` で代替 |
| 外部書き戻し (auto-send) を flag 1 つで有効化できる経路を Phase 10 で予約 | Phase 10 では実装しない、connector contract で経路の存在自体を禁止 (ADR-0010 §禁止事項 7) + test pin で「`post`/`send`/`comment`/`reply` メソッドが存在しないこと」を機械的に保証 | ADR-0016 §決定 (c) HITL 必須の延長、auto-send は prompt injection / hallucination が外部に伝播する経路を開く、構造的に経路が存在しない方が安全、将来再導入には新 ADR + ADR-0004 revisit + ADR-0016 §決定 (c) 整合の 3 要件すべてを要求 |
| Reply-draft 用に専用 projection (`reply_drafts` テーブル) を新設 | `proposals.candidates[i]` の JSON で代替、`(proposal_id, candidate_index)` natural key で reply_draft 状態を管理 | ADR-0002 single-source-of-truth (projection は increase せず) と整合、Phase 6 で確立した propose lifecycle を全継承、operator は `opshub propose list` / `apply` / `reject` の既存 verb で reply_draft も触れる |

## 20. Agent Runtime Boundary 改訂 (ADR-0004 Phase 10 form-A 吸収)

Phase 10 Sub-issue D (秘書 Agent Skills) で、ADR-0004 (Agent Runtime Boundary) を改訂し **形A (opshub は MCP + Agent Skills のみ提供、頭脳=runtime は外部ホスト)** を吸収した。新 ADR を立てず ADR-0004 改訂で対応した方針は 2026-05-30 に確定。理由は Phase 10 設計セッション §1 #3b で形A が既に確定済みで、本 ADR-0004 の boundary 不変条件 (auditability / safety / coordination 等) を継承したまま MCP を agent の正規書き込み経路として並列追加すれば足りるため。新 ADR は概念的二重化となる。

| 改訂節 | 変更点 |
|---|---|
| ADR-0004 §決定 (a) 形A 追加 | opshub は agent runtime (LLM 推論ループ / agent loop runner / 常駐 agent daemon) を持たない。提供する agent-facing surface は MCP サーバ (`opshub mcp serve` stdio) と Agent Skills (SKILL.md 標準) の 2 つに限定 |
| ADR-0004 §決定 (b) MCP 並列追加 | agent の書き込み経路一覧に MCP server を 2 番目として追加 (CLI と並列)。MCP 経路でも auditability / safety / validation / 認証情報の境界の不変条件は ADR-0022 §決定 (b)(c)(e) で保たれる |
| ADR-0004 §決定 (c) Agent Skills 配布 | 秘書 5 skill (daily-brief / next-actions / reply-draft / pr-review / file-lookup) は opshub 本体に同梱せず、`ozzy-labs/skills` リポに SKILL.md 実体を置き handbook ADR-0016 の `@ozzylabs/skills` Renovate preset 経由で配布。opshub 本体は仕様・catalog (`docs/secretary-agent.md`) と skill security scan ロジック (`tools/skill_scan.py`) のみ保持 |

| 却下案 | 採用案 | 理由 |
|---|---|---|
| 秘書層の boundary を新 ADR で起票 | ADR-0004 §決定 (a)(b)(c) として改訂吸収 | 既存 boundary 不変条件と同根 (auditability / safety / coordination / replayability)、新 ADR は概念的二重化、改訂で historical context (Phase 1-9 の CLI-first → Phase 10 の MCP 並列追加) が同一 ADR に残る |
| 形B: opshub に LangGraph / Claude Agent SDK 等の runtime を内蔵 | 形A (runtime を持たない) | runtime レイヤは資本投下と本番実績で固まる領域 (Phase 10 plan §10)、後発が勝つ見込み薄、vendor lock-in で ADR-0009 multi-agent neutrality と矛盾、外部ホストが既に runtime を持つため二重化 |
| 形C: opshub 独自の軽量 agent runtime を実装 | 形A | scope creep (prompt 管理 / tool registry / retry / streaming / cancellation / concurrent run 管理が必要)、operational memory 責務と乖離、MCP が標準クライアント機構として収束済みで形A が最小コスト |
| Agent Skills を opshub 本体に `share/skills/` 等で同梱し `opshub skills install` で配布 | `ozzy-labs/skills` preset 配布、opshub 本体は仕様 / catalog / scan ロジックのみ保持 | skill lifecycle と ①コア lifecycle が乖離、ozzy-labs エコシステムの skill 配布機構 (handbook ADR-0016) と二重化、複数ホスト間の skill 同期コスト、opshub install image 肥大化で M6 cold-start guard に影響 |
| skill security scan を `ozzy-labs/skills` 側 CI にのみ実装 | opshub 本体側にも scan ロジック (`tools/skill_scan.py`) を実装し本リポ内 skill 仕様にも適用テスト | 仕様変更時の skill 構造変更を本リポでも検出可能にする、二段検査で悪意ある skill が外部ホスト `.claude/skills/` に到達する経路を抑制、scan ロジック自体を opshub 本体に置くことで `ozzy-labs/skills` 側の CI 設定変更 (別 PR) を待たずに本リポでの spec 起点の検査を開始できる |

## 21. Office Document Content Extraction (ADR-0025)

Phase 11 Sub-issue F1 (2026-05-31) で Word `.docx` / Excel `.xlsx` / PowerPoint `.pptx` の本文を OpsHub Source として取り込むための抽出層を ADR-0025 で新規 pin した。`[office]` extras に閉じた markitdown 1 本経路 + 50 MB ファイル上限 + 500K chars 抽出後上限 + 抽出失敗 fail-safe (`body=None` + warning log) + 形式別 source_type 3 種 (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`) + Excel cells 上限 (10K/シート + 50K/workbook) + PowerPoint 本文 + speaker notes 両方含む (画像 OCR は Phase 12+ defer) を契約として確定。ADR-0019 §不変条件 (b) `open()` ban との緊張は ADR-0019 §決定 (b') opt-in 例外節 (`[connectors.<name>] content_extraction = true` 時に限り `core/document_extract.extract(path)` 経路のみ open 許可) で解消。実装は Phase 11 Sub-issue F2 (#235) で `src/opshub/core/document_extract.py` を新設予定。

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| `python-docx` + `openpyxl` + `python-pptx` を自前連結 | markitdown 1 本 | API 表面が形式ごとに肥大化、出力形式が ライブラリごとに異なり markdown 化変換層を自前実装、Microsoft 公式の markitdown が同等カバレッジを 1 API で提供 | ADR-0025 §決定 (a)、Alternatives #1 |
| `unstructured.io` 経由で多形式抽出 | markitdown 1 本 | 依存が重い (`nltk` / `pillow` / `pdfplumber` で 100MB 超)、内部 OCR 依存で ADR-0001 配布制約抵触、出力が `Element` リストで markdown 化に追加工程要 | ADR-0025 §決定 (a)、Alternatives #2 |
| 形式横断 1 タイプ (`office_document`) で source_type 統一 | 形式別 3 種 (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`) | 「Excel だけ recall」「PPT に絞った検索」等の operator UX 確保、agent skill 設計余地 (meeting-prep が PPT 優先 weight 等)、source_type 自由文字列のため 3 種追加コストはほぼゼロ | ADR-0025 §決定 (d)、Alternatives #3 |
| 抽出失敗時に SourceObserved を発行しない (fail-fast skip) | warning log + `body=None` で SourceObserved 発行継続 | metadata は valid 情報で抽出失敗で全捨ては過剰、後追い再試行の起点喪失、inbox から抽出失敗を operator が見えなくなる | ADR-0025 §決定 (c)、Alternatives #4 |
| 抽出キャッシュ table を Phase 11 MVP に含める | キャッシュなし (Phase 11.x 候補) | MVP scope 肥大化 (新 projection + migration + projector + test fixture)、Phase 11 MVP の主目的は経路確立で rebuild 性能は secondary、稀イベントとして許容可能 | ADR-0025 §決定 (i)、Alternatives #5 |
| ファイルサイズ上限 100 MB / 抽出後テキスト 1M chars に緩和 | 50 MB / 500K chars | 1 source が context window の 1/4 超で recall 全体が破綻、embedding cost 膨張、operator override で例外対応するほうが安全 default + 自由 escape hatch | ADR-0025 §決定 (b)、Alternatives #6 |

## 22. ADR-0019 改訂 (Phase 11 — content_extraction opt-in 例外節 + onedrive_drive 汎化)

Phase 11 Sub-issue F1 (2026-05-31) で ADR-0019 (Local-FS-backed Connector) を改訂し、Phase 9 で box_drive 専用に pin した §不変条件 (b) `open()` ban に **`content_extraction = true` opt-in の例外節** を §決定 (b') として追加した上で、Phase 11 Sub-issue F4-b (#237) の onedrive_drive 新設に向けた §パターン汎化節 (§決定 (j)) を追加。要点:

- **§決定 (b') opt-in 例外節**: `[connectors.<name>] content_extraction = false` (default) では §不変条件 (b) は完全維持、scanner は `stat()` のみで `open()` 一切禁止 (Phase 9 と挙動不変)。`content_extraction = true` の明示設定下でのみ、`core/document_extract.extract(path)` 経路 (= markitdown、ADR-0025 §決定 (a)) **のみ** open 許可。diff path (fingerprint 計算) は不変条件保持 (stat() 完結)、CldAPI / FSE hydration 抑制ガイドラインは「個別 file への単発 open」に限定する形で継続
- **§決定 (j) パターン汎化節**: Phase 11 F4-b で onedrive_drive を 2 vendor 目として追加するにあたり、box_drive / onedrive_drive 両方で成立する共通契約 (auth = OS-level / metadata = stat() / identity = rel_path / diff = fingerprint / 削除追跡なし / excludes = inline / operator precondition 外部化 / scan-only) を表で明文化。vendor x platform マトリクスとして `root_path` platform default 表に WSL2 `/mnt/onedrive` / macOS `~/OneDrive` を追記。`content_extraction` フックは両 connector に共通露出し、source_type は connector 名で分岐せず ADR-0025 §決定 (d) の 3 種を共通使用。共通基底 (`local_drive/base.py`) 抽出は XP rule of three の 3 vendor 目段階 (Phase 11.x+) まで持ち越し

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Phase 11 で `open()` ban を全面解除 | opt-in (`content_extraction = true`) の例外節として境界を絞る | Phase 9 で防いでいた network egress / cache 肥大化 / OS notification 暴発を最小化、Phase 9 operator は default false で無影響、scan walk の高速性 (stat() 完結) を維持、test invariant を「open は extractor 経由のみ」に進化させ構造的 guard を継承 |
| onedrive_drive を独立 ADR (ADR-0027 等) として起票 | ADR-0019 §パターン汎化節 (§決定 (j)) として吸収 | box_drive と onedrive_drive は 9 決定全て共有 (vendor 固有点は `root_path` platform default のみ)、独立 ADR は概念的二重化、Phase 11 plan §1 OQ3 で「ADR-0019 改訂で吸収」確定済 |
| Phase 11 で `connectors/local_drive/base.py` 共通基底を抽出 | コード共通基底抽出は Phase 11.x+ で 3 vendor 目と同時に再評価 | XP rule of three の 2 vendor 目で抽象化すると 3 vendor 目の quirks (Dropbox smart sync / Google Drive 分岐 root / iCloud Documents-only) で抽象が破壊的に変わるリスク、Phase 11 plan §3 F4-b 「box_drive を踏襲」と整合、設計レベルの汎化 (本節) は今やる / コード共通基底は後回しの分割で premature abstraction を回避 |
| `content_extraction` を connector 名で source_type 分岐 (`box_drive_office_doc` 等) | source_type は connector 名で分岐せず ADR-0025 §決定 (d) の 3 種を共通使用 | `/mnt/b/specs/api.docx` と `/mnt/onedrive/specs/api.docx` を同 source_type で扱うほうが Phase 4 recall / Phase 8 link traversal の query 表面が単純、`connector_name` + `rel_path` prefix で十分区別可能 |

## 23. ADR-0010 改訂 (Phase 11 — Teams + 本文抽出契約 + delta-link + User Token principal)

Phase 11 Sub-issue F1 (2026-05-31) で ADR-0010 (Connector Contract) を改訂し、Phase 10 改訂 (write-back ban、§禁止事項 7) を **保持したまま** 以下 4 点を加算追加。

- **改訂 (a) Teams 新コネクタ追加**: Phase 11 F5 (#238) の `connectors/teams/` connector を本 ADR の Connector Protocol + 責務 1-6 + 禁止事項 1-7 の契約対象に追加。Slack ADR-0018 / 既存 ms365 connector パターンに揃え、`source_type="teams_message"` で `sources` projection に persist
- **改訂 (b) 本文抽出契約**: local-FS-backed connector (box_drive / onedrive_drive) が Office 文書 (`.docx` / `.xlsx` / `.pptx`) の本文を取り込む経路を「連続 stat → 抽出 (`core/document_extract.extract`) → SourceObserved with body」として明示化。markitdown 1 本経路を ADR-0025 §決定 (a) で固定、connector が直接 `python-docx` / `openpyxl` / `python-pptx` を import / 呼び出すことは禁止。size / text / cells 上限と source_type 3 種は ADR-0025 で pin (connector ごとに独自上限を上書きしない)。text-only 本文取り込み (Slack / Outlook / Teams chat) は markitdown 経由を要さず mapper が直接 SourceObserved.body に載せる
- **改訂 (c) delta-link cursor + 失効時 full-pass fallback 義務**: Microsoft Graph delta query を cursor として使う connector (Phase 7 ms365 outlook / onedrive + Phase 11 teams) に、TTL 失効時 (`410 Gone` / `invalidatedDeltaToken`) の **自動 fallback** を義務化。WARNING log → 直近 N 日 (`fallback_window_days`、default 30、`opshub.toml` 上書き可) full-pass → 新 delta link 取得 → 次回 sync 差分 mode 復帰。fallback 自体失敗時は `ConnectorSyncFailed` (本 ADR §責務 4 整合)。Phase 7 既存 connector への適用は forward-compat (cursor 値は opaque string、TTL 失効検知時に fallback 起動、breaking change なし)
- **改訂 (d) Teams User Token principal**: Phase 11 F5 Teams connector の認証 principal を **User Token** に確定 (Slack ADR-0018 と同パターン)。Azure Portal App Registration で `Chat.Read` / `ChannelMessage.Read.All` 等 delegated permissions を operator が consent、MSAL device code / interactive flow で取得、ADR-0014 keyring 経路に **単一 slot** `connector:teams:token` で保管 (`src/opshub/connectors/teams/auth.py` の `TEAMS_TOKEN_SECRET_KEY`、User Token / 将来の Bot Token alternative は principal-neutral にこの 1 slot を共有)、env override は `OPSHUB_CONNECTOR_TEAMS_TOKEN`。Bot Token (Application permissions) は alternative として `docs/teams-setup.md` に記載するが default は User Token

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Teams を独立 ADR (ADR-0026 等) として起票 | ADR-0010 §Phase 11 改訂 (a) として吸収 | Phase 11 plan §1 OQ4 で「ADR-0010 改訂で吸収」確定済、Connector Protocol + 責務 1-6 + 禁止事項 1-7 を Teams にも適用する確認のみで独立 ADR は概念的二重化 |
| Teams Bot Token (Application permissions) を default principal | User Token (delegated permissions) を default、Bot Token は alternative | ADR-0018 Slack User Token と同根拠 (operator 1 名スケール、書き戻し非対応との整合、own context 自然表現)、企業 IT policy で User Token consent が阻まれる場合の退路として Bot Token を docs 記載 |
| connector が `python-docx` / `openpyxl` / `python-pptx` を直接 import / 呼び出し可能 | `core/document_extract.py` 1 module に markitdown 経路を集中化、connector からの直接呼び出しは禁止 | 3 ライブラリ分の error handling / size 上限 / fail-safe を connector 個別に実装すると Phase 11 改訂 (b) の契約 (size 上限 / fail-safe / source_type) が connector ごとに drift、`core/document_extract.py` 集中化で 1 経路に強制 |
| Graph delta-link 失効時に `ConnectorSyncFailed` で fail-fast (fallback なし) | 自動 fallback で直近 N 日 full-pass + 新 delta link 取得 | fail-fast だと operator が手動再実行するまで Teams chat の取り込みが完全停止、long-tail TTL 失効が標準運用に組み込まれており fail-fast は運用継続性を破壊、重複は SourceObserved の dedup で吸収可能で fallback の副作用は限定的 |
| `fallback_window_days` を hard-coded (固定 30 日) | `opshub.toml` operator override 可、default 30 日 | 1 年以上 outage 後の re-onboarding 等で `fallback_window_days = 365` 一時設定が必要、運用調整 escape hatch を残しつつ default は安全側 |

## 24. ADR-0004 改訂 (Phase 12 H1 — Skill SSOT を opshub に移管 + Skill catalog SSOT 確定)

Phase 12 H1 (2026-05-31) で ADR-0004 を改訂し、Phase 10 改訂時の前提 (`ozzy-labs/skills` preset 配布完成) を意図的に backout した上で、`docs/skills/<name>/SKILL.md` を opshub リポ内 SSOT として正式に認める。配信機構整備 (`ozzy-labs/skills` 側 CI + Renovate preset) は Phase 13+ に defer。同時に新 §決定 (c-2) として **Skill catalog SSOT = `docs/secretary-agent.md`** を独立条文化し、14 skills 体制 (Phase 10 で導入した 5 skill のうち `daily-brief` → `personal-brief` / `file-lookup` → `find-document` に rename された 5 + Phase 12 H2-H5 で追加される 9 = 計 14) の責務マップ / HITL boundary / MCP tool 依存マップ / pair structure を集約。Skill catalog を ADR 化しない判断 = 改訂頻度の高さと ADR の「決定の根拠」性質との相性悪さを優先。

| 却下案 | 採用案 | 理由 |
|---|---|---|
| `ozzy-labs/skills` 配布完成を待ってから Phase 12 へ進む | `docs/skills/` を SSOT として認め、配信機構整備は Phase 13+ に defer | Phase 10〜11 で `ozzy-labs/skills` 側 CI / preset 整備が未完、Phase 11 完了時点で 14 skill 体制への拡張を続けるためには preset 配布完成を待つ vs SSOT 移管の二択であり、後者の方が phase scope を膨らませない |
| Skill catalog (14 skills 責務マップ) を新 ADR (ADR-0026 等) として起票 | `docs/secretary-agent.md` を SSOT として集約、ADR 化しない | skill 追加 / rename / description 拡張は Phase 単位で何度も発生、ADR は「決定の根拠」を凍結するドキュメントタイプで頻繁な改訂と相性悪い、横断ビューの一元化として既に `docs/secretary-agent.md` が機能 |
| skill 単位の責務を `docs/secretary-agent.md` 1 ファイルに集約 | 個別 SKILL.md (責務縦) + secretary-agent.md (catalog 横) の 2 階層維持 | 個別 SKILL.md は host 側 skill loader が直接読む対象、catalog 集約だと skill 単位の load contract が成立しない |
| `daily-brief` / `file-lookup` の旧名を ADR / decisions-log / phase-10-plan からも sed 置換 | 歴史記録は旧名のまま注釈で対応、grep 0 ゲートはこれら 4 path を除外 | ADR は「いつ何を決めたか」の audit log、旧名で書かれた条文を改名すると過去の意思決定の文脈が読めなくなる、historical context を凍結したまま現行 doc / コード / テストは新名で運用する典型パターン |

## 25. ADR-0022 改訂 (Phase 12 H1 — search FTS5 + propose.apply HITL idempotent + 物理列ベース時間フィルタ)

Phase 12 H1 (2026-05-31) で ADR-0022 (MCP Server Surface) を改訂し、§決定 (f) として 4 つの surface 拡張を独立節として追加。既存 5 不変条件 (stdio 一択 / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) は完全維持。

- **(f-1) `search` 新規 read tool**: 既存 `SearchService.search` を MCP に露出。**`raw_query` flag は MCP schema から除外** (phrase quote default で host LLM の free-form token streams を安全に受ける)。`ReadCategory.SEARCH` 新設、annotation = `readOnlyHint=true, destructiveHint=false, idempotent=true, openWorldHint=false` (`recall.search` と同型)
- **(f-2) `propose.apply` 新規 HITL write tool**: 既存 `ProposalService.apply` を MCP に露出。**handler 層で `OpsHubError("already applied/rejected")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化** することで idempotent semantics を成立させる (本 ADR 史上初の `destructive=false` write カテゴリ)。event log を `aggregate_id=proposal_id` + `event_type=ProposalApplied` で scan して該当 `candidate_index` の `applied_entity_*` を取り出す経路で初回 apply と同一 payload を返す。`already rejected` および unrelated `OpsHubError` (not found / index 範囲外) は正規化せず MCP `isError` で上位伝播
- **(f-3) 既存 4 read tools の入力 schema 拡張**: `task.list` (`updated_after/before` → `tasks.updated_at`) / `inbox.list` (`created_after/before` → `inbox_items.created_at`) / `decision.list` (`recorded_after/before` → `decisions.recorded_at`) / `source.list` (`observed_after/before` → `sources.observed_at`) に **physical-column ベースの独立した時間フィルタ argument** を追加。半開区間 (`>= after` / `< before`)、ISO 8601 `date-time` string optional。annotation 変化なし
- **新 invariant 3 件**: (1) `_NON_DESTRUCTIVE_WRITES = frozenset({"propose.apply"})` を policy guard test で carve-out (他 write は `destructive=true` を強制継続) / (2) `search` input_schema の properties に `raw_query` が含まれない pin / (3) tool → (`*_after`, `*_before`) 写像表を pin

| 却下案 | 採用案 | 理由 |
|---|---|---|
| MCP `search` でも `raw_query` flag を露出 (CLI と対称) | MCP では除外、phrase quote default | host LLM が free-form token streams を投げる経路で FTS5 syntax 文字 (括弧 / コロン等) のエスケープが必要になり安全側に倒せない、CLI の power-user 経路は維持しつつ MCP は安全 default |
| `propose.apply` の idempotency を service 層で吸収 (CLI も同 semantics) | handler 層で OpsHubError catch + 正規化、CLI は従来通り fail-fast | CLI の operator は明示的に「2 回目です」と認識した上で再実行する想定、service 層を変えると CLI / MCP 双方の挙動を変えてしまい既存 CLI 動作を壊す、MCP 境界の annotation `idempotent=true` を契約として成立させる責任は handler 層が負う |
| 時間フィルタを business 概念 (`completed_after` 等) で命名 | physical-column 命名 (`updated_after` 等) | `task.list` の `completed_at` 列が projection に存在しない (state=completed + updated_after の組み合わせで近似)、business 命名は projection と乖離した瞬間に drift、物理列命名なら operator が「どの列を絞っているか」を意識せざるをえず混線が起きにくい |
| 全 tool で共通の `time_after` / `time_before` 命名で統一 | tool 別に独立命名 (`updated_after` / `created_after` / `recorded_after` / `observed_after`) | 共通命名は「どの projection の何の時間を絞るか」が tool 名と argument 名から自明にならない、tool 別命名でツール選択時点で対応列が明示される |

## 26. ADR-0016 改訂 (Phase 12 H1 — draft 系統一方針 §決定 (l) 独立条文化)

Phase 12 H1 (2026-05-31) で ADR-0016 (Action Loop and Structured Output) に §決定 (l) を追加し、Phase 12 で 14 skill 体制に拡張する際の draft 系全体の persist 方針 / `propose.generate` の `mode` 引数の射程 / triage の射程 / Candidate discriminated union freeze を独立条文として pin。

- **(a) persist 境界 = 「返信元 source の有無」で切る**: `reply-draft` は persist (`reply_to_source_id` が natural key)、`handoff-draft` / `announcement-draft` は **text-only** で persist しない。後者は host LLM が `brief` + `recall.search` + `source.get` + `decision.list` の read tool 群を合成して text を組み立てる経路で実装
- **(b) `mode` 引数の射程**: Phase 12 H4 で導入される `propose.generate` の `mode` 引数 (`inbox_triage` / `source_extract` / `meeting_followup`) は **persist 経路を持つ structured-output dispatch key に限定**。`handoff_draft` / `announcement_draft` は経由しない
- **(c) Triage 射程**: §決定 (j) の 3 値 triage (`respond` / `notify` / `ignore`) は **reply_draft 専用 signal**、`handoff-draft` / `announcement-draft` / `inbox-triage` 系は triage を持たない
- **(d) Candidate discriminated union freeze**: `Candidate = TaskCandidatePayload | DecisionCandidatePayload | ReplyDraftCandidatePayload` の 3 kind で freeze。新 candidate kind を追加せず、将来 persist 需要が顕在化したら §決定 (f) versioning パターンで対応

| 却下案 | 採用案 | 理由 |
|---|---|---|
| `handoff-draft` / `announcement-draft` も `reply-draft` と同様に persist (`HandoffDraftCandidatePayload` 等を追加) | text-only で persist しない | natural key が存在しない (自発生成、返信元 source なし)、proposal table に保存しても idempotency / 削除 / 編集の semantics が成立しない、使用頻度が週次〜月次でメリットが回収できない |
| Triage 3 分類を draft 系全体に共通の signal として位置づけ | reply_draft 専用 signal、他 draft type は独自体系 | `handoff-draft` (引き継ぎ書) / `announcement-draft` (告知文) で respond/notify/ignore 3 分類は意味をなさない、§決定 (j) の文面が "draft" を主語にしているため将来 misread されるリスクを本条文で明確化 |
| `propose.generate` の `mode` を全 draft type で使えるよう拡張 | persist 経路を持つ 4 mode に限定 | `mode=handoff_draft` を許すと structured output dispatch key と persist 経路の境界が曖昧化、host LLM 側で「mode を指定したのに persist されない」混乱が起きる |
| 新 candidate kind 追加に備えて Candidate union を open (Generic[CandidatePayloadProtocol] 等) | 3 kind で freeze、将来追加は §決定 (f) versioning パターンで対応 | open union は新 candidate kind 追加時に dispatch 分岐 / projection 拡張 / service 層分岐 / test fixture 全てを更新する metabolic load を hide、freeze + versioning パターンの方が「追加コストを払う Phase」を明示できる |

## 27. ADR-0010 改訂 (Phase 13 G1 — Google Workspace 追加 + Drive `changes.list` cursor + TTL fallback + Refresh Token principal = MS365 / Box pattern 明文化)

Phase 13 Sub-issue G1 (2026-05-31) で ADR-0010 (Connector Contract) を改訂し、Phase 10 改訂 (write-back ban、§禁止事項 7) と Phase 11 改訂 (a)-(d) を **保持** したまま以下 4 点を加算追加。Phase 12 plan §9 で forecast していた独立 ADR-0026 (Google Workspace connector) は **立てない** (Phase 11 流の単一改訂路線を踏襲)。

- **改訂 (e) Google Workspace 新コネクタ追加**: Phase 13 G3 (#277) の `connectors/google_workspace/` connector を本 ADR の Connector Protocol + 責務 1-6 + 禁止事項 1-7 の契約対象に追加。Drive API v3 `changes.list` 経由で Docs / Slides / Sheets の metadata + delta を fetch、`source_type="google_doc"` / `"google_slides"` / `"google_sheets"` で persist。**Drive push notification (`files.watch`) 禁止** を §禁止事項拡張として明文化 (能動性混入防止、形 A scope 抵触)、`changes.list` poll のみに制限
- **改訂 (f) Workspace export 経路の本文抽出契約**: Drive API `files.export` で Google native fmt → MS Office mediatype (docx / pptx / xlsx) → markitdown 経路を契約として pin。Phase 11 改訂 (b) の local-FS-backed connector 経路に対する Web API 経路の対応物。markitdown 1 本経路 (ADR-0025 §決定 (a)) を保持、Google ネイティブ markdown export は使わない (Sheets / Slides の markdown 直接 export 非対応で API 表面分岐リスク)
- **改訂 (g) Drive `changes.list` cursor + TTL fallback 義務**: Phase 11 改訂 (c) の Microsoft Graph delta-link + 失効時 full-pass fallback と完全同型を Drive API `changes.list` page token にも適用。TTL 失効時 (`400 invalidToken` / `404 startPageToken expired` / `410 Gone`) → WARNING log → 直近 N 日 (`fallback_window_days`、default 30) full-pass + 新 start page token 取得 → 次回 sync 差分 mode 復帰。fallback 自体失敗時は `ConnectorSyncFailed`
- **改訂 (h) Google Workspace User Token principal = MS365 / Box pattern**: Refresh Token + offline access + 自前 refresh + rotation 書き戻し pattern を確定。**Teams pattern (verbatim user token + アプリ層 refresh なし) とは別系統である旨を明文化** し、両 pattern が ADR-0010 内に並立することを 5 行 (token slot / アプリ層 refresh / rotation 書き戻し / rotation pin test / env override) × 2 列 (Google / Teams) の対比表で凍結。keyring slot `connector:google_workspace:refresh_token` + env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`、rotation pin test (`test_get_access_token_persists_rotated_refresh_token`) を MS365 / Box と同型で配置 (G3 DoD)

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Google Workspace を独立 ADR (ADR-0026 等) として起票 | ADR-0010 §Phase 13 改訂 (e) として吸収 | Phase 11 流の単一改訂路線を踏襲 (Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、と縮退継続)、Connector Protocol + 責務 1-6 + 禁止事項 1-7 を Google Workspace にも適用する確認のみで独立 ADR は概念的二重化、Phase 13 plan §1 OQ4 で「ADR-0010 + ADR-0014 + ADR-0025 の 3 改訂」を確定 |
| Teams pattern (verbatim user token + アプリ層 refresh なし) を Google Workspace にも採用 | MS365 / Box pattern (Refresh Token + 自前 refresh + rotation 書き戻し) を採用、Teams pattern とは別系統で並立 | Google Drive API access token は documented 1 hour TTL で短命、refresh token を offline access 取得で受けてアプリ層 refresh するのが Google OAuth 2.0 の標準運用、verbatim token のみ pattern では token 失効時に毎回 paste-code flow が必要で operator UX が極端に劣化、両 pattern を ADR-0010 内に並立明文化することで Phase 11 既存資産 (Teams) と Phase 13 新資産 (Google Workspace) の principal 選択根拠を将来も読める形で凍結 |
| Drive `files.watch` (push notification) で能動的に変更検知 | `changes.list` poll のみに制限、`files.watch` を connector に実装しない | 能動性混入は形 A (Phase 10 ADR-0004 改訂で確定) と直接抵触、構造的に経路を不在にすることで「flag 1 つで緩める」リスクを排除、Phase 14+ 能動性段階で再評価 |
| `changes.list` TTL 失効時に `ConnectorSyncFailed` で fail-fast (fallback なし) | 自動 fallback で直近 N 日 full-pass + 新 start page token 取得 | fail-fast だと operator が手動再実行するまで Google Workspace 文書の取り込みが完全停止、long-tail TTL 失効が標準運用に組み込まれており fail-fast は運用継続性を破壊、Phase 11 改訂 (c) の Teams fallback パターンを再利用することで operator メンタルモデルを 1 つに保つ |
| Drive write API (`files.update` / `comments.create` 等) を connector に実装し将来書き戻しに備える | Drive write API を connector に実装しない、ADR-0010 §禁止事項 7 (write-back ban) を Google Workspace に自然延長 | 「将来のために code path を作る」は ADR-0010 §禁止事項 7 (構造的に経路を不在にする原則) と直接抵触、書き戻しが必要になった時点で新 ADR で改訂 (Phase 11 §以降の再評価条件と同パターン) |

## 28. ADR-0014 改訂 (Phase 13 G1 — §Phase 7 Validation rotation pin リストに google_workspace 追加)

Phase 13 Sub-issue G1 (2026-05-31) で ADR-0014 (SaaS Token Storage) を改訂し、§Phase 7 Validation 節の **OAuth refresh token rotation の永続化** pin test リストに **`tests/unit/connectors/google_workspace/test_auth.py::test_get_access_token_persists_rotated_refresh_token`** を MS365 / Box に続く 3 件目として追加。keyring slot `connector:google_workspace:refresh_token` + env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` を pin。Phase 7 keyring keys 一覧にも Phase 11 Teams (5 件目) + Phase 13 Google Workspace (6 件目) を追記し、両 phase の追加経緯を本 ADR 内で凍結。

- **rotation pin test 追加の必要性**: Google OAuth 2.0 Refresh Token は documented rotation を行う (毎 access token 取得 / refresh で新 refresh token が返り得る)、書き戻し忘れは次回 refresh での失効を意味する → MS365 / Box で確立した「rotation 書き戻し forget regression 防止」test pattern を Google Workspace にも適用すべき
- **ADR-0010 §Phase 13 改訂 (h) との対応**: ADR-0010 で「Google Workspace = MS365 / Box pattern」を確定したため、token storage 経路の test pin も MS365 / Box と同型で配置するのが自然な帰結
- **本 ADR §Decision (薄ラッパー + 規約ベースの key 命名 + env var override + `secrets` extras 隔離) は無変更**: signature 変更ゼロ、Phase 13 改訂は Validation 節への pin test 追加のみ

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Google Workspace 用に新 ADR (ADR-0028 等) で token storage 経路を起票 | ADR-0014 §Phase 7 Validation 節への rotation pin リスト追加で吸収 | ADR-0014 の Decision (keyring + 規約 key + env override + extras 隔離) は MS365 / Box / Slack / GitHub で 6 件目の追加でも崩れない (Phase 7 validation で確認済)、新 ADR は概念的二重化 |
| Google Refresh Token を verbatim 保管 (rotation なし pattern、Teams 同様) | MS365 / Box 同様 rotation 書き戻し pattern + pin test 追加 | Google OAuth 2.0 仕様で rotation が前提、verbatim では Phase 13 plan §Alternatives #6 と同じく operator UX 劣化 |
| pin test を G5 closeout でまとめて追加 | G3 (#277) DoD で本 pin test を必須にする | G3 で auth.py 実装と同時に pin test を配置することで「実装と同期して test が育つ」原則を維持、G5 まで先送りすると implement 段階の TDD サイクルが切れる |

## 29. ADR-0025 改訂 (Phase 13 G1 — 新 source_type 3 種 + Workspace export 経路)

Phase 13 Sub-issue G1 (2026-05-31) で ADR-0025 (Office Document Content Extraction) を改訂し、§決定 (d) (形式別 3 種 `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`) を **保持** したまま、§決定 (d') (Google Workspace 由来 3 種 `google_doc` / `google_slides` / `google_sheets`) + §決定 (j) (Workspace export 経路) を加算追加。

- **(d') 形式別 3 種を加算追加**: Drive API が返す Google mimeType (`application/vnd.google-apps.document` / `.presentation` / `.spreadsheet`) を connector mapper で `google_doc` / `google_slides` / `google_sheets` source_type に正規化。MS Office 系 3 種 (§決定 (d)) と分離することで operator が「Workspace 由来か Office 由来か」を query レベルで区別可能 (find-document 自然文 query との整合)
- **(j) Workspace export 経路**: Drive API `files.export(fileId, mimeType=<Office mediatype>)` でバイナリ取得 → markitdown 経由抽出。**3 形式とも MS Office mediatype 経由で統一** (Docs だけ markdown 直接 export を採ると `core/document_extract.py` の経路が `google_doc` のみ別分岐になり API 表面整合性が崩れる)。`core/document_extract.extract` の API 表面を `path_or_bytes` 形式に拡張 (Phase 11 は path-only)、source_hint 引数を追加
- **既存 §決定 (a)-(i) は保持**: markitdown 1 本経路 / 50MB + 500K chars cap / fail-safe / Excel cells 上限 / PPT notes / ADR-0019 §不変条件 (b) 共存 / provenance / 抽出キャッシュなし、すべて Phase 11 から無変更で継承
- **OQ9 closeout (Phase 13 audit 2026-05-31 で確定)**: Phase 13 plan §8 OQ9 (ADR-0025 cap (50MB/500K chars) の Workspace export 適合性) は G2/G4 実測の結果 **closed**。代表 Sheets→xlsx / Docs→docx / Slides→pptx sample の export size 分布はすべて共通 cap 内に収まり、`[office.google_workspace] max_file_size_mb` separate override は **未導入**。`[office]` 単一 knob が Box Drive / OneDrive / Google Workspace の 3 経路を lockstep で governing する Phase 11 audit Cluster B two-key composition (§決定 (g)) を Workspace export 経路にもそのまま適用、操作面の単純さを優先。下表の 4 件目 (size cap 却下案) は当時の forecast snapshot として残置、本 closeout で OQ9 の決着を補足。

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Google Workspace 3 種と Office 3 種を 1 タイプ (`office_document`) に統合 | 形式別 6 種に分離 | 「Workspace 由来か Office 由来か」を operator が query レベルで区別可能、find-document 自然文 query との整合、本 ADR §Alternatives #3 (Phase 11 で却下) と同根拠 |
| Docs だけ `text/markdown` 直接 export、Sheets / Slides は Office mediatype 経由 | 3 形式とも MS Office mediatype 経由で統一 | `core/document_extract.py` の経路が `google_doc` のみ別分岐 (markdown export → そのまま body) になり、Sheets / Slides との API 表面整合性が崩れる、3 形式統一の方が design coherence が高い |
| Workspace export 経路用の新 ADR (ADR-0027 等) を起票 | ADR-0025 §決定 (j) として吸収 | 抽出層は markitdown 1 本経路 (§決定 (a)) で完結、Workspace export は input 経路の延伸 (Web API → MS Office mediatype → markitdown) のみで独立 ADR の論点を持たない、Phase 11 流の単一改訂路線を踏襲 |
| Workspace export 由来文書の size cap を MS Office 由来と同じ (50MB / 500K chars) で固定 | cap は §決定 (b) を継承、適合性は Phase 13 plan OQ9 で実測後に必要なら `[office.google_workspace] max_file_size_mb` separate override 導入 | Workspace export は元 file size 概念が Google native fmt → export 後 size で異なり、実測なしに固定すると skip 発生率が読めない、Phase 11 MVP の「operator override 可能」設計 (§決定 (b)) を継承することで適合性問題を運用調整 escape hatch で吸収 |

## 30. ADR-0010 改訂 (Phase 14 G1 — Gmail + Google Calendar 追加 + delta-cursor 型一般 generalize + Outlook 流本文抽出 + Gmail unit/Calendar unit + scope 拡張)

Phase 14 Sub-issue G1 (2026-05-31) で ADR-0010 (Connector Contract) を改訂し、Phase 10 改訂 (write-back ban、§禁止事項 7) と Phase 11 改訂 (a)-(d) と Phase 13 改訂 (e)-(h) を **保持** したまま以下 5 点を加算追加。Phase 13 plan §9 で forecast していた独立 ADR-0026 (Gmail / Calendar connector) は **立てない** (Phase 11 / 12 / 13 流の単一改訂路線を踏襲)。

- **改訂 (i) Gmail + Google Calendar 新コネクタ追加**: Phase 14 G3 (#295) `connectors/google_mail/` + G4 (#296) `connectors/google_calendar/` connector を本 ADR の Connector Protocol + 責務 1-6 + 禁止事項 1-7 の契約対象に追加。Gmail = Gmail API v1 `users.messages.list` + `users.history.list` 経由 delta、`source_type="gmail_message"` で persist (message 単位、Outlook と symmetric)。Calendar = Calendar API v3 `events.list(syncToken=...)` 経由 delta、`source_type="google_calendar"` で persist (master event only、override 別 record、MS365 Calendar と symmetric)。**Gmail / Calendar push notification (`users.watch` / Calendar `events.watch`) 禁止** を §禁止事項拡張として明文化 (Phase 13 改訂 (e) Drive `files.watch` 禁止と同型、能動性混入防止、形 A scope 抵触)、poll のみに制限
- **改訂 (j) delta-cursor 型 connector 全般への TTL fallback 一般化**: Phase 11 改訂 (c) で Microsoft Graph delta query 限定として導入し、Phase 13 改訂 (g) で Drive API `changes.list` page token に拡張した delta-cursor + 失効時 full-pass fallback 義務を、**delta-cursor 型 connector 全般 (Drive `changes.list` / Gmail History API / Calendar sync token / 後続 delta-cursor 型 connector)** に generalize。本 §(j) は §(c) / §(g) の SSOT を統合する位置付けで、両条文は本 §(j) の specialization として継続有効。vendor 別 TTL 失効 trigger 表 + Gmail / Calendar の time-window fetch 詳細 (Gmail `q="after:..."` / Calendar `timeMin` + `timeMax`) を pin
- **改訂 (k) 本文抽出契約 = Outlook 流継承 (Gmail / Calendar)**: Phase 11 改訂 (b) の markitdown 経路 (バイナリ文書、ADR-0025) とは **別系統** の text-only family (Slack / Outlook / Teams chat) に Gmail / Calendar を加える。Gmail = text/plain 優先 → text/html 生保持 (markitdown / html2text なし) / 添付 retain なし / threadId field 保持 / labelIds は body の冒頭に `[Labels: ...]` 形式で prepend (構造化 field なし、Outlook の `attendees_count` 流) / body 上限の `[gmail body truncated: N / M chars]` tag。Calendar = summary `start_iso - end_iso (N attendees)` フォーマット / attendee email list / 議題 / 会議室は body 埋め込み / RRULE は field 保持 (instance 展開は Phase 15+ projection 層) / markitdown 経由なし
- **改訂 (l) Gmail unit + Calendar unit + label / attendee 表現契約**: §(k) と分離して unit 設計を独立 pin。Gmail unit = message 単位 (`gmail_message`、Outlook と symmetric、thread 単位 source_type は作らない、event store immutability 整合)。Calendar unit = master event only (`google_calendar`、MS365 Calendar と symmetric、recurring instance 動的展開は Phase 15+ projection 層、override は別 record)。label / attendee は summary / body 埋め込みのみ (構造化 field 追加なし、Outlook 流 mapper symmetry 維持、Phase 14 plan OQ7 確定)
- **改訂 (m) Google OAuth principal の scope 拡張 + shared auth foundation**: Phase 13 改訂 (h) で `connector:google_workspace:refresh_token` keyring slot 単独で Drive 専用 (`drive.readonly`) として確定した Google OAuth principal を、Phase 14 で scope 拡張 (`drive.readonly + gmail.readonly + calendar.readonly`) + 3 connector 共有 + shared auth foundation 抽出 (`connectors/google_auth/auth.py`、G2 #294 で物理移動 = G1 plan の `google_common` 仮置きから G2 着手時に rename 採用、catch-all 化リスク回避)。**新 keyring slot 追加なし** (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token` は作らない)、1 Google account = 1 principal を 3 connector が共有。env override 名 `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` も変更なし。scope 宣言形式 = `auth.py` 内固定 list (Phase 14 plan §X.1 §設計選択の trade-off 参照、connector ごとの subset 宣言は不採用)。命名は §X.3 trade-off で `google_auth` を採用 (Phase 14 範囲で shared 化対象が auth.py のみのため責務を狭く明示)

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Gmail / Calendar を独立 ADR (ADR-0026 等) として起票 | ADR-0010 §Phase 14 改訂 (i) として吸収 | Phase 11 / 12 / 13 流の単一改訂路線を踏襲 (Phase 14 = 0 新規 + 2 改訂)、Connector Protocol + 責務 1-6 + 禁止事項 1-7 を Gmail / Calendar にも適用する確認のみで独立 ADR は概念的二重化、Phase 14 plan §1 OQ9 で「ADR-0010 + ADR-0014 の 2 改訂」を確定 |
| 新 keyring slot (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token`) を追加 | Phase 13 確定の `connector:google_workspace:refresh_token` 1 slot を 3 connector が共有、新 slot 追加なし | 1 Google account = 1 principal が opshub 秘書 MVP の前提 (ADR-0018 同根拠)、同一 Google account から Drive / Gmail / Calendar を取り込むのが自然な操作モデル、3 つの slot を別管理する意味なし (3 つすべてに同じ refresh token を入れることになり冗長)、scope 拡張で 1 slot 3 connector 共有が成立する非対称性は Google OAuth エコシステムの account 単位 token 設計に由来 |
| Gmail thread 単位 source_type (`gmail_thread` 等) を採用 | message 単位 (`gmail_message`、Outlook と symmetric)、threadId は field 保持 | event store immutability と摩擦 (thread = 複数 message の動的集約、message append 毎に thread record を再書きすると event log が無限増殖)、Phase 1 で確定した event-sourced architecture と直接抵触、thread aggregation は projection 層の責務として Phase 15+ で切る |
| Calendar instance 展開を connector layer で行う (recurring event を展開して emit) | master event + RRULE field、instance 展開は Phase 15+ projection 層 | master event = 1 record + RRULE field のほうが event-sourced と素直、connector で展開すると同一 event の複数 instance が大量 emit され、derived state であるべきものが event log に固定化される、両 calendar (ms365 / google) 同時に Phase 15+ で切るのが整合的 |
| Gmail HTML body に markitdown を通す | text/html 生保持 (Outlook と symmetric、markitdown / html2text なし) | markitdown を通すと HTML → markdown 変換層が mapper に増え、Outlook mapper との symmetry が崩れる、HTML rendering は host LLM / skill 側の責務 (markdown 化 / plain text 化のどちらが必要かは skill ごとに異なる)、mapper symmetry 維持を優先 |
| Gmail label / Calendar attendee を構造化 field (`labels` / `attendees`) として SourceObserved に追加 | summary / body 埋め込みのみ (Outlook の `attendees_count` 流、新 field なし) | SourceObserved domain 改変は migration を伴い、Outlook と非対称、Phase 14 時点では body 埋め込みで足り、構造化 filter は Phase 15+ で需要顕在化時に切る (両 connector ms365 / google 同時に改訂) |
| Teams pattern (verbatim user token + アプリ層 refresh なし) を Gmail / Calendar にも採用 | Phase 13 改訂 (h) で確立した MS365 / Box pattern を流用 (scope 拡張 + shared auth foundation 抽出) | Google OAuth Refresh Token は documented rotation を行う、verbatim token のみ pattern では毎回 paste-code flow が必要で operator UX 劣化 (Phase 13 plan §Alternatives #6 と同根拠)、Phase 14 改訂 (m) は Phase 13 改訂 (h) の流用 + scope 拡張 + shared auth foundation 抽出 |
| Gmail / Calendar push notification (`users.watch` / `events.watch`) で能動的に変更検知 | poll のみに制限、push notification を connector に実装しない | Phase 13 改訂 (e) Drive `files.watch` 禁止と同型、能動性混入は形 A (Phase 10 ADR-0004 改訂で確定) と直接抵触、構造的に経路を不在にすることで「flag 1 つで緩める」リスクを排除、Phase 16+ 能動性段階で再評価 |

## 31. ADR-0014 改訂 (Phase 14 G1 — google_workspace slot scope 拡大 + shared auth foundation 抽出方針)

Phase 14 Sub-issue G1 (2026-05-31) で ADR-0014 (SaaS Token Storage) を改訂し、§Phase 7 Validation 節の **`connector:google_workspace:refresh_token` slot の scope を Drive 専用 → Drive + Gmail + Calendar 全般 (`drive.readonly + gmail.readonly + calendar.readonly`) に拡大**、shared auth foundation 抽出方針を明示 (G1 plan では `connectors/google_common/auth.py` を仮置き、**G2 着手時の §X.3 再評価で `connectors/google_auth/auth.py` を採用** = catch-all 化リスク回避)。**新 slot 追加なし** (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token` は作らない)、1 Google account = 1 principal を 3 connector (Drive / Gmail / Calendar) が共有。env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` 名称はそのまま。rotation pin test (`test_get_access_token_persists_rotated_refresh_token`) は Phase 14 G2 で `tests/unit/connectors/google_workspace/` から **`tests/unit/connectors/google_auth/`** に物理移動 + shared 側で 1 本に集約 (3 connector 分の重複防止)。

- **slot 物理アドレス維持の根拠**: keyring key 文字列 `connector:google_workspace:refresh_token` 自体を変更しないことで、既存 operator の re-consent は scope 拡張の 1 回のみに最小化 (Google OAuth incremental authorization の挙動で、scope 拡張時は既存 refresh token を invalidate するが、scope が前回と一致していれば re-consent 不要)
- **shared auth foundation 抽出の必然性**: token refresh + rotation 書き戻しを 3 connector に各々実装すると pin test も 3 つ並ぶ、shared 化で 1 本に集約することで「rotation 書き戻し忘れ regression」を構造的に防ぐ。Phase 13 G3 で `src/opshub/connectors/google_workspace/auth.py` に置いた logic を G2 で `src/opshub/connectors/google_auth/auth.py` に物理移動 (G2 採用名)、Phase 13 既存 google_workspace connector は新場所から import に re-wire
- **ADR-0010 §Phase 14 改訂 (m) との対応**: ADR-0010 で「Google OAuth principal は scope 拡張 + 3 connector 共有 + shared auth foundation 抽出」を確定したため、token storage 経路の slot scope 拡大 + 抽出方針も本 ADR で同期する自然な帰結
- **本 ADR §Decision (薄ラッパー + 規約ベースの key 命名 + env var override + `secrets` extras 隔離) は無変更**: signature 変更ゼロ、Phase 14 改訂は §Phase 7 Validation 節への scope 拡張 + 物理移動方針の追記のみ。keyring slot 件数は 6 件のまま (Phase 7 = 4 + Phase 11 Teams = 5 + Phase 13 google_workspace = 6)、env override 件数も 6 件のまま

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Gmail / Calendar 用に新 keyring slot を追加 (Phase 13 google_workspace slot とは別管理) | Phase 13 slot を 3 connector で共有、scope のみ拡大 | 1 Google account = 1 principal が opshub 秘書 MVP の前提、3 つの slot を別管理する意味なし (同じ refresh token を 3 つ入れることになり冗長)、scope 拡張で 1 slot 3 connector 共有が成立する非対称性は Google OAuth エコシステム由来 |
| Gmail / Calendar 用に新 env override 名 (`OPSHUB_CONNECTOR_GOOGLE_MAIL_REFRESH_TOKEN` / `OPSHUB_CONNECTOR_GOOGLE_CALENDAR_REFRESH_TOKEN`) を追加 | Phase 13 確定の `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` を 3 connector が共有 | slot と同じ理由 (1 principal を 3 connector が共有するので env も 1 つで足る)、CI / 緊急用の env 名が増えると operator の覚えるべき変数が増える |
| keyring slot 文字列を `connector:google:refresh_token` に rename (google_workspace 限定の語感を解消) | `connector:google_workspace:refresh_token` を維持 (slot 物理アドレス維持) | Phase 13 既存 operator の re-consent コストを最小化 (slot 名変更 = 既存 token 全 invalidate + 再 paste-code flow 必須)、語感の不一致は ADR / docs で「3 connector 共有 slot」と明文化することで対応 |
| shared auth foundation を Phase 14 G3 / G4 の各 connector 内に individual に実装 (shared module 抽出なし) | `connectors/google_common/auth.py` に shared module として抽出 (G2 で物理移動) | token refresh + rotation 書き戻しを 3 connector に各々実装すると pin test が 3 つ並び、rotation 書き戻し忘れ regression を構造的に防げない、shared 化で 1 本に集約することで forget regression 防止が原則として成立 |
| shared auth foundation の rotation pin test を G3 / G4 で 3 connector 分配置 | shared 側 (`tests/unit/connectors/google_common/`) に 1 本集約 (G2 で物理移動) | 3 connector 分の重複 test は同じ logic を 3 回 assert することになり冗長、shared 側 1 本で「shared logic が壊れたら全 connector に影響する」ことを test 構造で明示できる |

## 32. Phase 14 closeout chronicle (Wave 4 G5、2026-05-31)

Phase 14 (Gmail + Google Calendar コネクタ、epic #292) の closeout を G5 (#297) で完了させ、Phase 14 を end-to-end で締めた。

- Wave 構成 (実績、2026-05-31): **Wave 1** = G1 ADR + plan + Phase 13 影響反映 PR #298 (commit f5d43c8、ADR-0010 改訂 §30 + ADR-0014 改訂 §31 + docs/phase-14-plan.md + 本 decisions-log §31 + Phase 14 chronicle 追加)。**Wave 2** = G2 shared auth foundation PR #300 (commit bd74191、`connectors/google_workspace/auth.py` → `connectors/google_auth/auth.py` 物理移動 + 3-scope 固定 list 拡張 + Phase 13 既存 import re-wire + token rotation pin test を `tests/unit/connectors/google_auth/` に集約、G2 着手時の rename 評価で `google_common` 仮置きから `google_auth` 採用 = catch-all 化リスク回避)。**Wave 3** = G3 Gmail PR #303 (commit 3c621c1) + G4 Calendar PR #301 (commit a4cefab) を並列実行 (G3 は rebase 後の force-push 制限で初回 PR #302 を superseded、新 branch + 新 PR で再投入)。**Wave 4** = G5 closeout PR (本 PR、docs 一括 + e2e lifecycle test + M6 guard 維持確認 + Phase 13 plan §9 forecast 整合化 + Phase 15+ outlook 再評価 + epic close)
- **mapper symmetry pin test** が G3 + G4 で `tests/unit/connectors/test_mapper_symmetry.py` に追加された (Outlook ↔ Gmail 8 ケース + ms365_calendar ↔ google_calendar 6 ケース = 計 14 ケース)。Phase 14 で mapper symmetry を unit test として凍結したため、将来 vendor-specific な調整を入れた際に divergence が intentional か機械的に確認できる。Phase 15+ で symmetric な拡張 (Calendar instance 展開 projection / Gmail thread aggregation projection / 添付の markitdown 経路 / label / attendee の構造化 field 化) を入れる場合の起点になる
- **Phase 13 plan §9 forecast 書き戻し** = Phase 13 audit R2-CROSS-06 (forecast 取り残し) 同型ミス防止のため、Phase 14 着手時の再評価 (画像 OCR / Drive Comments / Suggestions を Phase 15+ へ移送) を `docs/phase-13-plan.md` §9 に書き戻した。次回 Phase 着手時に「過去 plan の forecast を確認 + 再評価結果を書き戻す」を規律として継続する
- **新 source_type 2 種**: `gmail_message` + `google_calendar` を `domain/events/source.py` literal allowlist に追加 (G3 / G4 で実装)。Phase 12 H1 で確立した `source.list.observed_after/before` 物理列フィルタ経由で、14 skills 全てが透過的に利用可能 (mapper が `sources.body` に persist する限り skill 側に追加の変更は不要、Phase 11 / 13 と同型)
- **OAuth scope 拡張 + 1 回 re-consent**: Phase 13 までの既存 `google_workspace` operator は **`opshub connector auth set google_workspace` を 1 回再実行** することで Drive + Gmail + Calendar の 3 scope に拡張済 refresh token を取得できる (`docs/upgrading.md` §Phase 14 節 + `docs/google-workspace-setup.md` §Scopes に手順)。keyring slot 文字列は `connector:google_workspace:refresh_token` のまま維持 = 既存 operator の re-consent コストを最小化
- **新 ADR ゼロ、改訂 2 本** (ADR-0010 + ADR-0014) で Phase 14 を完結 (Phase 11 1 新規 + 2 改訂 → Phase 12 0 新規 + 3 改訂 → Phase 13 0 新規 + 3 改訂 → **Phase 14 0 新規 + 2 改訂**、単一改訂路線の縮退継続)。新 extras なし (`[connectors-google-workspace]` httpx 流用)
- **Phase 15+ outlook 再評価**: 画像 OCR (Phase 13 → 14 から再 defer) / Drive Comments / Suggestions (Phase 13 から繰り越し) / **Gmail / Calendar 添付の本文抽出 (新規 defer、ADR-0025 拡張)** / **Calendar instance 展開 projection (新規 defer、ms365 / google 両方同時)** / **Gmail thread aggregation projection (新規 defer)** / **メール・カレンダー meta 構造化 (label / attendee / response_status を field 化、新規 defer、ms365 / google 同時)** / 能動性段階 1-4 (cron 委譲 / 記憶キュレーション / 通知 / filewatch / **Drive `files.watch` + Gmail `users.watch` + Calendar `events.watch` push notification 再評価**) / 外部書き戻し (Teams / Drive / **Gmail send / Calendar event create** + HITL) / Notion / Jira / Linear / Confluence / `ozzy-labs/skills` 配布完成。詳細は `docs/phase-13-plan.md` §9 + `docs/phase-14-plan.md` §Phase 15+ outlook
