# 0013. Lock Granularity

- Status: Accepted
- Date: 2026-05-17
- Deciders: ozzy

## Context

Phase 2 で OpsHub に coordination layer を追加する (`docs/phase-2-plan.md` / Architecture §6)。Claude Code / Codex CLI / GitHub Copilot CLI / Gemini CLI + 人間が同一 opshub instance 上で同時並行に操作する前提のため、**lock 機構** がなければ次の問題が顕在化する。

- 2 つの agent が同時に同じ task を `Completed` に遷移させる (event 順序は確定するが意味的に重複)
- 1 つの agent が `opshub task complete <id>` を実行している最中に別の agent が `opshub projections rebuild` を走らせ、replay 中の整合性が乱れる
- 同じ work session を 2 つの actor が同時に start / end し、bracket が壊れる

ADR-0004 (Agent Runtime Boundary) で「すべての書き込みは `opshub` CLI / service 経由」と定めたため、lock は service 層に閉じて実装できる。しかし lock の **粒度** (どの単位で排他するか) は ADR-0004 では未定。principles.md Open Q #2 で「`task:<id>` / `project:<id>` / `global` の三階層案」が記録されている。

粒度は次の観点で決める必要がある。

1. **意味的な保護対象**: 「衝突したら困る最小単位」は task の状態遷移か、project レベルの構造変更か、opshub 全体のメンテナンスか
2. **コスト**: 粒度を細かくしすぎると lock の管理コスト (event 量、CLI overhead) が増える。粗くしすぎると並列性が落ちる
3. **event-sourced との整合**: lock の取得・解放自体が event 化される (`LockAcquired` / `LockReleased`)。粒度は scope 文字列で表現できる程度に単純である必要
4. **SQLite 単一プロセス前提 (ADR-0002)**: 分散 lock broker は不要。lock state は同じ SQLite に persist する
5. **CLI プロセス境界**: 1 lock は複数 CLI invocation を跨いで保持される。SQL の pessimistic row lock は connection 終了で外れるため使えない

## Decision

Lock scope は **3 階層** とする。すべて scope 文字列で表現し、`locks` projection の `scope_type` + `scope_id` 列に分解して格納する。

| Scope | 形式 | 用途 |
|---|---|---|
| `task:<task-ulid>` | task ID で指定 | 単一 task の state-modifying 操作を排他。`opshub task complete <id>` / `opshub task activate <id>` 等が取得 |
| `project:<project-ulid>` | project ID で指定 | project スコープの構造変更を排他。project 概念自体は step 6 (work sessions) で定義。Phase 2.x で project entity を導入するまで pre-allocated scope として残す |
| `global:` | 引数なし (scope_id は空) | opshub 全体を排他。`opshub projections rebuild` / `opshub db migrate` / `opshub workspace generate` 等のメンテナンスが取得 |

### Owner

Lock owner は `(actor, work_session_id)` の組。

- `actor` は CLI command の `--actor` フラグ または環境変数 `OPSHUB_ACTOR` から取得 (例: `agent:claude` / `cli:ozzy`)
- `work_session_id` は現在の work session の ULID。session 外で取得した lock は `work_session_id = NULL` (ad-hoc 操作用)

### Conflict semantics

- **同 owner の再 acquire**: 同 scope に既に同 owner の lock がある場合、新しい lock event を append せず既存 lock を返す (idempotent reacquire)
- **異 owner の acquire**: scope に既に lock が存在し owner が異なる場合、`ConflictError` を raise (fail-fast)。retry は呼び出し側の責任 (CLI exit code → agent / 人間が判断)
- **異 owner の release**: lock owner と release を試みた owner が一致しない場合、`OwnershipError` を raise。lock は外れない

### Scope 間の関係

- **`global:` lock は他のすべての lock を block する**。`global:` 取得時に既存 task / project lock があれば ConflictError。逆も同様 (任意の scope の lock 取得時に `global:` lock があれば ConflictError)
- **`task:` と `project:` は互いに独立**。`project:X` lock を持っていても、`task:Y` (X 配下の task) の acquire は通る。理由は §3.3 (Negative §2) を参照
- **同一 scope_type 内の異なる scope_id は独立**。`task:A` と `task:B` は競合しない

### 永続化

Lock state は event-sourced。Phase 2 step 1 で導入する `LockAcquired` / `LockReleased` event が authoritative。`locks` projection table は rebuild 可能。

| 列 | 型 | 説明 |
|---|---|---|
| `id` | TEXT PK | lock ULID (= LockAcquired event の aggregate_id) |
| `scope_type` | TEXT NOT NULL CHECK | `'task'` / `'project'` / `'global'` |
| `scope_id` | TEXT NOT NULL | task ULID / project ULID / 空文字 (global) |
| `actor` | TEXT NOT NULL | lock owner の actor |
| `work_session_id` | TEXT NULL | lock owner の work session (ad-hoc は NULL) |
| `acquired_at` | TIMESTAMP NOT NULL | LockAcquired event の occurred_at |
| `released_at` | TIMESTAMP NULL | LockReleased event の occurred_at。NULL = active |

`UNIQUE(scope_type, scope_id) WHERE released_at IS NULL` 相当の partial index で「同 scope の active lock は最大 1 件」を強制する (SQLite の partial index で実装可能)。

### Lock の TTL

Phase 2 では **TTL なし**。lock は明示 release されるまで保持される。crash 等で lock がリークした場合、`opshub lock release --force <id>` を提供する (admin 操作扱い、別 owner からの release を許す)。TTL 自動解放は Phase 2.x で再評価。

## Consequences

### Positive

1. **event-sourced との一貫性** — lock も他のすべての状態と同じく event 列から rebuild できる。`projections rebuild` で `locks` も再現可能
2. **scope の柔軟性** — task 単位の細かい排他から `global:` のメンテナンス排他まで同じ機構で表現できる
3. **SQLite 単一プロセス前提と整合** — 外部 broker (Redis / etcd) 不要。state は同じ DB に閉じる
4. **CLI プロセス境界を跨ぐ** — SQL row lock と異なり connection 終了で外れない
5. **多 agent coordination の primitive** — `acquire → execute → release` のパターンを architecture §6 推奨フローに沿って実装できる
6. **owner 識別が二段** — actor と work_session の組により、同じ agent でも別 session の lock を区別できる (誤 release を防ぐ)

### Negative / Trade-offs

1. **3 scope = 認知負荷** — ユーザー / agent が「どの scope を取るべきか」を毎回判断する必要がある
   - 緩和: CLI command 側で「この操作はこの scope」と固定する (例: `opshub task complete` は自動的に `task:<id>` を取る)。ユーザーが明示的に `opshub lock acquire` を叩くのは想定外動作 / 高度な操作のみ
2. **階層的 block なし** — `project:X` lock が `task:Y in X` を block しない。project 全体を厳密に排他したい場合、呼び出し側で project lock + 配下 task lock の両方を取る必要がある
   - 緩和: Phase 2 では `tasks` projection に `project_id` 列がなく、所属関係が schema 上未確定。実需要が出た段階 (Phase 2.x or 3) で「project lock が配下 task lock を含意する」semantics を ADR で追加 supersede する余地を残す
3. **`global:` lock の contention** — メンテナンス中は全 agent が block される
   - 緩和: メンテナンス操作 (`projections rebuild` / `db migrate`) は短時間で完了する設計を前提とする。長時間化する場合は別途 ADR で評価
4. **TTL なし** — crashed agent が lock を保持したままになるリスク
   - 緩和: `opshub lock release --force <id>` を admin 経路として提供。Phase 2.x で TTL 自動解放 / heartbeat 方式を再評価
5. **event 量の増加** — lock acquire/release が頻発すると event 列が膨らむ
   - 緩和: 1 lock = 2 event (acquire + release) で済むため、commit per operation のオーダーに収まる。archive 戦略は Phase 4 で評価 (ADR-0002 と整合)

## Alternatives Considered

### 1. 2 階層 (`task:` + `global:` のみ)

却下理由:

- Phase 2 で work session が複数 task をまたぐ前提のため、project レベルの coordination 単位は概念として必要
- `project:` scope を後から足す方が、最初から 3 階層で出して使われない方より破壊的改修コストが高い (locks projection の `scope_type` CHECK 制約を変更する必要)
- 3 階層にしても CLI が自動で適切な scope を選ぶ前提なので、ユーザー視点の認知負荷増加は実質的に小さい

### 2. 階層的 block (project lock が配下 task lock を含意)

却下理由:

- Phase 2 時点で `tasks` projection に `project_id` 列がなく、所属関係 schema が未確定 (project entity 自体が step 6 で初めて導入される)
- 階層 block を実装するには「lock acquire 時に scope の親に lock がないか」を毎回 scan する必要があり、event-sourced の純粋関数 projector と相性が悪い (locks projection の応答性が落ちる)
- 必要なら Phase 2.x で新 ADR (例: ADR-0014 Hierarchical Lock Blocking) で supersede する道を残す

### 3. 分散 lock broker (Redis / etcd)

却下理由:

- ADR-0002 単一 SQLite 原則違反
- local-first (Principles §1) と相反。外部プロセスを増やすと配布 / セットアップが複雑化
- 単一プロセス前提の OpsHub では over-engineering

### 4. Reader/Writer locks

却下理由:

- OpsHub の操作は read-heavy ではあるが、read は projection 直接読みで lock を取らない設計のため、reader lock の出番がない
- writer lock のみ必要なら exclusive lock で十分

### 5. SQL pessimistic row locking (`SELECT FOR UPDATE`)

却下理由:

- SQLite は `SELECT FOR UPDATE` 未対応 (BEGIN IMMEDIATE で代替可能だが意味が異なる)
- lock は CLI 1 invocation を超えて保持される必要があり、connection 終了で外れる row lock は使えない
- audit / replay の観点でも、event を残さず暗黙に取られる SQL lock は event-sourced 原則に反する

### 6. 楽観的並行制御 (CAS / version)

却下理由:

- event store は append-only なので、version 列での CAS は機械的に実装可能ではある
- しかし「複数 step を跨ぐ atomic な意図」(例: lock acquire → state inspect → state mutate → release) を表現できない
- 楽観 retry は agent の操作意図を毎回 re-evaluate する必要があり、CLI から呼ばれる文脈では複雑度に見合わない

## 関連

- [Principles 4 (Agent Runtime Boundary)](../principles.md)
- [Architecture 6 (Multi-Agent Coordination)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0009: Multi-Agent Neutrality](0009-multi-agent-neutrality.md)
- [Phase 2 Implementation Plan](../phase-2-plan.md) (§3 Lock service 部分が本 ADR を前提)
- Phase 2 epic: #23
