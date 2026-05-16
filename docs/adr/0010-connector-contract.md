# 0010. Connector Contract

- Status: Proposed
- Date: 2026-05-16
- Deciders: ozzy

## Context

Phase 3 で GitHub / Slack / Microsoft 365 / Box 等の Connector を実装する際、Connector が以下のことを「できる」状態にすると、OpsHub の整合性が崩れる。

1. **Task を Connector が自動生成する** — 外部 SaaS のイベントすべてが Task になり、人間の triage 判断を bypass する。誤判定タスクが大量発生
2. **Decision / Link を Connector が直接書く** — 「これは決定事項」「これは関連」の判定は本来 triage / 人間 / agent の責務。Connector が判定すると context を持たないまま誤った関係性が記録される
3. **Projection を Connector が直接更新** — Event Store を経由しないため audit / replay 性が壊れる (ADR-0002 違反)
4. **Event を Connector が任意定義** — semantic event 命名規約 (ADR-0002) から外れ、event の意味が散逸する

これらを防ぐために Connector の責務と禁止事項を明確化する必要がある。

## Decision

Connector は次の経路のみを実行する。

```text
external metadata fetch
  → source entity の作成・更新
  → source event の append (Event Store 経由、Application Service 呼び出し)
  → inbox item の作成
```

Connector の **責務**:

1. external API 呼び出し (差分 fetch)
2. source normalization (vendor 固有 field を OpsHub の Source Entity schema に変換)
3. cursor checkpointing (`connector_cursors` テーブル更新)
4. `SourceObserved` / `SourceReferenced` 等の source event を Application Service 経由で append
5. inbox item を Application Service 経由で作成 (triage 用キューに投入)
6. body 本文の minimization (ADR-0005 参照、summary / extracted action items のみ保持)

Connector の **禁止事項**:

1. **Task / Decision / Link を直接生成しない** — これらは triage / 人間 / agent の責務
2. **Projection table を直接更新しない** — 必ず Event Store 経由
3. **Event を Application Service を経由せず append しない**
4. **Vendor 固有 event 名を勝手に定義しない** — `domain/events/source.py` で集中管理
5. **Full body を Operational Memory に書き込まない** — ADR-0005 違反
6. **Lock を取得せず長時間 Task の状態を変更する操作を発火しない** — 必要なら Application Service が lock を取る

triage を経て Task / Decision / Link が生成される流れ:

```text
[inbox_items] → triage (人間 or agent) → TaskCreated / DecisionRecorded / LinkCreated events
              └─ Application Service 経由
```

## Consequences

### Positive

1. **誤検知タスクの抑止** — 外部 SaaS の全イベントが Task 化されない
2. **Audit 整合性** — すべての書き込みが Event Store 経由
3. **Connector の単純化** — 責務が「fetch + normalize + event 化」に限定される
4. **Replayability** — Connector を後から差し替えても Event Store は壊れない
5. **テスト容易性** — Connector は service mock で完結する

### Negative / Trade-offs

1. **triage 操作の頻度が増える** — Connector が直接 Task 化しないため、人間 / agent が triage を回す必要
2. **automated triage への発展余地** — Phase 4 で agent が自動 triage する場合も、必ず Application Service 経由で event を発行する設計を維持
3. **Connector 実装ボイラプレート** — 各 Connector が service 呼び出しを書く分、行数が増える (Connector base 抽象クラスで吸収可)

## 軽減策

1. **`connectors/base.py` に共通基底クラス** — `fetch_changes` / `normalize` / `emit_events` の 3 メソッドだけ実装すれば Connector が完成する形に
2. **triage skill / CLI を充実** — `opshub inbox triage` で複数 inbox item を一括処理できるように
3. **自動 triage rule** — Phase 4 で「特定 channel の特定 sender → 自動 Task 化」のような rule を YAML で書ける機構を検討 (ただし rule の評価結果も Application Service 経由で event 化)

## Alternatives Considered

### 1. Connector が Task を直接生成する

却下理由:

- 全 SaaS イベントが Task 化すると tasks テーブルが膨張
- 重要度判定を Connector が持つことになり、ドメインロジックの責務分散
- 削除が困難 (event 化されているため)

### 2. Connector が「重要そうな」イベントだけ Task 化する

却下理由:

- 「重要そうな」を Connector が判断するためのロジックが必要
- 判断基準が SaaS / 用途で異なり、Connector 実装が肥大化
- triage という機能の存在意義が薄れる

### 3. Inbox item を経由せず直接 Triaged Event Stream に流す

却下理由:

- 人間 / agent が「未処理」と「処理済み」を区別する手段が必要
- inbox という抽象が無いと「今すぐ見るべきもの」が判別不能
- inbox は projection なので柔軟に表示形式を変えられる利点も失う

### 4. Connector が複数の Application Service にまたがって書き込みを行う

却下理由:

- Connector の責務が肥大化
- 異常時の rollback / lock 管理が困難
- 採用案 (1 connector → 1 service 呼び出し / 1 event 単位) の方が atomic 性を保てる

## 関連

- [Principles 7 (Connector Contract)](../principles.md)
- [Architecture 2.1 (Connector Layer)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md)
