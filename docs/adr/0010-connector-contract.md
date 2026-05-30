# 0010. Connector Contract

- Status: Accepted (revised 2026-05-30 for Phase 10 Sub-issue E)
- Date: 2026-05-17 (initial); 2026-05-30 (Phase 10 §Write-back scope clarification: 当面 scope 外)
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
5. **Full body を Operational Memory に書き込まない** — ADR-0005 違反 (**Phase 10 で撤回**、ADR-0020 で本文ローカル保持に転換。本項は ADR-0005 supersede 後は無効。本文取り込みは `SourceObserved.body` + `provenance_origin` / `provenance_trust` 経由で行う)
6. **Lock を取得せず長時間 Task の状態を変更する操作を発火しない** — 必要なら Application Service が lock を取る
7. **外部 SaaS への書き戻し (write-back) を実装しない** — **Phase 10 改訂で明示**。`post` / `send` / `comment` / `reply` 等の SaaS API への書き込みメソッドを connector に実装しない。Sub-issue E (返信下書き生成) は **下書き提示まで** で完結し、外送信は operator が手で行う (ADR-0016 §決定 (c) HITL 必須の延長)。詳細は §Phase 10 改訂を参照

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

## Validation

Phase 3 sub-issue B (PR #51-55) で GitHub connector を本 contract に沿って実装し、`Connector` Protocol + `ConnectorContext` + `SourceService` 経由の event 連鎖 + cursor 永続化 + fail-fast (`ConnectorSyncFailed`) までを end-to-end で検証した (`tests/integration/test_github_connector_lifecycle.py` および sub-issue D の `tests/integration/test_phase3_lifecycle.py::test_github_connector_to_inbox_e2e`)。具体的に確認できたのは以下:

- Connector は Task / Decision / Link を一切生成しない (生成パスは `inbox triage` のみ)
- Projection 更新は `SourceService.observe` 経由でのみ発生 (Connector は SQL を直接叩かない)
- Event 名は `SourceObserved` / `ConnectorSyncStarted` / `ConnectorSyncCompleted` / `ConnectorSyncFailed` に集中管理され、vendor 固有 event は出現しない
- ADR-0005 整合 (summary / external_id / url のみ保持、full body は保持しない)
- 失敗時は `ConnectorSyncFailed` を append して fail-fast、token / PII を event log に漏らさない

### Phase 7 (Connectors Wave 2) validation

Phase 3 で導入した Connector Contract は Phase 7 で 3 新規 connector (Slack / Microsoft 365 / Box) の実装を経て validated:

- **Auth + token storage** — 3 connector とも `core/secrets` + keyring 経路を再利用 (ADR-0014 contract、`tests/unit/connectors/<name>/test_auth.py`)
- **Fetcher + cursor** — 3 connector とも `connector_cursors` projection で cursor 管理 (MS365 は 3 cursor key、Slack は per-channel JSON、Box は単一 stream_position)
- **Mapper + source_type** — 各 connector が distinct な `source_type` で `sources` projection に persist (`slack_message` / `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` / `box_event`)
- **ADR-0005 (External Content Min)** — 全 connector で `summary` ≤ 200 chars enforce (test pin: `tests/integration/test_phase7_lifecycle.py` および per-connector unit test)
- **Rate-limit handling** — exponential backoff 1s/2s/4s max 3 retries、最終失敗で `ConnectorSyncFailed` event (test pin per connector: `tests/integration/test_phase7_connector_atomicity.py`)
- **CI mock 規律** — 全 connector test は SDK / HTTP fully mocked、実 API 非接続

Contract の signature 変更は本 phase で発生せず (Phase 3 で確定済の `Connector` Protocol が 3 つの新 connector で適合)、ADR-0010 は Phase 7 で touch せず Phase 7.x 以降の Additional connectors / common OAuth helper refactor で再評価する。

## Phase 10 改訂 (Sub-issue E、2026-05-30)

Phase 10 Sub-issue E (返信下書き生成、ADR-0016 §決定 (i)) で connector contract に **write-back 非対応** を明示的な禁止事項として追記する (§禁止事項 7)。

### 背景

Phase 3-9 の connector はすべて **fetch (差分取り込み) only** で実装されており、SaaS API への書き込み (Slack message 送信 / Outlook メール送信 / GitHub PR コメント等) を行うメソッドは存在しない。Phase 10 で `reply_draft` candidate kind (ADR-0016 §決定 (i)) を導入し、LLM が「返信下書き」を生成可能になることで、「下書きをそのまま自動送信する経路を作るべきか」という設計圧力が新たに生じる。

### 決定

**Phase 10 では SaaS への書き戻し / 投稿 / 送信を実装しない**。reply_draft candidate の apply は `proposals.candidate_states` を `pending → applied` に flip するだけで完結し、生成された下書き本文は `proposals.candidates[i].body` に durable に保存される。operator が手で SaaS UI (Slack / Outlook 等) にコピペして送信する。

理由:

1. **Phase 10 緊張点 ③ の決定** (phase-10-plan.md §1.6): 「返信下書きの生成は作る、外部書き戻しは当面作らない」を Phase 10 着手時に確定済。本 ADR §禁止事項 7 はその実装層への落とし込み
2. **HITL 境界の保全** (ADR-0016 §決定 (c)): LLM 生成テキストが durable state に書かれる前に operator review を 1 段挟む原則を、SaaS 送信に対しても適用 (= 送信前にも operator が確認する経路を保つ)。auto-send は prompt injection / hallucination が外部に伝播する経路を開く
3. **構造的に経路が存在しないことの保証**: connector に `post` / `send` / `comment` / `reply` メソッドを実装しないことで「経路がそもそも無い」状態を維持する。CLI / MCP tool / Skill が誤って auto-send 経路を踏むリスクがゼロ
4. **test pin の根拠**: Phase 10 step E2 で `tests/integration/test_phase10_reply_draft_no_external_writeback.py` (または unit test 相当) として、`opshub.connectors.*` モジュール内に `post` / `send` / `comment` / `reply` / `create_comment` 等のメソッドが存在しないことを assert する test を設置する。これは ADR-0016 §決定 (c) の HITL 境界を **コードベース上で機械的に保証** するための contract test

### Phase 11 以降の再評価条件

外部書き戻しを将来導入する場合は:

1. 本 ADR を Superseded by する新 ADR
2. ADR-0004 (Agent Runtime Boundary) を explicitly revisit
3. ADR-0016 §決定 (c) HITL 必須宣言と整合する設計 (auto-send 禁止、送信前 operator 確認、送信履歴の event-sourced 記録) を新 ADR で pin

これらすべてが揃った場合のみ。flag 1 つで緩める / 個別 connector が独自に実装する経路は許可しない (Phase 6 ADR-0016 §決定 (c) の auto-apply 禁止と同じ強度の宣言)。

## 関連

- [Principles 7 (Connector Contract)](../principles.md)
- [Architecture 2.1 (Connector Layer)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md)
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md)
