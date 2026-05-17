# Architecture

> Status: Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connectors + workspace ingest、MVP scope = framework + GitHub) + Phase 4 (semantic recall layer、MVP scope = full Pluggable Embedder + recall + duplicate detection) shipped 2026-05-17. Briefing 自動生成 / `links` projection 本実装 / event 駆動自動 embed は Phase 5+ で別途。Slack / Microsoft 365 / Box connectors are deferred to Phase 3.x.

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

### 2.7 Workspace Generation Layer

projection を読み、markdown (tasks / briefings / reviews / handoffs / dashboards) を生成。read-only。Jinja2 で template 化。

### 2.8 Agent Runtime Boundary

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
| `links` | projection | Phase 4+ | entity 間 graph 関係 (Phase 3 では `inbox_items.source_ref` 列で簡易 link) |
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
| 3 | Connector framework + GitHub connector + workspace inbox file ingest (✅ 2026-05-17 完了)。Slack / Microsoft 365 / Box は Phase 3.x | vector recall |
| 4 | vector recall, semantic search, duplicate detection (Pluggable Embedder + sqlite-vec、✅ 2026-05-17 完了) | briefing 自動生成 / event 駆動自動 embed (Phase 5) |

詳細は [Principles 9 (Phased Delivery)](principles.md) 参照。

## Open Questions

1. 4.x で扱う `embeddings` テーブルのスキーマ (sqlite-vec への bind 方法を含む)
2. `Decision` テーブルと `Task` テーブルの関係 (Decision は Task の親か別 entity か)
3. `events` の partitioning / archive 戦略 (long-term 運用時)
4. multi-machine 利用 (将来 sync を許す場合の競合解決)
