# Phase 2 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17.

Phase 2 の目的は **Coordination layer** を Phase 1 の foundation 上に追加すること。inbox triage / decisions / locks / work sessions + agent runs / handoffs の 5 ワークストリームを event-sourced + projection + CLI + markdown のパターンで揃え、複数 agent + 人手の同時作業を安全に扱える状態にする。Connector (Phase 3) / Semantic recall (Phase 4) は範囲外。

## 1. 着手前に解消する TODO

Phase 1 完了時点で Phase 2 着手前に解消が必要な事項は **なし**。Phase 1 で `core/` / `db/` / `domain/events/` / `services/` / `projections/` / `cli/` / `markdown/` の境界が確立しており、Phase 2 は同じ pattern を踏襲する。

**確定済み事項** (Phase 2 着手前に解消済):

1. **Lock 粒度** — ADR-0013 で `task:<id>` / `project:<id>` / `global:` の 3 階層 + fail-fast conflict semantics を採択済 (step 5 はその通り実装)
2. **Inbox 入力経路** — Phase 2 は CLI 経由の manual enqueue のみ (`opshub inbox add`)。connector enqueue は Phase 3 で別途
3. **Triage の LLM 利用** — Phase 2 は LLM を呼ばない。triage は CLI コマンドが明示的に `--to-task` / `--decision` / `--discard` を指定する (principles.md Open Q #1 の判断は Phase 3+ に持ち越し)
4. **ファイル系 inbox の扱い** — Phase 2 では `workspace/{inbox,plans,notes}/*.md` の自動 ingest は実装しない。inbox は CLI 経由の manual enqueue のみ。ファイル系 ingest は Phase 3 connector framework で扱う
5. **Project entity の導入タイミング** — Phase 2 step 5 では `project:` lock scope は schema 上のみ予約し CLI から acquire 不可 (NotImplementedError)。Phase 2 内では project entity 自体を導入しない。Phase 2.x または Phase 3 で別途検討

## 2. Phase 2 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。各 PR は前 PR の merge を待ってから main を base に作成。

### 2.1 Foundation (2 PR)

step 1-2 は Phase 2 全体の土台。1 では全 event 型を一度に追加し、2 では全 projection table の migration を一度に追加する。後続 step (3-7) が並列着手可能になる。

| # | Commit | 概要 | 想定 PR # |
|---|---|---|---|
| 1 | `feat(domain): phase 2 events` | `domain/events/{inbox,decision,coordination,handoff}.py` を追加。`ItemEnqueued` / `ItemTriaged` / `DecisionRecorded` / `WorkSessionStarted` / `WorkSessionEnded` / `AgentRunStarted` / `AgentRunEnded` / `LockAcquired` / `LockReleased` / `HandoffOpened` / `HandoffClosed` の 11 event 型。`Phase2Event` discriminated union と `AllEvent = TaskEvent \| Phase2Event` を export。`SqlAlchemyEventStore.iter_all()` の TypeAdapter を更新 | PR #N |
| 2 | `feat(db): phase 2 projection tables` | migration `0004` - `0009` を **6 ファイルに分割** して 1 migration = 1 table を維持 (Phase 1 convention 準拠)。順序: `0004_create_inbox_items_table.py` / `0005_create_decisions_table.py` / `0006_create_work_sessions_table.py` / `0007_create_agent_runs_table.py` / `0008_create_locks_table.py` / `0009_create_handoffs_table.py`。`locks` migration には `UNIQUE(scope_type, scope_id) WHERE released_at IS NULL` partial unique index を含める。`opshub.db.schema.metadata` に各 Table 定義を登録 (projections module で公開) | PR #N |

### 2.2 Vertical features (5 PR、並列可)

step 3-7 は **互いに独立** で、step 1-2 完了後に同時着手可能。各 step は 1 ワークストリームを service + projection + CLI まで縦に貫く (markdown は step 8 で一括)。

| # | Commit | 概要 | 想定 PR # |
|---|---|---|---|
| 3 | `feat(coordination): inbox triage workflow` | `services/inbox_service.py` (`enqueue` / `triage` command) + `projections/inbox.py` (reducer) + `cli/inbox.py` (`opshub inbox add` / `list` / `triage --to-task <title>\|--decision <text>\|--discard <reason>`) | PR #N |
| 4 | `feat(coordination): decisions workflow` | `services/decision_service.py` + `projections/decisions.py` + `cli/decision.py` (`opshub decision record "<text>"` / `list`)。inbox triage から派生するルートも step 3 と整合する | PR #N |
| 5 | `feat(coordination): lock implementation` | `services/lock_service.py` (acquire / release、failure semantics は fail-fast) + `projections/locks.py` + `cli/lock.py` (`opshub lock acquire <scope>` / `release <id>` / `list`)。ADR-0013 (既に main に merged) に従って実装。lock owner = (actor, work_session_id) は `OPSHUB_ACTOR` / `OPSHUB_WORK_SESSION_ID` 環境変数または CLI フラグから解決 (step 5 PR 内で wiring helper として実装)。`project:` scope は schema 上は予約するが CLI からは acquire 不可 (NotImplementedError) | PR #N |
| 6 | `feat(coordination): work sessions and agent runs` | `services/{agent_session_service,work_session_service}.py` + `projections/{work_sessions,agent_runs}.py` + `cli/{session,agent}.py` (`opshub session start [--scope]` / `end`、`opshub agent run begin <agent-name>` / `end --summary`)。session は agent_run の outer bracket | PR #N |
| 7 | `feat(coordination): handoffs workflow` | `services/handoff_service.py` + `projections/handoffs.py` + `cli/handoff.py` (`opshub handoff open --from <a> --to <b> --topic "<...>"` / `close <id> --note`)。markdown 出力は step 8 | PR #N |

### 2.3 Surfaces (2 PR)

| # | Commit | 概要 | 想定 PR # |
|---|---|---|---|
| 8 | `feat(markdown): inbox/decisions/handoffs rendering` | `markdown/{inbox,decisions,handoffs}.py` + Jinja2 template 追加。`opshub workspace generate` を拡張して `~/opshub/workspace/generated/{inbox,decisions,handoffs}/*.md` を出力。冪等テスト (step 16 と同じパターン) | PR #N |
| 9 | `test: phase 2 end-to-end + docs` | `tests/integration/test_coordination_lifecycle.py` (enqueue → triage → task / decision、lock acquire / release、session + agent run bracket、handoff open / close)。README に Phase 2 command 一覧を追記。AGENTS.md / CLAUDE.md / principles.md / architecture.md に Phase 2 完了状態を反映 | PR #N |

**論理グルーピング** (milestone 候補):

- **Foundation** (PR #1-#2 / step 1-2): event 型 + projection table 一括追加
- **Vertical Features** (PR #3-#7 / step 3-7): 5 ワークストリームの service + projection + CLI を並列実装
- **Surfaces** (PR #8-#9 / step 8-9): markdown 拡張 + E2E + docs

`drive` orchestration の Wave 構成 (DAG):

```text
Wave 1: step 1 (events)
Wave 2: step 2 (migration、step 1 依存)
Wave 3: step 3,4,5,6,7 (5 並列、step 1+2 依存)
Wave 4: step 8 (markdown、step 3,4,7 依存)
Wave 5: step 9 (E2E、全 step 依存)
```

Phase 1 の経験 (`auto-merge-fires-fast` memory) から、wave 内並列は 5 並列でも `cli/app.py` への add_typer 衝突は許容範囲 (rebase + `--force-with-lease` で対応可)。新規 subcommand を持つ step 3-7 が同一ファイルを触るが、Phase 1 step 14-16 と同じ pattern で解決済。

## 3. Phase 2 完了の定義 (DoD)

### 機能 DoD

1. `opshub inbox add "review PR #99"` で event が append され、`inbox_items` projection に行が立つ
2. `opshub inbox triage <inbox-id> --to-task "review PR #99"` で `TaskCreated` + `ItemTriaged` event が連鎖し、task が draft で作られ inbox から消える
3. `opshub inbox triage <inbox-id> --decision "merge as-is"` で `DecisionRecorded` event が記録される
4. `opshub decision record "use python 3.13"` 単独でも decision が記録される
5. `opshub lock acquire task:<id> --actor agent:claude` → 同 scope への次の acquire は ConflictError で fail-fast
6. `opshub lock release <lock-id>` で解放
7. `opshub session start` → `opshub agent run begin codex` → `opshub agent run end --summary "..."` → `opshub session end` の bracket が正しく event 列に記録される
8. `opshub handoff open --from agent:claude --to ozzy --topic "..."` で handoff が記録され、markdown が出力される
9. `opshub workspace generate` を 2 回実行しても同じ markdown (冪等、Phase 1 と同じ性質を Phase 2 entity でも維持)
10. `opshub projections rebuild` が Phase 2 entity を含めて冪等

### 品質 DoD

1. CI で `uv sync --locked` → `ruff` → `pyright` → `mypy src tests` → `pytest` が緑 (新規テスト含む)
2. `lefthook run pre-commit --all-files` が緑
3. cold start: `time opshub --help` が **上限 300ms 以下** を維持 (lazy import 規約を Phase 2 cli にも徹底)
4. Lock service の concurrency 性質を unit + integration で網羅 (acquire 競合、release 後 re-acquire、scope 違いの independence、owner 不一致での release reject)
5. event-sourced replay 性: `projections rebuild` が Phase 2 projection についても idempotent (CI で property test)

### ドキュメント DoD

1. README に Phase 2 command 一覧を追記
2. AGENTS.md / CLAUDE.md / docs/principles.md / docs/architecture.md に Phase 2 完了状態を反映 (principles §9 Phased Delivery テーブルを Phase 2 = ✅ Complete に更新)
3. **ADR-0013 (Lock 粒度)** を新規起票し Accepted に昇格 (step 5 の前提) — ✅ 完了 (PR #24)
4. `docs/repository-structure.md` の `services/` / `projections/` / `cli/` セクションに Phase 2 ファイルを追記
5. `docs/data-model.md` (Phase 1 plan で「後日作成予定」だった) を本 phase で起こすか、本 plan に骨子を追記する判断

## 4. Phase 2 完了時に解消する Open Questions

着手前に確定済 (§1 参照):

1. **Lock 粒度 (旧 principles.md Open Q #2)** — ADR-0013 で確定済 (`task:` / `project:` / `global:` の 3 階層、owner = (actor, work_session_id)、fail-fast)
2. **Project 概念の導入タイミング** — Phase 2 では schema 予約のみで CLI からは acquire 不可。具体的な `projects` projection の実装は Phase 2.x or Phase 3 で別途検討

Phase 2 内では確定しなくてよい (Phase 3+ 持ち越し):

1. **LLM 利用方針 (principles.md Open Q #1)** — Phase 2 triage は LLM を使わない。Phase 3 で connector 由来 inbox を LLM 補助 triage する余地を残す
2. **SaaS token 保管方式 (principles.md Open Q #2)** — Phase 3 (connector) で確定
3. **Embedding 戦略の具象実装 (ADR-0012)** — Phase 4

## 5. Issue / PR 戦略

Phase 2 は **1 tracking issue + 9 PR** で管理する (Phase 1 と同じ運用)。

### 5.1 Tracking Issue

`phase-issue` skill で Phase 2 tracking issue を起票する。issue body には:

- Phase 2 のゴール (coordination layer)
- §2 の 9 step を 9 PR のチェックリストとして列挙
- §3 の DoD
- Cross-session handoff context
- ADR-0002 / ADR-0004 / ADR-0009 / ADR-0013 (Lock 粒度、main にマージ済) のリンク
- Phase 3 outlook (§6 と同期)

### 5.2 PR 戦略

- **1 step = 1 PR = 1 commit (squash 後)**
- PR タイトル: 該当 commit message と一致
- PR body: `Refs #<tracking-issue>` + Summary + Test plan
- step 1-2 は順次、step 3-7 は並列可、step 8-9 は順次
- マージ方法は **squash merge のみ**
- `想定 PR #` 列は **forecast** (実 PR # は phase-1-plan §2.2 と同様にずれる前提、auto-memory `pr-number-forecast-not-canonical` 参照)

### 5.3 Milestone (任意)

GitHub Milestones を使う場合、§2 の 3 group を milestone 化:

| Milestone | PR | step |
|---|---|---|
| Phase 2: Foundation | PR 1-2 | 1-2 |
| Phase 2: Vertical Features | PR 3-7 | 3-7 |
| Phase 2: Surfaces | PR 8-9 | 8-9 |

milestone は optional。tracking issue のチェックリストで十分なら省略可。

## 6. Phase 3 へのつなぎ

Phase 2 完了直後に Phase 3 epic issue を起票 (`phase-issue` skill 利用)。Phase 3 のスコープ:

- Connector framework (`connectors/base.py` の Connector 抽象、Phase 1 で defer されていたもの)
- GitHub / Slack / Microsoft 365 / Box の具象 connector (順次着手、全部入りは強制しない)
- `connector_cursors` projection と incremental sync
- SaaS token 保管 (`keyring` library / `pass` / `secret-tool` のいずれかを ADR で決定)
- Connector 由来 source の inbox 自動 enqueue (Phase 2 の `InboxService.enqueue` を呼び出す形)

Phase 3 着手時点で連動して見直すべき docs: principles §7 (Connector Contract) / architecture §4 (`connector_cursors` / `sources`) / ADR-0010 (Connector Contract) / ADR-0005 (External Content Minimization)。

## Open Questions (本ドキュメント固有)

1. Work session と project の関係 (session が複数 project に跨れるか、1 session = 1 project か)
2. Handoff の有効期限 (auto-close TTL を持つか、明示 close まで open のままか)
3. `opshub inbox triage --to-task <title>` が新規 task を作るとき、source ref から body を継承する規約 (今は title のみ受け取る案)
4. `tests/integration/test_coordination_lifecycle.py` の scope: 1 ファイルで全 workstream を貫くか、workstream ごとに分けるか (Phase 1 は `test_lifecycle.py` 1 本にまとめた)
