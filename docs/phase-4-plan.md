# Phase 4 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: Full Pluggable Embedder + sqlite-vec VectorStore + Recall + Duplicate detection。Briefings 自動生成 / links projection 本実装 / Slack/MS365/Box connector は Phase 4.x で別途。

Phase 4 の目的は **Semantic recall layer** を Phase 1-3 の foundation 上に追加すること。ADR-0012 で Phase 1 から interface freeze していた `Embedder` / `VectorStore` Protocol の具象実装を投入し、自然言語クエリで OpsHub 内の任意 entity (task / decision / inbox_item / source) を semantic 検索できる状態にする。Pluggable 設計 (local sentence-transformers + OpenAI + Voyage の 3 backend) を MVP 範囲で投入し、ADR-0012 §3 の config 駆動 backend 切替を end-to-end で検証する。

## 1. 着手前に解消する TODO

Phase 3 完了時点で Phase 4 着手前に解消が必要な事項は **なし**。Phase 1-3 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `events_table` schema.py 化 / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage) は Phase 4 も全て継承する。

**確定済み事項** (Phase 4 着手前に確定):

1. **Scope の絞り込み** — Phase 4 MVP = full Pluggable Embedder (local + OpenAI + Voyage) + sqlite-vec + recall + 重複検出。Briefing 自動生成 (LLM 呼び出し) / `links` projection 本実装 / Slack/MS365/Box connector は Phase 4.x で別 plan
2. **Embed の trigger** — Phase 4 MVP は **CLI-driven rebuild** のみ (`opshub embeddings rebuild` を operator が定期実行)。Projector hook 経由の event-driven 自動 embed は Phase 4.x (背景 queue 設計を含む)
3. **Default backend** — `[embedding] backend = "disabled"` のままを Phase 4 default として維持 (Phase 1-3 と同じ)。`local` / `openai` / `voyage` は opt-in。理由: `local` 採択は ~500MB-2GB の torch を pull、`openai` / `voyage` は API key + 課金前提のため、CI / 初回 install 体験を軽く保つ
4. **推奨モデル** — `local` = `BAAI/bge-m3` (1024-dim、多言語)、`openai` = `text-embedding-3-small` (1536-dim)、`voyage` = `voyage-3` (1024-dim)。本 plan で確定し ADR-0012 Open Q 1-2 を closeout (PR D1) で解消
5. **`embeddings` table 構造** — Phase 1 migration 0002 で作成済の `embeddings` (`vector BLOB` 列含む) を Phase 4 で metadata-only table に refactor し、`vector` データは新規 `embeddings_vec` virtual table (sqlite-vec backed) に分離。rowid で JOIN (ADR-0012 §5)。Phase 1 schema を尊重しつつ Phase 4 のクエリ性能要件を満たす
6. **重複検出の MVP** — `opshub embeddings find-duplicates [--threshold 0.92] [--entity-type source]` で nearest-neighbor scan を提供 (offline 解析用)。`connector sync` 経路で auto-detect する flow は Phase 4.x

## 1.1 Prep PR (Phase 1-3) で確立した実装契約 (Phase 4 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、event append + projection apply を 1 transaction にまとめる (PR #26 契約)
- 新規 projection は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追記
- 新規 event family は `Phase4Event` discriminated union を作り、`AllEvent` を `TaskEvent | Phase2Event | Phase3Event | Phase4Event` に拡張 (PR B1 で実施)
- 新規 CLI subcommand module は module-level import を `__future__` / `typer` / `typing` / `pathlib` に限定する (M6 cold-start guard が CI で検出)
- 新規 service は失敗 projector の atomicity test を 1 件追加 (PR #26 + Phase 2/3 で確立)
- 新規 projection は rebuild の冪等性テストを 1 件追加
- API embedder / VectorStore は network mock (CI で実 API / 実モデルダウンロードを叩かない、Phase 3 GitHub connector PR #52 と同じ規律)

## 2. Phase 4 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。

### 2.1 Sub-issue A: Embedder + VectorStore implementation (5 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `feat(db): enable sqlite extension loading + embeddings_vec migration` | `db/engine.py` を拡張: `create_engine_for_sqlite` の connect listener で `connection.enable_load_extension(True)` + `sqlite_vec.load(connection)` (sqlite-vec が installed なら、未 install なら no-op + 警告ログ)。migration `0013_create_embeddings_vec_table.py`: 既存 `embeddings` table から `vector BLOB` 列を ALTER TABLE DROP COLUMN で除去 + `embeddings_vec` virtual table `USING vec0(embedding float[N])` を作成 (dim は config の `embedding.dimensions` から決まる、初期 default 1024)。dim 不一致は migration 時に error。pyproject の `[vector]` extras (`sqlite-vec>=0.1`, `numpy>=2.0`) を install 前提に CI で `--extra vector` を追加 | A |
| A2 | `feat(vectors): SqliteVecStore implementation` | `vectors/sqlite_vec_store.py` 新設。`SqliteVecStore` が Phase 1 で確定した `VectorStore` Protocol を実装。`upsert(entity_type, entity_id, vector, model_id, model_version)` で `embeddings` metadata + `embeddings_vec` の 2 table 同期 (rowid 共有)。`query(query_vector, k, filter_sql=None)` は `embeddings_vec MATCH ?` + ORDER BY distance + JOIN `embeddings` で `(entity_type, entity_id, score)` を返す。`delete(entity_type, entity_id)` は 2 table 両方から行削除。in-memory SQLite (`:memory:`) でも動く test fixture を提供 | A |
| A3 | `feat(vectors): LocalSentenceTransformerEmbedder` | `vectors/local_embedder.py` 新設。`LocalSentenceTransformerEmbedder(model_id="BAAI/bge-m3", dim=1024)` が Phase 1 の `Embedder` Protocol を実装。`embed(texts)` で `sentence_transformers.SentenceTransformer` を lazy load (初回 call) し batch encode。`[local-embedding]` extras 必須。test は CI で sentence-transformers を install しない (重すぎるため `pytest.importorskip` で skip)、local dev のみ実行 | A |
| A4 | `feat(vectors): OpenAI + Voyage API embedders` | `vectors/openai_embedder.py` + `vectors/voyage_embedder.py` 新設。`openai` / `voyageai` 公式 SDK を使用 (extras 既存)。token は `core/secrets.get_secret("embedder:openai:api_key")` / `"embedder:voyage:api_key"` から (Phase 3 A6 の keyring + env var override が再利用される)。`OPSHUB_EMBEDDER_OPENAI_API_KEY` / `OPSHUB_EMBEDDER_VOYAGE_API_KEY` env var override 可。test は network mock (respx 不採用、SDK の test mode or unittest.mock.patch) | A |
| A5 | `feat(config): embedding backend resolution + factory` | `vectors/factory.py` 新設。`build_embedder(settings: OpsHubSettings) -> Embedder` が `settings.embedding.backend` ("local" / "openai" / "voyage" / "disabled") から具象 Embedder を返す。`"disabled"` は `NoOpEmbedder` (embed 呼ぶと `ConfigError`)。`build_vector_store(settings, engine) -> VectorStore` 同様 (Phase 4 MVP では SqliteVecStore のみだが factory shape を整える)。`core/config.py` の `EmbeddingSettings` を必要なら拡張 (`dimensions: int` 確定 + backend ごとの sub-section) | A |

### 2.2 Sub-issue B: Embedding orchestration (3 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(domain): embedding events` | `domain/events/embedding.py` 新設。3 event 型: `TextEmbedded(entity_type, entity_id, model_id, model_version, dim)` / `EmbeddingRebuildRequested(scope: Literal["all"] \| str, model_id, model_version)` / `EmbeddingFailed(entity_type, entity_id, model_id, error_message)`。`Phase4Event` discriminated union 新規、既存 `AllEvent` を `TaskEvent \| Phase2Event \| Phase3Event \| Phase4Event` に拡張 | B |
| B2 | `feat(services): embedding service (CLI-driven rebuild)` | `services/embedding_service.py` 新設。`EmbeddingService.embed_pending() -> EmbedResult`: 各 entity_type (task / decision / inbox_item / source) の現在 row を読み、`embeddings` projection と JOIN して「現 config の (model_id, model_version) で未 embed」の行のみ抽出 → `Embedder.embed()` で vector 化 → `VectorStore.upsert()` + `TextEmbedded` event append (atomic via uow_factory)。失敗時は `EmbeddingFailed` event で記録継続 (fail-fast せず、最終 result に failed_count を含める)。冪等: 同 config で 2 回呼んでも diff 分のみ処理 | B |
| B3 | `feat(cli): embeddings rebuild and status` | Phase 1 の `cli/embeddings.py` placeholder を実装に置き換え。`opshub embeddings rebuild [--entity-type X] [--limit N]`: `EmbeddingService.embed_pending` を実行、progress + result を stdout 表示。`opshub embeddings status` を拡張: backend 名 + model_id + 各 entity_type ごとの embedded / pending 件数を表形式で表示 (cli/_render 使用)。`opshub connector auth set embedder:openai` 風の auth subcommand も追加 (Phase 3 cli/connector.py auth pattern を model 化) | B |

### 2.3 Sub-issue C: Recall + duplicate detection (3 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(services): recall service` | `services/recall_service.py` 新設。`RecallService.recall(query_text: str, *, entity_type: str \| None = None, limit: int = 10, state: str \| None = None) -> list[RecallHit]`。手順: ① Embedder.embed_one(query_text) → query vector / ② VectorStore.query(vector, k=limit*2, filter_sql=...) で候補取得 / ③ JOIN with entity table (`tasks` / `decisions` / `inbox_items` / `sources`) で current row + filter (state など) を適用 / ④ score 順 top-N を `RecallHit(entity_type, entity_id, title, snippet, score)` で返す。query embedder と store の `model_id` 不一致は `ConfigError` で警告 ("rebuild required") | C |
| C2 | `feat(cli): recall command` | `cli/recall.py` 新設。`opshub recall "<query>" [--type task\|decision\|inbox_item\|source] [--limit N] [--state X] [--format table\|json\|md]`。`RecallService.recall(...)` を呼び `cli/_render` で出力。embed backend が disabled なら exit 2 + 案内 ("opshub embeddings rebuild + config の backend 切替 を実行") | C |
| C3 | `feat(services): duplicate detection` | `services/duplicate_service.py` 新設。`DuplicateService.find_duplicates(*, entity_type: str = "source", threshold: float = 0.92, limit: int = 100) -> list[DuplicatePair]`: `VectorStore` に対し各 entity の vector で nearest-neighbor 検索を行い、cosine score > threshold の pair を返す (self-match は除外)。`opshub embeddings find-duplicates` CLI も同 PR で追加 (output: pair の id + title + score、`cli/_render` で表形式)。`connector sync` 自動 detect は Phase 4.x で integrate | C |

### 2.4 Sub-issue D: Phase 4 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| D1 | `test: phase 4 end-to-end + docs` | `tests/integration/test_phase4_lifecycle.py` (workstream ごとに分割): mocked Embedder + in-memory SqliteVecStore で `opshub embeddings rebuild` → `opshub recall` → `opshub embeddings find-duplicates` の連鎖を CLI 経由で検証。README / AGENTS / CLAUDE / docs/principles.md / docs/architecture.md / docs/repository-structure.md に Phase 4 完了状態反映 (principles §9 で Phase 4 = ✅ Complete、architecture §2.6 を Phase 4 実装済に更新)。**ADR-0012 (Embedding Strategy) を refine**: §Open Questions 1-2 (default backend / model 選定) を解消 (本 plan §1 確定事項を ADR に moved)、Validation セクション追加 | D |

= 合計 **12 PR** (A 5 + B 3 + C 3 + D 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 (sqlite-vec migration) + A3 (local embedder) + A4 (API embedders) + B1 (events)  → 4 並列 (drive 並列度上限)
Wave 2: A2 (VectorStore、A1 依存)
Wave 3: A5 (factory、A2 + A3 + A4 依存)
Wave 4: B2 (EmbeddingService、A5 + B1 依存)
Wave 5: B3 (CLI、B2 依存) + C1 (RecallService、A5 依存) + C3 (DuplicateService、A5 依存)  → 3 並列
Wave 6: C2 (recall CLI、C1 依存)
Wave 7: D1 (closeout、全 sub-issue 依存)
```

= 7 wave。Wave 1 が 4 並列の頭打ち (drive `min(4, wave 内タスク数)`)。

**論理グルーピング** (sub-issue / milestone 候補):

- **Sub-issue A: Embedder + VectorStore impl** (PR A1-A5 / 5 PR)
- **Sub-issue B: Embedding orchestration** (PR B1-B3 / 3 PR)
- **Sub-issue C: Recall + duplicate detection** (PR C1-C3 / 3 PR)
- **Sub-issue D: Phase 4 closeout** (PR D1 / 1 PR)

## 3. Phase 4 完了の定義 (DoD)

### 機能 DoD

1. `opshub connector auth set embedder:openai` (or env var `OPSHUB_EMBEDDER_OPENAI_API_KEY`) で API key を keyring 保存
2. `~/.config/opshub/config.toml` の `[embedding] backend = "local"` / `"openai"` / `"voyage"` を切替えると、対応する Embedder が `build_embedder(settings)` で生成される
3. `opshub embeddings rebuild` で task / decision / inbox_item / source の summary が embed され、`embeddings` projection + `embeddings_vec` virtual table に persist
4. 同 rebuild を 2 回実行しても 2 回目は 0 件処理 (current `(model_id, model_version)` で既 embed のため skip = 冪等)
5. `opshub embeddings status` が backend + model_id + 各 entity_type の embedded / pending 件数を表示
6. `opshub recall "<japanese or english query>"` が semantic 検索結果を `(entity_type, entity_id, title, score)` で表示
7. `opshub recall --type task --state active "..."` で SQL filter と hybrid に動く
8. `opshub embeddings find-duplicates --threshold 0.92 --entity-type source` で類似 source pair を列挙
9. `opshub projections rebuild` が `embeddings` projection も含めて冪等
10. backend を `local` → `openai` に切替えた直後の `recall` は ConfigError + "rebuild required" 案内 (model_id 不一致検出)

### 品質 DoD

1. CI で `uv sync --locked --extra dev --extra vector --extra connectors-github` → `ruff` → `pyright` → `mypy src tests` → `pytest` 緑
2. `lefthook run pre-commit --all-files` 緑
3. cold start `time opshub --help` ≤ 300ms 維持 (M6 静的検査が `cli/recall.py` / `cli/embeddings.py` への heavy import を CI で検出)
4. 新規 service (EmbeddingService / RecallService / DuplicateService) が atomicity test を持つ (embed の partial 失敗が `EmbeddingFailed` で記録され projection は consistent に保たれることを検証)
5. event-sourced replay: `projections rebuild` が `embeddings` を含めて idempotent
6. API embedder (OpenAI / Voyage) は CI で実 API を叩かない (network mock)
7. local embedder (sentence-transformers) は CI で skip (`pytest.importorskip`)、local dev でのみ実行
8. Pluggable 設計が動作: backend 切替 → 同一 query で異なる embed → 検索結果の `model_id` が config と一致することをテストで pinning

### ドキュメント DoD

1. README に Phase 4 command 一覧 (`opshub embeddings rebuild / status / find-duplicates`、`opshub recall`、`opshub connector auth set embedder:*`) 追記
2. AGENTS.md / CLAUDE.md / docs/principles.md / docs/architecture.md / docs/repository-structure.md に Phase 4 完了状態反映 (principles §9 で Phase 4 = ✅ Complete、architecture §2.6 を "実装済 (local + OpenAI + Voyage)" に更新)
3. **ADR-0012 (Embedding Strategy) refine**: §Open Questions 1-2 を解消 (default backend / model 確定)、Validation セクション追加 (Phase 4 で contract が検証された旨)
4. docs/repository-structure.md の Phase 4 ファイル annotation を `[P4]` → `[P1+2+3+4]` (完了済) に更新

## 4. Phase 4 完了時に解消する Open Questions

着手早期に確定:

1. **embedding dimension の合意点** — Phase 4 default は `local=BAAI/bge-m3` の 1024-dim を採用 (`embeddings_vec` を `float[1024]` で作成)。OpenAI 1536-dim / Voyage 1024-dim 切替時は migration もしくは別 virtual table が必要 → 解決策: backend ごとに別 vec0 table (`embeddings_vec_local` / `embeddings_vec_openai` / `embeddings_vec_voyage`) を作成し、現 active backend のみ insert/query する。複数 backend 並列保持は Phase 4.x

Phase 4 内では確定しなくてよい (Phase 4.x / 5 持ち越し):

1. **Event-driven 自動 embed** — Phase 4 MVP は CLI-driven rebuild のみ。Projector hook + 背景 queue は Phase 4.x
2. **Briefing 自動生成** — LLM 呼び出しは Phase 4.x で別 ADR (LLM 利用方針、principles.md Open Q #1 と合わせて確定)
3. **`links` projection 本実装** — Phase 3 の `inbox_items.source_ref` 簡易 link で実用可、Phase 4.x で graph queries が必要になったら起こす
4. **Duplicate detection を connector sync に integrate** — Phase 4 MVP は CLI-driven scan のみ、auto-detect (sync 中に近傍検索 → `SourceLikelyDuplicate` event 発行) は Phase 4.x
5. **Slack / Microsoft 365 / Box connector** — Phase 3.x で並行 (Phase 4 とは独立 work stream)

## 5. Issue / PR 戦略

Phase 4 は **1 epic + 4 sub-issues + 12 PR** で管理する (Phase 3 と同じ階層構造)。

### 5.1 Tracking Structure

- **Epic issue**: `Phase 4: Semantic recall layer` — 4 sub-issue リンク + 全体 DoD + Phase 5 outlook
- **Sub-issue A**: `Phase 4 — Sub A: Embedder + VectorStore implementation` — PR A1-A5
- **Sub-issue B**: `Phase 4 — Sub B: Embedding orchestration` — PR B1-B3
- **Sub-issue C**: `Phase 4 — Sub C: Recall + duplicate detection` — PR C1-C3
- **Sub-issue D**: `Phase 4 — Sub D: Closeout` — PR D1

各 sub-issue は `Refs <epic>`、各 PR は `Refs <sub-issue>` を本文に含める。

### 5.2 PR 戦略

- **1 step = 1 PR = 1 commit (squash 後)**
- PR タイトル = commit subject
- PR body: `Refs #<sub-issue>` + Summary + Test plan
- マージ方法: **squash merge のみ**
- Wave 内並列 PR は rebase + `--force-with-lease` で衝突解消 (Phase 1-3 で確立)

### 5.3 Milestone (任意)

| Milestone | PR | Sub-issue |
|---|---|---|
| Phase 4: Vector Foundation | A1-A5 | A |
| Phase 4: Embedding Orchestration | B1-B3 | B |
| Phase 4: Recall + Duplicates | C1-C3 | C |
| Phase 4: Closeout | D1 | D |

## 6. Phase 5 outlook

Phase 4 完了直後に Phase 5 epic を起票 (`phase-issue` skill 利用)。Phase 5 / 4.x のスコープ候補:

- **Briefing 自動生成** (LLM 呼び出し): 日次 / 週次 で task + decision + source を要約した markdown briefing を生成。LLM 利用方針 ADR を本 phase で起票 (principles.md Open Q #1 解消)
- **`links` projection 本実装**: Phase 3 の `inbox_items.source_ref` 簡易 link を、`SourceReferenced` event を消費する `links_table` ベースに昇格。graph queries CLI (`opshub graph related <id>`) 提供
- **Event-driven 自動 embed**: Phase 4 MVP の CLI-driven を、projector hook + 背景 queue で transparent 化
- **Duplicate detection を connector sync に integrate**: SourceObserved 時に近傍検索 → `SourceLikelyDuplicate` event を auto-emit
- **Slack / Microsoft 365 / Box connector** (Phase 3.x): Phase 3 の framework + ADR-0010 Accepted contract を再利用して順次追加
- **Multi-machine sync** (principles.md Open Q #5): SQLite 同期 (litestream / Turso / Cloudflare D1) を導入するか、export/import CLI で済ますか

Phase 4.x / 5 着手時に連動して見直すべき docs: principles.md §1 (Local-first) / §6 (External Content Min) / ADR-0005 / ADR-0012 / ADR-0010。

## Open Questions (本ドキュメント固有)

1. `embeddings_vec_<backend>` 命名 vs 単一 `embeddings_vec` + dim 制約 (本 plan は backend ごと別 table、A1 着手前に再確認)
2. `EmbeddingService.embed_pending` の batch size default (案: 32 — sentence-transformers 推奨、OpenAI API は 100 まで OK、PR B2 着手前に benchmark)
3. `RecallService` の score 正規化 (sqlite-vec は L2 distance を返す; cosine similarity に変換するか raw のまま返すか。PR C1 で確定)
4. `opshub recall` の output snippet (title のみ vs summary 先頭 N 文字、Phase 3 `cli/_render` 共通モジュールで decision)
5. `model_id` 不一致時の挙動 (本 plan は ConfigError + "rebuild required" 案内。`recall` を fail-fast にするか warn して進めるか、PR C1 で確定)
6. duplicate detection の `threshold` default 値 (本 plan は 0.92 暫定、PR C3 着手時に手元データで tuning)
