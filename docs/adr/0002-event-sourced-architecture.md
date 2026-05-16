# 0002. Event-Sourced Architecture

- Status: Proposed
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub の中核要件:

1. **長期業務文脈の保持** — 「いつ・誰が・なぜそうしたか」を後からたどれる必要がある
2. **multi-agent coordination** — 複数 AI エージェントが同一ワークスペースを共有し、互いの変更を audit する必要がある
3. **replayability** — projection / markdown を任意のタイミングで再構築できる必要がある (UI 変更・schema 変更時の柔軟性)
4. **safety** — agent による silent destructive operation を後から検出できる必要がある

CRUD ベースの state 管理だと:

- 変更履歴が失われる (DB 上書きで「何が起きたか」が消える)
- projection は state そのものなので、表示形式変更ごとに migration が必要
- agent の操作を後から再現できない
- 監査ログを別途用意する必要がある (DRY 違反)

## Decision

OpsHub の永続化を **Event-Sourced** で構築する。

1. **Event Store** (`events` テーブル) を authoritative layer とする
   - append-only
   - immutable
   - SQLite に格納
   - 各 event は `id` / `aggregate_id` / `type` / `payload` / `schema_version` / `occurred_at` / `recorded_at` / `actor` を持つ
2. **Projection** (`tasks` / `sources` / `inbox_items` / ...) は event の純粋関数として構築する
   - disposable
   - `opshub projections rebuild` で event 列から完全再構築できる
   - CI で rebuild の決定性 (同じ event 列 → 同じ projection) を検証
3. **Event 命名は semantic**
   - 採用: `TaskActivated` / `TaskBlocked` / `TaskCompleted` / `DecisionRecorded`
   - 避ける: `TaskUpdated` / `RowChanged` のような generic CRUD 名
4. **event は backward-compatible** に進化させる
   - 新 field 追加は OK
   - 既存 field の意味変更は新 event type を起票
   - 破壊的変更は `schema_version` で分岐

## Consequences

### Positive

1. **完全な audit log** が無料で得られる
2. **replayability** — projection 構造の変更が migration 不要 (rebuild すれば良い)
3. **agent boundary との相性** — agent の操作はすべて event として記録される
4. **時系列分析** が後から容易 (週次レビュー・振り返り)
5. **schema 変更耐性** — 表示形式・分類・ラベル付けの変更が低コスト

### Negative / Trade-offs

1. **初期実装コスト** — CRUD よりコードが増える (event 型定義 + projector + service)
2. **event 進化の運用負荷** — 後方互換維持を継続的に意識する必要がある
3. **複雑なクエリ** — `events` テーブル直クエリは難解。`projections` 経由のクエリが基本
4. **ストレージ増加** — event を捨てない方針のため、長期的に行数が増える。Phase 4 以降で archive 戦略が必要
5. **学習コスト** — event-sourcing パターンに不慣れな参加者には導入障壁

## 軽減策

1. **Phase 1 で event を 5 種類程度に絞る** — `TaskCreated` / `TaskActivated` / `TaskCompleted` / `NoteRecorded` / `WorkSessionStarted` 程度から始め、必要に応じて追加
2. **eventsourcing library を使わず素朴に実装** — append + reducer + 純粋関数 projector のみ。専用ライブラリは将来検討
3. **`opshub event list` で生 event を確認できる CLI を提供** — debug 性を担保
4. **CI で rebuild 性をテスト** — 「同じ event 列 → 同じ projection」を property test 化

## Alternatives Considered

### 1. CRUD ベース + 別途 audit log

却下理由:

- audit log と state が二重管理になり、DRY 違反 + 同期ズレリスク
- projection 構造変更のたびに migration が必要
- replay 性がなく、agent の操作再現が困難

### 2. Hybrid: 重要 entity のみ event-sourced、それ以外は CRUD

却下理由:

- 境界が曖昧になり、設計が複雑化
- 「重要」の定義が時間とともに揺れる
- 結局すべて event-sourced にした方が一貫性が保てる

### 3. Git-backed (markdown ファイル + git commit を event 代わりに)

却下理由:

- git history は人間向け文脈には強いが、構造化クエリには向かない
- event 単位での粒度制御が難しい (commit = 複数変更)
- agent からの append が git operation を伴い遅い
- replay 性はあるが projection の構築コストが高い

### 4. CQRS + 別 DB (write store と read store を分離)

却下理由:

- OpsHub の規模 (個人 / 小チーム業務) で over-engineering
- 1 SQLite で十分。同 DB 内 `events` テーブル + projection で実用上の課題はない
- 将来スケール時に分離を検討する余地は残す

## 関連

- [Principles 2 (Event-Sourced)](../principles.md)
- [Architecture 3 (データフロー不変条件)](../architecture.md)
- [ADR-0003: Markdown as Workspace Surface](0003-markdown-as-workspace-surface.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
