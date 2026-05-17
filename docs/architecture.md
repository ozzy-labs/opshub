# Architecture

> Status: Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connectors + workspace ingest、MVP scope = framework + GitHub) + Phase 4 (semantic recall layer、MVP scope = full Pluggable Embedder + recall + duplicate detection) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + `opshub brief` + event-driven auto-embed 補助) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + `opshub propose` CLI、human-in-the-loop apply 必須)) + Phase 7 (Connectors Wave 2、MVP = Slack + Microsoft 365 + Box、epic #113) + Phase 8 (Knowledge graph layer、MVP = ADR-0017 + `links` projection (migration 0016) + 4 自動抽出経路 + manual link CRUD + `LinkService` traversal + `opshub link` / `opshub graph` CLI + `--expand-graph` integration、epic #128) complete (2026-05-17). 次の候補は Phase 9 (Multi-machine sync、principles §Open Q #5)。`llama.cpp` direct binding / briefing cache + narrow scope / connector-side automatic `SourceReferenced` 発行 / multi-machine sync は Phase 6.x / 7.x / 8.x / 9 以降。詳細は §9 (Phased Delivery) を参照。

OpsHub の高レベルアーキテクチャ・データフロー・データモデル・用語を記述する。具体的な決定の根拠は対応 ADR を参照。

## 1. レイヤ全体像

```text
┌────────────────────────────────────────────────────────────┐
│  External Systems                                          │
│  GitHub  Slack  Microsoft 365  Box  Office Files  ...      │
└─────────────────┬──────────────────────────────────────────┘
                  │ (Phase 3 ✅ GitHub 実装済 / Slack・MS365・Box は 3.x)
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

### 2.2 Application Services

CLI / Connector / Agent から渡された command を受け、ドメイン的に有効化を検証し、Event Store に append、Projector に通知、必要なら Lock を取得・解放する。

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

### 2.11 Workspace Generation Layer

projection を読み、markdown (tasks / briefings / reviews / handoffs / dashboards) を生成。read-only。Jinja2 で template 化。

### 2.12 Agent Runtime Boundary

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
| `sources` | projection | Phase 1+2+3 (✅ 実装済) | external item の現在状態 |
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

詳細は [ADR-0005: External Content Minimization](adr/0005-external-content-minimization.md) 参照。

### 8.1 External Content Retention

| 保持する | 保持しない |
|---|---|
| external IDs | full Slack history |
| URLs | full email bodies |
| summaries | confidential documents |
| metadata | credentials |
| extracted action items | access tokens |
| 最小引用 | binary artifacts |

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
| 8 | Knowledge graph layer (ADR-0017 + `links` projection (migration 0016) + 4 自動抽出経路 + manual link CRUD + `LinkService` traversal + `opshub link` / `opshub graph` CLI + `--expand-graph` integration、✅ 2026-05-17 完了、epic #128) | connector-side automatic `SourceReferenced` 発行 / graph visualisation web UI (Phase 8.x) / multi-machine sync (Phase 9) |

詳細は [Principles 9 (Phased Delivery)](principles.md) 参照。

## Open Questions

1. 4.x で扱う `embeddings` テーブルのスキーマ (sqlite-vec への bind 方法を含む)
2. `Decision` テーブルと `Task` テーブルの関係 (Decision は Task の親か別 entity か)
3. `events` の partitioning / archive 戦略 (long-term 運用時)
4. multi-machine 利用 (将来 sync を許す場合の競合解決) — principles.md §Open Q #5 と同件、Phase 9+ で別 plan (Phase 8 = Knowledge graph、epic #128 を先行)
