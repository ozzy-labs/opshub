# Architecture

> Status: Draft (in active design). Last reviewed: 2026-05-16.

OpsHub の高レベルアーキテクチャ・データフロー・データモデル・用語を記述する。具体的な決定の根拠は対応 ADR を参照。

## 1. レイヤ全体像

```text
┌────────────────────────────────────────────────────────────┐
│  External Systems                                          │
│  GitHub  Slack  Microsoft 365  Box  Office Files  ...      │
└─────────────────┬──────────────────────────────────────────┘
                  │ (Phase 3+)
                  ▼
┌────────────────────────────────────────────────────────────┐
│  Connector Layer  (src/opshub/connectors/*)                │
│  - external metadata → source entity                       │
│  - cursor checkpointing                                    │
│  - emits SourceObserved / SourceReferenced events          │
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
                │ + queries      │     │ (Phase 4)      │    │ markdown out   │
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

### 2.2 Application Services

CLI / Connector / Agent から渡された command を受け、ドメイン的に有効化を検証し、Event Store に append、Projector に通知、必要なら Lock を取得・解放する。

### 2.3 Event Store

`events` テーブル (SQLite)。append-only、immutable、`schema_version` 付き。Event は semantic ("TaskActivated") であるべきで、generic CRUD event は避ける。

### 2.4 Projection Layer

current state の relational view。`tasks` / `sources` / `inbox_items` / `decisions` / `links` / `work_sessions` / `agent_runs` / `locks` / `connector_cursors` 等。すべて event 列から rebuildable。

### 2.5 Graph Layer

entity 間の関係性 (`task → source` / `decision → meeting` 等) を `links` テーブルで relational に持つ。専用 graph DB は採用しない (Phase 4 以降で再評価)。

### 2.6 Vector Layer (Phase 4)

semantic 索引層は **Pluggable Embedder + Pluggable VectorStore** として設計する (ADR-0012)。Phase 1 で抽象境界 (`Embedder` / `VectorStore` Protocol) と `embeddings` projection schema を確定し、具象実装 (local `sentence-transformers` / OpenAI / Voyage 等の embedder、sqlite-vec backed store) は Phase 4 で着手する。

- **Embed 対象**: task summary / decision text / inbox_item summary / source summary / briefing / extracted action items (ADR-0005 整合、full body は対象外)
- **Storage**: sqlite-vec 仮想テーブル + `embeddings` projection (`entity_type`, `entity_id`, `model_id`, `model_version`, `dimensions`)
- **Refresh**: event 駆動 (`TaskActivated` 等の projector hook) + `opshub embeddings rebuild` で bulk 再計算 + model version 変更時に増分 re-embed
- **Backend 切替**: `~/.config/opshub/config.toml` の `[embedding]` セクションで `local` / `openai` / `voyage` / `disabled` を選択。Phase 1-3 デフォルトは `disabled`
- **Recall**: vector + SQL filter の hybrid search を CLI から提供 (`opshub recall ...`、Phase 4)

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

| Table | 種別 | 概要 |
|---|---|---|
| `events` | authoritative | append-only domain event log |
| `tasks` | projection | current task state |
| `projects` | projection | task / decision のグルーピング (Phase 2+) |
| `sources` | projection | external item の現在状態 |
| `inbox_items` | projection | 未 triage queue |
| `decisions` | projection | 決定記録 |
| `links` | projection | entity 間 graph 関係 |
| `work_sessions` | projection | 人間 / agent の execution session |
| `agent_runs` | projection | agent 実行記録 |
| `locks` | projection | coordination lock |
| `connector_cursors` | projection | 差分同期チェックポイント |
| `embeddings` | projection | 派生 semantic 索引メタ (Phase 4) |

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
- `Lock`: 競合回避 (粒度は Open Questions 参照)
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
| 3 | GitHub / Slack / Microsoft 365 / Box connector | vector recall |
| 4 | vector recall, semantic search, duplicate detection, briefing 自動生成 | — |

詳細は [Principles 9 (Phased Delivery)](principles.md) 参照。

## Open Questions

1. 4.x で扱う `embeddings` テーブルのスキーマ (sqlite-vec への bind 方法を含む)
2. Lock の粒度 (`task:<id>` / `project:<id>` / `global` の三階層案あり)
3. `Decision` テーブルと `Task` テーブルの関係 (Decision は Task の親か別 entity か)
4. `events` の partitioning / archive 戦略 (long-term 運用時)
5. multi-machine 利用 (将来 sync を許す場合の競合解決)
