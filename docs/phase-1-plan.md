# Phase 1 Implementation Plan

> Status: Draft (in active design). Last reviewed: 2026-05-16.

Phase 1 の目的は「event store + tasks + CLI + markdown 生成」が一気通貫で動く foundation を提供すること。Connector / Vector / Lock / Triage は含まない (Phase 2 以降)。

## 1. 着手前に解消する Python 適合 TODO

bootstrap で commons の Node 寄り設定が混入している。Phase 1 の最初の 1〜2 commit で次を順に解消する。

1. **`biome.json` を削除** — Python では使わない。`.commons/sync.yaml` の `pinned:` に `biome.json` を入れて以後の sync で再混入を防ぐ。
2. **`.mise.toml` を Python 用に置換** — `node` / `pnpm` の行を削除し、`uv` / `ruff` / `mypy` を追加。`shellcheck` / `shfmt` / `gitleaks` / `trivy` / `yamllint` 等の汎用ツールは維持。pinned に入れる。
3. **`AGENTS.md` の Tech Stack / 主要コマンドを書き換え** — pnpm 系コマンドを uv 系 (`uv sync` / `uv run pytest` / `uv run ruff check` 等) に。
4. **`.vscode/extensions.json` を Python 向けに調整** — `biomejs.biome` を削除、`charliermarsh.ruff` / `ms-python.python` / `ms-python.mypy-type-checker` を追加。pinned に入れる。
5. **`.devcontainer/Dockerfile`** — Node 24 ベースから Python 3.13 + uv ベースに切替。pinned に入れる。
6. **`lefthook.yaml` を Python 用に拡張** — `lefthook-base.yaml` を extends しつつ pre-commit に `ruff check --fix` / `ruff format` / `mypy` を追加。
7. **`pyproject.toml` + `.python-version` を新規作成** — PEP 621 + uv 設定 + ruff / mypy / pyright / pytest の `[tool.*]` セクション。`requires-python = ">=3.13"`。**ADR-0001 の `[project.optional-dependencies]` 設計 (`vector` / `local-embedding` / `api-embedding-*` / `connectors-*` / `dev` / `all`) を骨格として配置**。core は ML 依存ゼロを維持。

これらを終えた状態で初めて Phase 1 の実装 commit に入る。

## 2. Phase 1 Commit 順序

Conventional Commits 準拠。各 commit は ~200-500 行を目安にレビュー可能サイズに収める。

### 2.1 Bootstrap (✅ PR #1 で完了済)

bootstrap 部分は 7 TODO を 4 commit に集約し PR #1 で squash merge 済み。

| # | Commit | 状態 |
|---|---|---|
| 1 | `docs: add design-phase principles, architecture, and adrs` | ✅ PR #1 |
| 2 | `chore: scaffold ozzy-labs project configuration for python` | ✅ PR #1 |
| 3 | `chore: bootstrap python project skeleton` | ✅ PR #1 |
| 4 | `ci: add lint/type/test workflow` | ✅ PR #1 |

### 2.2 機能実装 (13 step、1 step = 1 PR)

| # | Commit | 概要 | 想定 PR # |
|---|---|---|---|
| 5 | `feat(core): foundational utilities` | `core/{ids,time,logging,errors,config}.py`。ULID 生成、tz-aware datetime helpers、structlog config、`OpsHubError` 基底、Pydantic Settings 基底。後続 step が依存する foundation | PR #5 |
| 6 | `feat(db): set up sqlalchemy engine and alembic` | `db/{engine,unit_of_work,schema}.py` + alembic init (`src/opshub/db/migrations/`)。`~/.local/share/opshub/db/opshub.sqlite` デフォルトパス解決 | PR #6 |
| 7 | `feat(db): initial migration with events and embeddings tables` | `0001_events.py` + `0002_embeddings.py` の 2 migration。`embeddings` は Phase 4 まで空運用、`UNIQUE(entity_type, entity_id, model_id, model_version)` 制約 (ADR-0012) | PR #7 |
| 8 | `feat(domain): task events` | `domain/events/{base,task}.py`。`DomainEvent` 抽象 + `TaskCreated` / `TaskActivated` / `TaskCompleted`、Pydantic `frozen=True` + discriminator + `schema_version` (ADR-0002) | PR #8 |
| 9 | `feat(services): task service` | `services/task_service.py`。CLI / agent からの command を検証 → event を append → projector に通知。lock は Phase 2 で実装 (ADR-0004) | PR #9 |
| 10 | `feat(projections): tasks projection + replay test` | `projections/{base,tasks,rebuild}.py`。冪等テスト (`projections rebuild` を 2 回 → 同一結果) を `tests/integration/` に (Principles §8) | PR #10 |
| 11 | `feat(vectors): pluggable embedder/store protocols` | `vectors/{embedder,store}.py` の Protocol 定義のみ (Phase 4 用、実装ゼロ)。interface freeze 用 unit test 併設 (ADR-0012) | PR #11 |
| 12 | `feat(config): settings for storage/workspace/embedding` | Pydantic Settings 拡張。`[storage]` (db path / cache path) + `[workspace]` (workspace root) + `[embedding]` (backend = "disabled" デフォルト) の 3 section (ADR-0012) | PR #12 |
| 13 | `feat(cli): bootstrap commands (init, db migrate)` | `opshub init` (first-time setup: dir 作成、config 初期化、migration 適用)、`opshub db migrate` (apply pending)。Typer lazy import 規約に従う (ADR-0001) | PR #13 |
| 14 | `feat(cli): task commands (create, list)` | `opshub task create` / `task list`。`--format md` 等の出力オプションを Phase 1 で確定 | PR #14 |
| 15 | `feat(cli): ops commands (projections, embeddings)` | `opshub projections rebuild`、`opshub embeddings status` (backend=disabled / 件数 0 を表示) | PR #15 |
| 16 | `feat(markdown): task list rendering to workspace` | `markdown/{render,tasks}.py` + `opshub workspace generate`。Jinja2 template、冪等テスト (Principles §8) | PR #16 |
| 17 | `test: end-to-end lifecycle + docs: readme quickstart` | `tests/integration/test_lifecycle.py` (create → list → generate)、README に Quickstart、cold-start 観測 (`time opshub version`: 上限 300ms / 目標 120ms) | PR #17 |

**論理グルーピング** (milestone 候補):

- **Foundation** (PR #5-#7 / step 5-7): core utilities + db engine + 初回 migration
- **Event Sourcing Core** (PR #8-#10 / step 8-10): domain events + service + projections
- **Interfaces** (PR #11-#12 / step 11-12): Phase 4 用 Protocol + 設定
- **CLI** (PR #13-#15 / step 13-15): 3 種類の CLI command 群
- **Output & Verification** (PR #16-#17 / step 16-17): markdown 生成 + E2E

各 PR は前 PR を base に main から作成。これで CI が毎回最新の main で走り、変化を最小化できる。

## 3. Phase 1 完了の定義 (DoD)

### 機能 DoD

1. `uv run opshub init` が初回 setup (DB dir 作成 / config 初期化 / migration 適用) を完了する
2. `uv run opshub task create "first task"` が動き、event が `events` テーブルに 1 行 append される
3. `uv run opshub task list --format md` が markdown を返す
4. `uv run opshub workspace generate` が `~/opshub/workspace/generated/tasks/*.md` を作る
5. `uv run opshub projections rebuild` が冪等 (2 回実行で同じ結果) — integration test で検証
6. `uv run opshub workspace generate` も冪等 (2 回実行で同じ markdown) — integration test で検証
7. `uv run opshub embeddings status` が `backend=disabled` / `embeddings: 0 rows` を返す (Phase 1 では機能を出さない、ADR-0012)

### 品質 DoD

1. `Embedder` / `VectorStore` Protocol が pyright + mypy --strict を通過 (実装なしでも型整合を CI で freeze)
2. `pyproject.toml` の `[project.optional-dependencies]` が ADR-0001 設計どおりに揃い、`uv tool install --from . opshub` (core only) が ML 依存なしで完了する
3. CI で `uv sync --locked` → `ruff check` → `ruff format --check` → `pyright` → `mypy src tests` → `pytest` が緑
4. `lefthook run pre-commit --all-files` がエラーなしで通る
5. cold start 観測: `time opshub version` が **上限 300ms 以下** (ストレッチ目標 120ms、ADR-0001 Negative §1)。lazy import 規約の徹底で達成

### ドキュメント DoD

1. AGENTS.md / CLAUDE.md / architecture.md / principles.md に Phase 1 完了状態が反映されている
2. README に Quickstart セクション (`uv tool install` → `opshub init` → `opshub task create` までの最短経路)
3. ADR-0001 〜 0007 および ADR-0012 が `Accepted` に昇格 (handbook ADR 運用に従い `Proposed` → `Accepted` 切替)

## 4. Phase 1 完了時に解消すべき Open Questions

ADR-0001 / ADR-0012 確定により Phase 1 で再確認するだけのもの:

1. **Task runner = `just`** — ADR-0001 で確定済み。Bootstrap step 4 (CI) で動作確認済 (PR #1)
2. **Embedding 戦略 = Pluggable** — ADR-0012 で確定済み。Phase 1 step 11-12 で interface + config パースが揃う
3. **`migrations/` 配置 = `src/opshub/db/migrations/`** — repository-structure.md Open Question 5 を本 plan で確定 (step 6)。理由: `importlib.resources` で wheel install 後もアクセス可能、package との整合

Phase 1 で骨格のみ用意し、Phase 2 以降で機能化:

1. **Lock の最小実装** — Phase 1 では lock は未実装でよいが、Phase 2 のために `locks` テーブル骨格は schema に入れておく (空のまま運用)
2. **`embeddings` テーブル骨格** — Phase 1 step 7 で migration を作り空のまま運用、Phase 4 で活用 (ADR-0012)

Phase 1 内では確定しなくてよいもの (Phase 2-4 に持ち越す):

1. LLM 利用方針 (embedding API 呼び出しと triage/summary 用 API の運用線引き、Phase 2)
2. SaaS token 保管方式 (Phase 3 で connector 着手時)
3. Embedding 具体モデル選定 (Phase 4、ADR-0012 Open Questions 1-2)

## 5. Issue / PR 戦略

Phase 1 は **1 tracking issue + 13 PRs** で管理する。

### 5.1 Tracking Issue

`phase-issue` skill で **Phase 1 tracking issue** を起票する。issue body に含める要素:

- Phase 1 のゴール (event store + tasks + CLI + markdown 生成)
- §2.2 の 13 step を 13 PR のチェックリストとして列挙
- §3 の DoD (機能 / 品質 / ドキュメント)
- Cross-session handoff context (AI agent が後から再開できるよう、現在着手中の step を記録)
- ADR-0001 / ADR-0002 / ADR-0012 等の relevant link
- Phase 2 outlook (§6 と同期)

### 5.2 PR 戦略

- **1 step = 1 PR = 1 commit (squash 後)**
- PR タイトル: 該当 commit message と一致 (例: `feat(core): foundational utilities`)
- PR body: `Refs #<tracking-issue>` + Summary (1-3 bullet) + Test plan チェックリスト
- 各 PR は **直前 PR の merge を待ってから main を base に作成**。これで CI が毎回最新 main で走り、PR 間の depend hell を避けられる
- マージ方法は **squash merge のみ** (CLAUDE.md / `.claude/rules/git-workflow.md` 準拠)
- PR タイトル / branch 名は Conventional Commits 形式が CI (`pr-check.yaml`) で検証される

### 5.3 Milestone (任意)

GitHub Milestones を使う場合、§2.2 の 5 group を milestone 化:

| Milestone | PR | step |
|---|---|---|
| Phase 1: Foundation | PR #5-#7 | 5-7 |
| Phase 1: Event Sourcing Core | PR #8-#10 | 8-10 |
| Phase 1: Interfaces | PR #11-#12 | 11-12 |
| Phase 1: CLI | PR #13-#15 | 13-15 |
| Phase 1: Output & Verification | PR #16-#17 | 16-17 |

milestone は optional。tracking issue のチェックリストで十分なら省略可。

## 6. Phase 2 へのつなぎ

Phase 1 完了直後に Phase 2 epic issue を起票 (`phase-issue` skill 利用)。Phase 2 のスコープ:

- `inbox triage` ワークフロー
- `decisions` テーブル + `DecisionRecorded` event
- `work_sessions` + `agent_runs`
- `Lock` の実装 (粒度確定)
- `handoffs` 機構

Phase 2 着手時点で連動して見直すべき docs: principles 7 (Connector Contract) / architecture 4 (データモデル) / architecture 6 (Multi-Agent Coordination)。

## Open Questions (本ドキュメント固有)

1. step 7 の `events` / `embeddings` テーブル schema 詳細 (列定義・index・partitioning)
2. step 11 の `Embedder` / `VectorStore` Protocol を freeze する unit test の粒度 (interface 安定性をどこまで CI で縛るか)
3. step 12 の config パースで `[connectors]` セクションを Phase 1 から先回りするか (現状は Phase 3 で追加予定)
4. step 14 の CLI 引数命名 (`--format` の名前を `--output` にするか、`--json` のような flag にするか)
5. PyPI 公開のタイミング (Phase 1 完了直後か、Phase 2 完了時か、Phase 3 末か)
6. `opshub init` と `opshub db migrate` の責務分離 (init は migration を内包すべきか、独立コマンドとして残すか)
