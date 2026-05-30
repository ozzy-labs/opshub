# Decisions Log (Rejected Alternatives)

> Status: Draft (in active design). Last reviewed: 2026-05-17.

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
| Body 切替時に既存 vector を invalidate しない (model_id 一致で skip) | `embeddings rebuild --rebuild-body` 等の経路で強制 re-embed | `model_id` / `model_version` は同じでも embed 元 text が summary→body に変わると vector の意味が変わる、operator が明示的に rebuild する経路を `embeddings rebuild` に乗せて backend 切替経路と同じ運用に統一 | ADR-0012 改訂版 §4、Phase 10 plan §3-B |

## 19a. Reply Draft Generation (ADR-0016 / ADR-0017 / ADR-0010 Phase 10 改訂)

Phase 10 Sub-issue E (返信下書き生成) は **既存 ADR の改訂** で吸収し、新 ADR を立てない方針を 2026-05-30 に確定した。理由は Phase 6 propose lifecycle (generate → review → apply / reject) と Phase 8 knowledge graph (links + `--expand-graph`) と Phase 10 本文保持 (ADR-0020) + 本文 embedding (ADR-0012 改訂) が前提として揃っており、新 candidate kind (`reply_draft`) を 1 つ追加し link_type を 2 種足し write-back 不許可を contract に書くだけで Sub-issue E の DoD が成立するため。

| 改訂 ADR | 変更点 |
|---|---|
| ADR-0016 §決定 (i)+(j)+(k) 追加 | `ReplyDraftCandidatePayload`(`kind="reply_draft"`、`reply_to_source_id/type` 必須、schema v2) / triage 3 分類 (`respond`/`notify`/`ignore`) を `propose generate` の structured field に追加 (generate-time の prompt-hint signal にとどめ、persist しない / `Proposal` / `ProposalGenerated` event には triage を載せない / auto-apply 経路は構造的に閉じる、Phase 10 監査 Round 2 で明確化) / 文体は静的プロンプトでなく recall した「自分が author の過去送信 event」を `<style_example>` 注入 + 文脈は `--expand-graph` で `<context_source>` 注入 |
| ADR-0017 §決定 (b) 改訂 | link_type enum に `reply_draft_replies_to` / `referenced_in_reply_draft` の 2 種追加 (auto-extracted 全 7 種に拡張)。新 event 発行はせず ADR-0017 §決定 (c) pure derived state projector パターンを踏襲 |
| ADR-0010 §禁止事項 7 追加 + §Phase 10 改訂節 | 外部 SaaS への書き戻し (write-back) を当面 scope 外と明示。`post` / `send` / `comment` / `reply` 等のメソッドを connector に実装しない契約。`tests/` で経路非存在を contract test として pin |

| 却下案 | 採用案 | 理由 |
|---|---|---|
| Reply-draft を新 ADR / 新 sub-system (`ReplyDraftService` + 専用 projection + `opshub reply ...` CLI) として独立 | Phase 6 propose lifecycle に `reply_draft` candidate kind を 1 つ追加して吸収 (ADR-0016 §決定 (i)) | Phase 6 で確立した generate→review→apply / triage / HITL / 既存 service 経由 / idempotent key / schema versioning が再利用可能、独立 sub-system は重複と CLI 表面の肥大化、`opshub propose --kind reply_draft` 1 経路で mental model が小さい (Sub-issue D 秘書 Skill 表とも整合) |
| Triage を separate API / separate event (`Triaged(source_id, classification)` + projection) に分離 | `propose generate` の structured output schema に `triage: Literal["respond","notify","ignore"] \| None` を載せる | LLM call の 1 段化で cost 倍を回避、triage を durable state にすると auto-apply 禁止原則 (ADR-0016 §決定 (c)) と緊張、「ノイズ source の自動破棄」は ADR-0020 §(b) excludes 経路の責務、LLM triage は post-hoc hint に留める |
| 文体を静的システムプロンプトに書く (Inbox Zero 流) | `author = self` 過去送信 event を recall して `<style_example>` ブロックとして注入、文脈は `--expand-graph` で `<context_source>` 注入 | テンプレ口調の暴走 (Inbox Zero の弱点) を回避、Sub-issue A 本文保持 + Sub-issue B 本文 embedding / FTS5 で hybrid recall が可能になった前提を活かす、Read AI Ada の自前 graph 相当を ADR-0017 §決定 (f) `--expand-graph` で代替 |
| 外部書き戻し (auto-send) を flag 1 つで有効化できる経路を Phase 10 で予約 | Phase 10 では実装しない、connector contract で経路の存在自体を禁止 (ADR-0010 §禁止事項 7) + test pin で「`post`/`send`/`comment`/`reply` メソッドが存在しないこと」を機械的に保証 | ADR-0016 §決定 (c) HITL 必須の延長、auto-send は prompt injection / hallucination が外部に伝播する経路を開く、構造的に経路が存在しない方が安全、将来再導入には新 ADR + ADR-0004 revisit + ADR-0016 §決定 (c) 整合の 3 要件すべてを要求 |
| Reply-draft 用に専用 projection (`reply_drafts` テーブル) を新設 | `proposals.candidates[i]` の JSON で代替、`(proposal_id, candidate_index)` natural key で reply_draft 状態を管理 | ADR-0002 single-source-of-truth (projection は increase せず) と整合、Phase 6 で確立した propose lifecycle を全継承、operator は `opshub propose list` / `apply` / `reject` の既存 verb で reply_draft も触れる |

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
