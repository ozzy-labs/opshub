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

## 2. Phase 1 Commit 順序 (17 step)

Conventional Commits 準拠。各 commit は ~200-500 行を目安にレビュー可能サイズに収める。

| # | Commit | 概要 |
|---|---|---|
| 1 | `chore: replace gitignore additions for python` | `.gitignore` に Python 用 (`__pycache__/` / `.venv/` / `*.sqlite*` / `db/` 等) を append |
| 2 | `chore: pin python tool versions in mise` | `.mise.toml` を Python 用に置換 (TODO 2) |
| 3 | `chore: bootstrap uv project skeleton` | `pyproject.toml` (ADR-0001 の `[project.optional-dependencies]` 骨格込み) + `uv.lock` + `src/opshub/__init__.py` + `src/opshub/__main__.py` (TODO 7) |
| 4 | `chore: configure ruff/mypy/pyright/pytest` | `pyproject.toml` 内ツール設定 (pyright も含む) + `tests/` 骨格 + `.python-version` |
| 5 | `chore: add python pre-commit hooks` | `lefthook.yaml` に ruff / mypy 追加 (TODO 6) |
| 6 | `chore: switch devcontainer to python base` | `.devcontainer/Dockerfile` Python 化 (TODO 5) |
| 7 | `chore: drop biome and switch vscode extensions` | `biome.json` 削除 + extensions 入れ替え (TODO 1, 4) + AGENTS.md Tech Stack 修正 (TODO 3) |
| 8 | `ci: add github actions for lint/type/test` | `.github/workflows/ci.yaml` (uv sync --locked + ruff + pyright + mypy + pytest) |
| 9 | `feat(db): initial schema with events and embeddings tables` | Alembic init + `events` テーブル + `embeddings` テーブル骨格 (Phase 1 では空運用、ADR-0012) |
| 10 | `feat(domain): TaskCreated/TaskActivated/TaskCompleted events` | Pydantic event 型 (`frozen=True` + discriminator) |
| 11 | `feat(services): task_service with event append` | service が event を append |
| 12 | `feat(projections): tasks projection + rebuild` | projector + `opshub projections rebuild` |
| 13 | `feat(vectors): pluggable embedder/store protocols` | `vectors/embedder.py` + `vectors/store.py` の Protocol のみ定義 (実装なし、ADR-0012)。Protocol freeze 用 unit test を併設 |
| 14 | `feat(config): embedding section parsing` | `~/.config/opshub/config.toml` の `[embedding]` セクションを Pydantic Settings でパース (Phase 1 は `disabled` のみ動作、ADR-0012) |
| 15 | `feat(cli): opshub init / db migrate / task create / task list / projections rebuild / embeddings status` | Typer command 群 (`embeddings status` は backend=disabled / 件数 0 を表示) |
| 16 | `feat(markdown): basic task list rendering to workspace` | Jinja2 template + `opshub workspace generate` |
| 17 | `test: end-to-end task lifecycle` + `docs: README quickstart` | E2E テスト + Quickstart |

commit 1-8 は **scaffolding / 環境**、9-12 が **event store + tasks の本筋**、13-14 が **Phase 4 への先回り (interface のみ)**、15-17 が **CLI / workspace / E2E**。前半が終わってから後半に入る。

## 3. Phase 1 完了の定義 (DoD)

1. `uv run opshub task create "first task"` が動き、event が `events` テーブルに 1 行 append される
2. `uv run opshub task list --format md` が markdown を返す
3. `uv run opshub workspace generate` が `~/opshub/workspace/generated/tasks/*.md` を作る
4. `uv run opshub projections rebuild` が冪等 (2 回実行で同じ結果)
5. `uv run opshub embeddings status` が `backend=disabled` / `embeddings: 0 rows` を返す (Phase 1 では機能を出さない、ADR-0012)
6. `Embedder` / `VectorStore` Protocol が pyright + mypy --strict を通過 (実装なしでも型整合を CI で freeze)
7. `pyproject.toml` の `[project.optional-dependencies]` が ADR-0001 設計どおりに揃い、`uv tool install --from . opshub` (core only) が ML 依存なしで完了する
8. CI で `uv sync --locked` → `ruff check` → `pyright` → `mypy` → `pytest` が緑
9. `lefthook run pre-commit --all-files` がエラーなしで通る
10. AGENTS.md / CLAUDE.md / architecture.md / principles.md に Phase 1 完了状態が反映されている
11. ADR-0001 〜 0007 および ADR-0012 が `Accepted` に昇格 (handbook ADR 運用に従い `Proposed` → `Accepted` 切替)

## 4. Phase 1 完了時に解消すべき Open Questions

ADR-0001 / ADR-0012 確定により Phase 1 で再確認するだけのもの:

1. **Task runner = `just`** — ADR-0001 で確定済み。Phase 1 step 8 (CI) で動作確認のみ
2. **Embedding 戦略 = Pluggable** — ADR-0012 で確定済み。Phase 1 step 13-14 で interface + config パースが揃う

Phase 1 で骨格のみ用意し、Phase 2 以降で機能化:

3. **Lock の最小実装** — Phase 1 では lock は未実装でよいが、Phase 2 のために `locks` テーブル骨格は schema に入れておく (空のまま運用)
4. **`embeddings` テーブル骨格** — Phase 1 で migration を作り空のまま運用、Phase 4 で活用 (ADR-0012)

Phase 1 内では確定しなくてよいもの (Phase 2-4 に持ち越す):

5. LLM 利用方針 (embedding API 呼び出しと triage/summary 用 API の運用線引き、Phase 2)
6. SaaS token 保管方式 (Phase 3 で connector 着手時)
7. Embedding 具体モデル選定 (Phase 4、ADR-0012 Open Questions 1-2)

## 5. Phase 2 へのつなぎ

Phase 1 完了直後に Phase 2 epic issue を起票 (`phase-issue` skill 利用)。Phase 2 のスコープ:

- `inbox triage` ワークフロー
- `decisions` テーブル + `DecisionRecorded` event
- `work_sessions` + `agent_runs`
- `Lock` の実装 (粒度確定)
- `handoffs` 機構

Phase 2 着手時点で連動して見直すべき docs: principles 7 (Connector Contract) / architecture 4 (データモデル) / architecture 6 (Multi-Agent Coordination)。

## Open Questions (本ドキュメント固有)

1. step 9 の `events` / `embeddings` テーブル schema 詳細 (列定義・index・partitioning)
2. step 13 の `Embedder` / `VectorStore` Protocol を freeze する unit test の粒度 (interface 安定性をどこまで CI で縛るか)
3. step 14 の config パースで `[embedding]` 以外のセクション (`[storage]` / `[workspace]` / `[connectors]`) を Phase 1 から先回りするか
4. step 15 の CLI 引数命名 (`--format` の名前を `--output` にするか、`--json` のような flag にするか)
5. PyPI 公開のタイミング (Phase 1 完了直後か、Phase 2 完了時か、Phase 3 末か)
