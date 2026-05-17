# Phase 3 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: MVP (framework + GitHub + workspace file ingest)。Slack / Microsoft 365 / Box の具象 connector は Phase 3.x で順次追加する (Phase 2 epic outlook の「順次着手、全部入りは強制しない」方針に沿う)。

Phase 3 の目的は **Connector layer + Workspace ingest** を Phase 2 の coordination foundation 上に追加すること。外部 SaaS (Phase 3 MVP では GitHub) からの差分取り込みと、人手記述された `workspace/{inbox}/*.md` の event 化を実現し、Phase 4 (Semantic recall) への入力素材を揃える。

## 1. 着手前に解消する TODO

Phase 2 完了時点で Phase 3 着手前に解消が必要な事項は **なし**。Phase 1+2 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `events_table` schema.py 化 / cli/* import whitelist の M6 検査 / `cli/_render` 共通モジュール) は Phase 3 も全て継承する。

**確定済み事項** (Phase 3 着手前に確定):

1. **Scope の絞り込み** — Phase 3 MVP は **GitHub connector のみ**。Slack / MS365 / Box は Phase 3.x で別 plan / epic を起こす
2. **Connector の実行 trigger** — Phase 3 は CLI on-demand のみ (`opshub connector sync github`)。scheduler / daemon は Phase 3.x 以降 (cron / systemd timer / launchd 等の議論を含む)
3. **File ingest 対象ディレクトリ** — Phase 3 は `workspace/inbox/*.md` のみ。`plans/` / `notes/` は人手リファレンス用途で event 化しない (architecture §7 で曖昧だった部分を本 plan で確定)
4. **Connector contract の Accept 昇格タイミング** — ADR-0010 (Connector Contract、現状 Proposed) は **sub-issue D Phase 3 closeout で Accept に昇格** する (GitHub connector 実装で contract が検証された後)

## 1.1 Prep PR (Phase 1+2) で確立した実装契約 (Phase 3 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、`with self._uow_factory() as conn: self._store.append(event, conn); self._projector.apply(event, conn)` の atomic pattern を使う
- 新規 projection は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追加する。`_PersistingProjector` と `projections rebuild` が自動で拾う
- 新規 event family は `Phase3Event` discriminated union を作り、`AllEvent` を `TaskEvent | Phase2Event | Phase3Event` に拡張する (PR A1 で実施)
- 新規 CLI subcommand module (`cli/connector.py` 等) は module-level import を `__future__` / `typer` / `typing` / `pathlib` に限定する (cold-start regression guard `tests/integration/test_cli_imports.py` が CI で検出)
- 新規 service は失敗 projector を inject する atomicity test を 1 件追加する (event 永続化と projection 反映が rollback 揃いになることを保証、PR #26 と同 pattern)
- 新規 projection は rebuild の冪等性テストを 1 件追加する (`tests/integration/test_projections_rebuild.py` 拡張)

## 2. Phase 3 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。

### 2.1 Sub-issue A: Connector framework foundation (6 PR、順次)

framework は後続 sub-issue B + C の前提。step 1-6 は順次着手 (互いに依存)。

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `feat(domain): source and connector events` | `domain/events/{source,connector}.py` 新設。`SourceObserved` / `SourceReferenced` / `ConnectorSyncStarted` / `ConnectorSyncCompleted` / `ConnectorSyncFailed` の 5 event 型。`Phase3Event` discriminated union 新規、既存 `AllEvent` を `TaskEvent \| Phase2Event \| Phase3Event` に拡張 | A |
| A2 | `feat(db): sources and connector_cursors migrations` | migration `0010_create_sources_table.py` (id / connector_name / external_id / source_type / title / url / summary / observed_at / updated_at + `UNIQUE(connector_name, external_id)`) + `0011_create_connector_cursors_table.py` (connector_name PK / cursor_value TEXT / updated_at / last_synced_at)。`projections/sources.py` / `projections/connector_cursors.py` に Table stub 追加 (reducer は A3 で) | A |
| A3 | `feat(projections): sources and connector_cursors` | reducer + registry 登録。`SourcesProjection` (SourceObserved → upsert by `(connector_name, external_id)`)、`ConnectorCursorsProjection` (ConnectorSyncStarted → upsert row、ConnectorSyncCompleted → update cursor_value + last_synced_at) | A |
| A4 | `feat(services): source service` | `services/source_service.py`。`observe(connector_name, external_id, source_type, title, url, summary)` → `SourceObserved` event + `InboxService.enqueue(summary=..., source_ref=f"{connector_name}:{external_id}")` を **同一 transaction** で連鎖 (uow_factory 経由)。`cursor_get(connector_name) -> str \| None` / `cursor_set(connector_name, value)` を提供 (sync run の境界で connector が呼ぶ) | A |
| A5 | `feat(connectors): base protocol and registry` | `connectors/__init__.py` / `connectors/base.py` (`Connector` Protocol with `name: str` / `sync(context: ConnectorContext) -> SyncResult`) + `connectors/context.py` (`ConnectorContext` dataclass: `source_service` / `secrets` / `cursor_value` / `logger`)。`cli/connector.py` (`opshub connector list` / `sync <name>` の placeholder。具象 connector ゼロでも動く、registry が空ならエラーメッセージ) | A |
| A6 | `feat(core): secrets storage` + ADR-0014 | **ADR-0014 (SaaS Token Storage)** を新規起票し Accept。`keyring` library 採用 (cross-platform、OS keychain backed、`keyring>=24`)。`core/secrets.py` (`get_secret(key)` / `set_secret(key, value)` / `delete_secret(key)`) + `pyproject.toml` の新規 `[project.optional-dependencies]` `secrets = ["keyring>=24"]`。test は in-memory keyring backend で | A |

### 2.2 Sub-issue B: GitHub connector (3 PR、A 完了後)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(connectors-github): auth resolution` | `connectors/github/__init__.py` / `connectors/github/auth.py`。`get_github_token()` が `core/secrets.get_secret("connector:github:pat")` から PAT を取得 (環境変数 `OPSHUB_GITHUB_PAT` で開発時 override 可)。pyproject 既存 `[connectors-github]` extras は変更なし (httpx + PyGithub は既出)。`opshub connector auth set github` CLI で keyring に保存する subcommand 追加 | B |
| B2 | `feat(connectors-github): fetch primitives` | `connectors/github/api.py`。httpx (PyGithub より軽い + 型整合性高) で Issues / PRs / Notifications を fetch する thin wrapper。incremental sync 用に `since` パラメータ / `Etag` header を扱う。test は `respx` または `httpx.MockTransport` で API モック | B |
| B3 | `feat(connectors-github): connector implementation + integration test` | `connectors/github/connector.py`。`GitHubConnector` が `Connector` Protocol を実装。`sync(context)` で cursor (= last `updated_at`) を基準に差分 fetch → 各 Issue/PR/Notification を `source_service.observe(...)` で SourceObserved + Inbox enqueue。`cli/connector.py` の registry に登録 (`opshub connector sync github` で実行)。`tests/integration/test_github_connector_lifecycle.py` で mocked GitHub API end-to-end | B |

### 2.3 Sub-issue C: Workspace file ingest (3 PR、A 完了後、B と並列可)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(markdown): inbox ingest parser` | `markdown/ingest.py`。`parse_inbox_file(path: Path) -> InboxItemDraft` (front-matter `summary` + body を抽出。front-matter なしの場合は first heading or filename を summary に)。`compute_file_hash(path) -> str` (内容ベース冪等チェック用) | C |
| C2 | `feat(workspace): file ingest service + state tracking` | `services/file_ingest_service.py`。`ingest_inbox_dir(workspace_root: Path) -> IngestResult`。新規ファイル (hash が `ingested_files` projection になければ) を `InboxService.enqueue` 経由で event 化 + `FileIngested` event を append し `ingested_files` projection を更新。migration `0012_create_ingested_files_table.py` (file_path / content_hash / ingested_at)。同一内容のファイル再 ingest は idempotent (no-op) | C |
| C3 | `feat(cli): workspace ingest command` | `cli/workspace.py` 拡張: `opshub workspace ingest [--dry-run]` で `IngestService.ingest_inbox_dir(settings.workspace.root)` を実行。stdout に enqueue した item 数 + skipped (既知 hash) 数を表示。integration test 追加 (workspace/inbox/ にファイル作成 → ingest → inbox_items + ingested_files 反映 → 同ファイル再 ingest で skip 確認) | C |

### 2.4 Sub-issue D: Phase 3 closeout (1 PR、B + C 完了後)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| D1 | `test: phase 3 end-to-end + docs` | `tests/integration/test_phase3_lifecycle.py` (sub-issue ごとに分割、conftest.py の `isolated_env` 流用)。mocked GitHub API でフル sync → SourceObserved + ItemEnqueued → triage → task の連鎖 を 1 test 関数で。workspace ingest 経路も別 test 関数で。README / AGENTS.md / CLAUDE.md / docs/principles.md / docs/architecture.md / docs/repository-structure.md に Phase 3 完了状態を反映 (principles §9 で Phase 3 = ✅ Complete、architecture §1 図に Connector Layer を **Phase 3 実装済** マーク)。**ADR-0010 (Connector Contract) を Proposed → Accepted に昇格** (sub-issue B で contract が検証されたため) | D |

= 合計 **13 PR** (A 6 + B 3 + C 3 + D 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 (events)
Wave 2: A2 (migrations、A1 依存)
Wave 3: A3 (projections、A2 依存)
Wave 4: A4 (source service、A3 依存) + A5 (connector base、A3 依存) + A6 (secrets、A1 依存) → 3 並列
Wave 5: B1 (github auth、A6 依存) + C1 (ingest parser、A1-A4 依存) → 2 並列
Wave 6: B2 (github fetch、B1 依存) + C2 (ingest service、C1 + A4 依存) → 2 並列
Wave 7: B3 (github connector、A5 + B2 依存) + C3 (workspace ingest CLI、C2 依存) → 2 並列
Wave 8: D1 (E2E + docs、全 sub-issue 依存)
```

**論理グルーピング** (sub-issue / milestone 候補):

- **Sub-issue A: Connector Framework Foundation** (PR A1-A6 / 6 PR)
- **Sub-issue B: GitHub Connector** (PR B1-B3 / 3 PR)
- **Sub-issue C: Workspace File Ingest** (PR C1-C3 / 3 PR)
- **Sub-issue D: Phase 3 Closeout** (PR D1 / 1 PR)

Phase 1+2 の経験から、Wave 内並列 (Wave 4 ×3、Wave 5/6/7 ×2 ずつ) で `cli/app.py` / `projections/registry.py` / `cli/_wiring.py` への add_typer/add_projection 衝突は rebase + `--force-with-lease` で解消可能 (Phase 2 step 14-16 + Wave 3 で実証済)。

## 3. Phase 3 完了の定義 (DoD)

### 機能 DoD

1. `opshub connector auth set github` で PAT を keyring に保存できる
2. `opshub connector sync github` で GitHub Issues / PRs / Notifications を差分取得し、`SourceObserved` + `ItemEnqueued` event が **同一 transaction** で連鎖する
3. 同 sync を 2 回実行しても重複 enqueue されない (cursor 機能 + SourceObserved の `UNIQUE(connector_name, external_id)` で保証)
4. `workspace/inbox/*.md` に手書きで .md ファイルを置き `opshub workspace ingest` を実行すると、各ファイルが ItemEnqueued される
5. 同 ingest を 2 回実行しても重複 enqueue されない (`ingested_files` projection で content_hash 追跡)
6. `opshub inbox list` が GitHub 起点 + ファイル起点 の inbox item 両方を表示
7. `opshub inbox triage <id> --to-task` で SourceReferenced event が記録され、task と source の link が `links_table` か projection に残る (Phase 3 では `inbox_items.source_ref` 列で link を持つ簡易実装で可。`links_table` は Phase 4 以降)
8. `opshub workspace generate` を 2 回実行しても同じ markdown (冪等、Phase 3 entity 含めて)
9. `opshub projections rebuild` が Phase 3 projection 含めて冪等

### 品質 DoD

1. CI で `uv sync --locked` → `ruff` → `pyright` → `mypy src tests` → `pytest` が緑
2. `lefthook run pre-commit --all-files` が緑
3. cold start `time opshub --help` ≤ 300ms 維持 (M6 静的検査が cli/connector.py / cli/workspace.py への heavy import を CI で検出)
4. 新規 service (SourceService / FileIngestService) が atomicity test を持つ (failing-projector → event + projection 両方 rollback)
5. event-sourced replay 性: `projections rebuild` が Phase 3 projection についても idempotent (`tests/integration/test_projections_rebuild.py` に追加)
6. GitHub API 呼び出しは network mock (respx / httpx_mock) で覆われ、CI で実 API を叩かない
7. Connector contract が GitHub 実装で検証されたことを ADR-0010 に Accept 昇格時に明記

### ドキュメント DoD

1. README に Phase 3 command 一覧 (`opshub connector auth set / sync / list`、`opshub workspace ingest`) を追記
2. AGENTS.md / CLAUDE.md / docs/principles.md / docs/architecture.md に Phase 3 完了状態を反映 (principles §9 で Phase 3 = ✅ Complete、architecture §1 図の Connector Layer を 実装済 マーク)
3. **ADR-0010 (Connector Contract) を Proposed → Accepted に昇格** (sub-issue D PR 同梱)
4. **ADR-0014 (SaaS Token Storage) を新規起票し Accept** (sub-issue A PR A6 同梱)
5. docs/repository-structure.md の Phase 3 ファイル annotation を `[P3]` → `[P1+2+3]` (完了済) に更新

## 4. Phase 3 完了時に解消する Open Questions

着手早期に確定:

1. **Token storage 方式** — ADR-0014 で `keyring` を採択 (PR A6 で確定)。`OPSHUB_GITHUB_PAT` env var override は開発時 fallback (production は keyring 必須)

Phase 3 内では確定しなくてよい (Phase 3.x / 4 持ち越し):

1. **Slack / MS365 / Box connector の優先順位** — Phase 3 完了後に別 plan
2. **scheduler / daemon 化** — Phase 3 は CLI on-demand。`opshub connector sync` を `cron` / `systemd timer` / `launchd` で叩く運用を README に書くのみ。in-process scheduler は Phase 4 で再評価
3. **Connector failure / retry semantics** — Phase 3 は単発 sync で fail-fast (ConnectorSyncFailed event 記録のみ、retry は次回 manual sync 任せ)。指数 backoff / dead letter queue は Phase 3.x
4. **`links_table` の物理実装** — Phase 3 は `inbox_items.source_ref` 列で簡易 link。専用 `links` projection は Phase 4 (graph queries 時) に再評価
5. **`plans/` / `notes/` の event 化** — Phase 3 では event 化しない (人手リファレンス用途)。Phase 4 で semantic 索引対象に含めるか別議論

## 5. Issue / PR 戦略

Phase 3 は **1 epic + 4 sub-issues + 13 PR** で管理する (Phase 1 + 2 と異なり、sub-issue 階層を導入)。

### 5.1 Tracking Structure

- **Epic issue**: `Phase 3: Connector layer + Workspace ingest` — 4 sub-issue のリンク + 全体 DoD + Phase 4 outlook
- **Sub-issue A**: `Phase 3 — Sub A: Connector framework foundation` — PR A1-A6 のチェックリスト
- **Sub-issue B**: `Phase 3 — Sub B: GitHub connector` — PR B1-B3 のチェックリスト
- **Sub-issue C**: `Phase 3 — Sub C: Workspace file ingest` — PR C1-C3 のチェックリスト
- **Sub-issue D**: `Phase 3 — Sub D: Closeout` — PR D1 のチェックリスト

各 sub-issue は `Refs <epic>` で epic に参照を張る。各 PR は `Refs <sub-issue>` で sub-issue に参照を張る。

### 5.2 PR 戦略

- **1 step = 1 PR = 1 commit (squash 後)**
- PR タイトル: 該当 commit message と一致
- PR body: `Refs #<sub-issue>` + Summary + Test plan
- マージ方法: **squash merge のみ**
- 各 PR は前 PR (DAG 上の依存元) の merge を待ってから main を base に作成
- Wave 内並列の PR は rebase + `--force-with-lease` で `cli/app.py` 等の追記衝突を解消 (Phase 1+2 で確立した pattern)

### 5.3 Milestone (任意)

GitHub Milestones を使う場合、4 sub-issue を milestone 化:

| Milestone | PR | Sub-issue |
|---|---|---|
| Phase 3: Framework Foundation | A1-A6 | A |
| Phase 3: GitHub Connector | B1-B3 | B |
| Phase 3: Workspace Ingest | C1-C3 | C |
| Phase 3: Closeout | D1 | D |

## 6. Phase 4 へのつなぎ

Phase 3 完了直後に Phase 4 epic を起票 (`phase-issue` skill 利用)。Phase 4 のスコープ:

- Embedding 具象実装 (ADR-0012 の `Embedder` Protocol を `sentence-transformers` / OpenAI / Voyage で実装)
- `sqlite-vec` backed `VectorStore` 実装 (ADR-0012)
- `opshub recall` CLI (hybrid search: vector + SQL filter)
- 重複検出 / briefing 自動生成 (Phase 3 で取り込んだ source の semantic 重複を検出)
- `links` projection の本実装 (Phase 3 の `inbox_items.source_ref` 簡易 link を本格 graph に展開)
- Phase 3.x で残った connector (Slack / MS365 / Box) のうち優先度の高いもの

Phase 4 着手時点で連動して見直すべき docs: principles §6 (External Content Minimization、embedding 対象範囲) / architecture §2.6 (Vector Layer) / ADR-0012 (Embedding Strategy)。

## Open Questions (本ドキュメント固有)

1. `connectors/github/api.py` で httpx と PyGithub のどちらをメインに使うか (本 plan は httpx 推し、軽量 + 型整合性で。PR B2 着手前に再確認)
2. `FileIngested` event の aggregate_id をどう決めるか (案: content_hash の先頭 26 文字、または file_path 由来の deterministic ULID)
3. GitHub Notification の subject (Issue / PR / Discussion / ...) ごとに event_type を分けるか、`SourceObserved.source_type` で表現するか (本 plan は後者推奨)
4. Connector failure 時の `ConnectorSyncFailed` event に error message / stack trace をどこまで残すか (PII / token leak 注意)
5. `opshub workspace ingest --dry-run` 出力フォーマット (どの file が enqueue 候補で、どの file が skip かを表示する shape を確定)
